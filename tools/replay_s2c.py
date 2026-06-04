#!/usr/bin/env python3
"""Replay the server->client (s2c) stream of a udp_relay capture through the
production read path (phases.read_packet_body) and report progress + the first
blocking datablock/ghost class.

This is the bit-exact validation harness for the datablock/ghost decoders: a
correct decoder advances the cursor with zero desync, so the replay reaches
deeper into the stream (more packets decoded clean, the in-game/login events
processed). Run from the ageoftime-minimal-bot dir:

    .venv/bin/python tools/replay_s2c.py [capture.jsonl]
"""
import json
import logging
import sys
from collections import Counter

sys.path.insert(0, ".")
from aotbot.bitstream import BitStream  # noqa: E402
from aotbot import protocol_constants as pc  # noqa: E402
from aotbot.events import EventManager, EventDecodeError  # noqa: E402
from aotbot.phases import GameConnectionPhases, AlignmentError  # noqa: E402

logging.disable(logging.CRITICAL)


def read_header(bs: BitStream):
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
    recs = [json.loads(l) for l in open(path) if l.strip()]
    s2c = [r for r in recs if r["dir"] == "s2c"]

    em = EventManager()
    cmds = Counter()

    def collect(verb, args, evt):
        cmds[verb] += 1
    em.set_default_handler(collect)
    for verb in ("MissionStartPhase1", "MissionStartPhase2", "MissionStartPhase3",
                 "MissionStart", "StartLogin", "LoginSuccess", "ServerMessage",
                 "ChatMessage", "WarningBox"):
        em.on_client_cmd(verb, (lambda v: (lambda a, e: cmds.__setitem__(v, cmds[v] + 1)))(verb))

    # Tracking ON so the ghost section is fully decoded -- this is the bit-exact
    # validation path (with tracking OFF the ghost section is skipped by design).
    ph = GameConnectionPhases(em, skip_lighting=True, track_objects=True)
    # We're decoding the *server's* stream; do not actually send acks. Mute the
    # outgoing command_to_server so phase handlers don't crash (no connection).
    em.command_to_server = lambda *a, **k: None  # type: ignore
    em._send_connection_message = lambda *a, **k: None  # type: ignore
    ph._send_connection_message = lambda *a, **k: None  # type: ignore

    last_seq = -1
    ok = 0
    blockers = Counter()
    first_block = None
    reached = []
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
            ok += 1
        except (AlignmentError, EventDecodeError) as e:
            key = str(e)[:120]
            blockers[key] += 1
            if first_block is None:
                first_block = (i, seq, key)

    print(f"s2c data packets decoded clean: {ok}")
    print(f"first blocker: {first_block}")
    print("blocker histogram:")
    for k, v in blockers.most_common():
        print(f"  {v:5}  {k}")
    print(f"phase state: {ph.state}  ghosting={ph.ghosting_active}")
    print(f"clientCmd/verb counts seen: {dict(cmds)}")


if __name__ == "__main__":
    main()
