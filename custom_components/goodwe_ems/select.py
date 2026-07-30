"""Selectorul de mod EMS (registrul 47511)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EMS_MODES
from .coordinator import GoodweEmsCoordinator
from .entity import GoodweEmsEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: GoodweEmsCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EmsModeSelect(coordinator)])


class EmsModeSelect(GoodweEmsEntity, SelectEntity):
    """Modul EMS curent.

    Dacă dispecerizarea pe preț e pornită, ea rescrie modul la fiecare ciclu —
    o selecție manuală va fi suprascrisă în cel mult un interval de scanare.
    Entitatea devine indisponibilă în acest caz, ca să nu pară că schimbarea
    a fost ignorată fără motiv.
    """

    _attr_icon = "mdi:home-battery"
    _attr_options = list(EMS_MODES)

    def __init__(self, coordinator: GoodweEmsCoordinator) -> None:
        super().__init__(coordinator, "ems_mode")

    @property
    def current_option(self) -> str | None:
        state = self.inverter
        if state is None or state.ems_mode is None:
            return None
        name = state.ems_mode_name
        return name if name in EMS_MODES else None

    @property
    def available(self) -> bool:
        return (
            super().available
            and not self.coordinator.dispatch_enabled
            and self.current_option is not None
        )

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.inverter.async_set_ems_mode(EMS_MODES[option])
        await self.coordinator.async_request_refresh()
