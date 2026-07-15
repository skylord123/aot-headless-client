#!/usr/bin/env python3
"""Replay captures and, for every packet whose body decode goes bad (bs.error,
big residual, or event-seq regression), report the datablock classes decoded in
that packet -- the LAST one started is the prime suspect for mis-consuming bits.

Run: .venv/bin/python tools/find_bad_datablocks.py CAPTURE.jsonl...
"""
import json
import logging
import sys
from collections import Counter

sys.path.insert(0, ".")

from aotbot.bitstream import BitStream
from aotbot import protocol_constants as pc
from aotbot import datablocks as db
from aotbot.events import EventManager
from aotbot.phases import GameConnectionPhases

logging.disable(logging.CRITICAL)


def read_header(bs: BitStream):
    bs.read_flag()
    bs.read_int(pc.PACKET_HEADER_CONNECT_SEQ_BITS)
    seq = bs.read_int(pc.PACKET_HEADER_SEQ_BITS)
    bs.read_int(pc.PACKET_HEADER_ACK_START_BITS)
    ptype = bs.read_int(pc.PACKET_HEADER_TYPE_BITS)
    abc = bs.read_int(pc.PACKET_HEADER_ACK_BYTE_COUNT_BITS)
    bs.read_int(8 * abc)
    return seq, ptype


suspects = Counter()
clean_counts = Counter()


def main() -> int:
    per_packet: list[str] = []

    orig_unpack = db.unpack_datablock

    def wrapped(bs_, cid):
        name = (db.DATABLOCK_CLASS_NAMES[cid]
                if 0 <= cid < len(db.DATABLOCK_CLASS_NAMES) else f"<{cid}>")
        per_packet.append(name)
        orig_unpack(bs_, cid)

    db.unpack_datablock = wrapped

    for path in sys.argv[1:]:
        em = EventManager()
        em.set_default_handler(lambda v, a, e: None)
        ph = GameConnectionPhases(em, skip_lighting=True, track_objects=False)
        em.request_send = lambda: None
        prev_seq = None
        for i, line in enumerate(open(path)):
            rec = json.loads(line)
            if rec.get("dir") != "s2c":
                continue
            data = bytes.fromhex(rec["hex"])
            if not data or not (data[0] & 0x01):
                continue
            bs = BitStream(data)
            seq, ptype = read_header(bs)
            if ptype != pc.PACKET_TYPE_DATA or seq == prev_seq:
                continue
            prev_seq = seq
            if bs.read_flag():
                bs.read_int(10)
                bs.read_int(10)
            if bs.read_flag():
                bs.read_int(10)
                bs.read_int(10)
            per_packet.clear()
            exc = None
            try:
                ph.read_packet_body(bs)
            except Exception as e:  # noqa: BLE001
                exc = e
            residual = len(data) * 8 - bs.get_bit_position()
            bad = exc is not None or bs.error or residual >= 16
            if bad and per_packet:
                # Prime suspect: the last datablock started in this packet.
                suspects[per_packet[-1]] += 1
                print(f"{path.split('/')[-1]} line={i} seq={seq} "
                      f"residual={residual} err={bs.error} exc={exc}: {per_packet}")
            elif per_packet:
                for n in per_packet:
                    clean_counts[n] += 1

    print("\n=== suspect (last-started in a bad packet) counts ===")
    for name, cnt in suspects.most_common():
        print(f"  {name:30s} bad-last={cnt:4d}  clean-decodes={clean_counts.get(name, 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


