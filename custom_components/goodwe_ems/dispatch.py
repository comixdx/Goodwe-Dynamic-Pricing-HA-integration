"""Dispecerizare pe preț: traduce seria PZU în comenzi EMS.

Motorul e deliberat fără stare persistentă. La fiecare ciclu recalculează
decizia de la zero din (serie, SOC, configurație) și o rescrie pe invertor.
Asta rezolvă simultan două probleme: registrele 47511/47512 sunt volatile și se
pierd la reboot, iar o decizie recalculată nu poate rămâne blocată într-o stare
învechită dacă un ciclu eșuează.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime

from .const import (
    DISPATCH_AUTO,
    DISPATCH_CHARGE_GRID,
    DISPATCH_DISCHARGE,
    DISPATCH_HOLD,
    DISPATCH_UNAVAILABLE,
    EMS_AUTO,
    EMS_BATTERY_STANDBY,
    EMS_CHARGE_BAT,
    EMS_DISCHARGE_BAT,
)
from .pzu_prices import (
    MTU_PER_HOUR,
    PriceSeries,
    cheapest_window,
    most_expensive_window,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchConfig:
    """Parametrii fizici și economici ai bateriei."""

    capacity_kwh: float
    max_charge_power_w: int
    max_discharge_power_w: int
    min_soc: int
    target_soc: int
    round_trip_efficiency: float
    cycle_cost_lei_mwh: float
    hold_for_peak: bool = False

    @property
    def charge_power_kw(self) -> float:
        return max(self.max_charge_power_w, 1) / 1000.0

    @property
    def discharge_power_kw(self) -> float:
        return max(self.max_discharge_power_w, 1) / 1000.0


@dataclass(frozen=True)
class BatteryState:
    """Ce știm despre baterie în acest ciclu.

    `charge_allow_kwh` și `discharge_allow_kwh` vin din registrele 10476/10478,
    care raportează energia pe care BMS-ul o permite chiar acum — deja cu
    derating de temperatură și cu limitele de celulă incluse. Sunt preferabile
    calculului `capacitate × SOC`, care presupune o baterie ideală.
    """

    soc: float | None
    capacity_kwh: float | None = None
    charge_allow_kwh: float | None = None
    discharge_allow_kwh: float | None = None


@dataclass(frozen=True)
class DispatchDecision:
    """Ce trebuie scris pe invertor în acest ciclu, plus motivul."""

    state: str
    ems_mode: int
    power_w: int
    reason: str
    charge_window: tuple[int, int] | None = None
    discharge_window: tuple[int, int] | None = None
    charge_price: float | None = None
    discharge_price: float | None = None

    @property
    def is_forced(self) -> bool:
        return self.state in (DISPATCH_CHARGE_GRID, DISPATCH_DISCHARGE)


def _mtus_for_energy(kwh: float, power_kw: float) -> int:
    """Câte sferturi de oră durează transferul unei energii la o putere dată."""
    if kwh <= 0:
        return 0
    return max(1, math.ceil(kwh / power_kw * MTU_PER_HOUR))


def _breakeven(config: DispatchConfig) -> float:
    """Diferența minimă de preț sub care arbitrajul e în pierdere.

    Un kWh cumpărat la preț `p` livrează doar `p * eficiență` la descărcare, iar
    ciclul consumă din durata de viață a bateriei. Sub pragul ăsta, profitul
    teoretic e negativ chiar dacă graficul arată tentant.
    """
    return config.cycle_cost_lei_mwh / max(config.round_trip_efficiency, 0.1)


def _in_window(index: int, window: tuple[int, int] | None) -> bool:
    return window is not None and window[0] <= index < window[1]


@dataclass(frozen=True)
class _Windows:
    """O pereche de ferestre încărcare/descărcare și marja dintre ele."""

    charge_window: tuple[int, int] | None
    charge_price: float | None
    discharge_window: tuple[int, int] | None
    discharge_price: float | None

    @property
    def margin(self) -> float | None:
        if self.charge_price is None or self.discharge_price is None:
            return None
        return self.discharge_price - self.charge_price

    @property
    def score(self) -> float:
        margin = self.margin
        return margin if margin is not None else float("-inf")


def _windows(
    series: PriceSeries,
    index: int,
    charge_mtus: int,
    discharge_mtus: int,
    charge_first: bool,
) -> _Windows:
    """Caută cele două ferestre într-o ordine dată, fără suprapunere.

    Ferestrele nu se pot suprapune: nu descarci energia pe care abia urmează
    s-o cumperi, și nu cumperi în intervalul în care ai decis să vinzi.
    """
    total = len(series.values)
    cw = dw = None
    cp = dp = None

    if charge_first:
        if charge_mtus > 0 and index + charge_mtus <= total:
            start, cp = cheapest_window(series, charge_mtus, start_from=index)
            cw = (start, start + charge_mtus)
        after = cw[1] if cw else index
        if discharge_mtus > 0 and after + discharge_mtus <= total:
            start, dp = most_expensive_window(series, discharge_mtus, start_from=after)
            dw = (start, start + discharge_mtus)
    else:
        if discharge_mtus > 0 and index + discharge_mtus <= total:
            start, dp = most_expensive_window(series, discharge_mtus, start_from=index)
            dw = (start, start + discharge_mtus)
        after = dw[1] if dw else index
        if charge_mtus > 0 and after + charge_mtus <= total:
            start, cp = cheapest_window(series, charge_mtus, start_from=after)
            cw = (start, start + charge_mtus)

    return _Windows(cw, cp, dw, dp)


def plan(
    series: PriceSeries | None,
    battery: BatteryState,
    config: DispatchConfig,
    now: datetime | None = None,
) -> DispatchDecision:
    """Calculează decizia pentru momentul curent.

    Repliere sigură: orice lipsă de date sau spread insuficient înseamnă
    autoconsum (EMS Auto), nu inacțiune. Invertorul rămâne mereu într-o stare
    explicită cunoscută.
    """
    if series is None:
        return DispatchDecision(
            DISPATCH_UNAVAILABLE, EMS_AUTO, 0, "Prețuri PZU indisponibile sau învechite"
        )
    soc = battery.soc
    if soc is None:
        return DispatchDecision(
            DISPATCH_UNAVAILABLE, EMS_AUTO, 0, "SOC-ul bateriei nu este disponibil"
        )

    index = series.index_at(now)
    remaining = len(series.values) - index
    if remaining < 2:
        return DispatchDecision(DISPATCH_AUTO, EMS_AUTO, 0, "Sfârșit de zi de livrare")

    breakeven = _breakeven(config)
    capacity = battery.capacity_kwh or config.capacity_kwh

    # Politica utilizatorului și limita instantanee a BMS-ului sunt două
    # constrângeri diferite; se aplică cea mai strânsă.
    headroom_kwh = max(0.0, (config.target_soc - soc) / 100.0 * capacity)
    if battery.charge_allow_kwh is not None:
        headroom_kwh = min(headroom_kwh, battery.charge_allow_kwh)

    usable_kwh = max(0.0, (soc - config.min_soc) / 100.0 * capacity)
    if battery.discharge_allow_kwh is not None:
        usable_kwh = min(usable_kwh, battery.discharge_allow_kwh)

    charge_mtus = min(_mtus_for_energy(headroom_kwh, config.charge_power_kw), remaining)
    discharge_mtus = min(
        _mtus_for_energy(usable_kwh, config.discharge_power_kw), remaining
    )

    # Cele două ordini posibile pentru restul zilei: cumperi apoi vinzi, sau
    # vinzi ce ai deja apoi reîncarci. Se evaluează amândouă și câștigă marja
    # mai mare. Fără asta, un ciclu care începe în plin vârf de preț ar căuta
    # întâi o fereastră de încărcare și ar rata vârful.
    best = max(
        (
            _windows(series, index, charge_mtus, discharge_mtus, charge_first=True),
            _windows(series, index, charge_mtus, discharge_mtus, charge_first=False),
        ),
        key=lambda p: p.score,
    )
    charge_window = best.charge_window
    charge_price = best.charge_price
    discharge_window = best.discharge_window
    discharge_price = best.discharge_price
    margin = best.margin

    # --- decizia ----------------------------------------------------------
    if _in_window(index, charge_window):
        if soc >= config.target_soc:
            return _auto(f"SOC {soc:.0f}% a atins ținta de {config.target_soc}%",
                         charge_window, discharge_window, charge_price, discharge_price)
        if margin is None:
            return _auto("Nu există fereastră de descărcare pentru arbitraj",
                         charge_window, discharge_window, charge_price, discharge_price)
        if margin < breakeven:
            return _auto(
                f"Marjă {margin:.0f} < prag {breakeven:.0f} lei/MWh",
                charge_window, discharge_window, charge_price, discharge_price,
            )
        return DispatchDecision(
            DISPATCH_CHARGE_GRID,
            EMS_CHARGE_BAT,
            config.max_charge_power_w,
            f"Fereastră ieftină ({charge_price:.0f} lei/MWh), marjă {margin:.0f}",
            charge_window, discharge_window, charge_price, discharge_price,
        )

    if _in_window(index, discharge_window):
        if soc <= config.min_soc:
            return _auto(f"SOC {soc:.0f}% la limita minimă de {config.min_soc}%",
                         charge_window, discharge_window, charge_price, discharge_price)
        if margin is not None and margin < breakeven:
            return _auto(
                f"Marjă {margin:.0f} < prag {breakeven:.0f} lei/MWh",
                charge_window, discharge_window, charge_price, discharge_price,
            )
        return DispatchDecision(
            DISPATCH_DISCHARGE,
            EMS_DISCHARGE_BAT,
            config.max_discharge_power_w,
            f"Fereastră scumpă ({discharge_price:.0f} lei/MWh)",
            charge_window, discharge_window, charge_price, discharge_price,
        )

    # Între încărcare și vârf: opțional, bateria stă pe loc ca să nu consume
    # în autoconsum energia cumpărată ieftin pentru vârf.
    #
    # Condiția nu poate fi „am trecut de fereastra de încărcare": motorul e
    # fără stare și recalculează fereastra de la `index` înainte, deci
    # `charge_window[1] > index` întotdeauna, iar ramura ieșea moartă. Situația
    # reală e că nu mai are ce încărca — SOC la țintă înseamnă zero headroom,
    # deci `charge_window is None` — și vârful e încă în față. Ramurile de mai
    # sus au returnat deja dacă suntem într-una dintre ferestre.
    #
    # Amânarea merită doar dacă vârful bate prețul de acum cu marja care
    # acoperă ciclarea; altfel bateria ar sta degeaba în timp ce casa trage din
    # rețea la un preț la fel de mare.
    if (
        config.hold_for_peak
        and discharge_window is not None
        and index < discharge_window[0]
        and soc > config.min_soc
        and discharge_price is not None
        and discharge_price - series.values[index] >= breakeven
    ):
        return DispatchDecision(
            DISPATCH_HOLD,
            EMS_BATTERY_STANDBY,
            0,
            f"Păstrez energia pentru vârful de {discharge_price:.0f} lei/MWh",
            charge_window, discharge_window, charge_price, discharge_price,
        )

    return _auto("În afara ferestrelor de arbitraj",
                 charge_window, discharge_window, charge_price, discharge_price)


def _auto(
    reason: str,
    charge_window: tuple[int, int] | None = None,
    discharge_window: tuple[int, int] | None = None,
    charge_price: float | None = None,
    discharge_price: float | None = None,
) -> DispatchDecision:
    return DispatchDecision(
        DISPATCH_AUTO, EMS_AUTO, 0, reason,
        charge_window, discharge_window, charge_price, discharge_price,
    )


def window_label(series: PriceSeries, window: tuple[int, int] | None) -> str | None:
    """„02:15 - 05:30", pentru atributele senzorului de dispecerizare."""
    if window is None:
        return None

    def fmt(idx: int) -> str:
        return series.local_time_of(min(idx, len(series.values))).strftime("%H:%M")

    return f"{fmt(window[0])} - {fmt(window[1])}"
