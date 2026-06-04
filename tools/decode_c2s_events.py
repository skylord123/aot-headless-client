#!/usr/bin/env python3
"""Decode the c2s event sections (the events the CLIENT sends the server) of a
relay capture, to see what reliable events the real client posts during the
load/ghost window that the bot doesn't.

Usage: decode_c2s_events.py <capture.jsonl>
"""
import json
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])
from aotbot.bitstream import BitStream  # noqa: E402
from aotbot import protocol_constants as pc  # noqa: E402
from aotbot.events import EventManager, EVENT_SEQ_MASK  # noqa: E402
import logging
logging.disable(logging.CRITICAL)

MAX_TRIGGER_KEYS = 6
EVENT_NAMES = {v: k for k, v in pc.EVENT_CLASS_IDS.items()}


def read_header(data):
    bs = BitStream(data)
    bs.read_flag(); bs.read_int(1)
    seq = bs.read_int(9); ack = bs.read_int(9); pt = bs.read_int(2)
    abc = bs.read_int(3); bs.read_int(8 * abc)
    return bs, seq, ack, pt


def skip_move(bs):
    for _ in range(3):
        if bs.read_flag():
            bs.read_int(16)
    bs.read_int(6); bs.read_int(6); bs.read_int(6)
    bs.read_flag()
    for _ in range(MAX_TRIGGER_KEYS):
        bs.read_flag()


def main():
    path = sys.argv[1]
    recs = [json.loads(l) for l in open(path) if l.strip()]
    c2s = [r for r in recs if r["dir"] == "c2s"]

    # A receive-side EventManager interpreting the CLIENT's events as the SERVER
    # would (so RemoteCommandEvent verbs detag against the client's send table,
    # which we learn from the client's own NetStringEvents).
    em = EventManager()
    em.set_default_handler(lambda v, a, e: None)

    for r in c2s:
        data = bytes.fromhex(r["hex"])
        if not data or not (data[0] & 1):
            continue
        bs, seq, ack, pt = read_header(data)
        if pt != 0:
            continue
        if bs.read_flag():
            bs.read_int(10); bs.read_int(10)
        if bs.read_flag():
            bs.read_int(10); bs.read_int(10)
        bs.set_string_buffer(bytearray(256))
        bs.read_flag()       # cameraPos
        bs.read_int(32)      # checksum
        bs.read_int(32)      # startMoveId
        mcount = bs.read_int(5)
        for _ in range(mcount):
            skip_move(bs)
        bs.read_flag()       # fov
        # event section
        evs = []
        prev = -1
        unguar = True
        try:
            for _ in range(256):
                bit = bs.read_flag()
                if bs.error:
                    break
                if unguar and not bit:
                    unguar = False
                    bit = bs.read_flag()
                    if bs.error:
                        break
                if not unguar and not bit:
                    break
                evseq = None
                if not unguar:
                    if bs.read_flag():
                        evseq = (prev + 1) & EVENT_SEQ_MASK
                    else:
                        evseq = bs.read_int(7)
                    prev = evseq
                cid = bs.read_int(pc.NET_CLASS_BITS_EVENT)
                name = EVENT_NAMES.get(cid, f"classId{cid}")
                detail = ""
                if cid == pc.NET_STRING_EVENT_CLASS_ID:
                    slot = bs.read_int(pc.STRING_TABLE_ENTRY_BIT_SIZE)
                    txt = bs.read_string()
                    em.recv_table.map_string(slot, txt)
                    detail = f"slot{slot}={txt!r}"
                elif cid == pc.REMOTE_COMMAND_EVENT_CLASS_ID:
                    argc = bs.read_int(5)
                    argv = [em.unpack_string(bs) for _ in range(argc)]
                    argv = [em.detag(a) for a in argv]
                    detail = f"cmd={argv}"
                elif cid == pc.EVENT_CLASS_IDS.get("ConnectionMessageEvent"):
                    s = bs.read_int(32); m = bs.read_int(3); g = bs.read_int(15)
                    detail = f"connMsg msg={m} seq={s} ghostCount={g}"
                else:
                    detail = "(payload not decoded)"
                evs.append(f"[seq{evseq} {name} {detail}]")
        except Exception as e:
            evs.append(f"<decode err {e}>")
        finally:
            bs.set_string_buffer(None)
        if evs:
            print(f"c2s seq={seq:4} ack={ack:4} mcount={mcount} len={len(data)}: " + " ".join(evs))


if __name__ == "__main__":
    main()
