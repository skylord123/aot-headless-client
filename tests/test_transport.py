"""Loopback tests for the asyncio UDP transport.

These wrap each coroutine in ``asyncio.run`` so the suite needs only ``pytest``
(no ``pytest-asyncio`` plugin).
"""

import asyncio
import logging

import pytest

from aotbot.transport import UdpTransport


def test_loopback_send_recv():
    async def go():
        # Bind two transports on localhost; send from one, receive on the other.
        a = UdpTransport()
        b = UdpTransport()
        await a.open(local_addr=("127.0.0.1", 0))
        await b.open(local_addr=("127.0.0.1", 0))
        try:
            b_addr = b.local_addr()
            payload = b"\xde\xad\xbe\xef hello torque"
            a.sendto(payload, ("127.0.0.1", b_addr[1]))

            data, addr = await asyncio.wait_for(b.recv(), timeout=2.0)
            assert data == payload
            assert addr[0] == "127.0.0.1"
        finally:
            a.close()
            b.close()

    asyncio.run(go())


def test_send_to_self():
    async def go():
        t = UdpTransport()
        await t.open(local_addr=("127.0.0.1", 0))
        try:
            addr = t.local_addr()
            t.sendto(b"self-packet", ("127.0.0.1", addr[1]))
            data, _src = await asyncio.wait_for(t.recv(), timeout=2.0)
            assert data == b"self-packet"
        finally:
            t.close()

    asyncio.run(go())


def test_async_iteration_yields_datagrams():
    async def go():
        a = UdpTransport()
        b = UdpTransport()
        await a.open(local_addr=("127.0.0.1", 0))
        await b.open(local_addr=("127.0.0.1", 0))
        try:
            port = b.local_addr()[1]
            for i in range(3):
                a.sendto(f"pkt{i}".encode(), ("127.0.0.1", port))

            received = []

            async def collect():
                async for data, _addr in b:
                    received.append(data)
                    if len(received) == 3:
                        return

            await asyncio.wait_for(collect(), timeout=2.0)
            assert sorted(received) == [b"pkt0", b"pkt1", b"pkt2"]
        finally:
            a.close()
            b.close()

    asyncio.run(go())


def test_sendto_before_open_raises():
    t = UdpTransport()
    with pytest.raises(RuntimeError):
        t.sendto(b"x", ("127.0.0.1", 9999))


def test_local_addr_none_before_open():
    t = UdpTransport()
    assert t.local_addr() is None
    assert not t.is_open


def test_hexdump_logging(caplog):
    async def go():
        a = UdpTransport()
        b = UdpTransport()
        await a.open(local_addr=("127.0.0.1", 0))
        await b.open(local_addr=("127.0.0.1", 0))
        try:
            with caplog.at_level(logging.DEBUG, logger="aotbot.transport"):
                a.sendto(b"\x01\x02\x03", ("127.0.0.1", b.local_addr()[1]))
                await asyncio.wait_for(b.recv(), timeout=2.0)
            msgs = "\n".join(r.getMessage() for r in caplog.records)
            assert "SEND" in msgs
            assert "RECV" in msgs
        finally:
            a.close()
            b.close()

    asyncio.run(go())
