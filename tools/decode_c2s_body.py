#!/usr/bin/env python3
"""Decode the c2s DATA packet bodies (control header + move stream + event
section presence) of a relay capture, to see what the real client sends that
the bot doesn't.

c2s control header (AoT write side): cameraPos flag(1) | checksum(32) |
startMoveId(32) | moveCount(5) | moveCount x Move(28 idle) | fov flag(1).
Then the event section.

Usage: decode_c2s_body.py <capture.jsonl> [maxrows]
"""
import json
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])
from aotbot.bitstream import BitStream  # noqa: E402

MAX_TRIGGER_KEYS = 6


def read_header(data):
    bs = BitStream(data)
    bs.read_flag(); bs.read_int(1)
    seq = bs.read_int(9)
    ack = bs.read_int(9)
    pt = bs.read_int(2)
    abc = bs.read_int(3)
    bs.read_int(8 * abc)
    return bs, seq, ack, pt


def read_one_move(bs):
    """Move::unpack idle form; returns dict of notable fields (non-idle if any
    rotation/trigger present)."""
    nonidle = False
    for _ in range(3):
        if bs.read_flag():
            bs.read_int(16)
            nonidle = True
    bs.read_int(6); bs.read_int(6); bs.read_int(6)  # px py pz
    bs.read_flag()  # freeLook
    trig = 0
    for _ in range(MAX_TRIGGER_KEYS):
        if bs.read_flag():
            trig += 1
    return nonidle, trig


def main():
    path = sys.argv[1]
    maxrows = int(sys.argv[2]) if len(sys.argv) > 2 else 80
    recs = [json.loads(l) for l in open(path) if l.strip()]
    c2s = [r for r in recs if r["dir"] == "c2s"]

    shown = 0
    move_count_hist = {}
    event_pkts = 0
    for r in c2s:
        data = bytes.fromhex(r["hex"])
        if not data or not (data[0] & 1):
            continue
        bs, seq, ack, pt = read_header(data)
        if pt != 0:
            continue
        # rate block
        ratebits = []
        if bs.read_flag():
            bs.read_int(10); bs.read_int(10); ratebits.append("rate")
        if bs.read_flag():
            bs.read_int(10); bs.read_int(10); ratebits.append("maxrate")
        cam = bs.read_flag()
        checksum = bs.read_int(32)
        start_move = bs.read_int(32)
        mcount = bs.read_int(5)
        move_count_hist[mcount] = move_count_hist.get(mcount, 0) + 1
        nonidle_moves = 0
        trig_total = 0
        for _ in range(mcount):
            ni, tr = read_one_move(bs)
            nonidle_moves += ni
            trig_total += tr
        fov = bs.read_flag()
        # event section: peek first phase bit
        bits_left = bs.bits_remaining() if hasattr(bs, "bits_remaining") else None
        ev_unguar = bs.read_flag()
        has_events = ev_unguar or False
        # try to see guaranteed-phase presence too
        guar = None
        if not ev_unguar:
            guar = bs.read_flag()
            has_events = bool(guar)
        if has_events:
            event_pkts += 1
        if shown < maxrows:
            print(f"seq={seq:4} ack={ack:4} cam={int(cam)} chk={checksum:#010x} "
                  f"startMove={start_move:6} mcount={mcount} nonidle={nonidle_moves} "
                  f"trig={trig_total} fov={int(fov)} rate={ratebits} "
                  f"events={'YES' if has_events else '-'} len={len(data)}")
            shown += 1

    print("move count histogram:", dict(sorted(move_count_hist.items())))
    print("c2s packets carrying events:", event_pkts)


if __name__ == "__main__":
    main()
