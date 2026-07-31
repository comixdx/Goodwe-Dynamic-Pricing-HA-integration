"""Teste pentru citirea telemetriei, cu un client Modbus simulat."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components"
sys.path.insert(0, str(ROOT))
_pkg = types.ModuleType("goodwe_ems")
_pkg.__path__ = [str(ROOT / "goodwe_ems")]
sys.modules.setdefault("goodwe_ems", _pkg)

from goodwe_ems.modbus import ModbusError  # noqa: E402
from goodwe_ems.readings import GoodweReader  # noqa: E402


def u32(value: int) -> tuple[int, int]:
    return (value >> 16) & 0xFFFF, value & 0xFFFF


def s32(value: int) -> tuple[int, int]:
    return u32(value + 0x100000000 if value < 0 else value)


def ascii_regs(text: str, registers: int) -> list[int]:
    raw = text.encode("ascii").ljust(registers * 2, b"\x00")
    return [(raw[i] << 8) | raw[i + 1] for i in range(0, registers * 2, 2)]


class FakeClient:
    """Spațiu de registre plat; adresele necunoscute întorc zero."""

    def __init__(self, registers: dict[int, int], broken: set[int] | None = None) -> None:
        self._regs = registers
        self._broken = broken or set()
        self.reads: list[tuple[int, int]] = []

    async def async_read(self, address: int, count: int = 1) -> list[int]:
        self.reads.append((address, count))
        if address in self._broken:
            raise ModbusError(f"bloc indisponibil la {address}")
        return [self._regs.get(address + i, 0) for i in range(count)]


def build_registers() -> dict[int, int]:
    regs: dict[int, int] = {}

    def put32(address: int, pair: tuple[int, int]) -> None:
        regs[address], regs[address + 1] = pair

    # Identificare
    regs[35001] = 10000
    for i, value in enumerate(ascii_regs("9010KETU123456AB", 8)):
        regs[35003 + i] = value
    for i, value in enumerate(ascii_regs("GW10K-ET", 5)):
        regs[35011 + i] = value

    # Putere
    put32(35105, u32(2100))  # PV1
    put32(35109, u32(1800))  # PV2
    put32(35137, s32(3600))  # putere totală invertor
    put32(35139, s32(-1200))  # activ contor: negativ = injecție
    put32(35141, s32(150))
    put32(35143, s32(3700))
    put32(35169, s32(400))  # backup
    put32(35171, s32(2400))  # consum total
    put32(35182, s32(-500))  # baterie
    put32(35191, u32(123456))  # 12345,6 kWh producție PV totală
    put32(35196, u32(78901))  # 7890,1 kWh exportați
    put32(35199, u32(54321))  # 5432,1 kWh importați
    put32(35206, u32(12345))  # 1234,5 kWh
    put32(35209, u32(9876))
    regs[35212] = 4  # module baterie
    put32(35264, s32(-250))  # al doilea pachet de baterii
    regs[35267] = 2
    put32(35301, u32(3900))  # PV total
    regs[35303] = 2

    # BMS
    regs[37007] = 62  # SOC
    regs[37008] = 99  # SOH
    put32(37056, u32(45678))  # 4567,8 kWh
    put32(37058, u32(43210))
    regs[37076] = 96  # 9,6 kWh
    regs[39074] = 96  # al doilea pachet

    # Energie permisă
    put32(10476, u32(3400))  # 3,4 kWh
    put32(10478, u32(5100))  # 5,1 kWh
    return regs


@pytest.fixture
def reader() -> tuple[GoodweReader, FakeClient]:
    client = FakeClient(build_registers())
    return GoodweReader(client), client


def test_device_info(reader) -> None:
    r, _ = reader
    info = asyncio.run(r.async_read_device_info())
    assert info.serial_number == "9010KETU123456AB"
    assert info.model_name == "GW10K-ET"
    assert info.rate_power == 10000


def test_power_readings(reader) -> None:
    r, _ = reader
    data = asyncio.run(r.async_read())
    assert data.pv_power == 3900
    assert data.pv1_power == 2100
    assert data.inverter_power == 3600
    assert data.load_power == 2400
    assert data.backup_load_power == 400
    assert data.errors == []


def test_signed_values_survive_the_round_trip(reader) -> None:
    """Injecția în rețea și descărcarea bateriei sunt valori negative."""
    r, _ = reader
    data = asyncio.run(r.async_read())
    assert data.ac_active_power == -1200
    assert data.battery_power == -500


def test_scale_factors(reader) -> None:
    r, _ = reader
    data = asyncio.run(r.async_read())
    assert data.rated_capacity_kwh == pytest.approx(9.6)
    assert data.total_capacity_kwh == pytest.approx(19.2)  # două pachete
    assert data.total_charge_energy_kwh == pytest.approx(4567.8)
    assert data.energy_charge_kwh == pytest.approx(1234.5)
    assert data.charge_allow_kwh == pytest.approx(3.4)  # Wh -> kWh
    assert data.discharge_allow_kwh == pytest.approx(5.1)


def test_energy_counters(reader) -> None:
    """Contoarele care alimentează tabloul Energy, în pași de 0,1 kWh."""
    r, _ = reader
    data = asyncio.run(r.async_read())
    assert data.pv_energy_total_kwh == pytest.approx(12345.6)
    assert data.grid_export_energy_kwh == pytest.approx(7890.1)
    assert data.grid_import_energy_kwh == pytest.approx(5432.1)


def test_counters_come_from_a_block_already_read(reader) -> None:
    """Contoarele stau în 35169..35212, deci nu costă nicio tranzacție în plus.

    Dacă cineva le mută pe un bloc propriu, testul cade — la 9600 bps o citire
    separată pentru trei registre e exact ce încearcă `_block` să evite.
    """
    r, client = reader
    asyncio.run(r.async_read())
    covering = [
        (start, count)
        for start, count in client.reads
        if start <= 35191 and start + count > 35200
    ]
    assert covering == [(35169, 44)]


@pytest.mark.parametrize(
    ("address", "field"),
    [
        (35191, "pv_energy_total_kwh"),
        (35196, "grid_export_energy_kwh"),
        (35199, "grid_import_energy_kwh"),
    ],
)
def test_unpopulated_counter_is_dropped(address: int, field: str) -> None:
    """0xFFFFFFFF înseamnă registru nepopulat, nu 429 GWh.

    Valoarea ar intra o dată în statisticile de lungă durată ale unui senzor
    `total_increasing` și ar rămâne acolo până la ștergerea manuală, așa că
    lipsa senzorului e preferabilă.
    """
    regs = build_registers()
    regs[address] = regs[address + 1] = 0xFFFF
    data = asyncio.run(GoodweReader(FakeClient(regs)).async_read())
    assert getattr(data, field) is None

    # Un contor stricat nu-i ia cu el pe ceilalți doi din același bloc.
    others = {
        "pv_energy_total_kwh",
        "grid_export_energy_kwh",
        "grid_import_energy_kwh",
    } - {field}
    assert all(getattr(data, name) is not None for name in others)


def test_zero_counter_is_believed() -> None:
    """O instalație nou pusă în funcțiune chiar are zero kWh produși."""
    regs = build_registers()
    regs[35191] = regs[35192] = 0
    data = asyncio.run(GoodweReader(FakeClient(regs)).async_read())
    assert data.pv_energy_total_kwh == 0.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (9_999_999, 999_999.9),  # sub prag: acceptat
        (10_000_000, None),  # exact pragul: respins
    ],
)
def test_counter_plausibility_threshold(raw: int, expected: float | None) -> None:
    """Pragul e la 1 GWh, o valoare pe care o casă nu o atinge niciodată."""
    regs = build_registers()
    regs[35191], regs[35192] = u32(raw)
    data = asyncio.run(GoodweReader(FakeClient(regs)).async_read())
    if expected is None:
        assert data.pv_energy_total_kwh is None
    else:
        assert data.pv_energy_total_kwh == pytest.approx(expected)


def test_second_pack_read_when_present(reader) -> None:
    r, _ = reader
    data = asyncio.run(r.async_read())
    assert data.battery2_power == -250
    assert data.total_battery_power == -750  # ambele pachete


def test_second_pack_ignored_when_absent() -> None:
    """Fără module raportate pe pachetul 2, puterea zero nu devine un senzor."""
    regs = build_registers()
    regs[35267] = 0
    for address in (35264, 35265):
        regs[address] = 0
    data = asyncio.run(GoodweReader(FakeClient(regs)).async_read())
    assert data.battery2_power is None
    assert data.total_battery_power == -500  # doar pachetul 1


def test_zero_allow_pair_is_treated_as_unsupported() -> None:
    """Baterie simultan plină și goală = registre nepopulate, nu date reale."""
    regs = build_registers()
    for address in (10476, 10477, 10478, 10479):
        regs[address] = 0
    data = asyncio.run(GoodweReader(FakeClient(regs)).async_read())
    assert data.charge_allow_kwh is None
    assert data.discharge_allow_kwh is None


def test_single_zero_allow_is_believed() -> None:
    """Bateria chiar poate fi plină: zero pe un singur registru e credibil."""
    regs = build_registers()
    regs[10476] = regs[10477] = 0
    data = asyncio.run(GoodweReader(FakeClient(regs)).async_read())
    assert data.charge_allow_kwh == 0.0
    assert data.discharge_allow_kwh == pytest.approx(5.1)


def test_failing_block_does_not_lose_the_others() -> None:
    client = FakeClient(build_registers(), broken={37007})
    data = asyncio.run(GoodweReader(client).async_read())
    assert data.soc is None
    assert data.pv_power == 3900  # restul a supraviețuit
    assert len(data.errors) == 1


def test_read_is_batched_into_few_transactions(reader) -> None:
    """La 9600 bps fiecare tranzacție costă; nu citim registru cu registru."""
    r, client = reader
    asyncio.run(r.async_read())
    assert len(client.reads) <= 10
