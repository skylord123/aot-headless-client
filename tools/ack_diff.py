#!/usr/bin/env python3
"""Decode connected-packet HEADERS from a relay JSONL capture and tabulate the
ack/notify accounting over time, to diff the bot's ack behaviour vs the real
client's during heavy event reception.

Header = gameFlag(1)|connectSeq(1)|seq(9)|ack(9)|type(2)|ackByteCount(3)|ackMask(count*8).
Only connected packets (first byte & 1) are decoded; OOB packets are skipped.

Usage: ack_diff.py <capture.jsonl> [c2s|s2c] [--full]
"""
import json
import sys
from dataclasses import dataclass

sys.path.insert(0, __file__.rsplit("/", 2)[0])
from aotbot.bitstream import BitStream  # noqa: E402

TYPE = {0: "DATA", 1: "PING", 2: "ACK"}


@dataclass
class Hdr:
    seq: int
    ack: int
    ptype: int
    ack_byte_count: int
    ack_mask: int
    body_bits: int


def decode_header(data: bytes) -> Hdr | None:
    if not data or not (data[0] & 1):
        return None
    bs = BitStream(data)
    bs.read_flag()  # gameFlag
    bs.read_int(1)  # connectSeq parity
    seq = bs.read_int(9)
    ack = bs.read_int(9)
    ptype = bs.read_int(2)
    if ptype > 2:
        return None
    abc = bs.read_int(3)
    mask = bs.read_int(8 * abc)
    body_bits = len(data) * 8 - (25 + 8 * abc)
    return Hdr(seq, ack, ptype, abc, mask, body_bits)


def main():
    path = sys.argv[1]
    want_dir = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in ("c2s", "s2c") else None
    full = "--full" in sys.argv

    rows = []
    with open(path) as f:
        for line in f:
            rec = json.loads(line)
            if want_dir and rec["dir"] != want_dir:
                continue
            data = bytes.fromhex(rec["hex"])
            h = decode_header(data)
            if h is None:
                continue
            rows.append((rec["t"], rec["dir"], h, len(data)))

    print(f"{path}  dir={want_dir or 'both'}  {len(rows)} connected packets")
    print(f"{'#':>4} {'t':>9} {'dir':>4} {'seq':>4} {'ack':>4} {'type':>4} "
          f"{'abc':>3} {'mask':>10} {'len':>4}")
    counts = {}
    for i, (t, d, h, ln) in enumerate(rows):
        counts[(d, TYPE.get(h.ptype))] = counts.get((d, TYPE.get(h.ptype)), 0) + 1
        if full or i < 200 or i % 1 == 0:
            print(f"{i:>4} {t:>9.1f} {d:>4} {h.seq:>4} {h.ack:>4} "
                  f"{TYPE.get(h.ptype):>4} {h.ack_byte_count:>3} "
                  f"0x{h.ack_mask:08x} {ln:>4}")
    print("type mix:", counts)


if __name__ == "__main__":
    main()
