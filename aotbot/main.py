"""Entry point for the Age of Time bot.

Wave 1 (this scaffold) loads configuration, sets up logging, and logs the
effective (password-redacted) config. The async glue that wires the protocol
client to the Node-RED bridge is stubbed with a clearly marked TODO and will be
filled in once the protocol modules land. This module runs cleanly today even
though those modules do not exist yet.

Run with: ``python -m aotbot.main`` or the installed ``aotbot`` console script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys

from .config import Config, ConfigError
from .logging_setup import setup_logging

log = logging.getLogger("aotbot.main")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aotbot",
        description="Headless Age of Time (Torque) protocol bot.",
    )
    # CLI overrides take precedence over .env / environment. Defaults are None so
    # unset flags fall through to the environment in Config.load(overrides=...).
    parser.add_argument("--host", dest="aot_server_host", default=None,
                        help="AoT server host/IP (overrides AOT_SERVER_HOST).")
    parser.add_argument("--port", dest="aot_server_port", type=int, default=None,
                        help="AoT server UDP port (overrides AOT_SERVER_PORT).")
    parser.add_argument("--username", dest="aot_username", default=None,
                        help="Account username (overrides AOT_USERNAME).")
    parser.add_argument("--log-level", dest="log_level", default=None,
                        help="Log level (overrides LOG_LEVEL).")
    parser.add_argument("--dump-packets", dest="dump_packets",
                        action="store_true", default=None,
                        help="Hexdump packets (overrides DUMP_PACKETS).")
    parser.add_argument("--env-file", dest="env_file", default=None,
                        help="Path to a .env file to load.")
    parser.add_argument("-i", "--interactive", dest="interactive",
                        action="store_true", default=None,
                        help="Force the interactive REPL (default: on when "
                             "stdin is a TTY).")
    parser.add_argument("--no-interactive", dest="interactive",
                        action="store_false",
                        help="Disable the interactive REPL.")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> Config:
    overrides = {
        "aot_server_host": args.aot_server_host,
        "aot_server_port": args.aot_server_port,
        "aot_username": args.aot_username,
        "log_level": args.log_level.lower() if args.log_level else None,
        "dump_packets": args.dump_packets,
    }
    return Config.load(dotenv_path=args.env_file, overrides=overrides)


def run(config: Config, interactive: bool = False) -> int:
    """Top-level run loop: connect, load, login, bridge chat to Node-RED."""
    log.info("aotbot starting with config: %s", config.redacted())
    try:
        asyncio.run(_main(config, interactive))
    except KeyboardInterrupt:
        log.info("interrupted; shutting down")
    return 0


def _has_credentials(config: Config) -> bool:
    return bool(config.aot_username) and bool(config.aot_password)


async def _main(config: Config, interactive: bool = False) -> None:
    from .client import AotClient
    from .nodered import NodeRedBridge
    from .websocket import WebSocketServer

    client = AotClient(config)
    loop = asyncio.get_running_loop()

    # ---- transports: either, both, or neither --------------------------- #
    # The bot can bridge to Node-RED (TCP client) and/or host a WebSocket
    # server. Each is opt-in via config; whichever are enabled receive the same
    # outbound events and feed the same inbound action handlers.
    bridge: Optional[NodeRedBridge] = None
    server: Optional[WebSocketServer] = None
    # Outbound sinks: async callables taking the JSON object to deliver.
    sinks: list[Callable[[dict], Awaitable]] = []

    if config.nodered_enabled:
        bridge = NodeRedBridge(config.nodered_host, config.nodered_port)
        sinks.append(lambda obj: bridge.send(json.dumps(obj)))
    if config.websocket_enabled:
        server = WebSocketServer(config.websocket_host, config.websocket_port)
        sinks.append(lambda obj: server.send(obj))

    if not sinks:
        log.warning(
            "no Node-RED or WebSocket transport configured; running with no "
            "external bridge (set NODERED_HOST/NODERED_PORT and/or WEBSOCKET_PORT)"
        )

    # ---- outbound: client events -> all transports ---------------------- #
    # Payloads are JSON objects with an "action" field, matching the in-game
    # Torque bot's Node-RED protocol (base/skylord/bot/NodeRED.cs:
    # jettisonStringify of a JettisonObject). Each enabled transport receives
    # the identical object (Node-RED gets it JSON-encoded; WebSocket clients get
    # the same object framed). See docs/nodered-protocol.md & websocket-protocol.md.
    def forward(obj: dict) -> None:
        for sink in sinks:
            asyncio.ensure_future(sink(obj))

    def on_chat(scope: str, name: str, message: str, raw: str) -> None:
        # Mirror TrackPlayerMessage(): action=player_message, isLocal, name, message.
        forward({
            "action": "player_message",
            "isLocal": scope == "local",
            "name": name,
            "message": message,
        })

    def on_server_message(msg_type: str, text: str, extra: list) -> None:
        # Only chat-HUD lines reach here (the client filters out empty control
        # messages, matching onServerMessage's getWordCount gate).
        forward({"action": "server_message", "message": text})

    def on_player_joined(
        name: str, client_id, location: str, message: str, usernames: list
    ) -> None:
        forward({
            "action": "player_joined",
            "name": name,
            "client_id": client_id,
            "location": location,
            "message": message,
            "associated_usernames": usernames,
        })

    def on_player_dropped(name: str, client_id, message: str, usernames: list) -> None:
        forward({
            "action": "player_dropped",
            "name": name,
            "client_id": client_id,
            "message": message,
            "associated_usernames": usernames,
        })

    def on_zone_change(player: str, zone: str, client_id) -> None:
        # Mirror NodeRED.cs TrackPlayerZoneChange: action/player/zone/message.
        forward({
            "action": "zone_change",
            "player": player,
            "zone": zone,
            "message": f"{player} entered {zone}",
        })

    def on_login_result(success: bool, detail: str) -> None:
        forward({"action": "login_result", "success": success, "detail": detail})

    def on_connection_state(state: str, logged_in: bool) -> None:
        # Emitted on every change to the connection lifecycle state OR the login
        # flag. `state` walks: connecting -> awaiting_challenge_response ->
        # awaiting_connect_response -> connected -> ingame_loggedout, then
        # disconnected/timed_out/rejected (or reconnecting while retrying).
        forward({
            "action": "connection_state",
            "state": state,
            "logged_in": logged_in,
        })

    def on_sync_clock(uptime_seconds: float, received_at: float) -> None:
        # Mirror ServerRunTimePackage (base/skylord/serverTime.cs): the server
        # reports its uptime in seconds on connect. Forward the value plus the
        # unix timestamp of when we received it. The uptime is simtime-derived
        # (CPU-speed dependent) and thus approximate -- consumers use a sudden
        # drop below the previous value to detect a server restart.
        forward({
            "action": "sync_clock",
            "uptime_seconds": uptime_seconds,
            "received_at": received_at,
        })

    client.on_chat = on_chat
    client.on_server_message = on_server_message
    client.on_player_joined = on_player_joined
    client.on_player_dropped = on_player_dropped
    client.on_zone_change = on_zone_change
    client.on_login_result = on_login_result
    client.on_connection_state = on_connection_state
    client.on_sync_clock = on_sync_clock

    # ---- inbound: action handlers (shared by both transports) ----------- #
    # The actual game-side behavior lives here, expressed against plain Python
    # values. Each transport adapts its own wire format onto these: Node-RED
    # parses a text command line into (verb, args); the WebSocket server parses
    # a JSON object {"action", ...fields}. Both end up calling the same act_*.
    def act_say(text: str) -> None:
        if text:
            client.say(text)

    def act_global(text: str) -> None:
        if text:
            client.global_chat(text)

    def act_login(user: str | None = None, password: str | None = None) -> None:
        if user and password:
            client.login(user, password)
        else:
            client.login()

    def act_logout() -> None:
        client.logout()

    def act_register(user: str | None = None, password: str | None = None) -> None:
        # Manual new-character registration (random appearance). Without a
        # user/pass pair, uses the configured account.
        if user and password:
            client.register_new_user(user, password)
        else:
            client.register_new_user()

    def act_raw(verb: str | None, args: list) -> None:
        if verb:
            client.command_to_server(verb, *args)

    def act_list_objects(include_removed: bool) -> None:
        # -> {"action":"object_list","objects":[...]}
        forward({
            "action": "object_list",
            "objects": client.list_objects(include_removed=include_removed),
        })

    def act_players() -> None:
        # -> {"action":"players","players":[...]} -- the online roster, each
        # joined to its matched Player ghost (object_id/position/full object,
        # joined_at as a unix timestamp).
        forward({"action": "players", "players": client.get_players()})

    def act_connection() -> None:
        # -> {"action":"connection_state","state":...,"logged_in":...} -- the
        # current status, on demand. Same shape as the pushed event.
        status = client.connection_status()
        forward({
            "action": "connection_state",
            "state": status["state"],
            "logged_in": status["logged_in"],
        })

    def act_sync_clock() -> None:
        # -> {"action":"sync_clock","uptime_seconds":...,"received_at":...} -- the
        # last server clock sync (nulls if none received yet). Same shape as the
        # pushed event.
        forward({"action": "sync_clock", **client.sync_clock_status()})

    def act_get_object(ghost_id) -> None:
        # -> {"action":"object","object":{...}|null}
        obj = None
        if ghost_id is not None:
            try:
                obj = client.get_object(int(ghost_id))
            except (ValueError, TypeError):
                obj = None
        forward({"action": "object", "object": obj})

    async def act_disconnect() -> None:
        await client.disconnect("bridge requested")

    # ---- Node-RED inbound: text command line -> act_* ------------------- #
    # Mirrors docs/nodered-protocol.md. say/global take the whole remainder as
    # one text arg; other verbs are shlex-tokenized into cmd.args.
    if bridge is not None:
        bridge.register_handler("say", lambda c: act_say(c.args[0] if c.args else ""))
        bridge.register_handler("global", lambda c: act_global(c.args[0] if c.args else ""))
        bridge.register_handler("login", lambda c: act_login(*c.args[:2]))
        bridge.register_handler("logout", lambda c: act_logout())
        bridge.register_handler("register", lambda c: act_register(*c.args[:2]))
        bridge.register_handler("raw", lambda c: act_raw(c.args[0], c.args[1:]) if c.args else None)
        bridge.register_handler(
            "list_objects",
            lambda c: act_list_objects(bool(c.args) and c.args[0].lower() in ("all", "1", "true")),
        )
        bridge.register_handler("players", lambda c: act_players())
        bridge.register_handler("connection_state", lambda c: act_connection())
        bridge.register_handler("sync_clock", lambda c: act_sync_clock())
        bridge.register_handler("get_object", lambda c: act_get_object(c.args[0] if c.args else None))
        bridge.register_handler("disconnect", lambda c: act_disconnect())

    # ---- WebSocket inbound: JSON object -> act_* ------------------------ #
    # Mirrors docs/websocket-protocol.md. The "action" selects the handler; the
    # remaining fields are read by name (e.g. {"action":"say","message":"hi"}).
    def ws_say(m: dict) -> None:
        # {"action":"say","message":...,"local":bool}. Defaults to GLOBAL chat;
        # "local": true sends local/proximity chat. ("global" remains an alias.)
        message = str(m.get("message", "") or "")
        if not message:
            return
        if m.get("local"):
            act_say(message)
        else:
            act_global(message)

    if server is not None:
        server.register_handler("say", ws_say)
        server.register_handler("global", lambda m: act_global(str(m.get("message", "") or "")))
        server.register_handler("login", lambda m: act_login(m.get("username"), m.get("password")))
        server.register_handler("logout", lambda m: act_logout())
        server.register_handler("register", lambda m: act_register(m.get("username"), m.get("password")))
        server.register_handler("raw", lambda m: act_raw(m.get("verb"), list(m.get("args") or [])))
        server.register_handler("list_objects", lambda m: act_list_objects(bool(m.get("all"))))
        server.register_handler("players", lambda m: act_players())
        server.register_handler("connection_state", lambda m: act_connection())
        server.register_handler("sync_clock", lambda m: act_sync_clock())
        server.register_handler("get_object", lambda m: act_get_object(m.get("ghost_id")))
        server.register_handler("disconnect", lambda m: act_disconnect())

    # Clean shutdown on SIGINT/SIGTERM.
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, ValueError):
            pass

    if bridge is not None:
        await bridge.start()
    if server is not None:
        await server.start()

    async def stop_transports() -> None:
        if bridge is not None:
            await bridge.stop()
        if server is not None:
            await server.stop()

    if not _has_credentials(config):
        log.warning("no credentials configured; staying logged out (decode-only)")

    # Optional interactive REPL (typed commands + tab-completion), running
    # alongside the live bot. /quit (or Ctrl-D) sets `stop`. Started once and
    # reused across reconnects (the client instance is stable; reset() rebuilds
    # only its internal protocol stack, so this reference stays valid).
    repl_task: Optional[asyncio.Task] = None
    if interactive:
        try:
            from .repl import run_repl
            repl_task = asyncio.create_task(run_repl(client, stop), name="repl")
        except Exception:  # noqa: BLE001 - never let REPL setup kill the bot
            log.exception("interactive REPL unavailable; continuing headless")

    target = config.aot_server_host or f"(resolve via master server {config.aot_master_url})"

    # Connect -> run-until-drop, optionally looping on AUTO_RECONNECT. With
    # auto-reconnect off, this runs exactly once (exit on the first drop/failure,
    # matching the prior behavior).
    try:
        while not stop.is_set():
            log.info("connecting to AoT server %s port %d", target, config.aot_server_port)
            ok = await client.connect()
            if ok:
                # Run until interrupted, /quit, or the connection drops.
                while not stop.is_set():
                    if client.conn is None or not client.conn.is_connected:
                        log.info("connection closed")
                        break
                    await asyncio.wait([asyncio.create_task(stop.wait())], timeout=1.0)
            else:
                log.error("connection failed")

            if stop.is_set():
                break
            if not config.auto_reconnect:
                log.info("auto-reconnect disabled; exiting")
                break

            # Tear down this attempt (closes the transport), announce the retry,
            # then wait AUTO_RECONNECT_INTERVAL -- interruptible by shutdown.
            await client.disconnect("reconnecting")
            client.mark_reconnecting()
            log.info("reconnecting in %.1fs", config.auto_reconnect_interval)
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=config.auto_reconnect_interval
                )
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            # Fresh protocol stack for the next attempt.
            client.reset()
    finally:
        if repl_task is not None:
            repl_task.cancel()
        await client.disconnect("aotbot shutting down")
        await stop_transports()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = build_config(args)
    except ConfigError as exc:
        # Logging may not be set up yet; go straight to stderr.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(level=config.log_level, dump_packets=config.dump_packets)
    # Interactive REPL: explicit flag wins, else auto-on when stdin is a TTY.
    interactive = args.interactive
    if interactive is None:
        interactive = sys.stdin.isatty()
    return run(config, interactive)


if __name__ == "__main__":
    sys.exit(main())
