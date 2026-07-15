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
    on_connection_state(state, logged_in)
    on_sync_clock(uptime_seconds, received_at)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from typing import Callable, Optional

from . import protocol_constants as pc
from .config import Config
from .crc import get_string_crc
from .events import EventManager, RemoteCommandEvent
from .masterserver import MasterServerError, fetch_server_host
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


def _substitute_placeholders(template: str, params: list[str]) -> str:
    """Replace %1..%9 placeholders in a template with the trailing args.

    ``params[0]`` is %1, ``params[1]`` is %2, … Done high-to-low so a longer
    token isn't clobbered by a shorter one. This is the substitution the engine
    performs for both ``clientCmdServerMessage`` and ``clientCmdChatMessage``.
    """
    s = template
    for i in range(min(len(params), 9), 0, -1):
        s = s.replace("%" + str(i), params[i - 1])
    return s


def _format_server_message(template: str, params: list[str]) -> str:
    """Substitute %1..%9 placeholders in a server-message template with the
    trailing args, then strip ML control chars and trim -- mirroring the line the
    engine builds for onServerMessage.
    """
    return _strip_ml_control_chars(_substitute_placeholders(template, params)).strip()


def _parse_client_id(value) -> Optional[int]:
    """Parse a MsgClient* ``clientId`` arg to int; ``None`` if missing/unparseable."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


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

        # --- Persistent state (survives reconnects) ---------------------- #
        # The UDP transport is reused across reconnects: connect() reopens it and
        # disconnect() closes it, so a single instance handles every attempt.
        self.transport = UdpTransport()
        # Host resolved from the master server (when AOT_SERVER_HOST is empty),
        # cached so reconnects don't re-fetch.
        self._resolved_host: Optional[str] = None

        # Public callbacks (assign directly; may be sync or async). These are set
        # by the owner (main.py / REPL) and MUST survive reset()/reconnect.
        self.on_chat: Optional[Callable[[str, str, str, str], None]] = None
        self.on_server_message: Optional[Callable[[str, str, list], None]] = None
        # Player roster changes (parsed from MsgClientJoin / MsgClientDrop).
        # associated_usernames is every real character name this client_id has
        # used this session (logged-out placeholders excluded).
        #   on_player_joined(name, client_id, location, message, associated_usernames)
        #   on_player_dropped(name, client_id, message, associated_usernames)
        self.on_player_joined: Optional[
            Callable[[str, Optional[int], str, str, list], None]
        ] = None
        self.on_player_dropped: Optional[
            Callable[[str, Optional[int], str, list], None]
        ] = None
        # A player changed world zone/region (parsed from MsgClientScoreChanged).
        #   on_zone_change(player, zone, client_id)
        self.on_zone_change: Optional[
            Callable[[str, str, Optional[int]], None]
        ] = None
        self.on_login_result: Optional[Callable[[bool, str], None]] = None
        # Connection-status change: emitted on every change to the connection
        # lifecycle state OR the login flag (deduplicated). See connection_status().
        #   on_connection_state(state: str, logged_in: bool)
        self.on_connection_state: Optional[Callable[[str, bool], None]] = None
        # Server clock sync (clientCmdSyncClock): the server reports its uptime in
        # seconds on connect. NOTE the value is derived from simtime and thus
        # tied to CPU speed, so it's approximate -- its main use is detecting a
        # server restart (a sudden drop below the previous value).
        #   on_sync_clock(uptime_seconds: float, received_at: float)
        self.on_sync_clock: Optional[Callable[[float, float], None]] = None

        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Last server clock sync received (see _on_sync_clock). Kept here (not in
        # the per-connection stack) so a client can query the most recent value
        # even across reconnects; it's refreshed on every connect.
        self._last_sync_clock: Optional[dict] = None

        # --- Per-connection protocol stack (rebuilt on each reconnect) ---- #
        self._build_protocol_stack()

    def _build_protocol_stack(self) -> None:
        """Construct the per-connection decode/event state.

        Called from ``__init__`` and again from :meth:`reset` before a
        reconnect, so a dropped connection's stale sequence numbers, mission
        phase, roster, and login flags never leak into the next attempt. The
        persistent public callbacks (assigned in ``__init__``) are left intact.
        """
        self.events = EventManager()
        self.phases = GameConnectionPhases(
            self.events,
            skip_lighting=self.config.aot_skip_lighting,
            track_objects=self.config.aot_track_objects,
        )
        self.conn: Optional[NetConnection] = None

        # Online-player roster (fed by MsgClientJoin/Drop/ScoreChanged).
        self.players = PlayerListRegistry()

        self._logged_in = False
        self._login_user: Optional[str] = None
        self._login_password: Optional[str] = None
        # New-character registration (auto-create on "Character does not exist!").
        self._register_pending = False
        self._register_name: Optional[str] = None
        self._register_overwrite = False
        # Connection-status tracking. _conn_state is the current lifecycle string
        # (see connection_status); _last_connection_emit dedupes the callback so
        # we only fire on a genuine (state, logged_in) change.
        self._conn_state = "disconnected"
        self._last_connection_emit: Optional[tuple] = None
        # Set once a fatal event-stream desync starts teardown, so the packets
        # still arriving mid-teardown don't spawn duplicate disconnect tasks.
        # Rebuilt (False) on every reset()/reconnect.
        self._desync_abort_started = False
        # True while auto-login is waiting for our username to free up: after a
        # crash+reconnect the PREVIOUS session's character can still be in-world
        # (the server hasn't timed the dead connection out yet), so logging in
        # would be rejected. We stay logged out and retry on every roster
        # change until the ghost session drops. Mirrors the in-game bot's
        # autoBotLogin() isInList(user, getPlayerNames(true)) wait loop
        # (base/skylord/bot/login.cs).
        self._login_deferred = False

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

    def reset(self) -> None:
        """Discard the previous connection's state so this client can reconnect.

        Rebuilds the protocol stack in place (same ``AotClient`` instance), so
        callers holding a reference — main.py's callback wiring, the REPL — keep
        working across reconnects. Call this between a drop and the next
        :meth:`connect`.
        """
        self._build_protocol_stack()

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
        ev.on_client_cmd("SyncClock", self._on_sync_clock)
        ev.set_default_handler(self._on_unhandled_cmd)

    async def connect(self, timeout: float = 20.0) -> bool:
        """Bind the socket, run the handshake, and wire the body hooks.

        Returns True once ConnectAccept is received.
        """
        # Fresh session: drop any stale roster from a previous connection (the
        # server re-sends MsgClientJoin for everyone online as we load in).
        self.players.clear()
        # Announce the attempt before the (potentially slow) host resolution +
        # handshake, so consumers see `connecting` immediately.
        self._set_connection("connecting")
        # Resolve the server host from the master server if none was configured.
        try:
            host = await self._resolve_server_host()
        except MasterServerError as exc:
            logger.error("%s", exc)
            return False
        await self.transport.open(local_addr=("0.0.0.0", 0))
        server = (host, self.config.aot_server_port)
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

    async def _resolve_server_host(self) -> str:
        """Return the configured server host, or resolve it from the master
        server list when ``AOT_SERVER_HOST`` is empty. Raises MasterServerError
        on failure. The resolved host is cached for subsequent reconnects."""
        if self.config.aot_server_host:
            return self.config.aot_server_host
        if self._resolved_host:
            return self._resolved_host
        loop = asyncio.get_running_loop()
        host = await loop.run_in_executor(
            None, lambda: fetch_server_host(self.config.aot_master_url)
        )
        self._resolved_host = host
        return host

    def _write_body(self, bs) -> None:
        # The send-seq the body rides in is the connection's just-incremented
        # last_send_seq.
        self.phases.write_packet_body(bs, self.conn.last_send_seq)

    def _read_body(self, bs) -> None:
        try:
            self.phases.read_packet_body(bs)
        except AlignmentError as exc:
            if exc.fatal:
                # The guaranteed-ordered event stream lost data the server
                # believes was delivered (the packet is ACKed regardless).
                # From here the string table / ghost table / phase state
                # silently diverge and every later "decoded" event is suspect
                # garbage -- the connection looks alive but chat/roster events
                # stop flowing. Drop it so auto-reconnect builds a fresh
                # session instead of zombie-ing.
                self._on_fatal_desync(exc)
            else:
                # Ghost-section loss: the event section was already consumed,
                # so only this packet's ghost updates are dropped. Log and
                # carry on.
                logger.warning("packet body alignment limit: %s", exc)
        finally:
            # After each received packet, check whether the ghost-always burst has
            # finished; if so, reply ReadyForNormalGhosts to unblock the server's
            # reliable-event window (the AoT server never sends a post-stream
            # GhostAlwaysDone to a headless client). This is what lets the full
            # load complete -> Phase3 -> MissionStart -> the login response.
            self.phases.maybe_send_ready_for_normal_ghosts()

    def _on_fatal_desync(self, exc: AlignmentError) -> None:
        """Tear the connection down after an unrecoverable event-stream desync.

        Called from inside the receive path (sync context), so the actual
        disconnect runs as a task; main.py's run loop then sees is_connected go
        false and reconnects (when AUTO_RECONNECT is on). Guarded so the
        packets that keep arriving while teardown is in flight don't spawn
        duplicate disconnects.
        """
        if self._desync_abort_started:
            return
        self._desync_abort_started = True
        logger.error(
            "unrecoverable event-stream desync (%s); "
            "dropping connection to force a clean reconnect",
            exc,
        )
        if self.conn is not None:
            asyncio.create_task(self.conn.disconnect("event stream desync"))

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

    def username_online(self, user: Optional[str] = None) -> bool:
        """True when a roster entry is CURRENTLY using ``user`` as its name.

        Matches the in-game bot's pre-login check (login.cs autoBotLogin ->
        isInList(user, getPlayerNames(true))): current names only -- a client
        that used the name earlier but has since logged out does not hold it --
        with the ``<Logged Out>``/``<Connecting>`` placeholders skipped, and
        case-insensitively (Torque name matching is case-insensitive).
        """
        u = (user if user is not None else self.config.aot_username).strip().lower()
        if not u:
            return False
        for p in self.players.list():
            name = p.name.strip()
            if name.startswith("<"):
                continue  # logged-out/connecting placeholder, not a held name
            if name.lower() == u:
                return True
        return False

    def _maybe_auto_login(self) -> None:
        """Auto-login -- unless our username is already in-world.

        If a previous session's character is still online (crashed bot whose
        connection the server hasn't dropped yet), stay logged out and mark the
        login deferred; every subsequent roster change re-runs this until the
        name frees up.
        """
        if self._logged_in:
            return
        user = self.config.aot_username
        if not (user and self.config.aot_password):
            return
        if self.username_online(user):
            if not self._login_deferred:
                logger.warning(
                    "%r is already online (previous session still connected?); "
                    "deferring login until it drops", user,
                )
                self._login_deferred = True
            return
        if self._login_deferred:
            self._login_deferred = False
            logger.info("%r is no longer online -> logging in now", user)
        self.login()

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
        self._set_connection()

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
        # clientCmdChatMessage(%sender, %voice, %pitch, %msgString, %a1..%a10) --
        # stock Torque layout. %msgString = args[3] is a TEMPLATE with %1..%9
        # placeholders (e.g. "%1: %2" for global, '%1 says, "%2"' for local); the
        # trailing args[4:] are the substitution params (%1 -> args[4], ...), just
        # like clientCmdServerMessage. We substitute first, then parse the
        # resulting HUD line exactly as the in-game bot's Chat_onChatMessage does.
        # (A single pre-formatted arg, with no params, passes through unchanged.)
        logger.debug("ChatMessage raw args: %r", args)
        if len(args) > 3:
            template, params = args[3], args[4:]
        else:
            template, params = (args[-1] if args else ""), []
        line = _substitute_placeholders(template, params)
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
        tag = msg_type.split(" ", 1)[0] if msg_type else ""

        # On a drop the registry removes the entry, so capture its accumulated
        # username history BEFORE updating the roster (so the player_dropped
        # event can still report every character that client_id ever used).
        dropped_usernames: list[str] = []
        if tag == "MsgClientDrop":
            drop_id = _parse_client_id(extra[1] if len(extra) > 1 else None)
            if drop_id is not None:
                existing = self.players.get(drop_id)
                if existing is not None:
                    dropped_usernames = list(existing.associated_usernames)

        # Player-roster messages (MsgClientJoin/Drop/ScoreChanged) drive the
        # online-player list exactly like playerList.cs's addMessageCallback
        # handlers. The raw (de-tagged) args feed the roster.
        self.players.handle_server_message(msg_type, extra)

        # The human-readable chat-HUD line. The engine fans every server message
        # out to the default message callback -> onServerMessage(detag(msg)),
        # which only does ChatHud.addLine() when the line has word content
        # (getWordCount). So most tagged control messages (MsgClientJoin/Drop/
        # ScoreChanged) carry an EMPTY msgString and never reach the HUD. We
        # mirror that: a `server_message` event is emitted ONLY for non-empty text
        # (see base/client/message.cs + chatHud.cs onServerMessage).
        text = _format_server_message(template, extra)

        # Structured player join/drop events, parsed from the tagged-message args
        # the same way playerList.cs handleClientJoin/handleClientDrop do. The
        # client name is the first trailing arg (StripMLControlChars(detag(...)));
        # MsgClientJoin also carries clientId/location. NOTE: the server re-sends
        # MsgClientJoin for everyone already online when WE connect (roster sync),
        # and for placeholder states like "<Connecting>"/"<Logged Out>", so these
        # events represent roster changes, not strictly brand-new logins.
        if tag == "MsgClientJoin":
            name = _strip_ml_control_chars(str(extra[0])).strip() if extra else ""
            client_id = _parse_client_id(extra[1] if len(extra) > 1 else None)
            location = (
                _strip_ml_control_chars(str(extra[3])).strip()
                if len(extra) > 3 else ""
            )
            # The roster (updated above) now holds the full username history.
            info = self.players.get(client_id) if client_id is not None else None
            usernames = list(info.associated_usernames) if info is not None else []
            logger.info(
                "player joined: %s (id=%s, loc=%s, usernames=%s)",
                name, client_id, location, usernames,
            )
            self._emit(
                self.on_player_joined, name, client_id, location, text, usernames
            )
        elif tag == "MsgClientDrop":
            name = _strip_ml_control_chars(str(extra[0])).strip() if extra else ""
            client_id = _parse_client_id(extra[1] if len(extra) > 1 else None)
            logger.info(
                "player dropped: %s (id=%s, usernames=%s)",
                name, client_id, dropped_usernames,
            )
            self._emit(
                self.on_player_dropped, name, client_id, text, dropped_usernames
            )
        elif tag == "MsgClientScoreChanged":
            # MsgClientScoreChanged(zone, clientId): despite the name, this AoT
            # server repurposes the "score" message as a world-zone change. We
            # mirror NodeRED.cs playerTrackerClientZoneChange: resolve the player
            # name from the roster (by clientId), skip logged-out/connecting
            # placeholders, and emit a zone_change. extra[0]=zone, extra[1]=id.
            zone = _strip_ml_control_chars(str(extra[0])).strip() if extra else ""
            client_id = _parse_client_id(extra[1] if len(extra) > 1 else None)
            info = self.players.get(client_id) if client_id is not None else None
            player = info.name if info is not None else ""
            if player and not player.startswith("<"):
                logger.info(
                    "zone change: %s entered %s (id=%s)", player, zone, client_id
                )
                self._emit(self.on_zone_change, player, zone, client_id)

        if text:
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
            return

        # A deferred auto-login retries on every roster change: the ghost
        # session holding our name can free it by dropping (MsgClientDrop) or
        # by re-logging as another character (a MsgClientJoin rename).
        if self._login_deferred and tag in ("MsgClientJoin", "MsgClientDrop"):
            self._maybe_auto_login()

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
        # Server-side rejection because the character is in use (we can race
        # the roster sync: our check saw the name free but the ghost session
        # was still connected server-side). Arm the deferred-login wait so the
        # next roster drop retries automatically.
        if not self._logged_in and "already logged in" in warn_text.lower():
            logger.warning(
                "server says the character is already logged in; "
                "deferring login until it drops"
            )
            self._login_deferred = True
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

    def _on_sync_clock(self, args: list[str], evt: RemoteCommandEvent) -> None:
        # clientCmdSyncClock(%time): the server reports its uptime in seconds
        # (see ageoftime-bot base/skylord/serverTime.cs). Mirror that package by
        # capturing the value alongside the local receive time. The uptime is
        # derived from simtime (CPU-speed dependent), so it's only approximate --
        # a sudden drop below the previous value signals a server restart.
        raw = args[0] if args else ""
        try:
            uptime_seconds = float(raw)
        except (ValueError, TypeError):
            logger.warning("clientCmdSyncClock: unparseable uptime %r", raw)
            return
        received_at = time.time()
        logger.info("server clock sync: uptime=%.0fs", uptime_seconds)
        # Remember the latest value so it can be queried on demand.
        self._last_sync_clock = {
            "uptime_seconds": uptime_seconds,
            "received_at": received_at,
        }
        self._emit(self.on_sync_clock, uptime_seconds, received_at)

    def sync_clock_status(self) -> dict:
        """The most recent server clock sync, or nulls if none received yet.

        Shape matches the pushed ``sync_clock`` event
        (``uptime_seconds`` + ``received_at``). See :meth:`_on_sync_clock`.
        """
        if self._last_sync_clock is None:
            return {"uptime_seconds": None, "received_at": None}
        return dict(self._last_sync_clock)

    def _on_unhandled_cmd(self, verb: str, args: list[str], evt: RemoteCommandEvent) -> None:
        logger.debug("unhandled clientCmd%s(%s)", verb, args)

    def _mark_logged_in(self, detail: str) -> None:
        if self._logged_in:
            return
        self._logged_in = True
        self._login_deferred = False
        logger.info("LOGGED IN (%s)", detail)
        # Detach the camera at our player: the server then ghosts us ALL
        # objects regardless of distance (mirrors cameraHack.cs
        # startCameraFly's commandToServer('dropCameraAtPlayer')), so the
        # roster/object telemetry sees the whole world, not just our scope
        # bubble.
        logger.info("requesting whole-world scope (dropCameraAtPlayer)")
        self.events.command_to_server("dropCameraAtPlayer")
        self._emit(self.on_login_result, True, detail)
        # Login flag flipped -> emit an updated connection status.
        self._set_connection()

    # ------------------------------------------------------------------ #
    # State plumbing
    # ------------------------------------------------------------------ #

    def _on_phase2_acked(self) -> None:
        # Eager auto-login after Phase2 (before MissionStart), mirroring the real
        # AoT client. Login is idempotent server-side here; the result arrives
        # later via clientCmdLoginSuccess / clientCmdWarningBox.
        logger.info("Phase2 acked -> eager login")
        self._set_connection("ingame_loggedout")
        self._maybe_auto_login()

    def _on_ingame(self) -> None:
        logger.info("reached in-game logged-out state")
        self._set_connection("ingame_loggedout")
        # Auto-login if credentials look real and we didn't already (eager path
        # at Phase2). Re-login here is harmless and matches the real client which
        # also re-sends login at MissionStart. Held while our username is still
        # in-world from a previous session (see _maybe_auto_login).
        self._maybe_auto_login()

    def _on_conn_state(self, state: ConnState) -> None:
        # A terminal transport state means we're no longer logged in; clear the
        # flag first so the emitted status reports logged_in=false.
        if state in (ConnState.DISCONNECTED, ConnState.TIMED_OUT, ConnState.REJECTED):
            self._logged_in = False
        self._set_connection(state.value)

    def _set_connection(self, state: Optional[str] = None) -> None:
        """Update the connection-status snapshot and emit on any real change.

        ``state`` updates the lifecycle string (left unchanged when ``None``, so
        a login-flag flip alone still emits). The callback fires only when the
        (state, logged_in) pair differs from the last emitted one.
        """
        if state is not None:
            self._conn_state = state
        key = (self._conn_state, self._logged_in)
        if key == self._last_connection_emit:
            return
        self._last_connection_emit = key
        self._emit(self.on_connection_state, self._conn_state, self._logged_in)

    def mark_reconnecting(self) -> None:
        """Report the ``reconnecting`` lifecycle state.

        Used by the auto-reconnect loop while it waits between attempts, so
        consumers can distinguish "waiting to retry" from a plain disconnect.
        """
        self._logged_in = False
        self._set_connection("reconnecting")

    def connection_status(self) -> dict:
        """Current connection status: lifecycle ``state`` + ``logged_in`` flag.

        This is the same shape pushed via ``on_connection_state`` and is used to
        answer on-demand ``connection_state`` queries from the bridges.
        """
        return {"state": self._conn_state, "logged_in": self._logged_in}

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
