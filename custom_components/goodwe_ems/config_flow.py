"""Config flow: conexiune Modbus, apoi parametrii bateriei."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_BAUDRATE,
    CONF_CONNECTION,
    CONF_CYCLE_COST,
    CONF_ENABLE_DISPATCH,
    CONF_ENTSOE_TOKEN,
    CONF_HOLD_FOR_PEAK,
    CONF_HOST,
    CONF_MAX_CHARGE_POWER,
    CONF_MAX_DISCHARGE_POWER,
    CONF_MIN_SOC,
    CONF_PORT,
    CONF_ROUND_TRIP_EFFICIENCY,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL_PORT,
    CONF_SLAVE,
    CONF_SOC_ENTITY,
    CONF_TARGET_SOC,
    CONNECTION_SERIAL,
    CONNECTION_TCP,
    CONNECTION_TYPES,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_BAUDRATE,
    DEFAULT_CYCLE_COST,
    DEFAULT_HOLD_FOR_PEAK,
    DEFAULT_MAX_CHARGE_POWER,
    DEFAULT_MAX_DISCHARGE_POWER,
    DEFAULT_MIN_SOC,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_ROUND_TRIP_EFFICIENCY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLAVE,
    DEFAULT_TARGET_SOC,
    DOMAIN,
    REG_MANUFACTURER_CODE,
)
from .modbus import GoodweModbusClient, ModbusError


def _number(
    minimum: float,
    maximum: float,
    step: float = 1,
    unit: str | None = None,
    slider: bool = False,
) -> selector.NumberSelector:
    """Câmp numeric cu limite și unitate vizibile în interfață.

    Un `int` simplu apare ca o casetă de text fără nicio indicație despre
    intervalul acceptat, iar utilizatorul află că a greșit abia la trimitere.
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
        # Cheia trebuie să lipsească atunci când nu există unitate: schema
        # selectorului cere un șir, iar None ar arunca vol.Invalid.
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


# NumberSelector întoarce întotdeauna float. pymodbus vrea int pentru port,
# slave și baudrate, iar registrele de SOC/putere se scriu ca U16.
_INT_KEYS = (
    CONF_PORT,
    CONF_BAUDRATE,
    CONF_SLAVE,
    CONF_MAX_CHARGE_POWER,
    CONF_MAX_DISCHARGE_POWER,
    CONF_MIN_SOC,
    CONF_TARGET_SOC,
    CONF_SCAN_INTERVAL,
)


def _coerce_ints(user_input: dict[str, Any]) -> dict[str, Any]:
    """Readuce la int valorile pe care selectorul le-a transformat în float."""
    return {
        key: int(value) if key in _INT_KEYS and value is not None else value
        for key, value in user_input.items()
    }


def _connection_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_CONNECTION, default=d.get(CONF_CONNECTION, CONNECTION_TCP)
            ): (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=CONNECTION_TYPES,
                        translation_key="connection",
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            ),
            vol.Optional(CONF_HOST, default=d.get(CONF_HOST, "")): str,
            vol.Optional(CONF_PORT, default=d.get(CONF_PORT, DEFAULT_PORT)): _number(
                1, 65535
            ),
            vol.Optional(CONF_SERIAL_PORT, default=d.get(CONF_SERIAL_PORT, "")): str,
            vol.Optional(
                CONF_BAUDRATE, default=d.get(CONF_BAUDRATE, DEFAULT_BAUDRATE)
            ): _number(1200, 115200, step=1, unit="bps"),
            vol.Required(CONF_SLAVE, default=d.get(CONF_SLAVE, DEFAULT_SLAVE)): _number(
                1, 247
            ),
        }
    )


class _OptionalEntitySelector(selector.EntitySelector):
    """EntitySelector care acceptă și „nicio entitate”.

    EntitySelector validează valoarea cu `entity_id_or_uuid`, iar un câmp golit
    nu trece („Entity is neither a valid entity ID nor a valid UUID”): un
    formular fără senzor de rezervă era imposibil de trimis. Interfața poate
    trimite cheia lipsă, `None` sau șirul gol, în funcție de versiune, așa că
    toate trei sunt tratate la fel.

    Un `vol.Any(selector, "", None)` ar fi părut mai direct, dar
    voluptuous_serialize nu știe să serializeze o alternativă cu literali, deci
    formularul nici nu s-ar mai fi afișat. Serializarea aici rămâne cea a
    clasei de bază.
    """

    def __call__(self, data: Any) -> str:
        if data is None or data == "":
            return ""
        return str(super().__call__(data))


def _soc_entity_key(current: str | None) -> vol.Marker:
    """Cheia selectorului: fără `default`, doar cu valoarea deja aleasă.

    Un `default=""` era exact ce refuza validarea, iar o valoare sugerată
    `None` ar ajunge tot acolo.
    """
    if current:
        return vol.Optional(CONF_SOC_ENTITY, description={"suggested_value": current})
    return vol.Optional(CONF_SOC_ENTITY)


def _normalise_soc_entity(user_input: dict[str, Any]) -> dict[str, Any]:
    """Golirea selectorului trebuie să șteargă entitatea, nu s-o păstreze.

    Opțiunile se citesc ca `{**entry.data, **entry.options}`, deci o cheie
    absentă din opțiuni lasă în picioare valoarea veche din date. Scriem
    explicit șirul gol.
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
                default=d.get(CONF_ROUND_TRIP_EFFICIENCY, DEFAULT_ROUND_TRIP_EFFICIENCY),
            ): _number(0.5, 1.0, step=0.01),
            vol.Optional(CONF_ENTSOE_TOKEN, default=d.get(CONF_ENTSOE_TOKEN, "")): str,
            # Fără filtru pe device_class: instalațiile care au nevoie de rezervă
            # sunt exact cele care nu au un senzor de baterie declarat ca atare
            # (template, REST, alt invertor), iar filtrul lăsa lista goală.
            _soc_entity_key(d.get(CONF_SOC_ENTITY)): _OptionalEntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(
                CONF_SCAN_INTERVAL, default=d.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ): _number(10, 600, unit="s"),
        }
    )


async def _async_probe(data: dict[str, Any]) -> None:
    """Deschide conexiunea și citește 47505 ca test de viață."""
    client = GoodweModbusClient(
        connection=data[CONF_CONNECTION],
        slave=data[CONF_SLAVE],
        host=data.get(CONF_HOST) or None,
        port=data.get(CONF_PORT, DEFAULT_PORT),
        serial_port=data.get(CONF_SERIAL_PORT) or None,
        baudrate=data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
    )
    try:
        await client.async_connect()
        await client.async_read_u16(REG_MANUFACTURER_CODE)
    finally:
        await client.async_close()


class GoodweEmsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._connection: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = _coerce_ints(user_input)
            if user_input[CONF_CONNECTION] == CONNECTION_SERIAL:
                if not user_input.get(CONF_SERIAL_PORT):
                    errors[CONF_SERIAL_PORT] = "serial_port_required"
            elif not user_input.get(CONF_HOST):
                errors[CONF_HOST] = "host_required"

            if not errors:
                try:
                    await _async_probe(user_input)
                except ModbusError:
                    errors["base"] = "cannot_connect"
                except Exception:  # noqa: BLE001
                    errors["base"] = "unknown"

            if not errors:
                unique = (
                    user_input.get(CONF_SERIAL_PORT)
                    or f"{user_input.get(CONF_HOST)}:{user_input.get(CONF_PORT)}"
                )
                await self.async_set_unique_id(f"{unique}/{user_input[CONF_SLAVE]}")
                self._abort_if_unique_id_configured()
                self._connection = user_input
                return await self.async_step_battery()

        return self.async_show_form(
            step_id="user", data_schema=_connection_schema(user_input), errors=errors
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return GoodweEmsOptionsFlow()


class GoodweEmsOptionsFlow(OptionsFlow):
    """Parametrii bateriei se pot schimba fără a reconfigura conexiunea."""

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
