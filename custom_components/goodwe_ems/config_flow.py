"""Config flow: find the inverter, then set the battery parameters.

The connection step asks for an IP address and nothing else. The library probes
UDP 8899 first and Modbus TCP 502 second, and detects the protocol family from
the serial number, so port, slave id and transport type are all things the user
no longer has to know.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from goodwe import Inverter, InverterError, connect
from goodwe.const import GOODWE_TCP_PORT, GOODWE_UDP_PORT
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_CYCLE_COST,
    CONF_ENABLE_DISPATCH,
    CONF_ENTSOE_TOKEN,
    CONF_HOLD_FOR_PEAK,
    CONF_MAX_CHARGE_POWER,
    CONF_MAX_DISCHARGE_POWER,
    CONF_MIN_SOC,
    CONF_MODEL_FAMILY,
    CONF_ROUND_TRIP_EFFICIENCY,
    CONF_SCAN_INTERVAL,
    CONF_SOC_ENTITY,
    CONF_TARGET_SOC,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_CYCLE_COST,
    DEFAULT_HOLD_FOR_PEAK,
    DEFAULT_MAX_CHARGE_POWER,
    DEFAULT_MAX_DISCHARGE_POWER,
    DEFAULT_MIN_SOC,
    DEFAULT_NAME,
    DEFAULT_ROUND_TRIP_EFFICIENCY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TARGET_SOC,
    DOMAIN,
)

CONNECTION_SCHEMA = vol.Schema({vol.Required(CONF_HOST): str})


def _number(
    minimum: float,
    maximum: float,
    step: float = 1,
    unit: str | None = None,
    slider: bool = False,
) -> selector.NumberSelector:
    """A numeric field whose bounds and unit are visible in the UI.

    A plain `int` renders as a text box with no indication of the accepted
    range, and the user only finds out they were wrong on submit.
    """
    config = selector.NumberSelectorConfig(
        min=minimum,
        max=maximum,
        step=step,
        mode=(
            selector.NumberSelectorMode.SLIDER
            if slider
            else selector.NumberSelectorMode.BOX
        ),
    )
    if unit is not None:
        # The key has to be absent when there is no unit: the selector's schema
        # requires a string, and None would raise vol.Invalid.
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


# NumberSelector always returns a float, and these all have to be written as
# integers.
_INT_KEYS = (
    CONF_MAX_CHARGE_POWER,
    CONF_MAX_DISCHARGE_POWER,
    CONF_MIN_SOC,
    CONF_TARGET_SOC,
    CONF_SCAN_INTERVAL,
)


def _coerce_ints(user_input: dict[str, Any]) -> dict[str, Any]:
    """Turn back into ints the values the selector made floats."""
    return {
        key: int(value) if key in _INT_KEYS and value is not None else value
        for key, value in user_input.items()
    }


class _OptionalEntitySelector(selector.EntitySelector):
    """An EntitySelector that also accepts "no entity".

    EntitySelector validates with `entity_id_or_uuid`, which an emptied field
    fails ("Entity is neither a valid entity ID nor a valid UUID"): a form with
    no fallback sensor was impossible to submit. The UI may send the key
    missing, `None` or the empty string depending on version, so all three are
    treated alike.

    A `vol.Any(selector, "", None)` would have looked more direct, but
    voluptuous_serialize cannot serialize an alternative containing literals, so
    the form would not have rendered at all. Serialization here stays the base
    class's.
    """

    def __call__(self, data: Any) -> str:
        if data is None or data == "":
            return ""
        return str(super().__call__(data))


def _soc_entity_key(current: str | None) -> vol.Marker:
    """The selector key: no `default`, only the already-chosen value.

    A `default=""` was exactly what validation refused, and a suggested value of
    `None` ends up in the same place.
    """
    if current:
        return vol.Optional(CONF_SOC_ENTITY, description={"suggested_value": current})
    return vol.Optional(CONF_SOC_ENTITY)


def _normalise_soc_entity(user_input: dict[str, Any]) -> dict[str, Any]:
    """Clearing the selector has to delete the entity, not keep it.

    Options are read as `{**entry.data, **entry.options}`, so a key absent from
    the options leaves the old value from data standing. Write the empty string
    explicitly.
    """
    return {**user_input, CONF_SOC_ENTITY: user_input.get(CONF_SOC_ENTITY) or ""}


def _battery_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_BATTERY_CAPACITY,
                default=d.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY),
            ): _number(1, 200, step=0.1, unit="kWh"),
            vol.Required(
                CONF_MIN_SOC, default=d.get(CONF_MIN_SOC, DEFAULT_MIN_SOC)
            ): _number(5, 95, unit="%", slider=True),
            vol.Required(
                CONF_TARGET_SOC, default=d.get(CONF_TARGET_SOC, DEFAULT_TARGET_SOC)
            ): _number(10, 100, unit="%", slider=True),
            vol.Required(
                CONF_MAX_CHARGE_POWER,
                default=d.get(CONF_MAX_CHARGE_POWER, DEFAULT_MAX_CHARGE_POWER),
            ): _number(100, 10000, unit="W"),
            vol.Required(
                CONF_MAX_DISCHARGE_POWER,
                default=d.get(CONF_MAX_DISCHARGE_POWER, DEFAULT_MAX_DISCHARGE_POWER),
            ): _number(100, 10000, unit="W"),
            vol.Required(
                CONF_ENABLE_DISPATCH, default=d.get(CONF_ENABLE_DISPATCH, False)
            ): bool,
            vol.Required(
                CONF_HOLD_FOR_PEAK,
                default=d.get(CONF_HOLD_FOR_PEAK, DEFAULT_HOLD_FOR_PEAK),
            ): bool,
            vol.Required(
                CONF_CYCLE_COST, default=d.get(CONF_CYCLE_COST, DEFAULT_CYCLE_COST)
            ): _number(0, 2000, unit="RON/MWh"),
            vol.Required(
                CONF_ROUND_TRIP_EFFICIENCY,
                default=d.get(
                    CONF_ROUND_TRIP_EFFICIENCY, DEFAULT_ROUND_TRIP_EFFICIENCY
                ),
            ): _number(0.5, 1.0, step=0.01),
            vol.Optional(
                CONF_ENTSOE_TOKEN, default=d.get(CONF_ENTSOE_TOKEN, "")
            ): str,
            # No device_class filter: the installations that need a fallback are
            # exactly the ones without a sensor declared as a battery (template,
            # REST, another inverter), and the filter left the list empty.
            _soc_entity_key(d.get(CONF_SOC_ENTITY)): _OptionalEntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=d.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): _number(10, 600, unit="s"),
        }
    )


class GoodweEmsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a GoodWe EMS config flow."""

    VERSION = 2

    def __init__(self) -> None:
        self._connection: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Find the inverter on the network."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            try:
                inverter, port = await self.async_detect_inverter_port(host=host)
            except InverterError:
                errors[CONF_HOST] = "cannot_connect"
            else:
                await self.async_set_unique_id(inverter.serial_number)
                self._abort_if_unique_id_configured()
                self._connection = {
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_MODEL_FAMILY: type(inverter).__name__,
                }
                return await self.async_step_battery()

        return self.async_show_form(
            step_id="user", data_schema=CONNECTION_SCHEMA, errors=errors
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Battery, price and dispatch parameters."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = _coerce_ints(user_input)
            if user_input[CONF_TARGET_SOC] <= user_input[CONF_MIN_SOC]:
                errors[CONF_TARGET_SOC] = "target_below_min"

            if not errors:
                return self.async_create_entry(
                    title=DEFAULT_NAME,
                    data={**self._connection, **_normalise_soc_entity(user_input)},
                )

        return self.async_show_form(
            step_id="battery", data_schema=_battery_schema(user_input), errors=errors
        )

    @staticmethod
    async def async_detect_inverter_port(host: str) -> tuple[Inverter, int]:
        """Detect which port the inverter listens on.

        UDP 8899 is what a Wi-Fi/LAN dongle exposes and is by far the common
        case; Modbus TCP 502 is the fallback for inverters reached through an
        Ethernet module.
        """
        port = GOODWE_UDP_PORT
        try:
            inverter = await connect(host=host, port=port, retries=3)
        except InverterError:
            port = GOODWE_TCP_PORT
            inverter = await connect(host=host, port=port, retries=3)
        return inverter, port

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return GoodweEmsOptionsFlow()


class GoodweEmsOptionsFlow(OptionsFlow):
    """Battery parameters can change without reconfiguring the connection."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = _coerce_ints(user_input)
            if user_input[CONF_TARGET_SOC] <= user_input[CONF_MIN_SOC]:
                errors[CONF_TARGET_SOC] = "target_below_min"
            if not errors:
                return self.async_create_entry(
                    title="", data=_normalise_soc_entity(user_input)
                )

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_battery_schema(user_input or current),
            errors=errors,
        )
