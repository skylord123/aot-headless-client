# aot-headless-client

A standalone, **headless** Python client for **Age of Time** (AoT) that
**reimplements the Torque Game Engine network protocol from scratch** — no game
engine, no rendering, no GUI. It performs the full UDP handshake, completes the
mission load, logs in, and then stays in-world as a lightweight client. From
there it can:

- relay chat (global + local/proximity) and server messages,
- send arbitrary `commandToServer(...)` verbs,
- track the live online-player roster and scoped game objects (positions,
  rotations, shape names, datablocks),
- bridge all of the above to **Node-RED** over TCP and/or host a **WebSocket
  server** that clients connect to, both speaking JSON with an `action` field,
- be driven interactively from a terminal REPL.

The import package is **`aotbot`** (so you run `python -m aotbot.main`); the
project/repo is named `aot-headless-client`.

> **What it is:** an unofficial, fan-made protocol client built by reverse-
> engineering the game's networking. It is not affiliated with or endorsed by
> the Age of Time developers. Use your own account, play nice with the server
> (the live-test tooling uses single, polite sessions with clean disconnects),
> and don't do anything you wouldn't do in the normal client.

For the full wire format, command verbs, and protocol flow, see
**[SPEC.md](./SPEC.md)** and the deep-dive notes under [`docs/`](./docs/).

## Requirements

- Python **>= 3.11**
- Runtime deps: `python-dotenv`, `prompt_toolkit`

## Setup

```bash
git clone <this-repo> aot-headless-client
cd aot-headless-client

python3 -m venv .venv
source .venv/bin/activate

# install the package (editable) + dev tools (pytest)
pip install -e ".[dev]"
# or runtime deps only:
# pip install -r requirements.txt
```

Then create your `.env` from the template and fill it in:

```bash
cp .env.example .env
$EDITOR .env
```

The real `.env` is **gitignored** — only `.env.example` is committed. Every
variable is documented in `.env.example`. At minimum set `AOT_USERNAME` and
`AOT_PASSWORD`; the server host is optional (blank ⇒ resolved from the master
server) and the port defaults to `28000`. The password is never sent in clear:
login sends `commandToServer('login', user, getStringCRC(pass))`.

## Configuration

Loaded from environment variables, seeded from `.env` (shell env wins over
`.env`). See `.env.example` for the full annotated list. Highlights:

| Variable             | Required | Default     | Purpose                                                   |
| -------------------- | -------- | ----------- | --------------------------------------------------------- |
| `AOT_SERVER_HOST`    | no       | (resolved)  | AoT server host/IP. Blank ⇒ resolve from the master server |
| `AOT_SERVER_PORT`    | no       | `28000`     | AoT server UDP port                                       |
| `AOT_MASTER_URL`     | no       | official    | Master server list, parsed for `IP <addr>` when host blank |
| `AOT_USERNAME`       | yes      | —           | Account username                                          |
| `AOT_PASSWORD`       | yes      | —           | Account password (sent as a CRC, never in clear)          |
| `AOT_CREATE_USER`    | no       | `false`     | Auto-create the character if it doesn't exist             |
| `AOT_TRACK_OBJECTS`  | no       | `false`     | Decode the ghost stream into a live object/player registry|
| `NODERED_HOST`       | no       | _(unset)_   | Node-RED TCP bridge host (both host+port required to enable) |
| `NODERED_PORT`       | no       | _(unset)_   | Node-RED TCP bridge port (both host+port required to enable) |
| `WEBSOCKET_PORT`     | no       | _(unset)_   | Host a WebSocket server on this port (unset → not started)  |
| `WEBSOCKET_HOST`     | no       | `0.0.0.0`   | Interface the WebSocket server binds (`127.0.0.1` = local)  |
| `LOG_LEVEL`          | no       | `info`      | debug / info / warning / error / critical                 |
| `DUMP_PACKETS`       | no       | `false`     | Hexdump + bit-decode UDP traffic (handshake debugging)    |
| `AOT_SKIP_LIGHTING`  | no       | `true`      | Ack mission phases 2/3 immediately to skip lighting load  |

Object/player **positions** require `AOT_TRACK_OBJECTS=true`. With it off the
ghost stream is still decoded for alignment, but no registry is kept (lower CPU);
chat, login, and the player roster (names/regions) still work.

## Running

```bash
# console script (installed by `pip install -e .`)
aotbot

# or as a module
python -m aotbot.main

# point at a specific env file / override values
aotbot --env-file ./prod.env --host 10.0.0.5 --port 28000 --dump-packets
```

When stdin is a TTY the interactive REPL starts automatically (disable with
`--no-interactive`). The bot connects, loads in, logs in, and bridges to
Node-RED and/or the WebSocket server (whichever are configured) for as long as
it runs; Ctrl-C / `/quit` disconnects cleanly.

## Docker

The image reads **all** configuration from the container environment — no `.env`
is baked in (and `.env*` is excluded via `.dockerignore`).

Pull the published image from GitHub Container Registry:

```bash
docker pull ghcr.io/skylord123/aot-headless-client:latest
```

Run it (pass config with `-e` flags, or reuse your local `.env` with
`--env-file`, which injects the values as container env vars):

```bash
docker run --rm --init \
  -e AOT_SERVER_HOST=1.2.3.4 -e AOT_SERVER_PORT=28000 \
  -e AOT_USERNAME='Your Account' -e AOT_PASSWORD='your-password' \
  -e AOT_TRACK_OBJECTS=true \
  ghcr.io/skylord123/aot-headless-client:latest

# or, locally, reuse .env (passed as env vars, not read from inside the image):
docker run --rm --init --env-file .env ghcr.io/skylord123/aot-headless-client:latest
```

Build it locally for testing:

```bash
docker build -t aot-headless-client .
docker run --rm --env-file .env aot-headless-client
```

The bot runs headless and as a non-root user; it installs SIGINT/SIGTERM
handlers for a clean disconnect (`--init` adds a PID-1 signal reaper).

CI builds the image on every push/PR (without publishing); pushes to the default
branch and `v*` tags publish to GHCR via the
[`docker-publish`](./.github/workflows/docker-publish.yml) workflow.

## Interactive REPL

Type-ahead, Tab-completion of commands (and `/cts` verbs), and fish-style inline
suggestions are provided by `prompt_toolkit`; incoming log lines render cleanly
above the prompt.

| Command                      | Description                                            |
| ---------------------------- | ------------------------------------------------------ |
| `/help`                      | List commands.                                         |
| `/say <msg>`                 | Global chat (`commandToServer MessageSent`).           |
| `/lsay <msg>`                | Local/proximity chat (`commandToServer Talk`).         |
| `/cts <verb> [args...]`      | Arbitrary `commandToServer` (aliases `/commandtoserver`, `/raw`). Quote args with spaces. |
| `/players` (`/who`, `/pl`)   | Online players: name, region, position, object id, join time. |
| `/objects [all] [class]`     | Scoped objects (needs `AOT_TRACK_OBJECTS`).            |
| `/object <ghostId>`          | One object's full attributes.                          |
| `/login [user] [pass]`       | Log in (defaults to the configured account).           |
| `/logout` / `/register`      | Log out / register a new character.                    |
| `/status` / `/quit`          | Connection state / disconnect and exit.                |

## Bridges (Node-RED & WebSocket)

The bot can expose itself two ways, **independently** — enable either, both, or
neither:

- **Node-RED (TCP client).** Set **both** `NODERED_HOST` and `NODERED_PORT`; the
  bot dials out to Node-RED. **Inbound** (Node-RED → bot) messages are line-based
  text commands; **outbound** (bot → Node-RED) are JSON objects with an `action`
  field. Full schema: [`docs/nodered-protocol.md`](./docs/nodered-protocol.md).
- **WebSocket (server).** Set `WEBSOCKET_PORT`; the bot *hosts* a WebSocket
  server that clients (Node-RED, a browser, a script) connect to. Messages are
  JSON objects in **both** directions, e.g. `{"action": "say", "message": "hi"}`.
  Full schema: [`docs/websocket-protocol.md`](./docs/websocket-protocol.md).

Both transports share the same `action` vocabulary, so a flow built for one maps
onto the other. Outbound actions include `player_message`, `server_message`
(only chat-HUD lines — empty control-message spam is suppressed), `player_joined`,
`player_dropped`, `zone_change`, `login_result`, `connection_state`,
`object_list`, `players`, and `object`.
Inbound actions include `say`, `global`, `login`, `logout`, `register`, `raw`
(commandToServer), `list_objects`, `players`, `get_object`, and `disconnect`.

Example — request the online-player roster (send `players` to Node-RED, or
`{"action":"players"}` over WebSocket); the bot replies with each player's
`location` (world region, e.g. "Port Town"), `joined_at` (unix timestamp), and —
when `AOT_TRACK_OBJECTS` is on — the matched `object_id`, `position`, and full
`object`.

## Project layout

```
aotbot/
  bitstream.py          # bit-packed BitStream reader/writer
  transport.py          # async UDP socket
  crc.py                # getStringCRC (zlib.crc32, finalized)
  protocol_constants.py # constants confirmed against the game binary
  netconn.py            # connection handshake + packet/notify/ack layer
  events.py             # RemoteCommandEvent / NetStringEvent + string tables
  datablocks.py         # per-class SimDataBlock unpackData decoders
  ghosts.py             # per-class NetObject unpackUpdate decoders
  phases.py             # GameConnection body + mission phases + ghost section
  telemetry.py          # scoped-object registry + decode-time value sink
  playerlist.py         # online-player roster + roster<->object matching
  client.py             # high-level connect/login/chat/query API
  nodered.py            # Node-RED TCP bridge (client)
  websocket.py          # WebSocket server bridge (RFC 6455, stdlib)
  repl.py               # interactive terminal REPL
  config.py             # .env -> Config; logging_setup.py; main.py (entrypoint)
docs/                   # protocol deep-dives (handshake, events, phases, CRC, ...)
tools/                  # capture/replay + reverse-engineering utilities
tests/                  # unit + capture-replay regression tests
```

## Tests

```bash
python -m pytest            # full suite
python -m pytest tests/test_phases.py
```

Some tests replay recorded session captures from `tools/captures/` as regression
guards; they skip automatically if a capture file is absent.

## License

MIT — see [LICENSE](./LICENSE).
