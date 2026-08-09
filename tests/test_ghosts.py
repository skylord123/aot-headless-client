"""Bit-exact unit tests for the per-class NetObject unpackUpdate decoders.

Each test writes the minimal "all masks clear" form of a class's unpackUpdate and
asserts the decoder consumes exactly the expected number of bits and stays
aligned (no error). The expected bit counts are derived from the CFG-followed
exe layouts documented in aotbot/ghosts.py / docs/re-deep-findings.md (Wave-9).
"""

import pytest

from aotbot.bitstream import BitStream
from aotbot import ghosts as gh


def _decode(class_name, write_fn, *, is_new=True):
    """Write a payload via write_fn(bs), then decode it with the named class's
    unpackUpdate and return the number of bits consumed."""
    bs = BitStream()
    write_fn(bs)
    rs = BitStream(bs.get_bytes())
    gh.DECODERS[class_name](rs, is_new)
    assert not rs.error
    return rs.get_bit_position()


def _roundtrip(class_name, write_fn, *, is_new=True):
    """Write a payload (with a per-packet string buffer installed, mirroring
    GameConnection::readPacket) and assert the named class's unpackUpdate consumes
    EXACTLY the bits the writer produced. Returns the bit count. Used for decoders
    that read strings (whose on-wire length depends on the dedup string buffer, so
    a hard-coded bit count is fragile -- the invariant we care about is that the
    decoder consumes precisely a faithful writer's output)."""
    bs = BitStream()
    bs.set_string_buffer(bytearray(256))
    write_fn(bs)
    written = bs.get_bit_position()
    rs = BitStream(bs.get_bytes())
    rs.set_string_buffer(bytearray(256))
    gh.DECODERS[class_name](rs, is_new)
    assert not rs.error
    assert rs.get_bit_position() == written, (
        f"{class_name}: decoder consumed {rs.get_bit_position()} bits, "
        f"writer produced {written}"
    )
    return written


# GameBase::unpackUpdate (0x456da0) reads TWO mask flags: a position mask
# (flag+Point3F) and a datablock-id mask (flag+readInt(10)+3). "all clear" = 2
# bits. ShapeBase (and every GameBase-derived class) calls the whole function as
# its parent, so its all-clear parent prefix is 2 GameBase flags + 1 master flag.
def test_game_base_all_clear():
    # GameBase: pos flag (0) + datablock flag (0) = 2 bits, no payload.
    def w(bs):
        bs.write_flag(False)
        bs.write_flag(False)
    assert _decode("GameBase", w) == 2


def test_game_base_with_point():
    # pos flag(1) + Point3F (96 bits) + datablock flag(0) = 98 bits.
    def w(bs):
        bs.write_flag(True)
        bs.write_bytes(b"\x00" * 12)
        bs.write_flag(False)
    assert _decode("GameBase", w) == 1 + 96 + 1


def test_game_base_with_datablock():
    # pos flag(0) + datablock flag(1) + readInt(10) = 1 + 1 + 10 = 12 bits.
    def w(bs):
        bs.write_flag(False)
        bs.write_flag(True)
        bs.write_int(7, 10)
    assert _decode("GameBase", w) == 12


def test_shape_base_all_clear():
    # GameBase pos(0) + datablock(0) + ShapeBase master flag(0) -> done. 3 bits.
    def w(bs):
        for _ in range(3):
            bs.write_flag(False)
    assert _decode("ShapeBase", w) == 3


def test_shape_base_sound_thread_play_polarity():
    # WAVE-23: the 4-slot SOUND-thread loop (0x4840f0..0x4841aa). The 10-bit
    # sound datablock id (0x484170) is read when the per-slot "play" flag
    # (setne @0x484147) is SET -- `cmp al,1; jne 0x48417f` @0x484150/0x484158
    # SKIPS the read when it is clear (TGE shapeBase.cc: `st.play = readFlag();
    # if (st.play) profile = readRangedU32(...)`). The prior transcription had
    # the polarity inverted, under-reading 10 bits per playing sound.
    #
    # Layout: GameBase pos(0)+datablock(0) + master(1) + damage(0) + thread(0)
    # + sound(1) + slot0[present(1) play(1) id(10)] + slot1[present(1) play(0)]
    # + slot2(0) + slot3(0) + image(0) + core(0) + mount(0) = 25 bits.
    def w(bs):
        bs.write_flag(False)          # GameBase pos mask
        bs.write_flag(False)          # GameBase datablock mask
        bs.write_flag(True)           # ShapeBase master mask (0x483e06)
        bs.write_flag(False)          # damage block (0x483e36)
        bs.write_flag(False)          # thread loop (0x483f13)
        bs.write_flag(True)           # sound loop (0x4840ce)
        bs.write_flag(True)           # slot 0 present (0x48411d)
        bs.write_flag(True)           # slot 0 play SET (0x484147)
        bs.write_int(0x2A7, 10)       # slot 0 sound datablock id (0x484170)
        bs.write_flag(True)           # slot 1 present
        bs.write_flag(False)          # slot 1 play CLEAR -> NO id read
        bs.write_flag(False)          # slot 2 absent
        bs.write_flag(False)          # slot 3 absent
        bs.write_flag(False)          # mounted-image loop (0x4841ea)
        bs.write_flag(False)          # core-state block (0x484572)
        bs.write_flag(False)          # mount block (0x484aa4)
    assert _decode("ShapeBase", w) == 25


def test_static_shape_all_clear():
    # ShapeBase (3 clear flags) + box/point flag(0) + static bool(0) = 5 bits.
    def w(bs):
        for _ in range(5):
            bs.write_flag(False)
    assert _decode("StaticShape", w) == 5


def test_camera_all_clear():
    # ShapeBase (3 clear flags: GameBase pos + datablock + master) + Camera
    # flag A(0) + Camera flag B(0) = 5 bits.
    def w(bs):
        for _ in range(5):
            bs.write_flag(False)
    assert _decode("Camera", w) == 5


def test_camera_flag_a_set():
    # ShapeBase (3 clear) + Camera flag A(1) -> END immediately = 4 bits.
    def w(bs):
        bs.write_flag(False)  # GameBase pos flag
        bs.write_flag(False)  # GameBase datablock flag
        bs.write_flag(False)  # ShapeBase master flag
        bs.write_flag(True)   # Camera flag A set -> end
    assert _decode("Camera", w) == 4


def test_camera_flag_b_set():
    # ShapeBase (3 clear) + flag A(0) + flag B(1) + 5 x read(4)=160 bits.
    def w(bs):
        for _ in range(3):
            bs.write_flag(False)  # GameBase pos + datablock + ShapeBase master
        bs.write_flag(False)  # flag A clear
        bs.write_flag(True)   # flag B set
        bs.write_bytes(b"\x00" * (5 * 4))
    assert _decode("Camera", w) == 5 + 5 * 32


def test_sun_all_clear():
    # Sun: single mask flag(0) -> 1 bit.
    assert _decode("Sun", lambda bs: bs.write_flag(False)) == 1


def test_sun_set():
    # flag(1) + Point3F(96) + 8 x read(4)=256 bits = 353 bits.
    def w(bs):
        bs.write_flag(True)
        bs.write_bytes(b"\x00" * (12 + 8 * 4))
    assert _decode("Sun", w) == 1 + 96 + 256


def test_simple_net_object():
    # one readString of empty string: write_string("") path.
    def w(bs):
        bs.write_string("")
    n = _decode("SimpleNetObject", w)
    assert n > 0  # consumed the string framing


def test_mission_marker_all_clear():
    # ShapeBase (3 clear flags) + MissionMarker flag(0) = 4 bits.
    def w(bs):
        for _ in range(4):
            bs.write_flag(False)
    assert _decode("MissionMarker", w) == 4


def test_player_all_clear():
    # ShapeBase (3 clear) + 6 leading Player block flags, the 6th (early-out)
    # being CLEAR so we fall to the pose flag (7th) which we also clear.
    #   ShapeBase: GameBase pos(0) GameBase datablock(0) master(0)
    #   Player: 0x46e6d8(0) 0x46e76d(0) 0x46e987(0) 0x46e9cb(0) 0x46ed05(0)
    #           0x46ed61(0) 0x46eda5(0)
    # = 3 + 7 = 10 bits.
    def w(bs):
        for _ in range(10):
            bs.write_flag(False)
    assert _decode("Player", w) == 10


def test_player_early_out_flag():
    # If the 0x46ed61 early-out flag is SET the method returns immediately after.
    #   ShapeBase 3 + Player flags 0x46e6d8..0x46ed05 (5 clear) + 0x46ed61 SET.
    def w(bs):
        for _ in range(3 + 5):
            bs.write_flag(False)
        bs.write_flag(True)   # early-out
    assert _decode("Player", w) == 3 + 5 + 1


def test_compressed_point_type3_absolute():
    """readCompressedPoint type 3 = a full-precision absolute world Point3F."""
    import struct
    from aotbot import telemetry
    bs = BitStream()
    bs.write_int(3, 2)                       # type 3
    bs.write_bytes(struct.pack("<fff", 292.647, 170.091, 213.218))
    rs = BitStream(bs.get_bytes())
    telemetry.set_compression_point(None)    # absolute path needs no reference
    pt, is_world = gh._read_compressed_point(rs)
    assert is_world is True
    assert pt[0] == pytest.approx(292.647, abs=1e-3)
    assert pt[1] == pytest.approx(170.091, abs=1e-3)
    assert pt[2] == pytest.approx(213.218, abs=1e-3)


def test_compressed_point_type0_dequant_jeff():
    """Regression: a captured PARKED remote player's compressed (type-0) pose
    dequantises to a sane bounded world position.

    Live winedbg/decode capture of "Jeff Bezos": with the client's control-object
    reference (281.790985, 175.593002, 213.212006) and scale 0.01, the raw
    type-0 signed ints (1077, -553, 1) dequantise to (292.56, 170.06, 213.22) --
    matching his true getTransform position (292.647, 170.091, 213.218) within the
    16-bit * 0.01 quantisation step. Bit consumption: 2 (type) + 3 * 16 = 50."""
    from aotbot import telemetry
    ref = (281.790985, 175.593002, 213.212006)
    telemetry.set_compression_point(ref)
    try:
        bs = BitStream()
        bs.write_int(0, 2)                   # type 0 -> 16-bit components
        for v in (1077, -553, 1):
            bs.write_signed_int(v, 16)
        rs = BitStream(bs.get_bytes())
        pt, is_world = gh._read_compressed_point(rs)
        assert is_world is True
        assert rs.get_bit_position() == 2 + 3 * 16
        assert pt[0] == pytest.approx(292.561, abs=1e-2)
        assert pt[1] == pytest.approx(170.063, abs=1e-2)
        assert pt[2] == pytest.approx(213.222, abs=1e-2)
        # Sane bounded world coords (not garbage like raw ints 1077,-553).
        assert all(-10000.0 < c < 10000.0 for c in pt)
    finally:
        telemetry.set_compression_point(None)


def test_compressed_point_type0_no_reference_consumes_bits():
    """Without a control reference the dequant is skipped (is_world False) but the
    raw signed ints are still consumed so the bit cursor stays exact."""
    from aotbot import telemetry
    telemetry.set_compression_point(None)
    bs = BitStream()
    bs.write_int(1, 2)                       # type 1 -> 18-bit components
    for v in (10, 20, 30):
        bs.write_signed_int(v, 18)
    rs = BitStream(bs.get_bytes())
    pt, is_world = gh._read_compressed_point(rs)
    assert is_world is False
    assert pt == (10, 20, 30)                # raw ints (unreferenced)
    assert rs.get_bit_position() == 2 + 3 * 18


def test_fx_brick_batcher_zero_bits():
    assert _decode("fxBrickBatcher", lambda bs: None) == 0


def test_physical_zone_clear():
    assert _decode("PhysicalZone", lambda bs: bs.write_flag(False)) == 1


def test_audio_emitter_all_clear():
    # leading flag + 19 fnFlag masks, all clear (each 1 bit) = 20 bits.
    def w(bs):
        for _ in range(20):
            bs.write_flag(False)
    assert _decode("AudioEmitter", w) == 20


# --- Wave-10 tail ghost classes ------------------------------------------- #


def test_marker_box_only():
    # parent(ret0) + mathRead "Box6F"/sphere (VA 0x421800) = 24 bytes + 1 sign
    # flag = 193 bits (Wave-12 winedbg fix), no mask.
    def w(bs):
        bs.write_bytes(b"\x00" * 24)
        bs.write_flag(False)  # trailing sign flag the engine reads @0x4218ad
    assert _decode("Marker", w) == 193


def test_fx_foliage_replicator_master_clear():
    # parent(ret0, 0 bits) + master mask flag (0) -> done. 1 bit.
    assert _decode(
        "fxFoliageReplicator", lambda bs: bs.write_flag(False)
    ) == 1


def test_fx_grass_replicator_master_clear():
    assert _decode(
        "fxGrassReplicator", lambda bs: bs.write_flag(False)
    ) == 1


def test_lightning_master_clear():
    # parent = GameBase (pos 0 + datablock 0) + Lightning master flag (0) -> done.
    # 3 bits.
    def w(bs):
        bs.write_flag(False)  # GameBase pos mask
        bs.write_flag(False)  # GameBase datablock mask
        bs.write_flag(False)  # Lightning master
    assert _decode("Lightning", w) == 3


def test_sky_master_clear_tail_clear():
    # master flag (0) -> skip settings; then 7 common-tail flags all clear.
    # = 1 + 7 = 8 bits.
    def w(bs):
        for _ in range(1 + 7):
            bs.write_flag(False)
    assert _decode("Sky", w) == 8


def test_hover_vehicle_master_set_returns_then_int3():
    # Vehicle: ShapeBase(3 clear flags) + flag A(0) + master flag B(1)->return;
    # HoverVehicle then reads an unconditional readInt(3). So 3+1+1 + 3 = 8 bits.
    def w(bs):
        bs.write_flag(False)  # GameBase pos (ShapeBase parent)
        bs.write_flag(False)  # GameBase datablock
        bs.write_flag(False)  # ShapeBase master
        bs.write_flag(False)  # Vehicle flag A
        bs.write_flag(True)   # Vehicle master flag B -> Vehicle returns
        bs.write_int(5, 3)    # HoverVehicle readInt(3)
    assert _decode("HoverVehicle", w) == 3 + 1 + 1 + 3


def test_water_block_minimal():
    # No master flag: Box6F(192) + Point3F(96) + 7 strings (each minimal: a
    # single useStringBuffer=0 + huffman terminator). We just assert it decodes
    # the leading fixed geometry + the two readInt(10) gates clear + trailing
    # flags without erroring on a hand-built minimal payload.
    def w(bs):
        bs.write_bytes(b"\x00" * 24)   # Box6F
        bs.write_bytes(b"\x00" * 12)   # Point3F
        for _ in range(7):
            bs.write_string("")        # 7 strings
        for _ in range(6):
            bs.write_bytes(b"\x00" * 4)
        bs.write_bytes(b"\x00")        # read(1)
        bs.write_flag(False)           # F1 gate clear
        bs.write_bytes(b"\x00")        # read(1)
        for _ in range(12):
            bs.write_bytes(b"\x00" * 4)
        bs.write_bytes(b"\x00" * 4)    # ColorF
        bs.write_bytes(b"\x00" * 4)    # read(4)
        bs.write_flag(False)           # F2
    bs = BitStream()
    w(bs)
    rs = BitStream(bs.get_bytes())
    gh.DECODERS["WaterBlock"](rs, True)
    assert not rs.error


def test_interior_instance_init_path_fg_present():
    """A-set (InitMask) path, bit-exact vs packUpdate @ VA 0x5084a0:
    flagA(1) + read4(mCRC,32) + string(name) + flagC + flagD + matrix(512) +
    Point3F(96) + flagE + string(skin) + flagF + flagG. With F/G flags clear and
    two empty strings, the F/G readInt(10) blocks are NOT entered. Confirms F/G
    are ALWAYS on the wire as flags (the Wave-11 finding) and the decoder reads
    them."""
    def w(bs):
        bs.set_string_buffer(bytearray(256))
        bs.write_flag(True)            # A = InitMask
        bs.write_bytes(b"\x00" * 4)    # mCRC
        bs.write_string("")            # mInteriorFileName
        bs.write_flag(False)           # C mShowTerrainInside
        bs.write_flag(False)           # D
        bs.write_bytes(b"\x00" * 64)   # matrix
        bs.write_bytes(b"\x00" * 12)   # Point3F mObjScale
        bs.write_flag(False)           # E mAlarmState
        bs.write_string("")            # mSkinBase
        bs.write_flag(False)           # F mAudioProfile present? no
        bs.write_flag(False)           # G mAudioEnvironment present? no
    bs = BitStream()
    w(bs)
    rs = BitStream(bs.get_bytes())
    rs.set_string_buffer(bytearray(256))
    gh.DECODERS["InteriorInstance"](rs, True)
    assert not rs.error


def test_interior_instance_init_path_fg_set():
    """A-set with F and G flags SET -> each followed by a readInt(10) id."""
    def w(bs):
        bs.set_string_buffer(bytearray(256))
        bs.write_flag(True)
        bs.write_bytes(b"\x00" * 4)
        bs.write_string("")
        bs.write_flag(False)
        bs.write_flag(False)
        bs.write_bytes(b"\x00" * 64)
        bs.write_bytes(b"\x00" * 12)
        bs.write_flag(False)
        bs.write_string("")
        bs.write_flag(True)            # F set
        bs.write_int(7, 10)            # audioProfile id
        bs.write_flag(True)            # G set
        bs.write_int(9, 10)            # audioEnvironment id
    bs = BitStream()
    w(bs)
    rs = BitStream(bs.get_bytes())
    rs.set_string_buffer(bytearray(256))
    gh.DECODERS["InteriorInstance"](rs, True)
    assert not rs.error


def test_interior_instance_normal_update_no_transform():
    """A-clear (normal) path: flagA(0) + transform flag(0) + alarm flag +
    skinBase flag(0) + audio flag(0). The light-grouper loop reads 0 bits for a
    freshly scoped interior."""
    def w(bs):
        bs.set_string_buffer(bytearray(256))
        bs.write_flag(False)           # A clear -> normal update
        bs.write_flag(False)           # transform present? no
        bs.write_flag(False)           # alarm
        bs.write_flag(False)           # skinBase mask
        bs.write_flag(False)           # audio mask
    bs = BitStream()
    w(bs)
    rs = BitStream(bs.get_bytes())
    rs.set_string_buffer(bytearray(256))
    gh.DECODERS["InteriorInstance"](rs, True)
    assert not rs.error
    assert rs.get_bit_position() == 5


def test_all_object_classes_have_decoders():
    # Final wave: every one of the 50 object classes has an unpackUpdate decoder.
    missing = [n for n in gh.OBJECT_CLASS_NAMES if n not in gh.DECODERS]
    assert missing == []


def test_out_of_range_class_still_raises():
    # A classId >= 50 is impossible from an unchanged server (proof of an
    # upstream misalignment); unpack_update must still raise with the id.
    bs = BitStream()
    bs.write_flag(True)
    rs = BitStream(bs.get_bytes())
    with pytest.raises(gh.GhostDecodeError):
        gh.unpack_update(rs, 58, is_new=True)


# --------------------------------------------------------------------------- #
# Wave-17 regression: the live current-world login desync.
#
# These guard the per-class unpackUpdate fixes that resolved the silent
# ghost-burst misalignment which broke live login after the telemetry commit:
#   * fxFoliageReplicator over-read by 4 bits (4 spurious 1-bit flags between
#     consecutive read(1) byte fields -- the exe reads them CONSECUTIVELY).
#   * DestructableSpawner / GoldSpawner / SpawnSphere / WayPoint each OVERRIDE
#     MissionMarker::unpackUpdate with extra trailing fields (the shared
#     MissionMarker decoder under-read them).
#   * fxSunLight / TerrainBlock were unported (now decoded).
# --------------------------------------------------------------------------- #


def test_fx_foliage_replicator_full_bit_length():
    """Master-set fxFoliageReplicator consumes the bit-exact body (NO spurious
    flags between the consecutive read(1) byte fields). The four +0x394/+0x395/
    +0x39c/+0x39d fields are CONSECUTIVE single-byte reads (Stream::read(1,&bool))
    -- the earlier transcription inserted 4 phantom 1-bit flags -> a +4-bit
    over-read that desynced the live ghost burst and broke login. This asserts
    the decoder consumes EXACTLY a faithful writer's body (the 4 bytes are
    written consecutively with no interleaved flag)."""
    def w(bs):
        bs.write_flag(True)                       # master SET
        bs.write_bytes(b"\x00" * 24); bs.write_flag(False)   # Box6F = 193
        bs.write_flag(False)                      # +0x358
        bs.write_bytes(b"\x00" * 16)              # 4 x read(4)
        bs.write_string("")                       # readString +0x364
        bs.write_bytes(b"\x00" * 32)              # 8 x read(4)
        bs.write_bytes(b"\x00")                   # +0x394 read(1) byte
        bs.write_bytes(b"\x00")                   # +0x395 read(1) byte  (consecutive)
        bs.write_bytes(b"\x00" * 4)               # +0x398 read(4)
        bs.write_bytes(b"\x00")                   # +0x39c read(1) byte
        bs.write_bytes(b"\x00")                   # +0x39d read(1) byte  (consecutive)
        bs.write_bytes(b"\x00" * 28)              # 7 x read(4)
        bs.write_flag(False); bs.write_flag(False)            # 2 flags
        bs.write_bytes(b"\x00" * 16)              # 4 x read(4)
        bs.write_flag(False); bs.write_flag(False)            # 2 flags
        bs.write_bytes(b"\x00" * 12)              # 3 x read(4)
        for _ in range(5):
            bs.write_flag(False)                  # 5 flags
        bs.write_bytes(b"\x00" * 4)               # read(4)
        bs.write_flag(False); bs.write_flag(False)            # 2 flags
        bs.write_bytes(b"\x00" * 4)               # read(4)
        bs.write_bytes(b"\x00" * 4)               # ColorF
    _roundtrip("fxFoliageReplicator", w)


def _shapebase_marker_clear(bs):
    # ShapeBase parent: GameBase pos(0)+datablock(0)+master(0) = 3 clear flags,
    # then MissionMarker's own flag(0) = 4 clear flags total.
    for _ in range(4):
        bs.write_flag(False)


def test_destructable_spawner_clear_and_set():
    # MissionMarker (4 clear) + DestructableSpawner flag(0) = 5 bits.
    assert _decode("DestructableSpawner", _shapebase_marker_clear_with(0)) == 5
    # flag(1) + read(4) = 4 + 1 + 32 = 37 bits.
    assert _decode("DestructableSpawner", _shapebase_marker_clear_with(1, b"\x00" * 4)) == 4 + 1 + 32


def _shapebase_marker_clear_with(extra_flag, *payloads):
    def w(bs):
        for _ in range(4):
            bs.write_flag(False)   # MissionMarker all-clear
        bs.write_flag(bool(extra_flag))
        for p in payloads:
            bs.write_bytes(p)
    return w


def test_gold_spawner_clear_and_set():
    assert _decode("GoldSpawner", _shapebase_marker_clear_with(0)) == 5
    # flag(1) + 6 x read(4) + read(1)byte = 4 + 1 + 192 + 8 = 205 bits.
    assert _decode(
        "GoldSpawner", _shapebase_marker_clear_with(1, b"\x00" * 24, b"\x00")
    ) == 4 + 1 + 6 * 32 + 8


def test_spawn_sphere_clear_and_set():
    assert _decode("SpawnSphere", _shapebase_marker_clear_with(0)) == 5
    # flag(1) + 4 x read(4) = 4 + 1 + 128 = 133 bits.
    assert _decode(
        "SpawnSphere", _shapebase_marker_clear_with(1, b"\x00" * 16)
    ) == 4 + 1 + 4 * 32


def test_way_point_three_clear_flags():
    # MissionMarker (4 clear) + 3 own flags all clear = 7 bits.
    def w(bs):
        for _ in range(4 + 3):
            bs.write_flag(False)
    assert _decode("WayPoint", w) == 7


def test_npc_and_maze_spawner_are_mission_marker():
    # NPCSpawner (0x4638a0 = jmp 0x463620) and MazeSpawner share MissionMarker.
    for cls in ("NPCSpawner", "MazeSpawner", "RoomMarker"):
        def w(bs):
            for _ in range(4):
                bs.write_flag(False)
        assert _decode(cls, w) == 4


def test_fx_sun_light_master_clear():
    assert _decode("fxSunLight", lambda bs: bs.write_flag(False)) == 1


def test_fx_sun_light_master_set():
    # master(1) + Box6F(193) + read(1)byte(8) + 2 strings(null=2 each=4) +
    # 2 read(4)(64) + read(1)byte(8) + ColorF(32) + 4 read(4)(128) + 14 flags +
    # 2 ColorF(64) + 10 read(4)(320) + 8 strings(16) + 6 read(4)(192).
    def w(bs):
        bs.write_flag(True)
        bs.write_bytes(b"\x00" * 24); bs.write_flag(False)  # Box6F 193
        bs.write_bytes(b"\x00")                # read(1) byte
        bs.write_string(""); bs.write_string("")            # 2 strings (null=2 each)
        bs.write_bytes(b"\x00" * 8)            # 2 x read(4)
        bs.write_bytes(b"\x00")                # read(1) byte
        bs.write_bytes(b"\x00" * 4)            # ColorF
        bs.write_bytes(b"\x00" * 16)           # 4 x read(4)
        for _ in range(14):
            bs.write_flag(False)
        bs.write_bytes(b"\x00" * 8)            # 2 x ColorF
        bs.write_bytes(b"\x00" * 40)           # 10 x read(4)
        for _ in range(8):
            bs.write_string("")
        bs.write_bytes(b"\x00" * 24)           # 6 x read(4)
    _roundtrip("fxSunLight", w)


def test_terrain_block_flag1_set():
    # flag1(1) + read(4) + 3 strings + 4 read(4) + count read(4)(=0, no loop).
    def w(bs):
        bs.write_flag(True)
        bs.write_bytes(b"\x00" * 4)            # read(4)
        bs.write_string(""); bs.write_string(""); bs.write_string("")  # 3 strings
        bs.write_bytes(b"\x00" * 16)           # 4 x read(4)
        bs.write_bytes(b"\x00" * 4)            # count = 0 (no loop)
    _roundtrip("TerrainBlock", w)


def test_terrain_block_flag1_clear_flag2_set():
    # flag1(0) + flag2(1) + read(4) count=0 = 1 + 1 + 32 = 34 bits.
    def w(bs):
        bs.write_flag(False)   # flag1 clear
        bs.write_flag(True)    # flag2 set
        bs.write_bytes(b"\x00" * 4)  # count = 0
    assert _decode("TerrainBlock", w) == 1 + 1 + 32


# --------------------------------------------------------------------------- #
# Final wave: the last 7 unported classes (vtable slot 0x4c resolved via
# regthunk -> ClassRep -> create -> objVtable):
#   FlyingVehicle 0x4c7c10, WheeledVehicle 0x4d2430, PathCamera 0x4649c0,
#   Splash 0x4be080, fxLight 0x4acc20, VehicleBlocker 0x4d0c50,
#   twSurfaceReference 0x4bfe50.
# --------------------------------------------------------------------------- #


def test_splash_clear_and_set():
    # GameBase (2 clear flags) + Splash flag(0) = 3 bits.
    def w0(bs):
        for _ in range(3):
            bs.write_flag(False)
    assert _decode("Splash", w0) == 3
    # GameBase (2 clear) + flag(1) + Point3F(96) = 99 bits.
    def w1(bs):
        bs.write_flag(False); bs.write_flag(False)
        bs.write_flag(True)
        bs.write_bytes(b"\x00" * 12)
    assert _decode("Splash", w1) == 2 + 1 + 96


def test_vehicle_blocker_unconditional_704_bits():
    # No mask at all: matrix(512) + Point3F(96) scale + Point3F(96) dims.
    def w(bs):
        bs.write_bytes(b"\x00" * (64 + 12 + 12))
    assert _decode("VehicleBlocker", w) == 704


def test_tw_surface_reference_clear_and_set():
    # master flag clear -> 1 bit.
    assert _decode("twSurfaceReference", lambda bs: bs.write_flag(False)) == 1
    # master(1) + Box6F(193) + read4(32) + flag(1) + read4(32) + ColorF(32).
    def w(bs):
        bs.write_flag(True)
        bs.write_bytes(b"\x00" * 24); bs.write_flag(False)  # Box6F = 193
        bs.write_bytes(b"\x00" * 4)    # +0x260
        bs.write_flag(False)           # +0x264
        bs.write_bytes(b"\x00" * 4)    # +0x268
        bs.write_bytes(b"\x00" * 4)    # ColorF
    assert _decode("twSurfaceReference", w) == 1 + 193 + 32 + 1 + 32 + 32


def test_fx_light_all_clear():
    # GameBase (2 clear) + flag A(0) + flag B(0) = 4 bits.
    def w(bs):
        for _ in range(4):
            bs.write_flag(False)
    assert _decode("fxLight", w) == 4


def test_fx_light_attach_and_transform():
    # A(1) + inner(1) + readInt(15) ghost id; B(1) + Box6F(193) + read(1)byte(8)
    # + read4(32).
    def w(bs):
        bs.write_flag(False); bs.write_flag(False)   # GameBase clear
        bs.write_flag(True)            # flag A
        bs.write_flag(True)            # attached
        bs.write_int(123, 15)          # attached ghost id
        bs.write_flag(True)            # flag B
        bs.write_bytes(b"\x00" * 24); bs.write_flag(False)  # Box6F = 193
        bs.write_bytes(b"\x00")        # read(1) byte
        bs.write_bytes(b"\x00" * 4)    # read(4)
    assert _decode("fxLight", w) == 2 + 1 + 1 + 15 + 1 + 193 + 8 + 32


def test_path_camera_all_clear():
    # ShapeBase (3 clear) + 4 mask flags clear + unconditional trailing flag
    # = 3 + 5 = 8 bits.
    def w(bs):
        for _ in range(8):
            bs.write_flag(False)
    assert _decode("PathCamera", w) == 8


def test_path_camera_spline_window():
    # WindowMask with 2 knots: each knot = Point3F(96) + QuatF(128) + read4(32)
    # + readInt(2) + readInt(1) = 259 bits.
    def w(bs):
        for _ in range(3):
            bs.write_flag(False)       # ShapeBase parent clear
        bs.write_flag(False)           # StateMask
        bs.write_flag(False)           # PositionMask
        bs.write_flag(False)           # TargetMask
        bs.write_flag(True)            # WindowMask
        bs.write_bytes(b"\x00" * 4)    # mNodeBase
        bs.write_bytes((2).to_bytes(4, "little"))  # mNodeCount = 2
        for _ in range(2):
            bs.write_bytes(b"\x00" * 12)   # knot position
            bs.write_bytes(b"\x00" * 16)   # knot rotation QuatF
            bs.write_bytes(b"\x00" * 4)    # knot speed
            bs.write_int(1, 2)             # knot type
            bs.write_int(0, 1)             # knot path
        bs.write_flag(False)           # trailing control flag
    assert _decode("PathCamera", w) == 3 + 4 + 32 + 32 + 2 * (96 + 128 + 32 + 3) + 1


def test_path_camera_negative_node_count_skips_loop():
    # The exe's loop bound is a SIGNED jle: a "negative" count reads no knots.
    def w(bs):
        for _ in range(3):
            bs.write_flag(False)
        bs.write_flag(False); bs.write_flag(False); bs.write_flag(False)
        bs.write_flag(True)            # WindowMask
        bs.write_bytes(b"\x00" * 4)    # mNodeBase
        bs.write_bytes((0xFFFFFFFF).to_bytes(4, "little"))  # mNodeCount = -1
        bs.write_flag(False)           # trailing flag
    assert _decode("PathCamera", w) == 3 + 4 + 32 + 32 + 1


def test_flying_vehicle_tail_after_vehicle_master_set():
    # Vehicle: ShapeBase(3 clear) + flag A(0) + master B(1) -> Vehicle returns;
    # FlyingVehicle tail (non-control object): createHeightOn flag +
    # readInt(3) thrust = 3+1+1+1+3 = 9 bits.
    def w(bs):
        for _ in range(3):
            bs.write_flag(False)   # ShapeBase parent clear
        bs.write_flag(False)       # Vehicle flag A
        bs.write_flag(True)        # Vehicle master B -> return
        bs.write_flag(True)        # createHeightOn
        bs.write_int(5, 3)         # mThrustDirection
    assert _decode("FlyingVehicle", w) == 3 + 1 + 1 + 1 + 3


def test_wheeled_vehicle_no_wheel_blocks():
    # Vehicle (master set = 5 bits) + W1(0) + W2(0) + braking(1 bit) + W4(0)
    # = 5 + 4 = 9 bits. No wheel count needed on this path.
    def w(bs):
        for _ in range(3):
            bs.write_flag(False)   # ShapeBase parent
        bs.write_flag(False)       # Vehicle flag A
        bs.write_flag(True)        # Vehicle master B -> return
        bs.write_flag(False)       # W1 wheel datablocks
        bs.write_flag(False)       # W2 control suppression
        bs.write_flag(True)        # mBraking
        bs.write_flag(False)       # W4 wheel motion
    assert _decode("WheeledVehicle", w) == 9


def test_wheeled_vehicle_w2_set_returns():
    # W2 SET -> immediate return: 5 + 1 + 1 = 7 bits.
    def w(bs):
        for _ in range(3):
            bs.write_flag(False)
        bs.write_flag(False)       # Vehicle flag A
        bs.write_flag(True)        # Vehicle master B
        bs.write_flag(False)       # W1
        bs.write_flag(True)        # W2 -> return
    assert _decode("WheeledVehicle", w) == 7


def test_wheeled_vehicle_wheel_blocks_with_known_count(monkeypatch):
    # With the .dts-derived wheel count supplied, both wheel loops decode:
    # W1: 2 x [flag(1) + tire int10 + spring int10 + powered flag + steering
    # read4] = 2 x 54; W4: 2 x 3 x read4 = 192.
    monkeypatch.setattr(gh, "WHEELED_VEHICLE_WHEEL_COUNT", 2)
    def w(bs):
        for _ in range(3):
            bs.write_flag(False)
        bs.write_flag(False)       # Vehicle flag A
        bs.write_flag(True)        # Vehicle master B
        bs.write_flag(True)        # W1 set
        for _ in range(2):
            bs.write_flag(True)    # wheel present
            bs.write_int(7, 10)    # tire db id
            bs.write_int(9, 10)    # spring db id
            bs.write_flag(True)    # powered
            bs.write_bytes(b"\x00" * 4)  # steering
        bs.write_flag(False)       # W2
        bs.write_flag(False)       # mBraking
        bs.write_flag(True)        # W4 set
        for _ in range(2):
            bs.write_bytes(b"\x00" * 12)  # avel/Dy/Dx
    assert _decode("WheeledVehicle", w) == 5 + 1 + 2 * 54 + 1 + 1 + 1 + 2 * 96


def test_wheeled_vehicle_unknown_count_raises():
    # Wheel block present but wheelCount unknown (the live default): the decoder
    # must raise GhostDecodeError (containment) rather than guess a loop count.
    assert gh.WHEELED_VEHICLE_WHEEL_COUNT is None
    bs = BitStream()
    for _ in range(3):
        bs.write_flag(False)
    bs.write_flag(False)           # Vehicle flag A
    bs.write_flag(True)            # Vehicle master B
    bs.write_flag(True)            # W1 set -> needs wheelCount
    rs = BitStream(bs.get_bytes())
    with pytest.raises(gh.GhostDecodeError):
        gh.DECODERS["WheeledVehicle"](rs, True)


# --------------------------------------------------------------------------- #
# Precipitation unpackUpdate tail (flag2/flag3, VA 0x4bc14a..0x4bc1fe) -- the
# bad_login line-2406 / seq-261 23-bit-residual fix.
# --------------------------------------------------------------------------- #


def test_precipitation_all_clear():
    # GameBase(2 clear) + flag1(0) + flag2(0) + flag3(0) = 5 bits. flag2/flag3
    # are read on EVERY update (the flag1-clear branch jumps to 0x4bc14a, not
    # the epilogue).
    def w(bs):
        for _ in range(5):
            bs.write_flag(False)
    assert _decode("Precipitation", w) == 5


def test_precipitation_flag2_only():
    # The exact seq-261 shape: flag1 block + flag2 SET (+0x500 F32) + flag3
    # clear. GameBase(1+96+1+10) + flag1(1+288+3) + flag2(1+32) + flag3(1).
    def w(bs):
        bs.write_flag(True)               # GameBase scale mask
        bs.write_bytes(b"\x00" * 12)      # Point3F
        bs.write_flag(True)               # GameBase datablock mask
        bs.write_int(284, 10)             # datablock id
        bs.write_flag(True)               # flag1
        bs.write_bytes(b"\x00" * 36)      # 9 x read(4)
        bs.write_flag(False)              # +0x524
        bs.write_flag(False)              # +0x525
        bs.write_flag(True)               # +0x526
        bs.write_flag(True)               # flag2
        bs.write_bytes(b"\x00\x00\x80\x3f")  # +0x500 = 1.0f
        bs.write_flag(False)              # flag3
    assert _decode("Precipitation", w) == (1 + 96 + 1 + 10) + (1 + 288 + 3) + (1 + 32) + 1


def test_precipitation_flag3_set():
    # flag3 SET -> 2 x read(4) (the 0x4bb750 clamp args).
    def w(bs):
        for _ in range(3):
            bs.write_flag(False)          # GameBase x2 + flag1
        bs.write_flag(False)              # flag2
        bs.write_flag(True)               # flag3
        bs.write_bytes(b"\x00" * 8)       # 2 x read(4)
    assert _decode("Precipitation", w) == 5 + 64


# --------------------------------------------------------------------------- #
# Control-object readPacketData (vtable slot 0xec) coverage -- the full-sweep
# override table (Vehicle/FlyingVehicle/WheeledVehicle + GameBase 0-bit stub).
# --------------------------------------------------------------------------- #


def _decode_packet_data(class_name, write_fn):
    bs = BitStream()
    write_fn(bs)
    rs = BitStream(bs.get_bytes())
    gh.PACKET_DATA_DECODERS[class_name](rs)
    assert not rs.error
    return rs.get_bit_position()


def test_packet_data_gamebase_stub_zero_bits():
    # GameBase::readPacketData == 0x485790 bare ret 8: ZERO bits for every
    # GameBase-derived non-ShapeBase class.
    for cls in ("GameBase", "Debris", "Lightning", "ParticleEmitterNode",
                "PathedInterior", "Precipitation", "Projectile", "Splash",
                "Trigger", "fxLight"):
        assert _decode_packet_data(cls, lambda bs: None) == 0


def test_packet_data_vehicle_547_bits():
    # Vehicle::readPacketData (0x4ce0c0): ShapeBase 8B + 2 x read(4) + Point3F +
    # PlaneF(16B) + 2 x Point3F + 3 flags = 68 bytes + 3 bits = 547 bits, always.
    def w(bs):
        bs.write_bytes(b"\x00" * 68)
        for _ in range(3):
            bs.write_flag(True)
    assert _decode_packet_data("HoverVehicle", w) == 547
    # FlyingVehicle's override (0x4c7300) adds only a bit-free tail.
    assert _decode_packet_data("FlyingVehicle", w) == 547


def test_packet_data_wheeled_vehicle_with_count(monkeypatch):
    # WheeledVehicle (0x4d20e0): Vehicle 547 + mBraking flag + wheelCount x
    # [3 x read(4) + slip flag].
    monkeypatch.setattr(gh, "WHEELED_VEHICLE_WHEEL_COUNT", 4)
    def w(bs):
        bs.write_bytes(b"\x00" * 68)
        for _ in range(3):
            bs.write_flag(False)
        bs.write_flag(True)               # mBraking
        for _ in range(4):
            bs.write_bytes(b"\x00" * 12)  # avel/Dy/Dx
            bs.write_flag(False)          # slipping
    assert _decode_packet_data("WheeledVehicle", w) == 547 + 1 + 4 * 97


def test_packet_data_wheeled_vehicle_unknown_count_raises():
    assert gh.WHEELED_VEHICLE_WHEEL_COUNT is None
    bs = BitStream()
    bs.write_bytes(b"\x00" * 68)
    for _ in range(4):
        bs.write_flag(False)
    rs = BitStream(bs.get_bytes())
    with pytest.raises(gh.GhostDecodeError):
        gh.PACKET_DATA_DECODERS["WheeledVehicle"](rs)


def test_packet_data_decoders_cover_all_gamebase_classes():
    # PACKET_DATA_DECODERS must be TOTAL over the GameBase-derived classes (the
    # only legal control objects; GameConnection::setControlObject takes a
    # GameBase*). The SceneObject-only classes have NO vtable slot 0xec.
    scene_object_only = {
        "AudioEmitter", "InteriorInstance", "Marker", "MissionArea",
        "PhysicalZone", "SimpleNetObject", "Sky", "Sun", "TSStatic",
        "TerrainBlock", "VehicleBlocker", "WaterBlock", "fxBrickBatcher",
        "fxDTSBrick", "fxFoliageReplicator", "fxGrassReplicator",
        "fxShapeReplicatedStatic", "fxShapeReplicator", "fxSunLight",
        "twSurfaceReference", "volumeLight",
    }
    for name in gh.OBJECT_CLASS_NAMES:
        if name in scene_object_only:
            assert name not in gh.PACKET_DATA_DECODERS
        else:
            assert name in gh.PACKET_DATA_DECODERS, name


# --------------------------------------------------------------------------- #
# ShapeBase damage block (0x483e36) -> damage telemetry (WAVE-22)
# --------------------------------------------------------------------------- #


def _decode_with_sink(class_name, write_fn, *, is_new=True):
    """Like _decode but with a telemetry DecodeSink installed; returns (bits,
    sink.fields)."""
    from aotbot import telemetry

    bs = BitStream()
    write_fn(bs)
    rs = BitStream(bs.get_bytes())
    sink = telemetry.DecodeSink()
    telemetry.set_sink(sink)
    try:
        gh.DECODERS[class_name](rs, is_new)
    finally:
        telemetry.set_sink(None)
    assert not rs.error
    return rs.get_bit_position(), sink.fields


def _write_damage_block(bs, *, frac_units: int, state: int):
    """The ShapeBase 0x483e36 damage block: writeFloat(frac,6) + writeInt(2)
    + normal-vector(8) (17 bits), prefixed by GameBase(2 clear) + master(1) +
    damage flag(1), followed by the remaining ShapeBase masks all clear."""
    bs.write_flag(False)          # GameBase pos
    bs.write_flag(False)          # GameBase datablock
    bs.write_flag(True)           # ShapeBase master
    bs.write_flag(True)           # damage block present
    bs.write_int(frac_units, 6)   # writeFloat(mDamage/maxDamage, 6) = u/63
    bs.write_int(state, 2)        # damage state (2 = Destroyed)
    # writeNormalVector(8) = signed-float(9) + signed-float(8) = 17 bits
    # (mDamageDir -- consumed, not surfaced).
    bs.write_int(0, 9)
    bs.write_int(0, 8)
    for _ in range(5):            # image-trigger/skin/mounted/core/mount masks
        bs.write_flag(False)


def test_shape_base_damage_block_emits_damage_level():
    def w(bs):
        _write_damage_block(bs, frac_units=32, state=0)  # ~half health, Enabled
    bits, fields = _decode_with_sink("ShapeBase", w)
    assert fields["damage_level"] == pytest.approx(32 / 63.0)
    assert fields["damage_state"] == 0
    # The 8-bit normal vector in this block is mDamageDir (last-hit direction),
    # NOT orientation -- it must no longer surface as rotation telemetry.
    assert "rotation" not in fields


def test_shape_base_damage_destroyed_state():
    def w(bs):
        _write_damage_block(bs, frac_units=63, state=2)  # dead
    _, fields = _decode_with_sink("ShapeBase", w)
    assert fields["damage_level"] == pytest.approx(1.0)
    assert fields["damage_state"] == 2


def test_player_inherits_damage_telemetry():
    # Player::unpackUpdate calls ShapeBase as its parent, so a Player update
    # carrying the damage block surfaces the same fields. Player's own masks
    # after the ShapeBase prefix are written all-clear.
    def w(bs):
        _write_damage_block(bs, frac_units=16, state=0)
        # Player's own 7 top-level masks after the ShapeBase parent, all clear
        # (0x46e6d8, 0x46e76d, 0x46e987, 0x46e9cb, 0x46ed05, 0x46ed61, 0x46eda5).
        for _ in range(7):
            bs.write_flag(False)
    bits, fields = _decode_with_sink("Player", w)
    assert fields["damage_level"] == pytest.approx(16 / 63.0)
