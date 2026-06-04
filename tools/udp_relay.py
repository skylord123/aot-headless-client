#!/usr/bin/env python3
"""Transparent UDP relay + datagram logger (root-free packet capture).

Listens on a local UDP port, forwards every datagram to the real AoT server,
forwards replies back, and logs BOTH directions to a JSONL file. Point the game
client at the listen address (e.g. set $Pref::ServerIP="127.0.0.1") and every
packet the real client exchanges with the server is captured at the application
layer -- no root / tcpdump needed.

Each log line: {"t": <monotonic ms>, "dir": "c2s"|"s2c", "len": N, "hex": "..."}
c2s = client->server (the real client's OUTBOUND packets -- what we want to diff
against our bot). s2c = server->client.

Usage:
    python udp_relay.py --listen 127.0.0.1:28000 \
        --server 45.148.165.55:28000 --out capture.jsonl
"""
import argparse
import asyncio
import json
import time


def parse_hostport(s: str) -> tuple[str, int]:
    host, _, port = s.rpartition(":")
    return host, int(port)


class Relay:
    def __init__(self, server_addr: tuple[str, int], out_path: str):
        self.server_addr = server_addr
        self.out = open(out_path, "w", buffering=1)  # line-buffered
        self.client_addr: tuple[str, int] | None = None
        self.client_transport: asyncio.DatagramTransport | None = None
        self.upstream_transport: asyncio.DatagramTransport | None = None
        self.t0 = time.monotonic()
        self.n_c2s = 0
        self.n_s2c = 0

    def _log(self, direction: str, data: bytes) -> None:
        rec = {
            "t": round((time.monotonic() - self.t0) * 1000, 1),
            "dir": direction,
            "len": len(data),
            "hex": data.hex(),
        }
        self.out.write(json.dumps(rec) + "\n")

    # client -> us
    def on_client_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        self.client_addr = addr
        self.n_c2s += 1
        self._log("c2s", data)
        if self.upstream_transport is not None:
            self.upstream_transport.sendto(data)  # connected to server_addr

    # server -> us
    def on_server_datagram(self, data: bytes) -> None:
        self.n_s2c += 1
        self._log("s2c", data)
        if self.client_transport is not None and self.client_addr is not None:
            self.client_transport.sendto(data, self.client_addr)


class ClientSideProto(asyncio.DatagramProtocol):
    def __init__(self, relay: Relay):
        self.relay = relay

    def connection_made(self, transport):
        self.relay.client_transport = transport

    def datagram_received(self, data, addr):
        self.relay.on_client_datagram(data, addr)


class UpstreamProto(asyncio.DatagramProtocol):
    def __init__(self, relay: Relay):
        self.relay = relay

    def connection_made(self, transport):
        self.relay.upstream_transport = transport

    def datagram_received(self, data, addr):
        self.relay.on_server_datagram(data)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:28000")
    ap.add_argument("--server", default="45.148.165.55:28000")
    ap.add_argument("--out", default="capture.jsonl")
    args = ap.parse_args()

    listen = parse_hostport(args.listen)
    server = parse_hostport(args.server)
    relay = Relay(server, args.out)

    loop = asyncio.get_running_loop()
    # Upstream socket: connected to the real server so replies route back here.
    await loop.create_datagram_endpoint(
        lambda: UpstreamProto(relay), remote_addr=server
    )
    # Client-facing socket.
    await loop.create_datagram_endpoint(
        lambda: ClientSideProto(relay), local_addr=listen
    )

    print(f"[relay] {listen[0]}:{listen[1]}  <->  {server[0]}:{server[1]}")
    print(f"[relay] logging to {args.out}")
    try:
        while True:
            await asyncio.sleep(2)
            print(f"[relay] c2s={relay.n_c2s} s2c={relay.n_s2c}", flush=True)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
