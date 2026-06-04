# Mission phases, datablock transfer, and reaching the in-game state

This documents the datablock-transfer + MissionStartPhase1/2/3 handshake, the ack
RemoteCommands, how the AoT bot fakes phases 2/3 to skip lighting, what the server
needs before it accepts `login`, and which ghost/datablock parsing can be stubbed
vs. must-parse to keep the bitstream aligned.

Citations are `file:line` into `/home/skylar/Projects/TorqueGameEngine2005`
(`$TGE`). AoT = custom fork, DSO v33. Divergence risks flagged `[CONFIRM-EXE]`.

Prereqs: handshake.md (packet/notify), event-system.md (RemoteCommandEvent +
string table). All the phase acks below are **RemoteCommandEvents**
(`commandToServer(...)`), so they ride the event section documented there.

---

## 1. The post-connect mission sequence (what the server drives)

Right after ConnectAccept (handshake.md §2.4), the server pushes the mission to
us as a mix of (a) **connection messages** (datablock-done signaling), (b)
**SimDataBlockEvents** (the actual datablocks), (c) **server `clientCmd*`
RemoteCommands** that drive the loading GUI, and (d) **ghost packets** (baseline
object state). The high-level flow, confirmed against AoT's client verbs
(`base/skylord/debugServerCommands.cs:29-37`,
`base/skylord/bot/gameConnection.cs`):

```
server -> us : clientCmdMissionStartPhase1(seq, missionName, musicTrack...?)
                + a stream of SimDataBlockEvents (the datablocks)
                + a ConnectionMessage(DataBlocksDone, seq) when all sent
us -> server : commandToServer('MissionStartPhase1Ack', seq)   [after datablocks loaded]
server -> us : clientCmdMissionStartPhase2(seq, ...)     (scene/lighting phase)
us -> server : commandToServer('MissionStartPhase2Ack', seq)
server -> us : clientCmdMissionStartPhase3(seq, ...)     (lighting/post-load)
us -> server : commandToServer('MissionStartPhase3Ack', seq)
server -> us : clientCmdMissionStart(seq)                (now fully in-game, logged out)
                also clientCmdStartLogin  -> client may auto-show login GUI
```

`[CONFIRM-EXE]` The **exact arg lists** for `clientCmdMissionStartPhase1/2/3` and
`clientCmdMissionStart` are AoT-server-script driven and must be read from a
capture. Stock TGE `clientCmdMissionStartPhase1(%seq, %missionName)` etc. — AoT
likely adds args. You only need to **read** them to stay aligned and to echo the
`%seq` back in the ack; you don't need to interpret them.

The phase-N `%seq` (a mission/loading sequence number) MUST be echoed in the
matching `MissionStartPhaseNAck`. In AoT's bot the literal `1` is passed
(`gameConnection.cs:41-42` sends `MissionStartPhase2Ack', 1`), suggesting the
seq is small/constant — but echo whatever the server sent in PhaseN to be safe.
`[CONFIRM-EXE]` confirm whether the server validates the ack's seq against what
it sent.

---

## 2. How the AoT bot fakes phases 2/3 (skip lighting)

`base/skylord/bot/gameConnection.cs:36-65`:

```torquescript
function BotGameConnection_onPhase1Complete() {
    if($SKYLORD::ENV::BOT::SKIP_LIGHTING) {
        commandToServer('MissionStartPhase2Ack', 1);   // claim phase 2 done
        commandToServer('MissionStartPhase3Ack', 1);   // claim phase 3 done
        onPhase3Complete();                              // local: pretend loaded
        return true;
    }
    return false;
}
// also: lightScene hook returns true (skips the actual scene lighting pass)
```

The trick: after **phase 1 completes** (datablocks received + acked), the client
normally would run scene lighting in phases 2/3 (expensive, needs a renderer).
The bot instead **immediately acks 2 and 3 without doing any lighting work**.
The server doesn't care whether the client actually lit the scene — it only
waits for the acks — so it advances to `clientCmdMissionStart` and the
logged-out in-game state. Our headless bot does the same: as soon as phase-1
datablocks are handled, send both acks back-to-back.

For our Python bot (no GUI, no lighting at all):

1. Detect `clientCmdMissionStartPhase1` (or the `DataBlocksDone` connection
   message — see §4).
2. Send `commandToServer('MissionStartPhase1Ack', <seq>)`.
   `[CONFIRM-EXE]` — whether Phase1Ack is required *separately* or whether the
   server starts phase 2 off the `DataBlocksDone`/datablock-loaded condition.
   In stock TGE the client sends `MissionStartPhase1Ack` from
   `onDataBlocksDone`/`clientCmdMissionStartPhase1` handler after datablocks
   preload. The bot script only explicitly fakes 2 & 3, implying phase-1 ack is
   handled by the stock client path it leaves intact — so we likely still must
   send Phase1Ack ourselves. Send it.
3. On `clientCmdMissionStartPhase2`: send `MissionStartPhase2Ack` immediately.
4. On `clientCmdMissionStartPhase3`: send `MissionStartPhase3Ack` immediately.
   (The AoT bot sends 2 and 3 together right after phase 1; both work — the
   server queues the phase-3 push and our ack satisfies it.)
5. On `clientCmdMissionStart`: we are **in-game, not logged in**. Now send the
   `login` RemoteCommand (event-system.md §5.1). AoT auto-login schedules off
   `clientCmdStartLogin` / `onPhase1Complete` (`base/skylord/bot/login.cs:118-119`).

`[CONFIRM-EXE]` The phase-ack **verb spellings** `MissionStartPhase1Ack`,
`MissionStartPhase2Ack`, `MissionStartPhase3Ack` are taken verbatim from the AoT
script and are authoritative (they're the verb the client sends). The server
resolves them via `serverCmdMissionStartPhase2Ack(%client, %seq)`.

---

## 3. GameConnection control header — MUST be consumed every server packet

Before the event section in every server→client DataPacket body, the client
reads a control/move/camera header (`GameConnection::readPacket`,
`gameConnection.cc:741-867`). **You must consume these exact bits or every
subsequent event/ghost read is misaligned.** Client-side (isConnectionToServer)
read order:

| # | field | bits | notes |
|---|-------|------|-------|
| 1 | mLastMoveAck | `readInt(32)` | server's ack of our move stream |
| 2 | damage/whiteout flag | `readFlag()` | if set: optional `readFlag()`→`readFloat(7)` damageFlash; optional `readFlag()`→`readFloat(7)` whiteOut |
| 3 | controlObj flag | `readFlag()` | if set: nested `readFlag()` chooses: (a) control-object update → `readInt(GhostIdBitSize=12)` ghost id + `obj->readPacketData(...)` ; (b) else read a compression point: 3× `read(F32)` |
| 4 | cameraObj flag | `readFlag()` | if set: `readInt(12)` ghost id + `obj->readPacketData(...)` |
| 5 | firstPerson flag | `readFlag()` | if set: `readFlag()` (the first-person bool) |
| 6 | fov flag | `readFlag()` | if set: `readInt(8)` fov |

`readFloat(n)` = `readInt(n)/((1<<n)-1)` (`bitStream.cc:315-318`). The hard parts
are `readPacketData` for the control and camera objects (3a / 4): those are
**ShapeBase player/camera state** (`ShapeBase::readPacketData`) and decode a
position, orientation, etc. **Until we are logged in we have no control object**,
so flags 3 and 4 will normally be `0` (the server only sends a control object
once we have a player). The early loading packets thus reduce to: 32-bit moveAck,
then a `0` damage flag, `0` control flag, `0` camera flag, `0` firstPerson, `0`
fov — easy to parse. `[CONFIRM-EXE]` Once a control object IS assigned (post-
login, post-spawn), `readPacketData` must be parsed to stay aligned — this is the
hardest alignment surface and overlaps with the ghosting "nice-to-have". For the
MVP we stay logged-out-then-login-as-spectator; if the server assigns a control
object we must parse `ShapeBase::readPacketData` (out of scope of this doc; see
`shapeBase.cc`).

Our **send** side (client→server) writes a mirror header
(`gameConnection.cc:879-920`): `writeFlag(mCameraPos==0)`, a 32-bit control-object
checksum, then `moveWritePacket` (§3.1), then firstPerson + fov flags. We can
write a minimal: cameraPos flag, `write(0)` checksum, an empty move list
(`moveWritePacket` with count 0), then two `0` flags.

### 3.1 moveWritePacket / moveReadPacket (`gameConnectionMoves.cc:302-365`)

Client→server moves:

| # | field | bits |
|---|-------|------|
| 1 | startMoveId | `writeInt(start, 32)` |
| 2 | count | `writeInt(count, MoveCountBits=5)` (capped at MaxMoveCount=30) |
| 3.. | each Move | `Move::pack(...)` × count |

For a headless bot that never moves, `count = 0` — write `start` (= our
`mLastMoveAck`, can be 0) and `count=0`, no Move bodies. This is the minimal
valid move section. (Server reads `start=readInt(32)`, `count=readInt(5)`, then
that many Move bodies.) `[CONFIRM-EXE]` `MoveCountBits=5`, `MaxMoveCount=30`
(`gameConnection.h:47-50`) — likely unchanged.

---

## 4. Datablock transfer (SimDataBlockEvent) and DataBlocksDone

Datablocks arrive as `SimDataBlockEvent`s (a client-event,
`gameConnectionEvents.cc:20,88-184`) inside the event section. Per-event wire
(unpack, `gameConnectionEvents.cc:110-132`):

| # | field | bits |
|---|-------|------|
| 1 | present flag | `readFlag()` — if 0, empty event (skip) |
| 2 | id | `readInt(DataBlockObjectIdBitSize=10) + DataBlockObjectIdFirst(3)` |
| 3 | classId | `readClassId(NetClassTypeDataBlock, group)` (variable width) |
| 4 | index | `readInt(10)` |
| 5 | total | `readInt(10+1 = 11)` |
| 6 | data | `obj->unpackData(bstream)` — **datablock-class-specific payload** |

`DataBlockObjectIdFirst=3`, `DataBlockObjectIdBitSize=10` (`console/simBase.h:51-52`).
`DataBlockQueueCount=16` (`gameConnection.h:28`) = how many are in flight at once.

When all datablocks are sent, the server posts a **ConnectionMessage**
`DataBlocksDone` (`gameConnectionEvents.cc:77-78`,
`handleConnectionMessage`/`gameConnection.cc:1209-1215`). That message is a
`ConnectionMessageEvent` (`netConnection.cc:37-70`):

| # | field | bits |
|---|-------|------|
| 1 | sequence | `read(U32)` (32) |
| 2 | message | `readInt(3)` (one of GhostStates / DataBlocksDone) |
| 3 | ghostCount | `readInt(GhostIdBitSize+1 = 13)` |

`DataBlocksDone` is enum value in `GhostStates` namespace
(`gameConnection.h`/`netConnection.h:722-731`). On receiving it the stock client
preloads datablocks then proceeds toward phase acks.

### 4.1 Can we stub datablock parsing?

**No — not the bits.** `SimDataBlockEvent::unpack` calls
`obj->unpackData(bstream)` whose length depends on the concrete datablock class
(`AudioProfile`, `ItemData`, `ShapeBaseImageData`, ...). To stay aligned we must
either (a) implement `unpackData` for every datablock class the server sends, or
(b) **not parse datablock contents at all and instead skip the whole event** —
which is impossible because the event is bit-packed with no length prefix. So:

- **Option A (correct, heavy):** port `unpackData` for each datablock class. Huge.
- **Option B (pragmatic):** `[CONFIRM-EXE]` measure whether AoT sends datablocks
  via SimDataBlockEvent at all during the *logged-out spectator* path, or whether
  with SKIP_LIGHTING the server defers/omits them. The bot reaching login *only*
  needs to (1) ack the connection messages and (2) keep the packet header/ack
  loop alive. If datablocks are interleaved in the event stream we cannot skip
  past one without decoding it. **This is the key open risk for the MVP.**
- **Likely reality:** the server WILL send datablocks (items, sounds, player
  shapes) before/around phase 1. We probably must implement `unpackData` for the
  datablock classes AoT uses, OR find that mission-phase skipping also reduces
  datablock volume. Start by capturing the first ~50 packets after ConnectAccept
  and counting SimDataBlockEvents.

Note `unpackData` itself doesn't *need* its result stored — we only need to walk
the exact bits. But the bit layout is per-class, so there's no generic skip.

---

## 5. Ghosting — minimum to stay aligned

Ghost section: `ghostReadPacket` (`netGhost.cc:446-549`), read **after** the
event section. Layout:

```
if not isGhostingTo(): return        # we DID setGhostTo(true) on connect, so we ARE ghosting-to
ghostingFlag = readFlag()            # if 0, no ghosts this packet -> done
if ghostingFlag == 0: return
idSize = readInt(GhostIndexBitSize=4) + 3     # bits used for ghost index this packet
loop while readFlag() == 1:          # each iteration = one ghost update
    index = readInt(idSize)
    if readFlag():                   # ghost being deleted
        delete local ghost[index]
    else:
        if local ghost[index] not present:    # NEW ghost
            classId = readClassId(NetClassTypeObject, group)   # variable width
            create NetObject, obj->unpackUpdate(this, bstream)
        else:                                  # existing ghost update
            obj->unpackUpdate(this, bstream)
```

`GhostIndexBitSize=4`, `GhostIdBitSize=12` (`netConnection.h:773-776`). Like
datablocks, **`unpackUpdate` is per-NetObject-class and bit-packed with no length
prefix** — there is no generic skip. To keep the stream aligned once ghosts flow
we must implement `unpackUpdate` for each ghosted class (Player, Item,
StaticShape, etc.).

`[CONFIRM-EXE]` **Crucial:** does the server start sending ghosts before login?
Ghosting is activated by the server's `activateGhosting`
(`netGhost.cc:789`), gated by `ReadyForNormalGhosts`/`GhostAlwaysDone` connection
messages (`netGhost.cc:719-787`). If the server only begins ghosting *after* we
have a player/control object (post-login/spawn), then during the
connect→login window the ghost section is just a single `readFlag()==0` and we
need NO ghost decoding. **This is the bet that makes the MVP feasible:** stay
logged out (or logged in but not spawned) so the server scopes nothing to us,
keeping the ghost section empty. If the server ghosts scene objects to a
logged-out client, we must decode those classes.

For the MVP, the safe stub is: read the ghost section assuming it is empty (one
`0` flag); if the flag is ever `1` you've hit content that needs real decoding —
log and treat as "need ghost decode" (the optional positions feature).

---

## 6. What the server minimally needs before accepting `login`

Inference (no AoT server source available; from client verbs + stock flow):

1. A valid handshake (handshake.md) — classCRC, GameString, protocol all correct.
2. The connection established and the packet/notify loop healthy (we ack data
   packets, answer pings). If we stop acking, the server times us out
   (`PingTimeout` chain) before login completes.
3. We progress the mission handshake far enough that the server puts us in the
   "in-game, logged out" state — i.e. it has sent (and we've acked) the phase
   sequence, and it sends `clientCmdMissionStart` / `clientCmdStartLogin`.
   **`login` is a normal RemoteCommand** and can technically be sent any time
   after connect, but AoT gates the login GUI on reaching the mission/start
   state, so the safe order is: connect → ack phases → on `clientCmdMissionStart`
   /`clientCmdStartLogin` → send `login`. `[CONFIRM-EXE]` whether the server
   rejects/ignores `login` sent before mission start.
4. The string table must have taught the server our `login` verb tag (a
   `NetStringEvent` precedes the RemoteCommandEvent — event-system.md §4.1); the
   engine does this automatically when we first reference the tag.

---

## 7. Minimal phase state machine for the bot

```
on ConnectAccept:        state = LOADING; mirror onConnectionEstablished flags
on each server DataPacket:
    parse header + rate block (handshake.md)
    parse GameConnection control header (§3)  # mostly zero flags pre-login
    parse event section (event-system.md §2):
        - NetStringEvent  -> update remote string table
        - ConnectionMessageEvent(DataBlocksDone) -> note datablocks done
        - SimDataBlockEvent -> decode (§4) [or confirm absent]
        - RemoteCommandEvent:
            clientCmdMissionStartPhase1 -> send MissionStartPhase1Ack(seq)
            clientCmdMissionStartPhase2 -> send MissionStartPhase2Ack(seq)
            clientCmdMissionStartPhase3 -> send MissionStartPhase3Ack(seq)
            clientCmdMissionStart / clientCmdStartLogin -> state = INGAME_LOGGEDOUT;
                                                           send login(user, crc)
            clientCmdLoginSuccess -> state = LOGGED_IN
            clientCmdServerMessage("<user> logged in.") -> also confirms login
            clientCmdWarningBox(tagged) -> login error (detag)
            clientCmdChatMessage(line) -> parse + forward to Node-RED
    parse ghost section (§5)  # expect empty pre-spawn; else needs decode
    build + send our DataPacket (ack) with minimal control header + empty
        move/event/ghost sections
keepalive: ensure we send a packet (or Ping) within PingTimeout; Ack incoming Pings
```

---

## Open questions / needs live confirmation

1. **Does AoT send SimDataBlockEvents during the (SKIP_LIGHTING) connect→login
   window?** If yes, we must implement per-class `unpackData` to stay aligned —
   the single biggest MVP scope risk. Capture & count.
2. **Does the server ghost any objects to a logged-out / unspawned client?** If
   no, the ghost section is always one `0` flag and needs no decoding. If yes,
   per-class `unpackUpdate` is required.
3. **Arg layouts of `clientCmdMissionStartPhase1/2/3` and `clientCmdMissionStart`**
   — needed only to read past them (and to echo `%seq`); capture.
4. **Is `MissionStartPhase1Ack` sent by us, or implicit?** Confirm the phase-1
   ack path (the bot script only explicitly fakes 2 & 3).
5. **Does the server validate the ack `%seq`** against the phase it sent (the bot
   passes literal `1`)?
6. **Is `login` accepted before `clientCmdMissionStart`?** Determines whether we
   can shortcut.
7. **Control object after login** — if the server assigns one (player/spectator),
   `GameConnection::readPacket` flags 3/4 become non-zero and
   `ShapeBase::readPacketData` must be decoded to stay aligned. Confirm whether a
   non-spawned logged-in bot avoids this.
8. **Constants** `DataBlockObjectIdBitSize=10`, `GhostIdBitSize=12`,
   `GhostIndexBitSize=4`, `ConnectionMessage message=3 bits / ghostCount=13 bits`
   — likely unchanged on the older fork but verify (a wrong width desyncs).
