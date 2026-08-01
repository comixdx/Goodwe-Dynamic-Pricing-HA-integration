"""Diagnostics support for GoodWe EMS."""

from __future__ import annotations

from typing import Any

from goodwe import InverterError
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import CONF_ENTSOE_TOKEN
from .coordinator import GoodweEmsConfigEntry

TO_REDACT = {CONF_HOST, CONF_ENTSOE_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: GoodweEmsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = config_entry.runtime_data
    inverter = data.inverter
    coordinator = data.coordinator

    try:
        settings = await inverter.read_settings_data()
    except InverterError as err:
        settings = {"error": str(err)}

    series = coordinator.series
    decision = coordinator.decision

    return {
        "entry": {
            "data": async_redact_data(dict(config_entry.data), TO_REDACT),
            "options": async_redact_data(dict(config_entry.options), TO_REDACT),
            "version": config_entry.version,
        },
        "inverter": {
            "family": type(inverter).__name__,
            "model_name": inverter.model_name,
            "rated_power": inverter.rated_power,
            "firmware": inverter.firmware,
            "arm_firmware": inverter.arm_firmware,
            "modbus_version": inverter.modbus_version,
            # The serial number is the device identifier and appears in every
            # entity id, so redacting it here would only make the report harder
            # to match up with the rest of the log.
            "serial_number": inverter.serial_number,
        },
        "runtime": coordinator.data,
        "settings": settings,
        "dispatch": {
            "enabled": coordinator.dispatch_enabled,
            "config": vars(coordinator.dispatch_config),
            "state": decision.state if decision else None,
            "reason": decision.reason if decision else None,
            "ems_mode": decision.ems_mode.name if decision else None,
            "power_w": decision.power_w if decision else None,
        },
        "prices": {
            "source": series.source if series else None,
            "day": series.day.isoformat() if series else None,
            "currency": series.currency if series else None,
            "intervals": len(series.values) if series else 0,
            "actionable": series.is_actionable() if series else False,
            "fetched_at": series.fetched_at.isoformat() if series else None,
            "monthly_weighted": coordinator.monthly_weighted,
        },
    }
