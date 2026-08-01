"""Constants for the GoodWe EMS integration.

The Modbus register map that used to live here is gone: the `goodwe` library
owns the protocol now and addresses its registers by name. What remains are the
few registers the library does not name, which are reached through its
`modbus<address>` pseudo-settings escape hatch (see `ems.py`), plus the
configuration and dispatch constants that are ours alone.

Register source: GoodWe ARM 745 Modbus Protocol Map, revision 28.03.2025.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "goodwe_ems"
MANUFACTURER: Final = "GoodWe"
DEFAULT_NAME: Final = "GoodWe EMS"

# Plain strings rather than the `Platform` enum on purpose: this module stays
# importable without Home Assistant, which is what lets the dispatch and EMS
# logic be unit-tested on its own. `async_forward_entry_setups` accepts either.
PLATFORMS: Final = ["button", "number", "select", "sensor", "switch"]

# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------

# Stored on the config entry so a reload reconnects to the same protocol family
# instead of paying for discovery again. Same key name as the core integration.
CONF_MODEL_FAMILY: Final = "model_family"

CONF_SCAN_INTERVAL: Final = "scan_interval"
DEFAULT_SCAN_INTERVAL: Final = 30  # seconds
MIN_SCAN_INTERVAL: Final = timedelta(seconds=10)

# --------------------------------------------------------------------------
# Battery and dispatch configuration
# --------------------------------------------------------------------------

CONF_BATTERY_CAPACITY: Final = "battery_capacity_kwh"
CONF_MAX_CHARGE_POWER: Final = "max_charge_power_w"
CONF_MAX_DISCHARGE_POWER: Final = "max_discharge_power_w"
CONF_MIN_SOC: Final = "min_soc"
CONF_TARGET_SOC: Final = "target_soc"
CONF_ROUND_TRIP_EFFICIENCY: Final = "round_trip_efficiency"
CONF_CYCLE_COST: Final = "cycle_cost_lei_mwh"
CONF_SOC_ENTITY: Final = "soc_entity"
CONF_ENTSOE_TOKEN: Final = "entsoe_token"
CONF_ENABLE_DISPATCH: Final = "enable_dispatch"
CONF_HOLD_FOR_PEAK: Final = "hold_for_peak"

DEFAULT_BATTERY_CAPACITY: Final = 10.0
DEFAULT_MAX_CHARGE_POWER: Final = 4600
DEFAULT_MAX_DISCHARGE_POWER: Final = 4600
DEFAULT_MIN_SOC: Final = 15
DEFAULT_TARGET_SOC: Final = 95
DEFAULT_ROUND_TRIP_EFFICIENCY: Final = 0.90
DEFAULT_CYCLE_COST: Final = 150.0  # lei/MWh cycled: wear plus losses
DEFAULT_HOLD_FOR_PEAK: Final = False

# --------------------------------------------------------------------------
# Registers the library does not name
#
# Passed to `read_setting` / `write_setting` as "modbus<address>", which the
# library forwards to a raw single-register read or write. Anything the library
# *does* name (ems_mode, ems_power_limit, grid_export, fast_charging, ...) is
# addressed by its library id instead and is deliberately absent here.
# --------------------------------------------------------------------------

# EMS arming. Without manufacturer code 2 in 47505 the inverter silently
# ignores every write to the EMS mode register.
REG_MANUFACTURER_CODE: Final = 47505
MANUFACTURER_CODE_EMS: Final = 2

# Export limit parameter. The library names 47510 as `grid_export_limit`, but
# types it unsigned, so a negative value (forced import) cannot be written
# through the named setting.
REG_FEED_POWER_PARAM: Final = 47510
REG_ANTI_BACKFLOW: Final = 46708

REG_MIN_DISCHARGE_SOC: Final = 45558
REG_MAX_CHARGE_SOC: Final = 45559
REG_CHARGE_DISCHARGE_ENABLE: Final = 45564
REG_BATTERY_CHARGE_LIMIT: Final = 45565
REG_BATTERY_DISCHARGE_LIMIT: Final = 45566
REG_INVERTER_AC_LIMIT: Final = 45567

REG_START_CHARGE_SOC: Final = 47531  # scaled by 10
REG_STOP_CHARGE_SOC: Final = 47532  # scaled by 10
REG_CLEAR_ECONOMIC_SCHEDULE: Final = 47533

# BMS block. Two 16-bit halves of a 32-bit Wh value each, because the
# pseudo-setting reads exactly one register at a time.
REG_CHARGE_ALLOW_WH: Final = 10476
REG_DISCHARGE_ALLOW_WH: Final = 10478
REG_BMS_RATED_CAPACITY: Final = 37076  # 0.1 kWh steps

SOC_SCALE: Final = 10  # for 47531 / 47532

FEED_POWER_MIN: Final = -30000
FEED_POWER_MAX: Final = 30000
EMS_POWER_MAX: Final = 10000
BATTERY_POWER_MAX: Final = 4600

# --------------------------------------------------------------------------
# Runtime sensor ids read from the library
# --------------------------------------------------------------------------

SENSOR_BATTERY_SOC: Final = "battery_soc"

# --------------------------------------------------------------------------
# Dispatch states
# --------------------------------------------------------------------------

DISPATCH_IDLE: Final = "idle"
DISPATCH_AUTO: Final = "auto"
DISPATCH_CHARGE_GRID: Final = "charge_grid"
DISPATCH_DISCHARGE: Final = "discharge"
DISPATCH_HOLD: Final = "hold"
DISPATCH_UNAVAILABLE: Final = "unavailable"

# --------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------

SERVICE_SET_EMS_MODE: Final = "set_ems_mode"
SERVICE_SET_EXPORT_LIMIT: Final = "set_export_limit"
SERVICE_FORCE_CHARGE: Final = "force_charge"
SERVICE_FORCE_DISCHARGE: Final = "force_discharge"
SERVICE_STOP_FORCING: Final = "stop_forcing"
SERVICE_CLEAR_SCHEDULE: Final = "clear_economic_schedule"
