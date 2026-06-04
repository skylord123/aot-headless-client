"""NetConnection: the Torque OOB handshake + connected packet/notify layer.

This sits on top of :class:`aotbot.transport.UdpTransport` and implements, in
order:

1. The connectionless out-of-band handshake state machine
   (ConnectChallengeRequest -> ChallengeResponse -> ConnectRequest ->
   ConnectAccept/Reject), mirroring ``$TGE/engine/sim/netInterface.cc`` with the
   AoT-confirmed constants (``GAME_STRING="Age Of Time Demo"``, protocol 11,
   classCRC ``0xFFFFFFFF``, netClassGroup 0). See docs/handshake.md.

2. The connected data-packet layer: the bit-packed header
   ``1|1|9|9|2|3|ackByteCount*8`` (gameFlag, connectSeq parity, lastSendSeq,
   lastSeqRecvd, packetType, ackByteCount, ackMask), sequence numbers, the
   sliding-window ack/notify reliability protocol, keep-alive ping/ack and a
   clean disconnect. Mirrors ``$TGE/engine/core/dnet.cc``
   (``ConnectionProtocol``) and ``netConnection.cc``.

Higher layers (phases / events) plug in via two hooks set by the owner:

* ``write_packet_body(bs)`` -- called while building a DataPacket, after the
  rate block, to append the GameConnection control header + event + ghost
  sections.
* ``read_packet_body(bs)`` -- called while parsing a received DataPacket, after
  the rate block, to consume the same sections.

The connection runs as an asyncio task pumping the shared UdpTransport. Only the
datagrams whose source matches our server address are handed to this connection;
that filtering is trivial here because we only ever talk to one server.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from typing import Callable, Optional, Tuple

from . import protocol_constants as pc
from .bitstream import BitStream
from .transport import UdpTransport

logger = logging.getLogger("aotbot.netconn")

Addr = Tuple[str, int]

# Handshake retry tuning (TGE netInterface.h:47-48). We keep the counts but use
# a slightly faster cadence so the bot reacts quickly; the server is stateless
# about our retries (it just answers each request).
CHALLENGE_RETRY_TIME = 2.5
CHALLENGE_RETRY_COUNT = 4
CONNECT_RETRY_TIME = 2.5
CONNECT_RETRY_COUNT = 4

# The bot's outgoing DataPacket clock. The REAL client sends EXACTLY one
# move-bearing DataPacket per game tick (~32 ms => ~31/s) via moveWritePacket,
# and it batches its ack of the server into that scheduled packet (its c2s
# inter-packet dt clusters at 13-17 ms with a hard 32/s ceiling; capture
# bad_login.jsonl). It does NOT fire an extra packet in reaction to each received
# packet.
#
# CAPTURE-CONFIRMED STALL CAUSE (Wave-15): the bot used to (a) send a DATA packet
# in REPLY to every received DATA packet AND (b) run a 100 ms keepalive on top,
# so during the GhostAlways flood (server ~33/s) it burst to 40-45/s with 165
# back-to-back (<=5 ms) packets -- well over the client's 32/s ceiling. The AoT
# server has FloodProtectionEnabled=1; that over-rate trips it and it stops
# servicing the connection mid-stream (the "213 ghost-event stall"). Sending on a
# fixed ~32 ms tick like the real client (one packet per tick, ack rides it) keeps
# us at/under 32/s and lets the full GhostAlways stream complete.
KEEPALIVE_INTERVAL = 0.032


class ConnState(enum.Enum):
    """NetConnection lifecycle (mirrors TGE NetConnectionState)."""

    DISCONNECTED = "disconnected"
    AWAITING_CHALLENGE_RESPONSE = "awaiting_challenge_response"
    AWAITING_CONNECT_RESPONSE = "awaiting_connect_response"
    CONNECTED = "connected"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"


class NetConnection:
    """Drives one connection to an AoT server over a shared UdpTransport."""

    def __init__(
        self,
        transport: UdpTransport,
        server_addr: Addr,
        *,
        join_password: str = "",
        connect_args: Optional[list[str]] = None,
        keepalive_interval: float = KEEPALIVE_INTERVAL,
    ) -> None:
        self.transport = transport
        self.server_addr = server_addr
        self.join_password = join_password
        self.connect_args = connect_args or []
        self.keepalive_interval = keepalive_interval

        # Handshake nonce. TGE uses getVirtualMilliseconds(); any 32-bit value
        # works (the server echoes it). The genuine client's captured
        # ConnectRequest used connect_sequence = 0; setting 0 live did NOT change
        # the server's behavior, so we keep a random nonce (more correct, avoids
        # two instances colliding). Override via AOT_CONNECT_SEQUENCE for parity
        # debugging against the capture.
        env_seq = os.environ.get("AOT_CONNECT_SEQUENCE")
        if env_seq is not None:
            self.connect_sequence = int(env_seq) & 0xFFFFFFFF
        else:
            self.connect_sequence = int.from_bytes(os.urandom(4), "little")
        self.address_digest: Optional[bytes] = None  # 16 raw bytes, echoed back.

        self.state = ConnState.DISCONNECTED
        self.reject_reason: Optional[str] = None
        self.disconnect_reason: Optional[str] = None
        self.server_protocol_version: Optional[int] = None

        # --- ConnectionProtocol notify-window state (dnet.cc) ---
        self.last_seq_recvd = 0       # mLastSeqRecvd
        self.highest_acked_seq = 0    # mHighestAckedSeq
        self.last_send_seq = 0        # mLastSendSeq (first DataPacket is 1)
        self.ack_mask = 0             # mAckMask
        self.last_recv_ack_ack = 0    # mLastRecvAckAck
        self.last_seq_recvd_at_send = [0] * pc.PACKET_WINDOW_SIZE
        self.connection_established = False  # set once a send of ours is acked

        # Rate negotiation (netConnection.cc:598-612). The client declares its
        # desired packet rate to the server in the first DataPacket's rate block;
        # the "changed" flag clears after one send. CAPTURE-CONFIRMED: the genuine
        # AoT client's first c2s data packet sets rateChanged AND maxRateChanged
        # with (updateDelay=32, packetSize=450) -- faster than the stock 102/200
        # default. The server reads this (line 506) to configure its send rate to
        # us; WITHOUT it the server kept us at a default that never streamed
        # datablocks, so we stalled at MissionStartPhase1. We send it once.
        self.rate_update_delay = 32
        self.rate_packet_size = 450
        self.rate_changed = True       # send mCurRate once
        self.max_rate_changed = True   # send mMaxRate once

        # Hooks installed by the phase/event layer (default: empty body).
        self.write_packet_body: Callable[[BitStream], None] = lambda bs: None
        self.read_packet_body: Callable[[BitStream], None] = lambda bs: None
        # Notify hook: handle_notify(send_seq, delivered: bool).
        self.on_notify: Callable[[int, bool], None] = lambda seq, ok: None
        # Returns True if the body layer has queued (unsent/NACKed) events that
        # need to ride a DATA packet. When False, a received DATA packet is
        # answered with a cheap ACK packet (which does NOT consume the send
        # window), so the heavy ghost-stream phase can't exhaust the 30-packet
        # window and stop us from acking (the cause of the ghost-stream stall).
        self.has_pending_data: Callable[[], bool] = lambda: False
        # Connection-state change hook.
        self.on_state_change: Callable[[ConnState], None] = lambda st: None

        # Async machinery.
        self._connected_event = asyncio.Event()
        self._done_event = asyncio.Event()
        self._pump_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._last_recv_time = time.monotonic()
        # Set true when there is queued event data the body wants to flush soon.
        self._send_requested = False

    # ------------------------------------------------------------------ #
    # State helpers
    # ------------------------------------------------------------------ #

    def _set_state(self, st: ConnState) -> None:
        if st != self.state:
            self.state = st
            logger.info("connection state -> %s", st.value)
            try:
                self.on_state_change(st)
            except Exception:  # never let a hook kill the connection
                logger.exception("on_state_change hook raised")

    @property
    def is_connected(self) -> bool:
        return self.state == ConnState.CONNECTED

    async def wait_connected(self, timeout: Optional[float] = None) -> bool:
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self.state == ConnState.CONNECTED

    async def wait_done(self, timeout: Optional[float] = None) -> None:
        await asyncio.wait_for(self._done_event.wait(), timeout=timeout)

    def request_send(self) -> None:
        """Mark that a data packet should be sent soon (queued events exist)."""
        self._send_requested = True

    # ------------------------------------------------------------------ #
    # OOB handshake -- packet builders
    # ------------------------------------------------------------------ #

    def _build_challenge_request(self) -> bytes:
        bs = BitStream()
        bs.write_u8(pc.CONNECT_CHALLENGE_REQUEST)
        bs.write_int(self.connect_sequence, 32)
        return bs.get_bytes()

    def _build_connect_request(self) -> bytes:
        bs = BitStream()
        bs.write_u8(pc.CONNECT_REQUEST)
        bs.write_int(self.connect_sequence, 32)
        # 16-byte address digest, echoed verbatim (4 x U32).
        assert self.address_digest is not None and len(self.address_digest) == 16
        bs.write_bytes(self.address_digest)
        # NetConnection subclass class name.
        bs.write_string(pc.CONNECTION_CLASS_NAME)
        # NetConnection::writeConnectRequest base payload.
        bs.write_int(pc.NET_CLASS_GROUP, 32)
        bs.write_int(pc.CONNECT_CLASS_CRC, 32)
        # GameConnection::writeConnectRequest payload.
        bs.write_string(pc.GAME_STRING)
        bs.write_int(pc.PROTOCOL_VERSION, 32)
        bs.write_int(pc.MIN_PROTOCOL_VERSION, 32)
        bs.write_string(self.join_password)
        bs.write_int(len(self.connect_args), 32)
        for arg in self.connect_args:
            bs.write_string(arg)
        return bs.get_bytes()

    def _build_disconnect(self, reason: str) -> bytes:
        bs = BitStream()
        bs.write_u8(pc.DISCONNECT)
        bs.write_int(self.connect_sequence, 32)
        bs.write_string(reason)
        return bs.get_bytes()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def connect(self, timeout: float = 15.0) -> bool:
        """Run the full OOB handshake; return True on ConnectAccept.

        Starts the receive pump (which also handles the connected layer) and
        drives the challenge/connect retransmits until accepted, rejected, or
        timed out.
        """
        self._set_state(ConnState.AWAITING_CHALLENGE_RESPONSE)
        self._pump_task = asyncio.create_task(self._recv_pump(), name="netconn-pump")
        try:
            ok = await self._run_handshake(timeout=timeout)
        except Exception:
            logger.exception("handshake error")
            ok = False
        if ok:
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(), name="netconn-keepalive"
            )
        return ok

    async def _run_handshake(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout

        # Phase 1: challenge request retransmits until we get a digest.
        for _ in range(CHALLENGE_RETRY_COUNT):
            self.transport.sendto(self._build_challenge_request(), self.server_addr)
            logger.debug("sent ConnectChallengeRequest seq=0x%08x", self.connect_sequence)
            try:
                await asyncio.wait_for(
                    self._wait_state_past(ConnState.AWAITING_CHALLENGE_RESPONSE),
                    timeout=min(CHALLENGE_RETRY_TIME, max(0.1, deadline - time.monotonic())),
                )
                break
            except asyncio.TimeoutError:
                if time.monotonic() >= deadline:
                    break
        if self.address_digest is None:
            logger.warning("no ChallengeResponse received")
            self._set_state(ConnState.TIMED_OUT)
            self._done_event.set()
            return False

        # Phase 2: connect request retransmits until accept/reject.
        for _ in range(CONNECT_RETRY_COUNT):
            if self.state in (ConnState.CONNECTED, ConnState.REJECTED):
                break
            self.transport.sendto(self._build_connect_request(), self.server_addr)
            logger.debug("sent ConnectRequest seq=0x%08x", self.connect_sequence)
            try:
                await asyncio.wait_for(
                    self._wait_state_past(ConnState.AWAITING_CONNECT_RESPONSE),
                    timeout=min(CONNECT_RETRY_TIME, max(0.1, deadline - time.monotonic())),
                )
                break
            except asyncio.TimeoutError:
                if time.monotonic() >= deadline:
                    break

        if self.state == ConnState.CONNECTED:
            return True
        if self.state == ConnState.REJECTED:
            logger.warning("connection rejected: %s", self.reject_reason)
        else:
            logger.warning("connect request timed out")
            self._set_state(ConnState.TIMED_OUT)
        self._done_event.set()
        return False

    async def _wait_state_past(self, st: ConnState) -> None:
        """Block until the state advances away from ``st``."""
        while self.state == st:
            await asyncio.sleep(0.02)

    async def disconnect(self, reason: str = "Done") -> None:
        """Send a Disconnect OOB packet and tear down tasks cleanly."""
        if self.transport.is_open and self.address_digest is not None:
            try:
                self.transport.sendto(self._build_disconnect(reason), self.server_addr)
            except Exception:
                pass
        self.disconnect_reason = reason
        await self._shutdown(ConnState.DISCONNECTED)

    async def _shutdown(self, st: ConnState) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            self._keepalive_task = None
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None
        self._set_state(st)
        self._connected_event.set()  # unblock any waiter
        self._done_event.set()

    # ------------------------------------------------------------------ #
    # Receive pump
    # ------------------------------------------------------------------ #

    async def _recv_pump(self) -> None:
        try:
            async for data, addr in self.transport:
                if addr[0] != self.server_addr[0] or addr[1] != self.server_addr[1]:
                    # Not our server (shouldn't happen on a dedicated socket).
                    continue
                self._last_recv_time = time.monotonic()
                try:
                    self._dispatch(data)
                except Exception:
                    logger.exception("error dispatching %d-byte packet", len(data))
        except asyncio.CancelledError:
            raise

    def _dispatch(self, data: bytes) -> None:
        if not data:
            return
        if data[0] & 0x01:
            # Connected data/protocol packet.
            self._process_raw_packet(data)
        else:
            # Out-of-band handshake packet; whole first byte is the type.
            self._handle_oob(data)

    def _handle_oob(self, data: bytes) -> None:
        bs = BitStream(data)
        ptype = bs.read_u8()
        if ptype == pc.CONNECT_CHALLENGE_RESPONSE:
            seq = bs.read_int(32)
            if seq != self.connect_sequence:
                logger.debug("challenge response seq mismatch; ignoring")
                return
            digest = bs.read_bytes(16)
            self.address_digest = bytes(digest)
            logger.debug("got ChallengeResponse, digest=%s", self.address_digest.hex())
            self._set_state(ConnState.AWAITING_CONNECT_RESPONSE)
        elif ptype == pc.CONNECT_ACCEPT:
            seq = bs.read_int(32)
            if seq != self.connect_sequence:
                logger.debug("connect accept seq mismatch; ignoring")
                return
            # GameConnection::readConnectAccept reads a U32 protocol version.
            self.server_protocol_version = bs.read_int(32)
            logger.info("ConnectAccept (server protocol=%s)", self.server_protocol_version)
            self.connection_established = False
            self._set_state(ConnState.CONNECTED)
            self._connected_event.set()
        elif ptype == pc.CONNECT_REJECT:
            seq = bs.read_int(32)
            if seq != self.connect_sequence:
                return
            self.reject_reason = bs.read_string()
            logger.warning("ConnectReject: %s", self.reject_reason)
            self._set_state(ConnState.REJECTED)
            self._connected_event.set()
        elif ptype == pc.DISCONNECT:
            seq = bs.read_int(32)
            if seq != self.connect_sequence:
                return
            self.disconnect_reason = bs.read_string()
            logger.warning("server Disconnect: %s", self.disconnect_reason)
            asyncio.create_task(self._shutdown(ConnState.DISCONNECTED))
        else:
            logger.debug("ignoring OOB packet type %d", ptype)

    # ------------------------------------------------------------------ #
    # Connected packet/notify layer (dnet.cc)
    # ------------------------------------------------------------------ #

    def _compute_ack_byte_count(self) -> int:
        return ((self.last_seq_recvd - self.last_recv_ack_ack + 7) >> 3)

    def _write_header(self, bs: BitStream, packet_type: int) -> None:
        """buildSendPacketHeader (dnet.cc:47-75)."""
        ack_byte_count = self._compute_ack_byte_count()
        if ack_byte_count > pc.MAX_ACK_BYTE_COUNT:
            ack_byte_count = pc.MAX_ACK_BYTE_COUNT
        if packet_type == pc.PACKET_TYPE_DATA:
            self.last_send_seq += 1
        bs.write_flag(True)  # gamePacketFlag
        bs.write_int(self.connect_sequence & 1, pc.PACKET_HEADER_CONNECT_SEQ_BITS)
        bs.write_int(self.last_send_seq, pc.PACKET_HEADER_SEQ_BITS)
        bs.write_int(self.last_seq_recvd, pc.PACKET_HEADER_ACK_START_BITS)
        bs.write_int(packet_type, pc.PACKET_HEADER_TYPE_BITS)
        bs.write_int(ack_byte_count, pc.PACKET_HEADER_ACK_BYTE_COUNT_BITS)
        bs.write_int(self.ack_mask, ack_byte_count * 8)
        if packet_type == pc.PACKET_TYPE_DATA:
            self.last_seq_recvd_at_send[self.last_send_seq & 0x1F] = self.last_seq_recvd

    def send_data_packet(self) -> None:
        """Build + send a DataPacket: header, rate block, then body hook."""
        if self.state != ConnState.CONNECTED:
            return
        if self.window_full():
            logger.debug("send window full; deferring data packet")
            return
        bs = BitStream()
        self._write_header(bs, pc.PACKET_TYPE_DATA)
        # Rate block (netConnection.cc:601-612): writeFlag(curRate.changed) [+
        # updateDelay(10)+packetSize(10)], then writeFlag(maxRate.changed) [+ ...].
        # Declared once (the changed flag clears after a send), mirroring the real
        # client's first packet (32/450). The server uses this to set its send
        # rate to us; without it datablocks never stream and we stall at Phase1.
        if bs.write_flag(self.rate_changed):
            bs.write_int(self.rate_update_delay, 10)
            bs.write_int(self.rate_packet_size, 10)
            self.rate_changed = False
        if bs.write_flag(self.max_rate_changed):
            bs.write_int(self.rate_update_delay, 10)
            bs.write_int(self.rate_packet_size, 10)
            self.max_rate_changed = False
        # Subclass body: GameConnection control header + events + ghosts.
        try:
            self.write_packet_body(bs)
        except Exception:
            logger.exception("write_packet_body hook raised")
        self.transport.sendto(bs.get_bytes(), self.server_addr)
        self._send_requested = False

    def send_ping_packet(self) -> None:
        if self.state != ConnState.CONNECTED:
            return
        bs = BitStream()
        self._write_header(bs, pc.PACKET_TYPE_PING)
        self.transport.sendto(bs.get_bytes(), self.server_addr)

    def send_ack_packet(self) -> None:
        if self.state != ConnState.CONNECTED:
            return
        bs = BitStream()
        self._write_header(bs, pc.PACKET_TYPE_ACK)
        self.transport.sendto(bs.get_bytes(), self.server_addr)

    def window_full(self) -> bool:
        """dnet.cc:233-236 -- stop sending DataPackets when 30 are unacked."""
        return (self.last_send_seq - self.highest_acked_seq) >= 30

    def _process_raw_packet(self, data: bytes) -> None:
        """processRawPacket (dnet.cc:103-231) for the connected layer."""
        if self.state != ConnState.CONNECTED:
            return
        bs = BitStream(data)
        bs.read_flag()  # gamePacketFlag (already known 1)
        pk_connect_seq_bit = bs.read_int(pc.PACKET_HEADER_CONNECT_SEQ_BITS)
        if pk_connect_seq_bit != (self.connect_sequence & 1):
            logger.debug("connect-seq parity mismatch; dropping packet")
            return
        pk_seq = bs.read_int(pc.PACKET_HEADER_SEQ_BITS)
        pk_highest_ack = bs.read_int(pc.PACKET_HEADER_ACK_START_BITS)
        pk_type = bs.read_int(pc.PACKET_HEADER_TYPE_BITS)
        if pk_type >= 3:
            logger.debug("invalid packet type %d; dropping", pk_type)
            return
        pk_ack_byte_count = bs.read_int(pc.PACKET_HEADER_ACK_BYTE_COUNT_BITS)
        if pk_ack_byte_count > pc.MAX_ACK_BYTE_COUNT:
            logger.debug("ackByteCount %d > max; dropping", pk_ack_byte_count)
            return
        pk_ack_mask = bs.read_int(8 * pk_ack_byte_count)

        # --- window reconstruction (dnet.cc:148-171) ---
        pk_seq |= self.last_seq_recvd & 0xFFFFFE00
        if pk_seq < self.last_seq_recvd:
            pk_seq += pc.SEQ_NUMBER_WRAP
        if pk_seq > self.last_seq_recvd + pc.SEQ_WINDOW_SLACK:
            logger.debug("seq %d out of window; dropping", pk_seq)
            return

        pk_highest_ack |= self.highest_acked_seq & 0xFFFFFE00
        if pk_highest_ack < self.highest_acked_seq:
            pk_highest_ack += pc.SEQ_NUMBER_WRAP
        if pk_highest_ack > self.last_send_seq:
            logger.debug("highestAck %d bogus; dropping", pk_highest_ack)
            return

        # --- ack/notify processing (dnet.cc:183-212) ---
        self.ack_mask = (self.ack_mask << (pk_seq - self.last_seq_recvd)) & 0xFFFFFFFF
        if pk_type == pc.PACKET_TYPE_DATA:
            self.ack_mask |= 1

        i = self.highest_acked_seq + 1
        while i <= pk_highest_ack:
            delivered = bool(pk_ack_mask & (1 << (pk_highest_ack - i)))
            if not self.connection_established and delivered:
                self.connection_established = True
            try:
                self.on_notify(i, delivered)
            except Exception:
                logger.exception("on_notify hook raised")
            if delivered:
                # dnet.cc:199 -- when the server confirms it received our packet
                # i, we learn it has seen our acks up to the lastSeqRecvd we
                # carried in that packet, so we can stop re-acking older received
                # packets. WITHOUT this, mLastRecvAckAck only crept forward via the
                # pk_seq-32 fallback below, the ackByteCount = (lastSeqRecvd -
                # lastRecvAckAck + 7) >> 3 grew, hit the 4-byte cap, and our ack
                # mask got truncated -- so the server never saw our acks for the
                # oldest in-flight reliable events and stalled the GhostAlways
                # stream at ~213/304. This single missing line was the stall.
                self.last_recv_ack_ack = self.last_seq_recvd_at_send[i & 0x1F]
            i += 1

        if pk_seq - self.last_recv_ack_ack > 32:
            self.last_recv_ack_ack = pk_seq - 32
        self.highest_acked_seq = pk_highest_ack

        # --- post-actions (dnet.cc:217-230) ---
        if pk_type == pc.PACKET_TYPE_PING:
            self.send_ack_packet()

        process_body = (self.last_seq_recvd != pk_seq) and (pk_type == pc.PACKET_TYPE_DATA)
        self.last_seq_recvd = pk_seq

        if process_body:
            self._handle_packet_body(bs)
            # Do NOT reflexively answer every received packet with its own
            # DataPacket. The real client does not: it sends exactly one
            # move-bearing DataPacket per ~32 ms game tick and lets that scheduled
            # packet carry its (batched) ack of everything received since the last
            # tick (capture bad_login.jsonl: c2s ack distances span 0..25 -- one
            # c2s packet acks many s2c packets via the ackMask; peak 32/s).
            #
            # WAVE-15 FIX: the old reflexive reply (a DATA packet per received
            # packet) plus a 100 ms keepalive burst us to 40-45/s during the
            # GhostAlways flood -- over the client's 32/s ceiling -- which trips the
            # AoT server's FloodProtection and stalls the stream at ~213/304. The
            # fixed-tick sender (_keepalive_loop, 32 ms) now carries the ack at the
            # client's natural rate. We only flush IMMEDIATELY here when there are
            # queued events to deliver (a NetStringEvent/RemoteCommandEvent/
            # ConnectionMessage the higher layer just enqueued) or an explicit send
            # was requested -- exactly the cases where latency matters and where
            # the real client also emits an event-bearing packet promptly.
            has_data = False
            try:
                has_data = self.has_pending_data()
            except Exception:
                logger.exception("has_pending_data hook raised")
            if (has_data or self._send_requested) and not self.window_full():
                self.send_data_packet()

    def _handle_packet_body(self, bs: BitStream) -> None:
        """Top of handlePacket (netConnection.cc:497-529): the rate block, then
        the subclass body via the installed hook.
        """
        if bs.read_flag():  # rateChangedFlag
            bs.read_int(10)  # updateDelay
            bs.read_int(10)  # packetSize
        if bs.read_flag():  # maxRateChangedFlag
            bs.read_int(10)
            bs.read_int(10)
        try:
            self.read_packet_body(bs)
        except Exception:
            logger.exception("read_packet_body hook raised")

    # ------------------------------------------------------------------ #
    # Keep-alive
    # ------------------------------------------------------------------ #

    async def _keepalive_loop(self) -> None:
        try:
            while self.state == ConnState.CONNECTED:
                await asyncio.sleep(self.keepalive_interval)
                if self.state != ConnState.CONNECTED:
                    break
                idle = time.monotonic() - self._last_recv_time
                if idle > pc.PING_TIMEOUT_MS / 1000.0 * (pc.DEFAULT_PING_RETRY_COUNT):
                    logger.warning("server silent for %.1fs; timing out", idle)
                    await self._shutdown(ConnState.TIMED_OUT)
                    return
                # Always send a DATA packet: it carries the GameConnection move
                # stream (>=1 idle Move with an advancing startMoveId) the AoT
                # server needs to keep advancing the connection, plus our ack of
                # the server and any queued events. The real client sends one
                # move-bearing packet per tick; a ping (no body, no moves) does
                # NOT advance the server's move state, so we never ping here.
                # If the send window is full we skip this tick (the server will
                # ack soon and free it).
                if not self.window_full():
                    self.send_data_packet()
        except asyncio.CancelledError:
            raise
