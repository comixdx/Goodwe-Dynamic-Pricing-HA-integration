"""EMS control layer on top of the `goodwe` library.

The library owns the protocol: framing, retries, family detection and the named
register map all live there. This module adds the two things it does not do.

First, arming. Writes to the EMS mode register are ignored unless manufacturer
code 2 sits in 47505, and the library has no notion of that; every EMS command
here goes through `async_arm` first.

Second, verified writes. 47511 and 47512 are volatile (Save = N in the protocol
map) and are silently lost when the inverter reboots. A write that did not take
is indistinguishable from one that did unless it is read back, so the EMS writes
read back and log a mismatch instead of letting the battery quietly sit idle.

Registers the library does not name are reached through its `modbus<address>`
pseudo-settings, which map to a raw single-register read or write.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from goodwe import EMSMode, Inverter, InverterError
from goodwe.exceptions import RequestRejectedException

from .const import (
    BATTERY_POWER_MAX,
    EMS_POWER_MAX,
    MANUFACTURER_CODE_EMS,
    REG_BMS_RATED_CAPACITY,
    REG_CHARGE_ALLOW_WH,
    REG_CLEAR_ECONOMIC_SCHEDULE,
    REG_DISCHARGE_ALLOW_WH,
    REG_MANUFACTURER_CODE,
)

_LOGGER = logging.getLogger(__name__)

# Delay between a write and its read-back. The inverter applies a write
# asynchronously; reading immediately returns the previous value often enough to
# produce a stream of false mismatch warnings.
_READBACK_DELAY = 0.15

# Library setting ids for the EMS pair, so the string literals appear once.
SETTING_EMS_MODE = "ems_mode"
SETTING_EMS_POWER = "ems_power_limit"

#: Selectable EMS modes, keyed by the translation key used in the select entity.
#: Built from the library enum rather than hand-written so a library update
#: cannot leave us offering a mode the inverter no longer accepts.
EMS_MODES: dict[str, EMSMode] = {mode.name.lower(): mode for mode in EMSMode}
EMS_MODES_REVERSE: dict[EMSMode, str] = {v: k for k, v in EMS_MODES.items()}


def pseudo_setting(register: int) -> str:
    """Library id for a raw single-register read or write.

    The underscore is load-bearing. The library guards on the prefix `modbus`
    but parses the address with `int(setting_id[7:])`, so it expects exactly one
    separator character; `modbus47505` would silently address register 7505.
    """
    return f"modbus_{register}"


def _clamp(value: float, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


@dataclass(frozen=True)
class BatteryLimits:
    """What the BMS says it can take or give right now.

    Preferable to `capacity x SoC`, which assumes an ideal battery: these
    figures already include temperature derating and per-cell limits. Every
    field is optional because not every model populates the registers.
    """

    charge_allow_kwh: float | None = None
    discharge_allow_kwh: float | None = None
    rated_capacity_kwh: float | None = None


class GoodweEms:
    """The EMS commands, grouped by what they accomplish."""

    def __init__(self, inverter: Inverter) -> None:
        self._inverter = inverter
        self._armed = False
        # Set once the BMS block has proved unreadable, so an inverter without
        # those registers is not probed three times per update cycle forever.
        self._bms_block_unsupported = False

    @property
    def inverter(self) -> Inverter:
        return self._inverter

    # ----------------------------------------------------------------------
    # Raw register access
    # ----------------------------------------------------------------------

    async def async_read_register(self, register: int) -> int:
        """Read one holding register as a signed 16-bit value."""
        return await self._inverter.read_setting(pseudo_setting(register))

    async def async_write_register(self, register: int, value: int) -> None:
        """Write one holding register."""
        await self._inverter.write_setting(pseudo_setting(register), value)

    async def _async_write_verified(self, setting: str, value: int) -> None:
        """Write a setting, then read it back and log any disagreement."""
        await self._inverter.write_setting(setting, value)
        await asyncio.sleep(_READBACK_DELAY)
        try:
            readback = await self._inverter.read_setting(setting)
        except (InverterError, ValueError) as err:
            _LOGGER.debug("Could not read back %s: %s", setting, err)
            return
        if readback != value:
            _LOGGER.warning(
                "Read-back mismatch on %s: wrote %s, read %s", setting, value, readback
            )

    # ----------------------------------------------------------------------
    # EMS commands
    # ----------------------------------------------------------------------

    async def async_arm(self, force: bool = False) -> None:
        """Put manufacturer code 2 in 47505, without which EMS mode is ignored."""
        if self._armed and not force:
            return
        current = await self.async_read_register(REG_MANUFACTURER_CODE)
        if current != MANUFACTURER_CODE_EMS:
            _LOGGER.debug(
                "Arming EMS: 47505 %s -> %s", current, MANUFACTURER_CODE_EMS
            )
            await self.async_write_register(
                REG_MANUFACTURER_CODE, MANUFACTURER_CODE_EMS
            )
        self._armed = True

    async def async_set_ems(self, mode: EMSMode, power: int = 0) -> None:
        """Set EMS mode and its power parameter.

        Power is written before the mode: enabling a mode while the previous
        run's power is still in place leaves the inverter on an unwanted
        setpoint for as long as it takes the second write to land.
        """
        await self.async_arm()
        await self._async_write_verified(
            SETTING_EMS_POWER, _clamp(power, 0, EMS_POWER_MAX)
        )
        await self._async_write_verified(SETTING_EMS_MODE, int(mode))

    async def async_set_ems_mode(self, mode: EMSMode) -> None:
        """Only the mode, leaving the current power alone."""
        await self.async_arm()
        await self._async_write_verified(SETTING_EMS_MODE, int(mode))

    async def async_set_ems_power(self, watts: int) -> None:
        """Only the power, leaving the current mode alone."""
        await self.async_arm()
        await self._async_write_verified(
            SETTING_EMS_POWER, _clamp(watts, 0, EMS_POWER_MAX)
        )

    async def async_get_ems_mode(self) -> EMSMode | None:
        """Current EMS mode, or None if the inverter reports an unknown value."""
        try:
            return await self._inverter.get_ems_mode()
        except ValueError:
            _LOGGER.debug("Inverter reported an EMS mode the library does not know")
            return None

    async def async_charge_from_grid(self, watts: int) -> None:
        """Forced charge: PV first, topped up from the grid."""
        await self.async_set_ems(
            EMSMode.CHARGE_BATTERY, _clamp(watts, 0, BATTERY_POWER_MAX)
        )

    async def async_discharge(self, watts: int) -> None:
        await self.async_set_ems(
            EMSMode.DISCHARGE_BATTERY, _clamp(watts, 0, BATTERY_POWER_MAX)
        )

    async def async_auto(self) -> None:
        """Self-consumption: the safe fallback state."""
        await self.async_set_ems(EMSMode.AUTO, 0)

    async def async_standby(self) -> None:
        """Battery neither charges nor discharges."""
        await self.async_set_ems(EMSMode.BATTERY_STANDBY, 0)

    async def async_clear_economic_schedule(self) -> None:
        """Clear the economic schedule in 47515-47530.

        Needed before price dispatch takes over: an active economic schedule
        competes with the EMS commands and the two together behave erratically.
        """
        await self.async_write_register(REG_CLEAR_ECONOMIC_SCHEDULE, 1)

    # ----------------------------------------------------------------------
    # BMS figures used by the dispatch engine
    # ----------------------------------------------------------------------

    async def async_read_battery_limits(self) -> BatteryLimits:
        """Read the BMS allowances. Every field is best-effort."""
        if self._bms_block_unsupported:
            return BatteryLimits()

        try:
            charge_wh = await self._async_read_u32(REG_CHARGE_ALLOW_WH)
            discharge_wh = await self._async_read_u32(REG_DISCHARGE_ALLOW_WH)
            rated = await self.async_read_register(REG_BMS_RATED_CAPACITY)
        except (RequestRejectedException, ValueError) as err:
            # The inverter answered, and the answer was "no such register".
            # That verdict will not change, so stop asking.
            _LOGGER.debug("BMS limit registers unsupported, not retrying: %s", err)
            self._bms_block_unsupported = True
            return BatteryLimits()
        except InverterError as err:
            # A timeout or a dropped packet. `RequestFailedException` is an
            # `InverterError` too, so latching here would let one missed UDP
            # datagram disable the BMS refinement for the rest of the session.
            _LOGGER.debug("BMS limit registers unreadable this cycle: %s", err)
            return BatteryLimits()

        # A battery cannot be full and empty at once. Both at zero means the
        # registers are not populated on this model, and passing those zeros on
        # would stall dispatch entirely. Zero on one of the two is credible.
        if not charge_wh and not discharge_wh:
            charge_kwh = discharge_kwh = None
        else:
            charge_kwh = charge_wh / 1000
            discharge_kwh = discharge_wh / 1000

        return BatteryLimits(
            charge_allow_kwh=charge_kwh,
            discharge_allow_kwh=discharge_kwh,
            rated_capacity_kwh=(rated / 10) or None,
        )

    async def _async_read_u32(self, register: int) -> int:
        """Read a 32-bit value as two registers.

        The pseudo-setting reads a single register and sign-extends it, so both
        halves are masked back to their unsigned 16-bit form before being
        recombined.
        """
        high = await self.async_read_register(register)
        low = await self.async_read_register(register + 1)
        return ((high & 0xFFFF) << 16) | (low & 0xFFFF)
