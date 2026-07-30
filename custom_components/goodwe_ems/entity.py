"""Clasa de bază comună tuturor entităților integrării."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_NAME, DOMAIN, MANUFACTURER
from .coordinator import GoodweEmsCoordinator


class GoodweEmsEntity(CoordinatorEntity[GoodweEmsCoordinator]):
    """Toate entitățile aparțin aceluiași dispozitiv logic."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: GoodweEmsCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_translation_key = key

        info = coordinator.device_info_data
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            manufacturer=MANUFACTURER,
            name=coordinator.entry.title or DEFAULT_NAME,
            model=info.model_name or "Invertor hibrid ET (ARM 745)",
            serial_number=info.serial_number,
        )

    @property
    def inverter(self):
        """Registrele de control din ultimul ciclu."""
        return self.coordinator.data.inverter if self.coordinator.data else None

    @property
    def live(self):
        """Telemetria din ultimul ciclu."""
        return self.coordinator.data.live if self.coordinator.data else None
