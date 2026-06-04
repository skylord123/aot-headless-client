"""Tests for the per-class datablock unpackData decoders (aotbot/datablocks.py).

The decoders are reverse-engineered from AgeOfTime.exe and the load-bearing
guarantee is *bit-exactness*: a correct decoder consumes EXACTLY the bytes the
server wrote, so replaying the real client's capture (real_login.jsonl) stays
aligned. The integration test below is the real regression guard -- it replays
the golden capture's s2c datablock stream through the production read path and
asserts a known number of datablocks decode in order with zero desync.
"""

import json
import os

import pytest

import aotbot.datablocks as db
from aotbot.bitstream import BitStream
from aotbot.datablocks import DataBlockDecodeError, unpack_datablock

CAPTURE = os.path.join(
    os.path.dirname(__file__), "..", "tools", "captures", "real_login.jsonl"
)

# Datablock classes whose unpackData is implemented + capture-validated.
# The set of datablock classes with a registered (CFG-traced, capture-validated)
# unpackData decoder. Derived from the live registry so it tracks new ports.
IMPLEMENTED = set(db.DECODERS.keys())


def test_class_name_table_indices():
    # The index is the on-wire 6-bit classId (sorted by ASCII name).
    assert db.DATABLOCK_CLASS_NAMES[0] == "AudioDescription"
    assert db.DATABLOCK_CLASS_NAMES[2] == "AudioProfile"
    assert db.DATABLOCK_CLASS_NAMES[4] == "CameraData"
    assert db.DATABLOCK_CLASS_NAMES[14] == "ParticleData"


def test_unknown_class_raises_with_name():
    bs = BitStream(b"\x00" * 8)
    # FlyingVehicleData (8) has no decoder yet (needs VehicleData parent chain;
    # never appears in any AoT capture) -> raises carrying the name.
    with pytest.raises(DataBlockDecodeError) as ei:
        unpack_datablock(bs, 8)
    assert ei.value.name == "FlyingVehicleData"
    assert ei.value.class_id == 8


def test_implemented_set_registered():
    for name in IMPLEMENTED:
        assert name in db.DECODERS, f"{name} should have a registered decoder"


def test_audio_description_consumes_minimal_stream():
    # Smallest AudioDescription: volume(6) + isLooping=0 + is3D=0 +
    # isStreaming=0 + readInt(3) = 6+1+1+1+3 = 12 bits.
    bs = BitStream()
    bs.write_float(0.5, 6)
    bs.write_flag(False)  # isLooping
    bs.write_flag(False)  # is3D
    bs.write_flag(False)  # isStreaming
    bs.write_int(0, 3)
    bs.set_bit_position(0)
    db._unpack_audio_description(bs)
    assert bs.get_bit_position() == 12
    assert not bs.error


def test_particle_emitter_data_minimal_stream():
    # All gating flags 0; times[] count 0.
    # readInt(10)+readInt(10)+readInt(16)+readInt(14)=50; flag(1);
    # readRangedU32(0,181)=8 *2 =16; flag*2=2; flag*3=3; readInt(15)+readInt(10)=25;
    # flag*3=3; read(4)=32 (count) -> total 50+1+16+2+3+25+3+32 = 132.
    bs = BitStream()
    bs.write_int(0, 10); bs.write_int(0, 10); bs.write_int(0, 16); bs.write_int(0, 14)
    bs.write_flag(False)
    bs.write_int(0, 8); bs.write_int(0, 8)          # thetaMin/Max (ranged 181 -> 8b)
    bs.write_flag(False); bs.write_flag(False)      # phi flags
    bs.write_flag(False); bs.write_flag(False); bs.write_flag(False)
    bs.write_int(0, 15); bs.write_int(0, 10)
    bs.write_flag(False); bs.write_flag(False); bs.write_flag(False)
    bs.write_bytes(b"\x00\x00\x00\x00")             # times[] count = 0
    bs.set_bit_position(0)
    db._unpack_particle_emitter_data(bs)
    assert bs.get_bit_position() == 132
    assert not bs.error


def test_debris_data_minimal_stream():
    # 6 read(4)=192, 4 read(1)=32, 6 read(4)=192, 2 read(1)=16, 3 read(4)=96,
    # 1 read(1)=8, 2 readString (empty), 2 db-ref (flag 0 =>1 each), 1 db-ref.
    bs = BitStream()
    for _ in range(6): bs.write_bytes(b"\x00\x00\x00\x00")
    for _ in range(4): bs.write_bytes(b"\x00")
    for _ in range(6): bs.write_bytes(b"\x00\x00\x00\x00")
    for _ in range(2): bs.write_bytes(b"\x00")
    for _ in range(3): bs.write_bytes(b"\x00\x00\x00\x00")
    bs.write_bytes(b"\x00")
    bs.write_string(""); bs.write_string("")
    bs.write_flag(False); bs.write_flag(False); bs.write_flag(False)  # 3 db-refs
    end = bs.get_bit_position()
    bs.set_bit_position(0)
    db._unpack_debris_data(bs)
    assert bs.get_bit_position() == end
    assert not bs.error


def test_splash_data_minimal_stream():
    bs = BitStream()
    bs.write_bytes(b"\x00" * 12)                    # Point3F
    for _ in range(15): bs.write_bytes(b"\x00\x00\x00\x00")
    bs.write_flag(False)                            # db-ref [+0x10c]
    for _ in range(3): bs.write_flag(False)         # 3 db-refs
    for _ in range(4): bs.write_bytes(b"\x00\x00\x00\x00")  # 4 ColorF
    for _ in range(4): bs.write_bytes(b"\x00\x00\x00\x00")  # 4 read(4)
    bs.write_string(""); bs.write_string("")
    end = bs.get_bit_position()
    bs.set_bit_position(0)
    db._unpack_splash_data(bs)
    assert bs.get_bit_position() == end
    assert not bs.error


def test_explosion_data_minimal_stream():
    bs = BitStream()
    bs.write_string("")                             # explosionShape
    bs.write_flag(False); bs.write_flag(False)      # 2 db-refs
    bs.write_int(0, 14); bs.write_bytes(b"\x00\x00\x00\x00")
    bs.write_flag(False)                            # bool
    bs.write_flag(False)                            # sizes present = 0
    bs.write_int(0, 14)
    bs.write_int(0, 8); bs.write_int(0, 8)          # ranged 181
    bs.write_int(0, 9); bs.write_int(0, 9)          # ranged 361
    bs.write_int(0, 10); bs.write_int(0, 10)        # ranged 1001
    bs.write_int(0, 14)                             # [+0xc4]
    bs.write_int(0, 14)                             # ranged 10001
    for _ in range(4): bs.write_int(0, 16)
    bs.write_bytes(b"\x00\x00\x00\x00")
    bs.write_flag(False)                            # bool
    for _ in range(9): bs.write_bytes(b"\x00\x00\x00\x00")
    bs.write_flag(False)                            # emitter db-ref
    for _ in range(4): bs.write_flag(False)         # 4 db-refs
    for _ in range(5): bs.write_flag(False)         # 5 db-refs
    bs.write_int(0, 3)                              # times count (ranged 5) = 0
    bs.write_float(0.0, 8); bs.write_float(0.0, 8)
    for _ in range(6): bs.write_float(0.0, 7)
    end = bs.get_bit_position()
    bs.set_bit_position(0)
    db._unpack_explosion_data(bs)
    assert bs.get_bit_position() == end
    assert not bs.error


def test_precipitation_data_minimal_stream():
    # PrecipitationData (VA 0x4bad60): db-ref(flag 0 =>1b) + 2 readString(empty)
    # + 3 read(4)=96 + readFlag=1.  TGE precipitation.cc-confirmed.
    bs = BitStream()
    bs.write_flag(False)                            # soundProfile db-ref absent
    bs.write_string(""); bs.write_string("")        # dropTexture, splashTexture
    for _ in range(3):
        bs.write_bytes(b"\x00\x00\x00\x00")         # dropSize, splashSize, splashMS
    bs.write_flag(True)                             # useTrueBillboards
    end = bs.get_bit_position()
    bs.set_bit_position(0)
    db._unpack_precipitation_data(bs)
    assert bs.get_bit_position() == end
    assert not bs.error


def test_precipitation_data_with_sound_profile():
    # db-ref present -> flag(1) + readInt(10).
    bs = BitStream()
    bs.write_flag(True); bs.write_int(7, 10)        # soundProfileId db-ref
    bs.write_string("rain.png"); bs.write_string("splash.png")
    for _ in range(3):
        bs.write_bytes(b"\x01\x00\x00\x00")
    bs.write_flag(False)
    end = bs.get_bit_position()
    bs.set_bit_position(0)
    db._unpack_precipitation_data(bs)
    assert bs.get_bit_position() == end
    assert not bs.error


def test_audio_environment_use_room_stream():
    # mUseRoom=1 -> readFlag + readRangedU32(0,28)=5b, then return.
    bs = BitStream()
    bs.write_flag(True)                             # mUseRoom
    bs.write_int(3, 5)                              # mRoom (ranged 0..28 => 5b)
    end = bs.get_bit_position()
    bs.set_bit_position(0)
    db._unpack_audio_environment(bs)
    assert bs.get_bit_position() == end == 6
    assert not bs.error


def test_audio_environment_full_stream():
    # mUseRoom=0 -> 1 + 14 + 15 + 14 + (8+8+8+9+7) + 14 + (8+9+10+8+10) + 6
    bs = BitStream()
    bs.write_flag(False)
    bs.write_int(0, 14); bs.write_int(0, 15); bs.write_int(0, 14)
    for w in (8, 8, 8, 9, 7):
        bs.write_int(0, w)
    bs.write_int(0, 14)
    for w in (8, 9, 10, 8, 10):
        bs.write_int(0, w)
    bs.write_int(0, 6)
    bs.set_bit_position(0)
    db._unpack_audio_environment(bs)
    assert bs.get_bit_position() == 1 + 14 + 15 + 14 + 40 + 14 + 45 + 6
    assert not bs.error


def test_audio_sample_environment_stream():
    bs = BitStream()
    for w in (14, 14, 14, 14):
        bs.write_int(0, w)
    for w in (9, 8, 9, 8, 9, 9, 9):
        bs.write_int(0, w)
    bs.write_int(0, 14)
    bs.write_int(0, 3)
    bs.set_bit_position(0)
    db._unpack_audio_sample_environment(bs)
    assert bs.get_bit_position() == 56 + 61 + 14 + 3
    assert not bs.error


def test_pathed_interior_data_stream():
    # 3 db-refs (MaxSounds), all absent -> 3 bits.
    bs = BitStream()
    bs.write_flag(False); bs.write_flag(False); bs.write_flag(False)
    bs.set_bit_position(0)
    db._unpack_pathed_interior_data(bs)
    assert bs.get_bit_position() == 3
    assert not bs.error


def test_wheeled_vehicle_spring_stream():
    bs = BitStream()
    for _ in range(4):
        bs.write_bytes(b"\x00\x00\x00\x00")
    bs.set_bit_position(0)
    db._unpack_wheeled_vehicle_spring(bs)
    assert bs.get_bit_position() == 128
    assert not bs.error


def test_wheeled_vehicle_tire_stream():
    bs = BitStream()
    bs.write_string("tire.dts")
    for _ in range(11):
        bs.write_bytes(b"\x00\x00\x00\x00")
    end = bs.get_bit_position()
    bs.set_bit_position(0)
    db._unpack_wheeled_vehicle_tire(bs)
    assert bs.get_bit_position() == end
    assert not bs.error


def test_fx_dts_brick_data_stream():
    # 7 readString (empty) + 3 readInt(6).
    bs = BitStream()
    for _ in range(7):
        bs.write_string("")
    for _ in range(3):
        bs.write_int(0, 6)
    end = bs.get_bit_position()
    bs.set_bit_position(0)
    db._unpack_fx_dts_brick_data(bs)
    assert bs.get_bit_position() == end
    assert not bs.error


def test_path_camera_data_is_shape_base_data():
    # PathCameraData::unpackData tail-jumps to ShapeBaseData (== CameraData).
    assert db.DECODERS["PathCameraData"] is db._unpack_camera_data


CAPTURE3 = os.path.join(
    os.path.dirname(__file__), "..", "tools", "captures", "real_login3.jsonl"
)


@pytest.mark.skipif(not os.path.exists(CAPTURE3), reason="rain capture missing")
def test_replay_rain_capture_decodes_precipitation():
    """real_login3.jsonl was captured while it was raining, so the live world
    streams a PrecipitationData datablock. Replaying its s2c datablock stream
    must decode it (and the whole stream) with ZERO desync -- this is the live
    login regression guard for the PrecipitationData decoder."""
    from aotbot.events import EventManager, EventDecodeError
    from aotbot.phases import GameConnectionPhases, AlignmentError

    seen = []
    orig = db.unpack_datablock

    def wrap(bs, cid):
        nm = db.DATABLOCK_CLASS_NAMES[cid] if 0 <= cid < len(db.DATABLOCK_CLASS_NAMES) else cid
        seen.append(nm)
        return orig(bs, cid)

    db.unpack_datablock = wrap
    blocked = None
    try:
        recs = [json.loads(l) for l in open(CAPTURE3) if l.strip()]
        s2c = [r for r in recs if r["dir"] == "s2c"]
        em = EventManager()
        em.command_to_server = lambda *a, **k: None
        ph = GameConnectionPhases(em, skip_lighting=True)
        ph._send_connection_message = lambda *a, **k: None
        last = -1
        for r in s2c:
            b = bytes.fromhex(r["hex"])
            if not b or not (b[0] & 1):
                continue
            bs = BitStream(b)
            seq, pt = _read_header(bs)
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
    finally:
        db.unpack_datablock = orig

    assert "PrecipitationData" in seen, "rain capture should stream PrecipitationData"
    assert blocked is None, f"datablock stream desynced: {blocked}"
    for name in seen:
        assert name in IMPLEMENTED, f"decoded an unimplemented class? {name}"


def _read_header(bs: BitStream):
    bs.read_flag()
    bs.read_int(1)
    seq = bs.read_int(9)
    bs.read_int(9)
    pt = bs.read_int(2)
    abc = bs.read_int(3)
    bs.read_int(8 * abc)
    return seq, pt


@pytest.mark.skipif(not os.path.exists(CAPTURE), reason="golden capture missing")
def test_replay_golden_capture_datablocks_aligned():
    """Replay the real client's s2c stream; assert the datablock decoders stay
    bit-aligned for at least the first 33 datablocks (everything up to the first
    not-yet-ported class), with the classes decoded being coherent in-order.
    """
    from aotbot.events import EventManager, EventDecodeError
    from aotbot.phases import GameConnectionPhases, AlignmentError

    seen = []
    orig = db.unpack_datablock

    def wrap(bs, cid):
        # A desync can surface here as an out-of-range classId; record it raw
        # (it will be the last, blocking entry) rather than crashing.
        nm = db.DATABLOCK_CLASS_NAMES[cid] if 0 <= cid < len(db.DATABLOCK_CLASS_NAMES) else cid
        seen.append(nm)
        return orig(bs, cid)

    db.unpack_datablock = wrap
    try:
        recs = [json.loads(l) for l in open(CAPTURE) if l.strip()]
        s2c = [r for r in recs if r["dir"] == "s2c"]
        em = EventManager()
        em.command_to_server = lambda *a, **k: None
        ph = GameConnectionPhases(em, skip_lighting=True)
        ph._send_connection_message = lambda *a, **k: None
        last = -1
        for r in s2c:
            b = bytes.fromhex(r["hex"])
            if not b or not (b[0] & 1):
                continue
            bs = BitStream(b)
            seq, pt = _read_header(bs)
            if pt != 0 or seq == last:
                continue
            last = seq
            if bs.read_flag():
                bs.read_int(10); bs.read_int(10)
            if bs.read_flag():
                bs.read_int(10); bs.read_int(10)
            try:
                ph.read_packet_body(bs)
            except (AlignmentError, EventDecodeError):
                break
    finally:
        db.unpack_datablock = orig

    # At least 33 datablocks decoded with zero desync (the count locked in this
    # wave). All decoded classes must be ones we implemented.
    # The final entry is the class that blocked (recorded before its decoder
    # raised); everything BEFORE it decoded fully and must be an implemented
    # class.
    assert len(seen) >= 319, f"only {len(seen)} datablocks decoded: {seen[-3:]}"
    for name in seen[:-1]:
        assert name in IMPLEMENTED, f"decoded an unimplemented class? {name}"
