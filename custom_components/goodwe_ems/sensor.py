"""Senzorii de telemetrie, de preț PZU și de stare a dispecerizării."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfApparentPower,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfReactivePower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DISPATCH_AUTO,
    DISPATCH_CHARGE_GRID,
    DISPATCH_DISCHARGE,
    DISPATCH_HOLD,
    DISPATCH_IDLE,
    DISPATCH_UNAVAILABLE,
    DOMAIN,
)
from .coordinator import GoodweEmsCoordinator
from .dispatch import window_label
from .entity import GoodweEmsEntity
from .pzu_prices import lei_per_kwh, spread
from .readings import LiveData


@dataclass(frozen=True, kw_only=True)
class GoodweSensorDescription(SensorEntityDescription):
    value_fn: Callable[[LiveData], float | int | None]


def _power(key: str, attr: str, **kwargs) -> GoodweSensorDescription:
    return GoodweSensorDescription(
        key=key,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, a=attr: getattr(d, a),
        **kwargs,
    )


def _energy(key: str, attr: str, **kwargs) -> GoodweSensorDescription:
    return GoodweSensorDescription(
        key=key,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        value_fn=lambda d, a=attr: getattr(d, a),
        **kwargs,
    )


DIAG = EntityCategory.DIAGNOSTIC

LIVE_SENSORS: tuple[GoodweSensorDescription, ...] = (
    # Putere
    _power("pv_power", "pv_power", icon="mdi:solar-power"),
    _power("inverter_power", "inverter_power", icon="mdi:sine-wave"),
    _power("ac_active_power", "ac_active_power", icon="mdi:transmission-tower"),
    _power("load_power", "load_power", icon="mdi:home-lightning-bolt"),
    _power("backup_load_power", "backup_load_power", icon="mdi:home-battery"),
    _power("battery_power", "battery_power", icon="mdi:battery-charging"),
    _power("battery2_power", "battery2_power", icon="mdi:battery-charging", entity_registry_enabled_default=False),
    _power("pv1_power", "pv1_power", entity_category=DIAG, entity_registry_enabled_default=False),
    _power("pv2_power", "pv2_power", entity_category=DIAG, entity_registry_enabled_default=False),
    _power("pv3_power", "pv3_power", entity_category=DIAG, entity_registry_enabled_default=False),
    _power("pv4_power", "pv4_power", entity_category=DIAG, entity_registry_enabled_default=False),
    GoodweSensorDescription(
        key="ac_reactive_power",
        device_class=SensorDeviceClass.REACTIVE_POWER,
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=DIAG,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.ac_reactive_power,
    ),
    GoodweSensorDescription(
        key="ac_apparent_power",
        device_class=SensorDeviceClass.APPARENT_POWER,
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=DIAG,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.ac_apparent_power,
    ),
    # Stare baterie
    GoodweSensorDescription(
        key="battery_soc",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.soc,
    ),
    GoodweSensorDescription(
        key="battery_soh",
        icon="mdi:heart-pulse",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=DIAG,
        value_fn=lambda d: d.soh,
    ),
    # Cei trei senzori de mai jos sunt kWh, dar nu energie *acumulată*: sunt
    # cantități stocate, care urcă și coboară. `ENERGY` ar fi fost greșit —
    # Home Assistant îl acceptă doar cu `total`/`total_increasing`, iar o
    # valoare care scade ar fi citită ca resetare de contor și ar strica
    # statisticile. `ENERGY_STORAGE` e clasa pentru „câtă energie e înăuntru
    # acum" și merge cu `measurement`.
    GoodweSensorDescription(
        key="battery_capacity",
        icon="mdi:battery-heart-variant",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=DIAG,
        value_fn=lambda d: d.total_capacity_kwh,
    ),
    GoodweSensorDescription(
        key="battery_charge_allow",
        icon="mdi:battery-arrow-up-outline",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.charge_allow_kwh,
    ),
    GoodweSensorDescription(
        key="battery_discharge_allow",
        icon="mdi:battery-arrow-down-outline",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.discharge_allow_kwh,
    ),
    GoodweSensorDescription(
        key="battery_strings",
        icon="mdi:battery-unknown",
        entity_category=DIAG,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.battery_strings,
    ),
    # Energie
    #
    # Cele trei de mai jos sunt singurele care pot popula tabloul Energy la
    # secțiunile solar și rețea: acolo intră doar kWh acumulați, iar senzorii
    # de putere de mai sus, oricât de corecți, nu sunt eligibili niciodată.
    _energy("pv_energy_total", "pv_energy_total_kwh", icon="mdi:solar-power-variant"),
    _energy("grid_import_energy", "grid_import_energy_kwh", icon="mdi:home-import-outline"),
    _energy("grid_export_energy", "grid_export_energy_kwh", icon="mdi:home-export-outline"),
    _energy("total_charge_energy", "total_charge_energy_kwh", icon="mdi:battery-plus"),
    _energy("total_discharge_energy", "total_discharge_energy_kwh", icon="mdi:battery-minus"),
    _energy("energy_charge", "energy_charge_kwh", entity_registry_enabled_default=False),
    _energy("energy_discharge", "energy_discharge_kwh", entity_registry_enabled_default=False),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: GoodweEmsCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            *(GoodweLiveSensor(coordinator, d) for d in LIVE_SENSORS),
            PzuPriceSensor(coordinator),
            PzuStatSensor(coordinator, "pzu_average_today", lambda s: s.average),
            PzuStatSensor(coordinator, "pzu_min_today", lambda s: s.minimum),
            PzuStatSensor(coordinator, "pzu_max_today", lambda s: s.maximum),
            PzuStatSensor(coordinator, "pzu_spread_today", spread),
            MonthlyWeightedSensor(coordinator),
            DispatchStateSensor(coordinator),
        ]
    )


class GoodweLiveSensor(GoodweEmsEntity, SensorEntity):
    """Un registru de telemetrie, expus ca senzor."""

    entity_description: GoodweSensorDescription

    def __init__(
        self, coordinator: GoodweEmsCoordinator, description: GoodweSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | None:
        data = self.live
        return self.entity_description.value_fn(data) if data else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class _PriceBase(GoodweEmsEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3

    @property
    def series(self):
        return self.coordinator.data.series if self.coordinator.data else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        series = self.series
        currency = series.currency if series else "RON"
        return f"{currency}/kWh"


class PzuPriceSensor(_PriceBase):
    """Prețul intervalului de 15 minute curent."""

    _attr_icon = "mdi:cash"

    def __init__(self, coordinator: GoodweEmsCoordinator) -> None:
        super().__init__(coordinator, "pzu_price")

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
            "sursa": series.source,
            "zi_livrare": series.day.isoformat(),
            "interval": index + 1,
            "intervale_total": len(series.values),
            "pret_mwh": round(series.values[index], 2),
            "moneda": series.currency,
            "actionabil": series.is_actionable(),
            "descarcat_la": series.fetched_at.isoformat(timespec="seconds"),
            "medii_orare_kwh": [round(lei_per_kwh(v), 4) for v in series.hourly_averages()],
            "intervale_kwh": [round(lei_per_kwh(v), 4) for v in series.values],
        }


class PzuStatSensor(_PriceBase):
    """Agregate ale zilei: medie, minim, maxim, amplitudine."""

    _attr_icon = "mdi:chart-line"

    def __init__(self, coordinator: GoodweEmsCoordinator, key: str, fn) -> None:
        super().__init__(coordinator, key)
        self._fn = fn

    @property
    def native_value(self) -> float | None:
        series = self.series
        return lei_per_kwh(self._fn(series)) if series else None


class MonthlyWeightedSensor(GoodweEmsEntity, SensorEntity):
    """Prețul mediu ponderat lunar — baza de decontare pentru prosumatori.

    Nu e media prețurilor orare. Ordinul ANRE 15/2022 folosește media ponderată
    publicată de OPCOM, iar cele două valori diferă numeric.
    """

    _attr_icon = "mdi:calendar-month"
    _attr_native_unit_of_measurement = "RON/MWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: GoodweEmsCoordinator) -> None:
        super().__init__(coordinator, "pzu_monthly_weighted")

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        return data.monthly_weighted[0] if data and data.monthly_weighted else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if not data or not data.monthly_weighted:
            return None
        return {"luna": data.monthly_weighted[1]}


class DispatchStateSensor(GoodweEmsEntity, SensorEntity):
    """Ce face motorul de dispecerizare acum și de ce."""

    _attr_icon = "mdi:calendar-clock"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        DISPATCH_IDLE,
        DISPATCH_AUTO,
        DISPATCH_CHARGE_GRID,
        DISPATCH_DISCHARGE,
        DISPATCH_HOLD,
        DISPATCH_UNAVAILABLE,
    ]

    def __init__(self, coordinator: GoodweEmsCoordinator) -> None:
        super().__init__(coordinator, "dispatch_state")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        return data.decision.state if data and data.decision else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if not data or not data.decision:
            return None
        decision, series = data.decision, data.series
        attrs: dict[str, Any] = {
            "motiv": decision.reason,
            "mod_ems": f"{decision.ems_mode:#06x}",
            "putere_w": decision.power_w,
            "soc": self.coordinator.current_soc(),
        }
        if series is not None:
            attrs["fereastra_incarcare"] = window_label(series, decision.charge_window)
            attrs["fereastra_descarcare"] = window_label(series, decision.discharge_window)
            if decision.charge_price is not None:
                attrs["pret_incarcare_mwh"] = round(decision.charge_price, 2)
            if decision.discharge_price is not None:
                attrs["pret_descarcare_mwh"] = round(decision.discharge_price, 2)
        return attrs
