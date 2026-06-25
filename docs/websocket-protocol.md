# WebSocket bridge protocol

The headless bot can **host a WebSocket server** that clients connect to for
bi-directional, JSON-based communication. This is an alternative (or a
complement) to the [Node-RED TCP bridge](./nodered-protocol.md): where the
Node-RED bridge makes the bot a TCP *client* that dials out, the WebSocket
bridge makes the bot a *server* that other things (Node-RED, a browser, a custom
script) connect to.

The server is enabled by setting `WEBSOCKET_PORT` in the environment (see
[Configuration](#configuration)). Implementation: `aotbot/websocket.py` (class
`WebSocketServer`), wired to game actions in `aotbot/main.py`.

Both transports speak the **same `action` vocabulary**, so a flow built for one
maps cleanly onto the other. The difference is purely the framing and the
inbound encoding:

| | Node-RED bridge | WebSocket bridge |
| --- | --- | --- |
| Bot role | TCP **client** (dials out) | **Server** (clients connect in) |
| Framing | line-based / `\n\n\n` terminator | RFC 6455 WebSocket frames |
| Inbound (→ bot) | text command line (`say hello`) | JSON object (`{"action":"say","message":"hello"}`) |
| Outbound (bot →) | JSON object | JSON object (identical shapes) |

Like the Node-RED bridge, this module is **transport + parse + dispatch only**.
The game-side behavior is owned by `aotbot/main.py`, which registers one handler
per `action`.

## Configuration

| Variable         | Required | Default   | Description                                                |
| ---------------- | -------- | --------- | ---------------------------------------------------------- |
| `WEBSOCKET_PORT` | no       | _(unset)_ | Port to host the WebSocket server on. **Unset → the server does not start.** |
| `WEBSOCKET_HOST` | no       | `0.0.0.0` | Interface to bind. `0.0.0.0` accepts connections from any host; use `127.0.0.1` to restrict to localhost. |

The WebSocket server and the Node-RED bridge are independent and both optional:

- Set `WEBSOCKET_PORT` to host the WebSocket server.
- Set **both** `NODERED_HOST` and `NODERED_PORT` to connect to Node-RED.
- Enable **either, both, or neither**. With neither set, the bot still runs
  (decode-only / interactive REPL) but has no external bridge.

## Framing (RFC 6455)

Standard WebSocket framing — any compliant client library handles it for you.
Notes on this server's implementation:

- The opening HTTP `Upgrade` handshake is performed normally
  (`Sec-WebSocket-Key` → `Sec-WebSocket-Accept`).
- Inbound (client → bot) frames are **masked**, as the spec requires of clients.
- Outbound (bot → client) frames are **unmasked text frames**, as required of a
  server.
- **Text** frames are processed; each must be a UTF-8 JSON object. Message
  **fragmentation / continuation** is reassembled before parsing. **Binary**
  frames are ignored.
- `ping` is answered with `pong`; a `close` frame is echoed and the connection
  is dropped. There is no application-level keepalive — rely on WebSocket
  ping/pong if you need one.

There is **no auto-reconnect** on the bot side (it is the server). If a client
disconnects it is simply dropped; the client is responsible for reconnecting.

## Connecting from Node-RED

Use a **`websocket out` / `websocket in`** node pair (or a single `websocket`
node) configured as a **client** pointing at `ws://<bot-host>:<WEBSOCKET_PORT>/`.
Set the node's payload type so it sends/receives the raw JSON string; feed it
`JSON.stringify(...)` payloads (`{"action": ...}`) and parse received messages
with a `JSON` node.

## Inbound messages (client → bot)

Every inbound message is a **JSON object** with a string `"action"` field that
selects the handler; the remaining fields are action-specific. The `action` is
**case-insensitive**. Messages that are not JSON objects, or that lack a string
`action`, are logged and ignored (the connection survives).

| `action`       | Fields                                  | Effect                                                       |
| -------------- | --------------------------------------- | ----------------------------------------------------------- |
| `say`          | `message` (str), `local` (bool, optional) | Send chat. **Defaults to global**; set `local: true` for local/proximity chat. No-op if `message` is empty/missing. |
| `global`       | `message` (str)                         | Global chat (alias for `say` with `local: false`). No-op if `message` is empty/missing. |
| `login`        | `username`, `password` (both optional)  | Log in. With both fields, logs in as that user; otherwise uses the configured account. |
| `logout`       | —                                       | Log out of the current account.                             |
| `register`     | `username`, `password` (both optional)  | Register a new character (random appearance). With both fields, registers that user; otherwise uses the configured account. |
| `disconnect`   | —                                       | Disconnect from the game server.                            |
| `raw`          | `verb` (str), `args` (array, optional)  | Arbitrary `commandToServer(verb, *args)` — escape hatch for any server command. |
| `players`      | —                                       | Request the online roster. Replies with a `players` message. |
| `list_objects` | `all` (bool, optional)                  | Request the tracked object list. With `all: true`, includes removed objects. Replies with `object_list`. |
| `get_object`   | `ghost_id` (int)                        | Request one object by integer ghost id. Replies with `object` (`null` if missing/invalid). |

The three `players` / `list_objects` / `get_object` requests trigger a JSON
reply broadcast to all connected clients — see
[Outbound messages](#outbound-messages).

### Inbound examples

One JSON object per WebSocket text frame:

```text
{"action": "say", "message": "hello there, world"}
{"action": "say", "local": false, "message": "hello there, world"}
{"action": "say", "local": true, "message": "hello there, world"}
{"action": "global", "message": "server restarting in 5 min"}
{"action": "login", "username": "alice", "password": "s3cret"}
{"action": "login"}
{"action": "logout"}
{"action": "register", "username": "alice", "password": "s3cret"}
{"action": "disconnect"}
{"action": "raw", "verb": "Talk", "args": ["hello world", 42]}
{"action": "players"}
{"action": "list_objects", "all": true}
{"action": "get_object", "ghost_id": 1234}
```

## Outbound messages (bot → client)

Outbound payloads are **JSON objects** carrying an `"action"` discriminator, and
are **broadcast to every connected client**. These are the same shapes the
Node-RED bridge emits — they are produced by the game client module
(`aotbot/main.py`) and mirror the in-engine Torque bot
(`base/skylord/bot/NodeRED.cs`). They fall into two groups.

### Event pushes

Emitted asynchronously as game state changes:

| `action`           | Fields                                  | Meaning                                          |
| ------------------ | --------------------------------------- | ------------------------------------------------ |
| `player_message`   | `isLocal` (bool), `name`, `message`     | Chat from another player; `isLocal` distinguishes local/proximity from global chat. |
| `server_message`   | `message`                               | A server/system message that reaches the chat HUD. Only **non-empty** chat-HUD lines are emitted — empty control-message strings are suppressed (see [Server messages vs. control messages](#server-messages-vs-control-messages)). |
| `player_joined`    | `name`, `client_id` (int\|null), `location`, `message`, `associated_usernames` (array) | A client appeared in the roster (`MsgClientJoin`). `message` is the chat-HUD line if any (often empty). `associated_usernames` is every real character name this `client_id` has used this session — see [Tracking usernames per client](#tracking-usernames-per-client). |
| `player_dropped`   | `name`, `client_id` (int\|null), `message`, `associated_usernames` (array) | A client left the roster (`MsgClientDrop`). `message` is the chat-HUD line, e.g. `"<name> has left the game."`. `associated_usernames` is the full name history (captured before the client is removed). |
| `zone_change`      | `player`, `zone`, `message`             | A player moved to a new world zone/region (`MsgClientScoreChanged`). `message` is `"<player> entered <zone>"`. Skipped for logged-out/connecting placeholders and unknown clients. |
| `login_result`     | `success` (bool), `detail`              | Outcome of a login attempt.                      |
| `connection_state` | `state`                                 | Connection lifecycle change (e.g. connecting/connected/disconnected). |

#### Server messages vs. control messages

The game server multiplexes everything through one `ServerMessage` command, tagged
by a `msgType`. The engine fans each one out to message callbacks: a **default**
callback turns it into a chat-HUD line (`onServerMessage`), and **tagged**
callbacks handle specific types (e.g. the player-list `MsgClientJoin`/
`MsgClientDrop`/`MsgClientScoreChanged` handlers). Crucially, `onServerMessage`
only adds a line *when the text is non-empty* (`getWordCount`).

The bot mirrors this split so consumers get clean, structured events:

- **`server_message`** is emitted **only** for messages whose text would actually
  hit the chat HUD (non-empty). The roster-sync `MsgClientJoin`/`ScoreChanged`
  spam (empty `msgString`) is **not** forwarded as `server_message`.
- **`player_joined` / `player_dropped`** are emitted for the `MsgClientJoin` /
  `MsgClientDrop` control messages, carrying the parsed `name` (and `client_id`,
  `location`). A message that is *both* a roster change *and* a HUD line (e.g.
  `"<name> has left the game."`) produces **both** events.

> **Roster-sync caveat:** the server re-sends `MsgClientJoin` for **everyone
> already online** when the bot connects, and for transient placeholder states
> like `"<Connecting>"` / `"<Logged Out>"`. So `player_joined` means "a roster
> entry appeared/updated", not strictly "a brand-new player logged in" — filter
> on `name` if you only want real logins.

#### Tracking usernames per client

A single connection (`client_id`) can log out and log back in as a **different
character** without dropping — each one fires a fresh `MsgClientJoin`. The bot
accumulates every real character name seen for a `client_id` into
`associated_usernames` (first-seen order, de-duplicated). Logged-out placeholder
names (anything starting with `<`, e.g. `"<Logged Out>"` / `"<Connecting>"`) are
**never** recorded — a real username can't contain `<`. The history is per
connection: when the client drops, its `client_id` is freed and the history is
gone (a reconnect gets a new `client_id`). `associated_usernames` is included on
`player_joined`, `player_dropped`, **and** each entry of the `players` reply.

### Query replies

Emitted in response to an inbound request:

| `action`      | In reply to    | Fields                          | Meaning                                            |
| ------------- | -------------- | ------------------------------- | -------------------------------------------------- |
| `players`     | `players`      | `players` (array)               | The online roster, each entry joined to its Player ghost. |
| `object_list` | `list_objects` | `objects` (array)               | The tracked object/ghost list.                     |
| `object`      | `get_object`   | `object` (object \| `null`)     | One object; `null` when the ghost id is unknown or unparseable. |

> **Note:** query replies are broadcast to **all** connected clients, not just
> the requester (mirroring the broadcast nature of the Node-RED bridge). A
> client should match a reply to its request by `action`.

### Outbound examples

One JSON object per WebSocket text frame:

```text
{"action": "player_message", "isLocal": true, "name": "alice", "message": "hello"}
{"action": "server_message", "message": "Server restarting in 5 minutes"}
{"action": "player_joined", "name": "DiscordBot", "client_id": 38239, "location": "Port Town", "message": "", "associated_usernames": ["DiscordBot"]}
{"action": "player_dropped", "name": "What's For Dinner", "client_id": 39570, "message": "What's For Dinner has left the game.", "associated_usernames": ["What's For Dinner"]}
{"action": "zone_change", "player": "alice", "zone": "Port Town", "message": "alice entered Port Town"}
{"action": "login_result", "success": true, "detail": "ok"}
{"action": "connection_state", "state": "connected"}
{"action": "players", "players": [{"name": "alice", "client_id": 7, "object_id": 1234, "associated_usernames": ["alice"]}]}
{"action": "object_list", "objects": [{"id": 1234, "class": "Player"}]}
{"action": "object", "object": {"id": 1234, "class": "Player"}}
```

(The `players` / `object_list` / `object` array element shapes are owned by the
game client's object tracking; the fields shown are illustrative.)

## Logging

Connection state and traffic are logged via the logger `aotbot.websocket`:

- `INFO`  — server listening, client connected/disconnected, server stopped.
- `WARNING` — handshake failures, send failures, malformed/actionless messages.
- `DEBUG` — `SENDING -> ...` and `RECEIVED <- ...` traffic.

## Public API (`WebSocketServer`)

```python
WebSocketServer(host="0.0.0.0", port=8765, *,
                on_connect=None, on_disconnect=None)

await server.start()                 # bind + accept clients
await server.stop()                  # disconnect all clients, stop listening
await server.send(obj: dict)         # broadcast JSON to all clients; returns count
server.connected                     # bool: at least one client connected
server.client_count                  # int: number of connected clients

server.register_handler(action, handler)   # handler(obj: dict) -> None | awaitable
server.set_default_handler(handler)         # fallback for unregistered actions
server.on_message = callback                # raw per-object hook (dict), before dispatch
```

Handlers and callbacks may be synchronous or `async`; the server awaits
coroutines and isolates handler exceptions so a faulty handler cannot kill a
client connection or the server.
