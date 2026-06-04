# Build a headless Python bot that speaks the Age of Time (Torque) network protocol

## Objective
Create a standalone, headless bot (no game engine, no rendering) that connects to the live **Age of Time** game server by **reimplementing the Torque network protocol from scratch**, then:
1. Completes the connection handshake and "loading" sequence the way the real client does (faking what we can).
2. Logs in via the same path the client uses: `commandToServer('login', "<user>", getStringCRC("<pass>"))`.
3. Receives chat / server messages and sends chat.
4. Bridges all of this to **Node-RED over TCP** so chat can be intercepted and injected from there.

Put all code under `./ageoftime-minimal-bot`. **Python is preferred** (asyncio); C is acceptable only if a bit-level networking concern forces it. Getting positions/rotations/other ghosted object data is a *nice-to-have*, not required for the MVP.

## Hard constraint: this is the real difficulty
This is **not** a TCP text protocol. Torque networking is **connectionless UDP with a custom reliability layer, and packets are bit-packed (not byte-aligned)**. You must implement a `BitStream` reader/writer and the connection state machine before anything else works. Budget accordingly.

## Reference materials (all local)
- **Game files & client scripts:** `./AgeOfTime` — especially `base/skylord/**` (the existing in-engine bot logic that you are re-implementing out-of-engine). Read these to learn the *exact* command verbs and flow; do not invent them.
- **Engine source (primary protocol reference):** `/home/skylar/Projects/TorqueGameEngine2005` — TGE **1.4**. The networking lives in:
  - `engine/sim/netConnection.{cc,h}`, `engine/sim/netInterface.{cc,h}` — handshake + packet/notify layer
  - `engine/sim/netEvent.cc`, `engine/game/gameConnectionEvents.cc` — event system (this is how chat/login travel)
  - `engine/sim/netStringTable.{cc,h}` — tagged-string negotiation
  - `engine/sim/netGhost.cc`, `engine/sim/netObject.{cc,h}` — ghosting (object replication; can be stubbed for MVP)
  - `engine/game/gameConnection.{cc,h}` — the GameConnection handshake + mission phases
  - `engine/core/` for `BitStream` and the CRC implementation (`getStringCRC` -> `CalculateCRC`)
- **RE notes & disassembly recipes:** `./AgeOfTime/docs`, plus the user's notes at `/home/skylar/Nextcloud/Notes/Homelab/AgeOfTime` (`tagged string notes.md`, `Exploits/`, etc.).

## Critical engine fact — verify the wire format against the EXE
Age of Time runs a **custom fork of TGE 1.x** (DSO bytecode **v33**, roughly TGE 1.2/1.3 era, compiled Jan 2009). The 1.4 source above is **close but not byte-exact**. Protocol constants that the AoT team may have changed and that **must be confirmed by disassembling `./AgeOfTime/AgeOfTime.exe.original`** before assuming the 1.4 values:
- The connect handshake's **protocol version / game string** and any join/connect args or password.
- `NetConnection` constants: packet header layout, `mProtocolVersion`, window sizes.
- The **`getStringCRC` / `CalculateCRC`** algorithm and whether it final-inverts (the login password hash must match the server byte-for-byte, so this has to be exactly right). Validate by reproducing a known `getStringCRC` value from inside the running game console.

Use the disassembly workflow already documented in the project (capstone/pefile; `VA = fileoff + 0x400000`).

## Protocol layers to implement, in order
1. **UDP socket + `BitStream`** (LE bit packing, `readInt(bits)`, `writeInt`, string/tagged-string read/write, etc.).
2. **NetInterface handshake** — connect-challenge request -> challenge response -> connect request (with the AoT game/version args) -> connect accept/reject. Mirror `netInterface.cc`.
3. **NetConnection packet layer** — sequence numbers, ack/notify, packet rate, splitting. Must round-trip correctly or the connection drops.
4. **Event system (`NetEvent` / `RemoteCommandEvent`)** — *this is the heart of chat & login*. `commandToServer(verb, args...)` and the server's `clientCmd*(args...)` are RemoteCommandEvents. Implement the tagged-string table (`NetStringTable`) negotiation so verbs/args encode and decode.
5. **Datablock transfer + mission phases** — implement enough to satisfy the server and reach the "in-game, not logged in" state.
6. **Ghosting** — *stub for MVP*: you must still parse ghost data well enough to keep the bitstream aligned and the connection alive, but you can discard the contents. (Full decode = the optional positions/rotations feature, do later.)

## The exact flow to replicate (confirmed from the client scripts)
1. **Connect** directly to a configurable `host:port` (skip the master server for MVP; the real client uses `$Pref::ServerIP` via `MM_Connect()`).
2. Server begins **mission download phase 1** (datablocks + ghost baseline). Ack it.
3. **Skip lighting** exactly as the bot already does in `base/skylord/bot/gameConnection.cs`:
   ```
   commandToServer('MissionStartPhase2Ack', 1);
   commandToServer('MissionStartPhase3Ack', 1);
   ```
   This jumps straight to the logged-out, in-game state.
4. **Login** (from `base/skylord/helpers/login.cs`):
   ```
   commandToServer('login', %user, getStringCRC(%pass));
   ```
   - Success signal from server: **`clientCmdLoginSuccess`** (a RemoteCommandEvent) — treat this as authoritative "logged in."
   - Failure: **`clientCmdWarningBox(%warnText, %btnText)`** — decode `%warnText` (it's a tagged string; `detag` it) for the error, e.g. `"Character does not exist!"`.
   - The server also broadcasts `"<Name> logged in."` as a chat/server message shortly after.

## Chat specifics (verbatim verbs — do not change)
- **Send chat:**
  - Local/proximity say: `commandToServer('Talk', "<text>")`
  - Global: `commandToServer('MessageSent', "<text>")`
  - (Note the engine de-dups identical consecutive messages by appending `-`; replicate if you want parity.)
- **Receive chat:** server sends **`clientCmdChatMessage(<chatHudLine>, ...)`** — a single preformatted line (the existing `Chat_onChatMessage` in `base/skylord/helpers/chat.cs` parses `"<name> says, \"...\""` for local and `"<name>: ..."` for global). Reproduce that parse to emit structured `{scope, name, message, raw}`.
- **System/server messages:** **`clientCmdServerMessage`** / `onServerMessage` (used for login/logout announcements). Decode and forward these too.
- **Tagged strings:** on the **send** side a verb/arg may be a tagged-string literal (`getTaggedString`); on the **receive** side resolve with `detag`. The string table is negotiated over the wire — your `NetStringTable` implementation must handle both directions.

## Configuration: use a `.env` file
All runtime configuration comes from a **`.env` file** loaded at startup (e.g. `python-dotenv`). Ship a **`.env.example`** as the committed template; the real `.env` must be **gitignored**. At minimum:

```
# Age of Time game server
AOT_SERVER_HOST=
AOT_SERVER_PORT=

# Bot account credentials
AOT_USERNAME=
AOT_PASSWORD=

# Node-RED TCP bridge
NODERED_HOST=localhost
NODERED_PORT=1881

# Behavior / debugging
LOG_LEVEL=info
DUMP_PACKETS=false
```

Env vars override nothing silently — log the effective config (with the password redacted) on startup. Add documented entries to `.env.example` for every new option you introduce.

## Node-RED bridge (match the existing convention so the same Node-RED flows work)
Re-create the line protocol from `base/skylord/NodeRED.cs`:
- The bot is a **TCP client** to Node-RED, default host/port from `.env` (**`localhost:1881`**).
- **Outbound** (bot -> Node-RED): send a message followed by the terminator **`\n\n\n`** (three newlines). Forward every received chat/server message and login/connection state change.
- **Inbound** (Node-RED -> bot): **line-based** (`onLine`) — each line is a command for the bot. Define a small, documented command grammar, e.g.:
  - `say <text>` / `global <text>` -> `Talk` / `MessageSent`
  - `login <user> <pass>` / `logout`
  - `connect <host:port>` / `disconnect`
  - `raw <verb> <args...>` -> arbitrary `commandToServer`
- Auto-reconnect to Node-RED with backoff, mirroring the existing `_retry_connect` behavior.

## Suggested architecture
- `transport.py` (UDP + BitStream), `netconn.py` (handshake + packet/notify), `events.py` (RemoteCommandEvent + string table), `phases.py` (datablock/phase handling + ghost-skip), `client.py` (high-level: connect -> load -> login -> chat API), `nodered.py` (TCP bridge), `config.py` (`.env` loading), `main.py` (glue), plus a `protocol_constants.py` populated from the EXE disassembly.
- Config via `.env` (see above) with optional CLI overrides.
- Structured logging; a `DUMP_PACKETS` mode that hexdumps + bit-decodes traffic for debugging the handshake.

## Verification strategy
1. Stand up against the real server (or a local AoT server via `./AgeOfTime/docker-compose.yml` if usable) and **capture the genuine client's handshake** (e.g. Wireshark / a UDP proxy) to diff your packets against the real ones — the handshake and CRC are the two things most likely to be wrong.
2. Milestone gates: (a) handshake accepted, (b) reach in-game/logged-out state without the server timing you out, (c) `clientCmdLoginSuccess` received, (d) chat received and parsed, (e) chat sent and visible in-game, (f) Node-RED round-trip working.
3. Confirm `getStringCRC` parity against a value generated in the live game console before trusting login.

## Known risks / unknowns to resolve early
- Exact AoT protocol version/game-string and any connect password (RE the EXE).
- Whether AoT customized packet headers or the event-class IDs vs stock TGE 1.4.
- `getStringCRC` finalization details.
- How much of ghosting/datablock parsing the server *requires* before it will accept `login` (you may need more than a pure stub to stay connected).

Start by getting the UDP handshake to "connect accepted," because nothing else is testable until the connection survives. Document each protocol constant you confirm from the EXE in `protocol_constants.py` with the file offset/VA you found it at.
