"""Tests for live-entity telemetry: the DecodeSink, the ObjectRegistry, value
extraction in the ghost decoders, the on/off flag, and a capture-replay
regression that asserts value extraction stays bit-exact (the clean s2c count of
the golden captures must not regress)."""

import json
import os
import struct

import pytest

from aotbot.bitstream import BitStream
from aotbot import ghosts as gh
from aotbot import telemetry as tm
from aotbot.events import EventManager, EventDecodeError
from aotbot.phases import GameConnectionPhases, AlignmentError


# --------------------------------------------------------------------------- #
# DecodeSink + emit
# --------------------------------------------------------------------------- #


def test_sink_keeps_first_position():
    s = tm.DecodeSink()
    s.set("position", (1.0, 2.0, 3.0))
    s.set("position", (9.0, 9.0, 9.0))  # ignored -- outermost position wins
    assert s.fields["position"] == (1.0, 2.0, 3.0)


def test_sink_overwrites_datablock():
    s = tm.DecodeSink()
    s.set("datablock_id", 5)
    s.set("datablock_id", 7)
    assert s.fields["datablock_id"] == 7


def test_emit_noop_without_sink():
    tm.set_sink(None)
    tm.emit("position", (1, 2, 3))  # must not raise
    assert tm.active_sink() is None


def test_emit_point3f_unpacks():
    s = tm.DecodeSink()
    tm.set_sink(s)
    try:
        raw = struct.pack("<fff", 10.5, -20.25, 30.0)
        tm.emit_point3f("position", raw)
    finally:
        tm.set_sink(None)
    assert s.fields["position"] == pytest.approx((10.5, -20.25, 30.0))


# --------------------------------------------------------------------------- #
# Value extraction in the decoders (bits unchanged; values captured)
# --------------------------------------------------------------------------- #


def _decode_with_sink(class_name, write_fn, *, is_new=True):
    bs = BitStream()
    write_fn(bs)
    rs = BitStream(bs.get_bytes())
    sink = tm.DecodeSink()
    tm.set_sink(sink)
    try:
        gh.DECODERS[class_name](rs, is_new)
    finally:
        tm.set_sink(None)
    assert not rs.error
    return sink, rs.get_bit_position()


def test_game_base_scale_extracted():
    # GameBase's first masked Point3F is the object SCALE (handed to setScale), not
    # a world position.
    scale = struct.pack("<fff", 1.0, 1.05, 0.95)

    def w(bs):
        bs.write_flag(True)        # scale mask
        bs.write_bytes(scale)
        bs.write_flag(False)       # datablock mask clear

    sink, bits = _decode_with_sink("GameBase", w)
    assert bits == 1 + 96 + 1
    assert sink.fields["scale"] == pytest.approx((1.0, 1.05, 0.95))
    assert "position" not in sink.fields


def test_game_base_datablock_extracted():
    def w(bs):
        bs.write_flag(False)       # position mask clear
        bs.write_flag(True)        # datablock mask
        bs.write_int(12, 10)       # datablock id (stored +3)

    sink, bits = _decode_with_sink("GameBase", w)
    assert bits == 12
    assert sink.fields["datablock_id"] == 12 + 3


def test_player_scale_via_gamebase():
    # Player's ShapeBase parent GameBase Point3F is the player SCALE.
    scale = struct.pack("<fff", 1.0, 1.0, 1.0)

    def w(bs):
        bs.write_flag(True)        # GameBase scale mask
        bs.write_bytes(scale)
        bs.write_flag(False)       # GameBase datablock mask
        bs.write_flag(False)       # ShapeBase master mask -> done
        # Player block: 7 leading flags, all clear.
        for _ in range(7):
            bs.write_flag(False)

    sink, _ = _decode_with_sink("Player", w)
    assert sink.fields["scale"] == pytest.approx((1.0, 1.0, 1.0))


def test_ts_static_matrix_position():
    # TSStatic: parent(0) + 64-byte matrix + Point3F scale + shapeName. The matrix
    # translation (elements 3,7,11) is the world position.
    m = [0.0] * 16
    m[3], m[7], m[11] = 1400.0, 620.0, 223.0
    matrix = struct.pack("<16f", *m)

    def w(bs):
        bs.write_bytes(matrix)
        bs.write_bytes(b"\x00" * 12)   # scale
        bs.write_string("foo.dts")

    sink, _ = _decode_with_sink("TSStatic", w)
    assert sink.fields["position"] == pytest.approx((1400.0, 620.0, 223.0))
    # TSStatic carries its model file directly in unpackUpdate -> "shape_file".
    assert sink.fields["shape_file"] == "foo.dts"


# --------------------------------------------------------------------------- #
# ObjectRegistry
# --------------------------------------------------------------------------- #


def test_registry_resolves_shape_from_datablock():
    reg = tm.ObjectRegistry()
    reg.record_datablock(7, "PlayerData", shape_file="player.dts")
    sink = tm.DecodeSink()
    sink.set("datablock_id", 7)
    sink.set("position", (1.0, 2.0, 3.0))
    rec = reg.update_from_sink(100, "Player", sink, is_new=True)
    assert rec.shape_name == "player.dts"
    assert rec.position == (1.0, 2.0, 3.0)
    assert reg.get(100).class_name == "Player"


def test_registry_backfills_shape_when_datablock_arrives_later():
    reg = tm.ObjectRegistry()
    sink = tm.DecodeSink()
    sink.set("datablock_id", 9)
    reg.update_from_sink(101, "Item", sink, is_new=True)
    assert reg.get(101).shape_name is None
    reg.record_datablock(9, "ItemData", shape_file="crossbow.dts")
    assert reg.get(101).shape_name == "crossbow.dts"


def test_registry_control_object_and_removal():
    reg = tm.ObjectRegistry()
    reg.update_from_sink(5, "Player", tm.DecodeSink(), is_control=True)
    assert reg.control_ghost_id == 5
    assert reg.get(5).is_control_object
    reg.remove(5)
    assert reg.get(5).scoped is False
    assert reg.control_ghost_id is None
    # removed objects are excluded from the default listing.
    assert reg.list_objects() == []
    assert len(reg.list_objects(include_removed=True)) == 1


def test_record_to_dict_rounds_position():
    rec = tm.ObjectRecord(ghost_id=3, class_name="Player",
                          position=(1.123456789, 2.0, 3.0))
    d = rec.to_dict()
    assert d["ghost_id"] == 3
    assert d["position"][0] == pytest.approx(1.1235, abs=1e-4)


# --------------------------------------------------------------------------- #
# On/off flag behavior
# --------------------------------------------------------------------------- #


def test_ghost_section_decoded_for_alignment_when_tracking_off():
    # Tracking OFF: the ghost section is STILL decoded (bits consumed) when
    # ghosting is active -- required for alignment / _ghost_classes / login -- but
    # NO registry is built (that's the part the flag actually gates).
    scale = struct.pack("<fff", 1.0, 1.05, 0.95)
    p = GameConnectionPhases(EventManager(), skip_lighting=True, track_objects=False)
    p.ghosting_active = True
    bs = BitStream()
    bs.write_flag(True)            # presence
    bs.write_int(0, 4)             # idSize = 3
    bs.write_flag(True)            # ghost present
    bs.write_int(4, 3)             # ghost id
    bs.write_flag(False)           # not a remove
    bs.write_int(21, 6)            # classId 21 == Player
    bs.write_flag(True); bs.write_bytes(scale)  # GameBase scale block
    bs.write_flag(False)           # GameBase datablock flag
    bs.write_flag(False)           # ShapeBase master flag
    for _ in range(7):
        bs.write_flag(False)       # Player block flags
    bs.write_flag(False)           # end of ghost loop
    rs = BitStream(bs.get_bytes())
    p._read_ghost_section(rs)
    assert not rs.error
    assert rs.get_bit_position() > 0          # decoded for alignment (NOT skipped)
    assert 4 in p._ghost_classes              # ghost class recorded
    assert p.registry is None                 # but no registry built
    assert p.list_objects() == []


def test_ghost_section_decoded_when_tracking_on_populates_registry():
    p = GameConnectionPhases(EventManager(), skip_lighting=True, track_objects=True)
    p.ghosting_active = True
    scale = struct.pack("<fff", 1.0, 1.05, 0.95)
    bs = BitStream()
    bs.write_flag(True)            # presence
    bs.write_int(0, 4)             # idSize = 3
    bs.write_flag(True)            # ghost present
    bs.write_int(4, 3)             # ghost id
    bs.write_flag(False)           # not a remove
    bs.write_int(21, 6)            # classId 21 == Player
    # Player: GameBase scale(1)+Point3F, datablock(0), ShapeBase master(0),
    # then 7 clear Player block flags (minimal tail).
    bs.write_flag(True)
    bs.write_bytes(scale)
    bs.write_flag(False)
    bs.write_flag(False)
    for _ in range(7):
        bs.write_flag(False)
    bs.write_flag(False)           # end of ghost loop
    rs = BitStream(bs.get_bytes())
    p._read_ghost_section(rs)
    assert not rs.error
    objs = p.list_objects()
    assert len(objs) == 1
    assert objs[0]["ghost_id"] == 4
    assert objs[0]["class_name"] == "Player"
    assert objs[0]["scale"] == pytest.approx([1.0, 1.05, 0.95])


# --------------------------------------------------------------------------- #
# Capture-replay regression (bit-exactness guard)
# --------------------------------------------------------------------------- #

_HERE = os.path.dirname(__file__)
_CAP = os.path.join(_HERE, "..", "tools", "captures")


def _replay_clean_count(path):
    recs = [json.loads(l) for l in open(path) if l.strip()]
    s2c = [r for r in recs if r["dir"] == "s2c"]
    em = EventManager()
    em.command_to_server = lambda *a, **k: None
    ph = GameConnectionPhases(em, skip_lighting=True, track_objects=True)
    ph._send_connection_message = lambda *a, **k: None
    em._send_connection_message = lambda *a, **k: None

    def rh(bs):
        bs.read_flag(); bs.read_int(1); seq = bs.read_int(9)
        bs.read_int(9); pt = bs.read_int(2); abc = bs.read_int(3)
        bs.read_int(8 * abc); return seq, pt

    last = -1; ok = 0
    for r in s2c:
        b = bytes.fromhex(r["hex"])
        if not b or not (b[0] & 1):
            continue
        bs = BitStream(b); seq, pt = rh(bs)
        if pt != 0 or seq == last:
            continue
        last = seq
        if bs.read_flag():
            bs.read_int(10); bs.read_int(10)
        if bs.read_flag():
            bs.read_int(10); bs.read_int(10)
        try:
            ph.read_packet_body(bs)
            ok += 1
        except (AlignmentError, EventDecodeError):
            pass
    return ok, ph


@pytest.mark.parametrize("name,minimum", [
    ("real_login.jsonl", 1274),
    ("bad_login.jsonl", 2055),
    ("bot_session_postfix.jsonl", 2461),
    # live_session_dbg.jsonl: a fresh logged-in+spawned session whose post-login
    # datablock burst streams a populated ShapeBaseImageData (the equipped-image
    # state blocks). The Wave-18 SBID state-block fix makes the WHOLE stream decode
    # with ZERO blockers (was: desync at the SBID over-read -> event classId 15).
    ("live_session_dbg.jsonl", 1582),
])
def test_capture_replay_no_regression(name, minimum):
    path = os.path.join(_CAP, name)
    if not os.path.exists(path):
        pytest.skip(f"capture {name} not present")
    ok, ph = _replay_clean_count(path)
    # The decoders are bit-exact: the clean count must NOT regress below the
    # value validated when value extraction was added (it may only grow).
    assert ok >= minimum, f"{name}: clean count {ok} regressed below {minimum}"


def test_capture_replay_populates_objects_with_positions():
    # The bad-login capture reaches full login; tracking ON must yield a registry
    # of scoped objects, including some with real positions and shape names.
    path = os.path.join(_CAP, "bad_login.jsonl")
    if not os.path.exists(path):
        pytest.skip("bad_login.jsonl not present")
    _, ph = _replay_clean_count(path)
    objs = ph.list_objects()
    assert len(objs) > 0
    assert any(o["position"] is not None for o in objs)
    assert any(o["shape_name"] for o in objs)


def test_real_login_tracks_player_with_position_and_shape():
    # real_login.jsonl is the real client's logged-in + SPAWNED session: it scopes
    # the controlled-player Player, AIPlayer NPCs, Items, a Projectile. Tracking ON
    # must yield Player/AIPlayer records that carry a real world position AND a
    # datablock-resolved shape (e.g. female.dts / Orc.dts), and the bot's own
    # control object must be flagged is_control_object with a Player shape.
    path = os.path.join(_CAP, "real_login.jsonl")
    if not os.path.exists(path):
        pytest.skip("real_login.jsonl not present")
    _, ph = _replay_clean_count(path)
    objs = ph.list_objects()

    players = [o for o in objs if o["class_name"] == "Player"]
    aiplayers = [o for o in objs if o["class_name"] == "AIPlayer"]
    assert players, "expected at least one Player ghost"
    assert aiplayers, "expected at least one AIPlayer ghost"

    # The control object (the spawned bot's own player) is tracked + flagged.
    ctrl = [o for o in objs if o["is_control_object"]]
    assert ctrl, "expected the control object (own player) to be tracked"
    own = ctrl[0]
    assert own["class_name"] in ("Player", "AIPlayer")
    assert own["position"] is not None
    # shape_name is the human label: the in-game NAME if known (mShapeNameTag),
    # else the datablock model file. The control object resolves at least one.
    assert own["shape_name"]

    # Players/AIPlayers carry a real world position + either a name (mShapeNameTag)
    # or a datablock-resolved .dts model file (distinct fields).
    assert any(p["position"] is not None for p in players)
    assert any(
        p["name"] or (p["shape_file"] and p["shape_file"].endswith(".dts"))
        for p in players + aiplayers
    )


def test_live_session_player_name_rotation_and_control_position():
    # live_session_dbg.jsonl: the bot's own logged-in+spawned session, with the
    # user's "Jeff Bezos" standing nearby. Validates (against getShapeName /
    # getTransform ground truth) the Wave-18 telemetry fixes:
    #   * shapeName/name: ShapeBase mShapeNameTag resolved via the receive
    #     NetStringTable -> the player USERNAME ("Jeff Bezos", "Mr Poopy Butthole").
    #   * NO garbage positions (the quantised non-world compressed point is no
    #     longer surfaced as a position).
    #   * the control object (bot's own player) reports the deterministic spawn
    #     transform 281.797 175.591 213.218 to float precision.
    #   * rotation decodes to a Z-axis angle in getTransform's convention
    #     (Jeff Bezos true angle 0.637333).
    path = os.path.join(_CAP, "live_session_dbg.jsonl")
    if not os.path.exists(path):
        pytest.skip("live_session_dbg.jsonl not present")
    _, ph = _replay_clean_count(path)
    objs = ph.list_objects()
    by_name = {o["name"]: o for o in objs if o.get("name")}

    # Names resolved via the NetStringTable.
    assert "Jeff Bezos" in by_name, "expected the standing player 'Jeff Bezos'"
    assert "Mr Poopy Butthole" in by_name, "expected the bot's own player"

    jeff = by_name["Jeff Bezos"]
    assert jeff["class_name"] == "Player"
    # Jeff's name comes from mShapeNameTag, NOT his datablock .dts.
    assert jeff["shape_file"] and jeff["shape_file"].endswith(".dts")
    assert jeff["name"] != jeff["shape_file"]
    # Rotation decodes to the getTransform-convention Z angle ~0.637 (7-bit yaw
    # quant step ~0.05). His exact transform was 292.647 170.091 213.218 / 0.637333.
    assert isinstance(jeff["rotation"], dict)
    assert jeff["rotation"]["axis"] == [0.0, 0.0, 1.0]
    assert abs(jeff["rotation"]["angle"] - 0.637333) < 0.06

    # The control object (bot's own player) at the deterministic spawn transform.
    own = by_name["Mr Poopy Butthole"]
    assert own["is_control_object"]
    assert own["position"] is not None
    px, py, pz = own["position"]
    assert abs(px - 281.797) < 0.05
    assert abs(py - 175.591) < 0.05
    assert abs(pz - 213.218) < 0.05

    # NO garbage coordinates anywhere: every surfaced position is finite and within
    # a sane world envelope (the old desync produced values like -216312, 120717).
    for o in objs:
        p = o["position"]
        if p is not None:
            assert all(abs(c) < 1e5 for c in p), f"garbage position on {o}"


def test_real_login_fills_marker_and_light_positions():
    # The Box6F position fallback: marker/spawner/light/replicator classes that
    # carry no GameBase/controlled-pose Point3F still surface a position from the
    # leading Point3F of their Box6F field (world_box). Before this fix these were
    # all "?". The bit cursor is unchanged (guarded by the no-regression test).
    path = os.path.join(_CAP, "real_login.jsonl")
    if not os.path.exists(path):
        pytest.skip("real_login.jsonl not present")
    _, ph = _replay_clean_count(path)
    objs = ph.list_objects()
    by_class = {}
    for o in objs:
        by_class.setdefault(o["class_name"], []).append(o)
    # Each of these classes (when scoped) must now have at least one positioned
    # record (it did not before the Box6F-origin fallback).
    for cn in ("SpawnSphere", "DestructableSpawner", "RoomMarker", "volumeLight",
               "NPCSpawner", "fxFoliageReplicator", "fxGrassReplicator"):
        recs = by_class.get(cn)
        if not recs:
            continue
        assert any(r["position"] is not None for r in recs), \
            f"{cn} still has no position after the Box6F fallback"
