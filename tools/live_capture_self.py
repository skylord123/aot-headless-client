#!/usr/bin/env python3
"""One-shot LIVE self-capture: connect (bad/fresh creds), dump every received
s2c datagram (and sent c2s) as JSONL hex (relay-compatible), then disconnect
cleanly. ONE polite session. Does NOT create a user.

Run: AOT_USERNAME=zztestNNN AOT_PASSWORD=x AOT_CREATE_USER=false \
     .venv/bin/python tools/live_capture_self.py OUT.jsonl
"""
import asyncio, json, logging, sys, time
sys.path.insert(0, ".")
from aotbot.config import Config
from aotbot.client import AotClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("cap")

async def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "tools/captures/live_self.jsonl"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0
    config = Config.load()
    client = AotClient(config)
    f = open(out, "w")
    nc = client.conn if hasattr(client, "_conn") else None

    warnings = []
    client.events.on_client_cmd("WarningBox", lambda a, e: (warnings.append(a), log.info("*** WarningBox %r", a)))

    ok = await client.connect()
    if not ok:
        log.error("connect failed"); f.close(); return
    nc = client.conn
    # wrap _dispatch (recv) and transport.sendto (send) to log
    orig_disp = nc._dispatch
    def disp(data):
        f.write(json.dumps({"dir": "s2c", "hex": data.hex()}) + "\n")
        return orig_disp(data)
    nc._dispatch = disp
    orig_send = nc.transport.sendto
    def send(data, addr):
        f.write(json.dumps({"dir": "c2s", "hex": data.hex()}) + "\n")
        return orig_send(data, addr)
    nc.transport.sendto = send

    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if warnings or client.logged_in:
            await asyncio.sleep(2); break
        await asyncio.sleep(0.25)
    await client.disconnect("self-capture done")
    f.close()
    log.info("wrote %s ; warnings=%s logged_in=%s", out, warnings, client.logged_in)

if __name__ == "__main__":
    asyncio.run(main())
