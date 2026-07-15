#!/usr/bin/env python3
"""Replay a JSONL capture's s2c stream through the CURRENT decode stack and
report the FIRST evidence of desync per category:

  * exception     -- AlignmentError/EventDecodeError raised (already logged live)
  * bs.error      -- a decoder read past the end of the packet (SILENT live!)
  * residual      -- packet "decoded fine" but left >= 8 unconsumed bits
                     (a flag/branch was misread somewhere earlier -- silent)
  * ghost-class   -- a NEW-ghost class id >= 50 (impossible on the wire from an
                     unchanged server; proof the ghost section was already
                     misaligned when it was read)

Mirrors client._read_body ordering, including the dnet header + rate block, and
keeps cross-packet state (phases/_ghost_classes/string table) exactly like the
live client so state-poisoning cascades reproduce.

Run: .venv/bin/python tools/replay_s2c_audit.py CAPTURE.jsonl [--verbose]
"""
import json
import logging
import sys

sys.path.insert(0, ".")

from aotbot.bitstream import BitStream
from aotbot import protocol_constants as pc
from aotbot.events import EventManager
from aotbot.phases import GameConnectionPhases, AlignmentError

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


def main() -> int:
    path = sys.argv[1]
    verbose = "--verbose" in sys.argv

    em = EventManager()
    ph = GameConnectionPhases(em, skip_lighting=True, track_objects=False)

    # Instrument ghost-class registration to catch impossible class ids.
    bad_class_hits = []
    orig_setitem = dict.__setitem__  # noqa: F841 (documentation of intent)

    class WatchedGhostClasses(dict):
        def __setitem__(self, ghost_id, class_id):
            if class_id >= 50:
                bad_class_hits.append((ghost_id, class_id))
            super().__setitem__(ghost_id, class_id)

    ph._ghost_classes = WatchedGhostClasses()

    firsts = {}
    counts = {"exception": 0, "bs.error": 0, "residual": 0, "ghost-class": 0}
    n_data = 0
    prev_seq = None

    for i, line in enumerate(open(path)):
        rec = json.loads(line)
        if rec.get("dir") != "s2c":
            continue
        data = bytes.fromhex(rec["hex"])
        if not data or not (data[0] & 0x01):
            continue  # OOB handshake packet
        bs = BitStream(data)
        seq, ptype = read_header(bs)
        if ptype != pc.PACKET_TYPE_DATA:
            continue
        if prev_seq is not None and seq == prev_seq:
            continue  # duplicate seq: live client skips the body
        prev_seq = seq
        n_data += 1
        # rate block (netconn._handle_packet_body)
        if bs.read_flag():
            bs.read_int(10)
            bs.read_int(10)
        if bs.read_flag():
            bs.read_int(10)
            bs.read_int(10)

        n_bad_before = len(bad_class_hits)
        exc_txt = None
        try:
            ph.read_packet_body(bs)
        except (AlignmentError, Exception) as exc:  # noqa: BLE001
            exc_txt = f"{type(exc).__name__}: {exc}"

        residual = len(data) * 8 - bs.get_bit_position()
        events = []
        if exc_txt:
            events.append(("exception", exc_txt))
        if bs.error:
            events.append(("bs.error", f"cursor={bs.get_bit_position()} len={len(data)*8}"))
        if exc_txt is None and not bs.error and residual >= 8:
            events.append(("residual", f"{residual} bits unconsumed of {len(data)*8}"))
        if len(bad_class_hits) > n_bad_before:
            events.append(("ghost-class", f"registered {bad_class_hits[n_bad_before:]}"))

        for kind, detail in events:
            counts[kind] += 1
            if kind not in firsts:
                firsts[kind] = (i, seq, detail)
                print(f"FIRST {kind:11s} line={i} seq={seq}: {detail}")
            elif verbose:
                print(f"      {kind:11s} line={i} seq={seq}: {detail}")

    print(f"\n{path}: {n_data} s2c DATA packets")
    for kind, cnt in counts.items():
        print(f"  {kind:11s}: {cnt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
