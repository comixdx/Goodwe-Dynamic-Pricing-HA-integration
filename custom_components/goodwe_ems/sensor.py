"""Inverter telemetry, PZU price and dispatch-state sensors.

The inverter sensors are not enumerated here. The library's sensor definitions
carry id, name, unit and kind, so the entities are generated from
`inverter.sensors()` and only the unit-to-device-class mapping lives in this
file. The hand-written register list this replaced could only ever describe one
inverter family.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from goodwe import Inverter, Sensor, SensorKind
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DISPATCH_AUTO,
    DISPATCH_CHARGE_GRID,
    DISPATCH_DISCHARGE,
    DISPATCH_HOLD,
    DISPATCH_IDLE,
    DISPATCH_UNAVAILABLE,
    DOMAIN,
)
from .coordinator import GoodweEmsConfigEntry, GoodweEmsCoordinator
from .dispatch import window_label
from .entity import GoodweEmsEntity
from .pzu_prices import lei_per_kwh, spread

# The coordinator handles all data updates, so parallel updates are not needed.
PARALLEL_UPDATES = 0

BATTERY_SOC = "battery_soc"

# Sensors reset to 0 at midnight. A PV-only inverter goes dead after sunset and
# resets its "_day" counters when it wakes up, which would otherwise show as a
# reset at sunrise rather than at midnight. With a battery attached the inverter
# stays awake and resets them itself.
DAILY_RESET = ["e_day", "e_load_day"]

# Everything else is filed under diagnostics, so the device page opens on the
# handful of figures people actually look at.
_MAIN_SENSORS = (
    "ppv",
    "house_consumption",
    "active_power",
    "battery_soc",
    "pbattery1",
    "e_day",
    "e_total",
    "meter_e_total_exp",
    "meter_e_total_imp",
    "e_bat_charge_total",
    "e_bat_discharge_total",
)

_ICONS: dict[SensorKind, str] = {
    SensorKind.PV: "mdi:solar-power",
    SensorKind.AC: "mdi:power-plug-outline",
    SensorKind.UPS: "mdi:power-plug-off-outline",
    SensorKind.BAT: "mdi:battery-high",
    SensorKind.GRID: "mdi:transmission-tower",
}


@dataclass(frozen=True)
class GoodweSensorEntityDescription(SensorEntityDescription):
    """Describes a GoodWe inverter sensor."""

    value: Callable[[GoodweEmsCoordinator, str], Any] = lambda coordinator, sensor: (
        coordinator.sensor_value(sensor)
    )
    available: Callable[[GoodweEmsCoordinator], bool] = lambda coordinator: (
        coordinator.last_update_success
    )


_DESCRIPTIONS: dict[str, GoodweSensorEntityDescription] = {
    "A": GoodweSensorEntityDescription(
        key="A",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
    ),
    "V": GoodweSensorEntityDescription(
        key="V",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
    ),
    "W": GoodweSensorEntityDescription(
        key="W",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    "kWh": GoodweSensorEntityDescription(
        key="kWh",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value=lambda coordinator, sensor: coordinator.total_sensor_value(sensor),
        available=lambda coordinator: coordinator.data is not None,
    ),
    "VA": GoodweSensorEntityDescription(
        key="VA",
        device_class=SensorDeviceClass.APPARENT_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        entity_registry_enabled_default=False,
    ),
    "var": GoodweSensorEntityDescription(
        key="var",
        device_class=SensorDeviceClass.REACTIVE_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        entity_registry_enabled_default=False,
    ),
    "C": GoodweSensorEntityDescription(
        key="C",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    "Hz": GoodweSensorEntityDescription(
        key="Hz",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
    ),
    "h": GoodweSensorEntityDescription(
        key="h",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.HOURS,
        entity_registry_enabled_default=False,
    ),
    "%": GoodweSensorEntityDescription(
        key="%",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
}
DIAG_SENSOR = GoodweSensorEntityDescription(
    key="_",
    state_class=SensorStateClass.MEASUREMENT,
)
TEXT_SENSOR = GoodweSensorEntityDescription(key="text")


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: GoodweEmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors from a config entry."""
    data = config_entry.runtime_data
    coordinator = data.coordinator
    device_info = data.device_info

    entities: list[SensorEntity] = [
        InverterSensor(coordinator, device_info, data.inverter, sensor)
        for sensor in data.inverter.sensors()
        if not sensor.id_.startswith("xx")
    ]

    entities.extend(
        [
            PzuPriceSensor(coordinator, device_info),
            PzuStatSensor(coordinator, device_info, "pzu_average_today", lambda s: s.average),
            PzuStatSensor(coordinator, device_info, "pzu_min_today", lambda s: s.minimum),
            PzuStatSensor(coordinator, device_info, "pzu_max_today", lambda s: s.maximum),
            PzuStatSensor(coordinator, device_info, "pzu_spread_today", spread),
            MonthlyWeightedSensor(coordinator, device_info),
            DispatchStateSensor(coordinator, device_info),
        ]
    )

    async_add_entities(entities)


class InverterSensor(CoordinatorEntity[GoodweEmsCoordinator], SensorEntity):
    """One sensor as the library defines it."""

    _attr_has_entity_name = True
    entity_description: GoodweSensorEntityDescription

    def __init__(
        self,
        coordinator: GoodweEmsCoordinator,
        device_info: DeviceInfo,
        inverter: Inverter,
        sensor: Sensor,
    ) -> None:
        """Initialize an inverter sensor."""
        super().__init__(coordinator)
        self._attr_name = sensor.name.strip()
        self._attr_unique_id = f"{DOMAIN}-{sensor.id_}-{inverter.serial_number}"
        self._attr_device_info = device_info
        self._attr_entity_category = (
            EntityCategory.DIAGNOSTIC if sensor.id_ not in _MAIN_SENSORS else None
        )
        try:
            self.entity_description = _DESCRIPTIONS[sensor.unit]
        except KeyError:
            if "Enum" in type(sensor).__name__ or sensor.id_ == "timestamp":
                self.entity_description = TEXT_SENSOR
            else:
                self.entity_description = DIAG_SENSOR
                self._attr_native_unit_of_measurement = sensor.unit
        self._attr_icon = _ICONS.get(sensor.kind)
        if sensor.id_ == BATTERY_SOC:
            self._attr_device_class = SensorDeviceClass.BATTERY
        self._sensor = sensor
        self._stop_reset: Callable[[], None] | None = None

    @property
    def native_value(self) -> StateType | date | datetime | Decimal:
        """Return the value reported by the sensor."""
        return self.entity_description.value(self.coordinator, self._sensor.id_)

    @property
    def available(self) -> bool:
        """Return whether the entity is available.

        Delegated to the description, because some sensors (energy produced
        today) should stay available even while a PV-only inverter is offline
        overnight and most sensors genuinely are not.
        """
        return self.entity_description.available(self.coordinator)

    @callback
    def async_reset(self, now) -> None:
        """Reset the value back to 0 at midnight."""
        if not self.coordinator.last_update_success:
            self.coordinator.reset_sensor(self._sensor.id_)
            self.async_write_ha_state()
        next_midnight = dt_util.start_of_local_day(
            dt_util.now() + timedelta(days=1, minutes=1)
        )
        self._stop_reset = async_track_point_in_time(
            self.hass, self.async_reset, next_midnight
        )

    async def async_added_to_hass(self) -> None:
        """Schedule the midnight reset task."""
        if self._sensor.id_ in DAILY_RESET:
            next_midnight = dt_util.start_of_local_day(
                dt_util.now() + timedelta(days=1)
            )
            self._stop_reset = async_track_point_in_time(
                self.hass, self.async_reset, next_midnight
            )
        await super().async_added_to_hass()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the midnight reset task."""
        if self._sensor.id_ in DAILY_RESET and self._stop_reset is not None:
            self._stop_reset()
        await super().async_will_remove_from_hass()


class _PriceBase(GoodweEmsEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3

    @property
    def series(self):
        return self.coordinator.series

    @property
    def native_unit_of_measurement(self) -> str | None:
        series = self.series
        currency = series.currency if series else "RON"
        return f"{currency}/kWh"


class PzuPriceSensor(_PriceBase):
    """Price of the current 15-minute interval."""

    def __init__(
        self, coordinator: GoodweEmsCoordinator, device_info: DeviceInfo
    ) -> None:
        super().__init__(coordinator, device_info, "pzu_price")

    @property
    def native_value(self) -> float | None:
        series = self.series
        return lei_per_kwh(series.value_at()) if series else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        series = self.series
        if series is None:
            return None
        index = series.index_at()
        return {
            "source": series.source,
            "delivery_day": series.day.isoformat(),
            "interval": index + 1,
            "intervals_total": len(series.values),
            "price_mwh": round(series.values[index], 2),
            "currency": series.currency,
            "actionable": series.is_actionable(),
            "fetched_at": series.fetched_at.isoformat(timespec="seconds"),
            "hourly_averages_kwh": [
                round(lei_per_kwh(v), 4) for v in series.hourly_averages()
            ],
            "intervals_kwh": [round(lei_per_kwh(v), 4) for v in series.values],
        }


class PzuStatSensor(_PriceBase):
    """Daily aggregates: average, minimum, maximum, spread."""

    def __init__(
        self,
        coordinator: GoodweEmsCoordinator,
        device_info: DeviceInfo,
        key: str,
        fn: Callable[[Any], float],
    ) -> None:
        super().__init__(coordinator, device_info, key)
        self._fn = fn

    @property
    def native_value(self) -> float | None:
        series = self.series
        return lei_per_kwh(self._fn(series)) if series else None


class MonthlyWeightedSensor(GoodweEmsEntity, SensorEntity):
    """Monthly weighted average price -- the prosumer settlement basis.

    Not the average of the hourly prices. ANRE order 15/2022 uses the weighted
    average OPCOM publishes, and the two differ numerically.
    """

    _attr_native_unit_of_measurement = "RON/MWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(
        self, coordinator: GoodweEmsCoordinator, device_info: DeviceInfo
    ) -> None:
        super().__init__(coordinator, device_info, "pzu_monthly_weighted")

    @property
    def native_value(self) -> float | None:
        weighted = self.coordinator.monthly_weighted
        return weighted[0] if weighted else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        weighted = self.coordinator.monthly_weighted
        return {"month": weighted[1]} if weighted else None


class DispatchStateSensor(GoodweEmsEntity, SensorEntity):
    """What the dispatch engine is doing right now, and why."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        DISPATCH_IDLE,
        DISPATCH_AUTO,
        DISPATCH_CHARGE_GRID,
        DISPATCH_DISCHARGE,
        DISPATCH_HOLD,
        DISPATCH_UNAVAILABLE,
    ]

    def __init__(
        self, coordinator: GoodweEmsCoordinator, device_info: DeviceInfo
    ) -> None:
        super().__init__(coordinator, device_info, "dispatch_state")

    @property
    def native_value(self) -> str | None:
        decision = self.coordinator.decision
        return decision.state if decision else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        decision = self.coordinator.decision
        if decision is None:
            return None
        series = self.coordinator.series
        attrs: dict[str, Any] = {
            "reason": decision.reason,
            "ems_mode": decision.ems_mode.name.lower(),
            "power_w": decision.power_w,
            "soc": self.coordinator.current_soc(),
        }
        if series is not None:
            attrs["charge_window"] = window_label(series, decision.charge_window)
            attrs["discharge_window"] = window_label(series, decision.discharge_window)
            if decision.charge_price is not None:
                attrs["charge_price_mwh"] = round(decision.charge_price, 2)
            if decision.discharge_price is not None:
                attrs["discharge_price_mwh"] = round(decision.discharge_price, 2)
        return attrs
