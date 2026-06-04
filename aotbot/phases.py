"""GameConnection packet body: control/move header, ghost section, mission phases.

This sits between netconn.py (packet/notify) and events.py (event section). It
implements the GameConnection-specific parts of a connected DataPacket body, in
the exact bit order the engine uses (mission-phases.md):

    [GameConnection control header]   (read flags 1..6, §3)
    [event section]                   (events.py)
    [ghost section]                   (§5 -- expect empty pre-spawn)

and the write-side mirror (control header + move section + event section + a
trailing ghost flag).

It also drives the MissionStartPhase1/2/3 ack flow ("skip lighting") to reach the
logged-out in-game state, registering the relevant ``clientCmd*`` handlers on the
EventManager.

Alignment philosophy (per the docs): there is no generic ghost/datablock skip.
During the connect->login window we *expect* the control flags and the ghost flag
to be zero (no control object, no ghosts scoped to a logged-out client). If they
are ever non-zero we cannot stay aligned, so we raise :class:`AlignmentError` --
the connection layer treats that packet's body as undecodable rather than
silently desyncing.
"""

from __future__ import annotations

import enum
import logging
import time
from typing import Callable, Optional

from . import protocol_constants as pc
from .bitstream import BitStream
from .events import EventManager, EventDecodeError

logger = logging.getLogger("aotbot.phases")

# GameConnection move-section constants (gameConnection.h:47-50).
MOVE_COUNT_BITS = 5
MAX_MOVE_COUNT = 30      # moveWritePacket caps count at MaxMoveCount (=30).
MAX_TRIGGER_KEYS = 6     # Move::pack: freeLook flag + 6 trigger flags.

# How many idle Moves the bot emits per outgoing DataPacket. The REAL client
# sends 1..3 moves in EVERY packet (modal 3; never 0 -- verified across all
# 1077 c2s data packets in tools/captures/real_login.jsonl). The AoT server
# gates serverCmd* processing on having received a move/control stream, so a
# moveCount=0 header (what the bot sent before) is acked but its events are
# dropped. We emit a small constant idle stream to match.
DEFAULT_MOVES_PER_PACKET = 3

# An idle/null Move packs to exactly 28 bits: 3 zero rotation-present flags,
# then px=py=pz=16 (Move::clamp maps 0.0 -> 16, the 6-bit center), then a
# freeLook flag and 6 trigger flags, all 0 (gameConnectionMoves.cc Move::pack;
# AoT Move::unpack @ VA 0x45b000 confirmed byte-identical to stock TGE, 6
# trigger keys). This matches MOVE 0 of every captured client packet exactly.
PACKED_MOVE_CENTER = 16  # clampRangeClamp(0.0) == 16

# ConnectionMessage values multiplexed in the event stream (3-bit field).
# GhostStates enum (netConnection.h:722-731) + GameConnection's two extra
# messages (gameConnection.h:110-111). These are the 3-bit "message" of a
# ConnectionMessageEvent (classId 0):
#   GhostAlwaysDone=0, ReadyForNormalGhosts=1, EndGhosting=2,
#   GhostAlwaysStarting=3, SendNextDownloadRequest=4, FileDownloadSizeMessage=5,
#   DataBlocksDone(=NumConnectionMessages)=6, DataBlocksDownloadDone=7.
# Server->client sends DataBlocksDone(6) when all datablocks are sent; the client
# replies DataBlocksDownloadDone(7) once its datablock list is drained
# (gameConnection.cc:1108-1125). GhostAlwaysStarting(3) precedes the ghost-always
# objects and flips mGhosting on.
CONNECTION_MSG_BITS = 3
GHOST_COUNT_BITS = pc.GHOST_ID_BIT_SIZE + 1  # 15 (AoT GhostIdBitSize=14 +1)

MSG_GHOST_ALWAYS_DONE = 0
MSG_READY_FOR_NORMAL_GHOSTS = 1
MSG_END_GHOSTING = 2
MSG_GHOST_ALWAYS_STARTING = 3
MSG_SEND_NEXT_DOWNLOAD_REQUEST = 4
MSG_FILE_DOWNLOAD_SIZE = 5
MSG_DATABLOCKS_DONE = 6
MSG_DATABLOCKS_DOWNLOAD_DONE = 7


# readCompressedPoint bit counts (bitStream.cc:457 gBitCounts; AoT table @
# VA 0x63c0f8 = {16, 18, 20, 32}). Used by the control header's compression
# point and by Sim3DAudioEvent's position.
COMPRESSED_POINT_BIT_COUNTS = (16, 18, 20, 32)


def _read_compressed_point(bs: BitStream) -> None:
    """BitStream::readCompressedPoint (bitStream.cc:498, AoT @ VA 0x421a70).

    Consume a quantised Point3F: ``readInt(2)`` type; type 3 -> 3 x F32 (full
    precision); types 0/1/2 -> 3 x ``readSignedInt(gBitCounts[type])`` (a sign
    flag + ``bits-1`` magnitude). We only need to advance the cursor, not the
    value (we never use the position).
    """
    t = bs.read_int(2)
    if t == 3:
        bs.read_bytes(12)  # 3 x F32
    else:
        n = COMPRESSED_POINT_BIT_COUNTS[t]
        for _ in range(3):
            bs.read_signed_int(n)  # readFlag(sign) + readInt(n-1)


class AlignmentError(Exception):
    """Raised when a packet body contains content we can't decode (control
    object / ghost / unknown event) -- continuing would desync the bitstream.
    """


class MissionState(enum.Enum):
    LOADING = "loading"
    PHASE1_DONE = "phase1_done"
    INGAME_LOGGEDOUT = "ingame_loggedout"


class GameConnectionPhases:
    """Owns the GameConnection packet body + mission-phase state machine."""

    def __init__(
        self,
        events: EventManager,
        *,
        skip_lighting: bool = True,
        moves_per_packet: int = DEFAULT_MOVES_PER_PACKET,
        track_objects: bool = False,
    ) -> None:
        self.events = events
        self.skip_lighting = skip_lighting

        # Live-entity telemetry. When OFF (default) we leave the ongoing ghost
        # SECTION untouched (it is last in each packet, after the control header +
        # event section, so stopping there keeps chat/login working) and keep no
        # registry -> minimal CPU. When ON we decode the ghost section fully and
        # maintain the scoped-object registry.
        self.track_objects = track_objects
        from .telemetry import ObjectRegistry
        self.registry = ObjectRegistry() if track_objects else None
        # Wire the event-side registry population (GhostAlwaysObjectEvent initial
        # state + SimDataBlockEvent shape names) when tracking is on.
        self.events.object_registry = self.registry if track_objects else None
        self.events.track_objects = track_objects
        # Install the connection's receive-string-table resolver so the ghost
        # decoders can turn a Player's name tag (ShapeBase mShapeNameTag, a 5-bit
        # NetStringTable slot) into the real username. The server teaches these
        # slots via NetStringEvent into events.recv_table.
        if track_objects:
            from . import telemetry as _tel
            _tel.set_string_resolver(self.events.recv_table.lookup)

        self.state = MissionState.LOADING
        self.last_move_ack = 0          # server's ack of our move stream
        # Outgoing move stream: the real client sends >=1 idle Move in every
        # data packet with a monotonically advancing startMoveId. We mirror that
        # (the server requires a move stream before it acts on our events).
        self.moves_per_packet = max(1, int(moves_per_packet))
        self._next_move_id = 0          # local running move index (startMoveId)
        self.datablocks_done = False
        self._phase1_seq = 0
        self._phase_acked: set[int] = set()
        # mGhosting (server-driven): until the server activates ghosting, the
        # ghost section is 0 bits (see _read_ghost_section).
        self.ghosting_active = False
        self._ghosting_sequence = 0     # mGhostingSequence (echoed in acks)
        # True once a real GhostAlwaysStarting arrives; gates ReadyForNormalGhosts
        # so we don't reply to the spurious pre-Starting GhostAlwaysDone (the real
        # client ignores it). See _on_connection_message.
        self._seen_ghost_always_starting = False
        # GhostAlways stream completion tracking. The AoT server (capture-confirmed
        # LIVE) sends GhostAlwaysStarting then streams the scoped ghost-always
        # objects as GhostAlwaysObjectEvents, but it does NOT send a post-stream
        # GhostAlwaysDone connection message to a headless client -- the only
        # GhostAlwaysDone(0) it sends arrives BEFORE GhostAlwaysStarting (a stale
        # one). So the GhostAlwaysDone-gated ReadyForNormalGhosts reply (the stock
        # netGhost.cc:936 path) never fires and the load stalls right after the
        # ghost-always burst ("213-ghost stall"). The real client breaks the stall
        # by registering each ghost-always object + completing the file-download
        # handshake, which culminates in ReadyForNormalGhosts. A headless bot has
        # no objects/files to register, so instead we detect that the ghost-always
        # BURST has finished (no new ghost-always object for a short idle window
        # after GhostAlwaysStarting) and send ReadyForNormalGhosts ourselves --
        # which unblocks the server (LIVE-confirmed: it then switches to normal
        # ghosting and completes Phase3 -> MissionStart -> the login response).
        self._ghost_always_count = 0
        self._last_ghost_always_time = 0.0
        self._ready_for_normal_sent = False
        # How long the ghost-always burst must be idle before we conclude it is
        # complete and send ReadyForNormalGhosts (seconds). The burst arrives at
        # ~30 events/packet-tick; 0.4s of silence reliably means it is done.
        self.ghost_always_idle_timeout = 0.4
        # Ghost id -> NetObject classId, so the ghost loop's new-vs-existing
        # branch matches the engine (a class id is only on the wire for NEW ids).
        self._ghost_classes: dict[int, int] = {}
        # The ghost id the server most recently scoped as our CONTROL object (the
        # bot's own Player once spawned). Set in the control-header control-object
        # branch; used to flag the registry record.
        self._control_ghost_id: Optional[int] = None

        # Hook fired when we reach the logged-out in-game state (client.py uses
        # this to trigger login).
        self.on_ingame: Callable[[], None] = lambda: None
        # Hook fired right after we ack Phase2 (the real AoT client auto-logins
        # early off onPhase1Complete, before MissionStart -- the server accepts
        # the login at this point and answers with clientCmdLoginSuccess /
        # clientCmdWarningBox once the full load completes). client.py uses this
        # for the eager login that surfaces the login result.
        self.on_phase2_acked: Callable[[], None] = lambda: None
        self._eager_login_done = False

        self._register_handlers()

    # ------------------------------------------------------------------ #
    # clientCmd* handlers driving the mission phases
    # ------------------------------------------------------------------ #

    def _register_handlers(self) -> None:
        ev = self.events
        ev.on_client_cmd("MissionStartPhase1", self._on_phase1)
        ev.on_client_cmd("MissionStartPhase2", self._on_phase2)
        ev.on_client_cmd("MissionStartPhase3", self._on_phase3)
        ev.on_client_cmd("MissionStart", self._on_mission_start)
        ev.on_client_cmd("StartLogin", self._on_start_login)
        # Drive the datablock/ghost connection-message handshake.
        ev.on_connection_message = self._on_connection_message
        # Record ghost id -> classId scoped by a GhostAlwaysObjectEvent so the
        # ghost section treats those ids as existing (no classId on the wire).
        ev.on_ghost_scoped = self._on_ghost_scoped

    def _on_ghost_scoped(self, ghost_id: int, class_id: int) -> None:
        self._ghost_classes[ghost_id] = class_id
        # Track the ghost-always burst so maybe_send_ready_for_normal_ghosts() can
        # detect when it has finished (idle for ghost_always_idle_timeout).
        if self.ghosting_active and not self._ready_for_normal_sent:
            self._ghost_always_count += 1
            self._last_ghost_always_time = time.monotonic()

    def maybe_send_ready_for_normal_ghosts(self) -> None:
        """Send ReadyForNormalGhosts once the ghost-always burst has gone idle.

        Called periodically by the connection's send tick. After
        GhostAlwaysStarting the server streams the scoped ghost-always objects;
        when no new GhostAlwaysObjectEvent has arrived for
        ``ghost_always_idle_timeout`` seconds we treat the burst as complete and
        reply ReadyForNormalGhosts (echoing the GhostAlwaysStarting sequence),
        exactly the message the stock client sends from loadNextGhostAlwaysObject
        once its save list drains. This unblocks the AoT server's reliable-event
        window (LIVE-confirmed) so it proceeds to Phase3 -> MissionStart -> the
        login response (clientCmdLoginSuccess / clientCmdWarningBox). Idempotent.
        """
        if self._ready_for_normal_sent:
            return
        if not (self.ghosting_active and self._seen_ghost_always_starting):
            return
        if self._ghost_always_count == 0:
            return
        if (time.monotonic() - self._last_ghost_always_time) < self.ghost_always_idle_timeout:
            return
        self._ready_for_normal_sent = True
        logger.info(
            "GhostAlways burst idle after %d objects -> ReadyForNormalGhosts(seq=%d)",
            self._ghost_always_count, self._ghosting_sequence,
        )
        self._send_connection_message(
            MSG_READY_FOR_NORMAL_GHOSTS, self._ghosting_sequence, 0
        )

    def _on_connection_message(self, message: int, sequence: int, ghost_count: int) -> None:
        """ConnectionMessageEvent handler (gameConnection.cc:1205-1226).

        * DataBlocksDone(6): the server finished sending datablocks for this
          mission sequence; the client must reply DataBlocksDownloadDone(7) once
          its datablock list is drained. We have no datablock list (headless), so
          we reply immediately to advance the server toward Phase2.
        * GhostAlwaysStarting(3): the server is about to send ghost-always
          objects -> mGhosting turns on; the ghost section becomes non-empty.
        * GhostAlwaysDone(0): the server has SENT all ghost-always objects. The
          client must register them and then reply ReadyForNormalGhosts(1) with
          the SAME ghostingSequence (netGhost.cc:931-936 loadNextGhostAlwaysObject
          -> sendConnectionMessage(ReadyForNormalGhosts, mGhostingSequence)).
          Without this reply the server NEVER advances past the GhostAlways stream
          for the full-load path (it just keeps the connection ghosting), so an
          invalid account never reaches Phase3 -> MissionStart -> the
          clientCmdWarningBox. (A valid login short-circuits via clientCmdLoginSuccess
          which is not gated on this, which is why valid login worked without it.)
          We have no files to download, so we reply immediately.
        """
        if message == MSG_DATABLOCKS_DONE:
            logger.info("ConnectionMessage DataBlocksDone(seq=%d) -> DataBlocksDownloadDone", sequence)
            self.datablocks_done = True
            self._send_connection_message(MSG_DATABLOCKS_DOWNLOAD_DONE, sequence, 0)
        elif message == MSG_GHOST_ALWAYS_STARTING:
            logger.info("ConnectionMessage GhostAlwaysStarting(ghostCount=%d) -> ghosting on", ghost_count)
            self.ghosting_active = True
            self._ghosting_sequence = sequence
            self._seen_ghost_always_starting = True
        elif message == MSG_GHOST_ALWAYS_DONE:
            # All ghost-always objects for the CURRENT ghosting sequence are now
            # on the client. Reply ReadyForNormalGhosts echoing mGhostingSequence
            # (the sequence the server sent with GhostAlwaysStarting -- NOT this
            # Done message's own sequence field, which the capture shows the real
            # client ignores). Only do this while ghosting is active; the server
            # then completes the load (Phase3 -> MissionStart -> login response).
            # CAPTURE-CONFIRMED: the real client sends exactly one c2s
            # ReadyForNormalGhosts(seq=1) after GhostAlwaysStarting(seq=1).
            # CAPTURE-FAITHFUL: only reply to a GhostAlwaysDone that follows a real
            # GhostAlwaysStarting (which carries the live mGhostingSequence). The
            # server emits a spurious early GhostAlwaysDone BEFORE GhostAlwaysStarting
            # (from a prior/initial ghosting state); the real client (real_login3.jsonl
            # c2s) does NOT respond to it -- it sends exactly ONE ReadyForNormalGhosts,
            # at event-seq ~25, echoing GhostAlwaysStarting's seq. Replying to the
            # early one (with our then-zero _ghosting_sequence) is a divergence the
            # server's ghost state machine ignores (sequence mismatch, netGhost.cc:747)
            # but it puts a stray event the genuine client never sends on the wire.
            if (
                self.ghosting_active
                and self._seen_ghost_always_starting
                and not self._ready_for_normal_sent
            ):
                # A genuine post-stream GhostAlwaysDone (if the server ever sends
                # one). The idle-detection path usually fires first for a headless
                # client; guard both with _ready_for_normal_sent so we send exactly
                # one ReadyForNormalGhosts.
                self._ready_for_normal_sent = True
                logger.info(
                    "ConnectionMessage GhostAlwaysDone -> ReadyForNormalGhosts(seq=%d)",
                    self._ghosting_sequence,
                )
                self._send_connection_message(
                    MSG_READY_FOR_NORMAL_GHOSTS, self._ghosting_sequence, 0
                )
            else:
                logger.debug(
                    "ConnectionMessage GhostAlwaysDone (pre-GhostAlwaysStarting) -- "
                    "ignored, like the real client"
                )
        elif message == MSG_END_GHOSTING:
            self.ghosting_active = False

    def _send_connection_message(self, message: int, sequence: int, ghost_count: int) -> None:
        """Queue a ConnectionMessageEvent (classId 0) back to the server.

        Pack mirror of the unpack (EXE @ VA 0x5464a0): write(U32 sequence),
        writeInt(message, 3), writeInt(ghostCount, GhostIdBitSize+1=15).
        """
        def writer(bs: BitStream, _seq=sequence, _msg=message, _gc=ghost_count) -> None:
            bs.write_int(_seq, 32)
            bs.write_int(_msg, CONNECTION_MSG_BITS)
            bs.write_int(_gc, GHOST_COUNT_BITS)

        self.events._enqueue(
            pc.EVENT_CLASS_IDS["ConnectionMessageEvent"],
            writer,
            description=f"ConnectionMessage(msg={message}, seq={sequence})",
        )

    @staticmethod
    def _seq_of(args: list[str]) -> int:
        if args:
            try:
                return int(args[0])
            except (ValueError, TypeError):
                return 1
        return 1

    def _on_phase1(self, args, _evt) -> None:
        seq = self._seq_of(args)
        logger.info("clientCmdMissionStartPhase1(seq=%s)", seq)
        self._phase1_seq = seq
        # Phase 1 ack: the stock client sends this from clientCmdMissionStartPhase1
        # (missionDownload.cs:8). It triggers the server's transmitDataBlocks(seq).
        # The skip-lighting trick (Phase2Ack/Phase3Ack) belongs in the PHASE 2
        # handler, NOT here: the AoT bot fakes 2 & 3 from onPhase1Complete, which
        # the engine invokes from clientCmdMissionStartPhase2 (missionDownload.cs:18),
        # i.e. only once the SERVER has sent Phase2. Sending the 2/3 acks before
        # Phase2 is premature and out of sequence.
        self.events.command_to_server("MissionStartPhase1Ack", seq)
        self.state = MissionState.PHASE1_DONE

    def _on_phase2(self, args, _evt) -> None:
        seq = self._seq_of(args)
        logger.info("clientCmdMissionStartPhase2(seq=%s)", seq)
        if 2 not in self._phase_acked:
            self.events.command_to_server("MissionStartPhase2Ack", seq)
            self._phase_acked.add(2)
        # Do NOT ack Phase3 here. The real client sends MissionStartPhase3Ack only
        # AFTER it has replied ReadyForNormalGhosts (capture bad_login.jsonl: c2s
        # Phase2Ack seq91, ReadyForNormalGhosts seq149, Phase3Ack seq167). With the
        # ghost-always burst now correctly completed by
        # maybe_send_ready_for_normal_ghosts(), the server actually SENDS
        # clientCmdMissionStartPhase3 (LIVE-confirmed) and we ack the real one in
        # _on_phase3 -- so the premature eager Phase3 ack (a Wave-12..14 crutch for
        # the then-unsolved ghost stall) is no longer needed and is removed to
        # match the real client's ordering.
        # Eager login (mirrors the real AoT client's onPhase1Complete auto-login):
        # send login now, before MissionStart. The server accepts it and delivers
        # the result (clientCmdLoginSuccess for a valid account, clientCmdWarningBox
        # "Wrong Password!" / "Character does not exist!" for a bad one) once the
        # full mission load completes.
        if not self._eager_login_done:
            self._eager_login_done = True
            try:
                self.on_phase2_acked()
            except Exception:
                logger.exception("on_phase2_acked hook raised")

    def _on_phase3(self, args, _evt) -> None:
        seq = self._seq_of(args)
        logger.info("clientCmdMissionStartPhase3(seq=%s)", seq)
        # We can't compute lighting (headless, no renderer); just ack so the
        # server advances to clientCmdMissionStart. This is the genuine Phase3
        # ack the server is waiting for (it only sends Phase3 after the ghost
        # stream completes, so by here the full load is essentially done).
        if 3 not in self._phase_acked:
            self.events.command_to_server("MissionStartPhase3Ack", seq)
            self._phase_acked.add(3)

    def _on_mission_start(self, args, _evt) -> None:
        logger.info("clientCmdMissionStart -> in-game, logged out")
        self._enter_ingame()

    def _on_start_login(self, args, _evt) -> None:
        logger.info("clientCmdStartLogin -> in-game, logged out")
        self._enter_ingame()

    def _enter_ingame(self) -> None:
        if self.state != MissionState.INGAME_LOGGEDOUT:
            self.state = MissionState.INGAME_LOGGEDOUT
            try:
                self.on_ingame()
            except Exception:
                logger.exception("on_ingame hook raised")

    # ------------------------------------------------------------------ #
    # Telemetry queries
    # ------------------------------------------------------------------ #

    def list_objects(self, include_removed: bool = False) -> list:
        """Return all currently-scoped objects as dicts (empty if tracking OFF)."""
        if self.registry is None:
            return []
        return self.registry.list_objects(include_removed=include_removed)

    def get_object(self, ghost_id: int):
        """Return one scoped object's record dict, or None."""
        if self.registry is None:
            return None
        rec = self.registry.get(ghost_id)
        return rec.to_dict() if rec is not None else None

    # ------------------------------------------------------------------ #
    # Packet body -- READ (server -> client)
    # ------------------------------------------------------------------ #

    def read_packet_body(self, bs: BitStream) -> None:
        """Consume the GameConnection control header, event section, ghost
        section, in order. Raises AlignmentError on undecodable content.

        GameConnection::readPacket (@ VA 0x4593c0) installs a 256-byte per-packet
        ``stringBuffer`` (``setStringBuffer`` @ 0x4593df) before reading the
        body, so EVERY ``readString`` in the event section uses the dedup-prefix
        path (a leading ``useStringBuffer`` flag + 8-bit offset + Huffman tail).
        We must mirror that or string events (NetStringEvent, RemoteCommandEvent
        CString args) desync. The buffer is per-packet (reset each call) and
        cleared afterward (``setStringBuffer(NULL)`` @ 0x459858).
        """
        bs.set_string_buffer(bytearray(256))
        try:
            self._read_control_header(bs)
            try:
                self.events.read_events(bs)
            except EventDecodeError as exc:
                raise AlignmentError(str(exc)) from exc
            # Install the point-compression REFERENCE for this packet's ghost
            # section: BitStream::readCompressedPoint (0x421a70) dequantises
            # types 0/1/2 as ``raw * 0.01 + reference``, where the engine's
            # reference ([this+0x28..0x30]) is the receiving connection's
            # CONTROL-OBJECT world position (winedbg-confirmed). A headless bot
            # recovers it from its control object's decoded type-3 absolute pose
            # (the previous packets' Player::unpackUpdate). Without it, parked
            # remote players (Jeff Bezos / Horse / Sword Giver) -- which only ever
            # send compressed pose updates -- decoded with ``position: null``.
            if self.track_objects and self.registry is not None:
                cid = self.registry.control_ghost_id
                ctrl = self.registry.get(cid) if cid is not None else None
                from . import telemetry
                telemetry.set_compression_point(
                    ctrl.position if ctrl is not None else None
                )
            self._read_ghost_section(bs)
        finally:
            bs.set_string_buffer(None)
            from . import telemetry
            telemetry.set_compression_point(None)

    def _read_control_header(self, bs: BitStream) -> None:
        """GameConnection::readPacket control header, **AoT fork**, client side.

        EXE-confirmed by disassembling GameConnection::readPacket @ VA 0x4593c0
        (the ``isConnectionToServer()`` / ``[edi+0xf0]&1`` true branch) and the
        server's mirror in writePacket @ VA 0x458849. AoT's layout differs from
        stock TGE 1.4 in two ways:

        * There are **4** flags after the move ack, NOT 5 -- AoT dropped the
          stock ``firstPerson`` flag entirely (damage, control, camera, fov).
        * The camera-object flag reads **only** the ghost id (``readInt(14)``);
          it does NOT call ``readPacketData`` (stock did). Ghost id width is 14
          bits (``GHOST_ID_BIT_SIZE``), not stock's 12.

        Pre-login the control/camera flags are zero (no control object scoped to
        a logged-out client), so this reduces to ``readInt(32)`` + 4 zero flags.
        """
        self.last_move_ack = bs.read_int(32)  # mLastMoveAck (readInt(32) @ 0x4593f8)

        # damage/whiteout flag (inline readFlag @ 0x4594a1).
        if bs.read_flag():
            if bs.read_flag():
                bs.read_float(7)  # damageFlash
            if bs.read_flag():
                bs.read_float(7)  # whiteOut

        # control-object flag (inline readFlag @ 0x459516).
        if bs.read_flag():
            if bs.read_flag():  # inner update flag (readFlag @ 0x459546)
                # control-object update: ghost id (14 bits) + readPacketData.
                # (@ 0x459569 push 0xe; readInt -> ghost id; @ 0x459593
                # call [edx+0xec] = the RESOLVED control object's readPacketData,
                # dispatched on the object's ACTUAL class -- NOT always ShapeBase.
                # Camera overrides it (0x44e680, 36+ bytes); Player overrides it
                # too. We look up the ghost's class (tracked in _ghost_classes via
                # the ghost section / GhostAlwaysObjectEvent) and call the right
                # readPacketData. Once a Camera/Player is the control object,
                # using the 8-byte ShapeBase form desynced the whole rest of the
                # packet (the ghost section read garbage class ids).
                from . import ghosts as _gh
                from . import telemetry
                control_ghost_id = bs.read_int(pc.GHOST_ID_BIT_SIZE)
                self._control_ghost_id = control_ghost_id
                class_id = self._ghost_classes.get(control_ghost_id)
                sink = telemetry.DecodeSink() if self.track_objects else None
                if sink is not None:
                    telemetry.set_sink(sink)
                try:
                    _gh.read_packet_data(bs, class_id)
                except _gh.GhostDecodeError as exc:
                    raise AlignmentError(
                        f"control-object readPacketData not ported: {exc}"
                    ) from exc
                finally:
                    if sink is not None:
                        telemetry.set_sink(None)
                if self.registry is not None and sink is not None:
                    name = (
                        _gh.OBJECT_CLASS_NAMES[class_id]
                        if class_id is not None
                        and 0 <= class_id < len(_gh.OBJECT_CLASS_NAMES)
                        else ""
                    )
                    self.registry.update_from_sink(
                        control_ghost_id, name, sink, is_control=True
                    )
            else:
                # No control object scoped to us: the server sends the camera
                # position as a full Point3F -- 3 raw 4-byte reads (slot 4 @
                # 0x4595c0/0x4595d6/0x4595ec) then a memcpy into the connection
                # (0x421170, NOT a bitstream read). This is NOT a compressed
                # point.
                bs.read_bytes(12)  # camera Point3F (3 x F32)

        # camera-object flag (inline readFlag @ 0x45966c). AoT reads only the
        # ghost id here (no readPacketData), so once a camera object IS scoped
        # the bits stay aligned -- but pre-login this flag is 0.
        if bs.read_flag():
            bs.read_int(pc.GHOST_ID_BIT_SIZE)  # readInt(14) @ 0x45969e

        # fov flag (inline readFlag @ 0x4596b5).
        if bs.read_flag():
            bs.read_int(8)  # fov

    def _read_ghost_section(self, bs: BitStream) -> None:
        """ghostReadPacket (AoT @ VA 0x549890).

        AoT gates the WHOLE section on ``mGhosting`` (``[edi+0x1c8]``): if the
        connection is not yet ghosting (``je 0x549ad0``), it reads **zero** bits
        and returns immediately -- there is no leading ``0`` flag. mGhosting is
        only set once the server activates ghosting (post-scope).

        When ghosting IS active the section is (loop disassembled @ 0x5498b7):

          1. readFlag presence; if 0 -> the section is empty (no ghost updates).
          2. ``idSize = readInt(4) + 3`` (the per-packet ghost-id bit width).
          3. per-ghost loop:
               * readFlag; if 0 -> end of loop.
               * ``ghostId = readInt(idSize)``
               * readFlag removeFlag:
                   - 1 -> the ghost is being removed (no payload).
                   - 0 -> an update follows:
                       * if this id is NEW (not yet in our ghost table):
                           ``classId = readClassId(NetClassTypeObject)`` (6 bits),
                           create the object, then ``obj->unpackUpdate(stream)``.
                       * else (existing): ``obj->unpackUpdate(stream)``.

        ``unpackUpdate`` (NetObject vtable slot 0x4c) is a per-class, length-less
        bit-packed payload (ghosts.py dispatches it). We track which ghost ids we
        have seen so the new-vs-existing branch matches the engine. If we hit a
        class whose ``unpackUpdate`` is not ported we raise AlignmentError
        carrying the class so the caller logs exactly what blocks.
        """
        if bs.error:
            return
        if not self.ghosting_active:
            return  # mGhosting == 0 -> ghostReadPacket reads nothing
        # The ghost section MUST be decoded whenever ghosting is active, even with
        # tracking OFF: it populates _ghost_classes (which the control-object
        # readPacketData in the control header and the ReadyForNormalGhosts
        # idle-detection both depend on) and keeps the bitstream aligned. Skipping
        # it (the old track_objects early-return) desynced the control header in
        # later packets -> garbage events -> login never completed. The on/off flag
        # only gates BUILDING the registry (the DecodeSink + update_from_sink): when
        # OFF we still decode for alignment but discard the values (the cheap part).
        from . import ghosts as _gh
        from . import telemetry

        if not bs.read_flag():  # presence (0x5498b7 inline readFlag)
            return  # no ghost updates this packet
        id_size = bs.read_int(pc.GHOST_INDEX_BIT_SIZE) + 3  # readInt(4)+3 (0x5498e0)

        for _ in range(1 << 14):  # bounded; real loop ends on the presence flag
            if bs.error:
                return
            if not bs.read_flag():  # this-ghost present? (0x549900 inline readFlag)
                return  # end of the ghost loop
            ghost_id = bs.read_int(id_size)  # readInt(idSize) (0x549932)
            if bs.read_flag():  # remove flag (0x5499f5 inline readFlag)
                # Ghost removal: no payload (engine frees the ghost).
                self._ghost_classes.pop(ghost_id, None)
                if self.registry is not None:
                    self.registry.remove(ghost_id)
                continue
            is_new = ghost_id not in self._ghost_classes
            if is_new:
                class_id = bs.read_int(pc.NET_CLASS_BITS_OBJECT)  # readClassId (0x54996c)
                self._ghost_classes[ghost_id] = class_id
            else:
                class_id = self._ghost_classes[ghost_id]
            sink = telemetry.DecodeSink() if self.track_objects else None
            if sink is not None:
                telemetry.set_sink(sink)
            try:
                _gh.unpack_update(bs, class_id, is_new)
            except _gh.GhostDecodeError as exc:
                raise AlignmentError(
                    f"ghost unpackUpdate not ported: {exc}"
                ) from exc
            finally:
                if sink is not None:
                    telemetry.set_sink(None)
            if self.registry is not None and sink is not None:
                name = (
                    _gh.OBJECT_CLASS_NAMES[class_id]
                    if 0 <= class_id < len(_gh.OBJECT_CLASS_NAMES)
                    else f"<{class_id}>"
                )
                self.registry.update_from_sink(
                    ghost_id, name, sink, is_new=is_new,
                    is_control=(ghost_id == self._control_ghost_id),
                )

    # ------------------------------------------------------------------ #
    # Packet body -- WRITE (client -> server)
    # ------------------------------------------------------------------ #

    def write_packet_body(self, bs: BitStream, send_seq: int) -> None:
        """Mirror header: control header (cameraPos flag + checksum), empty move
        list, event section, trailing ghost flag.

        Like the read side, GameConnection::writePacket installs a per-packet
        ``stringBuffer`` so our outgoing ``writeString``s use the dedup path the
        server expects. Install + clear it around the body.
        """
        bs.set_string_buffer(bytearray(256))
        try:
            self._write_control_header(bs)
            self.events.write_events(bs, send_seq)
            self._write_ghost_section(bs)
        finally:
            bs.set_string_buffer(None)

    def _write_control_header(self, bs: BitStream) -> None:
        """GameConnection write-side control header, **AoT fork**, client side.

        EXE-confirmed by disassembling GameConnection::writePacket @ VA 0x458710
        (the ``isConnectionToServer()`` / ``[esi+0xf0]&1`` TRUE branch @
        0x458747). AoT writes, in order:

        * cameraPos flag (``writeFlag`` @ 0x458762),
        * a 32-bit control-object checksum (``write(U32)`` @ 0x4587ef),
        * ``moveWritePacket`` @ 0x45b4b0 (startMoveId ``writeInt(32)`` + count
          ``writeInt(5)``, count capped at 30), then
        * exactly **ONE** trailing flag -- the fov-present flag
          (``writeFlag`` @ 0x45880b). AoT dropped the stock ``firstPerson``
          flag here too, so this side has ONE flag, not two.

        The earlier two-flag (firstPerson + fov) write put ONE EXTRA bit on the
        wire, which desynced the server's read of our packet body
        (moveReadPacket @ 0x45b5f0 then a single flag @ 0x45977e), so the server
        silently dropped every event we sent (acks, login, chat) -- the root
        cause of "server never advances past phase 1 / never answers login".

        We write: no camera/control object, an idle Move stream (count >= 1 with
        an advancing startMoveId -- the server gates serverCmd* on a non-empty
        move stream, so count=0 made every event we sent get silently dropped),
        fov flag 0.
        """
        bs.write_flag(True)   # mCameraPos == 0 (we have no camera/control object)
        bs.write_int(0, 32)   # control-object checksum (mLastControlObjectChecksum)
        self._write_moves(bs)
        bs.write_flag(False)  # fov flag (single trailing flag; no firstPerson)

    def _write_moves(self, bs: BitStream) -> None:
        """moveWritePacket (AoT @ VA 0x45b4b0): writeInt(startMoveId,32) +
        writeInt(count, MoveCountBits=5) (count capped at MaxMoveCount=30), then
        ``count`` x Move::pack.

        CAPTURE-FAITHFUL move stream. The real client's ``moveWritePacket`` writes
        ``start = mLastMoveAck`` (the first move the SERVER has NOT yet acked) and
        re-packs every still-unacked buffered move (capture: startMoveId tracks the
        server's move-ack, NOT a blind +count counter; e.g. start 0,0,2,3,5,...).
        The server (gameConnectionMoves.cc moveReadPacket) does
        ``skip = mLastMoveAck - start`` and only appends the moves past what it
        already has, so re-sending the unacked tail every packet is exactly what it
        expects. A blind ever-advancing startMoveId (our old behaviour) makes the
        server's ``skip`` go negative and SNAP ``mLastMoveAck = start`` every
        packet, so the server's move pipeline never settles -- a connection-state
        divergence from the genuine client during the load/ghost window.

        We generate one idle Move per packet (``moves_per_packet`` of them) and
        keep them buffered until the server's echoed ``mLastMoveAck`` (read in
        ``_read_control_header`` into ``self.last_move_ack``) confirms them. Then we
        write ``start = last_move_ack`` and re-pack ``[last_move_ack .. generated)``
        (capped at MaxMoveCount), matching the client's wire trace.
        """
        # Generate this tick's idle move(s).
        self._next_move_id = (self._next_move_id + self.moves_per_packet) & 0xFFFFFFFF
        # Drop moves the server has acked; the buffered window is
        # [last_move_ack .. _next_move_id).  start = first unacked move id.
        start = self.last_move_ack & 0xFFFFFFFF
        pending = (self._next_move_id - start) & 0xFFFFFFFF
        if pending > MAX_MOVE_COUNT:
            # The server is far behind acking (no control object consumes our
            # moves while logged out); cap to MaxMoveCount and advance start so we
            # never claim to resend more than 30, exactly like the real client's
            # moveWritePacket (count = min(size, MaxMoveCount); start += offset).
            start = (self._next_move_id - MAX_MOVE_COUNT) & 0xFFFFFFFF
            pending = MAX_MOVE_COUNT
        count = pending if pending > 0 else self.moves_per_packet
        count = min(count, MAX_MOVE_COUNT)
        bs.write_int(start, 32)
        bs.write_int(count, MOVE_COUNT_BITS)
        for _ in range(count):
            self._write_idle_move(bs)

    @staticmethod
    def _write_idle_move(bs: BitStream) -> None:
        """Move::pack of a null/idle Move (no rotation, no input).

        3 zero rotation-present flags (so no 16-bit angle follows), then the
        packed position px=py=pz=16 (Move::clamp maps 0.0 -> 16), a freeLook
        flag (0) and 6 trigger flags (0): 28 bits total, byte-identical to MOVE 0
        of every captured client packet.
        """
        bs.write_flag(False)  # pyaw == 0
        bs.write_flag(False)  # ppitch == 0
        bs.write_flag(False)  # proll == 0
        bs.write_int(PACKED_MOVE_CENTER, 6)  # px
        bs.write_int(PACKED_MOVE_CENTER, 6)  # py
        bs.write_int(PACKED_MOVE_CENTER, 6)  # pz
        bs.write_flag(False)  # freeLook
        for _ in range(MAX_TRIGGER_KEYS):
            bs.write_flag(False)  # trigger[i]

    def _write_ghost_section(self, bs: BitStream) -> None:
        # We do not ghost anything to the server: single 0 flag.
        bs.write_flag(False)
