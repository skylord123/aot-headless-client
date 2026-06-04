"""Stdlib logging setup for the bot.

Honors ``LOG_LEVEL`` from config, installs a single structured-ish stream
handler, and provides a hexdump helper used when ``DUMP_PACKETS`` is enabled so
the handshake/CRC traffic can be inspected.
"""

from __future__ import annotations

import logging
import sys

__all__ = ["setup_logging", "hexdump", "log_packet", "PACKET_LOGGER_NAME"]


class _DynamicStderrHandler(logging.StreamHandler):
    """StreamHandler that resolves ``sys.stderr`` lazily, at each emit.

    prompt_toolkit's ``patch_stdout()`` (used by the interactive REPL) swaps
    ``sys.stdout``/``sys.stderr`` for a proxy that renders output ABOVE the live
    prompt without disturbing what the user is typing. A normal StreamHandler
    captures the stderr reference at construction time — before ``patch_stdout``
    runs — so its writes go straight to the terminal and clobber the prompt.
    Looking up ``sys.stderr`` on every emit lets log lines flow through the proxy
    instead, so incoming chat/server messages scroll above the input cleanly.
    """

    def __init__(self) -> None:
        super().__init__(stream=sys.stderr)

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, value):  # noqa: D401 - ignore captured stream; use current
        pass

# Dedicated logger for raw packet dumps so it can be silenced independently.
PACKET_LOGGER_NAME = "aotbot.packets"

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "info", *, dump_packets: bool = False) -> None:
    """Configure root logging.

    Args:
        level: One of debug/info/warning/error/critical (case-insensitive).
        dump_packets: When True, the dedicated packet logger is set to DEBUG so
            :func:`log_packet` output is emitted; otherwise it is muted.
    """
    resolved = _LEVELS.get(level.lower(), logging.INFO)

    handler = _DynamicStderrHandler()
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger()
    # Replace any handlers we previously installed so re-running is idempotent.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved)

    packet_logger = logging.getLogger(PACKET_LOGGER_NAME)
    packet_logger.setLevel(logging.DEBUG if dump_packets else logging.CRITICAL + 1)


def hexdump(data: bytes, *, width: int = 16) -> str:
    """Return a classic offset/hex/ASCII hexdump of ``data``."""
    lines: list[str] = []
    for offset in range(0, len(data), width):
        chunk = data[offset : offset + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        # Pad so the ASCII column lines up on short final rows.
        hex_part = hex_part.ljust(width * 3 - 1)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:08x}  {hex_part}  |{ascii_part}|")
    return "\n".join(lines) if lines else "<empty>"


def log_packet(direction: str, addr: object, data: bytes) -> None:
    """Emit a hexdump of a packet to the packet logger (no-op unless enabled).

    Args:
        direction: e.g. ``"SEND"`` / ``"RECV"``.
        addr: Peer address (anything reprs sensibly, e.g. ``(host, port)``).
        data: The raw datagram bytes.
    """
    logger = logging.getLogger(PACKET_LOGGER_NAME)
    if not logger.isEnabledFor(logging.DEBUG):
        return
    logger.debug(
        "%s %s (%d bytes)\n%s", direction, addr, len(data), hexdump(data)
    )
