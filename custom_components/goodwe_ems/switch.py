"""Inverter control switches, plus the price dispatch switch."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from goodwe import InverterError
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    REG_ANTI_BACKFLOW,
    REG_CHARGE_DISCHARGE_ENABLE,
)
from .coordinator import GoodweEmsConfigEntry, GoodweEmsCoordinator
from .ems import GoodweEms
from .entity import GoodweEmsEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class GoodweSwitchEntityDescription(SwitchEntityDescription):
    """Describes a GoodWe switch entity."""

    getter: Callable[[GoodweEms], Awaitable[int]]
    setter: Callable[[GoodweEms, bool], Awaitable[None]]


def _register_switch(
    key: str, register: int, **kwargs
) -> GoodweSwitchEntityDescription:
    return GoodweSwitchEntityDescription(
        key=key,
        translation_key=key,
        getter=lambda ems, r=register: ems.async_read_register(r),
        setter=lambda ems, value, r=register: ems.async_write_register(r, int(value)),
        **kwargs,
    )


SWITCHES: tuple[GoodweSwitchEntityDescription, ...] = (
    GoodweSwitchEntityDescription(
        key="export_limit_enable",
        translation_key="export_limit_enable",
        getter=lambda ems: ems.inverter.read_setting("grid_export"),
        setter=lambda ems, value: ems.inverter.write_setting(
            "grid_export", int(value)
        ),
    ),
    GoodweSwitchEntityDescription(
        key="fast_charge",
        translation_key="fast_charge",
        getter=lambda ems: ems.inverter.read_setting("fast_charging"),
        setter=lambda ems, value: ems.inverter.write_setting(
            "fast_charging", int(value)
        ),
    ),
    _register_switch(
        "anti_backflow",
        REG_ANTI_BACKFLOW,
        entity_category=EntityCategory.CONFIG,
    ),
    _register_switch(
        "charge_discharge_enable",
        REG_CHARGE_DISCHARGE_ENABLE,
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: GoodweEmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switch entities from a config entry."""
    data = config_entry.runtime_data
    entities: list[SwitchEntity] = []

    for description in SWITCHES:
        try:
            current = await description.getter(data.ems)
        except (InverterError, ValueError):
            _LOGGER.debug("Inverter does not support setting %s", description.key)
            continue
        entities.append(
            GoodweEmsSwitch(data.device_info, description, data.ems, bool(current))
        )

    entities.append(DispatchSwitch(data.coordinator, data.device_info))
    async_add_entities(entities)


class GoodweEmsSwitch(SwitchEntity):
    """A switch backed directly by an inverter setting."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    entity_description: GoodweSwitchEntityDescription

    def __init__(
        self,
        device_info: DeviceInfo,
        description: GoodweSwitchEntityDescription,
        ems: GoodweEms,
        current_value: bool,
    ) -> None:
        self.entity_description = description
        self._attr_unique_id = (
            f"{DOMAIN}-{description.key}-{ems.inverter.serial_number}"
        )
        self._attr_device_info = device_info
        self._attr_is_on = current_value
        self._ems = ems

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, value: bool) -> None:
        await self.entity_description.setter(self._ems, value)
        self._attr_is_on = value
        self.async_write_ha_state()


class DispatchSwitch(GoodweEmsEntity, RestoreEntity, SwitchEntity):
    """Turn price dispatch on or off.

    The only entity with no register behind it: the state lives in the
    coordinator. Turning it off returns the inverter to Auto explicitly.

    Because the coordinator only holds it in memory, the state is restored from
    the entity's last state. The `enable_dispatch` config option remains the
    starting value at first install; after that the switch is the source of
    truth.
    """

    def __init__(
        self, coordinator: GoodweEmsCoordinator, device_info: DeviceInfo
    ) -> None:
        super().__init__(coordinator, device_info, "price_dispatch")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state not in (STATE_ON, STATE_OFF):
            return
        if self.coordinator.restore_dispatch_enabled(last.state == STATE_ON):
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()

    @property
    def is_on(self) -> bool:
        return self.coordinator.dispatch_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_dispatch_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_dispatch_enabled(False)
