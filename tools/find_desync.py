#!/usr/bin/env python3
"""Find the packet that raises, and trace its ghost section with cursor deltas."""
import json
import logging
import sys

sys.path.insert(0, ".")
from aotbot.bitstream import BitStream
from aotbot import protocol_constants as pc
from aotbot.events import EventManager, EventDecodeError
from aotbot.phases import GameConnectionPhases, AlignmentError
from aotbot import ghosts as gh

logging.disable(logging.CRITICAL)


def read_header(bs):
    bs.read_flag(); bs.read_int(1)
    seq = bs.read_int(9); bs.read_int(9)
    pt = bs.read_int(2); abc = bs.read_int(3)
    bs.read_int(8 * abc)
    return seq, pt


def trace_pkt(ph, em, b, seq, i):
    bs = BitStream(b)
    read_header(bs)
    if bs.read_flag():
        bs.read_int(10); bs.read_int(10)
    if bs.read_flag():
        bs.read_int(10); bs.read_int(10)
    print(f"=== iter {i} seq {seq} len {len(b)} ({len(b)*8} bits) ===")
    bs.set_string_buffer(bytearray(256))
    ph._read_control_header(bs)
    em.read_events(bs)
    print(f"after events: cursor={bs.getCurPos()} of {len(b)*8}; ghosting={ph.ghosting_active}")
    if not ph.ghosting_active:
        print("not ghosting"); return
    if not bs.read_flag():
        print("no ghost updates"); return
    id_size = bs.read_int(pc.GHOST_INDEX_BIT_SIZE) + 3
    print(f"id_size={id_size}")
    n = 0
    while True:
        if not bs.read_flag():
            print("end of loop"); break
        ghost_id = bs.read_int(id_size)
        if bs.read_flag():
            print(f"  ghost {ghost_id}: REMOVE"); ph._ghost_classes.pop(ghost_id, None); continue
        is_new = ghost_id not in ph._ghost_classes
        if is_new:
            class_id = bs.read_int(pc.NET_CLASS_BITS_OBJECT)
            ph._ghost_classes[ghost_id] = class_id
        else:
            class_id = ph._ghost_classes[ghost_id]
        name = gh.OBJECT_CLASS_NAMES[class_id] if 0 <= class_id < 50 else f"<{class_id}>"
        start = bs.getCurPos()
        try:
            gh.unpack_update(bs, class_id, is_new)
            print(f"  ghost {ghost_id}: {'NEW' if is_new else 'upd'} {name}(id={class_id}) consumed={bs.getCurPos()-start} cur={bs.getCurPos()}")
        except gh.GhostDecodeError as e:
            print(f"  ghost {ghost_id}: {'NEW' if is_new else 'upd'} {name}(id={class_id}) -> ERROR {e} cur={bs.getCurPos()}")
            return
        n += 1
        if n > 300:
            print("too many"); break
    print(f"final cur={bs.getCurPos()} of {len(b)*8}; trailing={len(b)*8 - bs.getCurPos()}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "tools/captures/real_login.jsonl"
    recs = [json.loads(l) for l in open(path) if l.strip()]
    s2c = [r for r in recs if r["dir"] == "s2c"]
    em = EventManager()
    em.set_default_handler(lambda v, a, e: None)
    ph = GameConnectionPhases(em, skip_lighting=True, track_objects=True)
    em.command_to_server = lambda *a, **k: None
    em._send_connection_message = lambda *a, **k: None
    ph._send_connection_message = lambda *a, **k: None

    last_seq = -1
    for i, r in enumerate(s2c):
        b = bytes.fromhex(r["hex"])
        if not b or not (b[0] & 1):
            continue
        bs = BitStream(b)
        seq, pt = read_header(bs)
        if pt != 0 or seq == last_seq:
            continue
        last_seq = seq
        if bs.read_flag():
            bs.read_int(10); bs.read_int(10)
        if bs.read_flag():
            bs.read_int(10); bs.read_int(10)
        try:
            ph.read_packet_body(bs)
        except (AlignmentError, EventDecodeError):
            # rewind and trace
            trace_pkt(ph, em, b, seq, i)
            return


if __name__ == "__main__":
    main()
