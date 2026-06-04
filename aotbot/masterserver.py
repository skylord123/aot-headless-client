"""Resolve the Age of Time server address from the master server list.

When ``AOT_SERVER_HOST`` is not set, the bot fetches the master server text file
(default ``https://master.ageoftime.com/server.txt``) and parses the advertised
IP from it. The file is a handful of ``KEY value`` lines, e.g.::

    IP 45.148.165.55
    NAME AoT Server
    PLAYERS ?/30

Only the ``IP`` is used (the port is not advertised, so ``AOT_SERVER_PORT`` --
default 28000 -- still applies).
"""

from __future__ import annotations

import logging
import urllib.request
from typing import Dict
from urllib.parse import urlparse

logger = logging.getLogger("aotbot.masterserver")

DEFAULT_MASTER_URL = "https://master.ageoftime.com/server.txt"

# The master server (Cloudflare-fronted) rejects the default "Python-urllib"
# User-Agent with HTTP 403, so send an explicit one.
USER_AGENT = "Mozilla/5.0 (compatible; aot-headless-client/0.1)"


class MasterServerError(RuntimeError):
    """Raised when the master server list can't be fetched or has no IP."""


def parse_server_txt(text: str) -> Dict[str, str]:
    """Parse the ``KEY rest-of-line`` pairs into a dict (keys upper-cased).

    Lines without a value (or blank) are tolerated; the first whitespace splits
    key from value, so values may themselves contain spaces (e.g. "AoT Server").
    """
    fields: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        key = parts[0].upper()
        fields[key] = parts[1].strip() if len(parts) > 1 else ""
    return fields


def fetch_server_host(url: str = DEFAULT_MASTER_URL, *, timeout: float = 10.0) -> str:
    """Fetch the master server list at ``url`` and return the advertised IP.

    Raises:
        MasterServerError: on a network/HTTP failure, a non-http(s) URL, or a
            list with no usable ``IP`` line.
    """
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise MasterServerError(
            f"refusing to fetch master server list from non-http(s) URL: {url!r}"
        )
    logger.info("resolving server address from master server %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - scheme checked above
            raw = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - surface any fetch failure uniformly
        raise MasterServerError(
            f"failed to fetch master server list from {url}: {exc}"
        ) from exc

    fields = parse_server_txt(raw)
    ip = fields.get("IP", "").strip()
    if not ip:
        raise MasterServerError(
            f"master server list at {url} had no IP line (keys: {sorted(fields)})"
        )
    logger.info(
        "master server advertises IP %s (name=%r players=%r)",
        ip, fields.get("NAME"), fields.get("PLAYERS"),
    )
    return ip
