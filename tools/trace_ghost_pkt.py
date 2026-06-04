#!/usr/bin/env python3
"""Trace the ghost section of a single s2c packet (by iter index), logging each
ghost id / class / bit-cursor delta so we can see which class's unpackUpdate
mis-sizes and desyncs the section.

    .venv/bin/python tools/trace_ghost_pkt.py [capture.jsonl] [iter_index]
"""
import json
import logging
import sys

sys.path.insert(0, ".")
from aotbot.bitstream import BitStream  # noqa: E402
from aotbot import protocol_constants as pc  # noqa: E402
from aotbot.events import EventManager  # noqa: E402
from aotbot.phases import GameConnectionPhases  # noqa: E402
from aotbot import ghosts as gh  # noqa: E402

logging.disable(logging.CRITICAL)


def read_header(bs):
    bs.read_flag()
    bs.read_int(1)
    seq = bs.read_int(9)
    bs.read_int(9)
    pt = bs.read_int(2)
    abc = bs.read_int(3)
    bs.read_int(8 * abc)
    return seq, pt


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "tools/captures/real_login.jsonl"
    target_iter = int(sys.argv[2]) if len(sys.argv) > 2 else 822
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
        if i != target_iter:
            # decode normally to keep state aligned
            try:
                ph.read_packet_body(bs)
            except Exception:
                pass
            continue

        # --- trace this packet's ghost section ---
        print(f"=== iter {i} seq {seq} len {len(b)} bytes ({len(b)*8} bits) ===")
        bs.set_string_buffer(bytearray(256))
        ph._read_control_header(bs)
        em.read_events(bs)
        print(f"after events: cursor={bs.getCurPos()} (of {len(b)*8})")
        # manual ghost section trace
        if not ph.ghosting_active:
            print("not ghosting")
            return
        if not bs.read_flag():
            print("ghost section: no updates")
            return
        id_size = bs.read_int(pc.GHOST_INDEX_BIT_SIZE) + 3
        print(f"id_size={id_size}")
        n = 0
        while True:
            if not bs.read_flag():
                print("end of ghost loop")
                break
            ghost_id = bs.read_int(id_size)
            removed = bs.read_flag()
            if removed:
                print(f"  ghost {ghost_id}: REMOVE")
                ph._ghost_classes.pop(ghost_id, None)
                continue
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
                consumed = bs.getCurPos() - start
                print(f"  ghost {ghost_id}: {'NEW ' if is_new else 'upd '}{name}(id={class_id}) consumed={consumed} cursor={bs.getCurPos()}")
            except gh.GhostDecodeError as e:
                print(f"  ghost {ghost_id}: {'NEW ' if is_new else 'upd '}{name}(id={class_id}) -> DECODE ERROR {e} cursor={bs.getCurPos()}")
                break
            n += 1
            if n > 200:
                print("too many")
                break
        print(f"final cursor={bs.getCurPos()} of {len(b)*8}; trailing bits={len(b)*8 - bs.getCurPos()}")
        return


if __name__ == "__main__":
    main()
