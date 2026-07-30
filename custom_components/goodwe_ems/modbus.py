"""Strat Modbus de nivel jos pentru invertoare GoodWe.

Încapsulează pymodbus și normalizează diferențele de API între versiuni.
Toate operațiile sunt serializate printr-un lock: invertoarele GoodWe nu
tolerează cereri concurente pe aceeași conexiune și răspund cu timeout.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

from .const import (
    CONNECTION_RTU_OVER_TCP,
    CONNECTION_SERIAL,
    CONNECTION_TCP,
)


class ModbusError(Exception):
    """Eroare de comunicație sau răspuns de excepție de la invertor."""


def to_signed16(value: int) -> int:
    return value - 0x10000 if value >= 0x8000 else value


def to_unsigned16(value: int) -> int:
    return value + 0x10000 if value < 0 else value


def to_signed32(high: int, low: int) -> int:
    raw = (high << 16) | low
    return raw - 0x100000000 if raw >= 0x80000000 else raw


def split32(value: int) -> tuple[int, int]:
    raw = value + 0x100000000 if value < 0 else value
    return (raw >> 16) & 0xFFFF, raw & 0xFFFF


class GoodweModbusClient:
    """Conexiune Modbus persistentă către invertor."""

    def __init__(
        self,
        connection: str,
        slave: int,
        host: str | None = None,
        port: int = 502,
        serial_port: str | None = None,
        baudrate: int = 9600,
        timeout: float = 10.0,
    ) -> None:
        self._connection = connection
        self._slave = slave
        self._host = host
        self._port = port
        self._serial_port = serial_port
        self._baudrate = baudrate
        self._timeout = timeout
        self._client: Any = None
        self._lock = asyncio.Lock()

    # -- ciclu de viață -----------------------------------------------------

    async def async_connect(self) -> None:
        if self._client is not None and getattr(self._client, "connected", False):
            return

        # Importul e amânat: pymodbus e o dependință declarată în manifest și
        # nu trebuie să blocheze încărcarea modulului la validarea hassfest.
        from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient

        if self._connection == CONNECTION_SERIAL:
            self._client = AsyncModbusSerialClient(
                port=self._serial_port,
                baudrate=self._baudrate,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=self._timeout,
            )
        else:
            kwargs: dict[str, Any] = {
                "host": self._host,
                "port": self._port,
                "timeout": self._timeout,
            }
            if self._connection == CONNECTION_RTU_OVER_TCP:
                kwargs["framer"] = _rtu_framer()
            self._client = AsyncModbusTcpClient(**kwargs)

        connected = await self._client.connect()
        if not connected:
            raise ModbusError(
                f"Conexiunea Modbus a eșuat ({self._connection} "
                f"{self._host or self._serial_port})"
            )

    async def async_close(self) -> None:
        if self._client is None:
            return
        close = self._client.close()
        if asyncio.iscoroutine(close):
            await close
        self._client = None

    # -- primitive ----------------------------------------------------------

    async def async_read(self, address: int, count: int = 1) -> list[int]:
        """Citește `count` holding registers începând de la `address`."""
        async with self._lock:
            await self.async_connect()
            result = await _invoke(
                self._client.read_holding_registers,
                address,
                count=count,
                slave=self._slave,
            )
            if result is None or _is_error(result):
                raise ModbusError(f"Citire eșuată la {address} (x{count}): {result}")
            registers = list(result.registers)
            if len(registers) != count:
                raise ModbusError(
                    f"Citire parțială la {address}: {len(registers)}/{count} registre"
                )
            return registers

    async def async_write(self, address: int, value: int) -> None:
        """Scrie un singur holding register."""
        async with self._lock:
            await self.async_connect()
            result = await _invoke(
                self._client.write_register,
                address,
                to_unsigned16(value),
                slave=self._slave,
            )
            if result is None or _is_error(result):
                raise ModbusError(f"Scriere eșuată la {address} = {value}: {result}")

    async def async_write_many(self, address: int, values: list[int]) -> None:
        """Scrie registre consecutive (funcție 0x10)."""
        async with self._lock:
            await self.async_connect()
            result = await _invoke(
                self._client.write_registers,
                address,
                [to_unsigned16(v) for v in values],
                slave=self._slave,
            )
            if result is None or _is_error(result):
                raise ModbusError(f"Scriere multiplă eșuată la {address}: {result}")

    # -- ajutoare tipizate --------------------------------------------------

    async def async_read_u16(self, address: int) -> int:
        return (await self.async_read(address))[0]

    async def async_read_s16(self, address: int) -> int:
        return to_signed16((await self.async_read(address))[0])

    async def async_read_s32(self, address: int) -> int:
        high, low = await self.async_read(address, 2)
        return to_signed32(high, low)

    async def async_write_s32(self, address: int, value: int) -> None:
        high, low = split32(value)
        await self.async_write_many(address, [high, low])

    async def async_write_verified(
        self, address: int, value: int, signed: bool = False
    ) -> int:
        """Scrie, apoi citește înapoi. Întoarce valoarea confirmată.

        Registrele volatile (47511, 47512, 47545) se pot pierde tăcut la un
        reboot de invertor. Readback-ul este singurul mod de a ști că scrierea
        a prins efectiv.
        """
        await self.async_write(address, value)
        await asyncio.sleep(0.15)
        readback = (
            await self.async_read_s16(address)
            if signed
            else await self.async_read_u16(address)
        )
        if readback != value:
            _LOGGER.warning(
                "Readback discordant la %s: scris %s, citit %s", address, value, readback
            )
        return readback


def _rtu_framer() -> Any:
    """Întoarce framer-ul RTU indiferent de versiunea pymodbus."""
    try:  # pymodbus >= 3.7
        from pymodbus import FramerType

        return FramerType.RTU
    except ImportError:
        pass
    try:  # pymodbus 3.5 - 3.6
        from pymodbus.framer import Framer

        return Framer.RTU
    except ImportError:
        from pymodbus.framer.rtu_framer import ModbusRtuFramer

        return ModbusRtuFramer


async def _invoke(func: Any, *args: Any, slave: int, **kwargs: Any) -> Any:
    """Apelează o metodă pymodbus, tratând redenumirea slave -> device_id."""
    try:
        return await func(*args, slave=slave, **kwargs)
    except TypeError:
        return await func(*args, device_id=slave, **kwargs)


def _is_error(result: Any) -> bool:
    is_error = getattr(result, "isError", None)
    return bool(is_error()) if callable(is_error) else False
