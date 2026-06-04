"""Unit tests for the GameConnection body: control header, ghost section,
mission-phase ack flow, and the full body round-trip.
"""

import json
import os

import pytest

import aotbot.protocol_constants as pc
from aotbot.bitstream import BitStream
from aotbot.events import EventManager
from aotbot.phases import (
    AlignmentError,
    GameConnectionPhases,
    MissionState,
    MOVE_COUNT_BITS,
    MAX_MOVE_COUNT,
    MAX_TRIGGER_KEYS,
    PACKED_MOVE_CENTER,
)


def _make(track_objects=False):
    return GameConnectionPhases(
        EventManager(), skip_lighting=True, track_objects=track_objects
    )


def _read_idle_move(rs: BitStream) -> None:
    """Consume one idle Move::pack body (server-read perspective): 3 zero
    rotation flags, px/py/pz (6 bits each, center), freeLook + 6 trigger flags.
    """
    for _ in range(3):
        assert rs.read_flag() is False        # rotation-present flag (no angle)
    assert rs.read_int(6) == PACKED_MOVE_CENTER  # px
    assert rs.read_int(6) == PACKED_MOVE_CENTER  # py
    assert rs.read_int(6) == PACKED_MOVE_CENTER  # pz
    assert rs.read_flag() is False            # freeLook
    for _ in range(MAX_TRIGGER_KEYS):
        assert rs.read_flag() is False        # trigger[i]


def test_write_control_header_minimal():
    """AoT client->server control header has a SINGLE trailing flag (fov), not
    two -- the stock firstPerson flag is dropped on the write side too (EXE
    GameConnection::writePacket @ VA 0x458710). Writing two flags here put one
    extra bit on the wire and desynced the server's read of our packet body.

    The header carries a NON-EMPTY idle move stream: the real client sends >=1
    Move in every data packet (verified across all 1077 c2s packets in
    real_login.jsonl), and the AoT server drops our events if moveCount==0.
    """
    p = GameConnectionPhases(EventManager(), skip_lighting=True, moves_per_packet=3)
    bs = BitStream()
    p._write_control_header(bs)
    total = bs.get_bit_position()
    rs = BitStream(bs.get_bytes())
    assert rs.read_flag() is True       # cameraPos == 0
    assert rs.read_int(32) == 0         # control-object checksum
    assert rs.read_int(32) == 0         # startMoveId (first packet)
    count = rs.read_int(MOVE_COUNT_BITS)
    assert count == 3                   # idle move stream (never 0)
    for _ in range(count):
        _read_idle_move(rs)
    assert rs.read_flag() is False      # fov flag (single trailing flag)
    # 1 + 32 + 32 + 5 + 3*28 (moves) + 1 = 155 bits; no phantom firstPerson flag.
    assert total == 1 + 32 + 32 + 5 + count * 28 + 1
    # startMoveId tracks the SERVER's move-ack (mLastMoveAck), like the real
    # client's moveWritePacket. With no server ack yet, start stays 0 and the
    # unacked-move window grows (count = 6 next packet) -- exactly the capture's
    # behaviour (startMoveId 0,0,2,3,5,... follows mLastMoveAck, NOT a blind
    # +count counter). Once the server acks moves (last_move_ack advances), start
    # follows it and the window shrinks.
    bs2 = BitStream()
    p._write_control_header(bs2)
    rs2 = BitStream(bs2.get_bytes())
    rs2.read_flag(); rs2.read_int(32)
    assert rs2.read_int(32) == 0        # start = server move-ack (still 0)
    assert rs2.read_int(MOVE_COUNT_BITS) == 6   # unacked window grew to 6
    # Simulate the server acking 4 moves: start jumps to 4, window shrinks.
    p.last_move_ack = 4
    bs3 = BitStream()
    p._write_control_header(bs3)
    rs3 = BitStream(bs3.get_bytes())
    rs3.read_flag(); rs3.read_int(32)
    assert rs3.read_int(32) == 4        # start = last_move_ack
    assert rs3.read_int(MOVE_COUNT_BITS) == (9 - 4)  # generated 9, acked 4 -> 5


def test_write_moves_caps_unacked_window_at_max_move_count():
    """When the server never acks our moves (no control object while logged out),
    the unacked window must cap at MaxMoveCount=30 and start must advance so we
    never claim to resend more than 30 moves -- mirroring the engine's
    moveWritePacket (count = min(size, MaxMoveCount); start += offset). A blind
    ever-growing count would overflow the 5-bit MoveCountBits field."""
    p = GameConnectionPhases(EventManager(), skip_lighting=True, moves_per_packet=3)
    p.last_move_ack = 0  # server never acks
    last_start = -1
    for _ in range(50):  # generate 150 moves with zero acks
        bs = BitStream()
        p._write_moves(bs)
        rs = BitStream(bs.get_bytes())
        start = rs.read_int(32)
        count = rs.read_int(MOVE_COUNT_BITS)
        assert count <= MAX_MOVE_COUNT          # never exceeds the 5-bit field
        assert start >= last_start              # start only advances
        last_start = start
    # After the window saturates, start tracks (generated - MaxMoveCount).
    assert count == MAX_MOVE_COUNT
    assert start == p._next_move_id - MAX_MOVE_COUNT


def test_read_control_header_all_zero_flags():
    """A pre-login server control header (moveAck + 4 zero flags -- AoT dropped
    the stock firstPerson flag) parses with no alignment error and consumes
    exactly the right bits."""
    p = _make()
    bs = BitStream()
    bs.write_int(7, 32)   # mLastMoveAck
    bs.write_flag(False)  # damage
    bs.write_flag(False)  # control
    bs.write_flag(False)  # camera
    bs.write_flag(False)  # fov
    rs = BitStream(bs.get_bytes())
    p._read_control_header(rs)
    assert p.last_move_ack == 7
    # Exactly 32 + 4 bits consumed (no phantom firstPerson flag).
    assert rs.get_bit_position() == 36


def test_read_control_header_control_object_reads_packet_data():
    # AoT's control-object update branch reads a 14-bit ghost id then the control
    # object's ShapeBase::readPacketData (@ VA 0x47e210 = two raw 4-byte reads,
    # 8 bytes). It must consume exactly those bits, NOT raise.
    p = _make()
    bs = BitStream()
    bs.write_int(7, 32)   # mLastMoveAck
    bs.write_flag(False)  # damage
    bs.write_flag(True)   # control flag set
    bs.write_flag(True)   # control-object update branch
    bs.write_int(3, pc.GHOST_ID_BIT_SIZE)
    bs.write_bytes(b"\x00" * 8)  # readPacketData: 2 x read(4)
    bs.write_flag(False)  # camera flag
    bs.write_flag(False)  # fov flag
    rs = BitStream(bs.get_bytes())
    p._read_control_header(rs)
    # 32 + damage(1) + control(1) + inner(1) + 14 + 64 + camera(1) + fov(1) = 115.
    assert rs.get_bit_position() == 32 + 1 + 1 + 1 + pc.GHOST_ID_BIT_SIZE + 64 + 1 + 1
    assert not rs.error


def test_read_control_header_no_control_object_reads_camera_point():
    # Control flag set, inner flag CLEAR -> the server sends the camera position
    # as a full Point3F (3 x read(4) = 12 bytes), NOT a compressed point.
    p = _make()
    bs = BitStream()
    bs.write_int(0, 32)
    bs.write_flag(False)  # damage
    bs.write_flag(True)   # control flag set
    bs.write_flag(False)  # inner flag clear -> camera Point3F
    bs.write_bytes(b"\x00" * 12)
    bs.write_flag(False)  # camera flag
    bs.write_flag(False)  # fov flag
    rs = BitStream(bs.get_bytes())
    p._read_control_header(rs)
    assert rs.get_bit_position() == 32 + 1 + 1 + 1 + 96 + 1 + 1
    assert not rs.error


def test_read_ghost_section_empty_ok():
    p = _make()
    bs = BitStream()
    bs.write_flag(False)  # no ghosts
    rs = BitStream(bs.get_bytes())
    p._read_ghost_section(rs)  # no raise


def test_read_ghost_section_empty_when_not_ghosting():
    """AoT's ghostReadPacket reads ZERO bits while mGhosting is off (the
    connect->login window), so the ghost section consumes nothing."""
    p = _make()
    assert p.ghosting_active is False
    bs = BitStream()
    bs.write_flag(True)  # would be ghost data, but we should not read it
    rs = BitStream(bs.get_bytes())
    p._read_ghost_section(rs)  # no raise, no read
    assert rs.get_bit_position() == 0


def test_read_ghost_section_empty_presence_consumes_one_bit():
    # ghosting active but the presence flag is 0 -> the section is empty (no
    # ghost updates), consuming exactly one bit and NOT raising. (Tracking ON so
    # the ghost section is actually decoded; OFF it is skipped by design.)
    p = _make(track_objects=True)
    p.ghosting_active = True
    bs = BitStream()
    bs.write_flag(False)  # no ghost updates this packet
    rs = BitStream(bs.get_bytes())
    p._read_ghost_section(rs)
    assert rs.get_bit_position() == 1
    assert not rs.error


def test_read_ghost_section_unported_class_raises():
    # presence=1, idSize=readInt(4)+3, one ghost present (flag 1), id, not a
    # remove (flag 0), NEW ghost -> readClassId(6) selects an object class with
    # no ported unpackUpdate -> AlignmentError carrying the class.
    p = _make(track_objects=True)
    p.ghosting_active = True
    bs = BitStream()
    bs.write_flag(True)        # presence
    bs.write_int(0, 4)         # idSize = 0+3 = 3
    bs.write_flag(True)        # this ghost present
    bs.write_int(5, 3)         # ghost id (3-bit)
    bs.write_flag(False)       # not a remove
    bs.write_int(5, 6)         # classId 5 == "FlyingVehicle" (no unpackUpdate yet)
    rs = BitStream(bs.get_bytes())
    with pytest.raises(AlignmentError):
        p._read_ghost_section(rs)


def test_read_ghost_section_ported_class_decodes():
    # A NEW StaticShape ghost (classId 31) with an all-zero unpackUpdate (every
    # mask flag clear) decodes without raising and consumes the right bits.
    p = _make(track_objects=True)
    p.ghosting_active = True
    bs = BitStream()
    bs.write_flag(True)        # presence
    bs.write_int(0, 4)         # idSize = 3
    bs.write_flag(True)        # ghost present
    bs.write_int(5, 3)         # ghost id
    bs.write_flag(False)       # not a remove
    bs.write_int(31, 6)        # classId 31 == "StaticShape"
    # StaticShape::unpackUpdate with everything clear:
    #   ShapeBase: GameBase pos flag(0); GameBase datablock flag(0); master(0).
    #   StaticShape: flag(0) (no box/point); flag(0) (static bool).
    bs.write_flag(False)       # GameBase pos mask
    bs.write_flag(False)       # GameBase datablock mask
    bs.write_flag(False)       # ShapeBase master mask
    bs.write_flag(False)       # StaticShape box/point flag
    bs.write_flag(False)       # StaticShape bool
    bs.write_flag(False)       # end of ghost loop
    rs = BitStream(bs.get_bytes())
    p._read_ghost_section(rs)  # no raise
    assert not rs.error


def test_read_body_server_to_client_empty():
    """A minimal server->client body (control header all-zero flags + empty
    event section + empty ghost flag) parses cleanly and consumes every bit.

    Note the WRITE side emits the client->server control header (a different
    layout: cameraPos flag + checksum + moveWritePacket), so this constructs the
    server->client form explicitly rather than round-tripping write_packet_body.
    """
    receiver = _make()
    bs = BitStream()
    # server->client control header (AoT): moveAck + 4 zero flags (damage,
    # control, camera, fov -- no firstPerson flag in this fork).
    bs.write_int(11, 32)
    for _ in range(4):
        bs.write_flag(False)
    # empty event section (end-unguaranteed, end-guaranteed, terminator).
    bs.write_flag(False)
    bs.write_flag(False)
    bs.write_flag(False)
    # empty ghost section: AoT reads ZERO bits while not ghosting, so we write
    # nothing here.

    total_bits = bs.get_bit_position()
    rs = BitStream(bs.get_bytes())
    receiver.read_packet_body(rs)
    assert receiver.last_move_ack == 11
    # Everything was consumed (only sub-byte padding may remain).
    assert total_bits - rs.get_bit_position() < 8
    assert not rs.error


def test_write_packet_body_client_to_server_parses_back():
    """The client->server body we emit can be parsed by a matching server-side
    reader (control header + move list + event + ghost flag)."""
    sender = GameConnectionPhases(EventManager(), skip_lighting=True, moves_per_packet=2)
    bs = BitStream()
    sender.write_packet_body(bs, send_seq=1)
    rs = BitStream(bs.get_bytes())
    rs.set_string_buffer(bytearray(256))   # the body installs a string buffer
    assert rs.read_flag() is True          # cameraPos flag
    assert rs.read_int(32) == 0            # checksum
    assert rs.read_int(32) == 0            # startMoveId
    count = rs.read_int(MOVE_COUNT_BITS)
    assert count == 2                      # idle move stream (never 0)
    for _ in range(count):
        _read_idle_move(rs)
    assert rs.read_flag() is False         # fov (single trailing flag)
    # event section: three zero flags (empty).
    assert rs.read_flag() is False
    assert rs.read_flag() is False
    assert rs.read_flag() is False
    # ghost flag.
    assert rs.read_flag() is False


def test_login_packet_matches_real_client_structure():
    """The bot's login DataPacket body must match the genuine client's wire
    structure (tools/captures/real_login.jsonl, c2s seq=92): a non-empty idle
    move stream, then NetStringEvent teaching the 'login' verb tag, then a
    RemoteCommandEvent argc=3 [TagString(verb), CString(user), Integer(crc)].

    Before the fix the bot wrote moveCount=0; the AoT server acked the packet
    but ran no serverCmd*, so login never completed.
    """
    from aotbot.crc import get_string_crc

    em = EventManager()
    ph = GameConnectionPhases(em, skip_lighting=True, moves_per_packet=2)
    em.command_to_server("login", "Mr Poopy Butthole", get_string_crc("poopy"))
    bs = BitStream()
    ph.write_packet_body(bs, send_seq=1)

    # Decode it the way the AoT server reads our packet body.
    rs = BitStream(bs.get_bytes())
    rs.set_string_buffer(bytearray(256))
    assert rs.read_flag() is True                 # cameraPos flag
    assert rs.read_int(32) == 0                    # checksum
    assert rs.read_int(32) == 0                    # startMoveId
    count = rs.read_int(MOVE_COUNT_BITS)
    assert count == 2 and count >= 1               # NON-EMPTY move stream
    for _ in range(count):
        _read_idle_move(rs)
    assert rs.read_flag() is False                 # fov flag

    # Event section decoded by a fresh "server" EventManager.
    server = EventManager()
    seen = []
    server.set_default_handler(
        lambda v, a, e: seen.append(("cmd", v, [server.detag(x) for x in a]))
    )
    orig_ns = server._read_net_string_event

    def ns(b):
        slot = b.read_int(pc.STRING_TABLE_ENTRY_BIT_SIZE)
        text = b.read_string()
        server.recv_table.map_string(slot, text)
        seen.append(("NS", slot, text))

    server._read_net_string_event = ns  # type: ignore
    server.read_events(rs)

    assert ("NS", 0, "login") in seen
    assert ("cmd", "login", ["Mr Poopy Butthole", "433638644"]) in seen
    assert not rs.error


def test_phase1_acks_phase1_only():
    """clientCmdMissionStartPhase1 should queue ONLY Phase1Ack (the skip-lighting
    Phase2/3 acks happen in the Phase2 handler, mirroring the engine path that
    fires onPhase1Complete from clientCmdMissionStartPhase2)."""
    sender = EventManager()    # acts as the "server" decoding our acks
    p = GameConnectionPhases(EventManager(), skip_lighting=True)

    sender.command_to_server("MissionStartPhase1", 1)
    bs = BitStream()
    sender.write_events(bs, current_send_seq=1)
    p.events.read_events(BitStream(bs.get_bytes()))

    assert p.state == MissionState.PHASE1_DONE
    joined = " ".join(ev.description for ev in p.events._out_queue)
    assert "MissionStartPhase1Ack" in joined
    assert "MissionStartPhase2Ack" not in joined
    assert "MissionStartPhase3Ack" not in joined


def test_phase2_acks_and_fires_eager_login():
    """clientCmdMissionStartPhase2 (skip-lighting) queues ONLY Phase2Ack and fires
    the eager-login hook (mirrors the real client's onPhase1Complete). It does NOT
    pre-ack Phase3 -- the real client acks Phase3 only after ReadyForNormalGhosts;
    with the ghost-always burst now completed correctly the server sends a real
    clientCmdMissionStartPhase3 that _on_phase3 acks."""
    sender = EventManager()
    p = GameConnectionPhases(EventManager(), skip_lighting=True)
    eager = []
    p.on_phase2_acked = lambda: eager.append(True)

    sender.command_to_server("MissionStartPhase2", 1)
    bs = BitStream()
    sender.write_events(bs, current_send_seq=1)
    p.events.read_events(BitStream(bs.get_bytes()))

    joined = " ".join(ev.description for ev in p.events._out_queue)
    assert "MissionStartPhase2Ack" in joined
    assert "MissionStartPhase3Ack" not in joined
    assert eager == [True]


def test_phase2_no_eager_phase3_when_not_skip_lighting():
    """With skip_lighting False we do NOT pre-ack Phase3 -- we wait for the real
    clientCmdMissionStartPhase3."""
    sender = EventManager()
    p = GameConnectionPhases(EventManager(), skip_lighting=False)
    sender.command_to_server("MissionStartPhase2", 1)
    bs = BitStream()
    sender.write_events(bs, current_send_seq=1)
    p.events.read_events(BitStream(bs.get_bytes()))
    joined = " ".join(ev.description for ev in p.events._out_queue)
    assert "MissionStartPhase2Ack" in joined
    assert "MissionStartPhase3Ack" not in joined


def test_phase3_acks_on_phase3_arrival():
    """Phase3Ack is sent only when clientCmdMissionStartPhase3 actually arrives."""
    sender = EventManager()
    p = GameConnectionPhases(EventManager(), skip_lighting=True)
    sender.command_to_server("MissionStartPhase3", 1)
    bs = BitStream()
    sender.write_events(bs, current_send_seq=1)
    p.events.read_events(BitStream(bs.get_bytes()))
    joined = " ".join(ev.description for ev in p.events._out_queue)
    assert "MissionStartPhase3Ack" in joined


def test_mission_start_enters_ingame_and_fires_hook():
    fired = []
    p = GameConnectionPhases(EventManager(), skip_lighting=True)
    p.on_ingame = lambda: fired.append(True)

    sender = EventManager()
    sender.command_to_server("MissionStart", 1)
    bs = BitStream()
    sender.write_events(bs, current_send_seq=1)
    p.events.read_events(BitStream(bs.get_bytes()))

    assert p.state == MissionState.INGAME_LOGGEDOUT
    assert fired == [True]


def test_phase_acks_echo_seq():
    p = GameConnectionPhases(EventManager(), skip_lighting=False)
    sender = EventManager()
    sender.command_to_server("MissionStartPhase2", 42)
    bs = BitStream()
    sender.write_events(bs, current_send_seq=1)
    p.events.read_events(BitStream(bs.get_bytes()))
    # Phase2Ack should echo seq 42.
    descs = " ".join(ev.description for ev in p.events._out_queue)
    assert "MissionStartPhase2Ack" in descs
    assert "42" in descs


def test_datablocks_done_triggers_download_done_reply():
    """Receiving a DataBlocksDone(6) connection message queues a
    DataBlocksDownloadDone(7) ConnectionMessageEvent back to the server."""
    from aotbot.phases import (
        MSG_DATABLOCKS_DONE,
        MSG_DATABLOCKS_DOWNLOAD_DONE,
        CONNECTION_MSG_BITS,
        GHOST_COUNT_BITS,
    )
    import aotbot.protocol_constants as pc
    p = _make()
    # Simulate the server sending a DataBlocksDone connection message.
    sender = EventManager()

    def write_connmsg(bs):
        bs.write_int(42, 32)                      # sequence
        bs.write_int(MSG_DATABLOCKS_DONE, CONNECTION_MSG_BITS)
        bs.write_int(0, GHOST_COUNT_BITS)         # ghostCount

    sender._enqueue(
        pc.EVENT_CLASS_IDS["ConnectionMessageEvent"], write_connmsg,
        description="DataBlocksDone",
    )
    bs = BitStream()
    sender.write_events(bs, current_send_seq=1)
    p.events.read_events(BitStream(bs.get_bytes()))

    assert p.datablocks_done is True
    descs = " ".join(ev.description for ev in p.events._out_queue)
    assert f"msg={MSG_DATABLOCKS_DOWNLOAD_DONE}" in descs


def test_ghost_always_starting_activates_ghosting():
    from aotbot.phases import MSG_GHOST_ALWAYS_STARTING
    p = _make()
    assert p.ghosting_active is False
    p._on_connection_message(MSG_GHOST_ALWAYS_STARTING, 7, 5)
    assert p.ghosting_active is True


def test_ready_for_normal_ghosts_gated_on_ghost_always_starting():
    """ReadyForNormalGhosts is only sent for a GhostAlwaysDone that FOLLOWS a real
    GhostAlwaysStarting (carrying the live mGhostingSequence). The server emits a
    spurious GhostAlwaysDone BEFORE GhostAlwaysStarting; the real client ignores it
    (real_login3.jsonl c2s sends exactly one ReadyForNormalGhosts). Replying to the
    early one is a stray event the genuine client never puts on the wire."""
    from aotbot.phases import (
        MSG_GHOST_ALWAYS_STARTING, MSG_GHOST_ALWAYS_DONE, MSG_READY_FOR_NORMAL_GHOSTS,
    )
    p = _make()
    # Pre-GhostAlwaysStarting GhostAlwaysDone -> NO ReadyForNormalGhosts queued.
    p.ghosting_active = True  # even if some earlier state set this
    p._on_connection_message(MSG_GHOST_ALWAYS_DONE, 0, 0)
    descs = " ".join(ev.description for ev in p.events._out_queue)
    assert f"msg={MSG_READY_FOR_NORMAL_GHOSTS}" not in descs
    # After a real GhostAlwaysStarting(seq=1), a GhostAlwaysDone DOES reply, echoing
    # the GhostAlwaysStarting sequence (NOT the Done's own sequence field).
    p._on_connection_message(MSG_GHOST_ALWAYS_STARTING, 1, 304)
    p._on_connection_message(MSG_GHOST_ALWAYS_DONE, 999, 0)
    descs = " ".join(ev.description for ev in p.events._out_queue)
    assert f"msg={MSG_READY_FOR_NORMAL_GHOSTS}, seq=1" in descs


def test_ready_for_normal_ghosts_on_burst_idle():
    """maybe_send_ready_for_normal_ghosts() sends ReadyForNormalGhosts once the
    ghost-always burst has been idle for ghost_always_idle_timeout, echoing the
    GhostAlwaysStarting sequence. This is the headless replacement for the stock
    GhostAlwaysDone-gated reply: the AoT server never sends a post-stream
    GhostAlwaysDone to a headless client, so the bot detects burst completion by
    idle and replies itself (LIVE-confirmed to unblock the 213-ghost stall ->
    Phase3 -> MissionStart -> the login response)."""
    from aotbot.phases import MSG_GHOST_ALWAYS_STARTING, MSG_READY_FOR_NORMAL_GHOSTS
    import time as _time
    p = _make()
    p.ghost_always_idle_timeout = 0.05
    p._on_connection_message(MSG_GHOST_ALWAYS_STARTING, 1, 304)
    # Simulate a few scoped ghost-always objects (resets the idle timer).
    for gid in range(3):
        p._on_ghost_scoped(16383 - gid, 9)
    # Not yet idle -> no ReadyForNormalGhosts.
    p.maybe_send_ready_for_normal_ghosts()
    descs = " ".join(ev.description for ev in p.events._out_queue)
    assert f"msg={MSG_READY_FOR_NORMAL_GHOSTS}" not in descs
    # After the idle window elapses -> exactly one ReadyForNormalGhosts(seq=1).
    _time.sleep(0.06)
    p.maybe_send_ready_for_normal_ghosts()
    descs = " ".join(ev.description for ev in p.events._out_queue)
    assert f"msg={MSG_READY_FOR_NORMAL_GHOSTS}, seq=1" in descs
    # Idempotent: a second call does not queue another.
    n = sum(1 for ev in p.events._out_queue
            if f"msg={MSG_READY_FOR_NORMAL_GHOSTS}" in ev.description)
    p.maybe_send_ready_for_normal_ghosts()
    n2 = sum(1 for ev in p.events._out_queue
             if f"msg={MSG_READY_FOR_NORMAL_GHOSTS}" in ev.description)
    assert n == n2 == 1


def test_ready_for_normal_ghosts_not_before_starting():
    """maybe_send_ready_for_normal_ghosts() does nothing before GhostAlwaysStarting
    or with no ghost-always objects received."""
    from aotbot.phases import MSG_READY_FOR_NORMAL_GHOSTS
    import time as _time
    p = _make()
    p.ghost_always_idle_timeout = 0.01
    _time.sleep(0.02)
    p.maybe_send_ready_for_normal_ghosts()
    descs = " ".join(ev.description for ev in p.events._out_queue)
    assert f"msg={MSG_READY_FOR_NORMAL_GHOSTS}" not in descs


def test_read_compressed_point_full_precision():
    """_read_compressed_point: type 3 -> 2 bits + 3 x F32 (96 bits)."""
    from aotbot.phases import _read_compressed_point
    bs = BitStream()
    bs.write_int(3, 2)            # full-precision type
    bs.write_bytes(b"\x00" * 12)  # 3 x F32
    bs.write_int(0b1, 1)          # sentinel
    rs = BitStream(bs.get_bytes())
    _read_compressed_point(rs)
    assert rs.get_bit_position() == 2 + 96
    assert rs.read_int(1) == 0b1


def test_read_compressed_point_quantised():
    """Types 0/1/2 -> 2 bits + 3 x readSignedInt(gBitCounts[type])."""
    from aotbot.phases import _read_compressed_point, COMPRESSED_POINT_BIT_COUNTS
    bs = BitStream()
    bs.write_int(1, 2)  # type 1 -> 18-bit signed each
    for _ in range(3):
        bs.write_signed_int(3, COMPRESSED_POINT_BIT_COUNTS[1])
    bs.write_int(0b1, 1)  # sentinel
    rs = BitStream(bs.get_bytes())
    _read_compressed_point(rs)
    # 2 type bits + 3 * 18 bits = 56 bits.
    assert rs.get_bit_position() == 2 + 3 * 18
    assert rs.read_int(1) == 0b1


# --------------------------------------------------------------------------- #
# Silent-misalignment regression guard (the telemetry-commit login regression).
#
# The b002722 telemetry commit added `if not self.track_objects: return` to
# `_read_ghost_section`, on the false premise that the ghost section is always
# strictly last and so skippable when telemetry is off. With tracking OFF (the
# LIVE default) that left the ghost-always burst's bits unconsumed and never
# populated `_ghost_classes` -> the s2c stream SILENTLY misaligned a few packets
# later (a misread bit landed on a decodable-looking value, no exception) ->
# garbage classId-15/6 events, and the login response (clientCmdLoginSuccess /
# clientCmdWarningBox) was NEVER decoded. The fix: ALWAYS decode the ghost
# section while ghosting is active; `track_objects` only gates BUILDING the
# registry, never alignment.
#
# These guards replay the golden captures with track_objects=False (== live) and
# assert the login response IS reached. A future re-introduction of any
# ghost-section / unpackUpdate misalignment that silently desyncs the stream
# would stop the replay before the login verb and fail here -- unlike a bare
# "clean packet count", which a silent misalignment passes.
# --------------------------------------------------------------------------- #

_CAPTURE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "tools", "captures"
)


def _read_packet_header(bs: BitStream):
    """Consume a NetConnection data-packet header (1|1|9|9|2|3|ackMask) and return
    (lastSendSeq, packetType)."""
    bs.read_flag()
    bs.read_int(1)
    seq = bs.read_int(9)
    bs.read_int(9)
    pt = bs.read_int(2)
    abc = bs.read_int(3)
    bs.read_int(8 * abc)
    return seq, pt


def _replay_to_verbs(capture_name, *, track_objects):
    """Replay a capture's s2c stream through the production read path and return
    (list of phase/login verbs seen in order, the blocking error str or None,
    the recorded ConnectionMessage (msg, seq) list).

    Mirrors tools/replay_s2c.py but records the verbs/ConnectionMessages needed
    for the value-sanity assertions below.
    """
    from aotbot.events import EventManager, EventDecodeError

    path = os.path.join(_CAPTURE_DIR, capture_name)
    recs = [json.loads(l) for l in open(path) if l.strip()]
    s2c = [r for r in recs if r["dir"] == "s2c"]

    em = EventManager()
    em.command_to_server = lambda *a, **k: None  # type: ignore
    ph = GameConnectionPhases(em, skip_lighting=True, track_objects=track_objects)
    ph._send_connection_message = lambda *a, **k: None  # type: ignore

    verbs = []
    for v in ("MissionStartPhase1", "MissionStartPhase2", "MissionStartPhase3",
              "MissionStart", "StartLogin", "LoginSuccess", "WarningBox"):
        em.on_client_cmd(
            v, (lambda vv: (lambda a, e: verbs.append(vv)))(v)
        )

    # Record ConnectionMessage (msg, seq) values as decoded by the REAL handler.
    cms = []
    real_cm = em.on_connection_message

    def cm_hook(message, sequence, ghost_count):
        cms.append((message, sequence))
        if real_cm is not None:
            real_cm(message, sequence, ghost_count)

    em.on_connection_message = cm_hook

    blocked = None
    last = -1
    for r in s2c:
        b = bytes.fromhex(r["hex"])
        if not b or not (b[0] & 1):
            continue
        bs = BitStream(b)
        seq, pt = _read_packet_header(bs)
        if pt != 0 or seq == last:
            continue
        last = seq
        if bs.read_flag():
            bs.read_int(10); bs.read_int(10)
        if bs.read_flag():
            bs.read_int(10); bs.read_int(10)
        try:
            ph.read_packet_body(bs)
        except (AlignmentError, EventDecodeError) as e:
            blocked = str(e)
            break
    return verbs, blocked, cms


@pytest.mark.skipif(
    not os.path.exists(os.path.join(_CAPTURE_DIR, "real_login.jsonl")),
    reason="golden valid-login capture missing",
)
def test_valid_login_capture_reaches_login_success_tracking_off():
    """LIVE default (track_objects=False): the real client's valid-login session
    MUST decode through the full ghost-always stream to clientCmdLoginSuccess.

    The telemetry-commit regression (ghost section skipped when tracking off)
    silently misaligned the stream and stopped before LoginSuccess; this guards
    it. Tracking is OFF on purpose -- that is what the bot runs."""
    verbs, blocked, _cms = _replay_to_verbs("real_login.jsonl", track_objects=False)
    assert "LoginSuccess" in verbs, (
        f"valid-login replay never reached LoginSuccess "
        f"(verbs={verbs}, blocked={blocked})"
    )


@pytest.mark.skipif(
    not os.path.exists(os.path.join(_CAPTURE_DIR, "bad_login.jsonl")),
    reason="bad-login capture missing",
)
def test_bad_login_capture_reaches_warning_box_tracking_off():
    """LIVE default (track_objects=False): the real client's bad-credential
    session MUST decode through the full load to clientCmdWarningBox ("Wrong
    Password!"). The login-failure-detection flow is gated on completing the
    full load, so a silent ghost-section misalignment hides the WarningBox."""
    verbs, blocked, _cms = _replay_to_verbs("bad_login.jsonl", track_objects=False)
    assert "WarningBox" in verbs, (
        f"bad-login replay never reached WarningBox "
        f"(verbs={verbs}, blocked={blocked})"
    )


@pytest.mark.skipif(
    not os.path.exists(os.path.join(_CAPTURE_DIR, "real_login.jsonl")),
    reason="golden valid-login capture missing",
)
def test_pre_ghost_connection_message_seqs_are_sane():
    """VALUE SANITY: every ConnectionMessage decoded BEFORE GhostAlwaysStarting
    (msg==3) -- i.e. the whole datablock-download phase -- must have a SMALL
    sequence (the real load uses single/low-digit seqs like the DataBlocksDone
    seq=1). A silent bit misalignment in the datablock/event stream blows these
    up to millions/billions. (Reads AFTER GhostAlwaysStarting can be polluted by
    the partially-decoded ghost burst and are intentionally not asserted here --
    the reach-the-login-verb guards above cover post-burst alignment.)"""
    _verbs, _blocked, cms = _replay_to_verbs("real_login.jsonl", track_objects=False)
    # Walk up to (and including) the first GhostAlwaysStarting; every CM seq up
    # to there must be small. GhostAlwaysStarting itself carries seq=1.
    for message, sequence in cms:
        assert sequence < 100_000, (
            f"insane pre-ghost ConnectionMessage seq={sequence} (msg={message}) "
            f"-- the s2c stream silently misaligned before GhostAlwaysStarting"
        )
        if message == 3:  # GhostAlwaysStarting -- end of the sane window
            break


@pytest.mark.skipif(
    not os.path.exists(os.path.join(_CAPTURE_DIR, "live_rain_freshacct.jsonl")),
    reason="live rain capture missing",
)
def test_rain_world_ghost_burst_decodes_without_misalignment():
    """The CURRENT (raining) world's GhostAlways burst MUST decode with ZERO
    AlignmentError -- the live login regression.

    live_rain_freshacct.jsonl is a fresh-account session captured from the bot
    against the live (raining) server. Its post-GhostAlwaysStarting burst is what
    silently misaligned after the telemetry commit: fxFoliageReplicator over-read
    by 4 bits and the spawner subclasses (DestructableSpawner/GoldSpawner/
    SpawnSphere/WayPoint) were decoded as the bare MissionMarker base, under-
    reading their tail. Either bug terminated the burst one object early -> a
    misread bit cascaded into garbage ConnectionMessage seqs (DataBlocksDone
    seq=2.7 billion) and the login response never decoded. This replays the whole
    capture with track_objects=False (live default) and asserts it never raises.
    """
    verbs, blocked, cms = _replay_to_verbs(
        "live_rain_freshacct.jsonl", track_objects=False
    )
    assert blocked is None, (
        f"rain-world ghost burst desynced: {blocked} (verbs={verbs})"
    )
    # VALUE SANITY: once GhostAlwaysStarting (msg==3) has been seen, the burst is
    # being consumed; a silent misalignment there blew the FOLLOWING
    # ConnectionMessage seqs up to millions/billions (e.g. DataBlocksDone
    # seq=2.7e9). Assert every ConnectionMessage seq AFTER GhostAlwaysStarting is
    # small. (Pre-burst download messages can legitimately carry a large seq and
    # are not part of the misalignment signature, so they are not asserted.)
    seen_start = False
    for message, sequence in cms:
        if message == 3:  # GhostAlwaysStarting
            seen_start = True
            continue
        if seen_start:
            assert sequence < 100_000, (
                f"insane post-burst ConnectionMessage seq={sequence} "
                f"(msg={message}) -- the rain-world ghost burst silently "
                f"misaligned"
            )
