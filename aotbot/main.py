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
    from .nodered import Command, NodeRedBridge

    client = AotClient(config)
    bridge = NodeRedBridge(config.nodered_host, config.nodered_port)
    loop = asyncio.get_running_loop()

    # ---- outbound: client events -> Node-RED ---------------------------- #
    # Payloads are JSON objects with an "action" field, matching the in-game
    # Torque bot's Node-RED protocol (base/skylord/bot/NodeRED.cs:
    # jettisonStringify of a JettisonObject). Node-RED parses payload as JSON.
    def forward(obj: dict) -> None:
        asyncio.ensure_future(bridge.send(json.dumps(obj)))

    def on_chat(scope: str, name: str, message: str, raw: str) -> None:
        # Mirror TrackPlayerMessage(): action=player_message, isLocal, name, message.
        forward({
            "action": "player_message",
            "isLocal": scope == "local",
            "name": name,
            "message": message,
        })

    def on_server_message(msg_type: str, text: str, extra: list) -> None:
        # Mirror BotNodeRed_onServerMessage(): action=server_message, message.
        forward({"action": "server_message", "message": text})

    def on_login_result(success: bool, detail: str) -> None:
        forward({"action": "login_result", "success": success, "detail": detail})

    def on_connection_state(state: str) -> None:
        forward({"action": "connection_state", "state": state})

    client.on_chat = on_chat
    client.on_server_message = on_server_message
    client.on_login_result = on_login_result
    client.on_connection_state = on_connection_state

    # ---- inbound: Node-RED command lines -> client actions -------------- #
    def h_say(cmd: Command) -> None:
        if cmd.args:
            client.say(cmd.args[0])

    def h_global(cmd: Command) -> None:
        if cmd.args:
            client.global_chat(cmd.args[0])

    def h_login(cmd: Command) -> None:
        if len(cmd.args) >= 2:
            client.login(cmd.args[0], cmd.args[1])
        else:
            client.login()

    def h_logout(cmd: Command) -> None:
        client.logout()

    def h_register(cmd: Command) -> None:
        # register [user] [pass] -- manual new-character registration (random
        # appearance). With no args, uses the configured account.
        if len(cmd.args) >= 2:
            client.register_new_user(cmd.args[0], cmd.args[1])
        else:
            client.register_new_user()

    def h_raw(cmd: Command) -> None:
        if cmd.args:
            client.command_to_server(cmd.args[0], *cmd.args[1:])

    def h_list_objects(cmd: Command) -> None:
        # list_objects [all] -> {"action":"object_list","objects":[...]}
        include_removed = bool(cmd.args) and cmd.args[0].lower() in ("all", "1", "true")
        forward({
            "action": "object_list",
            "objects": client.list_objects(include_removed=include_removed),
        })

    def h_players(cmd: Command) -> None:
        # players -> {"action":"players","players":[...]} -- the online roster,
        # each joined to its matched Player ghost (object_id/position/full object,
        # joined_at as a unix timestamp).
        forward({"action": "players", "players": client.get_players()})

    def h_get_object(cmd: Command) -> None:
        # get_object <ghost_id> -> {"action":"object","object":{...}|null}
        obj = None
        if cmd.args:
            try:
                obj = client.get_object(int(cmd.args[0]))
            except (ValueError, TypeError):
                obj = None
        forward({"action": "object", "object": obj})

    async def h_disconnect(cmd: Command) -> None:
        await client.disconnect("Node-RED requested")

    bridge.register_handler("say", h_say)
    bridge.register_handler("global", h_global)
    bridge.register_handler("login", h_login)
    bridge.register_handler("logout", h_logout)
    bridge.register_handler("register", h_register)
    bridge.register_handler("raw", h_raw)
    bridge.register_handler("list_objects", h_list_objects)
    bridge.register_handler("players", h_players)
    bridge.register_handler("get_object", h_get_object)
    bridge.register_handler("disconnect", h_disconnect)

    # Clean shutdown on SIGINT/SIGTERM.
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, ValueError):
            pass

    await bridge.start()

    target = config.aot_server_host or f"(resolve via master server {config.aot_master_url})"
    log.info("connecting to AoT server %s port %d", target, config.aot_server_port)
    ok = await client.connect()
    if not ok:
        log.error("connection failed; shutting down")
        await bridge.stop()
        return

    if not _has_credentials(config):
        log.warning("no credentials configured; staying logged out (decode-only)")

    # Optional interactive REPL (typed commands + tab-completion), running
    # alongside the live bot. /quit (or Ctrl-D) sets `stop`.
    repl_task: Optional[asyncio.Task] = None
    if interactive:
        try:
            from .repl import run_repl
            repl_task = asyncio.create_task(run_repl(client, stop), name="repl")
        except Exception:  # noqa: BLE001 - never let REPL setup kill the bot
            log.exception("interactive REPL unavailable; continuing headless")

    # Run until interrupted, /quit, or the connection drops.
    try:
        while not stop.is_set():
            if client.conn is None or not client.conn.is_connected:
                log.info("connection closed; exiting")
                break
            await asyncio.wait([asyncio.create_task(stop.wait())], timeout=1.0)
    finally:
        if repl_task is not None:
            repl_task.cancel()
        await client.disconnect("aotbot shutting down")
        await bridge.stop()


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
