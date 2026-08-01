"""Numeric inverter settings.

These are settings, not telemetry, so they are read once at setup and then only
when they change -- the same approach the core integration takes. Polling a
dozen individual settings on every coordinator cycle would multiply the traffic
to an inverter that does not enjoy being talked to.

A setting whose getter fails at setup is not offered at all: that is how a model
that does not implement the register makes itself known.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from goodwe import Inverter, InverterError
from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    BATTERY_POWER_MAX,
    DOMAIN,
    EMS_POWER_MAX,
    FEED_POWER_MAX,
    FEED_POWER_MIN,
    REG_BATTERY_CHARGE_LIMIT,
    REG_BATTERY_DISCHARGE_LIMIT,
    REG_FEED_POWER_PARAM,
    REG_INVERTER_AC_LIMIT,
    REG_MAX_CHARGE_SOC,
    REG_MIN_DISCHARGE_SOC,
    REG_START_CHARGE_SOC,
    REG_STOP_CHARGE_SOC,
    SOC_SCALE,
)
from .coordinator import GoodweEmsConfigEntry
from .ems import GoodweEms

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class GoodweNumberEntityDescription(NumberEntityDescription):
    """Describes a GoodWe number entity."""

    getter: Callable[[GoodweEms], Awaitable[float]]
    setter: Callable[[GoodweEms, float], Awaitable[None]]
    filter: Callable[[Inverter], bool] = lambda inv: True


def _setting_unit(inverter: Inverter, setting: str) -> str:
    """Unit the library declares for a setting, or "" if it has none."""
    return next((s.unit for s in inverter.settings() if s.id_ == setting), "")


def _register(
    key: str,
    register: int,
    minimum: float,
    maximum: float,
    step: float = 1,
    scale: int = 1,
    **kwargs,
) -> GoodweNumberEntityDescription:
    """A setting reached through a raw register, optionally scaled."""
    return GoodweNumberEntityDescription(
        key=key,
        translation_key=key,
        native_min_value=minimum,
        native_max_value=maximum,
        native_step=step,
        getter=lambda ems, r=register, s=scale: _read_scaled(ems, r, s),
        setter=lambda ems, value, r=register, s=scale: ems.async_write_register(
            r, round(value * s)
        ),
        **kwargs,
    )


async def _read_scaled(ems: GoodweEms, register: int, scale: int) -> float:
    return await ems.async_read_register(register) / scale


NUMBERS: tuple[GoodweNumberEntityDescription, ...] = (
    # Export limit in W. Written through the raw register rather than
    # `set_grid_export_limit`, which refuses negative values -- and a negative
    # export limit is how forced import is expressed.
    _register(
        "export_limit_power",
        REG_FEED_POWER_PARAM,
        FEED_POWER_MIN,
        FEED_POWER_MAX,
        step=100,
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        mode=NumberMode.BOX,
        filter=lambda inv: _setting_unit(inv, "grid_export_limit") != "%",
    ),
    # Export limit as a percentage of rated power, on the models that store it
    # that way in the same register.
    _register(
        "export_limit_percent",
        REG_FEED_POWER_PARAM,
        0,
        200,
        step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.BOX,
        filter=lambda inv: _setting_unit(inv, "grid_export_limit") == "%",
    ),
    GoodweNumberEntityDescription(
        key="ems_power",
        translation_key="ems_power",
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_min_value=0,
        native_max_value=EMS_POWER_MAX,
        native_step=100,
        mode=NumberMode.BOX,
        getter=lambda ems: ems.inverter.read_setting("ems_power_limit"),
        setter=lambda ems, value: ems.async_set_ems_power(int(value)),
    ),
    _register(
        "battery_charge_limit",
        REG_BATTERY_CHARGE_LIMIT,
        0,
        BATTERY_POWER_MAX,
        step=50,
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.CONFIG,
    ),
    _register(
        "battery_discharge_limit",
        REG_BATTERY_DISCHARGE_LIMIT,
        0,
        BATTERY_POWER_MAX,
        step=50,
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.CONFIG,
    ),
    _register(
        "inverter_ac_limit",
        REG_INVERTER_AC_LIMIT,
        0,
        BATTERY_POWER_MAX,
        step=50,
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.CONFIG,
    ),
    _register(
        "min_discharge_soc",
        REG_MIN_DISCHARGE_SOC,
        0,
        100,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
    ),
    _register(
        "max_charge_soc",
        REG_MAX_CHARGE_SOC,
        0,
        100,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
    ),
    _register(
        "start_charge_soc",
        REG_START_CHARGE_SOC,
        0,
        100,
        step=0.1,
        scale=SOC_SCALE,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
    ),
    _register(
        "stop_charge_soc",
        REG_STOP_CHARGE_SOC,
        0,
        100,
        step=0.1,
        scale=SOC_SCALE,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
    ),
    GoodweNumberEntityDescription(
        key="fast_charge_stop_soc",
        translation_key="fast_charge_stop_soc",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        entity_category=EntityCategory.CONFIG,
        # Only the threshold. Deliberately not routed through the library's
        # fast-charge helper, because adjusting the stop point must not start
        # charging from the grid as a side effect.
        getter=lambda ems: ems.inverter.read_setting("fast_charging_soc"),
        setter=lambda ems, value: ems.inverter.write_setting(
            "fast_charging_soc", int(value)
        ),
    ),
    GoodweNumberEntityDescription(
        key="battery_discharge_depth",
        translation_key="battery_discharge_depth",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=0,
        native_max_value=99,
        native_step=1,
        getter=lambda ems: ems.inverter.get_ongrid_battery_dod(),
        setter=lambda ems, value: ems.inverter.set_ongrid_battery_dod(int(value)),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: GoodweEmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the number entities from a config entry."""
    data = config_entry.runtime_data
    entities: list[NumberEntity] = []

    for description in NUMBERS:
        if not description.filter(data.inverter):
            continue
        try:
            current = await description.getter(data.ems)
        except (InverterError, ValueError):
            _LOGGER.debug("Inverter does not support setting %s", description.key)
            continue
        entities.append(
            GoodweEmsNumber(data.device_info, description, data.ems, current)
        )

    async_add_entities(entities)


class GoodweEmsNumber(NumberEntity):
    """A numeric inverter setting."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    entity_description: GoodweNumberEntityDescription

    def __init__(
        self,
        device_info: DeviceInfo,
        description: GoodweNumberEntityDescription,
        ems: GoodweEms,
        current_value: float,
    ) -> None:
        self.entity_description = description
        self._attr_unique_id = (
            f"{DOMAIN}-{description.key}-{ems.inverter.serial_number}"
        )
        self._attr_device_info = device_info
        self._attr_native_value = float(current_value)
        self._ems = ems

    async def async_set_native_value(self, value: float) -> None:
        """Write the new value to the inverter."""
        await self.entity_description.setter(self._ems, value)
        self._attr_native_value = value
        self.async_write_ha_state()
