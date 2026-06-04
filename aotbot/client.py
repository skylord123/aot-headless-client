"""High-level Age of Time client orchestration.

Ties together netconn.py (handshake + packet/notify), events.py
(RemoteCommandEvent + string tables + dispatch), and phases.py (GameConnection
body + mission phases) into a single object that:

* connects + completes the loading sequence (reaching the logged-out in-game
  state via the skip-lighting phase acks);
* exposes :meth:`login`, :meth:`say` (local) / :meth:`global_chat`, and
  :meth:`command_to_server` (raw);
* parses incoming chat exactly like ``base/skylord/helpers/chat.cs`` and emits
  structured ``{scope, name, message, raw}`` via the ``on_chat`` callback;
* forwards server messages and login/connection-state changes via callbacks.

Callbacks (set the attribute, sync or async):
    on_chat(scope, name, msg, raw)
    on_server_message(msg_type, text, raw_args)
    on_login_result(success, detail)
    on_connection_state(state_name)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
from typing import Callable, Optional

from . import protocol_constants as pc
from .config import Config
from .crc import get_string_crc
from .events import EventManager, RemoteCommandEvent
from .netconn import ConnState, NetConnection
from .phases import GameConnectionPhases, AlignmentError
from .playerlist import PlayerListRegistry, match_player_objects
from .transport import UdpTransport

logger = logging.getLogger("aotbot.client")


def _strip_ml_control_chars(s: str) -> str:
    """Strip Torque ML markup control chars (mirrors StripMLControlChars).

    AoT uses bytes < 0x20 (except common whitespace) as ML control prefixes.
    We drop control characters that aren't space; that covers the colour/tag
    codes that would otherwise corrupt the name/message split.
    """
    return "".join(c for c in s if c == " " or ord(c) >= 0x20)


def _format_server_message(template: str, params: list[str]) -> str:
    """Substitute %1..%9 placeholders in a server-message template with the
    trailing args, then strip ML control chars and trim -- mirroring the line the
    engine builds for onServerMessage. ``params[0]`` is %1, ``params[1]`` is %2, …
    Done high-to-low so a longer token isn't clobbered by a shorter one.
    """
    s = template
    for i in range(min(len(params), 9), 0, -1):
        s = s.replace("%" + str(i), params[i - 1])
    return _strip_ml_control_chars(s).strip()


def _should_randomize(value) -> bool:
    """Mirror NewCharacter_shouldRandomize: None/""/-1 means "pick a random value"."""
    return value is None or value == "" or value == -1 or value == "-1"


def _value_or_random_int(value, lo: int, hi: int) -> int:
    if _should_randomize(value):
        return random.randint(lo, hi)
    try:
        return int(str(value).strip())
    except ValueError:
        # Unparseable (e.g. a stray comment) -> randomize rather than crash.
        return random.randint(lo, hi)


def _value_or_random_float(value, lo: float, hi: float) -> float:
    if _should_randomize(value):
        return random.uniform(lo, hi)
    try:
        return float(str(value).strip())
    except ValueError:
        return random.uniform(lo, hi)


def parse_chat_line(line: str) -> dict:
    """Reproduce ``Chat_onChatMessage`` (base/skylord/helpers/chat.cs).

    Returns ``{"scope": "local"|"global", "name": str, "message": str,
    "raw": str}``. Local lines look like ``Name says, "text"``; global like
    ``Name: text``.
    """
    raw = line
    m = _strip_ml_control_chars(line)

    idx_colon = m.find(":")
    idx_comma = m.find(",")
    is_local = (
        (idx_colon > idx_comma and idx_colon >= 0 and idx_comma >= 0)
        or idx_colon <= 0
    )

    if is_local:
        says = m.find(' says, "')
        name = m[:says] if says >= 0 else m
        start = m.find('"')
        if start >= 0:
            tail = m[start + 1:].strip()
            # End = position of the last '"' in the remainder.
            rev = tail[::-1]
            last_q = rev.find('"')
            if last_q >= 0:
                msg = tail[: len(tail) - last_q - 1]
            else:
                msg = tail
        else:
            msg = ""
        scope = "local"
    else:
        name = m[:idx_colon]
        msg = m[idx_colon + 2:]
        scope = "global"

    return {"scope": scope, "name": name.strip(), "message": msg, "raw": raw}


class AotClient:
    """Headless Age of Time client."""

    def __init__(self, config: Config) -> None:
        self.config = config

        self.transport = UdpTransport()
        self.events = EventManager()
        self.phases = GameConnectionPhases(
            self.events,
            skip_lighting=config.aot_skip_lighting,
            track_objects=config.aot_track_objects,
        )
        self.conn: Optional[NetConnection] = None

        # Online-player roster (fed by MsgClientJoin/Drop/ScoreChanged).
        self.players = PlayerListRegistry()

        # Public callbacks (assign directly; may be sync or async).
        self.on_chat: Optional[Callable[[str, str, str, str], None]] = None
        self.on_server_message: Optional[Callable[[str, str, list], None]] = None
        self.on_login_result: Optional[Callable[[bool, str], None]] = None
        self.on_connection_state: Optional[Callable[[str], None]] = None

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._logged_in = False
        self._login_user: Optional[str] = None
        self._login_password: Optional[str] = None
        # New-character registration (auto-create on "Character does not exist!").
        self._register_pending = False
        self._register_name: Optional[str] = None
        self._register_overwrite = False

        # Wire EventManager -> connection send request.
        self.events.request_send = self._request_send

        # Register the chat/server/login clientCmd handlers.
        self._register_event_handlers()
        # On reaching in-game, optionally auto-login.
        self.phases.on_ingame = self._on_ingame
        # Eager login right after Phase2 (mirrors the real client's
        # onPhase1Complete auto-login). The server then answers with
        # clientCmdLoginSuccess (valid) or clientCmdWarningBox (bad creds /
        # missing character) once the full mission load completes.
        self.phases.on_phase2_acked = self._on_phase2_acked

    # ------------------------------------------------------------------ #
    # Setup / teardown
    # ------------------------------------------------------------------ #

    def _request_send(self) -> None:
        if self.conn is not None:
            self.conn.request_send()
            # Flush promptly so commands aren't delayed up to a keepalive tick.
            self.conn.send_data_packet()

    def _register_event_handlers(self) -> None:
        ev = self.events
        ev.on_client_cmd("ChatMessage", self._on_chat_message)
        ev.on_client_cmd("ServerMessage", self._on_server_message)
        ev.on_client_cmd("LoginSuccess", self._on_login_success)
        ev.on_client_cmd("WarningBox", self._on_warning_box)
        ev.on_client_cmd("ConfirmCharacterOverWrite", self._on_confirm_overwrite)
        ev.set_default_handler(self._on_unhandled_cmd)

    async def connect(self, timeout: float = 20.0) -> bool:
        """Bind the socket, run the handshake, and wire the body hooks.

        Returns True once ConnectAccept is received.
        """
        # Fresh session: drop any stale roster from a previous connection (the
        # server re-sends MsgClientJoin for everyone online as we load in).
        self.players.clear()
        await self.transport.open(local_addr=("0.0.0.0", 0))
        server = (self.config.aot_server_host, self.config.aot_server_port)
        # The genuine client sends setConnectArgs($version, $pref::Player::Name).
        # CAPTURE-CONFIRMED: arg1 is the PRE-LOGIN DISPLAY NAME ($pref::Player::Name
        # default "Fresh Meat"), NOT the account username -- the account is
        # supplied later by commandToServer('login', user, crc). Sending the
        # account name here made the server stall at MissionStartPhase1 (no
        # datablocks, no Phase2), so login never started. Use the prefs display
        # name (overridable via AOT_PLAYER_NAME) so the connect handshake mirrors
        # the real client.
        player_name = (
            getattr(self.config, "aot_player_name", None) or pc.DEFAULT_PLAYER_NAME
        )
        self.conn = NetConnection(
            self.transport,
            server,
            join_password="",
            connect_args=[pc.CLIENT_VERSION, player_name],
        )
        # Install body hooks.
        self.conn.write_packet_body = self._write_body
        self.conn.read_packet_body = self._read_body
        self.conn.on_notify = self.events.notify_event_delivered
        self.conn.on_state_change = self._on_conn_state
        # Let netconn answer idle incoming packets with cheap ACKs (no send-window
        # cost) unless we have queued events to deliver.
        self.conn.has_pending_data = self.events.has_pending_events
        return await self.conn.connect(timeout=timeout)

    def _write_body(self, bs) -> None:
        # The send-seq the body rides in is the connection's just-incremented
        # last_send_seq.
        self.phases.write_packet_body(bs, self.conn.last_send_seq)

    def _read_body(self, bs) -> None:
        try:
            self.phases.read_packet_body(bs)
        except AlignmentError as exc:
            # Past this point the bitstream is undecodable; log once per packet.
            logger.warning("packet body alignment limit: %s", exc)
        finally:
            # After each received packet, check whether the ghost-always burst has
            # finished; if so, reply ReadyForNormalGhosts to unblock the server's
            # reliable-event window (the AoT server never sends a post-stream
            # GhostAlwaysDone to a headless client). This is what lets the full
            # load complete -> Phase3 -> MissionStart -> the login response.
            self.phases.maybe_send_ready_for_normal_ghosts()

    async def disconnect(self, reason: str = "Done") -> None:
        if self.conn is not None:
            await self.conn.disconnect(reason)
        self.transport.close()

    async def wait_ingame(self, timeout: Optional[float] = None) -> bool:
        """Wait until we reach the logged-out in-game state."""
        deadline = None if timeout is None else (asyncio.get_event_loop().time() + timeout)
        from .phases import MissionState
        while self.phases.state != MissionState.INGAME_LOGGEDOUT:
            if self.conn is None or self.conn.state != ConnState.CONNECTED:
                return False
            if deadline is not None and asyncio.get_event_loop().time() > deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    # ------------------------------------------------------------------ #
    # Public actions
    # ------------------------------------------------------------------ #

    def command_to_server(self, verb: str, *args) -> None:
        """Send an arbitrary ``commandToServer(verb, *args)``."""
        self.events.command_to_server(verb, *args)

    def list_objects(self, include_removed: bool = False) -> list:
        """Return all currently-scoped game objects (positions/shapes/etc.).

        Empty unless ``AOT_TRACK_OBJECTS`` is enabled. Each entry is a dict with
        ghost_id, class_name, datablock_id, shape_name, position(x,y,z),
        rotation, mount, is_control_object, scoped, age.
        """
        return self.phases.list_objects(include_removed=include_removed)

    def get_object(self, ghost_id: int):
        """Return one scoped object's telemetry record dict, or None."""
        return self.phases.get_object(ghost_id)

    def get_players(self) -> list:
        """Return the online-player roster, each joined to its live ghost object.

        Mirrors the in-game PlayerListGui (everyone the server told us about via
        MsgClientJoin) with each player's world ``location`` (region, e.g. "Port
        Town"), augmented with the matched ``object_id`` / ``position`` (precise
        x,y,z) / full ``object`` -- found by matching the player's name to a
        scoped ghost whose netclass is ``Player`` (not ``AIPlayer``). ``object_id``
        is None when no such ghost is scoped (e.g. the player is out of range).
        ``joined_at`` is a unix timestamp. Object fields need AOT_TRACK_OBJECTS.
        """
        return match_player_objects(self.players.list(), self.list_objects())

    def login(self, user: Optional[str] = None, password: Optional[str] = None) -> None:
        """Send ``commandToServer('login', user, getStringCRC(pass))``."""
        user = user if user is not None else self.config.aot_username
        password = password if password is not None else self.config.aot_password
        self._login_user = user
        self._login_password = password  # kept for the auto-create path
        crc = get_string_crc(password)
        logger.info("logging in as %r (pass crc=%d)", user, crc)
        self.events.command_to_server("login", user, crc)

    def logout(self) -> None:
        self.events.command_to_server("logout")
        self._logged_in = False

    def say(self, text: str) -> None:
        """Local/proximity chat: ``commandToServer('Talk', text)``."""
        self.events.command_to_server("Talk", text)

    def global_chat(self, text: str) -> None:
        """Global chat: ``commandToServer('MessageSent', text)``."""
        self.events.command_to_server("MessageSent", text)

    def register_new_user(
        self,
        name: Optional[str] = None,
        password: Optional[str] = None,
        *,
        overwrite: Optional[bool] = None,
        abilities: Optional[str] = None,
        gender=None, posture=None, chest=None,
        x_scale=None, y_scale=None, z_scale=None,
        skin_tone=None, lip_tone=None, hair_style=None, hair_color=None,
        eye_color=None, face=None, ears=None, glasses=None,
    ) -> None:
        """Register a new character via ``commandToServer('newCharacter', ...)``.

        Mirrors the in-game ``registerNewUser()`` / ``CreateCharacterGui`` arg
        order. Any appearance/ability field left as ``None`` (or ``""``/``-1``) is
        randomized to a valid value, exactly like the in-game helper. Defaults for
        name/password/overwrite/abilities come from the pending login + config, so
        the auto-create path just calls ``register_new_user()`` with no args.

        Ranges: gender 0-1, posture/chest 0.0-1.0, x/y/zScale 0.9-1.1, skinTone
        0-9, lipTone skinTone-9, hairStyle 0-2, hairColor 0-4, eyeColor 0-3, face
        0-1, ears 0-1, glasses 0-1. abilities = 7 space-separated 1-10 values.
        """
        name = name or self._login_user or self.config.aot_username
        password = (
            password if password is not None
            else (self._login_password or self.config.aot_password)
        )
        overwrite = self.config.aot_create_overwrite if overwrite is None else overwrite
        abilities = abilities or self.config.aot_create_abilities or "1 1 1 1 1 1 1"

        # For each appearance field: an explicit arg wins; otherwise fall back to
        # the configured AOT_CREATE_* value (a raw string, possibly "" or "-1").
        # _value_or_random_* then treats None/""/-1 as "randomize", so an unset
        # config field becomes a valid random value -- exactly like env.cs.
        cfg = self.config
        _d = lambda v, c: c if v is None else v  # noqa: E731
        gender = _value_or_random_int(_d(gender, cfg.aot_create_gender), 0, 1)
        posture = _value_or_random_float(_d(posture, cfg.aot_create_posture), 0.0, 1.0)
        chest = _value_or_random_float(_d(chest, cfg.aot_create_chest), 0.0, 1.0)
        x_scale = _value_or_random_float(_d(x_scale, cfg.aot_create_x_scale), 0.9, 1.1)
        y_scale = _value_or_random_float(_d(y_scale, cfg.aot_create_y_scale), 0.9, 1.1)
        z_scale = _value_or_random_float(_d(z_scale, cfg.aot_create_z_scale), 0.9, 1.1)
        skin_tone = _value_or_random_int(_d(skin_tone, cfg.aot_create_skin_tone), 0, 9)
        # lipTone >= skinTone (matches the GUI / NewCharacter_valueOrRandomInt).
        lip_tone = _value_or_random_int(_d(lip_tone, cfg.aot_create_lip_tone), skin_tone, 9)
        hair_style = _value_or_random_int(_d(hair_style, cfg.aot_create_hair_style), 0, 2)
        hair_color = _value_or_random_int(_d(hair_color, cfg.aot_create_hair_color), 0, 4)
        eye_color = _value_or_random_int(_d(eye_color, cfg.aot_create_eye_color), 0, 3)
        face = _value_or_random_int(_d(face, cfg.aot_create_face), 0, 1)
        ears = _value_or_random_int(_d(ears, cfg.aot_create_ears), 0, 1)
        glasses = _value_or_random_int(_d(glasses, cfg.aot_create_glasses), 0, 1)

        self._register_pending = True
        self._register_name = name
        self._register_overwrite = bool(overwrite)
        # The post-create "<name> logged in." broadcast should match this name.
        self._login_user = name

        crc = get_string_crc(password)
        ow = 1 if overwrite else 0

        def _f(x: float) -> str:
            return f"{float(x):.6f}"

        logger.info("registering new character %r (overwrite=%d)", name, ow)
        self.events.command_to_server(
            "newCharacter", name, crc,
            gender, _f(posture), _f(chest), _f(x_scale), _f(y_scale), _f(z_scale),
            skin_tone, lip_tone, hair_style, hair_color, eye_color, face, ears,
            glasses, abilities, ow,
        )

    # ------------------------------------------------------------------ #
    # Incoming clientCmd* handlers
    # ------------------------------------------------------------------ #

    def _on_chat_message(self, args: list[str], evt: RemoteCommandEvent) -> None:
        # clientCmdChatMessage(%sender, %voice, %pitch, %msgString, ...) -- stock
        # Torque layout. The formatted HUD line we parse is %msgString = args[3]
        # (args[0] is the sender client/ghost id). This is the same string the
        # engine passes to onChatMessage(), which the in-game bot's
        # Chat_onChatMessage parses.
        line = args[3] if len(args) > 3 else (args[-1] if args else "")
        parsed = parse_chat_line(line)
        logger.info(
            "chat[%s] %s: %s", parsed["scope"], parsed["name"], parsed["message"]
        )
        self._emit(
            self.on_chat,
            parsed["scope"], parsed["name"], parsed["message"], parsed["raw"],
        )

    def _on_server_message(self, args: list[str], evt: RemoteCommandEvent) -> None:
        # clientCmdServerMessage(%msgType, %msgString, %a1..%a10): %msgString is a
        # template with %1..%9 placeholders that the client substitutes with the
        # trailing args (%1 -> %a1 -> args[2], %2 -> args[3], ...), then strips ML
        # control chars. This yields the same formatted line the in-game bot sees
        # via onServerMessage (StripMLControlChars(trim(detag(%x)))).
        msg_type = args[0] if len(args) > 0 else ""
        template = args[1] if len(args) > 1 else ""
        extra = args[2:]
        # Player-roster messages (MsgClientJoin/Drop/ScoreChanged) drive the
        # online-player list exactly like playerList.cs's addMessageCallback
        # handlers. The raw (de-tagged) args feed the roster; we still format and
        # forward the human line below like any other server message.
        self.players.handle_server_message(msg_type, extra)
        text = _format_server_message(template, extra)
        logger.info("server message [%s]: %s", msg_type, text)
        self._emit(self.on_server_message, msg_type, text, extra)
        # New-character creation confirmation: "New character created: <name>".
        # Mirrors NewCharacter_onServerMessage + botCreateUserSuccess (which marks
        # us logged in); the server logs us in as part of creation.
        if (
            self._register_pending
            and self._register_name
            and text.strip() == f"New character created: {self._register_name}"
        ):
            logger.info("new character created: %s", self._register_name)
            self._register_pending = False
            self._mark_logged_in("new character created")
            return
        # The login confirmation arrives here as "<user> logged in.".
        if (
            not self._logged_in
            and self._login_user
            and text.strip() == f"{self._login_user} logged in."
        ):
            self._mark_logged_in("server message broadcast")

    def _on_login_success(self, args: list[str], evt: RemoteCommandEvent) -> None:
        logger.info("clientCmdLoginSuccess")
        self._mark_logged_in("clientCmdLoginSuccess")

    def _on_warning_box(self, args: list[str], evt: RemoteCommandEvent) -> None:
        warn_text = args[0].strip() if args else ""
        # A warning while a newCharacter is pending = registration failure.
        if self._register_pending:
            logger.error("new character failed: %s", warn_text)
            self._register_pending = False
            self._emit(self.on_login_result, False, "create failed: " + warn_text)
            return
        logger.warning("login/warning box: %s", warn_text)
        # Auto-create the character on "Character does not exist!" (mirrors the
        # in-game botLoginFailure -> registerNewUser path) when configured.
        if (
            not self._logged_in
            and warn_text == "Character does not exist!"
            and self.config.aot_create_user
            and (self._login_user or self.config.aot_username)
        ):
            logger.info(
                "character does not exist; auto-creating %r",
                self._login_user or self.config.aot_username,
            )
            self.register_new_user()
            return
        if not self._logged_in:
            self._emit(self.on_login_result, False, warn_text)

    def _on_confirm_overwrite(self, args: list[str], evt: RemoteCommandEvent) -> None:
        # clientCmdConfirmCharacterOverWrite: server asks to confirm replacing an
        # existing character. Mirror NewCharacter_onConfirmOverwrite: we never send
        # the confirm, so this is always a registration failure here.
        if not self._register_pending:
            return
        self._register_pending = False
        if self._register_overwrite:
            msg = "Overwrite requested but the server still asked for confirmation."
        else:
            msg = "Character exists. Set AOT_CREATE_OVERWRITE=true to replace it."
        logger.error("new character: %s", msg)
        self._emit(self.on_login_result, False, msg)

    def _on_unhandled_cmd(self, verb: str, args: list[str], evt: RemoteCommandEvent) -> None:
        logger.debug("unhandled clientCmd%s(%s)", verb, args)

    def _mark_logged_in(self, detail: str) -> None:
        if self._logged_in:
            return
        self._logged_in = True
        logger.info("LOGGED IN (%s)", detail)
        self._emit(self.on_login_result, True, detail)

    # ------------------------------------------------------------------ #
    # State plumbing
    # ------------------------------------------------------------------ #

    def _on_phase2_acked(self) -> None:
        # Eager auto-login after Phase2 (before MissionStart), mirroring the real
        # AoT client. Login is idempotent server-side here; the result arrives
        # later via clientCmdLoginSuccess / clientCmdWarningBox.
        logger.info("Phase2 acked -> eager login")
        self._emit(self.on_connection_state, "ingame_loggedout")
        if self.config.aot_username and self.config.aot_password:
            self.login()

    def _on_ingame(self) -> None:
        logger.info("reached in-game logged-out state")
        self._emit(self.on_connection_state, "ingame_loggedout")
        # Auto-login if credentials look real and we didn't already (eager path
        # at Phase2). Re-login here is harmless and matches the real client which
        # also re-sends login at MissionStart.
        if (
            not self._logged_in
            and self.config.aot_username
            and self.config.aot_password
        ):
            self.login()

    def _on_conn_state(self, state: ConnState) -> None:
        self._emit(self.on_connection_state, state.value)

    def _emit(self, cb, *args) -> None:
        if cb is None:
            return
        try:
            result = cb(*args)
            if inspect.isawaitable(result):
                asyncio.ensure_future(result)
        except Exception:
            logger.exception("client callback raised")

    @property
    def logged_in(self) -> bool:
        return self._logged_in
