"""Update coordinator: one cycle that reads, decides and writes.

`data` is the library's runtime dictionary, exactly as the core GoodWe
integration exposes it, so sensor entities can be generated from
`inverter.sensors()` without a translation layer in between. The dispatch state
lives in attributes alongside it rather than inside `data`, because it is not
inverter telemetry and does not share its update semantics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from goodwe import Inverter, InverterError, RequestFailedException
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_CYCLE_COST,
    CONF_ENABLE_DISPATCH,
    CONF_ENTSOE_TOKEN,
    CONF_HOLD_FOR_PEAK,
    CONF_MAX_CHARGE_POWER,
    CONF_MAX_DISCHARGE_POWER,
    CONF_MIN_SOC,
    CONF_ROUND_TRIP_EFFICIENCY,
    CONF_SCAN_INTERVAL,
    CONF_SOC_ENTITY,
    CONF_TARGET_SOC,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_CYCLE_COST,
    DEFAULT_HOLD_FOR_PEAK,
    DEFAULT_MAX_CHARGE_POWER,
    DEFAULT_MAX_DISCHARGE_POWER,
    DEFAULT_MIN_SOC,
    DEFAULT_ROUND_TRIP_EFFICIENCY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TARGET_SOC,
    DISPATCH_IDLE,
    DOMAIN,
    SENSOR_BATTERY_SOC,
)
from .dispatch import BatteryState, DispatchConfig, DispatchDecision, plan
from .ems import BatteryLimits, GoodweEms
from .pzu_prices import RO_TZ, PriceError, PriceSeries, PzuPriceCoordinator

_LOGGER = logging.getLogger(__name__)

PRICE_RETRY_INTERVAL = timedelta(minutes=20)

# A plain alias rather than a `type` statement: core can rely on Python 3.13,
# but a custom component still gets loaded on 3.11 installations.
GoodweEmsConfigEntry = ConfigEntry["GoodweEmsRuntimeData"]


@dataclass
class GoodweEmsRuntimeData:
    """Everything the platforms need, hung off the config entry."""

    inverter: Inverter
    ems: GoodweEms
    coordinator: GoodweEmsCoordinator
    device_info: DeviceInfo


class GoodweEmsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Read the inverter, refresh prices, apply the decision."""

    config_entry: GoodweEmsConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GoodweEmsConfigEntry,
        ems: GoodweEms,
    ) -> None:
        """Initialize the update coordinator."""
        options = {**entry.data, **entry.options}
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=int(options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
            ),
        )
        self.ems = ems
        self.inverter = ems.inverter
        self._options = options
        self._last_data: dict[str, Any] = {}

        self._prices = PzuPriceCoordinator(
            async_get_clientsession(hass), options.get(CONF_ENTSOE_TOKEN) or None
        )
        self._soc_entity: str | None = options.get(CONF_SOC_ENTITY) or None
        self._dispatch_enabled: bool = bool(options.get(CONF_ENABLE_DISPATCH, False))
        self._limits = BatteryLimits()
        self._last_price_attempt: datetime | None = None
        self._last_monthly_day: date | None = None
        self._last_written: tuple[int, int] | None = None

        self.decision: DispatchDecision | None = None

    # -- inverter data, core-compatible accessors ---------------------------

    def sensor_value(self, sensor: str) -> Any:
        """Current, or last known, value of a runtime sensor."""
        value = self.data.get(sensor)
        return value if value is not None else self._last_data.get(sensor)

    def total_sensor_value(self, sensor: str) -> Any:
        """Current value of a 'total' sensor, which is never legitimately 0."""
        return self.data.get(sensor) or self._last_data.get(sensor)

    def reset_sensor(self, sensor: str) -> None:
        """Reset a daily cumulative sensor to 0 at midnight."""
        self._last_data[sensor] = 0
        self.data[sensor] = 0

    # -- price and dispatch state -------------------------------------------

    @property
    def series(self) -> PriceSeries | None:
        return self._prices.series

    @property
    def monthly_weighted(self) -> tuple[float, str] | None:
        return self._prices.monthly_weighted

    @property
    def dispatch_config(self) -> DispatchConfig:
        o = self._options
        return DispatchConfig(
            capacity_kwh=float(o.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)),
            max_charge_power_w=int(
                o.get(CONF_MAX_CHARGE_POWER, DEFAULT_MAX_CHARGE_POWER)
            ),
            max_discharge_power_w=int(
                o.get(CONF_MAX_DISCHARGE_POWER, DEFAULT_MAX_DISCHARGE_POWER)
            ),
            min_soc=int(o.get(CONF_MIN_SOC, DEFAULT_MIN_SOC)),
            target_soc=int(o.get(CONF_TARGET_SOC, DEFAULT_TARGET_SOC)),
            round_trip_efficiency=float(
                o.get(CONF_ROUND_TRIP_EFFICIENCY, DEFAULT_ROUND_TRIP_EFFICIENCY)
            ),
            cycle_cost_lei_mwh=float(o.get(CONF_CYCLE_COST, DEFAULT_CYCLE_COST)),
            hold_for_peak=bool(o.get(CONF_HOLD_FOR_PEAK, DEFAULT_HOLD_FOR_PEAK)),
        )

    @property
    def dispatch_enabled(self) -> bool:
        return self._dispatch_enabled

    def restore_dispatch_enabled(self, enabled: bool) -> bool:
        """Resume the switch state after a restart. True if it changed.

        The config option only seeds the switch; its runtime state is the source
        of truth afterwards, otherwise restarting Home Assistant would stop
        dispatch without anyone asking for it.

        Nothing is written to the inverter: the next cycle applies the decision
        anyway, and an `async_auto()` on every startup would be a pointless
        write.
        """
        if self._dispatch_enabled == enabled:
            return False
        self._dispatch_enabled = enabled
        return True

    async def async_set_dispatch_enabled(self, enabled: bool) -> None:
        """Turn price dispatch on or off. Turning it off returns to Auto."""
        self._dispatch_enabled = enabled
        if not enabled:
            try:
                await self.ems.async_auto()
            except (InverterError, ValueError) as err:
                _LOGGER.error("Returning to Auto mode failed: %s", err)
            self._last_written = None
        await self.async_request_refresh()

    # -- battery state -------------------------------------------------------

    def current_soc(self) -> float | None:
        """Battery state of charge, from the inverter or the fallback entity.

        The inverter is preferred because it is read in the same cycle as the
        rest of the decision. The external entity stays as a fallback for
        installations whose BMS block does not answer.
        """
        soc = self.data.get(SENSOR_BATTERY_SOC) if self.data else None
        if soc is not None:
            return float(soc)
        return self._soc_from_entity()

    def _soc_from_entity(self) -> float | None:
        if not self._soc_entity:
            return None
        state = self.hass.states.get(self._soc_entity)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _battery_state(self, runtime: dict[str, Any]) -> BatteryState:
        soc = runtime.get(SENSOR_BATTERY_SOC)
        return BatteryState(
            soc=float(soc) if soc is not None else self._soc_from_entity(),
            capacity_kwh=self._limits.rated_capacity_kwh,
            charge_allow_kwh=self._limits.charge_allow_kwh,
            discharge_allow_kwh=self._limits.discharge_allow_kwh,
        )

    # -- the main cycle ------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        self._last_data = self.data or {}
        try:
            runtime = await self.inverter.read_runtime_data()
        except RequestFailedException as ex:
            # UDP to the inverter is unreliable by definition. Isolated misses
            # are normal, so availability is only questioned after three
            # consecutive failures.
            if ex.consecutive_failures_count < 3:
                _LOGGER.debug(
                    "No response received (streak of %d)", ex.consecutive_failures_count
                )
                return self._last_data
            raise UpdateFailed(ex) from ex
        except InverterError as ex:
            raise UpdateFailed(ex) from ex

        self._limits = await self.ems.async_read_battery_limits()

        await self._async_maybe_refresh_prices()

        decision = plan(
            self._prices.actionable_series(),
            self._battery_state(runtime),
            self.dispatch_config,
        )

        if self._dispatch_enabled:
            await self._async_apply(decision)
        else:
            decision = DispatchDecision(
                DISPATCH_IDLE,
                decision.ems_mode,
                0,
                "Price dispatch is switched off",
                decision.charge_window,
                decision.discharge_window,
                decision.charge_price,
                decision.discharge_price,
            )

        self.decision = decision
        return runtime

    async def _async_maybe_refresh_prices(self) -> None:
        now = datetime.now(RO_TZ)
        series = self._prices.series
        # This condition has to match `is_actionable`. Checking only the day
        # here meant a series fetched at 00:05 stopped being actionable around
        # 18:05 but still counted as "fresh" for the guard below, so it was
        # never retried: dispatch died silently right across the evening peak,
        # every day, on any instance that does not get restarted.
        fresh = series is not None and series.is_actionable(now)

        should_retry = self._last_price_attempt is None or (
            now - self._last_price_attempt > PRICE_RETRY_INTERVAL
        )
        if not fresh and should_retry:
            self._last_price_attempt = now
            try:
                await self._prices.async_refresh()
            except (PriceError, Exception) as err:  # noqa: BLE001 - any network error
                _LOGGER.warning("Refreshing PZU prices failed: %s", err)

        if self._last_monthly_day != now.date():
            self._last_monthly_day = now.date()
            try:
                await self._prices.async_refresh_monthly()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Monthly weighted price unavailable: %s", err)

    async def _async_apply(self, decision: DispatchDecision) -> None:
        """Rewrite the EMS command every cycle.

        Not redundant: 47511 and 47512 are volatile and an inverter reboot
        silently empties them. `async_set_ems` reads back, so a disagreement
        shows up in the log instead of passing unnoticed.
        """
        target = (int(decision.ems_mode), decision.power_w)
        try:
            await self.ems.async_set_ems(decision.ems_mode, decision.power_w)
        except (InverterError, ValueError) as err:
            _LOGGER.error("Applying decision %s failed: %s", decision.state, err)
            return

        if target != self._last_written:
            _LOGGER.info(
                "Dispatch -> %s (%s @ %s W): %s",
                decision.state,
                decision.ems_mode.name,
                decision.power_w,
                decision.reason,
            )
            self._last_written = target

    async def async_shutdown_inverter(self) -> None:
        """On unload, do not leave the inverter on a forced command."""
        if self._dispatch_enabled:
            try:
                await self.ems.async_auto()
            except (InverterError, ValueError) as err:
                _LOGGER.debug("Returning to Auto on shutdown failed: %s", err)
