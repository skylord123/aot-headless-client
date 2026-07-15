"""Event section reader/writer + RemoteCommandEvent / NetStringEvent + dispatch.

This implements the NetEvent layer that rides inside a connected DataPacket body
(after the GameConnection control header). See docs/event-system.md.

What it covers:

* The event-section framing (``eventWritePacket`` / ``eventReadPacket``,
  netEvent.cc) -- two phases (unguaranteed then guaranteed-ordered) with the
  presence-bit / sequence / classId encoding and the trailing terminators.
* ``RemoteCommandEvent`` (classId 7) pack/unpack: ``argc`` (5 bits) then each arg
  via ``packString`` with the 2-bit type tags
  (Null=0, CString=1, TagString=2, Integer=3).
* ``NetStringEvent`` (classId 5) pack/unpack: (5-bit slot, Huffman string), and
  the per-connection 32-slot string tables (send + receive) negotiated lazily.
* A dispatch layer mapping incoming RemoteCommandEvents to ``clientCmd<Verb>``
  handlers, and an API (:meth:`EventManager.command_to_server`) to send
  ``commandToServer(verb, *args)``.

This module owns NO socket and NO timing; the connection layer (netconn.py) calls
:meth:`EventManager.write_events` while building a packet and
:meth:`EventManager.read_events` while parsing one. Guaranteed-ordered events are
tracked for in-order processing and re-sent until acked (notify-driven).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import protocol_constants as pc
from .bitstream import BitStream

logger = logging.getLogger("aotbot.events")

# RemoteCommandEvent constants (net.cc:27-30).
MAX_REMOTE_COMMAND_ARGS = 20
COMMAND_ARGS_BITS = 5

# StringTagPrefixByte: a tagged-string literal is "\x01" + decimal id.
STRING_TAG_PREFIX_BYTE = 0x01
STRING_TAG_PREFIX = "\x01"

# 7-bit ordered event sequence width (netEvent.cc).
EVENT_SEQ_BITS = 7
EVENT_SEQ_MASK = 0x7F

# ConnectionMessageEvent: 3-bit message field (netConnection.cc).
CONNECTION_MSG_BITS = 3

# Audio events carry a 10-bit datablock (AudioProfile) id (EXE: readInt(0xa)).
DATABLOCK_AUDIO_ID_BITS = 10

# SimDataBlockEvent envelope widths (EXE-confirmed @ VA 0x45a260, AoT fork):
#   readFlag present; readInt(10)+3 id; readClassId(DataBlock); readInt(10) index;
#   readInt(11) total; then obj->unpackData(bstream) (per-datablock-class).
# DataBlockObjectIdBitSize=10, DataBlockObjectIdFirst=3, total = id-bits + 1.
DATABLOCK_OBJECT_ID_BITS = 10       # EXE 0x45a2a9 push 0xa
DATABLOCK_OBJECT_ID_FIRST = 3       # EXE 0x45a2ba add eax,3
DATABLOCK_TOTAL_BITS = DATABLOCK_OBJECT_ID_BITS + 1  # EXE 0x45a2db push 0xb (=11)

# readCompressedPoint bit counts (bitStream.cc gBitCounts; AoT table @ VA
# 0x63c0f8 = {16,18,20,32}).
COMPRESSED_POINT_BIT_COUNTS = (16, 18, 20, 32)


def _read_compressed_point(bs: BitStream) -> None:
    """BitStream::readCompressedPoint (AoT @ VA 0x421a70).

    ``readInt(2)`` type; type 3 -> 3 x F32; types 0/1/2 ->
    3 x ``readSignedInt(gBitCounts[type])``. Advances the cursor only.
    """
    t = bs.read_int(2)
    if t == 3:
        bs.read_bytes(12)
    else:
        n = COMPRESSED_POINT_BIT_COUNTS[t]
        for _ in range(3):
            bs.read_signed_int(n)


# --------------------------------------------------------------------------- #
# Per-connection string tables (connectionStringTable.{cc,h})
# --------------------------------------------------------------------------- #


class ConnectionStringTable:
    """A 32-slot LRU window mapping strings <-> 5-bit on-the-wire ids.

    Two are kept per connection: one for what *we* send (we allocate slots and
    teach the peer via NetStringEvent) and one for what we *receive* (filled by
    incoming NetStringEvents). EntryCount=32, EntryBitSize=5.
    """

    ENTRY_COUNT = 1 << pc.STRING_TABLE_ENTRY_BIT_SIZE  # 32

    def __init__(self) -> None:
        # slot id -> string text (the de-tagged plain text of the tagged literal)
        self._slot_to_str: list[Optional[str]] = [None] * self.ENTRY_COUNT
        self._str_to_slot: dict[str, int] = {}
        # LRU ordering: most-recently used slot ids at the end.
        self._lru: list[int] = []

    def map_string(self, slot: int, text: str) -> None:
        """Receive side: record that the peer assigned ``slot`` -> ``text``."""
        slot &= self.ENTRY_COUNT - 1
        old = self._slot_to_str[slot]
        if old is not None and self._str_to_slot.get(old) == slot:
            del self._str_to_slot[old]
        self._slot_to_str[slot] = text
        self._str_to_slot[text] = slot

    def lookup(self, slot: int) -> Optional[str]:
        return self._slot_to_str[slot & (self.ENTRY_COUNT - 1)]

    def get_send_id(self, text: str) -> tuple[int, bool]:
        """Send side: return ``(slot, is_new)``. If new, the caller must emit a
        NetStringEvent teaching the peer the mapping. LRU-evicts when full.
        """
        if text in self._str_to_slot:
            slot = self._str_to_slot[text]
            self._touch(slot)
            return slot, False
        # Find a free slot, else evict the least-recently-used.
        slot = self._free_slot()
        old = self._slot_to_str[slot]
        if old is not None:
            self._str_to_slot.pop(old, None)
        self._slot_to_str[slot] = text
        self._str_to_slot[text] = slot
        self._touch(slot)
        return slot, True

    def _free_slot(self) -> int:
        for i in range(self.ENTRY_COUNT):
            if self._slot_to_str[i] is None:
                return i
        # All full: evict LRU (front of list).
        return self._lru[0]

    def _touch(self, slot: int) -> None:
        if slot in self._lru:
            self._lru.remove(slot)
        self._lru.append(slot)


# --------------------------------------------------------------------------- #
# Event value objects
# --------------------------------------------------------------------------- #


@dataclass
class RemoteCommandEvent:
    """A ``commandToServer`` / ``clientCmd*`` event.

    ``argv[0]`` is the verb (a tagged string on the wire); ``argv[1:]`` are the
    parameters. ``verb`` is the de-tagged plain verb text once resolved.
    """

    argv: list[str] = field(default_factory=list)

    @property
    def verb(self) -> str:
        return self.argv[0] if self.argv else ""

    @property
    def args(self) -> list[str]:
        return self.argv[1:]


@dataclass
class NetStringEvent:
    """Teaches the peer a connection-local (slot -> string) mapping."""

    index: int
    string: str


# --------------------------------------------------------------------------- #
# Outgoing event queue entry
# --------------------------------------------------------------------------- #


@dataclass
class _PendingEvent:
    """An event awaiting transmission/ack on our guaranteed-ordered stream."""

    classid: int
    payload_writer: Callable[[BitStream], None]
    seq: int = -1            # assigned 7-bit ordered seq when first sent
    sent_in_packet: int = -1  # netconn send-seq it last rode in (-1 = unsent)
    description: str = ""


# --------------------------------------------------------------------------- #
# EventManager
# --------------------------------------------------------------------------- #

_INT_RE = re.compile(r"^-?\d+$")


class EventManager:
    """Owns the event section for one connection (both directions)."""

    def __init__(self) -> None:
        self.send_table = ConnectionStringTable()    # our tags -> ids we teach
        self.recv_table = ConnectionStringTable()    # ids the server taught us

        # Guaranteed-ordered outgoing queue.
        self._out_queue: list[_PendingEvent] = []
        self._next_send_seq = 0  # 7-bit ordered seq for our outgoing events

        # Incoming ordered-event bookkeeping (we process payloads inline to stay
        # aligned; ordering is informational for the bot).
        self._next_recv_event_seq = 0

        # Verb-name -> handler(args: list[str], raw: RemoteCommandEvent).
        self._handlers: dict[str, Callable[[list[str], RemoteCommandEvent], None]] = {}
        self._default_handler: Optional[
            Callable[[str, list[str], RemoteCommandEvent], None]
        ] = None

        # Hook to ask the connection layer to flush a packet soon.
        self.request_send: Callable[[], None] = lambda: None
        # Hook fired for each ConnectionMessageEvent (message, sequence, ghostCount).
        self.on_connection_message: Optional[Callable[[int, int, int], None]] = None
        # Hook fired when a GhostAlwaysObjectEvent scopes a ghost id -> object
        # classId (so the ghost SECTION's new-vs-existing branch knows the class
        # of an already-scoped ghost). (ghost_id, class_id).
        self.on_ghost_scoped: Optional[Callable[[int, int], None]] = None
        # Live-entity telemetry (set by phases when AOT_TRACK_OBJECTS is on). The
        # GhostAlwaysObjectEvent's initial unpackUpdate + each SimDataBlockEvent's
        # shapeFile populate this registry. The unpackUpdate / unpackData are
        # consumed for alignment EITHER WAY; we only POPULATE the registry when on.
        self.object_registry = None
        self.track_objects = False

    # ------------------------------------------------------------------ #
    # Handler registration / dispatch
    # ------------------------------------------------------------------ #

    def on_client_cmd(
        self, verb: str, handler: Callable[[list[str], RemoteCommandEvent], None]
    ) -> None:
        """Register a handler for an incoming ``clientCmd<Verb>`` (verb without
        the ``clientCmd`` prefix, case-sensitive as the server sends it).
        """
        self._handlers[verb] = handler

    def set_default_handler(
        self, handler: Optional[Callable[[str, list[str], RemoteCommandEvent], None]]
    ) -> None:
        self._default_handler = handler

    # ------------------------------------------------------------------ #
    # Sending: commandToServer
    # ------------------------------------------------------------------ #

    def command_to_server(self, verb: str, *args) -> None:
        """Queue a RemoteCommandEvent: ``commandToServer(verb, *args)``.

        ``verb`` is a tagged string (gets a 5-bit connection-local id, taught via
        a NetStringEvent the first time we use it). ``args`` are plain
        strings/ints encoded by packString.
        """
        argv = [str(verb)] + [self._stringify(a) for a in args]
        if len(argv) > MAX_REMOTE_COMMAND_ARGS:
            argv = argv[:MAX_REMOTE_COMMAND_ARGS]

        # Ensure the verb tag is taught to the peer first (NetStringEvent), then
        # the RemoteCommandEvent referencing the slot.
        slot, is_new = self.send_table.get_send_id(str(verb))
        if is_new:
            self._enqueue_net_string_event(slot, str(verb))

        def write_payload(bs: BitStream, _argv=argv, _slot=slot) -> None:
            self._write_remote_command(bs, _argv, _slot)

        self._enqueue(
            pc.REMOTE_COMMAND_EVENT_CLASS_ID,
            write_payload,
            description=f"commandToServer{tuple(argv)!r}",
        )

    @staticmethod
    def _stringify(value) -> str:
        if isinstance(value, bool):
            return "1" if value else "0"
        return str(value)

    def _enqueue_net_string_event(self, slot: int, text: str) -> None:
        def write_payload(bs: BitStream, _slot=slot, _text=text) -> None:
            bs.write_int(_slot, pc.STRING_TABLE_ENTRY_BIT_SIZE)
            bs.write_string(_text)

        # Do NOT flush here: a NetStringEvent is always immediately followed by
        # the RemoteCommandEvent that references the slot it teaches. The real
        # client packs both into ONE packet (capture c2s seq=92 / seq=1). If we
        # flushed on the NetStringEvent enqueue, the two events would split across
        # two packets (the NetStringEvent alone, then the RemoteCommandEvent),
        # diverging from the genuine client; the caller flushes once after both
        # are queued.
        self._enqueue(
            pc.NET_STRING_EVENT_CLASS_ID,
            write_payload,
            description=f"NetStringEvent({slot}, {text!r})",
            flush=False,
        )

    def has_pending_events(self) -> bool:
        """True if any queued guaranteed event is unsent / NACKed and so needs to
        ride a DATA packet (sent_in_packet < 0). Events already in flight (awaiting
        their delivery notify) do NOT count -- re-sending them is handled by the
        NACK path, not by every received packet. Used by netconn to decide DATA
        vs cheap ACK in response to incoming packets (so idle acking during the
        ghost stream doesn't exhaust the send window)."""
        return any(ev.sent_in_packet < 0 for ev in self._out_queue)

    def _enqueue(
        self,
        classid: int,
        payload_writer: Callable[[BitStream], None],
        *,
        description: str,
        flush: bool = True,
    ) -> None:
        self._out_queue.append(
            _PendingEvent(classid=classid, payload_writer=payload_writer, description=description)
        )
        if flush:
            self.request_send()

    # ------------------------------------------------------------------ #
    # RemoteCommandEvent payload pack/unpack
    # ------------------------------------------------------------------ #

    def _write_remote_command(self, bs: BitStream, argv: list[str], verb_slot: int) -> None:
        """net.cc:74-83: writeInt(argc,5) then packString each arg in order.

        argv[0] is the verb; we force it to be packed as a TagString using the
        slot we just (lazily) allocated, exactly as the engine's
        validateSendString path does.
        """
        argc = len(argv)
        bs.write_int(argc, COMMAND_ARGS_BITS)
        # Verb as TagString.
        bs.write_int(pc.STRING_TAG_TAGSTRING, 2)
        bs.write_int(verb_slot, pc.STRING_TABLE_ENTRY_BIT_SIZE)
        # Remaining args via plain packString.
        for arg in argv[1:]:
            self.pack_string(bs, arg)

    def pack_string(self, bs: BitStream, s: str) -> None:
        """netConnection.cc:886-928 -- 2-bit type prefix + payload.

        Tagged-string literals (``\\x01<digits>``) encode as TagString using the
        SEND table (allocating + teaching a slot if needed). This path is used
        for args that are themselves tagged; the common case for our outgoing
        commands is CString / Integer args.
        """
        if s is None or s == "":
            bs.write_int(pc.STRING_TAG_NULL, 2)
            return
        if s.startswith(STRING_TAG_PREFIX):
            text = s[1:]
            slot, is_new = self.send_table.get_send_id(text)
            if is_new:
                self._enqueue_net_string_event(slot, text)
            bs.write_int(pc.STRING_TAG_TAGSTRING, 2)
            bs.write_int(slot, pc.STRING_TABLE_ENTRY_BIT_SIZE)
            return
        if _INT_RE.match(s):
            num = int(s)
            # Round-trip check (engine only uses Integer if dSprintf %d matches).
            if str(num) == s and -(1 << 31) <= num < (1 << 31):
                self._write_integer(bs, num)
                return
        bs.write_int(pc.STRING_TAG_CSTRING, 2)
        bs.write_string(s)

    @staticmethod
    def _write_integer(bs: BitStream, num: int) -> None:
        bs.write_int(pc.STRING_TAG_INTEGER, 2)
        neg = num < 0
        bs.write_flag(neg)
        mag = -num if neg else num
        if bs.write_flag(mag < 128):
            bs.write_int(mag, 7)
        elif bs.write_flag(mag < 32768):
            bs.write_int(mag, 15)
        else:
            bs.write_int(mag, 31)

    def unpack_string(self, bs: BitStream) -> str:
        """netConnection.cc:930-961 -- reverse of pack_string.

        A TagString returns the engine's reconstructed literal
        ``StringTagPrefixByte + decimal id``; callers that want the de-tagged
        text use :meth:`detag`.
        """
        tag = bs.read_int(2)
        if tag == pc.STRING_TAG_NULL:
            return ""
        if tag == pc.STRING_TAG_CSTRING:
            return bs.read_string()
        if tag == pc.STRING_TAG_TAGSTRING:
            slot = bs.read_int(pc.STRING_TABLE_ENTRY_BIT_SIZE)
            return STRING_TAG_PREFIX + str(slot)
        # Integer.
        neg = bs.read_flag()
        if bs.read_flag():
            mag = bs.read_int(7)
        elif bs.read_flag():
            mag = bs.read_int(15)
        else:
            mag = bs.read_int(31)
        return str(-mag if neg else mag)

    def detag(self, s: str) -> str:
        """Resolve a TagString literal (``\\x01<id>``) to text via the RECEIVE
        table; plain strings pass through unchanged.
        """
        if s.startswith(STRING_TAG_PREFIX):
            rest = s[1:]
            if rest.isdigit():
                text = self.recv_table.lookup(int(rest))
                if text is not None:
                    return text
            return rest
        return s

    # ------------------------------------------------------------------ #
    # Event section framing -- write side (eventWritePacket)
    # ------------------------------------------------------------------ #

    def write_events(self, bs: BitStream, current_send_seq: int) -> None:
        """Append the event section to a DataPacket body.

        We only ever send guaranteed-ordered events (RemoteCommandEvent /
        NetStringEvent are GuaranteedOrdered). So phase 1 (unguaranteed) is just
        the terminating ``0`` bit; phase 2 emits each event that is NOT already
        in flight.

        CRITICAL (was a bug): an event that has already ridden an unacked packet
        is **NOT re-sent** in subsequent packets -- it waits for that packet's
        notify (delivered -> dropped; lost -> marked for resend). The previous
        code re-emitted every queued event in EVERY packet and overwrote
        ``sent_in_packet`` each time, so the original delivery notify never
        matched and the event was both (a) resent ~30x/s forever and (b) never
        cleared. This left the AoT server seeing a duplicate, ever-resent
        Phase1Ack and not advancing. eventWritePacket only walks events whose
        send state says they need sending (netEvent.cc) -- i.e. unsent or NACKed.
        """
        # Phase 1 (unguaranteed): none -> end with 0.
        bs.write_flag(False)

        # Phase 2 (guaranteed-ordered): emit only events not currently in flight
        # (sent_in_packet < 0 == new or previously lost). Already-sent events
        # stay in the queue awaiting their notify; we must still preserve their
        # ordered seq so the seq stream the receiver sees stays monotonic.
        prev_seq = -1
        for ev in self._out_queue:
            # (Re)assign an ordered seq the first time we send it.
            if ev.seq < 0:
                ev.seq = self._next_send_seq & EVENT_SEQ_MASK
                self._next_send_seq = (self._next_send_seq + 1) & EVENT_SEQ_MASK
            if ev.sent_in_packet >= 0:
                # Already in flight in an unacked packet -- do not re-send it.
                # Keep prev_seq tracking the highest seq we would have emitted so
                # the shortcut-flag math stays correct for any trailing new event.
                prev_seq = ev.seq
                continue
            bs.write_flag(True)  # event present
            # Ordered seq: a "prev+1" shortcut flag (1 bit), then a 7-bit seq
            # ONLY when the shortcut flag is 0. We ALWAYS write the explicit seq
            # (shortcut flag 0), exactly as the real AoT client does on its first
            # guaranteed event. CAPTURE-CONFIRMED BUG: using the prev+1 shortcut
            # makes the receiver resolve our seq against ITS OWN prevSeq init
            # (which is not -1 for the connection's first client event), so the
            # server mis-sequenced our Phase1Ack RemoteCommandEvent, never ran
            # serverCmdMissionStartPhase1Ack, and never streamed datablocks /
            # advanced to Phase2 -- the root cause of the login wall. Writing the
            # explicit seq is always valid and removes that ambiguity.
            bs.write_flag(False)
            bs.write_int(ev.seq, EVENT_SEQ_BITS)
            prev_seq = ev.seq
            bs.write_int(ev.classid, pc.NET_CLASS_BITS_EVENT)
            ev.payload_writer(bs)
            ev.sent_in_packet = current_send_seq

        bs.write_flag(False)  # end of guaranteed phase
        # eventWritePacket writes one more terminating 0 (netEvent.cc:238).
        bs.write_flag(False)

    def notify_event_delivered(self, send_seq: int, delivered: bool) -> None:
        """netconn calls this per acked/nacked send-seq. On delivery, drop the
        events that rode that packet; on loss, mark them for resend.
        """
        remaining: list[_PendingEvent] = []
        for ev in self._out_queue:
            if ev.sent_in_packet == send_seq:
                if delivered:
                    continue  # confirmed; drop it
                ev.sent_in_packet = -1  # lost; will be resent (keep its seq)
            remaining.append(ev)
        self._out_queue = remaining

    # ------------------------------------------------------------------ #
    # Event section framing -- read side (eventReadPacket)
    # ------------------------------------------------------------------ #

    def read_events(self, bs: BitStream) -> None:
        """Consume the event section, processing each event to stay aligned.

        netEvent.cc:252-328 loop with the two-phase presence framing.
        """
        prev_seq = -1
        unguaranteed_phase = True
        # Guard against a malformed stream spinning forever.
        for _ in range(4096):
            bit = bs.read_flag()
            if bs.error:
                return
            if unguaranteed_phase and not bit:
                unguaranteed_phase = False
                bit = bs.read_flag()
                if bs.error:
                    return
            if not unguaranteed_phase and not bit:
                return  # end of event section
            # bit == 1 -> an event follows.
            if not unguaranteed_phase:
                # Ordered seq: a "prev+1" shortcut flag, then a 7-bit seq ONLY
                # if the flag is 0. EXE-confirmed: eventReadPacket @ VA 0x548d25
                # does an inline readFlag (body @ 0x548df4); if the bit is 1 it
                # sets seq=(prevSeq+1)&0x7F (0x548e22) WITHOUT reading 7 bits;
                # if 0 it falls to `push 7; readInt` @ 0x548d35. The earlier
                # "no shortcut" note was wrong -- omitting this flag read 5 extra
                # bits and desynced the first guaranteed event (classId 14).
                if bs.read_flag():
                    seq = (prev_seq + 1) & EVENT_SEQ_MASK
                else:
                    seq = bs.read_int(EVENT_SEQ_BITS)
                if bs.error:
                    return
                prev_seq = seq
            classid = bs.read_int(pc.NET_CLASS_BITS_EVENT)
            self._read_one_event(bs, classid)
            if bs.error:
                return

    def _read_one_event(self, bs: BitStream, classid: int) -> None:
        if classid == pc.NET_STRING_EVENT_CLASS_ID:
            self._read_net_string_event(bs)
        elif classid == pc.REMOTE_COMMAND_EVENT_CLASS_ID:
            self._read_remote_command_event(bs)
        elif classid == pc.EVENT_CLASS_IDS["ConnectionMessageEvent"]:
            self._read_connection_message_event(bs)
        elif classid == pc.EVENT_CLASS_IDS["PathManagerEvent"]:
            self._read_path_manager_event(bs)
        elif classid == pc.EVENT_CLASS_IDS["FileChunkEvent"]:
            self._read_file_chunk_event(bs)
        elif classid == pc.EVENT_CLASS_IDS["FileDownloadRequestEvent"]:
            self._read_file_download_request_event(bs)
        elif classid == pc.EVENT_CLASS_IDS["SetMissionCRCEvent"]:
            self._read_set_mission_crc_event(bs)
        elif classid == pc.EVENT_CLASS_IDS["Sim2DAudioEvent"]:
            self._read_sim2d_audio_event(bs)
        elif classid == pc.EVENT_CLASS_IDS["Sim3DAudioEvent"]:
            self._read_sim3d_audio_event(bs)
        elif classid == pc.EVENT_CLASS_IDS["LightningStrikeEvent"]:
            self._read_lightning_strike_event(bs)
        elif classid == pc.EVENT_CLASS_IDS["SimpleMessageEvent"]:
            self._read_simple_message_event(bs)
        elif classid == pc.EVENT_CLASS_IDS["GhostAlwaysObjectEvent"]:
            self._read_ghost_always_object_event(bs)
        elif classid == pc.EVENT_CLASS_IDS["SimDataBlockEvent"]:
            self._read_sim_datablock_event(bs)
        elif classid == pc.EVENT_CLASS_IDS["StaticBrickDataEvent"]:
            self._read_static_brick_data_event(bs)
        else:
            # We can't generically skip an unknown event (bit-packed, no length
            # prefix). Surface this loudly so phases.py / the caller can decide.
            raise EventDecodeError(classid)

    def _read_connection_message_event(self, bs: BitStream) -> None:
        """ConnectionMessageEvent::unpack (AoT @ VA 0x5464a0):
        read(U32 sequence) + readInt(3) message + readInt(GhostIdBitSize+1=15)
        ghostCount.

        EXE-confirmed widths: the U32 ``read(&sequence)`` (``call [eax+4]`` push
        4), then ``readInt(3)`` (``push 3``), then ``readInt(0xf=15)``
        (``push 0xf``). The 15-bit ghostCount = AoT's GhostIdBitSize(14)+1
        (stock TGE used 13). These signal DataBlocksDone / ghost-state
        transitions. We record the message but take no scoping action (we stay
        logged out / unspawned).
        """
        sequence = bs.read_int(32)
        message = bs.read_int(CONNECTION_MSG_BITS)
        ghost_count = bs.read_int(pc.GHOST_ID_BIT_SIZE + 1)
        logger.debug(
            "ConnectionMessageEvent seq=%d message=%d ghostCount=%d",
            sequence, message, ghost_count,
        )
        if self.on_connection_message is not None:
            try:
                self.on_connection_message(message, sequence, ghost_count)
            except Exception:
                logger.exception("on_connection_message hook raised")

    def _read_path_manager_event(self, bs: BitStream) -> None:
        """PathManagerEvent::unpack (pathManager.cc:86): walk a path definition.

        We only decode it to stay bit-aligned (the server pushes path/mission
        data during load). Layout: read(U32 modifiedPath), readFlag clearPaths,
        read(U32 totalTime), read(U32 numPoints), then per point Point3F (3 F32)
        + QuatF (4 F32) + read(U32 msToNext) + read(U32 smoothingType).
        """
        bs.read_int(32)        # modifiedPath
        bs.read_flag()         # clearPaths
        bs.read_int(32)        # totalTime
        num_points = bs.read_int(32)
        # Guard against a corrupt/huge count from an already-desynced stream.
        if num_points > 4096:
            raise EventDecodeError(pc.EVENT_CLASS_IDS["PathManagerEvent"])
        for _ in range(num_points):
            bs.read_bytes(4 * 3)  # Point3F position (3 x F32)
            bs.read_bytes(4 * 4)  # QuatF rotation (4 x F32)
            bs.read_int(32)       # msToNext
            bs.read_int(32)       # smoothingType
        logger.debug("PathManagerEvent: %d points", num_points)

    def _read_file_chunk_event(self, bs: BitStream) -> None:
        """FileChunkEvent::unpack (netDownload.cc): readRangedU32(0,63) chunkLen
        then chunkLen raw bytes. The server streams a file to us (a resource the
        client would normally write to disk). We decode it to stay aligned and
        discard the bytes -- a headless bot needs no on-disk resources.
        """
        chunk_len = bs.read_ranged_u32(0, 63)
        bs.read_bytes(chunk_len)
        logger.debug("FileChunkEvent: %d bytes (discarded)", chunk_len)

    def _read_file_download_request_event(self, bs: BitStream) -> None:
        """FileDownloadRequestEvent::unpack (netDownload.cc): readRangedU32(0,31)
        nameCount then that many writeString file names.
        """
        name_count = bs.read_ranged_u32(0, 31)
        names = [bs.read_string() for _ in range(name_count)]
        logger.debug("FileDownloadRequestEvent: %s", names)

    def _read_set_mission_crc_event(self, bs: BitStream) -> None:
        """SetMissionCRCEvent::unpack (AoT @ VA 0x457640): read(U32 crc).

        The server tells us the mission CRC. EXE-confirmed: a single
        ``read(4)`` U32 (``call [eax+4]`` push 4). We decode it to stay aligned;
        a headless bot needs no mission file so the value is discarded.
        """
        crc = bs.read_int(32)
        logger.debug("SetMissionCRCEvent: crc=0x%08x", crc)

    def _read_sim2d_audio_event(self, bs: BitStream) -> None:
        """Sim2DAudioEvent::unpack (AoT): readInt(10) audio-profile datablock id.

        A non-positional sound cue. EXE-confirmed payload is just the 10-bit
        datablock id (no transform). Decoded to stay aligned; discarded.
        """
        profile_id = bs.read_int(DATABLOCK_AUDIO_ID_BITS)
        logger.debug("Sim2DAudioEvent: profile=%d", profile_id)

    def _read_sim3d_audio_event(self, bs: BitStream) -> None:
        """Sim3DAudioEvent::unpack (AoT @ VA 0x45a6b0), RE-DISASSEMBLED.

        The old transcription (profile id + compressed point) missed the whole
        transform block, under-reading every 3D sound cue with an orientation
        and silently truncating the rest of the packet's events. True layout:

          * readInt(10)                     audio-profile datablock id (+3)
          * flag (bit test @0x45a763):      transform present; if SET ->
              - 3 x readFloat(8)            compressed quaternion x,y,z
                (width from the global @0x63d56c == 8; reads @0x45a794/7a5/7b7)
              - flag                        quat w sign (@0x45a817 -> fchs)
          * readCompressedPoint             position (@0x45a73f, scale from the
                                            global @0x63d568) -- ALWAYS read.
        """
        profile_id = bs.read_int(DATABLOCK_AUDIO_ID_BITS)
        if bs.read_flag():                 # transform present (0x45a763)
            bs.read_float(8)               # quat x (0x45a794)
            bs.read_float(8)               # quat y (0x45a7a5)
            bs.read_float(8)               # quat z (0x45a7b7)
            bs.read_flag()                 # quat w sign (0x45a817)
        _read_compressed_point(bs)         # position (0x45a73f)
        logger.debug("Sim3DAudioEvent: profile=%d", profile_id)

    def _read_lightning_strike_event(self, bs: BitStream) -> None:
        """LightningStrikeEvent::unpack (AoT @ VA 0x4b35f0): EMPTY -- reads zero
        bits (the disassembled method is a bare ``ret``). It is a pure
        presence/trigger marker; nothing on the wire beyond its classId.
        """
        logger.debug("LightningStrikeEvent (no payload)")

    def _read_simple_message_event(self, bs: BitStream) -> None:
        """SimpleMessageEvent::unpack (AoT @ VA 0x4c2cf0): readString(message).

        EXE-confirmed: a single ``readString`` (bitstream vtable slot 0x1c) which
        the engine then evals/prints. Uses the per-packet stringBuffer dedup path
        (installed by read_packet_body). Decoded to stay aligned.
        """
        msg = bs.read_string()
        logger.debug("SimpleMessageEvent: %r", msg)

    def _read_ghost_always_object_event(self, bs: BitStream) -> None:
        """GhostAlwaysObjectEvent::unpack (AoT @ VA 0x5496a0).

        EXE-confirmed (re-traced Wave-9 -- the prior reading STILL missed the
        unpackUpdate call that carries the object's whole initial state):

          * ``readInt(GhostIdBitSize=14)``           ghost id (@ 0x5496a9)
          * an inline ``readFlag``                    hasClassId (@ 0x5496ee)
              - if clear (==0): the engine creates the object by a NAME lookup
                (@ 0x5496c1) with NO further bit read, NO unpackUpdate.
              - if set (==1): ``readClassId(NetClassTypeObject)`` (6 bits,
                @ 0x549727), create the object by class id, then call the
                object's ``unpackUpdate(connection, stream)`` (vtable slot 0x4c,
                @ 0x54976b) -- the object's INITIAL state is packed right here,
                length-less, so we MUST decode it bit-exactly or the next event
                desyncs. (Wave-8 stopped after the classId; that swallowed the
                whole unpackUpdate payload as the next event's framing, which is
                why the stream desynced into a bogus classId 14 at the first
                scoped ghost.)

        Subsequent state for this ghost id arrives in the ghost SECTION
        (ghostReadPacket) as an *existing*-ghost ``unpackUpdate`` (is_new False).
        """
        ghost_id = bs.read_int(pc.GHOST_ID_BIT_SIZE)
        if not bs.read_flag():
            logger.debug("GhostAlwaysObjectEvent: ghost id %d (name lookup)", ghost_id)
            return
        class_id = bs.read_int(pc.NET_CLASS_BITS_OBJECT)
        logger.debug(
            "GhostAlwaysObjectEvent: ghost id %d classId %d (initial unpackUpdate)",
            ghost_id, class_id,
        )
        if self.on_ghost_scoped is not None:
            try:
                self.on_ghost_scoped(ghost_id, class_id)
            except Exception:
                logger.exception("on_ghost_scoped hook raised")
        # Initial-state unpackUpdate (is_new=True). Dispatch to ghosts.py; an
        # un-ported class raises GhostDecodeError -> EventDecodeError so the
        # caller logs exactly which object class blocks. When telemetry is ON we
        # install a DecodeSink to capture the object's INITIAL transform/datablock
        # state and seed the registry (consumed for alignment either way).
        from . import ghosts as _gh
        from . import telemetry
        sink = telemetry.DecodeSink() if self.track_objects else None
        if sink is not None:
            telemetry.set_sink(sink)
        try:
            _gh.unpack_update(bs, class_id, is_new=True)
        except _gh.GhostDecodeError as exc:
            logger.warning(
                "GhostAlwaysObjectEvent ghost id %d: %s -- cannot stay aligned",
                ghost_id, exc,
            )
            raise EventDecodeError(
                pc.EVENT_CLASS_IDS["GhostAlwaysObjectEvent"]
            ) from exc
        finally:
            if sink is not None:
                telemetry.set_sink(None)
        if sink is not None and self.object_registry is not None:
            name = (
                _gh.OBJECT_CLASS_NAMES[class_id]
                if 0 <= class_id < len(_gh.OBJECT_CLASS_NAMES)
                else f"<{class_id}>"
            )
            self.object_registry.update_from_sink(ghost_id, name, sink, is_new=True)

    def _read_sim_datablock_event(self, bs: BitStream) -> None:
        """SimDataBlockEvent::unpack (AoT @ VA 0x45a260).

        Envelope (EXE-confirmed): ``readFlag()`` present; if 0 the event is
        empty. Else ``readInt(10)+3`` id, ``readClassId(DataBlock)`` (6-bit
        width), ``readInt(10)`` index, ``readInt(11)`` total, then
        ``obj->unpackData(bstream)`` -- a **per-datablock-class** payload with
        NO length prefix.

        We decode the envelope (which is exact) and surface the datablock
        ``classId``. The per-class ``unpackData`` is not ported (the AoT server
        does not send any SimDataBlockEvent during the connect->login window --
        zero observed across 250+ live packets), so once a present datablock
        actually appears we cannot stay aligned and raise EventDecodeError with
        the class so the caller logs exactly which class is responsible.
        """
        if not bs.read_flag():
            logger.debug("SimDataBlockEvent: empty (present flag 0)")
            return
        db_id = bs.read_int(DATABLOCK_OBJECT_ID_BITS) + DATABLOCK_OBJECT_ID_FIRST
        class_id = self._read_class_id(bs, pc.NET_CLASS_BITS_DATABLOCK)
        index = bs.read_int(DATABLOCK_OBJECT_ID_BITS)
        total = bs.read_int(DATABLOCK_TOTAL_BITS)
        # Per-class unpackData (no length prefix). Dispatch to datablocks.py; if
        # the class isn't decodable we cannot stay aligned, so re-raise as an
        # EventDecodeError carrying the SimDataBlockEvent classId (and log the
        # responsible datablock class name).
        from . import datablocks as _db
        from . import telemetry
        logger.debug(
            "SimDataBlockEvent id=%d classId=%d index=%d total=%d",
            db_id, class_id, index, total,
        )
        # When tracking is ON capture the datablock's shapeFile (ShapeBaseData's
        # shapeName) so ghosts can resolve a real shape name from their datablock
        # id. unpackData is consumed for alignment either way.
        sink = telemetry.DecodeSink() if self.track_objects else None
        if sink is not None:
            telemetry.set_sink(sink)
        try:
            _db.unpack_datablock(bs, class_id)
        except _db.DataBlockDecodeError as exc:
            logger.warning(
                "SimDataBlockEvent id=%d index=%d/%d: %s -- cannot stay aligned",
                db_id, index, total, exc,
            )
            raise EventDecodeError(pc.EVENT_CLASS_IDS["SimDataBlockEvent"]) from exc
        finally:
            if sink is not None:
                telemetry.set_sink(None)
        if sink is not None and self.object_registry is not None:
            db_name = (
                _db.DATABLOCK_CLASS_NAMES[class_id]
                if 0 <= class_id < len(_db.DATABLOCK_CLASS_NAMES)
                else f"<{class_id}>"
            )
            self.object_registry.record_datablock(
                db_id, db_name, shape_file=sink.fields.get("shape_file")
            )

    @staticmethod
    def _read_class_id(bs: BitStream, bits: int) -> int:
        """readClassId (bitStream @ VA 0x421510): readInt(width) then bound-check.

        For our purposes (we don't instantiate the class) it's just readInt of
        the per-(group,type) width. The exe's bound check returns -1 on
        overflow; we just return the raw value.
        """
        return bs.read_int(bits)

    # StaticBrickDataEvent layout (EXE @ VA 0x4a0900): 16*(4*readFloat(8)) color
    # palette, 16*(readInt(6)+readString) brick categories, readInt(10) N then
    # N*readString. readFloat(8) = readInt(8); readString uses the stringBuffer
    # dedup path.
    # RE-DISASSEMBLED: the palette loop @0x4a0910 walks edi from 0x66b784 to
    # 0x66bb84 in 0x10 steps = (0x66bb84-0x66b784)/0x10 = 64 rows, NOT 16.
    # The old value under-read 48 rows x 32 bits = 1536 bits per event,
    # silently truncating every packet carrying a StaticBrickDataEvent.
    # (The category loop @0x4a0950 really is 16: ``cmp edi, 0x40`` step 4.)
    STATIC_BRICK_PALETTE_ROWS = 64
    STATIC_BRICK_CATEGORY_ROWS = 16

    def _read_static_brick_data_event(self, bs: BitStream) -> None:
        """StaticBrickDataEvent::unpack (AoT @ VA 0x4a0900).

        EXE-confirmed read sequence:
        * ``16 x (4 x readFloat(8))``  -- a 16-row colour/material palette
          (loop @ 0x4a0910, edi 0x66b780..0x66bb84 step 0x10, 4x readFloat(8)
          via the readFloat helper @ 0x421000);
        * ``16 x (readInt(6) + readString)`` -- brick categories
          (loop @ 0x4a0950);
        * ``readInt(10)`` N (count @ 0x4a0976) then ``N x readString``
          (loop @ 0x4a0990, readString helper @ 0x424230).

        AoT-specific global brick config pushed on connect. Decoded to stay
        aligned; values discarded.
        """
        for _ in range(self.STATIC_BRICK_PALETTE_ROWS):
            for _ in range(4):
                bs.read_int(8)  # readFloat(8)
        for _ in range(self.STATIC_BRICK_CATEGORY_ROWS):
            bs.read_int(6)
            bs.read_string()
        n = bs.read_int(DATABLOCK_AUDIO_ID_BITS)  # readInt(10)
        if n > 4096:
            raise EventDecodeError(pc.EVENT_CLASS_IDS["StaticBrickDataEvent"])
        for _ in range(n):
            bs.read_string()
        logger.debug("StaticBrickDataEvent: %d named bricks", n)

    def _read_net_string_event(self, bs: BitStream) -> None:
        index = bs.read_int(pc.STRING_TABLE_ENTRY_BIT_SIZE)
        text = bs.read_string()
        self.recv_table.map_string(index, text)
        logger.debug("NetStringEvent: recv slot %d -> %r", index, text)

    def _read_remote_command_event(self, bs: BitStream) -> None:
        argc = bs.read_int(COMMAND_ARGS_BITS)
        argv: list[str] = []
        for _ in range(argc):
            argv.append(self.unpack_string(bs))
        if not argv:
            return
        verb = self.detag(argv[0])
        # The engine strips to the text after the first space (net.cc), but AoT
        # verbs are single tokens; keep the whole de-tagged verb.
        verb = verb.split(" ")[0] if " " in verb else verb
        resolved_args = [self.detag(a) for a in argv[1:]]
        evt = RemoteCommandEvent(argv=[verb] + resolved_args)
        # Log EVERY received clientCmd (verb + de-tagged args) at INFO so the full
        # server->client command stream is visible on the console without the
        # DEBUG transport hexdump noise. Ghost/move data is NOT a clientCmd and
        # stays quiet. Set LOG_LEVEL=warning to silence.
        logger.info("clientCmd%s(%s)", verb, ", ".join(map(repr, resolved_args)))
        self._dispatch_remote_command(verb, resolved_args, evt)

    def _dispatch_remote_command(
        self, verb: str, args: list[str], evt: RemoteCommandEvent
    ) -> None:
        handler = self._handlers.get(verb)
        if handler is not None:
            try:
                handler(args, evt)
            except Exception:
                logger.exception("clientCmd%s handler raised", verb)
            return
        if self._default_handler is not None:
            try:
                self._default_handler(verb, args, evt)
            except Exception:
                logger.exception("default event handler raised")
        else:
            logger.debug("unhandled clientCmd%s(%s)", verb, args)


class EventDecodeError(Exception):
    """Raised when an event classId we cannot decode appears -- decoding it is
    required to stay bit-aligned, so the caller must treat the rest of the
    packet as undecodable.
    """

    def __init__(self, classid: int) -> None:
        super().__init__(f"cannot decode event classId {classid} (no generic skip)")
        self.classid = classid
