"""Constante și harta de registre pentru GoodWe EMS.

Sursa registrelor: GoodWe ARM 745 Modbus Protocol Map, revizia 28.03.2025.
Toate adresele sunt holding registers (funcție 0x03 citire / 0x06 scriere).
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "goodwe_ems"
MANUFACTURER: Final = "GoodWe"
DEFAULT_NAME: Final = "GoodWe EMS"

# --------------------------------------------------------------------------
# Comunicație
# --------------------------------------------------------------------------

CONF_CONNECTION: Final = "connection"
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_SERIAL_PORT: Final = "serial_port"
CONF_BAUDRATE: Final = "baudrate"
CONF_SLAVE: Final = "slave"
CONF_SCAN_INTERVAL: Final = "scan_interval"

CONNECTION_TCP: Final = "tcp"
CONNECTION_RTU_OVER_TCP: Final = "rtu_over_tcp"
CONNECTION_SERIAL: Final = "serial"
CONNECTION_TYPES: Final = [CONNECTION_TCP, CONNECTION_RTU_OVER_TCP, CONNECTION_SERIAL]

DEFAULT_PORT: Final = 502
DEFAULT_BAUDRATE: Final = 9600
DEFAULT_SLAVE: Final = 247  # 0xF7, adresa implicită GoodWe
DEFAULT_SCAN_INTERVAL: Final = 30  # secunde

# --------------------------------------------------------------------------
# Configurare baterie și dispecerizare
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
DEFAULT_CYCLE_COST: Final = 150.0  # lei/MWh ciclat, uzură + pierderi
DEFAULT_HOLD_FOR_PEAK: Final = False

# --------------------------------------------------------------------------
# 1. Limitare export / anti-backflow
# --------------------------------------------------------------------------

REG_FEED_POWER_ENABLE: Final = 47509  # U16 RW, 0/1
REG_FEED_POWER_PARAM: Final = 47510  # S16 RW, -30000..30000 W
REG_FEED_POWER_ENABLE_PARALLEL: Final = 42003  # U16 RW, montaj paralel / >30 kW
REG_ALLOWABLE_ONGRID_POWER: Final = 42004  # S32 RW, W
REG_ANTI_BACKFLOW: Final = 46708  # U16 RW, comutator general

FEED_POWER_MIN: Final = -30000
FEED_POWER_MAX: Final = 30000

# --------------------------------------------------------------------------
# 2. Comandă încărcare / descărcare baterie (EMS)
# --------------------------------------------------------------------------

REG_MANUFACTURER_CODE: Final = 47505  # U16 RW, trebuie 2 pentru EMS
REG_EMS_POWER_MODE: Final = 47511  # U16 RW, VOLATIL (Save = N)
REG_EMS_POWER_SET: Final = 47512  # U16 RW, 0..10000 W, VOLATIL (Save = N)

MANUFACTURER_CODE_EMS: Final = 2

EMS_AUTO: Final = 0x0001
EMS_CHARGE_PV: Final = 0x0002
EMS_DISCHARGE_PV: Final = 0x0003
EMS_IMPORT_AC: Final = 0x0004
EMS_EXPORT_AC: Final = 0x0005
EMS_BATTERY_STANDBY: Final = 0x0008
EMS_CHARGE_BAT: Final = 0x000B
EMS_DISCHARGE_BAT: Final = 0x000C
EMS_STOPPED: Final = 0x00FF

EMS_MODES: Final[dict[str, int]] = {
    "auto": EMS_AUTO,
    "charge_pv": EMS_CHARGE_PV,
    "discharge_pv": EMS_DISCHARGE_PV,
    "import_ac": EMS_IMPORT_AC,
    "export_ac": EMS_EXPORT_AC,
    "battery_standby": EMS_BATTERY_STANDBY,
    "charge_bat": EMS_CHARGE_BAT,
    "discharge_bat": EMS_DISCHARGE_BAT,
    "stopped": EMS_STOPPED,
}
EMS_MODES_REVERSE: Final[dict[int, str]] = {v: k for k, v in EMS_MODES.items()}

EMS_POWER_MIN: Final = 0
EMS_POWER_MAX: Final = 10000

# --------------------------------------------------------------------------
# 3. Încărcare din AC (rețea)
# --------------------------------------------------------------------------

REG_FAST_CHARGE_ENABLE: Final = 47545  # U16 RW, 0..3, VOLATIL
REG_FAST_CHARGE_STOP_SOC: Final = 47546  # U16 RW, 1..100 %
REG_OFFGRID_CHARGE_ENABLE: Final = 20332  # U16 RW, 0/1, doar off-grid

# --------------------------------------------------------------------------
# 4. Control încărcare / descărcare (limite și praguri)
# --------------------------------------------------------------------------

REG_CHARGE_DISCHARGE_ENABLE: Final = 45564  # U16 RW, 0/1
REG_BATTERY_CHARGE_LIMIT: Final = 45565  # U16 RW, 0..4600 W
REG_BATTERY_DISCHARGE_LIMIT: Final = 45566  # U16 RW, 0..4600 W
REG_INVERTER_AC_LIMIT: Final = 45567  # U16 RW, 0..4600 W
REG_MIN_DISCHARGE_SOC: Final = 45558  # U16 RW, 0..100 %
REG_MAX_CHARGE_SOC: Final = 45559  # U16 RW, 0..100 %
REG_DISCHARGE_DURATION: Final = 45560  # U16 RW, s
REG_DISCHARGE_POWER_DELTA: Final = 45561  # U16 RW, W
REG_CHARGE_DURATION: Final = 45562  # U16 RW, s
REG_CHARGE_POWER_DELTA: Final = 45563  # U16 RW, W
REG_START_CHARGE_SOC: Final = 47531  # U16 RW, scalare 10
REG_STOP_CHARGE_SOC: Final = 47532  # U16 RW, scalare 10
REG_CLEAR_ECONOMIC_SCHEDULE: Final = 47533  # U16 W
REG_PEAK_SHAVING_POWER: Final = 47542  # U32 RW, W
REG_PEAK_SHAVING_SOC: Final = 47544  # U16 RW, %

REG_MODBUS_ADDRESS: Final = 45127  # U16 RW, adresa slave

BATTERY_POWER_MAX: Final = 4600
SOC_SCALE: Final = 10  # pentru 47531 / 47532

# --------------------------------------------------------------------------
# Stări de dispecerizare
# --------------------------------------------------------------------------

DISPATCH_IDLE: Final = "idle"
DISPATCH_AUTO: Final = "auto"
DISPATCH_CHARGE_GRID: Final = "charge_grid"
DISPATCH_DISCHARGE: Final = "discharge"
DISPATCH_HOLD: Final = "hold"
DISPATCH_UNAVAILABLE: Final = "unavailable"

# --------------------------------------------------------------------------
# Servicii
# --------------------------------------------------------------------------

SERVICE_SET_EMS_MODE: Final = "set_ems_mode"
SERVICE_SET_EXPORT_LIMIT: Final = "set_export_limit"
SERVICE_FORCE_CHARGE: Final = "force_charge"
SERVICE_FORCE_DISCHARGE: Final = "force_discharge"
SERVICE_STOP_FORCING: Final = "stop_forcing"
SERVICE_CLEAR_SCHEDULE: Final = "clear_economic_schedule"

PLATFORMS: Final = ["sensor", "switch", "number", "select"]
