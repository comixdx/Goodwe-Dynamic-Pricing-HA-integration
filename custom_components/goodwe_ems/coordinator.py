"""Coordinatorul integrării: un singur ciclu care citește, decide și scrie."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_CYCLE_COST,
    CONF_ENABLE_DISPATCH,
    CONF_ENTSOE_TOKEN,
    CONF_MAX_CHARGE_POWER,
    CONF_MAX_DISCHARGE_POWER,
    CONF_MIN_SOC,
    CONF_ROUND_TRIP_EFFICIENCY,
    CONF_SCAN_INTERVAL,
    CONF_SOC_ENTITY,
    CONF_TARGET_SOC,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_CYCLE_COST,
    DEFAULT_MAX_CHARGE_POWER,
    DEFAULT_MAX_DISCHARGE_POWER,
    DEFAULT_MIN_SOC,
    DEFAULT_ROUND_TRIP_EFFICIENCY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TARGET_SOC,
    DISPATCH_IDLE,
    DOMAIN,
)
from .dispatch import BatteryState, DispatchConfig, DispatchDecision, plan
from .inverter import GoodweInverter, InverterState
from .modbus import ModbusError
from .pzu_prices import RO_TZ, PriceError, PriceSeries, PzuPriceCoordinator
from .readings import DeviceInfoData, GoodweReader, LiveData

_LOGGER = logging.getLogger(__name__)

PRICE_RETRY_INTERVAL = timedelta(minutes=20)


@dataclass
class GoodweEmsData:
    """Ce văd entitățile la fiecare actualizare."""

    inverter: InverterState
    live: LiveData
    series: PriceSeries | None
    decision: DispatchDecision | None
    monthly_weighted: tuple[float, str] | None
    dispatch_enabled: bool


class GoodweEmsCoordinator(DataUpdateCoordinator[GoodweEmsData]):
    """Citește invertorul, împrospătează prețurile, aplică decizia."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        inverter: GoodweInverter,
    ) -> None:
        options = {**entry.data, **entry.options}
        self.entry = entry
        self.inverter = inverter
        self.reader = GoodweReader(inverter.client)
        self.device_info_data = DeviceInfoData()
        self._options = options
        self._prices = PzuPriceCoordinator(
            async_get_clientsession(hass), options.get(CONF_ENTSOE_TOKEN) or None
        )
        self._soc_entity: str | None = options.get(CONF_SOC_ENTITY)
        self._dispatch_enabled: bool = bool(options.get(CONF_ENABLE_DISPATCH, False))
        self._last_price_attempt: datetime | None = None
        self._last_monthly_day: date | None = None
        self._last_written: tuple[int, int] | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=int(options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
            ),
        )

    # -- configurație -------------------------------------------------------

    @property
    def dispatch_config(self) -> DispatchConfig:
        o = self._options
        return DispatchConfig(
            capacity_kwh=float(o.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)),
            max_charge_power_w=int(o.get(CONF_MAX_CHARGE_POWER, DEFAULT_MAX_CHARGE_POWER)),
            max_discharge_power_w=int(
                o.get(CONF_MAX_DISCHARGE_POWER, DEFAULT_MAX_DISCHARGE_POWER)
            ),
            min_soc=int(o.get(CONF_MIN_SOC, DEFAULT_MIN_SOC)),
            target_soc=int(o.get(CONF_TARGET_SOC, DEFAULT_TARGET_SOC)),
            round_trip_efficiency=float(
                o.get(CONF_ROUND_TRIP_EFFICIENCY, DEFAULT_ROUND_TRIP_EFFICIENCY)
            ),
            cycle_cost_lei_mwh=float(o.get(CONF_CYCLE_COST, DEFAULT_CYCLE_COST)),
        )

    @property
    def dispatch_enabled(self) -> bool:
        return self._dispatch_enabled

    async def async_set_dispatch_enabled(self, enabled: bool) -> None:
        """Comutatorul de dispecerizare. La oprire, invertorul revine în Auto."""
        self._dispatch_enabled = enabled
        if not enabled:
            try:
                await self.inverter.async_auto()
            except ModbusError as err:
                _LOGGER.error("Revenirea în modul Auto a eșuat: %s", err)
            self._last_written = None
        await self.async_request_refresh()

    # -- starea bateriei ----------------------------------------------------

    def current_soc(self) -> float | None:
        """SOC-ul bateriei: întâi registrul 37007, apoi entitatea configurată.

        Registrul e sursa preferată pentru că e citit în același ciclu cu restul
        deciziei. Entitatea externă rămâne ca rezervă pentru instalațiile pe
        care blocul BMS nu răspunde.
        """
        if self.data is not None and self.data.live.soc is not None:
            return float(self.data.live.soc)
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

    def _battery_state(self, live: LiveData) -> BatteryState:
        return BatteryState(
            soc=float(live.soc) if live.soc is not None else self._soc_from_entity(),
            capacity_kwh=live.total_capacity_kwh,
            charge_allow_kwh=live.charge_allow_kwh,
            discharge_allow_kwh=live.discharge_allow_kwh,
        )

    # -- ciclul principal ---------------------------------------------------

    async def _async_update_data(self) -> GoodweEmsData:
        try:
            inverter_state = await self.inverter.async_read_state()
            live = await self.reader.async_read()
        except ModbusError as err:
            raise UpdateFailed(f"Citirea invertorului a eșuat: {err}") from err

        await self._async_maybe_refresh_prices()

        series = self._prices.actionable_series()
        decision = plan(series, self._battery_state(live), self.dispatch_config)

        if self._dispatch_enabled:
            await self._async_apply(decision)
        else:
            decision = DispatchDecision(
                DISPATCH_IDLE, decision.ems_mode, 0, "Dispecerizarea pe preț este oprită",
                decision.charge_window, decision.discharge_window,
                decision.charge_price, decision.discharge_price,
            )

        return GoodweEmsData(
            inverter=inverter_state,
            live=live,
            series=self._prices.series,
            decision=decision,
            monthly_weighted=self._prices.monthly_weighted,
            dispatch_enabled=self._dispatch_enabled,
        )

    async def _async_maybe_refresh_prices(self) -> None:
        now = datetime.now(RO_TZ)
        series = self._prices.series
        fresh = series is not None and series.day == now.date()

        should_retry = self._last_price_attempt is None or (
            now - self._last_price_attempt > PRICE_RETRY_INTERVAL
        )
        if not fresh and should_retry:
            self._last_price_attempt = now
            try:
                await self._prices.async_refresh()
            except (PriceError, Exception) as err:  # noqa: BLE001 - orice eroare de rețea
                _LOGGER.warning("Împrospătarea prețurilor PZU a eșuat: %s", err)

        if self._last_monthly_day != now.date():
            self._last_monthly_day = now.date()
            try:
                await self._prices.async_refresh_monthly()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Prețul mediu ponderat lunar indisponibil: %s", err)

    async def _async_apply(self, decision: DispatchDecision) -> None:
        """Rescrie comanda EMS la fiecare ciclu.

        Rescrierea nu e redundantă: 47511 și 47512 sunt volatile, iar un reboot
        de invertor le golește în tăcere. `async_set_ems` face readback, deci o
        discordanță apare în jurnal în loc să treacă neobservată.
        """
        target = (decision.ems_mode, decision.power_w)
        try:
            await self.inverter.async_set_ems(*target)
        except ModbusError as err:
            _LOGGER.error("Aplicarea deciziei %s a eșuat: %s", decision.state, err)
            return

        if target != self._last_written:
            _LOGGER.info(
                "Dispecerizare -> %s (%#06x @ %s W): %s",
                decision.state, decision.ems_mode, decision.power_w, decision.reason,
            )
            self._last_written = target

    async def async_shutdown_inverter(self) -> None:
        """La descărcarea integrării, invertorul nu rămâne pe o comandă forțată."""
        if self._dispatch_enabled:
            try:
                await self.inverter.async_auto()
            except ModbusError as err:
                _LOGGER.debug("Revenirea în Auto la oprire a eșuat: %s", err)
