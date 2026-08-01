"""Operation mode and EMS mode selectors.

Two different things share this platform. The operation mode is the inverter's
own work mode (General, Backup, Eco, ...), the same entity the core integration
offers. The EMS mode is register 47511, the one price dispatch drives.
"""

from __future__ import annotations

import logging

from goodwe import InverterError, OperationMode
from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import GoodweEmsConfigEntry, GoodweEmsCoordinator
from .ems import EMS_MODES, EMS_MODES_REVERSE, GoodweEms

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

_MODE_TO_OPTION: dict[OperationMode, str] = {
    OperationMode.GENERAL: "general",
    OperationMode.OFF_GRID: "off_grid",
    OperationMode.BACKUP: "backup",
    OperationMode.ECO: "eco",
    OperationMode.PEAK_SHAVING: "peak_shaving",
    OperationMode.SELF_USE: "self_use",
    OperationMode.ECO_CHARGE: "eco_charge",
    OperationMode.ECO_DISCHARGE: "eco_discharge",
}
_OPTION_TO_MODE: dict[str, OperationMode] = {
    value: key for key, value in _MODE_TO_OPTION.items()
}

OPERATION_MODE = SelectEntityDescription(
    key="operation_mode",
    translation_key="operation_mode",
    entity_category=EntityCategory.CONFIG,
)

EMS_MODE = SelectEntityDescription(
    key="ems_mode",
    translation_key="ems_mode",
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: GoodweEmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the select entities from a config entry."""
    data = config_entry.runtime_data
    entities: list[SelectEntity] = []

    supported_modes = await data.inverter.get_operation_modes(False)
    try:
        active_mode = await data.inverter.get_operation_mode()
    except (InverterError, ValueError):
        _LOGGER.debug("Could not read the inverter operation mode")
    else:
        active_option = _MODE_TO_OPTION.get(active_mode)
        if active_option is None:
            _LOGGER.warning(
                "Active operation mode %s is not one this integration knows; "
                "skipping the operation mode entity",
                active_mode,
            )
        else:
            entities.append(
                InverterOperationModeEntity(
                    data.device_info,
                    OPERATION_MODE,
                    data.ems,
                    [v for k, v in _MODE_TO_OPTION.items() if k in supported_modes],
                    active_option,
                )
            )

    try:
        ems_mode = await data.ems.async_get_ems_mode()
    except (InverterError, ValueError):
        _LOGGER.debug("Could not read the inverter EMS mode")
    else:
        entities.append(
            EmsModeEntity(
                data.device_info,
                EMS_MODE,
                data.ems,
                data.coordinator,
                EMS_MODES_REVERSE.get(ems_mode) if ems_mode else None,
            )
        )

    async_add_entities(entities)


class InverterOperationModeEntity(SelectEntity):
    """The inverter's work mode."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        device_info: DeviceInfo,
        description: SelectEntityDescription,
        ems: GoodweEms,
        supported_options: list[str],
        current_mode: str,
    ) -> None:
        self.entity_description = description
        self._attr_unique_id = (
            f"{DOMAIN}-{description.key}-{ems.inverter.serial_number}"
        )
        self._attr_device_info = device_info
        self._attr_options = supported_options
        self._attr_current_option = current_mode
        self._ems = ems

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        await self._ems.inverter.set_operation_mode(_OPTION_TO_MODE[option])
        self._attr_current_option = option
        self.async_write_ha_state()


class EmsModeEntity(SelectEntity):
    """The current EMS mode.

    While price dispatch is on it rewrites the mode every cycle, so a manual
    selection would be overwritten within one scan interval. The entity goes
    unavailable in that case rather than appearing to ignore the change for no
    reason.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_options = list(EMS_MODES)

    def __init__(
        self,
        device_info: DeviceInfo,
        description: SelectEntityDescription,
        ems: GoodweEms,
        coordinator: GoodweEmsCoordinator,
        current_mode: str | None,
    ) -> None:
        self.entity_description = description
        self._attr_unique_id = (
            f"{DOMAIN}-{description.key}-{ems.inverter.serial_number}"
        )
        self._attr_device_info = device_info
        self._attr_current_option = current_mode
        self._ems = ems
        self._coordinator = coordinator

    @property
    def available(self) -> bool:
        return not self._coordinator.dispatch_enabled

    async def async_select_option(self, option: str) -> None:
        """Write the EMS mode, arming the inverter first."""
        await self._ems.async_set_ems_mode(EMS_MODES[option])
        self._attr_current_option = option
        self.async_write_ha_state()
