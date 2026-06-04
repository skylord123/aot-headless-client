#!/usr/bin/env python3
"""One-shot LIVE bad-login probe (etiquette: ONE session, clean disconnect).

Connects with wrong credentials and observes whether the server runs the full
load to clientCmdMissionStart -> clientCmdWarningBox("Wrong Password!"), which is
gated on the ghost stream completing. Logs every clientCmd + phase transition.
Times out and disconnects cleanly. Does NOT create a user.

Run: AOT_USERNAME=test AOT_PASSWORD=wrongpass AOT_CREATE_USER=false \
     .venv/bin/python tools/live_badlogin_test.py
"""
import asyncio
import logging
import sys
import time

sys.path.insert(0, ".")
from aotbot.config import Config
from aotbot.client import AotClient

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("badlogin")


async def main():
    config = Config.load()
    client = AotClient(config)

    warnings = []
    if hasattr(client, "events"):
        client.events.on_client_cmd(
            "WarningBox",
            lambda a, e: (warnings.append(a), log.info("*** WarningBox %r", a)))

    log.info("connecting to %s:%d as user=%r (expecting bad-login)",
             config.aot_server_host, config.aot_server_port, config.aot_username)
    ok = await client.connect()
    if not ok:
        log.error("connect failed"); return

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if warnings:
            log.info("GOT WarningBox -> bad-login detection works: %r", warnings)
            break
        if client.logged_in:
            log.info("unexpectedly logged in (creds may be valid)")
            break
        await asyncio.sleep(0.5)
    else:
        log.warning("timed out with NO WarningBox and not logged in (213 stall persists)")

    await client.disconnect("badlogin probe done")
    log.info("disconnected cleanly; warnings=%s logged_in=%s", warnings, client.logged_in)


if __name__ == "__main__":
    asyncio.run(main())
