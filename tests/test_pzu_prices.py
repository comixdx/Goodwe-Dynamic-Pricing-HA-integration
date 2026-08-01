"""Teste pentru exportul CSV al raportului PIP de pe OPCOM.

Fixtura de mai jos reproduce exact structura reală a exportului -- titlu cu
ziua de livrare, bloc de agregate Base/Peak/Off_Peak, apoi antetul cu coloana
`Interval` urmat de un rând per interval -- ca să nu se strice din nou
neobservat dacă cineva simplifică parsarea înapoi la presupuneri despre
poziția coloanelor.

Rulează cu:
    pytest tests/
"""

from __future__ import annotations

import csv
import sys
import types
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components"
sys.path.insert(0, str(ROOT))

_pkg = types.ModuleType("goodwe_ems")
_pkg.__path__ = [str(ROOT / "goodwe_ems")]
sys.modules.setdefault("goodwe_ems", _pkg)

from goodwe_ems.pzu_prices import OpcomSource, PriceError  # noqa: E402


def _make_csv(day_text: str = "01/08/2026", n: int = 96) -> tuple[str, list[float]]:
    prices = [round(100 + i * 1.5, 2) for i in range(1, n + 1)]

    lines = [
        f'"PIP si volum tranzactionat pentru ziua de livrare: {day_text}"',
        "",
        '"","Pret mediu [lei/MWh]","Volum [MWh]","Rezolutie"',
        '"ROPEX_DAM_Base (1-24)","664.07","39232.6","PT15M"',
        '"ROPEX_DAM_Peak (33-80)","451.93","21468.9","PT15M"',
        '"ROPEX_DAM_Off_Peak (1-32) & (81-96)","876.21","17763.7","PT15M"',
        "",
        '"Zona de tranzactionare","Interval","Pret de Inchidere a Pietei [lei/MWh]",'
        '"Volum Tranzactionat [MW]","Volum Tranzactionat pe cumparare [MW]",'
        '"Volum Tranzactionat pe vanzare [MW]","Rezolutie"',
    ]
    lines += [
        f'"Romania","{i}","{p}","1000.0","1000.0","1000.0","PT15M"'
        for i, p in enumerate(prices, start=1)
    ]
    return "\r\n".join(lines), prices


def test_extract_delivery_day_reads_the_title_line():
    text, _ = _make_csv(day_text="2/8/2026")
    rows = list(csv.reader(text.splitlines()))
    assert OpcomSource._extract_delivery_day(rows[0][0]) == date(2026, 8, 2)


def test_extract_values_reads_the_price_column_in_interval_order():
    text, prices = _make_csv()
    rows = list(csv.reader(text.splitlines()))
    values = OpcomSource._extract_values(rows)
    assert values == prices


def test_parse_end_to_end_produces_a_valid_series():
    text, _ = _make_csv(day_text="01/08/2026")
    source = OpcomSource.__new__(OpcomSource)
    series = source._parse(text, date(2026, 8, 1))

    assert series.day == date(2026, 8, 1)
    assert series.currency == "RON"
    assert series.source == "opcom"
    assert len(series.values) == 96


def test_parse_rejects_a_day_mismatch_between_request_and_response():
    """OPCOM should never disagree with the day we asked for, but if it does
    (a caching bug on their end, a stale response) using the data anyway
    would silently misdate the whole series."""
    text, _ = _make_csv(day_text="02/08/2026")
    source = OpcomSource.__new__(OpcomSource)
    with pytest.raises(PriceError):
        source._parse(text, date(2026, 8, 1))


def test_parse_rejects_an_empty_response():
    """A date that has not been auctioned yet returns an empty body."""
    source = OpcomSource.__new__(OpcomSource)
    with pytest.raises(PriceError):
        source._parse("", date(2026, 8, 1))
