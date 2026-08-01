"""One-shot inverter actions."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from goodwe import InverterError
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import GoodweEmsConfigEntry
from .ems import GoodweEms

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class GoodweButtonEntityDescription(ButtonEntityDescription):
    """Describes a GoodWe button entity."""

    action: Callable[[GoodweEms], Awaitable[None]]
    #: Setting read at setup to decide whether the model supports the action.
    probe: str | None = None


BUTTONS: tuple[GoodweButtonEntityDescription, ...] = (
    GoodweButtonEntityDescription(
        key="synchronize_clock",
        translation_key="synchronize_clock",
        entity_category=EntityCategory.CONFIG,
        action=lambda ems: ems.inverter.write_setting("time", datetime.now()),
        probe="time",
    ),
    GoodweButtonEntityDescription(
        key="clear_economic_schedule",
        translation_key="clear_economic_schedule",
        entity_category=EntityCategory.CONFIG,
        action=lambda ems: ems.async_clear_economic_schedule(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: GoodweEmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the button entities from a config entry."""
    data = config_entry.runtime_data
    entities: list[ButtonEntity] = []

    for description in BUTTONS:
        if description.probe is not None:
            try:
                await data.inverter.read_setting(description.probe)
            except (InverterError, ValueError):
                _LOGGER.debug(
                    "Inverter does not support setting %s", description.probe
                )
                continue
        entities.append(GoodweEmsButton(data.device_info, description, data.ems))

    async_add_entities(entities)


class GoodweEmsButton(ButtonEntity):
    """A button that runs one inverter action."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    entity_description: GoodweButtonEntityDescription

    def __init__(
        self,
        device_info: DeviceInfo,
        description: GoodweButtonEntityDescription,
        ems: GoodweEms,
    ) -> None:
        self.entity_description = description
        self._attr_unique_id = (
            f"{DOMAIN}-{description.key}-{ems.inverter.serial_number}"
        )
        self._attr_device_info = device_info
        self._ems = ems

    async def async_press(self) -> None:
        """Run the action."""
        await self.entity_description.action(self._ems)
