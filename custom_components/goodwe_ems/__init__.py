"""Integrarea GoodWe EMS pentru Home Assistant."""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_BAUDRATE,
    CONF_CONNECTION,
    CONF_HOST,
    CONF_PORT,
    CONF_SERIAL_PORT,
    CONF_SLAVE,
    DEFAULT_BAUDRATE,
    DEFAULT_PORT,
    DEFAULT_SLAVE,
    DOMAIN,
    EMS_MODES,
    FEED_POWER_MAX,
    FEED_POWER_MIN,
    PLATFORMS,
    SERVICE_CLEAR_SCHEDULE,
    SERVICE_FORCE_CHARGE,
    SERVICE_FORCE_DISCHARGE,
    SERVICE_SET_EMS_MODE,
    SERVICE_SET_EXPORT_LIMIT,
    SERVICE_STOP_FORCING,
)
from .coordinator import GoodweEmsCoordinator
from .inverter import GoodweInverter
from .modbus import GoodweModbusClient, ModbusError

_LOGGER = logging.getLogger(__name__)

CARD_URL = "/goodwe_ems/goodwe-energy-flow-card.js"
CARD_FILENAME = "goodwe-energy-flow-card.js"
CARD_VERSION = "1.1.1"  # trebuie să corespundă cu CARD_VERSION din fișierul .js


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configurează o intrare."""
    data = {**entry.data, **entry.options}

    client = GoodweModbusClient(
        connection=data[CONF_CONNECTION],
        slave=data.get(CONF_SLAVE, DEFAULT_SLAVE),
        host=data.get(CONF_HOST),
        port=data.get(CONF_PORT, DEFAULT_PORT),
        serial_port=data.get(CONF_SERIAL_PORT),
        baudrate=data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
    )

    try:
        await client.async_connect()
    except ModbusError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = GoodweEmsCoordinator(hass, entry, GoodweInverter(client))
    coordinator.device_info_data = await coordinator.reader.async_read_device_info()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await _async_register_card(hass)
    _async_register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Descarcă intrarea, lăsând invertorul într-o stare cunoscută."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: GoodweEmsCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown_inverter()
        await coordinator.inverter.client.async_close()
        if not hass.data[DOMAIN]:
            for service in (
                SERVICE_SET_EMS_MODE,
                SERVICE_SET_EXPORT_LIMIT,
                SERVICE_FORCE_CHARGE,
                SERVICE_FORCE_DISCHARGE,
                SERVICE_STOP_FORCING,
                SERVICE_CLEAR_SCHEDULE,
            ):
                hass.services.async_remove(DOMAIN, service)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


# --------------------------------------------------------------------------
# Cardul Lovelace, servit din integrare
# --------------------------------------------------------------------------


async def _async_register_card(hass: HomeAssistant) -> None:
    """Servește cardul din `www/` ca să nu fie nevoie de un al doilea repo HACS.

    Steagul se pune abia după succes. Dacă îl puneam înainte de `await`, o
    înregistrare eșuată ar fi blocat definitiv orice reîncercare la a doua
    intrare, iar simptomul ar fi fost „Custom element doesn't exist" fără nicio
    urmă în jurnal.
    """
    if hass.data.get(f"{DOMAIN}_card_registered"):
        return

    path = Path(__file__).parent / "www" / CARD_FILENAME
    if not path.is_file():
        _LOGGER.error(
            "Cardul lipsește de la %s. Subfolderul www/ nu a fost copiat odată "
            "cu integrarea; recopiază folderul goodwe_ems complet",
            path,
        )
        return

    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(path), False)]
        )
    except RuntimeError:
        # Calea era deja înregistrată de o pornire anterioară din aceeași
        # sesiune; nu e o eroare, doar nu se poate înregistra de două ori.
        _LOGGER.debug("Calea statică %s era deja înregistrată", CARD_URL)

    # Sufixul de versiune forțează browserul să ceară fișierul nou după un
    # upgrade, în loc să servească varianta din cache.
    add_extra_js_url(hass, f"{CARD_URL}?v={CARD_VERSION}")
    hass.data[f"{DOMAIN}_card_registered"] = True
    _LOGGER.info("Cardul GoodWe Energy Flow este servit la %s", CARD_URL)


# --------------------------------------------------------------------------
# Servicii
# --------------------------------------------------------------------------

_ENTRY_SCHEMA = {vol.Optional("entry_id"): cv.string}

SCHEMA_SET_EMS_MODE = vol.Schema(
    {
        **_ENTRY_SCHEMA,
        vol.Required("mode"): vol.In(list(EMS_MODES)),
        vol.Optional("power", default=0): vol.All(int, vol.Range(min=0, max=10000)),
    }
)
SCHEMA_SET_EXPORT_LIMIT = vol.Schema(
    {
        **_ENTRY_SCHEMA,
        vol.Required("enabled"): cv.boolean,
        vol.Optional("power"): vol.All(
            int, vol.Range(min=FEED_POWER_MIN, max=FEED_POWER_MAX)
        ),
    }
)
SCHEMA_POWER = vol.Schema(
    {**_ENTRY_SCHEMA, vol.Required("power"): vol.All(int, vol.Range(min=0, max=10000))}
)
SCHEMA_PLAIN = vol.Schema(_ENTRY_SCHEMA)


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SET_EMS_MODE):
        return

    def _coordinator(call: ServiceCall) -> GoodweEmsCoordinator:
        entries: dict[str, GoodweEmsCoordinator] = hass.data[DOMAIN]
        entry_id = call.data.get("entry_id")
        if entry_id:
            if entry_id not in entries:
                raise vol.Invalid(f"entry_id necunoscut: {entry_id}")
            return entries[entry_id]
        if len(entries) != 1:
            raise vol.Invalid("Există mai multe invertoare — specifică entry_id")
        return next(iter(entries.values()))

    async def set_ems_mode(call: ServiceCall) -> None:
        coord = _coordinator(call)
        await coord.inverter.async_set_ems(
            EMS_MODES[call.data["mode"]], call.data.get("power", 0)
        )
        await coord.async_request_refresh()

    async def set_export_limit(call: ServiceCall) -> None:
        coord = _coordinator(call)
        await coord.inverter.async_set_export_limit(
            call.data["enabled"], call.data.get("power")
        )
        await coord.async_request_refresh()

    async def force_charge(call: ServiceCall) -> None:
        coord = _coordinator(call)
        await coord.inverter.async_charge_from_grid(call.data["power"])
        await coord.async_request_refresh()

    async def force_discharge(call: ServiceCall) -> None:
        coord = _coordinator(call)
        await coord.inverter.async_discharge(call.data["power"])
        await coord.async_request_refresh()

    async def stop_forcing(call: ServiceCall) -> None:
        coord = _coordinator(call)
        await coord.inverter.async_auto()
        await coord.async_request_refresh()

    async def clear_schedule(call: ServiceCall) -> None:
        coord = _coordinator(call)
        await coord.inverter.async_clear_economic_schedule()
        await coord.async_request_refresh()

    hass.services.async_register(DOMAIN, SERVICE_SET_EMS_MODE, set_ems_mode, SCHEMA_SET_EMS_MODE)
    hass.services.async_register(
        DOMAIN, SERVICE_SET_EXPORT_LIMIT, set_export_limit, SCHEMA_SET_EXPORT_LIMIT
    )
    hass.services.async_register(DOMAIN, SERVICE_FORCE_CHARGE, force_charge, SCHEMA_POWER)
    hass.services.async_register(
        DOMAIN, SERVICE_FORCE_DISCHARGE, force_discharge, SCHEMA_POWER
    )
    hass.services.async_register(DOMAIN, SERVICE_STOP_FORCING, stop_forcing, SCHEMA_PLAIN)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_SCHEDULE, clear_schedule, SCHEMA_PLAIN)
