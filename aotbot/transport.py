"""Asyncio UDP transport for the Age of Time bot.

This layer is intentionally **protocol-agnostic**: it only moves datagrams
(``bytes`` <-> ``addr``). The connection/handshake layer (``netconn.py``, owned
by another agent) sits on top and interprets the bytes via
:class:`aotbot.bitstream.BitStream`.

Torque networking is connectionless UDP, so a single bound socket sends to and
receives from any peer. We bind once, expose :meth:`sendto`, and yield inbound
datagrams via an async iterator / :meth:`recv` coroutine.

Optional hexdump logging is available on the ``aotbot.transport`` logger at
DEBUG level (gate it with the ``DUMP_PACKETS`` config option upstream).
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional, Tuple

logger = logging.getLogger("aotbot.transport")

Addr = Tuple[str, int]


def _hexdump(data: bytes, width: int = 16) -> str:
    """Return a classic offset / hex / ASCII hexdump of ``data``."""
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off : off + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        hex_part = hex_part.ljust(width * 3 - 1)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off:04x}  {hex_part}  {ascii_part}")
    return "\n".join(lines)


class _UDPProtocol(asyncio.DatagramProtocol):
    """Bridges asyncio's callback-based datagram API to an asyncio.Queue."""

    def __init__(self, queue: "asyncio.Queue[Tuple[bytes, Addr]]"):
        self._queue = queue
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:  # type: ignore[override]
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: Addr) -> None:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("RECV %d bytes from %s:%d\n%s", len(data), addr[0], addr[1], _hexdump(data))
        self._queue.put_nowait((data, addr))

    def error_received(self, exc: Exception) -> None:
        # On UDP, errors (e.g. ICMP port-unreachable) are informational.
        logger.warning("UDP error_received: %r", exc)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        if exc is not None:
            logger.warning("UDP connection_lost: %r", exc)


class UdpTransport:
    """A bound asyncio UDP socket that sends and receives datagrams.

    Usage::

        t = UdpTransport()
        await t.open(local_addr=("0.0.0.0", 0))
        t.sendto(b"...", ("1.2.3.4", 28000))
        data, addr = await t.recv()
        async for data, addr in t:
            ...
        t.close()
    """

    def __init__(self, recv_queue_maxsize: int = 0):
        self._protocol: Optional[_UDPProtocol] = None
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._queue: "asyncio.Queue[Tuple[bytes, Addr]]" = asyncio.Queue(recv_queue_maxsize)

    @property
    def is_open(self) -> bool:
        return self._transport is not None and not self._transport.is_closing()

    def local_addr(self) -> Optional[Addr]:
        """The bound local ``(host, port)``, or ``None`` if not open."""
        if self._transport is None:
            return None
        sock = self._transport.get_extra_info("socket")
        if sock is None:
            return None
        return sock.getsockname()

    async def open(
        self,
        local_addr: Addr = ("0.0.0.0", 0),
        *,
        remote_addr: Optional[Addr] = None,
    ) -> None:
        """Bind a UDP socket.

        :param local_addr: ``(host, port)`` to bind locally. Port 0 picks an
            ephemeral port (the usual case for a Torque client).
        :param remote_addr: optional default peer. Left ``None`` for Torque,
            which is connectionless and addresses each datagram explicitly.
        """
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _UDPProtocol(self._queue),
            local_addr=local_addr,
            remote_addr=remote_addr,
        )
        self._transport = transport  # type: ignore[assignment]
        self._protocol = protocol
        bound = self.local_addr()
        logger.info("UDP transport bound to %s", bound)

    def sendto(self, data: bytes, addr: Optional[Addr] = None) -> None:
        """Send a datagram to ``addr`` (or the default remote if bound with one).

        Non-blocking, matching ``asyncio.DatagramTransport.sendto``.
        """
        if self._transport is None:
            raise RuntimeError("UdpTransport.sendto called before open()")
        if logger.isEnabledFor(logging.DEBUG):
            dst = addr if addr is not None else self.local_addr()
            logger.debug("SEND %d bytes to %s\n%s", len(data), dst, _hexdump(data))
        self._transport.sendto(data, addr)

    async def recv(self) -> Tuple[bytes, Addr]:
        """Await the next inbound datagram as ``(data, addr)``."""
        return await self._queue.get()

    def __aiter__(self) -> AsyncIterator[Tuple[bytes, Addr]]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Tuple[bytes, Addr]]:
        while True:
            yield await self._queue.get()

    def close(self) -> None:
        """Close the socket. Safe to call multiple times."""
        if self._transport is not None:
            self._transport.close()
            self._transport = None
            self._protocol = None
            logger.info("UDP transport closed")
