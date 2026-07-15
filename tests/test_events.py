"""Unit tests for the event section: string table, packString, RemoteCommand,
NetStringEvent, and the read/write framing round-trip.
"""

import aotbot.protocol_constants as pc
from aotbot.bitstream import BitStream
from aotbot.events import (
    ConnectionStringTable,
    EventManager,
    RemoteCommandEvent,
    STRING_TAG_PREFIX,
)


# --------------------------------------------------------------------------- #
# ConnectionStringTable
# --------------------------------------------------------------------------- #


def test_string_table_assigns_and_reuses_slots():
    t = ConnectionStringTable()
    slot, is_new = t.get_send_id("Talk")
    assert is_new
    slot2, is_new2 = t.get_send_id("Talk")
    assert not is_new2 and slot2 == slot
    slot3, is_new3 = t.get_send_id("MessageSent")
    assert is_new3 and slot3 != slot


def test_string_table_map_and_lookup():
    t = ConnectionStringTable()
    t.map_string(7, "ChatMessage")
    assert t.lookup(7) == "ChatMessage"


def test_string_table_lru_eviction():
    t = ConnectionStringTable()
    ids = []
    for i in range(ConnectionStringTable.ENTRY_COUNT):
        s, _ = t.get_send_id(f"verb{i}")
        ids.append(s)
    assert len(set(ids)) == ConnectionStringTable.ENTRY_COUNT
    # One more allocation must evict the LRU slot (verb0).
    s_new, is_new = t.get_send_id("overflow")
    assert is_new
    # verb0 should now be gone; re-requesting it allocates fresh.
    _, again_new = t.get_send_id("verb0")
    assert again_new


# --------------------------------------------------------------------------- #
# packString / unpackString round-trips
# --------------------------------------------------------------------------- #


def _roundtrip_string(value):
    em = EventManager()
    bs = BitStream()
    em.pack_string(bs, value)
    rs = BitStream(bs.get_bytes())
    # Use a fresh manager for the read side to mimic a separate peer; but tag
    # ids must resolve, so for tagged values we share the table.
    return em.unpack_string(rs)


def test_packstring_empty_is_null():
    bs = BitStream()
    EventManager().pack_string(bs, "")
    rs = BitStream(bs.get_bytes())
    assert rs.read_int(2) == pc.STRING_TAG_NULL


def test_packstring_cstring_roundtrip():
    assert _roundtrip_string("hello world") == "hello world"


def test_packstring_integer_roundtrip_small():
    assert _roundtrip_string("42") == "42"


def test_packstring_integer_roundtrip_negative():
    assert _roundtrip_string("-7") == "-7"


def test_packstring_integer_roundtrip_large():
    # A big CRC-like value that still fits in signed 31 bits round-trips as int.
    assert _roundtrip_string("100000") == "100000"


def test_packstring_crc_value_as_cstring():
    # A full 32-bit CRC > 2^31 won't round-trip as signed int -> CString.
    big = str(0xDEADBEEF)  # 3735928559
    em = EventManager()
    bs = BitStream()
    em.pack_string(bs, big)
    rs = BitStream(bs.get_bytes())
    tag = rs.read_int(2)
    assert tag == pc.STRING_TAG_CSTRING
    assert rs.read_string() == big


def test_packstring_tagstring_uses_table_and_teaches():
    em = EventManager()
    bs = BitStream()
    em.pack_string(bs, STRING_TAG_PREFIX + "ChatMessage")
    # A NetStringEvent should have been queued teaching the peer.
    assert len(em._out_queue) == 1
    rs = BitStream(bs.get_bytes())
    assert rs.read_int(2) == pc.STRING_TAG_TAGSTRING
    slot = rs.read_int(pc.STRING_TABLE_ENTRY_BIT_SIZE)
    assert slot >= 0


# --------------------------------------------------------------------------- #
# RemoteCommandEvent: full event-section round-trip between two managers
# --------------------------------------------------------------------------- #


def test_command_to_server_roundtrip():
    sender = EventManager()
    receiver = EventManager()

    received = []
    receiver.set_default_handler(
        lambda verb, args, evt: received.append((verb, args))
    )

    sender.command_to_server("login", "alice", 12345)

    # Sender writes its event section.
    bs = BitStream()
    sender.write_events(bs, current_send_seq=1)

    # Receiver reads it. The NetStringEvent (teaching the 'login' tag) precedes
    # the RemoteCommandEvent, so the receiver can de-tag the verb.
    rs = BitStream(bs.get_bytes())
    receiver.read_events(rs)

    assert received == [("login", ["alice", "12345"])]


def test_talk_command_roundtrip():
    sender = EventManager()
    receiver = EventManager()
    got = []
    receiver.on_client_cmd("Talk", lambda args, evt: got.append(args))

    sender.command_to_server("Talk", "hello there")
    bs = BitStream()
    sender.write_events(bs, current_send_seq=1)
    receiver.read_events(BitStream(bs.get_bytes()))

    assert got == [["hello there"]]


def test_two_commands_share_taught_verb_tag():
    sender = EventManager()
    receiver = EventManager()
    got = []
    receiver.on_client_cmd("Talk", lambda args, evt: got.append(args[0]))

    sender.command_to_server("Talk", "one")
    sender.command_to_server("Talk", "two")
    bs = BitStream()
    sender.write_events(bs, current_send_seq=1)
    receiver.read_events(BitStream(bs.get_bytes()))
    assert got == ["one", "two"]


def test_net_string_event_populates_recv_table():
    em = EventManager()
    bs = BitStream()
    bs.write_int(9, pc.STRING_TABLE_ENTRY_BIT_SIZE)
    bs.write_string("ServerMessage")
    em._read_net_string_event(BitStream(bs.get_bytes()))
    assert em.recv_table.lookup(9) == "ServerMessage"
    assert em.detag(STRING_TAG_PREFIX + "9") == "ServerMessage"


def test_notify_delivered_drops_event():
    em = EventManager()
    em.command_to_server("Talk", "x")
    n = len(em._out_queue)
    assert n >= 1
    bs = BitStream()
    em.write_events(bs, current_send_seq=5)
    em.notify_event_delivered(5, delivered=True)
    assert em._out_queue == []


def test_notify_lost_keeps_event_for_resend():
    em = EventManager()
    em.command_to_server("Talk", "x")
    bs = BitStream()
    em.write_events(bs, current_send_seq=5)
    before = list(em._out_queue)
    em.notify_event_delivered(5, delivered=False)
    # Still queued, but marked unsent.
    assert len(em._out_queue) == len(before)
    assert all(ev.sent_in_packet == -1 for ev in em._out_queue)


def test_inflight_event_not_resent_until_acked():
    """An event already sent in an unacked packet must NOT be re-emitted in the
    next packet -- it waits for that packet's notify. The previous code re-sent
    every queued event in EVERY packet (and overwrote sent_in_packet each time),
    so the original delivery notify never matched: the event was both resent
    ~30x/s forever and never cleared. That made the AoT server see a duplicate,
    ever-resent Phase1Ack and stall.
    """
    em = EventManager()
    em.command_to_server("MissionStartPhase1Ack", 1)
    n = len(em._out_queue)
    assert n == 2  # NetStringEvent (teach verb) + RemoteCommandEvent

    # First packet: both events go out (sent_in_packet set to 1).
    bs1 = BitStream()
    em.write_events(bs1, current_send_seq=1)
    assert all(ev.sent_in_packet == 1 for ev in em._out_queue)

    # Second packet BEFORE any notify: events are in flight -> NOT re-emitted.
    # The event section is just the two terminator bits + the unguaranteed end.
    bs2 = BitStream()
    em.write_events(bs2, current_send_seq=2)
    # No event payload bytes: 3 flag bits only (round up to 1 byte, all zero).
    assert bs2.get_byte_position() == 1
    assert bs2.get_bytes() == b"\x00"
    # sent_in_packet stays at the original packet (1), NOT overwritten to 2.
    assert all(ev.sent_in_packet == 1 for ev in em._out_queue)

    # Now the original packet is acked -> events cleared.
    em.notify_event_delivered(1, delivered=True)
    assert em._out_queue == []


def test_verb_teach_and_command_ride_same_packet():
    """A commandToServer with a fresh verb enqueues a NetStringEvent (teach the
    tag) then the RemoteCommandEvent, and BOTH must flush in ONE packet (the real
    client packs them together -- capture c2s seq=92). The NetStringEvent enqueue
    must not trigger its own flush.
    """
    em = EventManager()
    flushes = []
    em.request_send = lambda: flushes.append(len(em._out_queue))
    em.command_to_server("login", "user", 123)
    # Exactly one flush, fired only after both events are queued.
    assert flushes == [2]
    assert len(em._out_queue) == 2


def test_connection_message_event_decode():
    """ConnectionMessageEvent unpack: read(U32 seq) + 3-bit msg + 15-bit
    ghostCount (AoT GhostIdBitSize=14 + 1; stock TGE used 13)."""
    em = EventManager()
    seen = []
    em.on_connection_message = lambda msg, seq, gc: seen.append((msg, seq, gc))
    bs = BitStream()
    bs.write_int(0x12345678, 32)  # sequence
    bs.write_int(4, 3)            # message (DataBlocksDone-ish)
    bs.write_int(7, pc.GHOST_ID_BIT_SIZE + 1)  # ghostCount
    em._read_connection_message_event(BitStream(bs.get_bytes()))
    assert seen == [(4, 0x12345678, 7)]


def test_file_chunk_event_decode_consumes_exact_bytes():
    em = EventManager()
    bs = BitStream()
    bs.write_ranged_u32(10, 0, 63)   # chunkLen
    bs.write_bytes(b"0123456789")    # 10 data bytes
    bs.write_int(0b1011, 4)          # sentinel after the event
    rs = BitStream(bs.get_bytes())
    em._read_file_chunk_event(rs)
    # The 4-bit sentinel must still be readable intact.
    assert rs.read_int(4) == 0b1011


def test_file_download_request_event_decode():
    em = EventManager()
    bs = BitStream()
    bs.write_ranged_u32(2, 0, 31)
    bs.write_string("a.dts")
    bs.write_string("b.png")
    bs.write_int(0b1, 1)
    rs = BitStream(bs.get_bytes())
    em._read_file_download_request_event(rs)
    assert rs.read_int(1) == 1


def test_path_manager_event_decode_consumes_points():
    em = EventManager()
    bs = BitStream()
    bs.write_int(1, 32)    # modifiedPath
    bs.write_flag(True)    # clearPaths
    bs.write_int(5000, 32)  # totalTime
    bs.write_int(2, 32)    # numPoints
    for _ in range(2):
        bs.write_bytes(b"\x00" * (4 * 3))  # Point3F
        bs.write_bytes(b"\x00" * (4 * 4))  # QuatF
        bs.write_int(100, 32)  # msToNext
        bs.write_int(0, 32)    # smoothingType
    bs.write_int(0b110, 3)     # sentinel
    rs = BitStream(bs.get_bytes())
    em._read_path_manager_event(rs)
    assert rs.read_int(3) == 0b110


def test_empty_event_section_is_two_zero_bits_then_terminator():
    em = EventManager()
    bs = BitStream()
    em.write_events(bs, current_send_seq=1)
    # 3 flag bits, all zero (end-unguaranteed, end-guaranteed, extra terminator).
    rs = BitStream(bs.get_bytes())
    assert rs.read_flag() is False
    assert rs.read_flag() is False
    assert rs.read_flag() is False
    # A receiver should parse an empty section without error.
    em2 = EventManager()
    em2.read_events(BitStream(bs.get_bytes()))


# --------------------------------------------------------------------------- #
# AoT-fork event payloads recovered in wave 3 (EXE-confirmed bit layouts)
# --------------------------------------------------------------------------- #


def test_set_mission_crc_event_decode():
    """SetMissionCRCEvent::unpack (VA 0x457640): a single U32 crc."""
    em = EventManager()
    bs = BitStream()
    bs.write_int(0xE937D30D, 32)
    bs.write_int(0b101, 3)  # sentinel
    rs = BitStream(bs.get_bytes())
    em._read_set_mission_crc_event(rs)
    assert rs.read_int(3) == 0b101


def test_lightning_strike_event_reads_nothing():
    """LightningStrikeEvent::unpack (VA 0x4b35f0) is empty -- consumes 0 bits."""
    em = EventManager()
    bs = BitStream()
    bs.write_int(0b1011, 4)  # sentinel that must survive untouched
    rs = BitStream(bs.get_bytes())
    em._read_lightning_strike_event(rs)
    assert rs.get_bit_position() == 0
    assert rs.read_int(4) == 0b1011


def test_sim2d_audio_event_decode():
    """Sim2DAudioEvent::unpack (VA 0x45a580): readInt(10) datablock id."""
    em = EventManager()
    bs = BitStream()
    bs.write_int(640, 10)
    bs.write_int(0b11, 2)  # sentinel
    rs = BitStream(bs.get_bytes())
    em._read_sim2d_audio_event(rs)
    assert rs.read_int(2) == 0b11


def test_sim3d_audio_event_full_precision_point():
    """Sim3DAudioEvent::unpack (VA 0x45a6b0): readInt(10) id + transform flag
    + compressed point.

    Transform flag clear (no quat); point type 3 = 3 raw F32 (96 bits).
    """
    em = EventManager()
    bs = BitStream()
    bs.write_int(161, 10)        # datablock id
    bs.write_flag(False)         # transform absent (bit test @0x45a763)
    bs.write_int(3, 2)           # compressed-point type 3 (full)
    bs.write_bytes(b"\x00" * 12)  # 3 x F32
    bs.write_int(0b101, 3)       # sentinel
    rs = BitStream(bs.get_bytes())
    em._read_sim3d_audio_event(rs)
    assert rs.read_int(3) == 0b101


def test_sim3d_audio_event_quantised_point_with_transform():
    """With the transform flag SET the event carries a compressed quaternion
    (3 x readFloat(8) + w-sign flag) before the compressed point; point types
    0/1/2 read 3 x readSignedInt(gBitCounts[type])."""
    em = EventManager()
    bs = BitStream()
    bs.write_int(5, 10)          # datablock id
    bs.write_flag(True)          # transform present
    for _ in range(3):
        bs.write_int(100, 8)     # quat x/y/z (readFloat(8))
    bs.write_flag(True)          # quat w sign
    bs.write_int(0, 2)           # point type 0 -> 16-bit signed each
    for _ in range(3):
        bs.write_signed_int(7, 16)  # placeholder coords
    bs.write_int(0b1, 1)         # sentinel
    rs = BitStream(bs.get_bytes())
    em._read_sim3d_audio_event(rs)
    assert rs.read_int(1) == 0b1


def test_simple_message_event_decode():
    """SimpleMessageEvent::unpack (VA 0x4c2cf0): readString(message)."""
    em = EventManager()
    bs = BitStream()
    bs.write_string("hello world")
    bs.write_int(0b10, 2)  # sentinel
    rs = BitStream(bs.get_bytes())
    em._read_simple_message_event(rs)
    assert rs.read_int(2) == 0b10


def test_ordered_event_seq_has_shortcut_flag():
    """AoT keeps the stock 'prev+1' shortcut flag before the 7-bit ordered seq
    (EXE-confirmed eventReadPacket @ VA 0x548df4). Two queued guaranteed events
    round-trip through write_events -> read_events.
    """
    sender = EventManager()
    sender.command_to_server("Talk", "a")  # NetStringEvent + RemoteCommandEvent
    bs = BitStream()
    sender.write_events(bs, current_send_seq=1)
    receiver = EventManager()
    received = []
    receiver.set_default_handler(lambda v, a, e: received.append((v, a)))
    receiver.read_events(BitStream(bs.get_bytes()))
    assert ("Talk", ["a"]) in received


def test_ordered_event_first_event_writes_explicit_seq():
    """The first guaranteed event is written with the shortcut flag 0 followed by
    an EXPLICIT 7-bit seq (0), matching the real AoT client. Using the prev+1
    shortcut here made the server resolve the seq against its own prevSeq init
    and mis-sequence our Phase1Ack (the login wall), so we never use it.
    """
    sender = EventManager()
    # Queue a single bare event with a known classid to read raw bits.
    sender._enqueue(
        pc.EVENT_CLASS_IDS["LightningStrikeEvent"],
        lambda bs: None,
        description="lightning",
    )
    bs = BitStream()
    sender.write_events(bs, current_send_seq=1)
    rs = BitStream(bs.get_bytes())
    assert rs.read_flag() is False   # end unguaranteed phase
    assert rs.read_flag() is True    # guaranteed presence
    assert rs.read_flag() is False   # shortcut flag 0 -> explicit seq follows
    assert rs.read_int(7) == 0  # explicit 7-bit seq
    # classId is read next (LightningStrike = 4).
    assert rs.read_int(pc.NET_CLASS_BITS_EVENT) == pc.EVENT_CLASS_IDS["LightningStrikeEvent"]


def test_static_brick_data_event_roundtrip():
    """StaticBrickDataEvent (classId 13) decode consumes the exact bit layout:
    64*(4*readFloat(8)) + 16*(readInt(6)+readString) + readInt(10) N + N*readString.

    The palette is 64 rows (EXE loop @0x4a0910: edi 0x66b784..0x66bb84 step
    0x10), not 16 -- the old 16-row value silently truncated every packet
    carrying this event.
    """
    em = EventManager()
    bs = BitStream()
    bs.set_string_buffer(bytearray(256))
    for _ in range(64):
        for _ in range(4):
            bs.write_int(123, 8)
    for i in range(16):
        bs.write_int(i % 64, 6)
        bs.write_string(f"cat{i}")
    bs.write_int(2, 10)  # N
    bs.write_string("brickA")
    bs.write_string("brickB")
    bs.write_int(0b101, 3)  # sentinel
    total = bs.get_bit_position()
    rs = BitStream(bs.get_bytes())
    rs.set_string_buffer(bytearray(256))
    em._read_static_brick_data_event(rs)
    assert rs.read_int(3) == 0b101  # sentinel intact -> exact bit count consumed
    assert not rs.error


def test_sim_datablock_event_empty_present_flag():
    """A SimDataBlockEvent with present-flag 0 consumes exactly one bit and does
    not raise (no datablock payload to decode)."""
    em = EventManager()
    bs = BitStream()
    bs.write_flag(False)   # not present
    bs.write_int(0b11, 2)  # sentinel
    rs = BitStream(bs.get_bytes())
    em._read_sim_datablock_event(rs)
    assert rs.read_int(2) == 0b11


def test_sim_datablock_event_present_raises_with_classid():
    """A present SimDataBlockEvent decodes the envelope then raises
    EventDecodeError (per-class unpackData not ported)."""
    from aotbot.events import EventDecodeError
    import pytest
    em = EventManager()
    bs = BitStream()
    bs.write_flag(True)               # present
    bs.write_int(5, 10)               # id - 3
    bs.write_int(8, pc.NET_CLASS_BITS_DATABLOCK)   # datablock classId (FlyingVehicleData, no decoder)
    bs.write_int(0, 10)               # index
    bs.write_int(1, 11)               # total
    rs = BitStream(bs.get_bytes())
    with pytest.raises(EventDecodeError):
        em._read_sim_datablock_event(rs)


def test_ghost_always_object_event_flag_set_reads_classid_and_unpack_update():
    """GhostAlwaysObjectEvent (AoT @ VA 0x5496a0): readInt(14) id + readFlag;
    if the flag is set, readClassId(Object)=6 bits THEN the object's initial
    unpackUpdate (slot 0x4c) follows -- the object state is packed in the event.
    Using StaticShape (classId 31) with an all-zero update mask, the decoder must
    consume 14+1+6 + (StaticShape unpackUpdate bits) and stay aligned."""
    em = EventManager()
    bs = BitStream()
    bs.write_int(16319, pc.GHOST_ID_BIT_SIZE)        # ghost id (14 bits)
    bs.write_flag(True)                              # hasClassId
    bs.write_int(31, pc.NET_CLASS_BITS_OBJECT)       # classId 31 == StaticShape
    # StaticShape::unpackUpdate, everything clear: GameBase pos flag(0) + GameBase
    # datablock flag(0); ShapeBase master flag(0); StaticShape box/point flag(0);
    # StaticShape bool(0) = 5 bits.
    for _ in range(5):
        bs.write_flag(False)
    bs.write_int(0b101, 3)                           # sentinel after
    rs = BitStream(bs.get_bytes())
    em._read_ghost_always_object_event(rs)
    assert rs.get_bit_position() == pc.GHOST_ID_BIT_SIZE + 1 + pc.NET_CLASS_BITS_OBJECT + 5
    assert rs.read_int(3) == 0b101


def test_ghost_always_object_event_flag_clear_no_classid():
    """When the flag is clear the engine creates by name -- NO classId is read,
    so the decoder consumes only 14+1 = 15 bits."""
    em = EventManager()
    bs = BitStream()
    bs.write_int(42, pc.GHOST_ID_BIT_SIZE)
    bs.write_flag(False)                             # hasClassId = 0
    bs.write_int(0b11, 2)                            # sentinel
    rs = BitStream(bs.get_bytes())
    em._read_ghost_always_object_event(rs)
    assert rs.get_bit_position() == pc.GHOST_ID_BIT_SIZE + 1
    assert rs.read_int(2) == 0b11
