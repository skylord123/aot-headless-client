"""WebSocket server bridge for the headless Age of Time bot.

Where the Node-RED bridge (:mod:`aotbot.nodered`) is a TCP *client* that dials
out to Node-RED, this module does the opposite: the bot **hosts** a WebSocket
server and clients (Node-RED, a browser, a custom script, ...) connect *to it*.
Communication is bi-directional and every frame is a JSON object carrying an
``"action"`` discriminator — the same ``action`` vocabulary the Node-RED bridge
uses on the wire, so flows written for one transport map cleanly onto the other.

Like the Node-RED bridge this module is **transport + parse + dispatch only**: it
owns none of the game-side behavior. The game client wiring in ``aotbot/main.py``
registers a handler per ``action`` and this server invokes them; outbound game
events are broadcast to every connected client via :meth:`WebSocketServer.send`.

This is a self-contained RFC 6455 server implementation (no third-party
dependency) built on :mod:`asyncio` streams, matching the project's
reimplement-the-protocol-on-stdlib approach. It supports the subset that real
clients need: the opening HTTP Upgrade handshake, masked client frames, text
message reassembly across fragments/continuations, and ping/pong/close control
frames. Server -> client frames are sent unmasked, as required of a server.

Inbound messages (client -> bot)
--------------------------------
Each inbound text frame must be a JSON **object** with a string ``"action"``
field; remaining fields are action-specific. The object is dispatched to the
handler registered for ``action`` (case-insensitive) or the default handler. The
``action`` vocabulary mirrors the Node-RED inbound commands; see
``docs/websocket-protocol.md`` for the authoritative reference. Examples::

    {"action": "say", "message": "hello"}
    {"action": "login", "username": "alice", "password": "s3cret"}
    {"action": "raw", "verb": "Talk", "args": ["hello world", 42]}

Outbound messages (bot -> client)
---------------------------------
Outbound payloads are JSON objects, each carrying an ``"action"`` field, and are
broadcast to every connected client. These are the same shapes the Node-RED
bridge emits (``player_message``, ``server_message``, ``player_joined``,
``player_dropped``, ``zone_change``, ``login_result``, ``connection_state``,
``players``, ``object_list``, ``object``); they are produced by
``aotbot/main.py``. See ``docs/websocket-protocol.md``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from typing import Awaitable, Callable, Optional, Union

logger = logging.getLogger("aotbot.websocket")

# RFC 6455 magic GUID appended to Sec-WebSocket-Key before SHA-1 hashing.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Frame opcodes (RFC 6455 §5.2).
_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

# Reject a single frame whose advertised payload exceeds this many bytes, to
# bound memory against a hostile/buggy client. Generous for JSON control traffic.
_MAX_FRAME_BYTES = 8 * 1024 * 1024

# A handler may be sync or async. It receives the parsed JSON object (a dict).
PayloadHandler = Callable[[dict], Union[None, Awaitable[None]]]


def encode_frame(opcode: int, payload: bytes) -> bytes:
    """Encode a single un-fragmented, unmasked server frame (FIN=1).

    Server -> client frames MUST NOT be masked (RFC 6455 §5.1).
    """
    b1 = 0x80 | (opcode & 0x0F)
    length = len(payload)
    if length < 126:
        header = bytes((b1, length))
    elif length < 65536:
        header = bytes((b1, 126)) + length.to_bytes(2, "big")
    else:
        header = bytes((b1, 127)) + length.to_bytes(8, "big")
    return header + payload


def encode_text_frame(text: str) -> bytes:
    """Encode a UTF-8 text frame."""
    return encode_frame(_OP_TEXT, text.encode("utf-8"))


class WebSocketServer:
    """Asyncio WebSocket server bridging connected clients to the bot.

    Transport + parsing + dispatch only — no game actions live here.

    Typical usage::

        server = WebSocketServer("0.0.0.0", 8765)
        server.register_handler("say", on_say)      # game client registers
        server.on_message = some_raw_observer         # optional raw hook
        await server.start()
        ...
        await server.send({"action": "server_message", "message": "hi"})
        ...
        await server.stop()
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        *,
        on_connect: Optional[Callable[[], Union[None, Awaitable[None]]]] = None,
        on_disconnect: Optional[Callable[[], Union[None, Awaitable[None]]]] = None,
    ) -> None:
        self.host = host
        self.port = port

        # Caller-supplied hooks.
        # on_message is invoked for EVERY inbound JSON object, before dispatch.
        self.on_message: Optional[PayloadHandler] = None
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect

        self._handlers: dict[str, PayloadHandler] = {}
        self._default_handler: Optional[PayloadHandler] = None

        self._server: Optional[asyncio.AbstractServer] = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._running = False

    # -- handler registration ------------------------------------------------

    def register_handler(self, action: str, handler: PayloadHandler) -> None:
        """Register a handler for an ``action`` (case-insensitive)."""
        self._handlers[action.lower()] = handler

    def set_default_handler(self, handler: Optional[PayloadHandler]) -> None:
        """Set a fallback handler for actions with no specific handler."""
        self._default_handler = handler

    # -- state ---------------------------------------------------------------

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def connected(self) -> bool:
        """True when at least one client is connected."""
        return bool(self._clients)

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Bind and start accepting client connections."""
        if self._running:
            return
        self._running = True
        self._server = await asyncio.start_server(
            self._on_client, self.host, self.port
        )
        logger.info("WebSocket server listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        """Stop the server and disconnect all clients."""
        self._running = False
        for writer in list(self._clients):
            try:
                writer.close()
            except (ConnectionError, OSError):
                pass
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except (ConnectionError, OSError):
                pass
            self._server = None
        logger.info("WebSocket server stopped")

    # -- send ----------------------------------------------------------------

    async def send(self, obj: dict) -> int:
        """Broadcast a JSON object to every connected client.

        Returns the number of clients it was delivered to. A no-op returning 0
        when no clients are connected (mirroring the Node-RED bridge's
        send-while-disconnected behavior).
        """
        if not self._clients:
            logger.debug("send() with no clients connected; dropping: %r", obj)
            return 0
        data = json.dumps(obj)
        frame = encode_text_frame(data)
        sent = 0
        for writer in list(self._clients):
            try:
                writer.write(frame)
                await writer.drain()
                sent += 1
            except (ConnectionError, OSError) as exc:
                logger.warning("send to client failed: %s", exc)
                self._clients.discard(writer)
        logger.debug("SENDING -> %s (%d client(s))", data, sent)
        return sent

    # -- internals -----------------------------------------------------------

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Per-connection coroutine: handshake, then read/dispatch until close."""
        peer = writer.get_extra_info("peername")
        try:
            ok = await self._handshake(reader, writer)
        except (ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
            logger.warning("WebSocket handshake failed from %s: %s", peer, exc)
            ok = False
        if not ok:
            try:
                writer.close()
            except (ConnectionError, OSError):
                pass
            return

        self._clients.add(writer)
        logger.info(
            "WebSocket client connected: %s (%d total)", peer, len(self._clients)
        )
        await self._fire(self.on_connect)
        try:
            await self._read_loop(reader, writer)
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass  # client vanished; normal disconnect
        except Exception as exc:  # defensive: never let one client kill others
            logger.warning("WebSocket client error (%s): %s", peer, exc)
        finally:
            self._clients.discard(writer)
            try:
                writer.close()
            except (ConnectionError, OSError):
                pass
            logger.info(
                "WebSocket client disconnected: %s (%d total)",
                peer,
                len(self._clients),
            )
            await self._fire(self.on_disconnect)

    async def _handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        """Perform the RFC 6455 opening handshake. Returns True on success."""
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = await reader.read(1024)
            if not chunk:
                return False
            data += chunk
            if len(data) > 65536:  # header block far larger than any real request
                return False

        header_block = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        lines = header_block.split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:  # skip the request line
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()

        key = headers.get("sec-websocket-key")
        upgrade = headers.get("upgrade", "").lower()
        if not key or "websocket" not in upgrade:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            try:
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            return False

        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("latin-1")).digest()
        ).decode("ascii")
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        writer.write(response.encode("latin-1"))
        await writer.drain()
        return True

    async def _read_loop(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Read frames, reassemble messages, and dispatch text payloads."""
        parts: list[bytes] = []
        message_is_text = False
        while self._running:
            frame = await self._read_frame(reader)
            if frame is None:
                break  # EOF / oversized frame -> close connection
            opcode, payload, fin = frame

            if opcode == _OP_CLOSE:
                # Echo a close frame (with the peer's code if present) and stop.
                await self._send_control(writer, _OP_CLOSE, payload[:125])
                break
            if opcode == _OP_PING:
                await self._send_control(writer, _OP_PONG, payload[:125])
                continue
            if opcode == _OP_PONG:
                continue

            if opcode in (_OP_TEXT, _OP_BINARY):
                parts = [payload]
                message_is_text = opcode == _OP_TEXT
            elif opcode == _OP_CONT:
                parts.append(payload)
            else:
                logger.debug("ignoring unknown opcode 0x%x", opcode)
                continue

            if fin:
                full = b"".join(parts)
                parts = []
                if message_is_text:
                    await self._handle_message(full)
                # Binary messages are not part of the protocol; silently ignore.

    async def _read_frame(
        self, reader: asyncio.StreamReader
    ) -> Optional[tuple[int, bytes, bool]]:
        """Read one frame -> ``(opcode, unmasked_payload, fin)`` or None on EOF."""
        try:
            header = await reader.readexactly(2)
        except asyncio.IncompleteReadError:
            return None
        b1, b2 = header[0], header[1]
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F

        if length == 126:
            length = int.from_bytes(await reader.readexactly(2), "big")
        elif length == 127:
            length = int.from_bytes(await reader.readexactly(8), "big")

        if length > _MAX_FRAME_BYTES:
            logger.warning("frame payload %d bytes exceeds cap; closing", length)
            return None

        mask_key = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(length) if length else b""
        if masked and payload:
            payload = bytes(byte ^ mask_key[i % 4] for i, byte in enumerate(payload))
        return opcode, payload, fin

    async def _send_control(
        self, writer: asyncio.StreamWriter, opcode: int, payload: bytes
    ) -> None:
        try:
            writer.write(encode_frame(opcode, payload))
            await writer.drain()
        except (ConnectionError, OSError):
            pass

    async def _handle_message(self, data: bytes) -> None:
        """Parse one complete text message as JSON and dispatch by ``action``."""
        text = data.decode("utf-8", errors="replace")
        logger.debug("RECEIVED <- %s", text)

        try:
            obj = json.loads(text)
        except ValueError:
            logger.warning("ignoring non-JSON message: %r", text)
            return
        if not isinstance(obj, dict):
            logger.warning("ignoring non-object JSON message: %r", text)
            return

        if self.on_message is not None:
            await self._fire(lambda: self.on_message(obj))  # type: ignore[misc]

        action = obj.get("action")
        if not isinstance(action, str):
            logger.warning("message missing string 'action' field: %r", obj)
            return

        handler = self._handlers.get(action.lower(), self._default_handler)
        if handler is None:
            logger.debug("no handler for action %r", action)
            return
        await self._fire(lambda: handler(obj))

    @staticmethod
    async def _fire(cb: Optional[Callable[[], Union[None, Awaitable[None]]]]) -> None:
        """Invoke a callback that may be sync or async; swallow its errors."""
        if cb is None:
            return
        try:
            result = cb()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # never let a handler kill the server
            logger.exception("handler raised: %s", exc)
