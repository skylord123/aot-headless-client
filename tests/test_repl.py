"""Tests for the interactive REPL command parsing, completion, and suggestions.

No TTY is used: we drive Repl._dispatch directly and exercise the completer /
auto-suggest with prompt_toolkit Documents.
"""

import asyncio

from prompt_toolkit.document import Document

from aotbot.repl import Repl, _SlashCompleter, _SlashAutoSuggest


class FakeClient:
    def __init__(self):
        self.calls = []
        self.conn = None
        self._login_user = None
        self.logged_in = False

    def global_chat(self, m): self.calls.append(("global", m))
    def say(self, m): self.calls.append(("local", m))
    def command_to_server(self, v, *a): self.calls.append(("cts", v, a))
    def login(self, u=None, p=None): self.calls.append(("login", u, p))
    def logout(self): self.calls.append(("logout",))
    def register_new_user(self, n=None, p=None): self.calls.append(("register", n, p))
    def list_objects(self, include_removed=False): return []
    def get_object(self, g): return None
    def get_players(self): return []


def _repl():
    return Repl(FakeClient(), asyncio.Event())


def _run(repl, *lines):
    async def go():
        for ln in lines:
            await repl._dispatch(ln)
    asyncio.run(go())


def test_say_is_global_lsay_is_local():
    r = _repl()
    _run(r, "/say hello world", "/lsay hi there")
    assert r.client.calls == [("global", "hello world"), ("local", "hi there")]


def test_cts_and_alias_with_quoting():
    r = _repl()
    _run(r, "/cts respawn", "/commandtoserver BankDeposit 100",
         '/raw Talk "quoted msg"')
    assert r.client.calls == [
        ("cts", "respawn", ()),
        ("cts", "BankDeposit", ("100",)),
        ("cts", "Talk", ("quoted msg",)),
    ]


def test_unknown_and_non_slash_are_safe():
    r = _repl()
    _run(r, "no slash here", "/bogus arg")
    assert r.client.calls == []  # nothing dispatched, no crash


def test_login_register_logout():
    r = _repl()
    _run(r, "/login alice secret", "/register Bob pw", "/logout", "/login")
    assert ("login", "alice", "secret") in r.client.calls
    assert ("register", "Bob", "pw") in r.client.calls
    assert ("logout",) in r.client.calls
    assert ("login", None, None) in r.client.calls


def test_players_table_renders(capsys):
    r = _repl()
    r.client.get_players = lambda: [
        {"name": "Jeff Bezos", "tag": "", "is_self": False, "location": "Port Town",
         "position": [292.6, 170.1, 213.2], "object_id": 5,
         "joined_at": 1_700_000_000, "object": {"ghost_id": 5}},
        {"name": "Mr Poopy Butthole", "tag": "", "is_self": True, "location": "",
         "position": None, "object_id": None,
         "joined_at": 1_700_000_050, "object": None},
    ]
    _run(r, "/players")
    out = capsys.readouterr().out
    assert "2 player(s) online:" in out
    assert "Jeff Bezos" in out and "293, 170, 213" in out  # .0f rounds 292.6
    assert "Port Town" in out                # world region (LOCATION column)
    assert "*me" in out                      # control object flagged
    assert "—" in out                        # missing region/position/obj id


def test_players_empty(capsys):
    r = _repl()
    _run(r, "/players")
    assert "no players" in capsys.readouterr().out


def _complete(r, text):
    comp = _SlashCompleter(r)
    return [c.text for c in comp.get_completions(Document(text, len(text)), None)]


def test_command_name_completion():
    r = _repl()
    assert "commandtoserver" in _complete(r, "/comm")
    assert "commands" not in _complete(r, "/comm")  # help alias removed
    assert set(_complete(r, "/s")) >= {"say", "status"}


def test_cts_verb_completion():
    r = _repl()
    assert _complete(r, "/cts re") == ["respawn"]
    assert "BankDeposit" in _complete(r, "/cts Bank")


def test_autosuggest_ghost_text():
    r = _repl()
    sug = _SlashAutoSuggest(r)
    s = sug.get_suggestion(None, Document("/command", len("/command")))
    assert s is not None and ("/command" + s.text) == "/commandtoserver"
    # no suggestion once there's a space (in args)
    assert sug.get_suggestion(None, Document("/cts ", 5)) is None
