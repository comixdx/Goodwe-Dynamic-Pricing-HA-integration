"""Price dispatch: translate the PZU series into EMS commands.

The engine is deliberately stateless. Every cycle it recomputes the decision
from scratch out of (series, SoC, config) and rewrites it to the inverter. That
solves two problems at once: registers 47511/47512 are volatile and are lost on
reboot, and a recomputed decision cannot get stuck in a stale state when one
cycle fails.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime

from goodwe import EMSMode

from .const import (
    DISPATCH_AUTO,
    DISPATCH_CHARGE_GRID,
    DISPATCH_DISCHARGE,
    DISPATCH_HOLD,
    DISPATCH_UNAVAILABLE,
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
    """Physical and economic parameters of the battery."""

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
    """What we know about the battery this cycle.

    `charge_allow_kwh` and `discharge_allow_kwh` come from the BMS block, which
    reports the energy the battery will accept or give right now, temperature
    derating and cell limits already included. They are preferable to
    `capacity x SoC`, which assumes an ideal battery.
    """

    soc: float | None
    capacity_kwh: float | None = None
    charge_allow_kwh: float | None = None
    discharge_allow_kwh: float | None = None


@dataclass(frozen=True)
class DispatchDecision:
    """What to write to the inverter this cycle, and why."""

    state: str
    ems_mode: EMSMode
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
    """How many quarter-hours moving this much energy takes at this power."""
    if kwh <= 0:
        return 0
    return max(1, math.ceil(kwh / power_kw * MTU_PER_HOUR))


def _breakeven(config: DispatchConfig) -> float:
    """Smallest price spread that still leaves arbitrage profitable.

    A kWh bought at price `p` only delivers `p * efficiency` on discharge, and
    the cycle spends part of the battery's life. Below this threshold the
    theoretical profit is negative however tempting the curve looks.
    """
    return config.cycle_cost_lei_mwh / max(config.round_trip_efficiency, 0.1)


def _in_window(index: int, window: tuple[int, int] | None) -> bool:
    return window is not None and window[0] <= index < window[1]


@dataclass(frozen=True)
class _Windows:
    """A charge/discharge window pair and the margin between them."""

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
    """Find both windows in a given order, without overlap.

    The windows cannot overlap: you do not discharge the energy you are about to
    buy, and you do not buy during the interval you decided to sell in.
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
    """Compute the decision for the current moment.

    Safe fallback: any missing data or insufficient spread means
    self-consumption (EMS Auto), not inaction. The inverter is always left in an
    explicit, known state.
    """
    if series is None:
        return DispatchDecision(
            DISPATCH_UNAVAILABLE,
            EMSMode.AUTO,
            0,
            "PZU prices unavailable or stale",
        )
    soc = battery.soc
    if soc is None:
        return DispatchDecision(
            DISPATCH_UNAVAILABLE,
            EMSMode.AUTO,
            0,
            "Battery state of charge is unavailable",
        )

    index = series.index_at(now)
    remaining = len(series.values) - index
    if remaining < 2:
        return DispatchDecision(
            DISPATCH_AUTO, EMSMode.AUTO, 0, "End of the delivery day"
        )

    breakeven = _breakeven(config)
    capacity = battery.capacity_kwh or config.capacity_kwh

    # The user's policy and the BMS's instantaneous limit are two different
    # constraints; the tighter one wins.
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

    # Two possible orderings for the rest of the day: buy then sell, or sell
    # what you already have then recharge. Both are evaluated and the larger
    # margin wins. Without this, a cycle starting in the middle of a price peak
    # would look for a charge window first and miss the peak.
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

    # --- the decision -----------------------------------------------------
    if _in_window(index, charge_window):
        if soc >= config.target_soc:
            return _auto(
                f"SoC {soc:.0f}% reached the {config.target_soc}% target",
                charge_window, discharge_window, charge_price, discharge_price,
            )
        if margin is None:
            return _auto(
                "No discharge window to arbitrage against",
                charge_window, discharge_window, charge_price, discharge_price,
            )
        if margin < breakeven:
            return _auto(
                f"Margin {margin:.0f} < threshold {breakeven:.0f} lei/MWh",
                charge_window, discharge_window, charge_price, discharge_price,
            )
        return DispatchDecision(
            DISPATCH_CHARGE_GRID,
            EMSMode.CHARGE_BATTERY,
            config.max_charge_power_w,
            f"Cheap window ({charge_price:.0f} lei/MWh), margin {margin:.0f}",
            charge_window, discharge_window, charge_price, discharge_price,
        )

    if _in_window(index, discharge_window):
        if soc <= config.min_soc:
            return _auto(
                f"SoC {soc:.0f}% is at the {config.min_soc}% floor",
                charge_window, discharge_window, charge_price, discharge_price,
            )
        if margin is not None and margin < breakeven:
            return _auto(
                f"Margin {margin:.0f} < threshold {breakeven:.0f} lei/MWh",
                charge_window, discharge_window, charge_price, discharge_price,
            )
        return DispatchDecision(
            DISPATCH_DISCHARGE,
            EMSMode.DISCHARGE_BATTERY,
            config.max_discharge_power_w,
            f"Expensive window ({discharge_price:.0f} lei/MWh)",
            charge_window, discharge_window, charge_price, discharge_price,
        )

    # Between charging and the peak: optionally hold the battery still so
    # self-consumption does not eat the energy bought cheaply for the peak.
    #
    # The condition cannot be "we are past the charge window": the engine is
    # stateless and recomputes the window from `index` forward, so
    # `charge_window[1] > index` always held and the branch was dead. The real
    # situation is that there is nothing left to charge -- SoC at target means
    # zero headroom, hence `charge_window is None` -- while the peak is still
    # ahead. The branches above have already returned if we are inside a window.
    #
    # Waiting is only worth it if the peak beats the current price by the margin
    # that covers cycling; otherwise the battery sits idle while the house draws
    # from the grid at an equally high price.
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
            EMSMode.BATTERY_STANDBY,
            0,
            f"Holding energy for the {discharge_price:.0f} lei/MWh peak",
            charge_window, discharge_window, charge_price, discharge_price,
        )

    return _auto(
        "Outside the arbitrage windows",
        charge_window, discharge_window, charge_price, discharge_price,
    )


def _auto(
    reason: str,
    charge_window: tuple[int, int] | None = None,
    discharge_window: tuple[int, int] | None = None,
    charge_price: float | None = None,
    discharge_price: float | None = None,
) -> DispatchDecision:
    return DispatchDecision(
        DISPATCH_AUTO, EMSMode.AUTO, 0, reason,
        charge_window, discharge_window, charge_price, discharge_price,
    )


def window_label(series: PriceSeries, window: tuple[int, int] | None) -> str | None:
    """"02:15 - 05:30", for the dispatch sensor's attributes."""
    if window is None:
        return None

    def fmt(idx: int) -> str:
        return series.local_time_of(min(idx, len(series.values))).strftime("%H:%M")

    return f"{fmt(window[0])} - {fmt(window[1])}"
