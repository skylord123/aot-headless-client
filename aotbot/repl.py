"""Interactive terminal REPL for the bot.

Type slash-commands while the bot is connected, e.g.::

    /help
    /say hello everyone          (global chat)
    /lsay hi neighbours          (local/proximity chat)
    /cts respawn                 (commandToServer; alias of /commandtoserver)
    /objects                     (list scoped objects -- needs AOT_TRACK_OBJECTS)

Features (via prompt_toolkit): Tab-completion of command names (and the verb of
/cts), fish-style inline auto-suggestion (the matching command shown as ghost
text; accept with -> / End), and history. It runs as an asyncio task alongside
the live bot; ``patch_stdout`` keeps the bot's log output rendering cleanly above
the prompt.

The REPL is best-effort: if prompt_toolkit is missing or stdin isn't a TTY, the
caller falls back to a plain wait loop (see main.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.patch_stdout import patch_stdout

logger = logging.getLogger("aotbot.repl")

# commandToServer verbs offered as completions for /cts (from the AoT scripts).
KNOWN_VERBS = [
    "Talk", "MessageSent", "login", "logout", "use", "cast", "action",
    "BankDeposit", "Discard", "respawn", "suicide", "newCharacter",
    "ToggleCamera", "DropPlayerAtCamera", "dropCameraAtPlayer",
    "MissionStartPhase1Ack", "MissionStartPhase2Ack", "MissionStartPhase3Ack",
]


@dataclass
class Command:
    name: str
    handler: Callable[[str], Optional[Awaitable]]  # bound method; takes the arg string
    usage: str
    help: str
    aliases: tuple[str, ...] = ()
    free_text: bool = False  # True: the arg is the whole rest of the line (chat)


class Repl:
    """Interactive command loop bound to an :class:`AotClient`."""

    def __init__(self, client, stop: asyncio.Event) -> None:
        self.client = client
        self.stop = stop
        self.commands: dict[str, Command] = {}
        self._by_lookup: dict[str, Command] = {}  # name+aliases -> Command
        self._register_commands()
        self.session: PromptSession = PromptSession(
            completer=_SlashCompleter(self),
            auto_suggest=_SlashAutoSuggest(self),
            complete_while_typing=True,
        )

    # ------------------------------------------------------------------ #
    # Command registry
    # ------------------------------------------------------------------ #

    def _add(self, cmd: Command) -> None:
        self.commands[cmd.name] = cmd
        for key in (cmd.name, *cmd.aliases):
            self._by_lookup[key.lower()] = cmd

    def all_names(self) -> list[str]:
        """Primary names + aliases, for completion."""
        names: set[str] = set()
        for cmd in self.commands.values():
            names.add(cmd.name)
            names.update(cmd.aliases)
        return sorted(names)

    def _register_commands(self) -> None:
        self._add(Command("help", self._c_help, "/help",
                          "List commands.", aliases=("h", "?")))
        self._add(Command("say", self._c_say, "/say <message>",
                          "Send to GLOBAL chat (commandToServer MessageSent).",
                          free_text=True))
        self._add(Command("lsay", self._c_lsay, "/lsay <message>",
                          "Send to LOCAL/proximity chat (commandToServer Talk).",
                          free_text=True))
        self._add(Command("commandtoserver", self._c_cts, "/cts <verb> [args...]",
                          "Send an arbitrary commandToServer(verb, args...).",
                          aliases=("cts", "raw")))
        self._add(Command("login", self._c_login, "/login [user] [pass]",
                          "Log in (defaults to the configured account)."))
        self._add(Command("logout", self._c_logout, "/logout", "Log out."))
        self._add(Command("register", self._c_register, "/register [user] [pass]",
                          "Register a new character (random appearance)."))
        self._add(Command("objects", self._c_objects, "/objects [all] [class]",
                          "List scoped objects (needs AOT_TRACK_OBJECTS=true); "
                          "optional class-name filter, e.g. /objects player.",
                          aliases=("ls", "lsobj")))
        self._add(Command("object", self._c_object, "/object <ghostId>",
                          "Show one object's attributes.", aliases=("obj",)))
        self._add(Command("players", self._c_players, "/players",
                          "List online players (name, region, position, object "
                          "id, joined). Object data needs AOT_TRACK_OBJECTS.",
                          aliases=("who", "pl")))
        self._add(Command("status", self._c_status, "/status",
                          "Show connection / login state."))
        self._add(Command("quit", self._c_quit, "/quit",
                          "Disconnect and exit.", aliases=("exit", "q")))

    # ------------------------------------------------------------------ #
    # Run loop
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        print("Interactive mode — type /help for commands, /quit to exit.")
        with patch_stdout():
            while not self.stop.is_set():
                try:
                    line = await self.session.prompt_async("aot> ")
                except (EOFError, KeyboardInterrupt):
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    await self._dispatch(line)
                except Exception:  # noqa: BLE001 - never let a bad command kill the REPL
                    logger.exception("command error")
        self.stop.set()

    async def _dispatch(self, line: str) -> None:
        if not line.startswith("/"):
            print("commands start with '/'. Try /help.")
            return
        body = line[1:]
        name, _, rest = body.partition(" ")
        cmd = self._by_lookup.get(name.lower())
        if cmd is None:
            print(f"unknown command: /{name} (try /help)")
            return
        result = cmd.handler(rest.strip())
        if asyncio.iscoroutine(result):
            await result

    # ------------------------------------------------------------------ #
    # Handlers
    # ------------------------------------------------------------------ #

    def _c_help(self, arg: str) -> None:
        print("Commands:")
        for cmd in self.commands.values():
            al = f"  (aliases: {', '.join('/' + a for a in cmd.aliases)})" if cmd.aliases else ""
            print(f"  {cmd.usage:<28} {cmd.help}{al}")

    def _c_say(self, arg: str) -> None:
        if not arg:
            print("usage: /say <message>")
            return
        self.client.global_chat(arg)
        print(f"[global] {arg}")

    def _c_lsay(self, arg: str) -> None:
        if not arg:
            print("usage: /lsay <message>")
            return
        self.client.say(arg)
        print(f"[local] {arg}")

    def _c_cts(self, arg: str) -> None:
        if not arg:
            print("usage: /cts <verb> [args...]")
            return
        try:
            parts = shlex.split(arg)
        except ValueError:
            parts = arg.split()
        verb, args = parts[0], parts[1:]
        self.client.command_to_server(verb, *args)
        print(f"-> commandToServer({verb!r}{''.join(', ' + repr(a) for a in args)})")

    def _c_login(self, arg: str) -> None:
        parts = shlex.split(arg) if arg else []
        if len(parts) >= 2:
            self.client.login(parts[0], parts[1])
        else:
            self.client.login()
        print("login sent")

    def _c_logout(self, arg: str) -> None:
        self.client.logout()
        print("logout sent")

    def _c_register(self, arg: str) -> None:
        parts = shlex.split(arg) if arg else []
        if len(parts) >= 2:
            self.client.register_new_user(parts[0], parts[1])
        else:
            self.client.register_new_user()
        print("register (newCharacter) sent")

    def _c_objects(self, arg: str) -> None:
        # /objects [all] [<class-substring>]
        # "all" includes removed (out-of-scope) records; any other token is a
        # case-insensitive class-name filter (e.g. "/objects player" shows only
        # Player/AIPlayer). Both may be combined ("/objects all player").
        tokens = arg.split()
        include_removed = False
        class_filter: str | None = None
        for tok in tokens:
            if tok.lower() in ("all", "1", "true"):
                include_removed = True
            else:
                class_filter = tok.lower()
        objs = self.client.list_objects(include_removed=include_removed)
        if not objs:
            print("(no objects — is AOT_TRACK_OBJECTS=true and the bot logged in?)")
            return
        if class_filter:
            objs = [o for o in objs
                    if class_filter in str(o.get("class_name", "")).lower()]
            if not objs:
                print(f"(no objects matching {class_filter!r})")
                return
        # Surface the control object (the bot's own player) first, then players,
        # then everything else -- the common case is "where are the players".
        def _key(o):
            cn = str(o.get("class_name", "")).lower()
            return (
                0 if o.get("is_control_object") else
                1 if cn in ("player", "aiplayer") else 2,
                str(o.get("class_name", "")),
                o.get("ghost_id", 0),
            )
        objs.sort(key=_key)
        print(f"{len(objs)} object(s):")
        for o in objs[:200]:
            pos = o.get("position")
            pos_s = f"({pos[0]:.0f},{pos[1]:.0f},{pos[2]:.0f})" if pos else "?"
            rot = o.get("rotation")
            rot_s = f" rot={rot}" if rot is not None else ""
            tag = " *self" if o.get("is_control_object") else ""
            print(f"  #{o.get('ghost_id'):<6} {str(o.get('class_name')):<18} "
                  f"{str(o.get('shape_name') or ''):<22} {pos_s}{rot_s}{tag}")

    def _c_object(self, arg: str) -> None:
        if not arg.strip():
            print("usage: /object <ghostId>")
            return
        try:
            gid = int(arg.split()[0])
        except ValueError:
            print("ghostId must be an integer")
            return
        obj = self.client.get_object(gid)
        print(json.dumps(obj, indent=2, default=str) if obj else f"(no object #{gid})")

    def _c_players(self, arg: str) -> None:
        players = self.client.get_players()
        if not players:
            print("(no players — none joined yet, or not loaded in)")
            return
        rows = []
        for p in players:
            name = str(p.get("name") or "")
            tag = p.get("tag") or ""
            if p.get("is_self"):
                tag = (tag + " *me").strip()
            label = f"{name} {tag}".strip()
            location = str(p.get("location") or "—")
            pos = p.get("position")
            pos_s = (f"{pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f}"
                     if pos else "—")
            oid = p.get("object_id")
            oid_s = str(oid) if oid is not None else "—"
            ts = p.get("joined_at")
            try:
                joined = (datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                          if ts else "—")
            except (ValueError, OSError, TypeError):
                joined = "—"
            rows.append((label, location, pos_s, oid_s, joined))

        headers = ("PLAYER", "LOCATION", "POSITION", "OBJ ID", "JOINED")
        widths = [len(h) for h in headers]
        for r in rows:
            for i, cell in enumerate(r):
                widths[i] = max(widths[i], len(cell))

        def fmt(cells):
            return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

        print(f"{len(rows)} player(s) online:")
        print(fmt(headers))
        print(fmt(["-" * w for w in widths]))
        for r in rows:
            print(fmt(r))

    def _c_status(self, arg: str) -> None:
        conn = self.client.conn
        state = conn.state.value if conn is not None else "no-connection"
        print(f"connection: {state} | logged_in: {self.client.logged_in} "
              f"| user: {self.client._login_user!r}")

    async def _c_quit(self, arg: str) -> None:
        print("disconnecting…")
        try:
            await self.client.disconnect("user quit")
        except Exception:  # noqa: BLE001
            pass
        self.stop.set()


# --------------------------------------------------------------------------- #
# Completion + auto-suggestion
# --------------------------------------------------------------------------- #


class _SlashCompleter(Completer):
    """Tab-completes '/command' names and the verb of /cts."""

    def __init__(self, repl: Repl) -> None:
        self.repl = repl

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        body = text[1:]
        if " " not in body:
            # Completing the command name.
            word = body.lower()
            for name in self.repl.all_names():
                if name.startswith(word):
                    yield Completion(name, start_position=-len(body),
                                     display=f"/{name}")
            return
        # Completing args: for /cts/raw complete the (single) verb token.
        name, _, rest = body.partition(" ")
        cmd = self.repl._by_lookup.get(name.lower())
        if cmd is not None and cmd.name == "commandtoserver" and " " not in rest:
            word = rest.lower()
            for verb in KNOWN_VERBS:
                if verb.lower().startswith(word):
                    yield Completion(verb, start_position=-len(rest))


class _SlashAutoSuggest(AutoSuggest):
    """Fish-style inline suggestion: completes the matching command as ghost text."""

    def __init__(self, repl: Repl) -> None:
        self.repl = repl

    def get_suggestion(self, buffer, document):
        text = document.text
        if not text.startswith("/") or " " in text:
            return None
        word = text[1:].lower()
        if not word:
            return None
        for name in self.repl.all_names():
            if name.startswith(word) and name != word:
                return Suggestion(name[len(word):])
        return None


async def run_repl(client, stop: asyncio.Event) -> None:
    """Entry point used by main.py. Runs until /quit, EOF, or ``stop`` is set."""
    await Repl(client, stop).run()
