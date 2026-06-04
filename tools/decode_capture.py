#!/usr/bin/env python3
"""Decode a udp_relay.py JSONL capture of the REAL AoT client and dump the
client->server (c2s) packet bodies -- the control/move header + event section --
so we can diff against what our bot writes.

Run from the ageoftime-minimal-bot dir:
    .venv/bin/python tools/decode_capture.py tools/captures/real_login.jsonl
"""
import json
import sys

sys.path.insert(0, ".")
from aotbot.bitstream import BitStream  # noqa: E402
from aotbot import protocol_constants as pc  # noqa: E402
from aotbot.events import EventManager  # noqa: E402
from aotbot.phases import _read_compressed_point  # noqa: E402

DATA = getattr(pc, "PACKET_TYPE_DATA", 0)


def read_header(bs: BitStream):
    gp = bs.read_flag()
    connseq = bs.read_int(pc.PACKET_HEADER_CONNECT_SEQ_BITS)
    seq = bs.read_int(pc.PACKET_HEADER_SEQ_BITS)
    ack = bs.read_int(pc.PACKET_HEADER_ACK_START_BITS)
    ptype = bs.read_int(pc.PACKET_HEADER_TYPE_BITS)
    abc = bs.read_int(pc.PACKET_HEADER_ACK_BYTE_COUNT_BITS)
    amask = bs.read_int(8 * abc)
    return dict(gp=gp, connseq=connseq, seq=seq, ack=ack, ptype=ptype, abc=abc, amask=amask)


def read_rate_block(bs: BitStream):
    if bs.read_flag():
        bs.read_int(10); bs.read_int(10)
    if bs.read_flag():
        bs.read_int(10); bs.read_int(10)


def read_move(bs: BitStream):
    """Move::unpack (AoT @ VA 0x45b000, byte-identical to stock TGE).

    3 x (rotation-present flag [+ readInt(16)]), then px/py/pz readInt(6),
    then freeLook flag, then MaxTriggerKeys(=6) trigger flags. An idle Move is
    28 bits. Calibrated against c2s seq=1 (MissionStartPhase1Ack) and verified to
    decode all 1077 c2s data packets in real_login.jsonl with zero errors.
    """
    for _ in range(3):
        if bs.read_flag():
            bs.read_int(16)
    bs.read_int(6); bs.read_int(6); bs.read_int(6)
    bs.read_flag()           # freeLook
    for _ in range(6):       # MaxTriggerKeys
        bs.read_flag()


def read_c2s_control_header(bs: BitStream):
    """Parse what the REAL client wrote (server-read perspective), following the
    RE'd writePacket client branch: camPos flag, [point], U32 checksum,
    startMoveId U32, count(5), [moves], fov flag, [fov]."""
    info = {}
    cam = bs.read_flag()
    info["cam_flag"] = int(cam)
    if cam:
        info["cam_point_startbit"] = bs.get_bit_position()
        _read_compressed_point(bs)  # camera position
    info["checksum"] = bs.read_int(32)
    info["startMoveId"] = bs.read_int(32)
    count = bs.read_int(5)
    info["move_count"] = count
    info["fov_flag_bit"] = bs.get_bit_position()
    return info, count


def decode_c2s(payload: bytes, em: EventManager):
    bs = BitStream(payload)
    hdr = read_header(bs)
    if not hdr["gp"]:
        return ("OOB", hdr, None, None)
    if hdr["ptype"] != DATA:
        return ("CTRL", hdr, None, None)  # ping/ack, no body
    read_rate_block(bs)
    bs.set_string_buffer(bytearray(256))
    ctrl, count = read_c2s_control_header(bs)
    if count > 30:
        return ("DATA", hdr, ctrl, f"<bogus move count {count}>")
    # Consume the calibrated idle/real move stream (28 bits each when idle).
    for _ in range(count):
        read_move(bs)
    # fov flag
    if bs.read_flag():
        bs.read_int(8)
    # event section
    events = []
    em._captured = events  # type: ignore
    try:
        em.read_events(bs)
    except Exception as e:  # noqa: BLE001
        events.append(("ERROR", repr(e)))
    return ("DATA", hdr, ctrl, events)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "tools/captures/real_login.jsonl"
    recs = [json.loads(l) for l in open(path) if l.strip()]
    c2s = [r for r in recs if r["dir"] == "c2s"]
    print(f"total={len(recs)} c2s={len(c2s)} s2c={len(recs)-len(c2s)}")

    em = EventManager()

    def collect(verb, args, evt):
        getattr(em, "_captured", []).append(
            ("clientCmd" + verb, [em.detag(a) for a in args])
        )
    em.set_default_handler(collect)
    # capture NetStringEvent teaches too
    def ns(bs):
        slot = bs.read_int(pc.STRING_TABLE_ENTRY_BIT_SIZE)
        text = bs.read_string()
        em.recv_table.map_string(slot, text)
        getattr(em, "_captured", []).append(("NetString", slot, repr(text)))
    em._read_net_string_event = ns  # type: ignore

    login_dumped = False
    clean = errs = 0
    for i, r in enumerate(c2s):
        payload = bytes.fromhex(r["hex"])
        kind, hdr, ctrl, events = decode_c2s(payload, em)
        if kind != "DATA":
            continue
        if isinstance(events, list) and any(e[0] == "ERROR" for e in events):
            errs += 1
        else:
            clean += 1
        ev_str = events if isinstance(events, str) else \
            ", ".join(str(e) for e in events) if events else "(no events)"
        has_login = isinstance(events, list) and any(
            "login" in str(e).lower() for e in events
        )
        # Only print packets that actually carry events (skip the idle stream).
        if isinstance(events, list) and events:
            tag = "  <<< LOGIN" if has_login else ""
            print(f"[{i:3}] t={r['t']:8} seq={hdr['seq']:4} cam={ctrl['cam_flag']} "
                  f"chk={ctrl['checksum']} mvId={ctrl['startMoveId']} cnt={ctrl['move_count']} "
                  f"| {ev_str}{tag}")
        if has_login and not login_dumped:
            login_dumped = True
            print("\n=== FIRST LOGIN c2s PACKET (full) ===")
            print(f"  hex: {r['hex']}")
            print(f"  header: {hdr}")
            print(f"  control: {ctrl}")
            print(f"  events: {events}\n")
    print(f"\nc2s data packets decoded clean={clean} errors={errs}")


if __name__ == "__main__":
    main()
