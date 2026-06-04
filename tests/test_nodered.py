"""Tests for aotbot.nodered.NodeRedBridge using a local asyncio TCP server.

Covers: connect, outbound terminator framing, inbound multi-line/partial-buffer
framing, the command parser grammar (including quoted ``raw`` args), and
reconnect-on-disconnect.
"""

from __future__ import annotations

import asyncio

import pytest

from aotbot.nodered import TERMINATOR, Command, NodeRedBridge, parse_line


# --------------------------------------------------------------------------- #
# Parser tests (pure, no I/O)
# --------------------------------------------------------------------------- #


def test_parse_say_keeps_whole_text():
    cmd = parse_line("say hello there, world")
    assert cmd == Command(verb="say", args=["hello there, world"], raw="say hello there, world")


def test_parse_global_keeps_whole_text_and_quotes():
    cmd = parse_line('global "quotes" stay literal')
    assert cmd.verb == "global"
    assert cmd.args == ['"quotes" stay literal']


def test_parse_login():
    cmd = parse_line("login alice s3cret")
    assert cmd.verb == "login"
    assert cmd.args == ["alice", "s3cret"]


def test_parse_logout_no_args():
    cmd = parse_line("logout")
    assert cmd == Command(verb="logout", args=[], raw="logout")


def test_parse_connect():
    cmd = parse_line("connect 127.0.0.1:28000")
    assert cmd.verb == "connect"
    assert cmd.args == ["127.0.0.1:28000"]


def test_parse_disconnect_no_args():
    cmd = parse_line("disconnect")
    assert cmd.verb == "disconnect" and cmd.args == []


def test_parse_raw_multiple_args_with_quoting():
    cmd = parse_line('raw Talk "hello world" 42')
    assert cmd.verb == "raw"
    assert cmd.args == ["Talk", "hello world", "42"]


def test_parse_verb_is_lowercased():
    assert parse_line("SAY hi").verb == "say"
    assert parse_line("Raw Foo bar").verb == "raw"


def test_parse_blank_line_returns_none():
    assert parse_line("") is None
    assert parse_line("   \r\n") is None


def test_parse_strips_trailing_crlf():
    cmd = parse_line("logout\r\n")
    assert cmd.raw == "logout"


def test_parse_unbalanced_quotes_falls_back():
    cmd = parse_line('raw Talk "unterminated')
    # shlex would raise; we fall back to whitespace split.
    assert cmd.verb == "raw"
    assert cmd.args == ["Talk", '"unterminated']


# --------------------------------------------------------------------------- #
# Mock Node-RED server helper
# --------------------------------------------------------------------------- #


class MockServer:
    """A minimal asyncio TCP server that records received bytes and can push
    lines to the connected client."""

    def __init__(self):
        self.server: asyncio.AbstractServer | None = None
        self.received = bytearray()
        self.port = 0
        self._client_writer: asyncio.StreamWriter | None = None
        self.connections = 0
        self._connected_evt = asyncio.Event()

    async def start(self):
        self.server = await asyncio.start_server(self._on_client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.connections += 1
        self._client_writer = writer
        self._connected_evt.set()
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                self.received.extend(chunk)
        except (ConnectionError, OSError):
            pass

    async def wait_for_client(self, timeout=2.0):
        await asyncio.wait_for(self._connected_evt.wait(), timeout)

    async def push(self, data: str):
        assert self._client_writer is not None
        self._client_writer.write(data.encode("utf-8"))
        await self._client_writer.drain()

    async def drop_client(self):
        """Close the current client connection to simulate a server-side drop."""
        if self._client_writer is not None:
            self._client_writer.close()
            try:
                await self._client_writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self._client_writer = None
        self._connected_evt.clear()

    async def stop(self):
        # Close any lingering client first so its handler task can finish,
        # then close the listening socket. We deliberately do NOT await
        # wait_closed() with an unbounded wait (a slow handler teardown could
        # hang the test); a short bounded wait is enough.
        if self._client_writer is not None:
            self._client_writer.close()
            self._client_writer = None
        if self.server is not None:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 1.0)
            except asyncio.TimeoutError:
                pass


async def _wait_until(predicate, timeout=2.0, interval=0.01):
    """Poll until predicate() is truthy or raise asyncio.TimeoutError."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise asyncio.TimeoutError(f"condition not met within {timeout}s")


# --------------------------------------------------------------------------- #
# Integration tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_connect_and_state():
    srv = MockServer()
    await srv.start()
    bridge = NodeRedBridge("127.0.0.1", srv.port)
    try:
        await bridge.start()
        await srv.wait_for_client()
        await _wait_until(lambda: bridge.connected)
        assert bridge.connected
    finally:
        await bridge.stop()
        await srv.stop()


@pytest.mark.asyncio
async def test_outbound_carries_terminator():
    srv = MockServer()
    await srv.start()
    bridge = NodeRedBridge("127.0.0.1", srv.port)
    try:
        await bridge.start()
        await srv.wait_for_client()
        await _wait_until(lambda: bridge.connected)
        ok = await bridge.send("alice: hello")
        assert ok
        await _wait_until(lambda: srv.received.endswith(TERMINATOR.encode()))
        assert srv.received.decode() == "alice: hello" + TERMINATOR
    finally:
        await bridge.stop()
        await srv.stop()


@pytest.mark.asyncio
async def test_send_when_disconnected_is_noop():
    bridge = NodeRedBridge("127.0.0.1", 1)  # never started/connected
    assert await bridge.send("nope") is False


@pytest.mark.asyncio
async def test_inbound_multiline_and_partial_framing():
    srv = MockServer()
    await srv.start()
    lines: list[str] = []
    cmds: list[Command] = []

    bridge = NodeRedBridge("127.0.0.1", srv.port)
    bridge.on_line = lines.append
    bridge.set_default_handler(cmds.append)
    try:
        await bridge.start()
        await srv.wait_for_client()
        await _wait_until(lambda: bridge.connected)

        # Two complete lines in one write.
        await srv.push("say hello\nglobal world\n")
        # A partial line, then its completion in a second write.
        await srv.push("login al")
        await srv.push("ice secret\n")

        await _wait_until(lambda: len(cmds) == 3)
        assert lines == ["say hello", "global world", "login alice secret"]
        assert cmds[0] == Command("say", ["hello"], "say hello")
        assert cmds[1] == Command("global", ["world"], "global world")
        assert cmds[2] == Command("login", ["alice", "secret"], "login alice secret")
    finally:
        await bridge.stop()
        await srv.stop()


@pytest.mark.asyncio
async def test_verb_dispatch_to_registered_handler():
    srv = MockServer()
    await srv.start()
    say_args: list[list[str]] = []
    raw_args: list[list[str]] = []

    bridge = NodeRedBridge("127.0.0.1", srv.port)
    bridge.register_handler("say", lambda c: say_args.append(c.args))
    bridge.register_handler("raw", lambda c: raw_args.append(c.args))
    try:
        await bridge.start()
        await srv.wait_for_client()
        await _wait_until(lambda: bridge.connected)

        await srv.push('say hi everyone\nraw Talk "hello world" 42\n')
        await _wait_until(lambda: say_args and raw_args)

        assert say_args == [["hi everyone"]]
        assert raw_args == [["Talk", "hello world", "42"]]
    finally:
        await bridge.stop()
        await srv.stop()


@pytest.mark.asyncio
async def test_async_handler_is_awaited():
    srv = MockServer()
    await srv.start()
    seen = asyncio.Event()

    async def handler(cmd: Command):
        await asyncio.sleep(0)
        seen.set()

    bridge = NodeRedBridge("127.0.0.1", srv.port)
    bridge.register_handler("logout", handler)
    try:
        await bridge.start()
        await srv.wait_for_client()
        await _wait_until(lambda: bridge.connected)
        await srv.push("logout\n")
        await asyncio.wait_for(seen.wait(), 2.0)
    finally:
        await bridge.stop()
        await srv.stop()


@pytest.mark.asyncio
async def test_reconnect_on_disconnect():
    srv = MockServer()
    await srv.start()
    bridge = NodeRedBridge("127.0.0.1", srv.port)
    try:
        await bridge.start()
        await srv.wait_for_client()
        await _wait_until(lambda: bridge.connected)
        assert srv.connections == 1

        # Drop the client server-side; bridge should reconnect (fast: 1s).
        await srv.drop_client()
        await _wait_until(lambda: not bridge.connected, timeout=2.0)
        await srv.wait_for_client(timeout=4.0)
        await _wait_until(lambda: bridge.connected, timeout=4.0)
        assert srv.connections >= 2
    finally:
        await bridge.stop()
        await srv.stop()
