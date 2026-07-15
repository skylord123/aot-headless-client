"""Bit-exactness regression: replay recorded live s2c captures through the full
decode stack (control header -> events -> ghosts) and assert no packet loses
alignment.

These captures shipped in tools/captures/ and cover the datablock load stream,
the GhostAlways burst, and live gameplay (items dropping, NPCs moving, chat).
Any decoder that consumes the wrong number of bits shows up as an exception, a
silent bitstream over-read (bs.error), or a packet with >= 16 unconsumed bits
("residual" -- up to ~9 bits is normal: the event-section terminator can land
in the byte padding).

This is the regression net for the WAVE-19 decoder fixes (ShapeBaseImageData
state-tail + d05/d08 head, Box6F sign flags, ProjectileData bare flag,
StaticBrickDataEvent 64-row palette, fxGrassReplicator phantom flags, Item
blocks 2-4 + rotation-axis field): before them, every capture desynced during
the datablock load phase, which silently ate guaranteed-ordered events and --
on the live VPS -- eventually zombied the session.
"""

import json
import os

import pytest

import aotbot.protocol_constants as pc
from aotbot.bitstream import BitStream
from aotbot.events import EventManager
from aotbot.phases import GameConnectionPhases

CAPTURE_DIR = os.path.join(os.path.dirname(__file__), "..", "tools", "captures")

# Captures that must replay 100% clean. (real_login.jsonl is excluded: it still
# carries a known ±1-bit ambiguity in two rare Projectile update paths, pending
# a winedbg trace.)
CLEAN_CAPTURES = [
    "bot_session.jsonl",
    "bot_session_postfix.jsonl",
    "bot_session_213stall.jsonl",
    "live_rain_freshacct.jsonl",
    "live_session_dbg.jsonl",
    "real_login3.jsonl",
]


def _read_header(bs: BitStream):
    bs.read_flag()
    bs.read_int(pc.PACKET_HEADER_CONNECT_SEQ_BITS)
    seq = bs.read_int(pc.PACKET_HEADER_SEQ_BITS)
    bs.read_int(pc.PACKET_HEADER_ACK_START_BITS)
    ptype = bs.read_int(pc.PACKET_HEADER_TYPE_BITS)
    abc = bs.read_int(pc.PACKET_HEADER_ACK_BYTE_COUNT_BITS)
    bs.read_int(8 * abc)
    return seq, ptype


@pytest.mark.parametrize("capture", CLEAN_CAPTURES)
def test_capture_replays_bit_exact(capture):
    path = os.path.join(CAPTURE_DIR, capture)
    if not os.path.exists(path):
        pytest.skip(f"{capture} not present")

    em = EventManager()
    em.set_default_handler(lambda v, a, e: None)
    em.request_send = lambda: None
    ph = GameConnectionPhases(em, skip_lighting=True, track_objects=False)

    bad = []
    n = 0
    prev_seq = None
    with open(path) as f:
        for i, line in enumerate(f):
            rec = json.loads(line)
            if rec.get("dir") != "s2c":
                continue
            data = bytes.fromhex(rec["hex"])
            if not data or not (data[0] & 0x01):
                continue
            bs = BitStream(data)
            seq, ptype = _read_header(bs)
            if ptype != pc.PACKET_TYPE_DATA or seq == prev_seq:
                continue
            prev_seq = seq
            n += 1
            if bs.read_flag():
                bs.read_int(10)
                bs.read_int(10)
            if bs.read_flag():
                bs.read_int(10)
                bs.read_int(10)
            exc = None
            try:
                ph.read_packet_body(bs)
            except Exception as e:  # noqa: BLE001
                exc = e
            residual = len(data) * 8 - bs.get_bit_position()
            if exc is not None or bs.error or residual >= 16:
                bad.append(
                    f"line={i} seq={seq} residual={residual} "
                    f"err={bs.error} exc={exc}"
                )

    assert n > 0, "capture contained no s2c DATA packets"
    assert not bad, f"{len(bad)}/{n} packets lost alignment: " + "; ".join(bad[:5])
