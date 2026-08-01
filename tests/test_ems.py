"""Teste pentru stratul de control EMS, cu un invertor simulat.

Nu au nevoie de Home Assistant. Rulează cu:
    pytest tests/
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from goodwe import EMSMode, InverterError
from goodwe.exceptions import RequestFailedException, RequestRejectedException

ROOT = Path(__file__).resolve().parents[1] / "custom_components"
sys.path.insert(0, str(ROOT))

# Pachetul se încarcă direct, ocolind __init__.py care importă Home Assistant.
_pkg = types.ModuleType("goodwe_ems")
_pkg.__path__ = [str(ROOT / "goodwe_ems")]
sys.modules.setdefault("goodwe_ems", _pkg)

from goodwe_ems.const import (  # noqa: E402
    MANUFACTURER_CODE_EMS,
    REG_CHARGE_ALLOW_WH,
    REG_CLEAR_ECONOMIC_SCHEDULE,
    REG_DISCHARGE_ALLOW_WH,
    REG_MANUFACTURER_CODE,
    REG_BMS_RATED_CAPACITY,
)
from goodwe_ems.ems import GoodweEms  # noqa: E402


class FakeInverter:
    """Spațiu de setări plat; cheile necunoscute întorc zero."""

    serial_number = "9010KETU000000"

    def __init__(
        self,
        settings: dict[str, int] | None = None,
        broken: set[str] | None = None,
    ) -> None:
        self.settings_data: dict[str, int] = settings or {}
        self._broken = broken or set()
        self.writes: list[tuple[str, int]] = []

    async def read_setting(self, setting_id: str) -> int:
        if setting_id in self._broken:
            raise InverterError(f"unsupported {setting_id}")
        return self.settings_data.get(setting_id, 0)

    async def write_setting(self, setting_id: str, value) -> None:
        if setting_id in self._broken:
            raise InverterError(f"unsupported {setting_id}")
        self.writes.append((setting_id, value))
        self.settings_data[setting_id] = value


def modbus(register: int) -> str:
    """Formatul cerut de bibliotecă, verificat în `test_pseudo_setting_format`."""
    return f"modbus_{register}"


def test_pseudo_setting_format() -> None:
    """Biblioteca taie adresa cu `setting_id[7:]`, deci separatorul e obligatoriu.

    Fără el, `modbus47505` s-ar fi citit ca registrul 7505 — o scriere pe cu
    totul altceva, fără nicio eroare.
    """
    from goodwe_ems.ems import pseudo_setting

    assert pseudo_setting(47505) == "modbus_47505"
    assert int(pseudo_setting(47505)[7:]) == 47505


@pytest.fixture(autouse=True)
def _no_readback_delay(monkeypatch):
    """Scurtcircuitează pauza dinaintea citirii de verificare."""
    import goodwe_ems.ems as ems_module

    monkeypatch.setattr(ems_module, "_READBACK_DELAY", 0)


# --------------------------------------------------------------------------
# Armare
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ems_command_arms_the_inverter_first() -> None:
    """Fără codul de producător în 47505, modul EMS e ignorat tăcut."""
    inverter = FakeInverter()
    ems = GoodweEms(inverter)

    await ems.async_charge_from_grid(3000)

    assert inverter.writes[0] == (
        modbus(REG_MANUFACTURER_CODE),
        MANUFACTURER_CODE_EMS,
    )
    assert ("ems_power_limit", 3000) in inverter.writes
    assert ("ems_mode", int(EMSMode.CHARGE_BATTERY)) in inverter.writes


@pytest.mark.asyncio
async def test_arming_is_skipped_when_already_armed() -> None:
    inverter = FakeInverter({modbus(REG_MANUFACTURER_CODE): MANUFACTURER_CODE_EMS})
    ems = GoodweEms(inverter)

    await ems.async_auto()

    assert modbus(REG_MANUFACTURER_CODE) not in [w[0] for w in inverter.writes]


@pytest.mark.asyncio
async def test_arming_happens_once_per_session() -> None:
    inverter = FakeInverter()
    ems = GoodweEms(inverter)

    await ems.async_auto()
    await ems.async_auto()

    armings = [w for w in inverter.writes if w[0] == modbus(REG_MANUFACTURER_CODE)]
    assert len(armings) == 1


# --------------------------------------------------------------------------
# Ordinea și limitele scrierilor
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_power_is_written_before_mode() -> None:
    """Modul activat cu puterea veche lasă invertorul pe un setpoint nedorit."""
    inverter = FakeInverter()
    ems = GoodweEms(inverter)

    await ems.async_set_ems(EMSMode.DISCHARGE_BATTERY, 2500)

    keys = [w[0] for w in inverter.writes]
    assert keys.index("ems_power_limit") < keys.index("ems_mode")


@pytest.mark.asyncio
async def test_power_is_clamped_to_the_battery_rating() -> None:
    inverter = FakeInverter()
    ems = GoodweEms(inverter)

    await ems.async_charge_from_grid(99000)

    assert ("ems_power_limit", 4600) in inverter.writes


@pytest.mark.asyncio
async def test_readback_mismatch_is_logged_not_raised(caplog) -> None:
    """Registrele volatile pot pierde o scriere; asta trebuie să se vadă."""

    class DroppingInverter(FakeInverter):
        async def write_setting(self, setting_id: str, value) -> None:
            self.writes.append((setting_id, value))
            # Scrierea e acceptată, dar nu are efect.

    inverter = DroppingInverter()
    ems = GoodweEms(inverter)

    await ems.async_set_ems(EMSMode.CHARGE_BATTERY, 1000)

    assert "Read-back mismatch" in caplog.text


@pytest.mark.asyncio
async def test_clear_economic_schedule_writes_the_trigger_register() -> None:
    inverter = FakeInverter()
    ems = GoodweEms(inverter)

    await ems.async_clear_economic_schedule()

    assert (modbus(REG_CLEAR_ECONOMIC_SCHEDULE), 1) in inverter.writes


# --------------------------------------------------------------------------
# Citirea limitelor BMS
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_battery_limits_recombine_the_32_bit_halves() -> None:
    inverter = FakeInverter(
        {
            modbus(REG_CHARGE_ALLOW_WH): 0,
            modbus(REG_CHARGE_ALLOW_WH + 1): 5500,
            modbus(REG_DISCHARGE_ALLOW_WH): 0,
            modbus(REG_DISCHARGE_ALLOW_WH + 1): 4200,
            modbus(REG_BMS_RATED_CAPACITY): 100,
        }
    )
    limits = await GoodweEms(inverter).async_read_battery_limits()

    assert limits.charge_allow_kwh == pytest.approx(5.5)
    assert limits.discharge_allow_kwh == pytest.approx(4.2)
    assert limits.rated_capacity_kwh == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_high_half_is_masked_back_to_unsigned() -> None:
    """Pseudo-setarea întoarce valori cu semn; jumătatea înaltă nu e negativă."""
    inverter = FakeInverter(
        {
            modbus(REG_CHARGE_ALLOW_WH): -1,  # 0xFFFF pe fir
            modbus(REG_CHARGE_ALLOW_WH + 1): 0,
            modbus(REG_DISCHARGE_ALLOW_WH): 0,
            modbus(REG_DISCHARGE_ALLOW_WH + 1): 1000,
        }
    )
    limits = await GoodweEms(inverter).async_read_battery_limits()

    assert limits.charge_allow_kwh == pytest.approx(0xFFFF * 65.536)


@pytest.mark.asyncio
async def test_both_zero_means_the_registers_are_unpopulated() -> None:
    """O baterie nu poate fi simultan plină și goală."""
    limits = await GoodweEms(FakeInverter()).async_read_battery_limits()

    assert limits.charge_allow_kwh is None
    assert limits.discharge_allow_kwh is None


class _RaisingInverter(FakeInverter):
    """Ridică o excepție dată la citirea blocului BMS, și numără citirile."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error
        self.reads = 0

    async def read_setting(self, setting_id: str) -> int:
        self.reads += 1
        if setting_id == modbus(REG_CHARGE_ALLOW_WH):
            raise self._error
        return await super().read_setting(setting_id)


@pytest.mark.asyncio
async def test_unsupported_bms_block_is_probed_only_once() -> None:
    """Un model fără registrele astea nu trebuie interogat la fiecare ciclu."""
    inverter = _RaisingInverter(RequestRejectedException("ILLEGAL DATA ADDRESS"))
    ems = GoodweEms(inverter)

    assert (await ems.async_read_battery_limits()).charge_allow_kwh is None
    after_first = inverter.reads
    await ems.async_read_battery_limits()

    assert inverter.reads == after_first


@pytest.mark.asyncio
async def test_transient_failure_does_not_disable_the_bms_block() -> None:
    """Regresie: un pachet UDP pierdut nu are voie să oprească definitiv citirea.

    `RequestFailedException` derivă din `InverterError`, deci o prindere prea
    largă marca blocul drept nesuportat la primul timeout și dispecerizarea
    pierdea limitele BMS pentru tot restul sesiunii.
    """
    inverter = _RaisingInverter(RequestFailedException("timeout", 1))
    ems = GoodweEms(inverter)

    assert (await ems.async_read_battery_limits()).charge_allow_kwh is None
    after_first = inverter.reads
    await ems.async_read_battery_limits()

    assert inverter.reads > after_first
