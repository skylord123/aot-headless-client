#!/usr/bin/env python3
"""Replay a capture up to a target line, then bit-trace that packet's body:
control header fields, every event (classid + cursor), every ghost record
(id / new / class / bits consumed). State (ghost table, string table, phases)
is built by replaying every prior packet exactly like the live client.

Run: .venv/bin/python tools/trace_capture_pkt.py CAPTURE.jsonl TARGET_LINE
"""
import json
import logging
import sys

sys.path.insert(0, ".")

from aotbot.bitstream import BitStream
from aotbot import protocol_constants as pc
from aotbot import ghosts as gh
from aotbot.events import EventManager, EventDecodeError
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


def rate_block(bs: BitStream):
    if bs.read_flag():
        bs.read_int(10)
        bs.read_int(10)
    if bs.read_flag():
        bs.read_int(10)
        bs.read_int(10)


def _install_bitlog():
    """Log every primitive BitStream read with cursor and value (class-level)."""
    for op in ("read_int", "read_flag", "read_bytes", "read_float",
               "read_signed_int", "read_string", "read_ranged_u32"):
        if not hasattr(BitStream, op):
            continue
        orig = getattr(BitStream, op)

        def make(orig=orig, op=op):
            def wrapper(self, *a):
                pos = self.get_bit_position()
                val = orig(self, *a)
                out = val if not isinstance(val, (bytes, bytearray)) else val.hex()
                print(f"        bit[{pos:5d}+{self.get_bit_position()-pos:3d}] "
                      f"{op}{a} -> {out!r}")
                return val
            return wrapper

        setattr(BitStream, op, make())


def trace(ph: GameConnectionPhases, em: EventManager, data: bytes, seq: int):
    # Surface the per-event envelope logs (SimDataBlockEvent id/class/index)
    # and per-datablock bit consumption for the traced packet only.
    logging.disable(logging.NOTSET)
    logging.basicConfig(level=logging.DEBUG, format="      %(message)s")
    import aotbot.datablocks as db

    orig_unpack = db.unpack_datablock

    def wrapped(bs_, cid):
        name = (db.DATABLOCK_CLASS_NAMES[cid]
                if 0 <= cid < len(db.DATABLOCK_CLASS_NAMES) else f"<{cid}>")
        s = bs_.get_bit_position()
        try:
            orig_unpack(bs_, cid)
        finally:
            print(f"      -> datablock {name}({cid}) consumed={bs_.get_bit_position() - s} "
                  f"err={bs_.error}")

    db.unpack_datablock = wrapped
    total = len(data) * 8
    if "--bitlog" in sys.argv:
        _install_bitlog()
    bs = BitStream(data)
    read_header(bs)
    rate_block(bs)
    print(f"--- trace seq={seq} len={total} bits, body starts at {bs.get_bit_position()}")
    bs.set_string_buffer(bytearray(256))

    ph._read_control_header(bs)
    print(f"control header done: cur={bs.get_bit_position()} moveAck={ph.last_move_ack} "
          f"ctrl_ghost={ph._control_ghost_id}")

    # Event section, one event at a time (mirror of EventManager.read_events).
    prev_seq = -1
    ungtd = True
    for _ in range(4096):
        bit = bs.read_flag()
        if bs.error:
            print(f"  bs.error reading event presence at cur={bs.get_bit_position()}")
            return
        if ungtd and not bit:
            ungtd = False
            bit = bs.read_flag()
        if not ungtd and not bit:
            break
        if not ungtd:
            if bs.read_flag():
                eseq = (prev_seq + 1) & 0x7F
            else:
                eseq = bs.read_int(7)
            prev_seq = eseq
        else:
            eseq = None
        classid = bs.read_int(pc.NET_CLASS_BITS_EVENT)
        start = bs.get_bit_position()
        try:
            em._read_one_event(bs, classid)
            print(f"  event classid={classid:2d} seq={eseq} consumed={bs.get_bit_position()-start} "
                  f"cur={bs.get_bit_position()} err={bs.error}")
        except EventDecodeError as e:
            print(f"  event classid={classid:2d} seq={eseq} -> ERROR {e} cur={bs.get_bit_position()}")
            return
        if bs.error:
            print(f"  bs.error inside event classid={classid} cur={bs.get_bit_position()}")
            return
    print(f"events done: cur={bs.get_bit_position()} ghosting={ph.ghosting_active}")

    if not ph.ghosting_active:
        print(f"not ghosting; final cur={bs.get_bit_position()} of {total} "
              f"(residual {total - bs.get_bit_position()})")
        return
    if not bs.read_flag():
        print(f"no ghost updates; final cur={bs.get_bit_position()} of {total} "
              f"(residual {total - bs.get_bit_position()})")
        return
    id_size = bs.read_int(pc.GHOST_INDEX_BIT_SIZE) + 3
    print(f"ghost section: id_size={id_size}")
    for _ in range(1 << 14):
        if bs.error:
            print(f"  bs.error in ghost loop at cur={bs.get_bit_position()}")
            return
        if not bs.read_flag():
            break
        ghost_id = bs.read_int(id_size)
        if bs.read_flag():
            print(f"  ghost {ghost_id}: REMOVE (known={ghost_id in ph._ghost_classes})")
            ph._ghost_classes.pop(ghost_id, None)
            continue
        is_new = ghost_id not in ph._ghost_classes
        if is_new:
            class_id = bs.read_int(pc.NET_CLASS_BITS_OBJECT)
            ph._ghost_classes[ghost_id] = class_id
        else:
            class_id = ph._ghost_classes[ghost_id]
        name = (gh.OBJECT_CLASS_NAMES[class_id]
                if 0 <= class_id < len(gh.OBJECT_CLASS_NAMES) else f"<{class_id}>")
        start = bs.get_bit_position()
        try:
            gh.unpack_update(bs, class_id, is_new)
        except gh.GhostDecodeError as e:
            print(f"  ghost {ghost_id}: {'NEW' if is_new else 'upd'} {name}({class_id}) "
                  f"-> ERROR {e} cur={bs.get_bit_position()}")
            return
        print(f"  ghost {ghost_id}: {'NEW' if is_new else 'upd'} {name}({class_id}) "
              f"consumed={bs.get_bit_position()-start} cur={bs.get_bit_position()} err={bs.error}")
    print(f"final cur={bs.get_bit_position()} of {total} "
          f"(residual {total - bs.get_bit_position()}) err={bs.error}")


def main():
    path, target = sys.argv[1], int(sys.argv[2])
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
        if i == target:
            trace(ph, em, data, seq)
            return
        rate_block(bs)
        try:
            ph.read_packet_body(bs)
        except (AlignmentError, Exception):  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
