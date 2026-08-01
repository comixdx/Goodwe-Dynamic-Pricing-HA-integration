"""Base class for the integration's own coordinator-backed entities.

Inverter sensors are generated from the library's own sensor definitions and do
not use this class; it is for the entities that have no register behind them --
prices, the dispatch state, the dispatch switch.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GoodweEmsCoordinator


class GoodweEmsEntity(CoordinatorEntity[GoodweEmsCoordinator]):
    """An entity of the inverter device, keyed by the inverter's serial number."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GoodweEmsCoordinator,
        device_info: DeviceInfo,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = (
            f"{DOMAIN}-{key}-{coordinator.inverter.serial_number}"
        )
        self._attr_translation_key = key
        self._attr_device_info = device_info
