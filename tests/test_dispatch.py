"""Teste pentru logica pură: serie de prețuri și dispecerizare.

Nu au nevoie de Home Assistant și nici de un invertor. Rulează cu:
    pytest tests/
"""

from __future__ import annotations

import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components"
sys.path.insert(0, str(ROOT))

# Pachetul se încarcă direct, ocolind __init__.py care importă Home Assistant.
_pkg = types.ModuleType("goodwe_ems")
_pkg.__path__ = [str(ROOT / "goodwe_ems")]
sys.modules.setdefault("goodwe_ems", _pkg)

from goodwe_ems import dispatch  # noqa: E402
from goodwe_ems.dispatch import BatteryState  # noqa: E402
from goodwe_ems.const import (  # noqa: E402
    DISPATCH_AUTO,
    DISPATCH_CHARGE_GRID,
    DISPATCH_DISCHARGE,
    DISPATCH_HOLD,
    DISPATCH_UNAVAILABLE,
    EMS_BATTERY_STANDBY,
)
from goodwe_ems.pzu_prices import (  # noqa: E402
    RO_TZ,
    PriceError,
    PriceSeries,
    cheapest_window,
    expected_mtu_count,
    lei_per_kwh,
    monthly_settlement,
    most_expensive_window,
    parse_ro_number,
    spread,
)

TODAY = datetime.now(RO_TZ).date()
CONFIG = dispatch.DispatchConfig(
    capacity_kwh=10.0,
    max_charge_power_w=4600,
    max_discharge_power_w=4600,
    min_soc=15,
    target_soc=95,
    round_trip_efficiency=0.90,
    cycle_cost_lei_mwh=150.0,
)
CONFIG_HOLD = dispatch.DispatchConfig(**{**CONFIG.__dict__, "hold_for_peak": True})


def _profile(day: date = TODAY) -> list[float]:
    """Zi tipică: gol noaptea, prânz ieftin de la PV, vârf seara."""
    values = []
    for i in range(expected_mtu_count(day)):
        hour = i // 4
        if hour < 5:
            values.append(180.0)
        elif 10 <= hour < 15:
            values.append(320.0)
        elif 19 <= hour < 22:
            values.append(1250.0)
        else:
            values.append(600.0)
    return values


def _series(values: list[float] | None = None, day: date = TODAY, **kwargs) -> PriceSeries:
    return PriceSeries(
        day=day,
        values=tuple(values if values is not None else _profile(day)),
        currency=kwargs.get("currency", "RON"),
        source=kwargs.get("source", "test"),
        fetched_at=kwargs.get("fetched_at", datetime.now(RO_TZ)),
    )


def _at(hour: int, minute: int = 0, day: date = TODAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=RO_TZ)


# --------------------------------------------------------------------------
# Numărarea intervalelor
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 7, 15), 96),
        (date(2026, 3, 29), 92),  # ora se dă înainte, ziua are 23 h
        (date(2026, 10, 25), 100),  # ora se dă înapoi, ziua are 25 h
    ],
)
def test_mtu_count_dst(day: date, expected: int) -> None:
    assert expected_mtu_count(day) == expected


# --------------------------------------------------------------------------
# Validarea seriei
# --------------------------------------------------------------------------


def test_series_rejects_wrong_length() -> None:
    with pytest.raises(PriceError):
        _series(_profile()[:50])


def test_series_rejects_implausible_price() -> None:
    values = _profile()
    values[3] = 99_999.0
    with pytest.raises(PriceError):
        _series(values)


def test_stale_series_is_not_actionable() -> None:
    yesterday = TODAY - timedelta(days=1)
    series = _series(
        _profile(yesterday),
        day=yesterday,
        fetched_at=datetime.now(RO_TZ) - timedelta(days=1),
    )
    assert series.is_actionable() is False


def test_old_fetch_of_today_is_not_actionable() -> None:
    series = _series(fetched_at=datetime.now(RO_TZ) - timedelta(hours=19))
    assert series.is_actionable() is False


def test_hourly_averages_collapse_quarters() -> None:
    series = _series()
    assert len(series.hourly_averages()) == len(series.values) // 4
    assert series.hourly_averages()[0] == pytest.approx(180.0)


# --------------------------------------------------------------------------
# Ferestre
# --------------------------------------------------------------------------


def test_cheapest_window_finds_the_night() -> None:
    start, price = cheapest_window(_series(), 8)
    assert start < 12  # înainte de 03:00
    assert price == pytest.approx(180.0)


def test_most_expensive_window_finds_the_evening_peak() -> None:
    start, price = most_expensive_window(_series(), 8)
    assert 76 <= start <= 80  # în jur de 19:00
    assert price == pytest.approx(1250.0)


def test_spread() -> None:
    assert spread(_series()) == pytest.approx(1070.0)


# --------------------------------------------------------------------------
# Decizii
# --------------------------------------------------------------------------


def test_charges_in_the_cheap_night_window() -> None:
    decision = dispatch.plan(_series(), BatteryState(soc=20), CONFIG, now=_at(2))
    assert decision.state == DISPATCH_CHARGE_GRID
    assert decision.power_w == CONFIG.max_charge_power_w


def test_discharges_during_the_evening_peak() -> None:
    """Regresie: ordinea încărcare-întâi rata vârful dacă ciclul cădea în el."""
    decision = dispatch.plan(_series(), BatteryState(soc=80), CONFIG, now=_at(20))
    assert decision.state == DISPATCH_DISCHARGE


def test_does_not_charge_above_target_soc() -> None:
    decision = dispatch.plan(_series(), BatteryState(soc=96), CONFIG, now=_at(2))
    assert decision.state != DISPATCH_CHARGE_GRID


def test_does_not_discharge_at_min_soc() -> None:
    decision = dispatch.plan(_series(), BatteryState(soc=15), CONFIG, now=_at(20))
    assert decision.state != DISPATCH_DISCHARGE


def test_flat_day_never_arbitrages() -> None:
    flat = _series([500.0 + (i % 4) * 5 for i in range(expected_mtu_count(TODAY))])
    states = {
        dispatch.plan(flat, BatteryState(soc=50), CONFIG, now=_at(hour)).state
        for hour in range(23)
    }
    assert states == {DISPATCH_AUTO}


def test_missing_prices_fall_back_to_auto() -> None:
    decision = dispatch.plan(None, BatteryState(soc=50), CONFIG)
    assert decision.state == DISPATCH_UNAVAILABLE
    assert decision.ems_mode == 0x0001  # EMS Auto, nu inacțiune


def test_missing_soc_falls_back_to_auto() -> None:
    decision = dispatch.plan(_series(), BatteryState(soc=None), CONFIG)
    assert decision.state == DISPATCH_UNAVAILABLE
    assert decision.ems_mode == 0x0001


# --------------------------------------------------------------------------
# Păstrarea energiei pentru vârf (hold_for_peak)
# --------------------------------------------------------------------------


def test_hold_is_opt_in() -> None:
    """Fără opțiune, o baterie plină înainte de vârf rămâne în autoconsum."""
    decision = dispatch.plan(_series(), BatteryState(soc=95), CONFIG, now=_at(16))
    assert decision.state == DISPATCH_AUTO


def test_holds_a_full_battery_for_the_peak() -> None:
    """Regresie: condiția `charge_window[1] <= index` nu se putea îndeplini.

    Motorul recalculează fereastra de încărcare de la momentul curent înainte,
    deci sfârșitul ei e mereu în viitor și ramura ieșea moartă indiferent de
    opțiune. Cazul real e SOC la țintă — nimic de încărcat — cu vârful în față.
    """
    decision = dispatch.plan(_series(), BatteryState(soc=95), CONFIG_HOLD, now=_at(16))
    assert decision.state == DISPATCH_HOLD
    assert decision.ems_mode == EMS_BATTERY_STANDBY
    assert decision.power_w == 0


def test_hold_yields_to_the_peak_itself() -> None:
    decision = dispatch.plan(_series(), BatteryState(soc=95), CONFIG_HOLD, now=_at(20))
    assert decision.state == DISPATCH_DISCHARGE


def test_hold_respects_min_soc() -> None:
    """Nu are ce păstra: sub minim bateria nu are voie să livreze nimic."""
    decision = dispatch.plan(_series(), BatteryState(soc=15), CONFIG_HOLD, now=_at(16))
    assert decision.state != DISPATCH_HOLD


def test_hold_does_not_block_charging() -> None:
    """O baterie descărcată încarcă în fereastra ieftină, nu stă pe loc."""
    decision = dispatch.plan(_series(), BatteryState(soc=20), CONFIG_HOLD, now=_at(2))
    assert decision.state == DISPATCH_CHARGE_GRID


def test_hold_needs_a_worthwhile_peak() -> None:
    """Dacă vârful nu bate prețul de acum cu marja de ciclare, nu merită."""
    flat = _series([500.0 + (i % 4) * 5 for i in range(expected_mtu_count(TODAY))])
    decision = dispatch.plan(flat, BatteryState(soc=95), CONFIG_HOLD, now=_at(16))
    assert decision.state == DISPATCH_AUTO


def test_breakeven_accounts_for_efficiency() -> None:
    lossy = dispatch.DispatchConfig(**{**CONFIG.__dict__, "round_trip_efficiency": 0.5})
    assert dispatch._breakeven(lossy) > dispatch._breakeven(CONFIG)


# --------------------------------------------------------------------------
# Ajutoare de decontare
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1.234,56", 1234.56), ("694,48", 694.48), ("-12,5", -12.5), ("abc", None)],
)
def test_parse_ro_number(text: str, expected: float | None) -> None:
    assert parse_ro_number(text) == expected


def test_monthly_settlement_uses_weighted_price() -> None:
    # Iunie 2026: 694,48 lei/MWh publicat de OPCOM
    assert monthly_settlement(420.0, 694.48) == pytest.approx(291.68, abs=0.01)


def test_lei_per_kwh() -> None:
    assert lei_per_kwh(694.48) == pytest.approx(0.69448)


# --------------------------------------------------------------------------
# Limitele BMS (registrele 10476 / 10478)
# --------------------------------------------------------------------------


def test_bms_limit_blocks_charging() -> None:
    """BMS-ul spune că nu mai încape nimic, deși SOC-ul sugerează că da."""
    battery = BatteryState(soc=20, capacity_kwh=10.0, charge_allow_kwh=0.0,
                           discharge_allow_kwh=0.5)
    decision = dispatch.plan(_series(), battery, CONFIG, now=_at(2))
    assert decision.state != DISPATCH_CHARGE_GRID


def test_bms_limit_blocks_discharging() -> None:
    battery = BatteryState(soc=80, capacity_kwh=10.0, charge_allow_kwh=2.0,
                           discharge_allow_kwh=0.0)
    decision = dispatch.plan(_series(), battery, CONFIG, now=_at(20))
    assert decision.state != DISPATCH_DISCHARGE


def test_policy_wins_when_stricter_than_bms() -> None:
    """SOC sub pragul minim al utilizatorului oprește descărcarea chiar dacă
    BMS-ul ar mai permite."""
    battery = BatteryState(soc=15, capacity_kwh=10.0, discharge_allow_kwh=5.0)
    decision = dispatch.plan(_series(), battery, CONFIG, now=_at(20))
    assert decision.state != DISPATCH_DISCHARGE


def test_bms_capacity_overrides_configured_value() -> None:
    """Capacitatea raportată de BMS are prioritate față de cea din configurare."""
    small = BatteryState(soc=50, capacity_kwh=2.0)
    large = BatteryState(soc=50, capacity_kwh=40.0)
    small_plan = dispatch.plan(_series(), small, CONFIG, now=_at(2))
    large_plan = dispatch.plan(_series(), large, CONFIG, now=_at(2))
    # O baterie mai mare are nevoie de o fereastră de încărcare mai lungă.
    span = lambda d: d.charge_window[1] - d.charge_window[0]
    assert span(large_plan) > span(small_plan)


def test_bms_values_ignored_when_both_zero() -> None:
    """Registrele nepopulate (0 și 0) nu trebuie să blocheze dispecerizarea.

    Filtrul e în readings.py; aici verificăm doar că None înseamnă „ignoră".
    """
    battery = BatteryState(soc=20, capacity_kwh=10.0,
                           charge_allow_kwh=None, discharge_allow_kwh=None)
    decision = dispatch.plan(_series(), battery, CONFIG, now=_at(2))
    assert decision.state == DISPATCH_CHARGE_GRID
