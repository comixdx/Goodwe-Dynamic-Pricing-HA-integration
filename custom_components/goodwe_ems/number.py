"""Parametrii numerici de control."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    BATTERY_POWER_MAX,
    DOMAIN,
    EMS_POWER_MAX,
    FEED_POWER_MAX,
    FEED_POWER_MIN,
)
from .coordinator import GoodweEmsCoordinator
from .entity import GoodweEmsEntity
from .inverter import GoodweInverter, InverterState


@dataclass(frozen=True, kw_only=True)
class GoodweNumberDescription(NumberEntityDescription):
    value_fn: Callable[[InverterState], float | None]
    set_fn: Callable[[GoodweInverter, float], Awaitable[None]]


NUMBERS: tuple[GoodweNumberDescription, ...] = (
    GoodweNumberDescription(
        key="export_limit_power",
        icon="mdi:transmission-tower-export",
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_min_value=FEED_POWER_MIN,
        native_max_value=FEED_POWER_MAX,
        native_step=100,
        mode=NumberMode.BOX,
        value_fn=lambda s: s.feed_power_param,
        set_fn=lambda inv, v: inv.async_set_export_limit_power(int(v)),
    ),
    GoodweNumberDescription(
        key="ems_power",
        icon="mdi:flash",
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_min_value=0,
        native_max_value=EMS_POWER_MAX,
        native_step=100,
        mode=NumberMode.BOX,
        value_fn=lambda s: s.ems_power,
        set_fn=lambda inv, v: inv.async_set_ems_power(int(v)),
    ),
    GoodweNumberDescription(
        key="battery_charge_limit",
        icon="mdi:battery-arrow-up",
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_min_value=0,
        native_max_value=BATTERY_POWER_MAX,
        native_step=50,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.battery_charge_limit,
        set_fn=lambda inv, v: inv.async_set_charge_limit(int(v)),
    ),
    GoodweNumberDescription(
        key="battery_discharge_limit",
        icon="mdi:battery-arrow-down",
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_min_value=0,
        native_max_value=BATTERY_POWER_MAX,
        native_step=50,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.battery_discharge_limit,
        set_fn=lambda inv, v: inv.async_set_discharge_limit(int(v)),
    ),
    GoodweNumberDescription(
        key="inverter_ac_limit",
        icon="mdi:sine-wave",
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_min_value=0,
        native_max_value=BATTERY_POWER_MAX,
        native_step=50,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.inverter_ac_limit,
        set_fn=lambda inv, v: inv.async_set_ac_limit(int(v)),
    ),
    GoodweNumberDescription(
        key="min_discharge_soc",
        icon="mdi:battery-low",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.min_discharge_soc,
        set_fn=lambda inv, v: inv.async_set_min_discharge_soc(int(v)),
    ),
    GoodweNumberDescription(
        key="max_charge_soc",
        icon="mdi:battery-high",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.max_charge_soc,
        set_fn=lambda inv, v: inv.async_set_max_charge_soc(int(v)),
    ),
    GoodweNumberDescription(
        key="fast_charge_stop_soc",
        icon="mdi:battery-charging-100",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.fast_charge_stop_soc,
        set_fn=lambda inv, v: inv.async_set_fast_charge(True, int(v)),
    ),
    GoodweNumberDescription(
        key="start_charge_soc",
        icon="mdi:battery-charging-outline",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=0,
        native_max_value=100,
        native_step=0.1,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.start_charge_soc,
        set_fn=lambda inv, v: inv.async_set_start_charge_soc(v),
    ),
    GoodweNumberDescription(
        key="stop_charge_soc",
        icon="mdi:battery-charging-outline",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=0,
        native_max_value=100,
        native_step=0.1,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.stop_charge_soc,
        set_fn=lambda inv, v: inv.async_set_stop_charge_soc(v),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: GoodweEmsCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(GoodweEmsNumber(coordinator, d) for d in NUMBERS)


class GoodweEmsNumber(GoodweEmsEntity, NumberEntity):
    entity_description: GoodweNumberDescription

    def __init__(
        self, coordinator: GoodweEmsCoordinator, description: GoodweNumberDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        state = self.inverter
        return self.entity_description.value_fn(state) if state else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    async def async_set_native_value(self, value: float) -> None:
        await self.entity_description.set_fn(self.coordinator.inverter, value)
        await self.coordinator.async_request_refresh()
