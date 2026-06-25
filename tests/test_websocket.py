"""Tests for aotbot.websocket.WebSocketServer.

Covers the RFC 6455 server end-to-end with a minimal hand-rolled client:
the opening handshake, masked client text frames (small + extended length),
fragmented messages, ping/pong, server -> client broadcast, JSON action
dispatch, and graceful handling of malformed / non-JSON / actionless input.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from aotbot.websocket import (
    _OP_CLOSE,
    _OP_CONT,
    _OP_PING,
    _OP_PONG,
    _OP_TEXT,
    WebSocketServer,
)


# --------------------------------------------------------------------------- #
# Minimal RFC 6455 client (test helper)
# --------------------------------------------------------------------------- #


class WSClient:
    """A tiny WebSocket client: handshake + masked send + frame read."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, host: str, port: int) -> "WSClient":
        reader, writer = await asyncio.open_connection(host, port)
        # Client key is arbitrary for our server (it does not validate the
        # accept value back); use a fixed, valid base64 16-byte key.
        key = base64.b64encode(b"0123456789abcdef").decode()
        request = (
            "GET / HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        writer.write(request.encode("latin-1"))
        await writer.drain()
        # Read the 101 response headers (until blank line).
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = await reader.read(256)
            if not chunk:
                raise AssertionError("server closed during handshake")
            resp += chunk
        assert resp.startswith(b"HTTP/1.1 101"), resp[:64]
        assert b"sec-websocket-accept:" in resp.lower()
        return cls(reader, writer)

    def _frame(self, opcode: int, payload: bytes, fin: bool = True) -> bytes:
        b1 = (0x80 if fin else 0x00) | (opcode & 0x0F)
        mask = bytes((0x21, 0x52, 0xA3, 0xF4))  # fixed mask key
        length = len(payload)
        if length < 126:
            header = bytes((b1, 0x80 | length))
        elif length < 65536:
            header = bytes((b1, 0x80 | 126)) + length.to_bytes(2, "big")
        else:
            header = bytes((b1, 0x80 | 127)) + length.to_bytes(8, "big")
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return header + mask + masked

    async def send_text(self, text: str) -> None:
        self.writer.write(self._frame(_OP_TEXT, text.encode("utf-8")))
        await self.writer.drain()

    async def send_json(self, obj: dict) -> None:
        await self.send_text(json.dumps(obj))

    async def send_raw(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def send_fragmented_text(self, parts: list[str]) -> None:
        # First frame TEXT fin=0, middle CONT fin=0, last CONT fin=1.
        for i, part in enumerate(parts):
            opcode = _OP_TEXT if i == 0 else _OP_CONT
            fin = i == len(parts) - 1
            self.writer.write(self._frame(opcode, part.encode("utf-8"), fin=fin))
        await self.writer.drain()

    async def send_ping(self, payload: bytes = b"hi") -> None:
        self.writer.write(self._frame(_OP_PING, payload))
        await self.writer.drain()

    async def read_frame(self) -> tuple[int, bytes]:
        """Read one (unmasked) server frame -> (opcode, payload)."""
        header = await self.reader.readexactly(2)
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(await self.reader.readexactly(2), "big")
        elif length == 127:
            length = int.from_bytes(await self.reader.readexactly(8), "big")
        payload = await self.reader.readexactly(length) if length else b""
        return opcode, payload

    async def read_json(self) -> dict:
        opcode, payload = await self.read_frame()
        assert opcode == _OP_TEXT
        return json.loads(payload.decode("utf-8"))

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def _wait_until(predicate, timeout=2.0, interval=0.01):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise asyncio.TimeoutError("condition not met in time")


async def _start_server(**kwargs) -> WebSocketServer:
    server = WebSocketServer("127.0.0.1", 0, **kwargs)
    await server.start()
    return server


def _port(server: WebSocketServer) -> int:
    assert server._server is not None
    return server._server.sockets[0].getsockname()[1]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_handshake_and_client_count():
    server = await _start_server()
    try:
        client = await WSClient.connect("127.0.0.1", _port(server))
        await _wait_until(lambda: server.client_count == 1)
        assert server.connected
        await client.close()
        await _wait_until(lambda: server.client_count == 0)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_inbound_json_dispatch_by_action():
    server = await _start_server()
    seen: list[dict] = []
    server.register_handler("say", seen.append)
    try:
        client = await WSClient.connect("127.0.0.1", _port(server))
        await _wait_until(lambda: server.client_count == 1)
        await client.send_json({"action": "say", "message": "hello"})
        await _wait_until(lambda: seen)
        assert seen == [{"action": "say", "message": "hello"}]
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_action_is_case_insensitive():
    server = await _start_server()
    seen: list[dict] = []
    server.register_handler("say", seen.append)
    try:
        client = await WSClient.connect("127.0.0.1", _port(server))
        await _wait_until(lambda: server.client_count == 1)
        await client.send_json({"action": "SAY", "message": "yo"})
        await _wait_until(lambda: seen)
        assert seen[0]["message"] == "yo"
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_default_handler_for_unknown_action():
    server = await _start_server()
    seen: list[dict] = []
    server.set_default_handler(seen.append)
    try:
        client = await WSClient.connect("127.0.0.1", _port(server))
        await _wait_until(lambda: server.client_count == 1)
        await client.send_json({"action": "nope", "x": 1})
        await _wait_until(lambda: seen)
        assert seen[0]["action"] == "nope"
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_outbound_broadcast_to_all_clients():
    server = await _start_server()
    try:
        c1 = await WSClient.connect("127.0.0.1", _port(server))
        c2 = await WSClient.connect("127.0.0.1", _port(server))
        await _wait_until(lambda: server.client_count == 2)

        sent = await server.send({"action": "server_message", "message": "hi all"})
        assert sent == 2

        for c in (c1, c2):
            obj = await asyncio.wait_for(c.read_json(), 2.0)
            assert obj == {"action": "server_message", "message": "hi all"}
        await c1.close()
        await c2.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_send_with_no_clients_is_noop():
    server = await _start_server()
    try:
        assert await server.send({"action": "x"}) == 0
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_fragmented_text_message_reassembled():
    server = await _start_server()
    seen: list[dict] = []
    server.register_handler("say", seen.append)
    try:
        client = await WSClient.connect("127.0.0.1", _port(server))
        await _wait_until(lambda: server.client_count == 1)
        payload = json.dumps({"action": "say", "message": "fragmented"})
        third = len(payload) // 3
        await client.send_fragmented_text(
            [payload[:third], payload[third : 2 * third], payload[2 * third :]]
        )
        await _wait_until(lambda: seen)
        assert seen[0]["message"] == "fragmented"
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_extended_length_payload():
    server = await _start_server()
    seen: list[dict] = []
    server.register_handler("say", seen.append)
    try:
        client = await WSClient.connect("127.0.0.1", _port(server))
        await _wait_until(lambda: server.client_count == 1)
        big = "x" * 5000  # forces the 126 / 2-byte extended length path
        await client.send_json({"action": "say", "message": big})
        await _wait_until(lambda: seen)
        assert seen[0]["message"] == big
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_ping_gets_pong():
    server = await _start_server()
    try:
        client = await WSClient.connect("127.0.0.1", _port(server))
        await _wait_until(lambda: server.client_count == 1)
        await client.send_ping(b"ping-payload")
        opcode, payload = await asyncio.wait_for(client.read_frame(), 2.0)
        assert opcode == _OP_PONG
        assert payload == b"ping-payload"
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_malformed_json_is_ignored_connection_survives():
    server = await _start_server()
    seen: list[dict] = []
    server.register_handler("say", seen.append)
    try:
        client = await WSClient.connect("127.0.0.1", _port(server))
        await _wait_until(lambda: server.client_count == 1)
        await client.send_text("this is not json")      # ignored
        await client.send_json({"no": "action field"})    # ignored (no action)
        await client.send_json({"action": "say", "message": "ok"})  # handled
        await _wait_until(lambda: seen)
        assert seen == [{"action": "say", "message": "ok"}]
        assert server.client_count == 1  # survived the bad frames
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_async_handler_is_awaited():
    server = await _start_server()
    fired = asyncio.Event()

    async def handler(_obj: dict):
        await asyncio.sleep(0)
        fired.set()

    server.register_handler("logout", handler)
    try:
        client = await WSClient.connect("127.0.0.1", _port(server))
        await _wait_until(lambda: server.client_count == 1)
        await client.send_json({"action": "logout"})
        await asyncio.wait_for(fired.wait(), 2.0)
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_handler_exception_does_not_kill_connection():
    server = await _start_server()
    ok_seen: list[dict] = []

    def boom(_obj: dict):
        raise RuntimeError("handler blew up")

    server.register_handler("boom", boom)
    server.register_handler("say", ok_seen.append)
    try:
        client = await WSClient.connect("127.0.0.1", _port(server))
        await _wait_until(lambda: server.client_count == 1)
        await client.send_json({"action": "boom"})
        await client.send_json({"action": "say", "message": "still alive"})
        await _wait_until(lambda: ok_seen)
        assert ok_seen[0]["message"] == "still alive"
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_client_close_frame_handled():
    server = await _start_server()
    try:
        client = await WSClient.connect("127.0.0.1", _port(server))
        await _wait_until(lambda: server.client_count == 1)
        # Send a masked close frame; server should echo a close and drop us.
        await client.send_raw(client._frame(_OP_CLOSE, b""))
        opcode, _ = await asyncio.wait_for(client.read_frame(), 2.0)
        assert opcode == _OP_CLOSE
        await _wait_until(lambda: server.client_count == 0)
        await client.close()
    finally:
        await server.stop()
