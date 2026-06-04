#!/usr/bin/env python3
"""One-shot LIVE telemetry test (etiquette: ONE session, clean disconnect).

Connects to the live AoT server with object tracking forced ON, waits for login
+ the ghost stream, observes a few seconds (so positions update as entities
move), then prints a summary of list_objects() and disconnects cleanly. Run from
the bot dir with the real .env present. Does NOT modify .env.
"""
import asyncio
import logging
import sys
from collections import Counter
from dataclasses import replace

sys.path.insert(0, ".")
from aotbot.config import Config
from aotbot.client import AotClient

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("objtest")


async def main():
    config = Config.load()
    config = replace(config, aot_track_objects=True)  # force tracking on for the test
    client = AotClient(config)

    log.info("connecting to %s:%d (AOT_TRACK_OBJECTS forced ON)",
             config.aot_server_host, config.aot_server_port)
    ok = await client.connect()
    if not ok:
        log.error("connect failed"); return

    for _ in range(80):
        if client.logged_in:
            break
        await asyncio.sleep(0.5)
    if not client.logged_in:
        log.warning("did not reach logged-in within 40s; reporting what scoped anyway")

    # Snapshot 1.
    await asyncio.sleep(3)
    objs1 = {o["ghost_id"]: o["position"] for o in client.list_objects()}
    log.info("snapshot 1: %d scoped objects", len(objs1))

    # Observe movement.
    await asyncio.sleep(5)
    objs = client.list_objects()
    by_class = Counter(o["class_name"] for o in objs)
    with_pos = [o for o in objs if o["position"]]
    moved = sum(
        1 for o in objs
        if o["position"] and objs1.get(o["ghost_id"]) not in (None, o["position"])
    )
    ctrl = client.phases.registry.control_ghost_id if client.phases.registry else None

    log.info("=== telemetry summary ===")
    log.info("total scoped: %d   with world position: %d   moved in 5s: %d",
             len(objs), len(with_pos), moved)
    log.info("control ghost id (bot's own player): %s", ctrl)
    log.info("by class: %s", dict(by_class))
    log.info("sample objects (players/items/world):")
    shown = 0
    # Players first, then a sample of the rest.
    players = [o for o in objs if o["class_name"] in ("Player", "AIPlayer")]
    others = [o for o in objs if o not in players]
    for o in players + others:
        if o["class_name"] in ("Player", "AIPlayer", "Item", "StaticShape",
                                "InteriorInstance", "TSStatic") and (
                o["position"] or o["shape_name"] or o.get("name")):
            rot = o.get("rotation")
            ang = rot.get("angle") if isinstance(rot, dict) else rot
            log.info("  gid=%-5d %-9s name=%-18r pos=%s ang=%s shape=%r ctrl=%s",
                     o["ghost_id"], o["class_name"], o.get("name"),
                     o["position"], ang, o.get("shape_file"),
                     o["is_control_object"])
            shown += 1
            if shown >= 25:
                break

    await client.disconnect("telemetry test complete")
    log.info("disconnected cleanly")


if __name__ == "__main__":
    asyncio.run(main())
