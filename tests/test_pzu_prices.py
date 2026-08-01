"""Teste pentru scraping-ul OPCOM.

OPCOM împarte tabelul ROPEX_DAM_15min pe două jumătăți de zi, fiecare pe
rândul propriului indicator, cu un rând intermediar care repetă doar
numerele de coloană ca antet secundar -- vezi comentariul din
`OpcomSource._extract_values`. Fixtura de mai jos reproduce exact acea
structură, ca să nu se strice din nou neobservat dacă cineva simplifică
euristica înapoi la „rândul cu cele mai multe prețuri".

Rulează cu:
    pytest tests/
"""

from __future__ import annotations

import sys
import types
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components"
sys.path.insert(0, str(ROOT))

_pkg = types.ModuleType("goodwe_ems")
_pkg.__path__ = [str(ROOT / "goodwe_ems")]
sys.modules.setdefault("goodwe_ems", _pkg)

from goodwe_ems.pzu_prices import OpcomSource  # noqa: E402

from bs4 import BeautifulSoup  # noqa: E402


def _cell(value) -> str:
    return f"<td>{value}</td>"


def _ro(value: float) -> str:
    """Formatează ca OPCOM: virgulă zecimală, fără separator de mii sub 1000."""
    return f"{value:.2f}".replace(".", ",")


def _make_table(day_text: str = "2/8/2026") -> str:
    first_half = [_ro(100 + i) for i in range(1, 49)]  # intervalele 1-48
    second_half = [_ro(200 + i) for i in range(1, 49)]  # intervalele 49-96

    header_row = "".join(_cell(i) for i in range(1, 49))
    base_row = "".join(_cell(c) for c in ["ROPEX_DAM_Base", _ro(632.86), *first_half])
    # Rândul mislabeled: doar agregatul are virgulă, restul sunt antete goale.
    peak_row = "".join(
        _cell(c) for c in ["ROPEX_DAM_Peak", _ro(363.57), *range(49, 97)]
    )
    offpeak_row = "".join(
        _cell(c) for c in ["ROPEX_DAM_Off_Peak", _ro(902.16), *second_half]
    )

    return f"""
    <table>
      <tr><td>ROPEX_DAM_15min</td>
          <td>Piata pentru ziua urmatoare - Ziua de livrare&nbsp;&nbsp;{day_text} [lei/MWh]</td></tr>
      <tr>{header_row}</tr>
      <tr>{base_row}</tr>
      <tr>{peak_row}</tr>
      <tr>{offpeak_row}</tr>
    </table>
    """, first_half, second_half


def test_extract_delivery_day_reads_the_short_date_format():
    html, _, _ = _make_table(day_text="2/8/2026")
    table = OpcomSource._find_table(BeautifulSoup(html, "html.parser"))
    assert OpcomSource._extract_delivery_day(table) == date(2026, 8, 2)


def test_extract_values_reassembles_the_two_split_halves_in_order():
    html, first_half, second_half = _make_table()
    table = OpcomSource._find_table(BeautifulSoup(html, "html.parser"))
    values = OpcomSource._extract_values(table)

    expected = [float(v.replace(",", ".")) for v in first_half + second_half]
    assert values == expected


def test_extract_values_ignores_the_mislabeled_header_row():
    """The 'ROPEX_DAM_Peak' row carries only its own aggregate plus bare
    column numbers -- it must contribute nothing beyond that aggregate being
    dropped, or the series would gain phantom extra values."""
    html, first_half, second_half = _make_table()
    table = OpcomSource._find_table(BeautifulSoup(html, "html.parser"))
    values = OpcomSource._extract_values(table)
    assert len(values) == 96


def test_parse_end_to_end_produces_a_valid_series():
    html, _, _ = _make_table()
    source = OpcomSource.__new__(OpcomSource)
    series = source._parse(html)

    assert series.day == date(2026, 8, 2)
    assert series.currency == "RON"
    assert series.source == "opcom"
    assert len(series.values) == 96
