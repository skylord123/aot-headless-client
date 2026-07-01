# Node-RED bridge protocol

The headless bot bridges Age of Time chat / connection state to **Node-RED** over
a plain TCP connection. The bot is the **TCP client**; Node-RED is the server
(default `localhost:1881`, from the `.env` keys `NODERED_HOST` / `NODERED_PORT`).

This bridge re-creates the on-the-wire convention of the in-engine
implementation `AgeOfTime/base/skylord/NodeRED.cs`, so the same Node-RED flows
work against either the engine or the headless bot. Implementation:
`aotbot/nodered.py` (class `NodeRedBridge`).

The bridge is **transport + parsing + dispatch only**. It does not perform any
game actions; the game client module registers handlers per command verb.

## Framing

### Outbound (bot -> Node-RED)

Each message is sent as UTF-8 followed by the terminator `\n\n\n` (three
newlines). This matches `NodeRED::send`:

```
NodeRedTCP.send(%msg @ "\n\n\n");
```

Example bytes for the message `alice: hello`:

```
alice: hello\n\n\n
```

The bot forwards received chat/server messages and login/connection state
changes outbound, and replies to query commands. The exact outbound message
shapes are owned by the game client module (`aotbot/main.py`), not the bridge;
they are documented in [Outbound messages](#outbound-messages) below.

### Inbound (Node-RED -> bot)

Line-based. The bridge accumulates a receive buffer and splits it on `\n`; each
complete line is one command (`NodeRedTCP::onLine`). Partial lines are buffered
until their terminating `\n` arrives, so a command may span multiple TCP reads
and multiple commands may arrive in a single read. A trailing `\r` (CRLF) is
tolerated and stripped. Blank lines are ignored.

## Auto-reconnect

The bridge auto-reconnects with backoff, mirroring `NodeRED::_retry_connect`:

```
%connectionDelay = NodeRedTCP.connection_attempts < 5 ? 1000 : 5000;
```

- First 4 consecutive attempts: retry after **1s**.
- Attempt 5 and beyond: retry after **5s**.

The attempt counter resets to 0 on a successful connect. Calling `stop()`
disables reconnect.

## Inbound command grammar

Each inbound line is parsed into a structured command `(verb, args, raw)` and
dispatched to the handler registered for `verb` (or a default handler). The
verb is **case-insensitive** (normalized to lowercase).

The bridge only **parses and dispatches**. The command set below is wired up by
the game client module (`aotbot/main.py`); the bridge itself knows nothing about
these specific verbs.

| Command                  | Args (parsed)                | Effect                                                       |
| ------------------------ | ---------------------------- | ----------------------------------------------------------- |
| `say <text>`             | `[text]`                     | Local/proximity chat. No-op if `text` is empty.             |
| `global <text>`          | `[text]`                     | Global chat. No-op if `text` is empty.                      |
| `login`                  | `[]`                         | Log in with the configured account credentials.             |
| `login <user> <pass>`    | `[user, pass]`               | Log in as `user` with password `pass`.                      |
| `logout`                 | `[]`                         | Log out of the current account.                             |
| `register`               | `[]`                         | Register a new character for the configured account (random appearance). |
| `register <user> <pass>` | `[user, pass]`               | Register a new character `user` with password `pass`.       |
| `disconnect`             | `[]`                         | Disconnect from the game server.                            |
| `raw <verb> <args...>`   | `[verb, *args]`              | Arbitrary `commandToServer(verb, *args)` — escape hatch for any server command. |
| `players`                | `[]`                         | Request the online roster. Replies with a `players` message. |
| `connection_state`       | `[]`                         | Request the current connection status. Replies with a `connection_state` message (`state` + `logged_in`). |
| `list_objects [all]`     | `[]` or `[all\|1\|true]`     | Request the tracked object list (with `all`/`1`/`true`, includes removed objects). Replies with `object_list`. |
| `get_object <ghost_id>`  | `[ghost_id]`                 | Request one object by integer ghost id. Replies with `object` (`null` if missing/invalid). |

The `players` / `connection_state` / `list_objects` / `get_object` query commands
each trigger a JSON reply — see [Outbound messages](#outbound-messages).

### Parsing rules

- The first whitespace-delimited token is the lowercased **verb**.
- `say` / `global` — the entire remainder of the line is taken as a **single
  text argument**, with internal whitespace and any quote characters preserved
  verbatim. No quoting is needed for chat text.
- **All other verbs** — the remainder is tokenized with **shell-like quoting**
  (`shlex`), so multi-word arguments can be quoted. If the quoting is unbalanced,
  the parser falls back to a plain whitespace split (never raises).
- Verbs that take no arguments (`logout`, `disconnect`, `players`,
  `connection_state`) ignore any extra tokens.
- **Unknown verbs** are still parsed into a command and dispatched to the
  default handler (if any); otherwise the line is logged and ignored.

### Examples

```
say hello there, world
  -> verb="say"     args=["hello there, world"]

global server restarting in 5 min
  -> verb="global"  args=["server restarting in 5 min"]

login alice s3cret
  -> verb="login"   args=["alice", "s3cret"]

logout
  -> verb="logout"  args=[]

list_objects all
  -> verb="list_objects" args=["all"]

get_object 1234
  -> verb="get_object"   args=["1234"]

connection_state
  -> verb="connection_state" args=[]

disconnect
  -> verb="disconnect" args=[]

raw Talk "hello world" 42
  -> verb="raw"     args=["Talk", "hello world", "42"]
```

## Outbound messages

Outbound payloads are **JSON objects**, each sent as one line terminated by
`\n\n\n` (so Node-RED can `JSON.parse` the payload). Every object carries an
`"action"` discriminator. As with inbound parsing, the bridge only transports
the bytes — these shapes are produced by the game client module (`aotbot/main.py`)
and mirror the in-engine Torque bot (`base/skylord/bot/NodeRED.cs`).

They fall into two groups.

### Event pushes

Emitted asynchronously as game state changes:

| `action`           | Fields                                  | Meaning                                          |
| ------------------ | --------------------------------------- | ------------------------------------------------ |
| `player_message`   | `isLocal` (bool), `name`, `message`     | Chat from another player; `isLocal` distinguishes local/proximity from global chat. |
| `server_message`   | `message`                               | A server/system message that reaches the chat HUD. Only **non-empty** chat-HUD lines are emitted; empty control-message strings are suppressed (see note below). |
| `player_joined`    | `name`, `client_id` (int\|null), `location`, `message`, `associated_usernames` (array) | A client appeared in the roster (`MsgClientJoin`). `message` is the chat-HUD line if any (often empty). `associated_usernames` is every real character name this `client_id` has used this session. |
| `player_dropped`   | `name`, `client_id` (int\|null), `message`, `associated_usernames` (array) | A client left the roster (`MsgClientDrop`). `message` is the chat-HUD line, e.g. `"<name> has left the game."`. `associated_usernames` is the full name history (captured before the client is removed). |
| `zone_change`      | `player`, `zone`, `message`             | A player moved to a new world zone/region (`MsgClientScoreChanged`). `message` is `"<player> entered <zone>"`. Skipped for logged-out/connecting placeholders and unknown clients. |
| `login_result`     | `success` (bool), `detail`              | Outcome of a login attempt.                      |
| `connection_state` | `state` (str), `logged_in` (bool)       | Connection lifecycle / login change. Also requestable on demand — see [Connection state](#connection-state). |
| `sync_clock`       | `uptime_seconds` (number), `received_at` (unix seconds) | Server-reported uptime, emitted on connect (`clientCmdSyncClock`). See note below. |

#### Connection state

`connection_state` reports the bot's own connection lifecycle plus its login
status. It is **pushed on every change** to either field, and can also be
**requested on demand** (send the `connection_state` command) — both use the
same JSON shape:

```json
{"action": "connection_state", "state": "connected", "logged_in": false}
```

- **`state`** — the current lifecycle stage (string).
- **`logged_in`** — whether the bot is logged into an account (bool), independent
  of `state`: the bot reaches `ingame_loggedout` first and only flips
  `logged_in` to `true` once the account login completes.

A normal session walks the states **in this order**:

| Order | `state`                        | Meaning                                                          |
| ----- | ------------------------------ | ---------------------------------------------------------------- |
| 1     | `connecting`                   | A connect attempt has started (host resolution + handshake).     |
| 2     | `awaiting_challenge_response`  | Sent the connect challenge; waiting for the server's reply.      |
| 3     | `awaiting_connect_response`    | Challenge solved; waiting for connect accept/reject.             |
| 4     | `connected`                    | The UDP connection was accepted; mission load begins.            |
| 5     | `ingame_loggedout`             | Reached the in-game logged-out state (ready to log in).          |
| —     | `disconnected`                 | The connection ended cleanly / was dropped.                      |
| —     | `timed_out`                    | The connection timed out (no response).                          |
| —     | `rejected`                     | The server rejected the connect request.                         |
| —     | `reconnecting`                 | Waiting to retry, only when `AUTO_RECONNECT` is enabled (below). |

`logged_in` flips to `true` (while `state` stays `ingame_loggedout`) after a
successful login, and back to `false` on logout or any disconnect. Consecutive
identical `(state, logged_in)` pairs are de-duplicated, so you only receive an
event on a genuine change.

**Auto-reconnect.** By default the bot **exits** when the connection drops. Set
`AUTO_RECONNECT=true` to instead retry in a loop, waiting
`AUTO_RECONNECT_INTERVAL` seconds (default `2.0`) between attempts. While waiting
it emits `connection_state` with `state: "reconnecting"`, then `connecting` again
on the next attempt — so watching this event stream is how a consumer observes a
server restart / reconnect cycle. (Both variables are read from the environment /
`.env`; see `.env.example`.)

On connect the server sends `clientCmdSyncClock(<uptime>)`, reporting its uptime
in seconds. The bot forwards this as a **`sync_clock`** event carrying that
`uptime_seconds` value and `received_at` — the unix timestamp (seconds) of when
the bot received it, so a consumer can extrapolate the current uptime as
`uptime_seconds + (now - received_at)`. This mirrors the in-engine
`ServerRunTimePackage` (`base/skylord/serverTime.cs`). The uptime is derived from
the server's simtime, which depends on the host CPU speed, so it is
**approximate** — its primary use is **detecting a server restart**: when a new
`sync_clock` reports an `uptime_seconds` that has suddenly **dropped below** the
previous value, the server has restarted. The bot re-emits `sync_clock` each time
it reconnects.

The server multiplexes everything through one tagged `ServerMessage` command.
The engine adds a chat-HUD line only when the message text is non-empty
(`onServerMessage` / `getWordCount` in `base/client/message.cs` + `chatHud.cs`),
and routes the tagged `MsgClientJoin` / `MsgClientDrop` control messages to the
player-list handlers. The bot mirrors this: **`server_message` is emitted only
for non-empty chat-HUD lines** (so the empty roster-sync `MsgClientJoin` /
`MsgClientScoreChanged` spam is dropped), while `MsgClientJoin` / `MsgClientDrop`
become structured **`player_joined` / `player_dropped`** events. A message that
is both a roster change and a HUD line (e.g. `"<name> has left the game."`)
produces both. Note the server re-sends `MsgClientJoin` for everyone already
online when the bot connects (and for `"<Connecting>"` / `"<Logged Out>"`
placeholders), so `player_joined` means "roster entry appeared", not strictly a
brand-new login — filter on `name` if you only want real logins.

A single connection (`client_id`) can log out and back in as a **different**
character without dropping (each fires a new `MsgClientJoin`). The bot
accumulates every real character name seen for a `client_id` into
`associated_usernames` (first-seen order, de-duplicated); logged-out placeholder
names (anything starting with `<`, which a real username can never contain) are
excluded. It is reported on `player_joined`, `player_dropped`, and each `players`
reply entry. The history is per connection — when the client drops, the
`client_id` is freed and the history is gone.

### Query replies

Emitted in response to an inbound query command:

| `action`      | In reply to    | Fields                          | Meaning                                            |
| ------------- | -------------- | ------------------------------- | -------------------------------------------------- |
| `players`     | `players`      | `players` (array)               | The online roster, each entry joined to its Player ghost. |
| `connection_state` | `connection_state` | `state` (str), `logged_in` (bool) | The current connection status (same shape as the pushed event). |
| `object_list` | `list_objects` | `objects` (array)               | The tracked object/ghost list.                     |
| `object`      | `get_object`   | `object` (object \| `null`)     | One object; `null` when the ghost id is unknown or unparseable. |

### Examples

One JSON object per line (terminator omitted for readability):

```text
{"action": "player_message", "isLocal": true, "name": "alice", "message": "hello"}
{"action": "server_message", "message": "Server restarting in 5 minutes"}
{"action": "player_joined", "name": "DiscordBot", "client_id": 38239, "location": "Port Town", "message": "", "associated_usernames": ["DiscordBot"]}
{"action": "player_dropped", "name": "What's For Dinner", "client_id": 39570, "message": "What's For Dinner has left the game.", "associated_usernames": ["What's For Dinner"]}
{"action": "zone_change", "player": "alice", "zone": "Port Town", "message": "alice entered Port Town"}
{"action": "login_result", "success": true, "detail": "ok"}
{"action": "connection_state", "state": "connected", "logged_in": false}
{"action": "connection_state", "state": "ingame_loggedout", "logged_in": true}
{"action": "connection_state", "state": "reconnecting", "logged_in": false}
{"action": "sync_clock", "uptime_seconds": 86400.0, "received_at": 1751328000.123}
{"action": "players", "players": [{"name": "alice", "client_id": 7, "object_id": 1234, "associated_usernames": ["alice"]}]}
{"action": "object_list", "objects": [{"id": 1234, "class": "Player"}]}
{"action": "object", "object": {"id": 1234, "class": "Player"}}
```

(The `players` / `object_list` / `object` array element shapes are owned by the
game client's object tracking; the fields shown are illustrative.)

## Logging

Connection state and traffic are logged via the logger `aotbot.nodered`:

- `INFO`  — connect attempts, connected, disconnected/retry, stopped.
- `WARNING` — connect/send failures, send-while-disconnected.
- `DEBUG` — `SENDING -> ...` and `RECEIVED <- ...` traffic (parity with the
  engine's `$NODE_RED::DEBUG` logging).

## Public API (`NodeRedBridge`)

```python
NodeRedBridge(host="localhost", port=1881, *,
              on_line=None, on_connect=None, on_disconnect=None)

await bridge.start()                 # begin connect + auto-reconnect loop
await bridge.stop()                  # tear down; disables reconnect
await bridge.send(message: str)      # send line + "\n\n\n"; False if not connected
bridge.connected                     # bool property

bridge.register_handler(verb, handler)   # handler(cmd: Command) -> None | awaitable
bridge.set_default_handler(handler)      # fallback for unregistered verbs
bridge.on_line = callback                # raw per-line hook (str), before dispatch
```

`Command` is a frozen dataclass: `Command(verb: str, args: list[str], raw: str)`.
Handlers and callbacks may be either synchronous or `async`; the bridge awaits
coroutines and isolates handler exceptions so a faulty handler cannot kill the
connection loop.
```
