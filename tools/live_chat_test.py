#!/usr/bin/env python3
"""One-shot LIVE chat-send test (etiquette: ONE session, clean disconnect).

Connects to the live AoT server, waits for clientCmdLoginSuccess, sends ONE
polite global chat, waits briefly to observe it echo back / any reply, then
disconnects cleanly. Run from the bot dir with the real .env present.
"""
import asyncio
import logging
import sys

sys.path.insert(0, ".")
from aotbot.config import Config
from aotbot.client import AotClient

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("chattest")

POLITE = "Hello! (automated test, please ignore)"


async def main():
    config = Config.load()
    client = AotClient(config)
    chats = []
    client.on_chat = lambda scope, name, msg, raw: chats.append((scope, name, msg))

    log.info("connecting to %s:%d", config.aot_server_host, config.aot_server_port)
    ok = await client.connect()
    if not ok:
        log.error("connect failed"); return

    # Wait up to 30s for login.
    for _ in range(60):
        if client.logged_in:
            break
        await asyncio.sleep(0.5)
    if not client.logged_in:
        log.error("did not reach logged-in; aborting (no chat sent)")
        await client.disconnect("test done"); return

    log.info("LOGGED IN. sending ONE polite global chat: %r", POLITE)
    client.global_chat(POLITE)

    # Observe for a few seconds (chat echo / replies), then leave.
    await asyncio.sleep(6)
    log.info("recent chat observed (%d lines): %s", len(chats), chats[-8:])
    await client.disconnect("chat test complete")
    log.info("disconnected cleanly")


if __name__ == "__main__":
    asyncio.run(main())
