"""Node-RED TCP bridge for the headless Age of Time bot.

This module is a pure transport + parsing + dispatch layer. It owns NONE of the
game-side behavior: the game client module registers handlers and this bridge
invokes them. It mirrors the in-engine implementation in
``AgeOfTime/base/skylord/NodeRED.cs`` so the same Node-RED flows work unchanged.

Wire conventions (must match ``NodeRED.cs``)
--------------------------------------------
- The bot is a TCP *client* connecting to Node-RED (default ``localhost:1881``).
- OUTBOUND (bot -> Node-RED): every message is sent followed by the terminator
  ``"\n\n\n"`` (three newlines). See ``NodeRED::send``.
- INBOUND (Node-RED -> bot): line-based. We accumulate a receive buffer and
  split it on ``"\n"``; each complete line is one command (``onLine``).
- Auto-reconnect with backoff: 1s for the first few attempts, then 5s, matching
  ``NodeRED::_retry_connect`` (``connection_attempts < 5 ? 1000 : 5000``).

Inbound command grammar
------------------------
Each inbound line is parsed into a :class:`Command` ``(verb, args, raw)`` and
dispatched to a registered handler. The documented grammar (see also
``docs/nodered-protocol.md``) is::

    say <text>              # local/proximity chat   -> commandToServer('Talk', text)
    global <text>           # global chat            -> commandToServer('MessageSent', text)
    login <user> <pass>     # log in
    logout                  # log out
    connect <host:port>     # connect to a game server
    disconnect              # disconnect from the game server
    raw <verb> <args...>    # arbitrary commandToServer(verb, args...)

Parsing rules:

- The first whitespace-delimited token is the lowercased ``verb``.
- ``say`` / ``global`` take the entire remainder of the line as a single text
  argument (no quoting needed, whitespace preserved).
- ``login`` / ``connect`` / ``raw`` are tokenized with shell-like quoting
  (``shlex``) so multi-word arguments can be quoted, e.g.
  ``raw Talk "hello world"`` -> verb ``Talk`` with one arg ``hello world``.
- ``logout`` / ``disconnect`` take no arguments.
- Unknown verbs are still parsed into a :class:`Command` and dispatched; the
  handler decides what to do (it may log/ignore).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Union

logger = logging.getLogger("aotbot.nodered")

# Outbound message terminator — three newlines, matching NodeRED::send.
TERMINATOR = "\n\n\n"

# Verbs whose argument is the whole remainder of the line (no tokenizing).
_FREE_TEXT_VERBS = {"say", "global"}

# Backoff schedule mirroring NodeRED::_retry_connect.
_FAST_RETRY_ATTEMPTS = 5
_FAST_RETRY_DELAY = 1.0
_SLOW_RETRY_DELAY = 5.0

# A handler may be sync or async. It receives the parsed Command.
LineCallback = Callable[[str], Union[None, Awaitable[None]]]
CommandHandler = Callable[["Command"], Union[None, Awaitable[None]]]


@dataclass(frozen=True)
class Command:
    """A parsed inbound command from Node-RED.

    Attributes:
        verb: the lowercased command verb (e.g. ``"say"``, ``"raw"``).
        args: the parsed argument list. For free-text verbs (``say``/``global``)
            this is a single-element list holding the whole remaining text. For
            ``raw`` it is ``[game_verb, *game_args]``.
        raw: the original line as received (trailing ``\r``/``\n`` stripped).
    """

    verb: str
    args: list[str] = field(default_factory=list)
    raw: str = ""


def parse_line(line: str) -> Optional[Command]:
    """Parse a single inbound line into a :class:`Command`.

    Returns ``None`` for blank lines (after stripping). Never raises on
    malformed quoting — falls back to whitespace splitting.
    """
    raw = line.rstrip("\r\n")
    stripped = raw.strip()
    if not stripped:
        return None

    # Split off the verb; the remainder keeps its internal whitespace.
    parts = stripped.split(None, 1)
    verb = parts[0].lower()
    remainder = parts[1] if len(parts) > 1 else ""

    if verb in _FREE_TEXT_VERBS:
        # Whole remainder is one text argument (may be empty).
        args = [remainder] if remainder else []
        return Command(verb=verb, args=args, raw=raw)

    if not remainder:
        return Command(verb=verb, args=[], raw=raw)

    # Tokenize the remainder with shell-like quoting; fall back to a plain
    # split if the quoting is unbalanced.
    try:
        args = shlex.split(remainder)
    except ValueError:
        args = remainder.split()

    return Command(verb=verb, args=args, raw=raw)


class NodeRedBridge:
    """Asyncio TCP client bridging the bot to Node-RED.

    Transport + parsing + dispatch only — no game actions live here.

    Typical usage::

        bridge = NodeRedBridge("localhost", 1881)
        bridge.register_handler("say", on_say)        # game client registers
        bridge.on_line = some_raw_line_observer        # optional raw hook
        await bridge.start()
        ...
        await bridge.send("hello node-red")
        ...
        await bridge.stop()
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1881,
        *,
        on_line: Optional[LineCallback] = None,
        on_connect: Optional[Callable[[], Union[None, Awaitable[None]]]] = None,
        on_disconnect: Optional[Callable[[], Union[None, Awaitable[None]]]] = None,
    ) -> None:
        self.host = host
        self.port = port

        # Caller-supplied hooks.
        # on_line is invoked for EVERY inbound line (raw), before dispatch.
        self.on_line: Optional[LineCallback] = on_line
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect

        self._handlers: dict[str, CommandHandler] = {}
        self._default_handler: Optional[CommandHandler] = None

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._run_task: Optional[asyncio.Task] = None
        self._connected = False
        self._running = False
        # Number of consecutive connect attempts (drives the backoff curve).
        self._connection_attempts = 0

    # -- handler registration ------------------------------------------------

    def register_handler(self, verb: str, handler: CommandHandler) -> None:
        """Register a handler for a command verb (case-insensitive)."""
        self._handlers[verb.lower()] = handler

    def set_default_handler(self, handler: Optional[CommandHandler]) -> None:
        """Set a fallback handler for verbs with no specific handler."""
        self._default_handler = handler

    # -- state ---------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Start the connection lifecycle (connect + auto-reconnect loop)."""
        if self._running:
            return
        self._running = True
        self._connection_attempts = 0
        self._run_task = asyncio.create_task(self._run(), name="nodered-bridge")

    async def stop(self) -> None:
        """Stop the bridge and tear down the connection. Disables reconnect."""
        self._running = False
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        self._writer = None
        self._reader = None
        self._connected = False

        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            self._run_task = None
        logger.info("Node-RED bridge stopped")

    # -- send ----------------------------------------------------------------

    async def send(self, message: str) -> bool:
        """Send a message to Node-RED with the ``\n\n\n`` terminator.

        No-op (logs a warning) and returns ``False`` if not connected, matching
        ``NodeRED::send`` which only sends when ``is_connected()``.
        """
        if not self._connected or self._writer is None:
            logger.warning("send() called while not connected; dropping: %r", message)
            return False
        try:
            self._writer.write((message + TERMINATOR).encode("utf-8"))
            await self._writer.drain()
            logger.debug("SENDING -> %s", message)
            return True
        except (ConnectionError, OSError) as exc:
            logger.warning("send() failed: %s", exc)
            self._connected = False
            return False

    # -- internals -----------------------------------------------------------

    async def _run(self) -> None:
        """Connect/read/reconnect loop. Runs until :meth:`stop`."""
        while self._running:
            self._connection_attempts += 1
            try:
                logger.info(
                    "Connecting to Node-RED at %s:%s (attempt %d)",
                    self.host,
                    self.port,
                    self._connection_attempts,
                )
                self._reader, self._writer = await asyncio.open_connection(
                    self.host, self.port
                )
            except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                logger.warning("Connect failed: %s", exc)
                if not self._running:
                    break
                await asyncio.sleep(self._retry_delay())
                continue

            self._connected = True
            self._connection_attempts = 0
            logger.info("Connected to Node-RED")
            await self._fire(self.on_connect)

            try:
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # defensive: keep the loop alive
                logger.warning("Read loop error: %s", exc)
            finally:
                self._connected = False
                await self._fire(self.on_disconnect)

            if not self._running:
                break
            logger.info("Disconnected from Node-RED; retrying in %.0fms",
                        self._retry_delay() * 1000)
            await asyncio.sleep(self._retry_delay())

    def _retry_delay(self) -> float:
        """Backoff: 1s for the first few attempts, then 5s (NodeRED.cs parity)."""
        if self._connection_attempts < _FAST_RETRY_ATTEMPTS:
            return _FAST_RETRY_DELAY
        return _SLOW_RETRY_DELAY

    async def _read_loop(self) -> None:
        """Accumulate bytes and split into lines, dispatching each (onLine)."""
        assert self._reader is not None
        buffer = ""
        while self._running:
            chunk = await self._reader.read(4096)
            if not chunk:  # EOF -> remote closed
                break
            buffer += chunk.decode("utf-8", errors="replace")
            # Split into complete lines; keep the trailing partial in buffer.
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                await self._handle_line(line)

    async def _handle_line(self, line: str) -> None:
        """Process one inbound line: raw hook, parse, dispatch."""
        logger.debug("RECEIVED <- %s", line.rstrip("\r"))

        if self.on_line is not None:
            await self._fire(lambda: self.on_line(line))  # type: ignore[misc]

        cmd = parse_line(line)
        if cmd is None:
            return

        handler = self._handlers.get(cmd.verb, self._default_handler)
        if handler is None:
            logger.debug("No handler for verb %r (line=%r)", cmd.verb, cmd.raw)
            return
        await self._fire(lambda: handler(cmd))

    @staticmethod
    async def _fire(cb: Optional[Callable[[], Union[None, Awaitable[None]]]]) -> None:
        """Invoke a callback that may be sync or async; swallow its errors."""
        if cb is None:
            return
        try:
            result = cb()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # never let a handler kill the bridge
            logger.exception("handler raised: %s", exc)
