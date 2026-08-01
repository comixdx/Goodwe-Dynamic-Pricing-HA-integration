"""The GoodWe EMS integration for Home Assistant."""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol
from goodwe import Inverter, InverterError, connect
from goodwe.const import GOODWE_UDP_PORT
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceInfo

from .config_flow import GoodweEmsConfigFlow
from .const import (
    CONF_MODEL_FAMILY,
    DEFAULT_NAME,
    DOMAIN,
    FEED_POWER_MAX,
    FEED_POWER_MIN,
    MANUFACTURER,
    PLATFORMS,
    REG_FEED_POWER_PARAM,
    SERVICE_CLEAR_SCHEDULE,
    SERVICE_FORCE_CHARGE,
    SERVICE_FORCE_DISCHARGE,
    SERVICE_SET_EMS_MODE,
    SERVICE_SET_EXPORT_LIMIT,
    SERVICE_STOP_FORCING,
)
from .coordinator import (
    GoodweEmsConfigEntry,
    GoodweEmsCoordinator,
    GoodweEmsRuntimeData,
)
from .ems import EMS_MODES, GoodweEms

_LOGGER = logging.getLogger(__name__)

CARD_URL = "/goodwe_ems/goodwe-energy-flow-card.js"
CARD_FILENAME = "goodwe-energy-flow-card.js"
CARD_VERSION = "1.4.0"  # must match CARD_VERSION inside the .js file


async def async_setup_entry(hass: HomeAssistant, entry: GoodweEmsConfigEntry) -> bool:
    """Set up a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, GOODWE_UDP_PORT)
    model_family = entry.data.get(CONF_MODEL_FAMILY)

    try:
        inverter = await connect(
            host=host,
            port=port,
            family=model_family,
            retries=3,
        )
    except InverterError as err:
        # The communication port can change under the inverter after a firmware
        # update, which otherwise looks exactly like the inverter being gone.
        try:
            inverter = await _async_check_port(hass, entry, host)
        except InverterError:
            raise ConfigEntryNotReady from err

    device_info = DeviceInfo(
        configuration_url="https://www.semsportal.com",
        identifiers={(DOMAIN, inverter.serial_number)},
        name=entry.title or DEFAULT_NAME,
        manufacturer=MANUFACTURER,
        model=inverter.model_name,
        serial_number=inverter.serial_number,
        sw_version=f"{inverter.firmware} / {inverter.arm_firmware}",
    )

    ems = GoodweEms(inverter)
    coordinator = GoodweEmsCoordinator(hass, entry, ems)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = GoodweEmsRuntimeData(
        inverter=inverter,
        ems=ems,
        coordinator=coordinator,
        device_info=device_info,
    )

    await _async_register_card(hass)
    _async_register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_check_port(
    hass: HomeAssistant, entry: GoodweEmsConfigEntry, host: str
) -> Inverter:
    """Re-detect the inverter's port and store it on the entry."""
    inverter, port = await GoodweEmsConfigFlow.async_detect_inverter_port(host=host)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_MODEL_FAMILY: type(inverter).__name__,
        },
    )
    return inverter


async def async_unload_entry(hass: HomeAssistant, entry: GoodweEmsConfigEntry) -> bool:
    """Unload the entry, leaving the inverter in a known state."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.coordinator.async_shutdown_inverter()
        if not _other_entries(hass, entry):
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


async def async_migrate_entry(
    hass: HomeAssistant, entry: GoodweEmsConfigEntry
) -> bool:
    """Migrate entries created by the pymodbus-based versions.

    Version 1 stored a whole Modbus connection: transport type, TCP port or
    serial device, and a slave id. The library takes a host and detects the rest,
    so the migration keeps the host, throws the rest away and re-probes. Serial
    and RTU-over-TCP entries cannot be carried over at all -- the library speaks
    UDP and Modbus TCP only -- so those are failed deliberately rather than
    silently reconnected to something else.
    """
    if entry.version > 1:
        return True

    host = entry.data.get(CONF_HOST)
    if not host:
        _LOGGER.error(
            "Cannot migrate this entry automatically: it was configured over a "
            "serial connection, which the goodwe library does not support. "
            "Remove the integration and add it again using the inverter's IP "
            "address"
        )
        return False

    try:
        inverter, port = await GoodweEmsConfigFlow.async_detect_inverter_port(host=host)
    except InverterError:
        # Not a permanent failure: the inverter may simply be asleep. Home
        # Assistant retries the migration on the next start.
        _LOGGER.warning("Inverter at %s did not answer, migration deferred", host)
        return False

    # Everything except the connection keys is ours and survives untouched.
    carried = {
        key: value
        for key, value in entry.data.items()
        if key not in (CONF_HOST, CONF_PORT, "connection", "serial_port", "baudrate", "slave")
    }
    hass.config_entries.async_update_entry(
        entry,
        data={
            **carried,
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_MODEL_FAMILY: type(inverter).__name__,
        },
        unique_id=inverter.serial_number,
        version=2,
    )
    _LOGGER.info(
        "Migrated %s to the goodwe library (port %s, family %s, S/N %s)",
        host,
        port,
        type(inverter).__name__,
        inverter.serial_number,
    )
    return True


def _other_entries(hass: HomeAssistant, entry: GoodweEmsConfigEntry) -> bool:
    """Is any other entry of this integration still loaded?"""
    return any(
        other.entry_id != entry.entry_id and hasattr(other, "runtime_data")
        for other in hass.config_entries.async_loaded_entries(DOMAIN)
    )


async def _async_reload_entry(hass: HomeAssistant, entry: GoodweEmsConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


# --------------------------------------------------------------------------
# The Lovelace card, served from the integration
# --------------------------------------------------------------------------


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the card out of `www/` so a second HACS repo is not needed.

    The flag is only set after success. Setting it before the `await` meant a
    failed registration permanently blocked any retry on a second entry, and the
    symptom was "Custom element doesn't exist" with nothing in the log.
    """
    if hass.data.get(f"{DOMAIN}_card_registered"):
        return

    path = Path(__file__).parent / "www" / CARD_FILENAME
    if not path.is_file():
        _LOGGER.error(
            "The card is missing from %s. The www/ subfolder was not copied "
            "along with the integration; copy the whole goodwe_ems folder again",
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
        # Already registered by an earlier start in the same session. Not an
        # error, it just cannot be registered twice.
        _LOGGER.debug("Static path %s was already registered", CARD_URL)

    # The version suffix forces the browser to fetch the new file after an
    # upgrade instead of serving the cached one.
    add_extra_js_url(hass, f"{CARD_URL}?v={CARD_VERSION}")
    hass.data[f"{DOMAIN}_card_registered"] = True
    _LOGGER.info("The GoodWe Energy Flow card is served at %s", CARD_URL)


# --------------------------------------------------------------------------
# Services
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

    def _entry(call: ServiceCall) -> GoodweEmsConfigEntry:
        entries = [
            entry
            for entry in hass.config_entries.async_loaded_entries(DOMAIN)
            if hasattr(entry, "runtime_data")
        ]
        entry_id = call.data.get("entry_id")
        if entry_id:
            for entry in entries:
                if entry.entry_id == entry_id:
                    return entry
            raise vol.Invalid(f"Unknown entry_id: {entry_id}")
        if len(entries) != 1:
            raise vol.Invalid("Several inverters are configured -- pass entry_id")
        return entries[0]

    async def set_ems_mode(call: ServiceCall) -> None:
        data = _entry(call).runtime_data
        await data.ems.async_set_ems(
            EMS_MODES[call.data["mode"]], call.data.get("power", 0)
        )
        await data.coordinator.async_request_refresh()

    async def set_export_limit(call: ServiceCall) -> None:
        data = _entry(call).runtime_data
        power = call.data.get("power")
        # Written before the enable flag: enabling with a stale parameter can
        # leave the inverter on an unwanted limit for a few seconds.
        if power is not None:
            await data.ems.async_write_register(REG_FEED_POWER_PARAM, int(power))
        await data.inverter.write_setting("grid_export", int(call.data["enabled"]))
        await data.coordinator.async_request_refresh()

    async def force_charge(call: ServiceCall) -> None:
        data = _entry(call).runtime_data
        await data.ems.async_charge_from_grid(call.data["power"])
        await data.coordinator.async_request_refresh()

    async def force_discharge(call: ServiceCall) -> None:
        data = _entry(call).runtime_data
        await data.ems.async_discharge(call.data["power"])
        await data.coordinator.async_request_refresh()

    async def stop_forcing(call: ServiceCall) -> None:
        data = _entry(call).runtime_data
        await data.ems.async_auto()
        await data.coordinator.async_request_refresh()

    async def clear_schedule(call: ServiceCall) -> None:
        data = _entry(call).runtime_data
        await data.ems.async_clear_economic_schedule()
        await data.coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_SET_EMS_MODE, set_ems_mode, SCHEMA_SET_EMS_MODE
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_EXPORT_LIMIT, set_export_limit, SCHEMA_SET_EXPORT_LIMIT
    )
    hass.services.async_register(
        DOMAIN, SERVICE_FORCE_CHARGE, force_charge, SCHEMA_POWER
    )
    hass.services.async_register(
        DOMAIN, SERVICE_FORCE_DISCHARGE, force_discharge, SCHEMA_POWER
    )
    hass.services.async_register(
        DOMAIN, SERVICE_STOP_FORCING, stop_forcing, SCHEMA_PLAIN
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_SCHEDULE, clear_schedule, SCHEMA_PLAIN
    )
