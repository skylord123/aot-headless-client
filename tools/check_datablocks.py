#!/usr/bin/env python3
"""Replay real_login.jsonl s2c; report datablock decode progress + first blocker.

Validates the per-class unpackData decoders bit-exactly: a correct decoder
advances the cursor with zero desync, so more datablocks decode before any
blocker. Run from the ageoftime-minimal-bot dir.
"""
import sys, json, logging
logging.disable(logging.CRITICAL)
sys.path.insert(0, ".")
from aotbot.bitstream import BitStream  # noqa: E402
from aotbot.events import EventManager, EventDecodeError  # noqa: E402
from aotbot.phases import GameConnectionPhases, AlignmentError  # noqa: E402
from aotbot import datablocks as db  # noqa: E402

orig = db.unpack_datablock
seen = []


def wrap(bs, cid):
    nm = db.DATABLOCK_CLASS_NAMES[cid] if cid < len(db.DATABLOCK_CLASS_NAMES) else cid
    seen.append(nm)
    return orig(bs, cid)


db.unpack_datablock = wrap

recs = [json.loads(l) for l in open(sys.argv[1] if len(sys.argv) > 1 else "tools/captures/real_login.jsonl") if l.strip()]
s2c = [r for r in recs if r["dir"] == "s2c"]
em = EventManager(); em.command_to_server = lambda *a, **k: None
ph = GameConnectionPhases(em, skip_lighting=True); ph._send_connection_message = lambda *a, **k: None


def rh(bs):
    bs.read_flag(); bs.read_int(1); s = bs.read_int(9); bs.read_int(9); pt = bs.read_int(2); abc = bs.read_int(3); bs.read_int(8 * abc); return s, pt


last = -1
blocked = None
for i, r in enumerate(s2c):
    b = bytes.fromhex(r["hex"])
    if not b or not (b[0] & 1):
        continue
    bs = BitStream(b); seq, pt = rh(bs)
    if pt != 0 or seq == last:
        continue
    last = seq
    if bs.read_flag(): bs.read_int(10); bs.read_int(10)
    if bs.read_flag(): bs.read_int(10); bs.read_int(10)
    try:
        ph.read_packet_body(bs)
    except (AlignmentError, EventDecodeError) as e:
        blocked = (i, seq, str(e))
        break

print(f"datablocks decoded clean: {len(seen)}")
if seen:
    print(f"last decoded: {seen[-3:]}")
if blocked:
    print(f"FIRST BLOCK at iter {blocked[0]} seq {blocked[1]}: {blocked[2]}")
else:
    print("NO BLOCK -- reached end of stream")
