"""Unit tests for the NetConnection handshake state machine and the connected
packet header round-trip, driven against a loopback mock server.

All coroutines run via ``asyncio.run`` so no pytest-asyncio is required.
"""

import asyncio
import os

import aotbot.protocol_constants as pc
from aotbot.bitstream import BitStream
from aotbot.netconn import ConnState, NetConnection
from aotbot.transport import UdpTransport


class MockServer:
    """A minimal AoT-like server: answers the OOB handshake on a UDP socket."""

    def __init__(self):
        self.transport = UdpTransport()
        self.digest = os.urandom(16)
        self.client_seq = None
        self.task = None
        self.protocol_to_send = pc.PROTOCOL_VERSION
        self.reject_reason = None  # if set, reject instead of accept
        self.data_packets_received = []

    async def start(self):
        await self.transport.open(local_addr=("127.0.0.1", 0))
        self.task = asyncio.create_task(self._loop())

    @property
    def addr(self):
        return self.transport.local_addr()

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.transport.close()

    async def _loop(self):
        async for data, addr in self.transport:
            if data[0] & 0x01:
                self.data_packets_received.append(data)
                continue
            self._handle_oob(data, addr)

    def _handle_oob(self, data, addr):
        bs = BitStream(data)
        ptype = bs.read_u8()
        seq = bs.read_int(32)
        if ptype == pc.CONNECT_CHALLENGE_REQUEST:
            self.client_seq = seq
            out = BitStream()
            out.write_u8(pc.CONNECT_CHALLENGE_RESPONSE)
            out.write_int(seq, 32)
            out.write_bytes(self.digest)
            self.transport.sendto(out.get_bytes(), addr)
        elif ptype == pc.CONNECT_REQUEST:
            # Echo digest + read identity fields (we don't strictly validate).
            digest = bs.read_bytes(16)
            assert bytes(digest) == self.digest
            classname = bs.read_string()
            group = bs.read_int(32)
            crc = bs.read_int(32)
            game = bs.read_string()
            self.seen = dict(classname=classname, group=group, crc=crc, game=game)
            out = BitStream()
            if self.reject_reason is not None:
                out.write_u8(pc.CONNECT_REJECT)
                out.write_int(seq, 32)
                out.write_string(self.reject_reason)
            else:
                out.write_u8(pc.CONNECT_ACCEPT)
                out.write_int(seq, 32)
                out.write_int(self.protocol_to_send, 32)
            self.transport.sendto(out.get_bytes(), addr)


def _new_conn(server):
    t = UdpTransport()
    return t


def test_handshake_accept():
    async def go():
        server = MockServer()
        await server.start()
        t = UdpTransport()
        await t.open(local_addr=("127.0.0.1", 0))
        conn = NetConnection(t, server.addr)
        try:
            ok = await conn.connect(timeout=5.0)
            assert ok is True
            assert conn.state == ConnState.CONNECTED
            assert conn.server_protocol_version == pc.PROTOCOL_VERSION
            assert conn.address_digest == server.digest
            # The server saw our AoT identity fields.
            assert server.seen["classname"] == pc.CONNECTION_CLASS_NAME
            assert server.seen["group"] == pc.NET_CLASS_GROUP
            assert server.seen["crc"] == pc.CONNECT_CLASS_CRC
            assert server.seen["game"] == pc.GAME_STRING
        finally:
            await conn.disconnect("test done")
            t.close()
            await server.stop()

    asyncio.run(go())


def test_handshake_reject():
    async def go():
        server = MockServer()
        server.reject_reason = "CHR_INVALID"
        await server.start()
        t = UdpTransport()
        await t.open(local_addr=("127.0.0.1", 0))
        conn = NetConnection(t, server.addr)
        try:
            ok = await conn.connect(timeout=5.0)
            assert ok is False
            assert conn.state == ConnState.REJECTED
            assert conn.reject_reason == "CHR_INVALID"
        finally:
            t.close()
            await server.stop()

    asyncio.run(go())


def test_handshake_timeout_no_server():
    async def go():
        t = UdpTransport()
        await t.open(local_addr=("127.0.0.1", 0))
        # Point at a port with nothing listening.
        conn = NetConnection(t, ("127.0.0.1", 1))
        conn_timeout = 0.6
        try:
            ok = await conn.connect(timeout=conn_timeout)
            assert ok is False
            assert conn.state == ConnState.TIMED_OUT
        finally:
            t.close()

    asyncio.run(go())


def test_connect_request_wire_format():
    """The ConnectRequest bytes match the EXE-confirmed layout exactly."""
    t = UdpTransport()
    conn = NetConnection(t, ("127.0.0.1", 28000))
    conn.connect_sequence = 0x11223344
    conn.address_digest = bytes(range(16))
    pkt = conn._build_connect_request()
    bs = BitStream(pkt)
    assert bs.read_u8() == pc.CONNECT_REQUEST
    assert bs.read_int(32) == 0x11223344
    assert bytes(bs.read_bytes(16)) == bytes(range(16))
    assert bs.read_string() == pc.CONNECTION_CLASS_NAME
    assert bs.read_int(32) == pc.NET_CLASS_GROUP
    assert bs.read_int(32) == pc.CONNECT_CLASS_CRC
    assert bs.read_string() == pc.GAME_STRING
    assert bs.read_int(32) == pc.PROTOCOL_VERSION
    assert bs.read_int(32) == pc.MIN_PROTOCOL_VERSION
    assert bs.read_string() == ""        # empty join password
    assert bs.read_int(32) == 0          # connectArgc


def test_connect_request_carries_version_arg():
    """When connect_args are set (version + player name), they ride after the
    join password as connectArgc + writeString each. This is what makes the
    real AoT server stop disconnecting us with the 'newest version' message."""
    t = UdpTransport()
    conn = NetConnection(
        t, ("127.0.0.1", 28000),
        connect_args=[pc.CLIENT_VERSION, "Player"],
    )
    conn.connect_sequence = 1
    conn.address_digest = bytes(16)
    bs = BitStream(conn._build_connect_request())
    bs.read_u8()
    bs.read_int(32)
    bs.read_bytes(16)
    bs.read_string()           # className
    bs.read_int(32)            # group
    bs.read_int(32)            # crc
    bs.read_string()           # game string
    bs.read_int(32)            # proto
    bs.read_int(32)            # min proto
    assert bs.read_string() == ""   # join password
    assert bs.read_int(32) == 2     # connectArgc
    assert bs.read_string() == pc.CLIENT_VERSION
    assert bs.read_string() == "Player"


def test_data_packet_header_layout():
    """A built DataPacket header parses back to the same field values and the
    first byte's LSB is 1 (data family)."""
    t = UdpTransport()
    conn = NetConnection(t, ("127.0.0.1", 28000))
    conn.connect_sequence = 0b1010  # parity bit 0
    conn.state = ConnState.CONNECTED
    conn.last_seq_recvd = 5
    conn.last_recv_ack_ack = 0
    conn.ack_mask = 0b1011

    bs = BitStream()
    conn._write_header(bs, pc.PACKET_TYPE_DATA)
    out = bs.get_bytes()
    assert out[0] & 0x01 == 1  # data/connection family

    # last_send_seq was pre-incremented from 0 to 1 for a DataPacket.
    assert conn.last_send_seq == 1

    rs = BitStream(out)
    assert rs.read_flag() is True                       # game flag
    assert rs.read_int(1) == (conn.connect_sequence & 1)  # parity
    assert rs.read_int(9) == 1                           # send seq
    assert rs.read_int(9) == 5                           # last recvd
    assert rs.read_int(2) == pc.PACKET_TYPE_DATA
    ack_byte_count = rs.read_int(3)
    assert ack_byte_count == ((5 - 0 + 7) >> 3)          # == 1
    assert rs.read_int(ack_byte_count * 8) == 0b1011


def test_process_raw_packet_acks_and_advances():
    """Feeding the connection a server DataPacket advances its receive window
    and triggers a body read + ack send."""
    async def go():
        server = MockServer()
        await server.start()
        t = UdpTransport()
        await t.open(local_addr=("127.0.0.1", 0))
        conn = NetConnection(t, server.addr)
        ok = await conn.connect(timeout=5.0)
        assert ok

        body_reads = []
        conn.read_packet_body = lambda bs: body_reads.append(True)

        # Build a server->client DataPacket: header (type Data) + rate block(0,0)
        # + empty body (the hook records the read).
        pkt = BitStream()
        # Mirror buildSendPacketHeader from the server's perspective.
        pkt.write_flag(True)
        pkt.write_int(conn.connect_sequence & 1, 1)
        pkt.write_int(1, 9)   # server send seq = 1
        pkt.write_int(0, 9)   # server's last recvd of us
        pkt.write_int(pc.PACKET_TYPE_DATA, 2)
        pkt.write_int(0, 3)   # ackByteCount 0
        # rate block
        pkt.write_flag(False)
        pkt.write_flag(False)
        server.transport.sendto(pkt.get_bytes(), t.local_addr())

        # Let the pump process it.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if conn.last_seq_recvd == 1:
                break
        assert conn.last_seq_recvd == 1
        assert body_reads  # body hook fired

        await conn.disconnect("done")
        t.close()
        await server.stop()

    asyncio.run(go())
