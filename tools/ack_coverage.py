#!/usr/bin/env python3
"""Check whether EVERY s2c DATA packet seq was positively-acked by the bot in
its c2s ackMask -- i.e. simulate the SERVER's mHighestAckedSeq + notify walk on
the bot's c2s acks and report any s2c DATA seq the server would see as dropped.

A server event-bearing packet that the server sees as dropped (NACKed) blocks
mLastAckedEventSeq until it is resent+acked. If a particular s2c seq is *never*
positively acked by the bot, that's the stall.

Usage: ack_coverage.py <capture.jsonl>
"""
import json
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])
from aotbot.bitstream import BitStream  # noqa: E402


def read_header(data):
    bs = BitStream(data)
    bs.read_flag(); bs.read_int(1)
    seq = bs.read_int(9)
    ack = bs.read_int(9)
    pt = bs.read_int(2)
    abc = bs.read_int(3)
    mask = bs.read_int(8 * abc)
    return seq, ack, pt, abc, mask


def main():
    path = sys.argv[1]
    recs = [json.loads(l) for l in open(path) if l.strip()]

    # All s2c DATA packet seqs the server actually sent (unwrap 9-bit).
    s2c_seqs_sent = set()
    s2c_last = 0
    s2c_ext_max = 0
    for r in recs:
        if r["dir"] != "s2c":
            continue
        data = bytes.fromhex(r["hex"])
        if not data or not (data[0] & 1):
            continue
        seq, ack, pt, abc, mask = read_header(data)
        ext = seq | (s2c_last & ~0x1FF)
        if ext < s2c_last:
            ext += 0x200
        s2c_last = ext
        if pt == 0:
            s2c_seqs_sent.add(ext)
            s2c_ext_max = max(s2c_ext_max, ext)

    # Simulate the server side: walk c2s acks, mark each s2c seq delivered/not.
    # Server's mHighestAckedSeq over the s2c stream; we extend the 9-bit ack.
    delivered = set()
    nacked = set()
    highest = 0
    c2s_ack_last = 0
    for r in recs:
        if r["dir"] != "c2s":
            continue
        data = bytes.fromhex(r["hex"])
        if not data or not (data[0] & 1):
            continue
        seq, ack, pt, abc, mask = read_header(data)
        ext_ack = ack | (c2s_ack_last & ~0x1FF)
        if ext_ack < c2s_ack_last:
            ext_ack += 0x200
        c2s_ack_last = max(c2s_ack_last, ext_ack)
        for i in range(highest + 1, ext_ack + 1):
            ok = bool(mask & (1 << (ext_ack - i)))
            if ok:
                delivered.add(i)
                nacked.discard(i)
            else:
                if i not in delivered:
                    nacked.add(i)
        if ext_ack > highest:
            highest = ext_ack

    print(f"capture: {path}")
    print(f"s2c DATA seqs sent: {len(s2c_seqs_sent)}  max ext seq: {s2c_ext_max}")
    print(f"server saw highest ack of us: {highest}")
    print(f"s2c seqs positively delivered (per server): {len(delivered)}")
    never = sorted(s for s in s2c_seqs_sent if s not in delivered and s <= highest)
    print(f"s2c DATA seqs sent but NEVER positively-acked (<=highest): {len(never)}")
    if never:
        print("  first 30:", never[:30])
    # also: how the server's notify-walk would advance (consecutive delivered)
    walk = 0
    while (walk + 1) in delivered:
        walk += 1
    print(f"server consecutive-delivered walk reaches s2c seq: {walk}")
    # the first gap:
    gap = walk + 1
    print(f"first non-consecutive-delivered s2c seq (the blocker): {gap} "
          f"(sent={gap in s2c_seqs_sent}, delivered={gap in delivered})")


if __name__ == "__main__":
    main()
