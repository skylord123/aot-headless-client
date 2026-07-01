"""Unit tests for AotClient: chat-line parsing (chat.cs parity), the
clientCmd* dispatch into structured callbacks, and login crc encoding.
"""

import asyncio

from aotbot.bitstream import BitStream
from aotbot.client import AotClient, parse_chat_line
from aotbot.config import Config
from aotbot.crc import get_string_crc
from aotbot.events import EventManager


def _cfg(**over):
    base = dict(
        aot_server_host="127.0.0.1",
        aot_server_port=28000,
        aot_username="bot",
        aot_password="secret",
    )
    base.update(over)
    return Config(**base)


# --------------------------------------------------------------------------- #
# Chat line parsing (mirrors base/skylord/helpers/chat.cs)
# --------------------------------------------------------------------------- #


def test_parse_local_chat():
    out = parse_chat_line('Alice says, "hello world"')
    assert out["scope"] == "local"
    assert out["name"] == "Alice"
    assert out["message"] == "hello world"


def test_parse_global_chat():
    out = parse_chat_line("Bob: hey everyone")
    assert out["scope"] == "global"
    assert out["name"] == "Bob"
    assert out["message"] == "hey everyone"


def test_parse_global_with_colon_in_message():
    # Colon appears after the name colon; still global (name is up to first ':').
    out = parse_chat_line("Carol: ratio 3:1 today")
    assert out["scope"] == "global"
    assert out["name"] == "Carol"
    assert out["message"] == "ratio 3:1 today"


def test_parse_strips_ml_control_chars():
    raw = "\x02\x07Dave says, \"yo\""
    out = parse_chat_line(raw)
    assert out["name"] == "Dave"
    assert out["message"] == "yo"
    assert out["raw"] == raw


# --------------------------------------------------------------------------- #
# clientCmd* dispatch -> client callbacks (round-trip through EventManager)
# --------------------------------------------------------------------------- #


def _feed_command(client: AotClient, verb: str, *args):
    """Encode commandToServer(verb, *args) from a peer and feed it into the
    client's EventManager, exercising the full unpack + dispatch path."""
    peer = EventManager()
    peer.command_to_server(verb, *args)
    bs = BitStream()
    peer.write_events(bs, current_send_seq=1)
    client.events.read_events(BitStream(bs.get_bytes()))


def test_chat_message_dispatch_emits_on_chat():
    client = AotClient(_cfg())
    got = []
    client.on_chat = lambda scope, name, msg, raw: got.append((scope, name, msg))
    _feed_command(client, "ChatMessage", 'Eve says, "hi"')
    assert got == [("local", "Eve", "hi")]


def test_chat_message_substitutes_global_template_args():
    # Real server layout: clientCmdChatMessage(sender, voice, pitch, template,
    # %a1, %a2). The template "%1: %2" must be filled from the trailing args
    # BEFORE parsing (regression: previously name/message came through as %1/%2).
    client = AotClient(_cfg())
    got = []
    client.on_chat = lambda scope, name, msg, raw: got.append((scope, name, msg))
    _feed_command(client, "ChatMessage", "7", "0", "0", "%1: %2", "Bob", "hello there")
    assert got == [("global", "Bob", "hello there")]


def test_chat_message_substitutes_local_template_args():
    client = AotClient(_cfg())
    got = []
    client.on_chat = lambda scope, name, msg, raw: got.append((scope, name, msg))
    _feed_command(client, "ChatMessage", "7", "0", "0", '%1 says, "%2"', "Alice", "hi all")
    assert got == [("local", "Alice", "hi all")]


def test_server_message_dispatch_emits():
    client = AotClient(_cfg())
    got = []
    client.on_server_message = lambda t, text, extra: got.append((t, text))
    _feed_command(client, "ServerMessage", "0", "Welcome to AoT")
    assert got == [("0", "Welcome to AoT")]


def test_empty_server_message_is_suppressed():
    # Control messages with an empty msgString never reach the chat HUD
    # (onServerMessage's getWordCount gate), so no server_message is emitted.
    client = AotClient(_cfg())
    got = []
    client.on_server_message = lambda t, text, extra: got.append((t, text))
    # MsgClientScoreChanged carries no human text.
    _feed_command(client, "ServerMessage", "MsgClientScoreChanged", "", "\x04Port Town", "48708")
    # MsgClientJoin with empty msgString (the common roster-sync case).
    _feed_command(client, "ServerMessage", "MsgClientJoin", "", "DiscordBot", "38239", "", "\x04Port Town", "0", "0", "0")
    assert got == []


def test_msg_client_join_emits_player_joined():
    client = AotClient(_cfg())
    joined = []
    server_msgs = []
    client.on_player_joined = lambda name, cid, loc, msg, users: joined.append((name, cid, loc, msg, users))
    client.on_server_message = lambda t, text, extra: server_msgs.append(text)
    _feed_command(client, "ServerMessage", "MsgClientJoin", "", "DiscordBot", "38239", "", "\x04Port Town", "0", "0", "0")
    assert joined == [("DiscordBot", 38239, "Port Town", "", ["DiscordBot"])]
    assert server_msgs == []  # empty msgString -> no chat-HUD line


def test_msg_client_join_with_text_emits_both():
    client = AotClient(_cfg())
    joined = []
    server_msgs = []
    client.on_player_joined = lambda name, cid, loc, msg, users: joined.append((name, msg, users))
    client.on_server_message = lambda t, text, extra: server_msgs.append(text)
    _feed_command(client, "ServerMessage", "MsgClientJoin", "\x02A new player connected.", "<Connecting>", "48715", "", "", "0", "0", "0")
    # Placeholder "<Connecting>" name is NOT recorded as a real username.
    assert joined == [("<Connecting>", "A new player connected.", [])]
    assert server_msgs == ["A new player connected."]  # non-empty -> also a HUD line


def test_msg_client_drop_emits_player_dropped_with_name_and_message():
    client = AotClient(_cfg())
    dropped = []
    server_msgs = []
    client.on_player_dropped = lambda name, cid, msg, users: dropped.append((name, cid, msg, users))
    client.on_server_message = lambda t, text, extra: server_msgs.append(text)
    # Client joins first so the registry has the username history before the drop.
    _feed_command(client, "ServerMessage", "MsgClientJoin", "", "What's For Dinner", "39570", "", "\x04Shop", "0", "0", "0")
    _feed_command(client, "ServerMessage", "MsgClientDrop", "\x02%1 has left the game.", "What's For Dinner", "39570")
    assert dropped == [("What's For Dinner", 39570, "What's For Dinner has left the game.", ["What's For Dinner"])]
    assert server_msgs == ["What's For Dinner has left the game."]


def test_zone_change_emits_with_resolved_player_name():
    client = AotClient(_cfg())
    zones = []
    server_msgs = []
    client.on_zone_change = lambda player, zone, cid: zones.append((player, zone, cid))
    client.on_server_message = lambda t, text, extra: server_msgs.append(text)
    # Player must be in the roster for the name to resolve (like the in-game
    # playerList_playerNameFromId lookup).
    _feed_command(client, "ServerMessage", "MsgClientJoin", "", "Alice", "51592", "", "\x04The Wilderness", "0", "0", "0")
    _feed_command(client, "ServerMessage", "MsgClientScoreChanged", "", "\x04Port Town", "51592")
    assert zones == [("Alice", "Port Town", 51592)]
    assert server_msgs == []  # zone change carries no chat-HUD line


def test_zone_change_skipped_for_unknown_or_placeholder_player():
    client = AotClient(_cfg())
    zones = []
    client.on_zone_change = lambda player, zone, cid: zones.append((player, zone))
    # Unknown client_id -> no name resolved -> no event.
    _feed_command(client, "ServerMessage", "MsgClientScoreChanged", "", "\x04Port Town", "99999")
    # Logged-out placeholder name -> skipped.
    _feed_command(client, "ServerMessage", "MsgClientJoin", "", "<Logged Out>", "12345", "", "", "0", "0", "0")
    _feed_command(client, "ServerMessage", "MsgClientScoreChanged", "", "\x04Shop", "12345")
    assert zones == []


def test_associated_usernames_accumulate_across_relogin():
    # Same connection (client_id) logs out and back in as different characters;
    # all real names accumulate, placeholders are ignored, order is preserved.
    client = AotClient(_cfg())
    joined = []
    client.on_player_joined = lambda name, cid, loc, msg, users: joined.append((name, list(users)))
    cid = "48708"
    _feed_command(client, "ServerMessage", "MsgClientJoin", "", "Alice", cid, "", "\x04Port Town", "0", "0", "0")
    _feed_command(client, "ServerMessage", "MsgClientJoin", "", "<Logged Out>", cid, "", "", "0", "0", "0")
    _feed_command(client, "ServerMessage", "MsgClientJoin", "", "Bob", cid, "", "\x04Shop", "0", "0", "0")
    _feed_command(client, "ServerMessage", "MsgClientJoin", "", "Alice", cid, "", "\x04Shop", "0", "0", "0")  # dup
    assert [u for _, u in joined] == [
        ["Alice"],
        ["Alice"],            # placeholder ignored, history unchanged
        ["Alice", "Bob"],
        ["Alice", "Bob"],     # duplicate not re-added
    ]
    # Exposed on the roster too.
    roster = {p["client_id"]: p for p in client.get_players()}
    assert roster[48708]["associated_usernames"] == ["Alice", "Bob"]


def test_login_success_marks_logged_in():
    client = AotClient(_cfg())
    results = []
    client.on_login_result = lambda ok, detail: results.append((ok, detail))
    client._login_user = "bot"
    _feed_command(client, "LoginSuccess")
    assert client.logged_in is True
    assert results and results[0][0] is True


def test_warning_box_reports_login_failure():
    client = AotClient(_cfg())
    results = []
    client.on_login_result = lambda ok, detail: results.append((ok, detail))
    _feed_command(client, "WarningBox", "Character does not exist!", "OK")
    assert results == [(False, "Character does not exist!")]


def test_server_message_logged_in_broadcast_marks_login():
    client = AotClient(_cfg())
    results = []
    client.on_login_result = lambda ok, detail: results.append(ok)
    client._login_user = "bot"
    _feed_command(client, "ServerMessage", "0", "bot logged in.")
    assert client.logged_in is True
    assert results == [True]


# --------------------------------------------------------------------------- #
# Outgoing actions: login crc + chat verbs
# --------------------------------------------------------------------------- #


def test_login_sends_crc_hash():
    client = AotClient(_cfg(aot_password="hunter2"))
    sent = []
    # Capture what the EventManager queues.
    orig = client.events.command_to_server
    client.events.command_to_server = lambda verb, *a: sent.append((verb, a))
    client.login()
    assert sent[0][0] == "login"
    assert sent[0][1][0] == "bot"
    assert sent[0][1][1] == get_string_crc("hunter2")


def test_say_and_global_use_correct_verbs():
    client = AotClient(_cfg())
    sent = []
    client.events.command_to_server = lambda verb, *a: sent.append((verb, a))
    client.say("local hi")
    client.global_chat("global hi")
    assert sent[0] == ("Talk", ("local hi",))
    assert sent[1] == ("MessageSent", ("global hi",))


def test_chat_roundtrip_say_then_decode():
    """End-to-end: a Talk we send encodes, and a peer decodes the same verb."""
    client = AotClient(_cfg())
    peer = EventManager()
    decoded = []
    peer.on_client_cmd("Talk", lambda args, evt: decoded.append(args[0]))

    client.say("round trip")
    bs = BitStream()
    client.events.write_events(bs, current_send_seq=1)
    peer.read_events(BitStream(bs.get_bytes()))
    assert decoded == ["round trip"]


# --------------------------------------------------------------------------- #
# New-character registration (mirrors helpers/newCharacter.cs registerNewUser)
# --------------------------------------------------------------------------- #


def test_register_new_user_emits_newcharacter_with_arg_order():
    client = AotClient(_cfg())
    sent = []
    client.events.command_to_server = lambda verb, *a: sent.append((verb, a))
    client.register_new_user(
        "Bob Smith", "secret", overwrite=False, gender=1, posture=0.5, chest=0.5,
        x_scale=1.0, y_scale=1.0, z_scale=1.0, skin_tone=5, lip_tone=6,
        hair_style=1, hair_color=2, eye_color=0, face=0, ears=1, glasses=0,
        abilities="10 10 1 5 1 1 1",
    )
    verb, a = sent[0]
    assert verb == "newCharacter"
    # name, crc, gender, posture, chest, x, y, z, skin, lip, hair, hairC, eye,
    # face, ears, glasses, abilities, overwrite  == 18 args.
    assert len(a) == 18
    assert a[0] == "Bob Smith"
    assert a[1] == 1554180325  # getStringCRC("secret") == zlib.crc32
    assert a[2] == 1           # gender
    assert a[8] == 5 and a[9] == 6     # skinTone, lipTone
    assert a[16] == "10 10 1 5 1 1 1"  # abilities
    assert a[17] == 0          # overwrite


def test_register_randomizes_unset_appearance_in_range():
    client = AotClient(_cfg())
    sent = []
    client.events.command_to_server = lambda verb, *a: sent.append(a)
    client.register_new_user("R", "pw")  # everything random
    a = sent[0]
    assert a[2] in (0, 1)                 # gender
    assert 0 <= float(a[3]) <= 1.0        # posture
    assert 0.9 <= float(a[5]) <= 1.1      # xScale
    skin, lip = a[8], a[9]
    assert 0 <= skin <= 9 and skin <= lip <= 9   # lipTone >= skinTone
    assert a[16] == "1 1 1 1 1 1 1"       # default abilities


def test_auto_create_on_character_does_not_exist():
    client = AotClient(_cfg(aot_create_user=True))
    sent = []
    client.events.command_to_server = lambda verb, *a: sent.append(verb)
    client.login("Newbie", "pw")
    _feed_command(client, "WarningBox", "Character does not exist!")
    assert "newCharacter" in sent


def test_no_auto_create_when_disabled():
    client = AotClient(_cfg(aot_create_user=False))
    sent = []
    client.events.command_to_server = lambda verb, *a: sent.append(verb)
    client.login("Newbie", "pw")
    _feed_command(client, "WarningBox", "Character does not exist!")
    assert "newCharacter" not in sent


def test_new_character_created_marks_logged_in():
    client = AotClient(_cfg(aot_create_user=True))
    client.events.command_to_server = lambda *a, **k: None
    client.login("Bob", "pw")
    _feed_command(client, "WarningBox", "Character does not exist!")  # -> register
    _feed_command(client, "ServerMessage", "MsgType", "New character created: Bob")
    assert client.logged_in


def test_register_uses_config_appearance_and_randomizes_blanks():
    client = AotClient(_cfg(
        aot_create_gender="1", aot_create_skin_tone="3", aot_create_hair_color="4",
        aot_create_face="-1",          # -1 -> random
        aot_create_glasses="garbage",  # unparseable -> random (no crash)
    ))
    sent = []
    client.events.command_to_server = lambda verb, *a: sent.append(a)
    client.register_new_user("Bob", "pw")  # appearance pulled from config/random
    a = sent[0]
    assert a[2] == 1 and a[8] == 3 and a[11] == 4   # gender, skin, hairColor (fixed)
    assert a[9] >= 3                                 # lipTone clamped >= skinTone
    assert a[13] in (0, 1) and a[15] in (0, 1)       # face(-1) / glasses(bad) -> random


# --------------------------------------------------------------------------- #
# Connection status (state + logged_in) + reset()
# --------------------------------------------------------------------------- #


def test_connection_status_defaults_disconnected_loggedout():
    client = AotClient(_cfg())
    assert client.connection_status() == {"state": "disconnected", "logged_in": False}


def test_login_success_emits_connection_status_change():
    # LoginSuccess flips the login flag; the connection-status callback fires with
    # the unchanged state and logged_in=True.
    client = AotClient(_cfg())
    events = []
    client.on_connection_state = lambda state, logged_in: events.append((state, logged_in))
    client._login_user = "bot"
    _feed_command(client, "LoginSuccess")
    assert client.connection_status() == {"state": "disconnected", "logged_in": True}
    assert events == [("disconnected", True)]


def test_connection_status_dedupes_repeat_emits():
    client = AotClient(_cfg())
    events = []
    client.on_connection_state = lambda state, logged_in: events.append((state, logged_in))
    client._set_connection("connecting")
    client._set_connection("connecting")  # identical -> suppressed
    client._set_connection("connected")
    assert events == [("connecting", False), ("connected", False)]


def test_mark_reconnecting_reports_state_and_clears_login():
    client = AotClient(_cfg())
    client._logged_in = True
    events = []
    client.on_connection_state = lambda state, logged_in: events.append((state, logged_in))
    client.mark_reconnecting()
    assert client.connection_status() == {"state": "reconnecting", "logged_in": False}
    assert events == [("reconnecting", False)]


def test_reset_rebuilds_fresh_protocol_stack():
    client = AotClient(_cfg())
    # Dirty the per-connection state.
    client._logged_in = True
    client._login_user = "bot"
    client._set_connection("connected")
    old_events, old_phases = client.events, client.phases
    client.reset()
    assert client.connection_status() == {"state": "disconnected", "logged_in": False}
    assert client._login_user is None
    assert client.events is not old_events
    assert client.phases is not old_phases
    # Public callbacks survive a reset.
    marker = lambda *a: None
    client.on_chat = marker
    client.reset()
    assert client.on_chat is marker


# --------------------------------------------------------------------------- #
# Server clock sync (clientCmdSyncClock)
# --------------------------------------------------------------------------- #


def test_sync_clock_emits_uptime_and_receive_time():
    client = AotClient(_cfg())
    got = []
    client.on_sync_clock = lambda uptime, received_at: got.append((uptime, received_at))
    _feed_command(client, "SyncClock", "86400")
    assert len(got) == 1
    uptime, received_at = got[0]
    assert uptime == 86400.0
    assert received_at > 0


def test_sync_clock_ignores_unparseable_value():
    client = AotClient(_cfg())
    got = []
    client.on_sync_clock = lambda uptime, received_at: got.append((uptime, received_at))
    _feed_command(client, "SyncClock", "not-a-number")
    assert got == []


def test_sync_clock_status_defaults_to_nulls():
    client = AotClient(_cfg())
    assert client.sync_clock_status() == {"uptime_seconds": None, "received_at": None}


def test_sync_clock_status_returns_last_value():
    client = AotClient(_cfg())
    _feed_command(client, "SyncClock", "3600")
    status = client.sync_clock_status()
    assert status["uptime_seconds"] == 3600.0
    assert status["received_at"] > 0
    # A second sync replaces the stored value.
    _feed_command(client, "SyncClock", "120")
    assert client.sync_clock_status()["uptime_seconds"] == 120.0


def test_sync_clock_status_survives_reset():
    client = AotClient(_cfg())
    _feed_command(client, "SyncClock", "999")
    client.reset()
    assert client.sync_clock_status()["uptime_seconds"] == 999.0
