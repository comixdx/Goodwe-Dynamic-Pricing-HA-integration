"""Comutatoarele de control ale invertorului."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import GoodweEmsCoordinator
from .entity import GoodweEmsEntity
from .inverter import GoodweInverter, InverterState


@dataclass(frozen=True, kw_only=True)
class GoodweSwitchDescription(SwitchEntityDescription):
    value_fn: Callable[[InverterState], bool | None]
    set_fn: Callable[[GoodweInverter, bool], Awaitable[None]]


SWITCHES: tuple[GoodweSwitchDescription, ...] = (
    GoodweSwitchDescription(
        key="export_limit_enable",
        icon="mdi:transmission-tower-export",
        value_fn=lambda s: s.feed_power_enable,
        set_fn=lambda inv, v: inv.async_set_export_limit_enabled(v),
    ),
    GoodweSwitchDescription(
        key="anti_backflow",
        icon="mdi:transmission-tower-off",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.anti_backflow,
        set_fn=lambda inv, v: inv.async_set_anti_backflow(v),
    ),
    GoodweSwitchDescription(
        key="charge_discharge_enable",
        icon="mdi:battery-sync",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.charge_discharge_enable,
        set_fn=lambda inv, v: inv.async_set_charge_discharge_enabled(v),
    ),
    GoodweSwitchDescription(
        key="fast_charge",
        icon="mdi:battery-charging-high",
        value_fn=lambda s: None if s.fast_charge_enable is None else bool(s.fast_charge_enable),
        set_fn=lambda inv, v: inv.async_set_fast_charge(v),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: GoodweEmsCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = [
        GoodweEmsSwitch(coordinator, description) for description in SWITCHES
    ]
    entities.append(DispatchSwitch(coordinator))
    async_add_entities(entities)


class GoodweEmsSwitch(GoodweEmsEntity, SwitchEntity):
    """Comutator legat direct de un registru."""

    entity_description: GoodweSwitchDescription

    def __init__(
        self, coordinator: GoodweEmsCoordinator, description: GoodweSwitchDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        state = self.inverter
        return self.entity_description.value_fn(state) if state else None

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.entity_description.set_fn(self.coordinator.inverter, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.entity_description.set_fn(self.coordinator.inverter, False)
        await self.coordinator.async_request_refresh()


class DispatchSwitch(GoodweEmsEntity, RestoreEntity, SwitchEntity):
    """Pornește sau oprește dispecerizarea pe preț.

    E singura entitate care nu corespunde unui registru: starea trăiește în
    coordinator. La oprire, invertorul e readus explicit în modul Auto.

    Coordinatorul o ține doar în memorie, deci starea se reia din ultima stare
    a entității. Opțiunea `enable_dispatch` din configurare rămâne valoarea de
    pornire la prima instalare; după aceea comutatorul e sursa de adevăr.
    """

    _attr_icon = "mdi:cash-clock"

    def __init__(self, coordinator: GoodweEmsCoordinator) -> None:
        super().__init__(coordinator, "price_dispatch")

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
