"""Citirea datelor instantanee din invertor.

Registrele de telemetrie sunt împrăștiate pe intervale largi, deci citirea se
face în blocuri: mai puține tranzacții Modbus decât un registru pe rând, dar
fără să cerem sute de registre pe care nu le folosim. Un bloc care eșuează nu
invalidează restul — câmpurile lui rămân None.

Sursa: GoodWe ARM 745 Modbus Protocol Map, revizia 28.03.2025.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Final

from .modbus import GoodweModbusClient, ModbusError, to_signed32

_LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Registre statice, citite o singură dată la pornire
# --------------------------------------------------------------------------

REG_RATE_POWER: Final = 35001  # U16 RO — unitatea nu e precizată în hartă
REG_INVERTER_SN: Final = 35003  # STR, 8 registre / 16 octeți ASCII
REG_MODEL_NAME: Final = 35011  # STR, 5 registre / 10 octeți ASCII
STATIC_BLOCK: Final = (35001, 15)  # 35001..35015

# --------------------------------------------------------------------------
# Blocuri de telemetrie: (adresă de start, număr de registre)
# --------------------------------------------------------------------------

BLOCK_PV_AC: Final = (35105, 41)  # 35105..35145
BLOCK_LOAD_BATTERY: Final = (35169, 44)  # 35169..35212
BLOCK_BATTERY2: Final = (35264, 5)  # 35264..35268
BLOCK_PV_TOTAL: Final = (35301, 4)  # 35301..35304
BLOCK_BMS_SOC: Final = (37007, 3)  # 37007..37009
BLOCK_BMS_ENERGY: Final = (37056, 22)  # 37056..37077
BLOCK_BMS2_CAPACITY: Final = (39074, 1)
BLOCK_ALLOW: Final = (10473, 8)  # 10473..10480

# Deplasamente în interiorul blocurilor
_PV1_POWER: Final = 35105
_PV2_POWER: Final = 35109
_PV3_POWER: Final = 35113
_PV4_POWER: Final = 35117
_INVERTER_R: Final = 35124
_INVERTER_S: Final = 35129
_INVERTER_T: Final = 35134
_TOTAL_INVERTER_POWER: Final = 35137
_AC_ACTIVE_POWER: Final = 35139
_AC_REACTIVE_POWER: Final = 35141
_AC_APPARENT_POWER: Final = 35143

_BACKUP_LOAD_POWER: Final = 35169
_TOTAL_LOAD_POWER: Final = 35171
_BATTERY1_POWER: Final = 35182
_ENERGY_CHARGE: Final = 35206
_ENERGY_DISCHARGE: Final = 35209
_BATTERY_STRINGS: Final = 35212

_BATTERY2_POWER: Final = 35264
_BATTERY_STRINGS2: Final = 35267

_PV_TOTAL_POWER: Final = 35301
_PV_CHANNELS: Final = 35303

_BMS_SOC: Final = 37007
_BMS_SOH: Final = 37008

_TOTAL_CHARGE_ENERGY: Final = 37056
_TOTAL_DISCHARGE_ENERGY: Final = 37058
_BMS1_RATED_CAPACITY: Final = 37076

_BATTERY_CAPACITY_AGG: Final = 10473
_CHARGE_ALLOW_WH: Final = 10476
_DISCHARGE_ALLOW_WH: Final = 10478


@dataclass
class DeviceInfoData:
    """Identitatea invertorului, citită o singură dată."""

    serial_number: str | None = None
    model_name: str | None = None
    rate_power: int | None = None


@dataclass
class LiveData:
    """Un instantaneu de telemetrie."""

    # Putere
    pv_power: int | None = None
    pv1_power: int | None = None
    pv2_power: int | None = None
    pv3_power: int | None = None
    pv4_power: int | None = None
    pv_channels: int | None = None
    inverter_power: int | None = None
    ac_active_power: int | None = None
    ac_reactive_power: int | None = None
    ac_apparent_power: int | None = None
    load_power: int | None = None
    backup_load_power: int | None = None
    battery_power: int | None = None
    battery2_power: int | None = None

    # Stare baterie
    soc: int | None = None
    soh: int | None = None
    battery_strings: int | None = None
    rated_capacity_kwh: float | None = None
    rated_capacity2_kwh: float | None = None
    charge_allow_kwh: float | None = None
    discharge_allow_kwh: float | None = None

    # Energie cumulată
    total_charge_energy_kwh: float | None = None
    total_discharge_energy_kwh: float | None = None
    energy_charge_kwh: float | None = None
    energy_discharge_kwh: float | None = None

    errors: list[str] = field(default_factory=list)

    @property
    def total_capacity_kwh(self) -> float | None:
        """Suma pachetelor BMS, dacă cel puțin unul a răspuns."""
        packs = [c for c in (self.rated_capacity_kwh, self.rated_capacity2_kwh) if c]
        return sum(packs) if packs else None

    @property
    def total_battery_power(self) -> int | None:
        packs = [p for p in (self.battery_power, self.battery2_power) if p is not None]
        return sum(packs) if packs else None


class _Block:
    """Acces prin adresă absolută la un bloc de registre citit."""

    def __init__(self, start: int, registers: list[int]) -> None:
        self._start = start
        self._regs = registers

    def u16(self, address: int) -> int:
        return self._regs[address - self._start]

    def s16(self, address: int) -> int:
        raw = self.u16(address)
        return raw - 0x10000 if raw >= 0x8000 else raw

    def u32(self, address: int) -> int:
        i = address - self._start
        return (self._regs[i] << 16) | self._regs[i + 1]

    def s32(self, address: int) -> int:
        i = address - self._start
        return to_signed32(self._regs[i], self._regs[i + 1])

    def string(self, address: int, registers: int) -> str:
        i = address - self._start
        raw = bytearray()
        for reg in self._regs[i : i + registers]:
            raw += bytes(((reg >> 8) & 0xFF, reg & 0xFF))
        return raw.decode("ascii", errors="ignore").strip("\x00 ").strip()


class GoodweReader:
    """Citește telemetria, tolerând blocuri indisponibile."""

    def __init__(self, client: GoodweModbusClient) -> None:
        self._client = client

    async def async_read_device_info(self) -> DeviceInfoData:
        info = DeviceInfoData()
        try:
            block = _Block(
                STATIC_BLOCK[0], await self._client.async_read(*STATIC_BLOCK)
            )
        except ModbusError as err:
            _LOGGER.debug("Blocul de identificare nu a putut fi citit: %s", err)
            return info

        info.rate_power = block.u16(REG_RATE_POWER) or None
        info.serial_number = block.string(REG_INVERTER_SN, 8) or None
        info.model_name = block.string(REG_MODEL_NAME, 5) or None
        return info

    async def async_read(self) -> LiveData:
        data = LiveData()

        if (b := await self._block(BLOCK_PV_AC, data)) is not None:
            data.pv1_power = b.u32(_PV1_POWER)
            data.pv2_power = b.u32(_PV2_POWER)
            data.pv3_power = b.u32(_PV3_POWER)
            data.pv4_power = b.u32(_PV4_POWER)
            data.inverter_power = b.s32(_TOTAL_INVERTER_POWER)
            data.ac_active_power = b.s32(_AC_ACTIVE_POWER)
            data.ac_reactive_power = b.s32(_AC_REACTIVE_POWER)
            data.ac_apparent_power = b.s32(_AC_APPARENT_POWER)

        if (b := await self._block(BLOCK_LOAD_BATTERY, data)) is not None:
            data.backup_load_power = b.s32(_BACKUP_LOAD_POWER)
            data.load_power = b.s32(_TOTAL_LOAD_POWER)
            data.battery_power = b.s32(_BATTERY1_POWER)
            data.energy_charge_kwh = b.u32(_ENERGY_CHARGE) / 10
            data.energy_discharge_kwh = b.u32(_ENERGY_DISCHARGE) / 10
            data.battery_strings = b.u16(_BATTERY_STRINGS)

        if (b := await self._block(BLOCK_BATTERY2, data)) is not None:
            power = b.s32(_BATTERY2_POWER)
            # Al doilea pachet lipsește pe majoritatea instalațiilor; registrul
            # întoarce zero, ceea ce nu se distinge de „inactiv". Îl păstrăm
            # doar dacă numărul de module raportat e nenul.
            data.battery2_power = power if b.u16(_BATTERY_STRINGS2) else None

        if (b := await self._block(BLOCK_PV_TOTAL, data)) is not None:
            data.pv_power = b.u32(_PV_TOTAL_POWER)
            data.pv_channels = b.u16(_PV_CHANNELS)

        if (b := await self._block(BLOCK_BMS_SOC, data)) is not None:
            data.soc = b.u16(_BMS_SOC)
            data.soh = b.u16(_BMS_SOH)

        if (b := await self._block(BLOCK_BMS_ENERGY, data)) is not None:
            data.total_charge_energy_kwh = b.u32(_TOTAL_CHARGE_ENERGY) / 10
            data.total_discharge_energy_kwh = b.u32(_TOTAL_DISCHARGE_ENERGY) / 10
            data.rated_capacity_kwh = (b.u16(_BMS1_RATED_CAPACITY) / 10) or None

        if (b := await self._block(BLOCK_BMS2_CAPACITY, data)) is not None:
            data.rated_capacity2_kwh = (b.u16(39074) / 10) or None

        if (b := await self._block(BLOCK_ALLOW, data)) is not None:
            # Wh în hartă; le ținem în kWh, ca restul energiilor.
            data.charge_allow_kwh = b.u32(_CHARGE_ALLOW_WH) / 1000
            data.discharge_allow_kwh = b.u32(_DISCHARGE_ALLOW_WH) / 1000

            # O baterie nu poate fi simultan plină și goală. Dacă ambele ies
            # zero, registrele nu sunt populate pe acest model, iar zerourile
            # ar bloca dispecerizarea complet. Zero pe unul singur e credibil.
            if not data.charge_allow_kwh and not data.discharge_allow_kwh:
                data.charge_allow_kwh = data.discharge_allow_kwh = None

        return data

    async def _block(self, block: tuple[int, int], data: LiveData) -> _Block | None:
        try:
            return _Block(block[0], await self._client.async_read(*block))
        except ModbusError as err:
            data.errors.append(f"{block[0]}+{block[1]}: {err}")
            return None
