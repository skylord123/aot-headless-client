"""Tests for the online-player roster and roster<->ghost object matching."""

import time

from aotbot.playerlist import (
    PlayerListRegistry,
    PlayerInfo,
    match_player_objects,
)


# clientCmdServerMessage(msgType, msgString, a1, a2, ...) -> the bot passes the
# args AFTER msgType/msgString as `extra`. For MsgClientJoin that is
# [name, clientId, _, location, isAI, isAdmin, isSuperAdmin] (playerList.cs);
# the "score" slot is repurposed as the player's world region on this server.
def _join_extra(name, cid, location="Port Town", ai=0, admin=0, sup=0):
    return [name, str(cid), "", str(location), str(ai), str(admin), str(sup)]


def test_join_drop_and_zone_change():
    reg = PlayerListRegistry()
    reg.handle_server_message("MsgClientJoin",
                              _join_extra("Jeff Bezos", 42, location="Port Town"))
    reg.handle_server_message("MsgClientJoin", _join_extra("Horse", 7, ai=1))
    names = {p.name: p for p in reg.list()}
    assert set(names) == {"Jeff Bezos", "Horse"}
    assert names["Jeff Bezos"].client_id == 42
    assert names["Jeff Bezos"].location == "Port Town"
    assert names["Horse"].is_ai is True

    # MsgClientScoreChanged is a ZONE change: extra[0] is the new region.
    reg.handle_server_message("MsgClientScoreChanged", ["Wilderness", "42"])
    assert {p.name: p.location for p in reg.list()}["Jeff Bezos"] == "Wilderness"

    # Drop removes by clientId.
    reg.handle_server_message("MsgClientDrop", ["Jeff Bezos", "42"])
    assert [p.name for p in reg.list()] == ["Horse"]


def test_tag_precedence_and_ml_strip():
    reg = PlayerListRegistry()
    reg.handle_server_message("MsgClientJoin",
                              _join_extra("\x02Admin Guy", 1, admin=1, sup=1))
    p = reg.list()[0]
    assert p.name == "Admin Guy"          # ML control char stripped
    assert p.tag == "[Super]"             # super outranks admin


def test_join_preserves_original_join_time():
    reg = PlayerListRegistry()
    reg.handle_server_message("MsgClientJoin", _join_extra("X", 1))
    first = reg.list()[0].joined_at
    time.sleep(0.01)
    # A re-broadcast must not reset joined_at, but may update the region.
    reg.handle_server_message("MsgClientJoin", _join_extra("X", 1, location="Cove"))
    p = reg.list()[0]
    assert p.joined_at == first
    assert p.location == "Cove"


def test_to_dict_has_unix_join_timestamp_and_location():
    p = PlayerInfo(client_id=1, name="Bob", location="Port Town")
    d = p.to_dict()
    assert isinstance(d["joined_at"], int)
    assert abs(d["joined_at"] - time.time()) < 5
    assert d["tag"] == "" and d["client_id"] == 1
    assert d["location"] == "Port Town" and "score" not in d


def test_match_player_objects_by_name_and_class():
    players = [
        PlayerInfo(client_id=1, name="Jeff Bezos"),
        PlayerInfo(client_id=2, name="Nobody Here"),
    ]
    objects = [
        # Real player ghost: class Player, name matches.
        {"ghost_id": 5, "class_name": "Player", "name": "Jeff Bezos",
         "position": [292.6, 170.1, 213.2], "is_control_object": False},
        # An AIPlayer with the same name must NOT match (class filter).
        {"ghost_id": 9, "class_name": "AIPlayer", "name": "Jeff Bezos",
         "position": [0, 0, 0]},
        # Unrelated Player ghost.
        {"ghost_id": 6, "class_name": "Player", "name": "Horse",
         "position": [1, 2, 3]},
    ]
    out = match_player_objects(players, objects)
    by_name = {p["name"]: p for p in out}

    jeff = by_name["Jeff Bezos"]
    assert jeff["object_id"] == 5            # the Player ghost, not the AIPlayer
    assert jeff["position"] == [292.6, 170.1, 213.2]
    assert jeff["object"]["ghost_id"] == 5   # full object JSON included
    assert jeff["is_self"] is False

    # No scoped ghost for this player -> object fields are None, not an error.
    nobody = by_name["Nobody Here"]
    assert nobody["object_id"] is None
    assert nobody["position"] is None
    assert nobody["object"] is None


def test_match_flags_control_object_as_self():
    players = [PlayerInfo(client_id=1, name="Mr Poopy Butthole")]
    objects = [{"ghost_id": 3, "class_name": "Player",
                "name": "Mr Poopy Butthole", "position": [1, 1, 1],
                "is_control_object": True}]
    out = match_player_objects(players, objects)
    assert out[0]["is_self"] is True
    assert out[0]["object_id"] == 3
