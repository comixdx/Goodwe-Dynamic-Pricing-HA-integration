"""API de nivel înalt peste harta de registre GoodWe.

Fiecare metodă publică corespunde unei funcții din protocolul ARM 745, nu unui
registru izolat — de exemplu `async_charge_from_grid` scrie codul de producător,
modul EMS și puterea, în ordinea cerută de invertor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .const import (
    BATTERY_POWER_MAX,
    EMS_AUTO,
    EMS_CHARGE_BAT,
    EMS_DISCHARGE_BAT,
    EMS_MODES_REVERSE,
    EMS_POWER_MAX,
    EMS_STOPPED,
    FEED_POWER_MAX,
    FEED_POWER_MIN,
    MANUFACTURER_CODE_EMS,
    REG_ANTI_BACKFLOW,
    REG_BATTERY_CHARGE_LIMIT,
    REG_BATTERY_DISCHARGE_LIMIT,
    REG_CHARGE_DISCHARGE_ENABLE,
    REG_CLEAR_ECONOMIC_SCHEDULE,
    REG_EMS_POWER_MODE,
    REG_EMS_POWER_SET,
    REG_FAST_CHARGE_ENABLE,
    REG_FAST_CHARGE_STOP_SOC,
    REG_FEED_POWER_ENABLE,
    REG_FEED_POWER_PARAM,
    REG_INVERTER_AC_LIMIT,
    REG_MANUFACTURER_CODE,
    REG_MAX_CHARGE_SOC,
    REG_MIN_DISCHARGE_SOC,
    REG_START_CHARGE_SOC,
    REG_STOP_CHARGE_SOC,
    SOC_SCALE,
)
from .modbus import GoodweModbusClient, ModbusError

_LOGGER = logging.getLogger(__name__)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


@dataclass
class InverterState:
    """Instantaneu al registrelor de control citite la fiecare ciclu."""

    ems_mode: int | None = None
    ems_power: int | None = None
    manufacturer_code: int | None = None
    feed_power_enable: bool | None = None
    feed_power_param: int | None = None
    anti_backflow: bool | None = None
    charge_discharge_enable: bool | None = None
    battery_charge_limit: int | None = None
    battery_discharge_limit: int | None = None
    inverter_ac_limit: int | None = None
    min_discharge_soc: int | None = None
    max_charge_soc: int | None = None
    fast_charge_enable: int | None = None
    fast_charge_stop_soc: int | None = None
    start_charge_soc: float | None = None
    stop_charge_soc: float | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ems_mode_name(self) -> str | None:
        if self.ems_mode is None:
            return None
        return EMS_MODES_REVERSE.get(self.ems_mode, f"unknown_{self.ems_mode:#06x}")


class GoodweInverter:
    """Comenzile de control, grupate pe cele patru funcții."""

    def __init__(self, client: GoodweModbusClient) -> None:
        self._client = client
        self._ems_armed = False

    @property
    def client(self) -> GoodweModbusClient:
        return self._client

    # ----------------------------------------------------------------------
    # Citire stare
    # ----------------------------------------------------------------------

    async def async_read_state(self) -> InverterState:
        """Citește toate registrele de control într-un singur ciclu.

        Registrele nu sunt contigue, deci citirea se face în blocuri. Un bloc
        care eșuează nu invalidează restul — câmpurile rămân None și eroarea e
        raportată în `errors`.
        """
        state = InverterState()

        # Bloc 45558-45567: praguri SOC + limite de putere
        try:
            regs = await self._client.async_read(REG_MIN_DISCHARGE_SOC, 10)
            state.min_discharge_soc = regs[0]
            state.max_charge_soc = regs[1]
            state.charge_discharge_enable = bool(regs[6])
            state.battery_charge_limit = regs[7]
            state.battery_discharge_limit = regs[8]
            state.inverter_ac_limit = regs[9]
        except ModbusError as err:
            state.errors.append(f"45558-45567: {err}")

        # Bloc 47505-47512: cod producător, mod EMS, putere EMS
        try:
            regs = await self._client.async_read(REG_MANUFACTURER_CODE, 8)
            state.manufacturer_code = regs[0]
            state.feed_power_enable = bool(regs[4])
            raw = regs[5]
            state.feed_power_param = raw - 0x10000 if raw >= 0x8000 else raw
            state.ems_mode = regs[6]
            state.ems_power = regs[7]
        except ModbusError as err:
            state.errors.append(f"47505-47512: {err}")

        # Bloc 47531-47532: praguri de forțare a încărcării (scalare 10)
        try:
            regs = await self._client.async_read(REG_START_CHARGE_SOC, 2)
            state.start_charge_soc = regs[0] / SOC_SCALE
            state.stop_charge_soc = regs[1] / SOC_SCALE
        except ModbusError as err:
            state.errors.append(f"47531-47532: {err}")

        # Bloc 47545-47546: încărcare rapidă
        try:
            regs = await self._client.async_read(REG_FAST_CHARGE_ENABLE, 2)
            state.fast_charge_enable = regs[0]
            state.fast_charge_stop_soc = regs[1]
        except ModbusError as err:
            state.errors.append(f"47545-47546: {err}")

        # Comutatorul general anti-backflow
        try:
            state.anti_backflow = bool(await self._client.async_read_u16(REG_ANTI_BACKFLOW))
        except ModbusError as err:
            state.errors.append(f"46708: {err}")

        return state

    # ----------------------------------------------------------------------
    # 1. Limitare export
    # ----------------------------------------------------------------------

    async def async_set_export_limit_enabled(self, enabled: bool) -> None:
        await self._client.async_write_verified(REG_FEED_POWER_ENABLE, int(enabled))

    async def async_set_export_limit_power(self, watts: int) -> None:
        """Puterea maximă injectată în rețea. Negativ = import forțat."""
        value = _clamp(watts, FEED_POWER_MIN, FEED_POWER_MAX)
        await self._client.async_write_verified(REG_FEED_POWER_PARAM, value, signed=True)

    async def async_set_anti_backflow(self, enabled: bool) -> None:
        await self._client.async_write_verified(REG_ANTI_BACKFLOW, int(enabled))

    async def async_set_export_limit(self, enabled: bool, watts: int | None = None) -> None:
        """Aplică limita completă: întâi puterea, apoi activarea.

        Ordinea contează — activarea cu un parametru vechi poate lăsa
        invertorul câteva secunde pe o limită nedorită.
        """
        if watts is not None:
            await self.async_set_export_limit_power(watts)
        await self.async_set_export_limit_enabled(enabled)

    # ----------------------------------------------------------------------
    # 2 & 3. Comandă EMS (încărcare / descărcare / din rețea)
    # ----------------------------------------------------------------------

    async def async_arm_ems(self, force: bool = False) -> None:
        """Scrie codul de producător 2 în 47505 — fără el modul EMS e ignorat."""
        if self._ems_armed and not force:
            return
        current = await self._client.async_read_u16(REG_MANUFACTURER_CODE)
        if current != MANUFACTURER_CODE_EMS:
            _LOGGER.debug("Armez EMS: 47505 %s -> %s", current, MANUFACTURER_CODE_EMS)
            await self._client.async_write_verified(
                REG_MANUFACTURER_CODE, MANUFACTURER_CODE_EMS
            )
        self._ems_armed = True

    async def async_set_ems(self, mode: int, power: int = 0) -> None:
        """Setează modul EMS și puterea asociată.

        47511 și 47512 sunt volatile (Save = N în protocol): se pierd la
        repornirea invertorului. Bucla de dispecerizare le rescrie la fiecare
        ciclu, nu o singură dată la începutul ferestrei.
        """
        await self.async_arm_ems()
        power = _clamp(power, 0, EMS_POWER_MAX)
        await self._client.async_write_verified(REG_EMS_POWER_SET, power)
        await self._client.async_write_verified(REG_EMS_POWER_MODE, mode)

    async def async_set_ems_mode(self, mode: int) -> None:
        """Doar modul, păstrând puterea curentă."""
        await self.async_arm_ems()
        await self._client.async_write_verified(REG_EMS_POWER_MODE, mode)

    async def async_set_ems_power(self, watts: int) -> None:
        """Doar puterea, păstrând modul curent."""
        await self.async_arm_ems()
        await self._client.async_write_verified(
            REG_EMS_POWER_SET, _clamp(watts, 0, EMS_POWER_MAX)
        )

    async def async_charge_from_grid(self, watts: int) -> None:
        """Încărcare forțată: PV prioritar, completare din rețea."""
        await self.async_set_ems(EMS_CHARGE_BAT, _clamp(watts, 0, BATTERY_POWER_MAX))

    async def async_discharge(self, watts: int) -> None:
        await self.async_set_ems(EMS_DISCHARGE_BAT, _clamp(watts, 0, BATTERY_POWER_MAX))

    async def async_auto(self) -> None:
        """Autoconsum — starea sigură de repliere."""
        await self.async_set_ems(EMS_AUTO, 0)

    async def async_stop(self) -> None:
        await self.async_set_ems(EMS_STOPPED, 0)

    async def async_set_fast_charge(self, enabled: bool, stop_soc: int | None = None) -> None:
        if stop_soc is not None:
            await self.async_set_fast_charge_stop_soc(stop_soc)
        await self._client.async_write_verified(REG_FAST_CHARGE_ENABLE, int(enabled))

    async def async_set_fast_charge_stop_soc(self, percent: int) -> None:
        """Doar pragul de oprire (47546), fără a atinge activarea (47545).

        Separat de `async_set_fast_charge` pentru că reglarea pragului nu are
        voie să pornească încărcarea din rețea ca efect secundar.
        """
        await self._client.async_write_verified(
            REG_FAST_CHARGE_STOP_SOC, _clamp(percent, 1, 100)
        )

    # ----------------------------------------------------------------------
    # 4. Limite și praguri
    # ----------------------------------------------------------------------

    async def async_set_charge_discharge_enabled(self, enabled: bool) -> None:
        await self._client.async_write_verified(REG_CHARGE_DISCHARGE_ENABLE, int(enabled))

    async def async_set_charge_limit(self, watts: int) -> None:
        await self._client.async_write_verified(
            REG_BATTERY_CHARGE_LIMIT, _clamp(watts, 0, BATTERY_POWER_MAX)
        )

    async def async_set_discharge_limit(self, watts: int) -> None:
        await self._client.async_write_verified(
            REG_BATTERY_DISCHARGE_LIMIT, _clamp(watts, 0, BATTERY_POWER_MAX)
        )

    async def async_set_ac_limit(self, watts: int) -> None:
        await self._client.async_write_verified(
            REG_INVERTER_AC_LIMIT, _clamp(watts, 0, BATTERY_POWER_MAX)
        )

    async def async_set_min_discharge_soc(self, percent: int) -> None:
        await self._client.async_write_verified(
            REG_MIN_DISCHARGE_SOC, _clamp(percent, 0, 100)
        )

    async def async_set_max_charge_soc(self, percent: int) -> None:
        await self._client.async_write_verified(REG_MAX_CHARGE_SOC, _clamp(percent, 0, 100))

    async def async_set_start_charge_soc(self, percent: float) -> None:
        await self._client.async_write_verified(
            REG_START_CHARGE_SOC, _clamp(round(percent * SOC_SCALE), 0, 1000)
        )

    async def async_set_stop_charge_soc(self, percent: float) -> None:
        await self._client.async_write_verified(
            REG_STOP_CHARGE_SOC, _clamp(round(percent * SOC_SCALE), 0, 1000)
        )

    async def async_clear_economic_schedule(self) -> None:
        """Șterge programul economic 47515-47530.

        Necesar înainte de dispecerizarea pe preț: un program economic activ
        concurează cu comenzile EMS și produce comportament neregulat.
        """
        await self._client.async_write(REG_CLEAR_ECONOMIC_SCHEDULE, 1)
