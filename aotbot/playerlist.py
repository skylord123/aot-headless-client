"""Online-player roster, driven by the server's player-list messages.

The server keeps every client's PlayerListGui in sync by broadcasting message-
system messages (base/client/message.cs -> clientCmdServerMessage dispatches to
addMessageCallback handlers in base/client/scripts/playerList.cs):

* ``MsgClientJoin(name, clientId, _, location, isAI, isAdmin, isSuperAdmin)`` --
  a client joined (also re-sent for everyone already online when WE connect).
* ``MsgClientDrop(name, clientId)`` -- a client left.
* ``MsgClientScoreChanged(location, clientId)`` -- despite the name, this AoT
  server repurposes the "score" field/message as the player's WORLD REGION
  (e.g. "Port Town", "Wilderness"); MsgClientScoreChanged is fired on every zone
  change with the new region name (see base/skylord/bot/NodeRED.cs
  playerTrackerClientZoneChange + serverStatsJsonString, which reads the region
  out of the PlayerListGui "score" column).

clientCmdServerMessage(%msgType, %msgString, %a1, %a2, ...) invokes the callback
as ``call(func, msgType, msgString, a1, a2, ...)`` so, after the bot strips the
leading ``msgType``/``msgString``, the callback args line up as ``extra[0]=a1``
(name), ``extra[1]=a2`` (clientId), etc. -- see playerList.cs::handleClientJoin.

This module owns the roster (:class:`PlayerListRegistry`) and the pure function
that joins the roster to the live ghost objects (:func:`match_player_objects`),
so the matching is unit-testable without a live connection.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _strip_ml(s: str) -> str:
    """Drop Torque ML markup control chars (mirrors StripMLControlChars)."""
    return "".join(c for c in s if c == " " or ord(c) >= 0x20)


def _norm_name(s: Optional[str]) -> str:
    """Canonical form for matching a roster name to a ghost's shape name."""
    return _strip_ml(s or "").strip().lower()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def _to_bool(value: Any) -> bool:
    return str(value).strip() not in ("", "0", "false", "False")


def _get(seq: List[Any], i: int, default: Any = "") -> Any:
    return seq[i] if 0 <= i < len(seq) else default


@dataclass
class PlayerInfo:
    """One online client as the server's player-list messages describe it."""

    client_id: int
    name: str
    # World region the player is in (e.g. "Port Town"), from the server's
    # repurposed "score" field. Empty for <Logged Out>/<Connecting> clients.
    location: str = ""
    is_ai: bool = False
    is_admin: bool = False
    is_super_admin: bool = False
    # Unix timestamp we first saw this client join. For clients already online
    # when the bot connects (the server re-sends MsgClientJoin for everyone on
    # connect), this is the bot's connect time, not their true join time.
    joined_at: float = field(default_factory=time.time)

    @property
    def tag(self) -> str:
        # Mirrors PlayerListGui::update's precedence: Super > Admin > Bot.
        if self.is_super_admin:
            return "[Super]"
        if self.is_admin:
            return "[Admin]"
        if self.is_ai:
            return "[Bot]"
        return ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client_id": self.client_id,
            "name": self.name,
            "location": self.location,
            "is_ai": self.is_ai,
            "is_admin": self.is_admin,
            "is_super_admin": self.is_super_admin,
            "tag": self.tag,
            "joined_at": int(self.joined_at),  # unix timestamp (seconds)
        }


class PlayerListRegistry:
    """``clientId -> PlayerInfo`` roster fed by the player-list server messages."""

    def __init__(self) -> None:
        self._players: Dict[int, PlayerInfo] = {}

    # -- message ingest ---------------------------------------------------- #

    def handle_server_message(self, msg_type: str, extra: List[Any]) -> bool:
        """Update the roster from a server message. ``extra`` is the arg list
        AFTER msgType/msgString (i.e. ``a1, a2, ...``). Returns True if handled.
        """
        if msg_type == "MsgClientJoin":
            self._join(extra)
        elif msg_type == "MsgClientDrop":
            self._drop(extra)
        elif msg_type == "MsgClientScoreChanged":
            self._zone_change(extra)
        else:
            return False
        return True

    def _join(self, extra: List[Any]) -> None:
        # playerList.cs handleClientJoin(name, clientId, _, location, isAI,
        # isAdmin, isSuperAdmin) == extra[0..6].
        client_id = _to_int(_get(extra, 1), default=-1)
        if client_id < 0:
            return
        name = _strip_ml(str(_get(extra, 0))).strip()
        location = _strip_ml(str(_get(extra, 3))).strip()
        is_ai = _to_bool(_get(extra, 4))
        is_admin = _to_bool(_get(extra, 5))
        is_super = _to_bool(_get(extra, 6))
        existing = self._players.get(client_id)
        if existing is None:
            self._players[client_id] = PlayerInfo(
                client_id=client_id, name=name, location=location, is_ai=is_ai,
                is_admin=is_admin, is_super_admin=is_super,
            )
        else:
            # Refresh fields but preserve the original join time.
            existing.name = name
            existing.location = location
            existing.is_ai = is_ai
            existing.is_admin = is_admin
            existing.is_super_admin = is_super

    def _drop(self, extra: List[Any]) -> None:
        # handleClientDrop(name, clientId) == extra[0..1].
        client_id = _to_int(_get(extra, 1), default=-1)
        self._players.pop(client_id, None)

    def _zone_change(self, extra: List[Any]) -> None:
        # MsgClientScoreChanged(location, clientId) == extra[0..1]: the "score"
        # arg is the player's new world region (NodeRED.cs zone-change handler).
        location = _strip_ml(str(_get(extra, 0))).strip()
        client_id = _to_int(_get(extra, 1), default=-1)
        p = self._players.get(client_id)
        if p is not None:
            p.location = location

    # -- query ------------------------------------------------------------- #

    def list(self) -> List[PlayerInfo]:
        return sorted(self._players.values(), key=lambda p: (p.name.lower(), p.client_id))

    def clear(self) -> None:
        self._players.clear()


def match_player_objects(
    players: List[PlayerInfo], objects: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Join the roster to live ghost objects.

    Each online player's object id is found by matching its name to a ghost whose
    netclass is exactly ``Player`` (NOT ``AIPlayer``) and whose shape name (the
    mShapeNameTag username) equals the player's name. If no such ghost is scoped
    (e.g. the player is too far away to be ghosted to us), the object fields are
    ``None``. Returns each player's :meth:`PlayerInfo.to_dict` augmented with
    ``object_id``, ``position``, ``is_self`` and the full ``object`` dict.
    """
    by_name: Dict[str, Dict[str, Any]] = {}
    for o in objects:
        if str(o.get("class_name", "")).lower() != "player":
            continue
        nm = _norm_name(o.get("name") or o.get("shape_name"))
        if nm:
            by_name.setdefault(nm, o)  # first scoped match wins
    out: List[Dict[str, Any]] = []
    for p in players:
        obj = by_name.get(_norm_name(p.name))
        d = p.to_dict()
        d["object_id"] = obj.get("ghost_id") if obj else None
        d["position"] = obj.get("position") if obj else None
        d["is_self"] = bool(obj and obj.get("is_control_object"))
        d["object"] = obj  # full player object JSON, or None
        out.append(d)
    return out
