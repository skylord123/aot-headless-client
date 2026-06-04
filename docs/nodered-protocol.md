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
changes outbound (the exact outbound message shapes are owned by the game
client module, not the bridge).

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

| Command                  | Args                         | Intended game action                        |
| ------------------------ | ---------------------------- | ------------------------------------------- |
| `say <text>`             | `[text]`                     | local/proximity chat: `commandToServer('Talk', text)` |
| `global <text>`          | `[text]`                     | global chat: `commandToServer('MessageSent', text)`   |
| `login <user> <pass>`    | `[user, pass]`               | log in (`commandToServer('login', user, getStringCRC(pass))`) |
| `logout`                 | `[]`                         | log out                                     |
| `connect <host:port>`    | `[host:port]`                | connect to a game server                    |
| `disconnect`             | `[]`                         | disconnect from the game server             |
| `raw <verb> <args...>`   | `[verb, *args]`              | arbitrary `commandToServer(verb, args...)`  |

> The bridge only **parses and dispatches**; the mapping to `commandToServer`
> verbs above is implemented by the game client module's handlers.

### Parsing rules

- The first whitespace-delimited token is the lowercased **verb**.
- `say` / `global` — the entire remainder of the line is taken as a **single
  text argument**, with internal whitespace and any quote characters preserved
  verbatim. No quoting is needed for chat text.
- `login` / `connect` / `raw` — the remainder is tokenized with **shell-like
  quoting** (`shlex`), so multi-word arguments can be quoted. If the quoting is
  unbalanced, the parser falls back to a plain whitespace split (never raises).
- `logout` / `disconnect` — take no arguments.
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

connect 127.0.0.1:28000
  -> verb="connect" args=["127.0.0.1:28000"]

disconnect
  -> verb="disconnect" args=[]

raw Talk "hello world" 42
  -> verb="raw"     args=["Talk", "hello world", "42"]
```

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
