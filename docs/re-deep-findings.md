# Deep reverse-engineering findings

Static RE of `AgeOfTime/AgeOfTime.exe.original` (32-bit PE, image base
**0x400000**, `.text` flat-mapped so `fileoff == VA - 0x400000`) to unblock the
Python Torque-protocol reimplementation. Tooling: `capstone` 5.x + `pefile` in
`ageoftime-minimal-bot/.venv`. The local `TorqueGameEngine2005` (TGE 1.4) tree
provided the *algorithms*; every value below was then read out of the AoT binary.

Three targets, all resolved. Confidence: **CONFIRMED** = read directly from the
exe; **INFERRED** = engine-source logic applied to exe-confirmed inputs;
**STILL-UNKNOWN** = needs a live data-packet capture.

---

## Methodology recap

`AbstractClassRep::initialize()` (stock TGE algorithm, `consoleObject.cc:95`):
- builds a linked list of every `ConcreteClassRep<T>` (registered at static-init);
- for each `(NetClassGroup, NetClassType)` it collects matching classes, **sorts
  them by `dStrcmp(name)`** (`ACRCompare`, ASCII byte order), numbers them
  `0..count-1` (= the wire `classId`), and sets
  `NetClassBitSize[group][type] = getBinLog2(getNextPow2(count + 1))`
  = `ceil(log2(count + 1))`.

So the classId↔name map and the bit widths are fully determined by *which*
classes the exe registers. I recovered that set directly from the binary.

Key exe anchors found:
- `registerClassRep` @ **VA 0x4179E0** — prepends `this` to the class link list
  head pointer @ **VA 0x65A040** (`this->nextClass[+0x24]=head; head=this`).
- `ConcreteClassRep` ctor (one representative instantiation) @ **VA 0x4C2B90**,
  `ret 0x10` (4 stack args + `this` in ecx). Field layout confirmed:
  vtable@+0x00, `mClassGroupMask`@+0x04, `mClassType`@+0x08, `mNetEventDir`@+0x0C,
  `mClassId[4]`@+0x10..0x1F (initialised to -1 → **NetClassGroupsCount = 4**),
  `mClassName`@+0x20, `nextClass`@+0x24, `parentClass`@+0x28, `mNamespace`@+0x2C.
- Each registration *thunk* in `.text` looks like (RemoteCommandEvent @ 0x5E8160):
  `push 0 (netEventDir); push 2 (classType); push 1 (groupMaskBIT); push 0x6028E8 (name*); mov ecx, 0x670C40 (ClassRep obj); call 0x4C2B90`.
  I scanned `.text` for `mov ecx,<.data imm32>; call rel32` anchors and decoded
  the four preceding `push`es, cross-checking the call target really is a
  `registerClassRep`-calling ctor.

Completeness check for events: I scanned `.rdata` for every `*Event\0` string.
The only `*Event` strings that are **not** registered networkable classes are
`ProcessInputEvent`, `ProcessTimeEvent` (internal `SimEvent` scheduler types) and
`onInputEvent` (a callback name). All 14 NetEvents are accounted for.

---

## Target 1 — Event class IDs + classId bit width — **CONFIRMED**

All AoT networkable classes are in **NetClassGroupGame** (group 0, mask BIT(0)=1).
No class registers under the Community group (only group masks 0 and 1 appear;
mask 0 = non-networkable SimObjects like Gui controls).

Per-type counts and resulting wire widths (group Game):

| NetClassType | count | `getNextPow2(count+1)` | **classId bits** |
|---|---|---|---|
| Object (0)    | 50 | 64 | **6** |
| DataBlock (1) | 34 | 64 | **6** |
| Event (2)     | 14 | 16 | **4** |

### The 14 NetClassTypeEvent classes (group Game), sorted by ASCII name

The index **is** the on-wire 4-bit `classId`:

| classId | event class | netEventDir |
|---|---|---|
| 0 | ConnectionMessageEvent | Any(0) |
| 1 | FileChunkEvent | Any(0) |
| 2 | FileDownloadRequestEvent | Any(0) |
| 3 | GhostAlwaysObjectEvent | Any(0) |
| 4 | LightningStrikeEvent | ServerToClient(1) |
| **5** | **NetStringEvent** | Any(0) |
| 6 | PathManagerEvent | Any(0) |
| **7** | **RemoteCommandEvent** | Any(0) |
| 8 | SetMissionCRCEvent | ServerToClient(1) |
| 9 | Sim2DAudioEvent | ServerToClient(1) |
| 10 | Sim3DAudioEvent | ServerToClient(1) |
| 11 | SimDataBlockEvent | ServerToClient(1) |
| 12 | SimpleMessageEvent | Any(0) |
| 13 | StaticBrickDataEvent | ServerToClient(1) |

**The two that matter for chat/login:**
- **`RemoteCommandEvent` = classId 7** (`commandToServer` / `clientCmd*`).
- **`NetStringEvent` = classId 5** (tagged-string-table teach event).

`netEventDir` for both is **Any (0)** → flows in both directions, as expected.

Representative registration VAs (ClassRep `.data` object):
RemoteCommandEvent obj 0x670C40 (thunk @ 0x5E8160); NetStringEvent obj 0x6CDDAC;
GhostAlwaysObjectEvent obj 0x6CE46C (thunk @ 0x5EC830, ctor 0x54A8D0);
SimDataBlockEvent obj 0x666190.

Confidence: **CONFIRMED**. Counts and names read from the exe; IDs/widths are the
exe inputs run through the stock `initialize()` sort+`getNextPow2` formula
(INFERRED step, but that formula is the only one TGE/AoT uses and it is not
fork-sensitive). The runtime `NetClassBitSize` table lives in `.data` (zero in the
file image, computed at startup), so the widths cannot be read as a literal — they
are computed from the exe-confirmed counts. A single captured event would
trivially re-confirm the 4-bit width and id 7.

### Object & DataBlock class lists (for ghost/datablock decode later)

Object (6-bit classId, 0..49): AIPlayer, AudioEmitter, Camera, Debris,
DestructableSpawner, FlyingVehicle, GameBase, GoldSpawner, HoverVehicle,
InteriorInstance, Item, Lightning, Marker, MazeSpawner, MissionArea,
MissionMarker, NPCSpawner, ParticleEmitterNode, PathCamera, PathedInterior,
PhysicalZone, Player, Precipitation, Projectile, RoomMarker, ScopeAlwaysShape,
ShapeBase, SimpleNetObject, Sky, SpawnSphere, Splash, StaticShape, Sun, TSStatic,
TerrainBlock, Trigger, VehicleBlocker, WaterBlock, WayPoint, WheeledVehicle,
fxBrickBatcher, fxDTSBrick, fxFoliageReplicator, fxGrassReplicator, fxLight,
fxShapeReplicatedStatic, fxShapeReplicator, fxSunLight, twSurfaceReference,
volumeLight.

DataBlock (6-bit classId, 0..33): AudioDescription, AudioEnvironment,
AudioProfile, AudioSampleEnvironment, CameraData, DebrisData, DecalData,
ExplosionData, FlyingVehicleData, GameBaseData, HoverVehicleData, ItemData,
LightningData, MissionMarkerData, ParticleData, ParticleEmitterData,
ParticleEmitterNodeData, PathCameraData, PathedInteriorData, PlayerData,
PrecipitationData, ProjectileData, ShapeBaseData, ShapeBaseImageData, SimDataBlock,
SplashData, StaticShapeData, TSShapeConstructor, TriggerData, WheeledVehicleData,
WheeledVehicleSpring, WheeledVehicleTire, fxDTSBrickData, fxLightData.

(These are sorted; index = classId. AoT additions over stock TGE include the
`fx*` / brick / spawner / maze classes — note the AoT-specific
`StaticBrickDataEvent`, `ConnectionMessageEvent`, `PathManagerEvent`,
`LightningStrikeEvent` events.)

---

## Target 2 — ConnectRequest `classCRC` (NetClassGroup CRC) — **CONFIRMED**

`GameConnection::writeConnectRequest` (@ VA 0x457AA0) first calls
`Parent::writeConnectRequest` = **`NetConnection::writeConnectRequest` @ VA
0x547170**, which writes two U32s before the game-string block:

```
547176  write(U32 mNetClassGroup)        ; from [this+0xf4]
547197  mov edx, [ecx*4 + 0x638d4c]      ; classCRC[group]  (table @ VA 0x638D4C)
54719e  write(U32 classCRC)
```

The class-CRC table `AbstractClassRep::classCRC[4]` is at **VA 0x638D4C**. Its
**static initializer in the file image is `{0xFFFFFFFF, 0x00000000, 0x00000000,
0x00000000}`** (= `{INITIAL_CRC_VALUE,}`). There are exactly **two** `.text`
references to the table — VA 0x54719A (in `writeConnectRequest`) and VA 0x5471F9
(in `readConnectRequest`) — and **both are reads**. **Nothing in the exe ever
writes the table.** So AoT, like stock TGE 1.4, never recomputes a manifest CRC.

Therefore:
- **`netClassGroup = 0`** (NetClassGroupGame).
- **`classCRC = 0xFFFFFFFF`** — a fixed constant, NOT a hash of the class list.

`readConnectRequest` (@ ~0x5471D0) compares received group == `mNetClassGroup` and
received CRC == `classCRC[group]`; mismatch sets error string **"CHR_INVALID"**
(literal @ VA 0x612E18) and rejects. Since the value is the constant 0xFFFFFFFF,
our client just sends `write(U32 0)` then `write(U32 0xFFFFFFFF)`.

Confidence: **CONFIRMED**. This removes the "single biggest blocker" the handshake
doc worried about — there is no class-manifest CRC to reconstruct; it is a literal.

Full connect-request body order (base + GameConnection, all exe-confirmed):
```
write(U32 netClassGroup = 0)
write(U32 classCRC = 0xFFFFFFFF)
writeString("Age Of Time Demo")
write(U32 currentProtocol = 11)
write(U32 minProtocol = 11)
writeString(joinPassword)        ; "" if none
write(U32 connectArgc)
connectArgc * writeString(argv[i])
```

---

## Target 3 — Data-packet header bit layout — **CONFIRMED**

`ConnectionProtocol::buildSendPacketHeader` @ **VA 0x422920** (located via the
`"build hdr %d %d"` debug-log string @ VA 0x5F1E00, xref at 0x4229C1). Helpers:
`0x420E20 = writeFlag(bool)` (1 bit), `0x420FA0 = writeInt(value, numBits)`.

Exact emitted sequence (offsets are the `call` sites):

| VA | call | field | bits |
|---|---|---|---|
| 0x422946 | writeFlag(true) | gamePacketFlag | 1 |
| 0x42295B | writeInt(mConnectSequence & 1, **1**) | connectSeq parity ([this+0x94]) | 1 |
| 0x42296C | writeInt(mLastSendSeq, **9**) | packet seq ([this+0x8C]) | 9 |
| 0x42297C | writeInt(mLastSeqRecvd, **9**) | highest-recv ack ([this+0x84]) | 9 |
| 0x422986 | writeInt(packetType, **2**) | 0=Data 1=Ping 2=Ack | 2 |
| 0x422990 | writeInt(ackByteCount, **3**) | ack-mask byte count | 3 |
| 0x4229A9 | writeInt(mAckMask, ackByteCount*8) | ack bitmask ([this+0x90]) | count*8 |

`ackByteCount = ((mLastSeqRecvd[+0x84] - mLastRecvAckAck[+0x98] + 7) >> 3)`
(computed @ 0x42292A-0x422939). On a DataPacket `mLastSendSeq` is pre-incremented
(0x422940).

So the header is **`1 | 1 | 9 | 9 | 2 | 3 | ackByteCount*8`** = 25 fixed bits +
0..32 mask bits. This is **byte-identical to stock TGE 1.4** — the older AoT fork
did **not** tweak the data-packet header widths or order. The doc's previously
*assumed* layout is now confirmed.

Field offsets on the `ConnectionProtocol`/`NetConnection` object (useful for the
read path / debugging): mLastSeqRecvd@+0x84, mLastSendSeq@+0x8C, mAckMask@+0x90,
mConnectSequence@+0x94, mLastRecvAckAck@+0x98.

Confidence: **CONFIRMED** (write side, byte-level). The read path
(`processRawPacket`) is the literal mirror in TGE and rejects `packetType >= 3`;
I could not pin its string xref (loaded via a computed address) but the write side
is definitive and the two must agree or the engine couldn't talk to itself.

---

## Bonus — live UDP probe (read-only) — **CONFIRMED PROTOCOL_VERSION path**

Sent a single `ConnectChallengeRequest` to **45.148.165.55:28000**
(`U8 type=26 | U32 connectSequence` whole-byte LE) and got a reply:

```
sent seq      = 0x2b175e90
reply (21 B)  = 1e <seq:4> <digest:16>
reply[0]      = 0x1E = 30  (ConnectChallengeResponse)   ✓
echoed seq    = 0x2b175e90  == sent seq                 ✓
addressDigest = 4 x U32 (opaque, server-secret MD5)     ✓
```

This live-confirms (separately from the static RE): OOB packet types
26→30, the 21-byte challenge-response layout (`U8 | U32 seq | U32[4] digest`),
and the sequence-echo handshake. I deliberately did **not** send a ConnectRequest
(that would attempt to actually join) — kept it read-only. The server is up and
speaks the expected handshake.

---

## Updated constants (in `aotbot/protocol_constants.py`)

CONFIRMED / corrected this pass:
- `NET_CLASS_BITS_EVENT = 4`, `NET_CLASS_BITS_OBJECT = 6`,
  `NET_CLASS_BITS_DATABLOCK = 6`; counts 14/50/34.
- `EVENT_CLASS_IDS` map; `REMOTE_COMMAND_EVENT_CLASS_ID = 7`,
  `NET_STRING_EVENT_CLASS_ID = 5`.
- `NET_CLASS_GROUP = 0`, `CONNECT_CLASS_CRC = 0xFFFFFFFF`.
- Data-packet header widths re-labelled EXE-confirmed with the `call`-site VAs.
- **Bug fix:** `packString` 2-bit type tags were transcribed wrong
  (`Integer=2, CString=3`). Corrected to the real TGE enum
  `NullString=0, CString=1, TagString=2, Integer=3` (matches both wire docs).

---

## Wave-2 live resolution — connectArgc / connectArgv (was UNCONFIRMED)

Resolved against the live server + decompiled client scripts:

- **`connectArgc = 2`**, `connectArgv = ["29", "<PlayerName>"]`.
  The genuine client does `setConnectArgs($version, $pref::Player::Name)` in
  `MM_Connect()` / `MJ_connect()` / `JoinServerGui` (decompiled from
  `base/client/scripts/{mainMenuGui,manualjoin,JoinServerGui}.cs.dso`).
  `$version` is the integer global set in `AgeOfTime/main.cs:6` -> `$version = 29;`.
- **The server validates `argv[0]` (version) AFTER ConnectAccept.** Sending
  `argc=0` gets a clean ConnectAccept (protocol 11 echoed) and then an immediate
  in-band **Disconnect** with reason "You do not have the newest version. Visit
  www.AgeOfTime.com for the latest downloads." Sending `["29", name]` makes the
  server keep us connected and stream the mission/load data packets.
- Stored as `CLIENT_VERSION = "29"` in `protocol_constants.py`; `client.py`
  sends `[CLIENT_VERSION, username-or-"Player"]`.
- (The engine's `getVersionNumber()` returns **3** and `getBuildString()`
  formats the double at VA 0x5fd520 = 0.003 with `"%5.3f"`; neither is the value
  used on the connect handshake — the script `$version=29` is.)

Also confirmed live: **ConnectAccept echoes the U32 server protocol = 11**, and
the data-packet header `1|1|9|9|2|3|ackMask` round-trips against the real server
(we processed 120+ server DataPackets with correct seq/ack/notify and replied
with acks; the server kept the connection alive, confirming the read path mirror
and the keep-alive/ping behavior).

## Wave-3 — load-phase event decode (classId-1 et al.) — **CONFIRMED via live capture + EXE**

This pass captured 104 real server DataPackets (`/tmp/aot_capture.json`, via a
read-only connect), then bit-traced them against the exe disassembly. The
load-phase desync was **NOT** the classId-1 event itself — it was the
**GameConnection control header** and **event-section framing**, which diverge
from stock TGE. classId 1 is **FileChunkEvent** (confirmed: 2nd alphabetically;
registration thunk @ VA 0x5ec7e6, ctor 0x5486d0, vtable 0x612ff4) and its
`unpack` (@ VA 0x5481b0) is **byte-identical to stock**: `readRangedU32(0,63)`
(6 bits) + that many raw bytes. The 14-event sorted-name list in the table above
was re-verified directly from the exe (all 14 registration thunks decoded:
classType=2, groupMask=1; netEventDir 1 for SimData/Sim2D/Sim3D/SetMissionCRC/
StaticBrickData/Lightning, 0 for the rest).

### The real divergences (all EXE-confirmed, live-validated)

**GameConnection::readPacket control header** (@ VA 0x4593c0, client/
`isConnectionToServer()` branch; server-write mirror @ writePacket 0x458849):
- `readInt(32)` mLastMoveAck, then **4** flags (NOT stock's 5): **damage**
  (inline readFlag @ 0x4594a1), **control** (@ 0x459516), **camera** (@ 0x45966c),
  **fov** (@ 0x4596b5). **AoT dropped the stock `firstPerson` flag.**
- control-flag set: inner flag → readInt(**14**) ghost id + ShapeBase
  readPacketData (post-spawn), else a `readCompressedPoint`.
- camera-flag set: **only** `readInt(14)` ghost id (AoT does NOT call
  readPacketData here, unlike stock).
- **`GhostIdBitSize = 14`** (stock = 12), read directly from the `push 0xe`
  ghost-id reads. ⇒ ConnectionMessageEvent ghostCount = `readInt(15)`
  (GhostIdBitSize+1), confirmed by ConnMsg::unpack `push 0xf` @ VA 0x5464a0.
- The header installs a per-packet 256-byte **stringBuffer**
  (`setStringBuffer` @ 0x4593df) BEFORE the body, so **every `readString` in the
  event/ghost sections uses the dedup-prefix path** (leading `useStringBuffer`
  flag + 8-bit offset + Huffman tail). Omitting this desyncs all string events.

**eventReadPacket** (@ VA 0x548c70): two-phase framing matches our reader, EXCEPT
the guaranteed-ordered seq is a **plain `readInt(7)` with NO "prev+1" shortcut
flag** (@ 0x548d35 — `push 7; readInt` directly). Stock TGE had a shortcut bit.

**ConnectionMessageEvent::unpack** (@ VA 0x5464a0, AoT): `read(U32 sequence)` +
`readInt(3) message` + `readInt(15) ghostCount` (stock used 13 for ghostCount).

### Event `unpack` layouts recovered this pass (EXE VAs)

| event | classId | unpack VA | wire payload |
|---|---|---|---|
| ConnectionMessageEvent | 0 | 0x5464a0 | U32 seq, readInt(3) msg, readInt(15) ghostCount |
| FileChunkEvent | 1 | 0x5481b0 | readRangedU32(0,63) len + len raw bytes |
| FileDownloadRequestEvent | 2 | (stock) | readRangedU32(0,31) + that many readString |
| LightningStrikeEvent | 4 | 0x4b35f0 | **empty** (bare ret — no wire payload) |
| NetStringEvent | 5 | 0x5442e0 | readInt(5) slot + readString |
| PathManagerEvent | 6 | 0x54d2c0 | U32 modPath, flag clearPaths, U32 totalTime, U32 numPoints, then per-point data |
| SetMissionCRCEvent | 8 | 0x457640 | read(U32 crc) |
| Sim2DAudioEvent | 9 | 0x45a580 | readInt(10) audio-profile datablock id |
| Sim3DAudioEvent | 10 | 0x45a6b0 | readInt(10) datablock id + readCompressedPoint |
| SimpleMessageEvent | 12 | 0x4c2cf0 | readString(message) |
| StaticBrickDataEvent | 13 | 0x4a0900 | 4×readFloat(8) + readInt(6) + ranged + readInt(10) ... (brick payload; NOT yet ported) |
| GhostAlwaysObjectEvent | 3 | 0x5496a0 | readInt(14) ghost id + create + unpackUpdate (per-class; NOT ported) |

`readCompressedPoint` (@ VA 0x421a70, scale table @ 0x63c0f8 = {16,18,20,32}):
`readInt(2)` type; type 3 → 3×F32; types 0/1/2 → 3×`readSignedInt(gBitCounts[t])`.

### Result (live, real server)

With the control-header (4 flags / GhostId 14), no-shortcut seq, ConnMsg-15,
stringBuffer, and the event decoders above all implemented:
- **103/104** captured packets now decode end-to-end with the production
  `phases.read_packet_body` (vs 0 before — it desynced on the first content
  packet). The **first content packet decodes a fully coherent event chain**
  (Sim3DAudio db=640, SetMissionCRC, NetString = the real English
  "...do not have the correct version..." server string, LightningStrike,
  Sim3DAudio, SimpleMessage).
- Implemented event decoders (`aotbot/events.py`): ConnectionMessage, FileChunk,
  FileDownloadRequest, NetString, PathManager, SetMissionCRC, Sim2DAudio,
  Sim3DAudio (+ compressed point), LightningStrike (no-op), SimpleMessage,
  RemoteCommand.

### STILL BLOCKED (next wave)  — *superseded by Wave-4 below*

(Historical: "one packet class still desyncs ... reads invalid classId 14 ...
~5 more leading bits".) **Wave-4 RESOLVED this.** See below.

---

## Wave-4 — the ~5-bit anomaly SOLVED; control-header write bug fixed; reached Phase1 live; server-event gate identified — **EXE + LIVE confirmed**

### 4a. The ~5-bit anomaly: the guaranteed-event `prev+1` shortcut flag (RESOLVED)

**True cause:** the prior model's note ("AoT reads a plain 7-bit ordered seq —
NO stock 'prev+1' shortcut flag") was **WRONG**. AoT keeps the stock shortcut.
`eventReadPacket` @ **VA 0x548c70**, guaranteed-phase seq read:
- `0x548d25-0x548d2b` is an inline readFlag *check*; its body is `0x548df4`
  (the real readFlag). The bit value lands in `cl`.
- `0x548e1c` `je 0x548d35`: **flag == 0** → `push 7; call readInt` (full 7-bit seq).
- `0x548e22` (flag == 1): `ebp = prevSeq; inc ebp; and ebp,0x7f` → `seq = (prevSeq+1)&0x7F`,
  **no 7-bit read**. `prevSeq` inits to **-1** (`0x548d1e or ebp,-1`).

So the framing is `presence | shortcutFlag | (7-bit seq if shortcut==0) | classId`.
Omitting the shortcut bit made our 7-bit `readInt` swallow the shortcut bit + 6
of the real seq bits, then the classId read the leftover seq bit + 3 wrong bits =
**14 (invalid)** — exactly the "~5 unaccounted bits" symptom. Fixed in
`events.py` `read_events`/`write_events`. Verified: **104/104** packets in
`/tmp/aot_capture.json` and **151/151** in a fresh live capture decode
end-to-end, and the first guaranteed event now reads classId **7
(RemoteCommandEvent)** with seq 11 — a coherent chain.

(`readClassId` itself is @ **VA 0x421510**: `readInt(NetClassBitSize[group][type])`
where the width table is @ 0x65a074, indexed `(type + 3*group)` as dwords, then a
bound-check vs `mClassCount` @ 0x65a044, return -1 on overflow. Event width = 4.)

### 4b. Client→server control-header write bug: ONE trailing flag, not two (FIXED)

`GameConnection::writePacket` @ **VA 0x458710** (client branch @ 0x458747) writes,
in order: cameraPos flag (`writeFlag` @ 0x458762), `write(U32 checksum)` @ 0x4587ef,
`moveWritePacket` @ 0x45b4b0 (`writeInt(startMoveId,32)` + `writeInt(count,5)`,
count capped 30), then **exactly ONE** trailing flag — the fov-present flag
(`writeFlag` @ 0x45880b). AoT dropped the stock `firstPerson` flag on the WRITE
side too. The mirror server-read is @ **VA 0x459738** (server branch): cameraPos
flag, `read(U32)` checksum @ 0x459758, `moveReadPacket` @ 0x45b5f0
(`readInt(32)` start + `readInt(5)` count), then ONE flag @ 0x45977e.

Our `phases._write_control_header` was writing **two** trailing flags
(firstPerson + fov) — one extra bit — which shifted the server's read of our whole
packet body, so the server silently dropped every event we sent. Fixed to write a
single fov flag (header is now 1+32+32+5+1 = **71 bits**, EXE-exact). After this
fix our outgoing packets decode byte-for-byte as the server reads them.

### 4c. SimDataBlockEvent (11), StaticBrickDataEvent (13), ghost section — decoded/ported

- **SimDataBlockEvent::unpack** @ **VA 0x45a260** (AoT): `readFlag` present; if 0
  empty. Else `readInt(10)+3` id (DataBlockObjectIdBitSize=10, First=3),
  `readClassId(DataBlock)` (6-bit), `readInt(10)` index, `readInt(11)` total, then
  `obj->unpackData(bstream)` (per-class, no length prefix). Envelope ported in
  `events._read_sim_datablock_event`; the per-class `unpackData` is NOT ported
  because **the AoT server sends ZERO SimDataBlockEvents during the connect→login
  window** (0 observed across 250+ live packets across multiple sessions). If one
  ever appears we decode the envelope, log the datablock classId, and raise (no
  generic skip).
- **StaticBrickDataEvent::unpack** @ **VA 0x4a0900** (AoT) — fully ported
  (`events._read_static_brick_data_event`): `16×(4×readFloat(8))` colour palette
  (loop @ 0x4a0910), `16×(readInt(6)+readString)` categories (loop @ 0x4a0950),
  `readInt(10)` N (@ 0x4a0976) then `N×readString` (loop @ 0x4a0990). Not seen
  live in the window either, but bit-exact and round-trip tested.
- **ghostReadPacket** @ **VA 0x549890**: gated on `mGhosting` ([edi+0x1c8]); if off
  it reads **ZERO** bits (`je 0x549ad0`) — confirmed; ghosting is never active in
  the connect→login window. When on: `readFlag` present, `readInt(4)+3` idSize,
  then a flag-gated loop of `readInt(idSize)` index + (new: `readClassId(Object)` +
  create + unpackUpdate; else unpackUpdate). Per-class `unpackUpdate` not ported
  (never reached). `phases._read_ghost_section` mirrors the gate; `ghosting_active`
  is flipped by the GhostAlwaysStarting(3) connection message.
- **Connection-message enum** (3-bit, GhostStates + GameConnection extras):
  GhostAlwaysDone=0, ReadyForNormalGhosts=1, EndGhosting=2, GhostAlwaysStarting=3,
  SendNextDownloadRequest=4, FileDownloadSizeMessage=5, **DataBlocksDone=6**,
  **DataBlocksDownloadDone=7**. ConnectionMessageEvent pack/unpack
  (@ 0x5464a0) = `write/read(U32 seq) | int(3) message | int(15) ghostCount`
  (15 = AoT GhostIdBitSize 14 + 1). `phases` now replies DataBlocksDownloadDone(7)
  on receiving DataBlocksDone(6).

### 4d. Mission-phase flow corrected

Phase1 handler now sends ONLY `MissionStartPhase1Ack` (the skip-lighting
Phase2Ack/Phase3Ack moved to the Phase2 handler — the AoT bot fakes 2 & 3 from
`onPhase1Complete`, which the engine fires from `clientCmdMissionStartPhase2`,
i.e. only after the SERVER sends Phase2; acking 2/3 on Phase1 is premature/out of
sequence). Stock server gate (TGE `serverCmdMissionStartPhase1Ack`,
example/common/server/missionDownload.cs): `%seq == $missionSequence &&
$MissionRunning && %client.currentPhase == 0` → `setMissionCRC` + `transmitDataBlocks`.
Phase1 arg0 (= `$missionSequence`) decodes as Integer **1** (`INT(7b):1`); we echo 1.

### 4e. LIVE RESULT and the REMAINING blocker (server-side, unresolved)

With 4a+4b+4c+4d the bot reaches **`clientCmdMissionStartPhase1` live** and sends
a byte-perfect `MissionStartPhase1Ack(1)`. Confirmed at the wire level: the ack
packet decodes EXACTLY as the server reads it (control header aligns at bit 130,
NetStringEvent slot0→"MissionStartPhase1Ack" seq0, RemoteCommandEvent argc2
[TagString slot0, Integer 1] seq1, clean terminator), and the server **acks the
packet** (our `highestAckedSeq` advances past it; the outgoing-event queue empties
= notify-delivered). **Yet the AoT server takes no action**: no datablocks, no
DataBlocksDone, no Phase2, no MissionStart — for 60+ s. It stays in Phase1 forever.

The bot processes NONE of our client→server events. Ruled out with live probes:
- encoding (byte-perfect; decodes identically to the server's own read path);
- event ordered-seq value/start (tried 0 and 1; tried shortcut and explicit forms);
- string-table slot (tried slot 0, 20, 31 — the server teaches its own tags from
  slot 0 upward, but per-direction tables make this irrelevant, and high slots
  also failed);
- connect args (`["29", name]` is required — name-only yields NO Phase1; tried
  version 29/30/100/999, all reach Phase1 and all still get the synced
  "wrong version" `MsgConnectionError` pref, so version is NOT a per-client gate);
- proactively sending DataBlocksDownloadDone(7) (also ignored);
- `commandToServer('login'/'Login', ...)` with bogus creds → **no `clientCmdWarningBox`**,
  proving the server processes none of our `serverCmd*` even for login.

**Conclusion:** the remaining gate is **server-side and opaque to client-exe RE** —
the AoT *server* (remote; not in our files) does not invoke `serverCmd*` for our
connection despite delivering+acking our events. The real client's identical
`MissionStartPhase1Ack` works in-game, and our bytes are provably identical on the
wire, so the difference is a connection-STATE precondition the AoT server requires
that is not visible in `AgeOfTime.exe` (which is the *client*). **Next step:** a
diff capture of the genuine AoT client's first client→server DataPackets
(run the real game with a UDP sniffer / hook `Net::sendto`) to find the byte(s)
that differ from ours, OR the AoT *server* binary/scripts. The packet/event/ghost
*decode* is complete and aligned through everything the server actually sends;
the block is purely "server won't act on our (valid, delivered) events."

Files touched this wave: `aotbot/events.py` (shortcut flag read+write;
SimDataBlock/StaticBrick decoders; connection-message helpers),
`aotbot/phases.py` (single fov write flag; corrected phase flow; connection-message
handshake incl. DataBlocksDownloadDone reply; ghosting activation),
`tests/test_events.py` + `tests/test_phases.py` (updated/added). All 187 tests pass.

---

### (historical) Load-phase event payloads (the original MVP blocker)

After the version arg, the server's first DataPackets carry an event section
that begins (unguaranteed phase) with `ConnectionMessageEvent` (classId 0,
decodes cleanly as `U32 seq | 3-bit msg | 13-bit ghostCount`) followed by an
event with classId **1** whose payload does NOT match stock TGE
`FileChunkEvent` (`readRangedU32(0,63)` + that many bytes) — decoding it as such
leaves the next classId reading as 14 (invalid), i.e. a bit-length mismatch. A
width brute-force over the ConnectionMessage and chunk fields found no clean
multi-event parse, so AoT's load-phase event #1 has a fork-specific payload
(likely a customized file/datablock chunk). Per mission-phases.md §4.1 there is
no generic skip for a bit-packed, length-less event payload, so reaching the
mission-phase `clientCmd*` drivers requires porting that event's exact
`unpackData`. This is the documented "biggest MVP scope risk" and is left as the
next wave. The connection layer degrades gracefully: it logs an alignment limit,
keeps acking at the packet/notify level, and stays connected.

## STILL-UNKNOWN / needs a live data-packet capture

1. **`ConnectionStringTable::EntryBitSize`** (the 5-bit TagString id width) and
   **`CommandArgsBits`** (the 5-bit RemoteCommandEvent argc width). Assumed 5 from
   TGE; not re-read from the exe this pass (packString's `writeInt(...,2/5)` calls
   did not surface in a width-signature scan — likely inlined or a 2-bit-specific
   helper). Low risk but a captured RemoteCommandEvent would confirm.
2. **`StringTagPrefixByte`** (assumed 0x01) — not re-confirmed in the exe.
3. **`connectArgc` value the live client sends** and whether a non-empty
   `joinPassword` is required — needs a capture of the genuine client's
   ConnectRequest (or just try argc=0, "" first).
4. **`processRawPacket` read path** — write side is byte-confirmed; the read
   mirror is assumed identical (it must be). Not separately disassembled.
5. **`ConnectAccept` server protocol version** echoed back — confirm it accepts
   11 (we only confirmed the client *writes* 11/11).
6. **Ghost / move-control header bit sizes** — out of scope this pass; needed only
   once datablock/ghost parsing is reached.

None of these block the handshake; targets 1-3 (the stated blockers) are resolved.

---

## Wave-6 — Phase-2 decode: SimDataBlockEvent per-class unpackData, GhostAlwaysObjectEvent, ghost-section framing — **EXE + capture-validated**

The login wall (the outgoing event-seq shortcut bug) was already fixed and
verified live in Wave-5. This wave attacks the *Phase-2* decode so the bot can
stay bit-aligned through the datablock + ghost-always download (the gate the
server requires before it sends GhostAlwaysDone -> Phase3 -> StartLogin).

Ground truth: replaying `tools/captures/real_login.jsonl` s2c through the
production `phases.read_packet_body` (tools/replay_s2c.py, tools/check_datablocks.py).
Clean s2c packet decode went from **131 -> 1042** this wave.

### Bitstream primitive VAs (confirmed)
- `0x420e20` **writeFlag** (takes a bool arg, `ret 4`) — NOT readFlag. Readers
  use the inline idiom `mov eax,[esi+0xc]; cmp eax,[esi+0x18]; ...; setne cl`.
- `0x420f60` **readInt(numBits)**; `0x421000` **readFloat(numBits)** (0..1);
  `0x4210b0` the inner readFloat used by **readSignedFloat**; `0x421510`
  **readClassId**; `0x421a70` **readCompressedPoint**; `0x4216f0`
  **readNormalVector(bits)** = readInt(bits+1) + readInt(bits) (= 2*bits+1 bits);
  bitstream vtable **slot 0x04 = read(size,ptr)** (raw bytes), **slot 0x1c =
  readString**. Datablock **unpackData = vtable slot 0x48**; NetObject
  **unpackUpdate = vtable slot 0x4c**.

### SimDataBlockEvent (classId 11) — per-class unpackData (aotbot/datablocks.py)
Envelope (events._read_sim_datablock_event, @ VA 0x45a260): readFlag present;
readInt(10)+3 id; readClassId(DataBlock,6b); readInt(10) index; readInt(11)
total; then `obj->unpackData` (NO length prefix). The capture's stream is
`total=436` datablocks, sent in index order. Decoders implemented + validated
bit-exactly (the stream decodes the first 33 datablocks in order, zero desync):

| datablock class | classId | unpackData VA | notes |
|---|---|---|---|
| SimDataBlock (base) | 24 | 0x4c99b0 | bare `ret` — 0 bits |
| GameBaseData | 9 | 0x456510 | 0 bits (calls base) |
| ShapeBaseData | 22 | 0x47cad0 | flag+read(4) CRC, 2 strings, 9×(flag?read(4)), string, flag, db-ref, 3 flags, 2 db-refs, 3 flags |
| AudioDescription | 0 | 0x58e1e0 | readFloat(6); flag?3×read(4); flag; flag?(2×read(4),2×readInt(9),readFloat(6),readNormalVector(8),read(4)); readInt(3) |
| AudioProfile | 2 | 0x58e3e0 | 2× db-ref; readString |
| CameraData | 4 | 0x464210 | == ShapeBaseData (tail jmp) |
| MissionMarkerData | 13 | 0x47cad0 | == ShapeBaseData (no override) |
| ParticleData | 14 | 0x4b76d0 | readFloat(12); flag?read(4); readSignedFloat(12); readFloat(9); flag?read(4); readInt(10)×2; flag?read(4); flag?(readInt(11)×2); flag; (readInt(2)+1)×(4×readFloat(7),readFloat(14),readFloat(8)); readInt(6)×readString |
| StaticShapeData | 26 | 0x48dc10 | ShapeBaseData + flag + read(4) |
| TriggerData | 28 | 0x4b6690 | GameBaseData + read(4) — geometry is NOT on the wire (rebuilt from mission file); just one U32 |

DataBlockObjectIdFirst=3, id width=getBinLog2(0x400)=10. A datablock cross-ref
is `flag + readInt(10)+3` (datablocks._read_db_ref).

**Still un-ported datablock classes (the current Phase-2 datablock wall, in the
order they next appear):** ParticleEmitterData (id 15, unpackData @ 0x4b85e0 —
readInt(10),readInt(10),readInt(16),readInt(14), 2×readRangedU32(0..181),
readInt(16), 2×readRangedU32(0..361), ... + conditional blocks; the
compiler-reordered out-of-line `jle` blocks make a correct linear transcription
risky and it MUST be CFG-followed), then ProjectileData (0x475a90), ItemData
(0x45d8a0/0x45e3b0), ExplosionData (0x4959a0), PlayerData (0x468110 — very
large), ShapeBaseImageData (0x4859a0), TSShapeConstructor (0x57fbe0),
DebrisData (0x451b70), ParticleEmitterNodeData. Each needs the same
exe-read + capture-validate loop. Resolver helper: see the session's
`/tmp/aottools.py` `resolve_unpackdata(name)` (regthunk -> ClassRep ctor ->
ClassRep vtable -> create() -> obj ctor -> obj vtable[0x48]); VERIFY each is a
*reader* (inline-readFlag idiom, no 0x420e20 writeFlag calls) before trusting it.

### GhostAlwaysObjectEvent (classId 3) — **trivial, ported**
`unpack` @ VA 0x5496a0 reads ONLY `readInt(GhostIdBitSize=14)` (the ghost id) and
allocates an info struct (`push 0xe; readInt; <alloc>; ret 8`) — there is NO
per-object payload in the event. The object data arrives via the ghost SECTION.
Ported in events._read_ghost_always_object_event; removed all 76 classId-3
blockers.

### Ghost section framing (phases._read_ghost_section, ghostReadPacket @ 0x549890)
Gated on mGhosting ([edi+0x1c8]) — 0 bits when off. When on (loop @ 0x5498b7):
1. readFlag presence; if 0 the section is empty.
2. `idSize = readInt(4) + 3`.
3. per-ghost loop: readFlag (if 0 end); `readInt(idSize)` ghost id; readFlag
   removeFlag (1 -> remove, no payload); else if NEW id `readClassId(Object,6b)`
   + create, then `obj->unpackUpdate` (slot 0x4c); else existing -> `unpackUpdate`.
Implemented exactly (tracks ghost-id -> classId so the new-vs-existing branch
matches). The per-class `unpackUpdate` (aotbot/ghosts.py) is NOT yet ported for
any object class, so a scoped object raises AlignmentError naming the class —
the precise next RE target. This framing alone (plus GhostAlwaysObjectEvent)
took clean s2c decode 131 -> 1042 packets.

### Gate status (live ceiling)
GhostAlwaysDone / Phase3 / StartLogin are withheld by the server until the
client has received ALL datablocks + ghost-always objects. So reaching login
live still requires finishing the remaining datablock `unpackData` AND the
per-class ghost `unpackUpdate`. The bot's login/chat WIRING is in place and
correct (client.on_ingame auto-fires `commandToServer('login', user, crc)`;
`global_chat` -> `commandToServer('MessageSent', text)`; incoming ServerMessage
"<user> logged in." and clientCmdLoginSuccess both mark logged-in) — it will
fire the moment Phase-2 decode is complete enough to reach StartLogin.

---

## Wave-7 — more datablock `unpackData` decoders; SBID state-loop blocker — **EXE + capture-validated**

Continued the Phase-2 datablock grind. Clean datablock decode (replaying
`tools/captures/real_login.jsonl` via `tools/check_datablocks.py`) went from
**33 -> 73 of 436** this wave; suite 196 -> 200 tests pass.

Helper VAs confirmed this wave:
- `0x4244e0` = getNextPow2, `0x424510` = getBinLog2. The idiom
  `push N; call 0x4244e0; push eax; call 0x424510; push eax; call readInt` is a
  `readRangedU32(0, N-1)` reading `getBinLog2(getNextPow2(N))` bits
  (N=0xb5->8b, 0x169->9b, 0x3e9->10b, 0x2711->14b, 0x400->10b db-ref, 5->3b,
  0x3e81->14b).
- `0x424230` = readString (calls bitstream vtable slot 0x1c); the pushed arg is
  NOT maxLen.
- `0x421240` = mathRead Point3F (3 x read(4) = 12 bytes).
- `0x421800` = read Box6F (Point3F + 3 x read(4) = 24 bytes).
- `0x4243f0` = ColorF::read (4 raw bytes).
- `0x421570` = readSignedInt(n) (1 sign-flag bit + `n-1` magnitude bits via
  `0x420e80` = readBits; for n=0x10 that's 16 bits total).
- `0x456510` GameBaseData::unpackData re-verified = **0 bits** (calls SimDataBlock
  bare-ret, sets a byte).

### Datablock classes ported (CFG-traced, capture-validated bit-exact)

| class | classId | unpackData VA | wire layout (summary) |
|---|---|---|---|
| ParticleEmitterData | 15 | 0x4b85e0 | readInt(10),(10),(16),(14); flag?readInt(16); 2xreadRangedU32(0,181)=8b; flag?ranged(0,361)=9b; flag?ranged(0,361)=9b; 3xflag(bare); readInt(15); readInt(10); 3xflag(bare); read(4) count + count x read(4) |
| ExplosionData | 7 | 0x4959a0 | readString; 2x db-ref; readInt(14); read(4); flag(bare); flag?3xreadInt(16); readInt(14); 2xranged181=8b; 2xranged361=9b; 2xranged1001=10b; readInt(14); ranged10001=14b; 4xreadInt(16); read(4); flag(bare); 9xread(4); db-ref; 4x db-ref; 5x db-ref; count=ranged(0,5)=3b; count x readFloat(8); count x 3xranged(0,16001)=14b; 2xreadFloat(8); 6xreadFloat(7) |
| SplashData | 25 | 0x4bd990 | Point3F(12B); 15xread(4); db-ref; 3x db-ref; 4x ColorF(4B); 4xread(4); 2xreadString |
| DebrisData | 5 | 0x451b70 | 6xread(4); 4xread(1)bool-byte; 6xread(4); 2xread(1); 3xread(4); 1xread(1); 2xreadString; 2x db-ref; 1x db-ref. (NB the bool fields are `Stream::read(1,&bool)` = a whole BYTE, not a 1-bit readFlag.) |

After ParticleEmitterData several Camera/Explosion-family blocks decoded for
free; after the four above the stream reaches index 72 cleanly and blocks on
ShapeBaseImageData (index 72).

### CURRENT BLOCKER: ShapeBaseImageData (classId 23, unpackData @ 0x4859a0)

`ShapeBaseImageData::unpackData` IS implemented in `datablocks.py`
(`_unpack_shape_base_image_data`) but is **NOT bit-exact and is deliberately
NOT registered** (so the stream raises cleanly at SBID rather than corrupting
downstream). True on-wire length for the first SBID in the capture = **1431
bits** (brute-forced: that exact SBID-consume count is the unique value that
lets the rest of the s2c stream decode 2 more datablocks).

What IS verified for SBID (all CFG-traced, polarity checked via the je-targets):
- Pre-loop fields validate: flag?read(4); flag(bare); readString (= clean
  `"base/data/shapes/player/crossbow.dts"` via the per-packet stringBuffer
  dedup path); read(4); **TWO eyeOffset "box" flags with INVERTED polarity**
  (Box6F=24B read when the flag bit is CLEAR -- the je-target is the read, not
  the default); 2xflag(bare); read(4); flag(bare); read(4); flag(bare); db-ref;
  flag?(2xread(4)+4xreadFloat(7)); Point3F(12B); 2xread(4); db-ref.
- Then a **31-iteration state-machine loop** (counter `0x1f` @ 0x485ea0,
  `dec; jne 0x485ea0`). Each iter: a state-present flag (`je 0x486280` ->
  loop-continuation when bit==0); if present: readString; 11xreadInt(5);
  flag?read(4); 5xflag(bare); flag?read(4) then 3xreadInt(3) (the 3xreadInt(3)
  is UNCONDITIONAL, reached by both flag branches); flag?readSignedInt(16);
  flag?readSignedInt(16); 2xflag(bare); flag?(readInt(10)+2xread(4));
  flag?readInt(10). Then after the loop: 4xreadString.
- **The bug:** with the above the decode overruns 1431 (a "first present state"
  is found at bit 1496, already past the true end), so a present state-block's
  read sequence is off by a few bits OR the pre-loop is off by a few bits right
  before the loop. The readInt(5) values in the (mis-aligned) present block are
  not coherent, confirming the desync is inside the state-block or its entry.
  NEXT: re-trace the state-block at 0x485ea0..0x4862dc with a path-merge CFG
  walker (the linear-with-fallthrough heuristic is unreliable here), and
  re-verify the pre-loop field right before 0x485ea0 (the db-ref @ 0x485e3b /
  the Point3F @ 0x485dfb). Once SBID is bit-exact, register it; the next classes
  after it per the index order are then surfaced by `check_datablocks.py`
  (PlayerData @ 0x468110, ProjectileData @ 0x475a90, ItemData, TSShapeConstructor,
  ParticleEmitterNodeData, ...).

### Status
Login NOT reached live (Phase-2 datablock decode still incomplete: 73/436, the
server withholds GhostAlwaysDone/Phase3/StartLogin until ALL datablocks +
ghost-always objects are consumed). No live connection was made this wave (all
work was offline against the golden capture). Login/chat wiring remains complete
and will fire once decode reaches StartLogin.

---

## Wave-8 — SBID unblocked (Wave-7 was a probe artifact) + 8 new datablock decoders + GhostAlwaysObjectEvent fix — **EXE + capture-validated**

Clean datablock decode (`tools/check_datablocks.py`) went **73 -> 319 of 436**;
clean s2c packet decode (`tools/replay_s2c.py`) **1042 -> 1166**; suite 200 pass.

### The Wave-7 SBID "1431 vs 1479 overrun" was a MEASUREMENT ARTIFACT (no decoder bug)
`_unpack_shape_base_image_data` (VA 0x4859a0) was bit-exact all along. The Wave-7
"overrun" probe re-ran the decoder AFTER `phases.read_packet_body`'s
`finally: bs.set_string_buffer(None)` had cleared the per-packet string buffer,
so the SBID's leading dedup-prefix `readString` took the no-buffer Huffman path,
swallowed the `useStringBuffer` flag bit, and ran away (593 garbage bits instead
of the real 219-bit `"base/data/shapes/player/crossbow.dts"`). With the string
buffer installed (as in a real packet read) the pre-loop + 31-state loop + 4 tail
strings consume exactly the right bits. The 31-state loop CFG (0x485ea0..0x4863a2)
was fully re-traced and matches the existing implementation byte-for-byte
(per-state: present-flag; readString; 11xreadInt(5); flag?read(4); 5xflag;
flag?read(4)+3xreadInt(3); flag?readSignedInt(16) x2; 2xflag;
flag?(readInt(10)+3+2xread(4)); flag?readInt(10)+3). **Lesson:** when measuring an
unpackData in isolation, copy/keep the string buffer.

### Datablock decoders ported this wave (all CFG-traced + capture bit-exact)

| class | classId | unpackData VA | wire layout (summary) |
|---|---|---|---|
| ShapeBaseImageData | 23 | 0x4859a0 | (registered; see above) |
| ProjectileData | 21 | 0x475a90 | readString; flag?3xread(4); 7x db-ref; flag(bare)+read(4); 2x db-ref; 6x db-ref; flag?(readFloat(8)+3xreadFloat(7)); flag?3xreadFloat(7); 3xreadInt(12); flag(bare); flag?3xread(4) |
| ItemData | 11 | 0x45d8a0 | ShapeBaseData; 2xreadFloat(10); flag(bare); flag?readFloat(10); flag?read(4); flag?(readInt(2)+4xreadFloat(7)+2xread(4)+flag); 8xreadString |
| TSShapeConstructor | 27 | 0x57fbe0 | readString; readInt(7) count; count x readString |
| DecalData | 6 | 0x544a40 | 2xread(4); readString |
| PlayerData | 19 | 0x468110 | ShapeBaseData; 4xflag(bare); 27xread(4); 18x db-ref (sound[]); 3xread(4) boxSize; flag(bare); flag?db-ref; 2xread(4); flag?db-ref; read(4); 2x flag?db-ref; 3x flag?db-ref (splashEmitters); 9xread(4); flag(bare). AoT sends all scalar tunables as raw read(4) (no readInt(JumpDelayBits) like stock). |
| ParticleEmitterNodeData | 16 | 0x4b6690 | SAME fn as TriggerData: GameBaseData + one read(4). |
| LightningData | 12 | 0x4b3950 | 8x db-ref; 8xreadString; 1x db-ref |
| fxLightData | 33 | 0x4aae50 | read(1)bool; 2xread(4); ColorF; readString; ColorF; 3xread(1)bool; 7xread(4); 5xflag; 2xflag; 2xColorF; 12xread(4); flag; 7xreadString; 5xread(4); 5xflag. (read(1,&bool)=one BYTE=8 bits, vs inline readFlag=1 bit; ColorF=4 raw bytes.) |

Also registered the trivial base classes GameBaseData (9, 0 bits), ShapeBaseData
(22, via its decoder), SimDataBlock (24, 0 bits) for completeness.

### GhostAlwaysObjectEvent (classId 3) unpack was WRONG (Wave-6 missed a flag)
`unpack` @ VA 0x5496a0 reads `readInt(GhostIdBitSize=14)` THEN **`readFlag`**, and
if the flag is set, `readClassId(NetClassTypeObject)` (6 bits) + create-by-classId
(@ 0x549727); if clear, create-by-name (@ 0x5496c1, no further bits). The Wave-6
"14 bits only" reading silently consumed the trailing flag as the *next* event's
framing bit -- which only desynced once a flag==1 instance appeared (ghost-always
scoping begins ~packet 170). Fixed in `events._read_ghost_always_object_event`.
This moved the first blocker from packet 170 to 181.

### CURRENT BLOCKER (Wave-9 target): the ghost/control-object layer, NOT datablocks
All 20 datablock classes that appear in the golden capture now decode bit-exact;
the remaining 117 of 436 are (per the capture's repeating
ParticleEmitterData/ExplosionData/ProjectileData triples) the SAME already-ported
classes -- they are gated behind a desync in the **ghost section / control
header** that begins once ghosting is active and real ghost objects arrive.
`tools/replay_s2c.py` blocker histogram (over the whole capture): 63x
"control-object readPacketData (post-spawn); ShapeBase decode not implemented"
(the control-object branch of `phases._read_control_header` -- needs ShapeBase
`readPacketData`), plus event classId 6/14/15 "desyncs" that are really the ghost
section's per-class `unpackUpdate` (NetObject vtable slot 0x4c) being unported, so
the post-ghost event framing misaligns. NEXT: port the per-class ghost
`unpackUpdate` (ghosts.py) for the Object classes that appear in the ghost stream
(start with the ones whose GhostAlwaysObjectEvent classIds were observed:
Camera/StaticShape/Player/Item/etc.) and the ShapeBase `readPacketData` for the
control-object header branch. Login still NOT reached live (server withholds
Phase3/StartLogin until all ghost-always objects are consumed); no live connection
made this wave (offline against the golden capture).

---

## Wave-9 — control-object readPacketData + the GhostAlways NetObject unpackUpdate family — **EXE + capture-validated**

Clean s2c packet decode (`tools/replay_s2c.py`) went **1166 -> 1237** this wave, and
(critically) this is now an HONEST count: the GhostAlwaysObjectEvent was previously
swallowing the object's initial `unpackUpdate` payload (so packets "decoded clean"
while silently desyncing). The whole GhostAlways NetObject `unpackUpdate` family is
now ported and bit-exact for every class that scopes in the golden capture except a
short, documented tail. Suite 204 pass.

### Control-object branch fixed (removed all 63 control-object blockers)
`GameConnection::readPacket` control-object branch (@ 0x459546): inner `readFlag`
(@ 0x421200, the function readFlag). If set: `readInt(14)` ghost id + the control
object's **`readPacketData`** (vtable slot 0xec). **ShapeBase::readPacketData @ VA
0x47e210** = Parent (GameBase @ 0x485790 = bare `ret 8`, 0 bits) + **two raw 4-byte
reads** (`read(4)` slot 4): a control-angle F32 and an F32 at [esi+0x950] = **8
bytes**. All ShapeBase subclasses share slot 0xec = 0x47e210. If the inner flag is
CLEAR the server sends the camera position as a **full Point3F (3 x read(4) = 12
bytes)** + a memcpy (0x421170, NOT a bitstream read) -- it is NOT a readCompressedPoint.
Fixed in `phases._read_control_header`.

### GhostAlwaysObjectEvent (classId 3) was STILL wrong (Wave-8 missed unpackUpdate)
`unpack` @ VA 0x5496a0: `readInt(14)` id; inline `readFlag` (@ 0x5496ee); if clear ->
create-by-name (0 further bits); if set -> `readClassId(NetClassTypeObject)` (6 bits)
+ create + the object's **`unpackUpdate`** (slot 0x4c, @ 0x54976b). The object's
INITIAL state is packed right in the event, length-less. Wave-8 stopped after the
classId, swallowing the unpackUpdate as the next event's framing -> the "classId 14"
desync at the first scoped ghost (packet 181). Fixed in
`events._read_ghost_always_object_event`; it dispatches to `ghosts.unpack_update`
(is_new=True) and records the ghost id->classId so the ghost SECTION treats it as
existing (via the new `EventManager.on_ghost_scoped` hook -> `phases._on_ghost_scoped`).

### NetObject `unpackUpdate` decoders ported (CFG-followed, capture bit-exact)

Bitstream/helper VAs used: `readFlag`(fn) 0x421200; readInt 0x420f60; readFloat
0x421000; readSignedFloat 0x4210b0 (reads n bits); readSignedInt 0x421570 (sign flag
+ n-1 bits = n total); readNormalVector 0x4216f0 (= readSignedFloat(bits+1) +
readSignedFloat(bits) = 2*bits+1 bits); Point3F(mathRead) 0x421240 = 12B; Box6F
0x421800 = Point3F + 3xread(4) = 24B; ColorF 0x4243f0 = 4xread(1) = 4B; PlaneF
0x4656d0 = 4xread(4) = 16B; readString 0x424230. Move::unpack 0x45b000 = 3x[flag;
if set readInt(16)] + 3xreadInt(6) + freeLook flag + 6 trigger flags.

| class | unpackUpdate VA | summary |
|---|---|---|
| ShapeBase (base) | 0x483d90 | GameBase parent; master flag; orientation block (readFloat6+readInt2+readNormalVec8); 4x image-trigger loop; 4x image-skin loop (flag; if not inner-flag readInt10); 8x mounted-image loop (datablock readInt10 + 5 flags + readInt3 + flag + 4xreadInt6); core-state block (damage flags; tagged-string skin @0x546fc0; per-node hide/scale loop readInt8 cnt + cnt*[flag;4xreadFloat8]; thread state readInt8 n + n*readInt8 + 20*readInt8); mount block (flag; flag; readInt14+readInt5) |
| Player / AIPlayer | 0x46e690 | ShapeBase parent + pos/vel(4xread4) + rot/energy block(2xreadFloat6 + opt 7xread4+readInt7+2xread4 + 2xread4) + readInt3 + state block(readInt8+2flag+opt readSignedFloat6+flag) + readInt8 + early-out flag + controlled-pose block(2flag+readInt3+opt readInt7+cpoint+opt(normalVec10+readInt13)+readFloat7+2xreadSignedFloat6+Move::unpack+flag) |
| Item | 0x45e5f0 | ShapeBase parent + flag(3flag + while-flag:Point3F + flag + read4 + readString) |
| StaticShape / ScopeAlwaysShape | 0x48df30 | ShapeBase parent + flag(Box6F+Point3F) + flag |
| Camera | 0x44e8b0 | ShapeBase parent + 1 flag (set-path reads nothing) |
| MissionMarker / RoomMarker / WayPoint / SpawnSphere / *Spawner | 0x463620 | ShapeBase parent + flag(Box6F+Point3F) |
| GameBase / Debris | 0x456da0 | flag; if set Point3F |
| Sun | 0x55d4e0 | flag; if set Point3F + 8xread(4) |
| SimpleNetObject | 0x4c2fc0 | one readString |
| AudioEmitter | 0x44d010 | parent0; leading flag; then ~19 fnFlag(0x44c3a0)-gated fields: 0x10000 Box6F; 1/2 flag+readInt10; 4 readString; 8/0x20/0x40/0x40000 flag; 0x10..0x20000 read(4) (0x1000 = 3xread4); fnFlag reads 1 bit each |
| fxShapeReplicator | 0x4aef80 | parent0; mask flag; Box6F+3xreadInt32+readString+4xreadInt32+4xPoint3F+readSignedInt32+5flag+readSignedInt32+flag+Point3F+3flag+readInt32+ColorF+flag |
| Trigger | 0x48ffb0 | GameBase parent + Box6F + Point3F + polyhedron(readInt32 N + N*Point3F + readInt32 M + M*PlaneF) |
| PhysicalZone | 0x4667d0 | parent0 + flag(16-float matrix(64B) + Point3F + polyhedron) |
| PathedInterior | 0x5157a0 | GameBase parent + flag(readString+read4+Box6F+Point3F+read4) |
| TSStatic / fxShapeReplicatedStatic | 0x4917e0 | parent0 + matrix(64B) + Point3F + readString |
| InteriorInstance | 0x507b50 | parent0 + flag(read4+readString) + flag + flag + matrix(64B) + Point3F + flag(readString) [see TODO below] |
| volumeLight | 0x4c1700 | parent0 + Box6F + read(1)byte + readString + 6xread(4) + 2xColorF |
| MissionArea | 0x4620a0 | flag; if set RectI(4xread4) + 2xread(4) |
| fxBrickBatcher | 0x4a0d60 | `jmp 0x485790` (bare ret) -> 0 bits |

### Result + remaining blockers
`tools/replay_s2c.py`: **1237** clean (was 1166). First hard stop: packet 169/seq 132,
`fxFoliageReplicator` (classId 42) embedded in a GhostAlwaysObjectEvent -- unported.
Remaining histogram: `event classId 15` x29 (a NON-ghost event-section desync that
predates this wave -- next RE target, see below), `classId 3` x6 (= unported tail
ghost classes fxFoliageReplicator/fxGrassReplicator/WaterBlock), classId 6 x2,
classId 11 x1. Remaining unported ghost classes (all low-frequency in the capture):
fxFoliageReplicator, fxGrassReplicator (both big mask-gated blocks like
fxShapeReplicator -- need the per-field inline-readFlag vs overflow-check disambiguated),
HoverVehicle (0x4c9aa0), FlyingVehicle (0x4c7c10), WheeledVehicle, Lightning (0x4b3d80),
Marker (0x551d30), WaterBlock (0x56f300), Precipitation (0x4bbf70), Projectile (0x476bf0),
Splash, fxLight/fxSunLight/fxDTSBrick, twSurfaceReference, Sky/TerrainBlock,
ParticleEmitterNode/PathCamera/VehicleBlocker.

### Two open TODOs (left raising / documented, per the never-guess rule)
1. **InteriorInstance trailing mask** (@ 0x507d16/0x507d7e): the exe has two further
   `flag; if set readInt(10)` blocks after the readString, but reading them desyncs
   13 capture packets while only fixing 1 (the cid15 at pkt216). The GhostAlways
   *initial*-scope updates in this capture do not carry those two mask bits -- there
   is an outer gate on that tail not yet resolved. `_unpack_interior_instance` stops
   before them (empirically the more bit-exact form). Resolve the outer gate before
   adding them.
2. **`event classId 15` x29**: a pure event-section desync (NOT ghost-related; lastGhost
   context is empty). Present since Wave-8. The event chain at pkt216 is
   GhostAlwaysObjectEvent(3) -> ConnectionMessage(0) -> bogus 15; tied to the
   InteriorInstance bit count (TODO 1). Likely the same root cause.

### Gate status (live)
GhostAlwaysDone / Phase3 / StartLogin are still withheld until the client consumes
ALL ghost-always objects; the unported tail above must be finished first. No live
connection made this wave (all work offline against the golden capture). Login/chat
wiring remains complete and will fire the instant decode reaches StartLogin.

---

## Wave-10 — remaining tail ghost classes ported; InteriorInstance F/G gate proven absent for this capture — **EXE + capture-validated**

Clean s2c decode (`tools/replay_s2c.py`) **1237 -> 1243**. Suite **219 pass**.
Seven more NetObject `unpackUpdate` decoders ported (all CFG-followed in
`AgeOfTime.exe.original`, slot 0x4c); after them, EVERY ghost class that scopes
in the golden capture now has a decoder (the replay histogram has **no** "ghost
unpackUpdate not ported" entries -- all remaining blockers are event-section
desyncs, see below).

### NetObject `unpackUpdate` decoders ported this wave

| class | unpackUpdate VA | summary |
|---|---|---|
| fxFoliageReplicator | 0x4a5560 | parent(ret0); master flag (je 0x4a5c2b); then fully sequential: Box6F; flag; 4×read(4); readString; 9×read(4); 2×read(1)byte; read(4); 2×read(1)byte; 7×read(4); 2×flag; 4×read(4); 2×flag; 3×read(4); 5×flag; read(4); 2×flag; read(4); ColorF |
| fxGrassReplicator | 0x4a9dd0 | same shape as fxFoliage: master flag (je 0x4aa5f7); Box6F; flag; 4×read(4); readString; 8×read(4); 2×read(1); read(4); 2×read(1); 7×read(4); 2×flag; 4×read(4); 2×flag; 3×read(4); 6×flag; read(4); 2×flag; read(4); 3×ColorF; 3×flag; read(4); flag; read(4) |
| WaterBlock | 0x56f300 | NO master flag. Box6F; Point3F; 7×readString (5 explicit + 2-iter loop @0x56f39c); 6×read(4); read(1); flag→readInt(10); read(1); 12×read(4); ColorF; read(4); flag |
| Marker | 0x551d30 | parent(ret0) + Box6F. No mask. |
| Lightning | 0x4b3d80 | parent=GameBase::unpackUpdate; master flag (ret if clear); Point3F; Point3F; 10×read(4); read(1)byte; read(4) |
| HoverVehicle | 0x4c9aa0 | Vehicle::unpackUpdate (0x4cf130) + unconditional readInt(3) |
| Sky | 0x55c1b0 | NO parent. Master flag gates a "settings" block (readString; 4×read(4) incl a U32 cloud-layer COUNT; 2×read(1); 3×read(4); 2×read(1); read(4); flag; COUNT×[6×read(4)+read(1)]; 3×[readString+2×read(4)]; 3×read(4); read(4); flag→5×read(4)); BOTH master states then fall into a COMMON tail of 7 ``flag; if set {reads}`` blocks (read(1),read(1),2×r4,2×r4,3×r4,4×r4,3×r4). |

`Vehicle::unpackUpdate` @ **0x4cf130** (shared base for HoverVehicle/FlyingVehicle/
WheeledVehicle): ShapeBase parent; flag A (1 bit); flag B MASTER (``jne`` -> return
if SET); readFloat(9)×2; Move::unpack; flag C → readCompressedPoint + PlaneF(16) +
2×Point3F + readFlag; flag D → readFloat(8).

### FlyingVehicle (0x4c7c10) — LEFT UNPORTED (data-dependent gate, never-guess)
FlyingVehicle = Vehicle::unpackUpdate then ``cmp [this+0x274], 0; je end`` gating a
``flag + readInt(3)``. The gate reads a NON-wire member field (+0x274) set elsewhere,
not a bitstream bit, so it cannot be reproduced deterministically from the stream
alone. It appears only twice in the capture (downstream of the InteriorInstance
desync, so never actually reached cleanly). Left raising with the class name.

### InteriorInstance trailing F/G blocks — PROVEN ABSENT for every capture update
The Wave-9 TODO 1 (the two trailing ``flag; readInt(10)`` blocks @0x507cd4/0x507ce6
in the A-set path) is now characterized: the **full CFG** (0x507b8d) is
`flag A → {A-set: read4,string,flagC,flagD,matrix(64B),Point3F,flagE,string,
flagF→int10,flagG→int10} | {A-clear: flagX→matrix+Point3F, flag}`. Instrumented
against the capture: **all 134 InteriorInstance updates are A-set with F==G==0**,
and reading the F/G blocks (even though each is just a clear 1-bit flag) REGRESSES
clean decode 1243→1236 (it fixes pkt180 but breaks 7 others). Conclusion: the F/G
mask bits are **not on the wire** for these initial-scope updates -- there is a
genuine outer gate (likely server-side scope/transmit, tied to the interior's
render-mode or a datablock flag) that suppresses them, with no distinguishing bit
in `unpackUpdate` itself. `_unpack_interior_instance` therefore stops before F/G
(the empirically bit-exact form for every capture update). This is the SAME root
cause as the residual `event classId 6/11/15` desyncs (all occur in the event
chain immediately following an InteriorInstance A-set update on packet 180+), so
resolving the F/G gate would clear all of them at once.

### Result + remaining blocker
`tools/replay_s2c.py`: **1243** clean. First hard stop: **packet 180/seq131,
event classId 6** (PathManagerEvent) -- a desync in the event chain that follows
the first InteriorInstance A-set update, root-caused to the InteriorInstance F/G
outer gate above (the ghost itself decodes; the desync is the missing/extra mask
bit). Remaining histogram: `event classId 15` ×29, `classId 6` ×2, `classId 11`
×1 -- ALL the same InteriorInstance-gate family. No ghost-class blockers remain.
Login NOT reached live (server withholds GhostAlwaysDone/Phase3/StartLogin until
the full GhostAlways stream is consumed bit-exactly; the InteriorInstance gate
blocks that). No live connection made this wave (offline against the golden
capture). Login/chat wiring remains complete and will fire the instant decode
reaches StartLogin.

NEXT: resolve the InteriorInstance F/G outer gate. Candidates to check: (a) whether
the gate is the interior's `detailLevel`/`mirrorSurface` datablock field (read it
from the matching SimDataBlockEvent and condition F/G on it); (b) re-trace whether
flag E (+0x2ac) or a flag C/D *value* (not just presence) gates the F/G blocks
in the exe via a data-flow trace, not just the control-flow CFG; (c) capture a
second login session and diff the InteriorInstance byte ranges to see if F/G ever
appear. Until then F/G stay omitted (the bit-exact form for this capture).

---

## Wave-11 — InteriorInstance F/G gate SOLVED (write-side trace); fxGrass/fxFoliage flag bugs fixed; new blocker = fxShapeReplicator string — **EXE-confirmed**

### The Wave-10 "F/G absent" conclusion was WRONG. F/G are ALWAYS on the wire.
Disassembled the WRITE side **`InteriorInstance::packUpdate` @ VA 0x5084a0**
(NetObject vtable **slot 0x48**, found via the InteriorInstance vtable @ 0x60b5bc;
slot 0x4c = unpackUpdate 0x507b50, slot 0x50 = a shared SimSet helper 0x54c640,
NOT packUpdate -- the pack/unpack pair here is 0x48/0x4c). packUpdate's InitMask
branch (`writeFlag(mask & InitMask)` @ 0x5084e3, then `je 0x508602` to the
non-init branch) writes, UNCONDITIONALLY and in this exact order:

```
write(mCRC)                          (slot 8, 4 bytes)        @0x508505
writeString(mInteriorFileName)       (slot 0x20)             @0x508518
writeFlag([+0x278] mShowTerrainInside)  C                    @0x508526
writeFlag([+0x290])                     D  (AoT-added)       @0x508536
mathWrite(mObjToWorld)               64B matrix              @0x508543
mathWrite(mObjScale)                 Point3F 12B             @0x508550
writeFlag([+0x2ac] mAlarmState)         E                    @0x508563
writeString([+0x268] mSkinBase)      (slot 0x20)             @0x508578
writeFlag([+0x280] mAudioProfile!=0)    F; if set writeRangedU32(...10b)  @0x508589/0x5085b5
writeFlag([+0x284] mAudioEnvironment!=0) G; if set writeRangedU32(...10b) @0x5085c8/0x5085f8
jmp 0x508829 (return)                -- AoT DROPPED stock's trailing mUseGLLighting flag
```

So **F and G are `writeFlag(member != NULL)` -- always-written 1-bit flags, with
NO outer gate.** They are present on the wire for EVERY initial (A-set) update;
only the trailing `readInt(10)` id is conditional on the flag. The read side
(`unpackUpdate` @ 0x507b8d, fully re-traced) mirrors this exactly: the A-set path
is read4, readString, flagC[+0x278], flagD[+0x290], matrix, Point3F,
flagE[+0x2ac], readString, flagF[+0x280]->int10, flagG[+0x284]->int10, return.

**The Wave-10 regression (reading F/G "broke 7 packets") was a MEASUREMENT
ARTIFACT of a WRONG unpackUpdate transcription.** The prior `_unpack_interior_instance`
gated mCRC/mInteriorFileName behind flag A, then dropped mAlarmState + the
*unconditional* mSkinBase string AND F/G -- a completely different (wrong) field
order that happened to leave the stream "clean" on packets whose downstream
ghosts/events it luckily realigned. Replacing it with the bit-exact A-set
sequence above FIXES capture pkt180/216/231 (the classId-6/15 desyncs the task
targeted) -- the II ghost now decodes correctly, including F/G.

### fxGrassReplicator (0x4a9dd0) + fxFoliageReplicator (0x4a5560): dropped flags
Correcting the II surfaced two PRE-EXISTING transcription bugs in the replicator
unpackUpdate decoders (they were never reached cleanly before because the wrong
II masked them). Both had **FOUR interleaved 1-bit flags dropped** from the
byte-read run (grass @0x4a9fc6/fe6/01e/03e; foliage @ the analogous sites) and
foliage additionally had 9 read(4) where the disassembly has 8. Re-derived both
from the CFG (sizes: `push 4`=read(4); `push ebx`(==1) + test al/setne/mov byte
= read(1) byte; `setne` = inline readFlag). Fixed. Datablock decode 319->320,
s2c clean 1235->1236.

### NEW blocker: fxShapeReplicator (0x4aef80) under-reads by exactly 17 bits
First hard stop is now **pkt181/seq132**: a GhostAlwaysObjectEvent (classId 3)
scopes ghost id 16319 as classId 46 = **fxShapeReplicator** (initial unpackUpdate),
and the following SimDataBlockEvent then reads a garbage classId. Brute-forcing
the fxShapeReplicator consume length against the capture: the unique value that
makes the next SimDataBlockEvent decode cleanly (classId 11, datablock
LightningData id 12, index 73, total 657) is **2014 bits**, but the current
decoder consumes **1997** -- a **17-bit deficit**. The full call sequence
(master flag; Box6F; 3xreadInt32; readString; 4xreadInt32; 4xPoint3F;
readSignedInt32; 5xflag; readSignedInt32; flag; Point3F; 3xflag; readInt32;
ColorF; flag) was re-verified instruction-by-instruction against 0x4aef80..0x4af2f0
and matches the decoder EXACTLY; every readInt is `push 0x20`(32b), Box6F=24B,
readSignedInt(0x20)=32b (0x421570 = 1 sign + (n-1) magnitude). The 17 missing bits
are NOT in any field block found by static CFG, and the variable-length readString
self-terminates (Huffman) at 962 bits (a 119-byte binary blob -- already garbage,
i.e. its START is misaligned). 17 == `readNormalVector(8)` (2*8+1) but no such
call exists in the function. The control header (36b), event framing (to bit 85),
and GhostAlwaysObjectEvent envelope (readInt14 id + flag + readClassId6, to bit
106) were ALL verified bit-exact, so the 17-bit slip is inside fxShapeReplicator's
own unpackUpdate yet invisible to static disasm. This is the precise next target:
needs runtime instrumentation of the real client's fxShapeReplicator::unpackUpdate
(hook 0x4aef80, log BitStream::getCurPos before/after each field) OR a second
capture to diff the exact bit offsets. (This blocker is INDEPENDENT of and
predates the II gate -- pkt181 desynced identically under the old II.)

### Net status
s2c clean 1243(old, inflated by the wrong II)->**1236** (honest; +3 II packets
fixed, but the now-correct II exposes the pre-existing fxShapeReplicator desync on
~8 downstream packets that the wrong II had been masking). Datablocks 319->**320**.
Suite **229 pass** (+3 InteriorInstance bit-exact tests, +grass/foliage already
covered by master-clear tests, + test_datablocks wrapper hardened vs out-of-range
classId). Login still NOT reached live (decode does not reach GhostAlwaysDone ->
Phase3 -> StartLogin; the server withholds them until the full GhostAlways stream
decodes bit-exactly, which the fxShapeReplicator 17-bit slip blocks). No live
connection made (per etiquette: offline until decode reaches GhostAlwaysDone).
Login/chat/Node-RED wiring remains complete and will fire the instant decode
reaches StartLogin.

---

## Wave-12 — fxShapeReplicator 17-bit slip SOLVED by RUNTIME INSTRUMENTATION (winedbg/gdb); root cause = a hidden sign-flag in mathRead "Box6F" (VA 0x421800) — **RUNTIME-CONFIRMED**

The static call sequence (waves 9-11) matched the binary instruction-by-instruction
yet under-read by 17 bits, because the slip was inside a *helper* the static
transcription trusted: **the `0x421800` "Box6F" mathRead is 193 bits, not 192.**

### Method that worked (winedbg --gdb, in the docker wine image)
- `winedbg` alone (native debugger) lacks gdb-style per-breakpoint command
  lists, so use **`winedbg --gdb <pid>`** which proxies to a real **gdb**
  (apt-installed inside the `skylord123/aot-wine-x11-novnc-docker:v0.0.5`
  container; the host has no gdb and no win32 prefix). gdb's `break *0xVA` +
  `commands/silent/printf/cont` blocks give automated per-field logging.
- **BitStream curPos (bit cursor) member offset = `[this+0x0C]`** (read directly
  from `readBits` @ 0x420e80: `mov eax,[ecx+0xc]` at entry, `[ecx+0xc]+=numBits`
  at exit; buffer ptr @ +0x08, error byte @ +0x14, buffer bit-length @ +0x18).
- In `fxShapeReplicator::unpackUpdate` (0x4aef80) the **BitStream `this` is held
  in `ESI` for the whole function** (`mov esi,[esp+0x4c]` after the pushes), so a
  single deref `*(int*)($esi+0xc)` logs the cursor at every call site. The fx
  region is byte-identical between `AgeOfTime.exe` (running) and
  `.exe.original` (RE'd), so the static VAs map 1:1.
- **Launch gotcha:** bare `wine`/`winedbg --gdb AgeOfTime.exe` page-faults to
  0x0 during mission load (the documented bare-wine crash) and also stops on
  `kernel32!IsBadReadPtr`'s deliberate probe-SIGSEGV. Run the game under the
  PROVEN supervisord harness (the image's normal entrypoint = `wineconsole
  AgeOfTime.exe`, which survives load) and **attach** `winedbg --gdb <decimalPID>`
  to it, with `handle SIGSEGV nostop noprint pass` so IsBadReadPtr probes don't
  stop the game. The winpid from `winedbg --command "info proc"` is HEX; convert
  to decimal for `winedbg --gdb`. fxShapeReplicator only runs during the
  GhostAlways *initial scope*, so force one re-scope by breaking the connection
  (kill+let `keepOnlineLoop` reconnect, or restart the process) while gdb stays
  armed. Tools: `tools/fxshape_trace.gdb` (breakpoint+log script),
  `tools/attach_live.sh` / orchestration in `/tmp/aottrace/`.

### The per-field runtime trace (instance #1, start bit 106 — matches the capture)
```
106 master flag (+1) -> 107
107 "Box6F"(0x421800)  +193  -> 300     <-- STATIC SAID 192. THE BUG.
300 readInt(32) x3            -> 396
396 readString         +357  -> 753     (was misread as 962 under the 1-bit slip)
753 readInt(32) x4 ; Point3F x4 ; readSignedInt(32) ; 5 flags ;
    readSignedInt(32) ; 1 flag ; Point3F ; 3 flags ; readInt(32) ; ColorF ;
    1 final flag                         (all exactly as modeled)
```
All four observed live instances gave Box6F = **193** and readString = 357/302/
297/302 (variable). The cursor math closes to the bit with Box6F=193.

### Root cause: `mathRead 0x421800` reads a trailing SIGN FLAG
`0x421800` is NOT a plain 24-byte read. Disassembly (cross-checked by the runtime
cursor): `Point3F (3xread4=96b) + 3xread4 (96b)` then it computes
`sqrt(x^2+y^2+z^2)` and at **0x4218ad-0x4218c3 reads one inline readFlag (a sign
bit) and negates the magnitude (`fchs`) if set** = **192 + 1 = 193 bits**. The
missing sign flag left the cursor 1 bit short entering the variable-length
`readString`, which then self-terminated (Huffman) at a garbage offset and ran
far past the true end — the cumulative misread that *presented* as a 17-bit
deficit (962 vs 357 on the string alone). FIX in `ghosts._read_box6f`: read 24
bytes **+ 1 sign flag**. (This helper is shared by Trigger/Marker/StaticShape/
PhysicalZone/volumeLight/WaterBlock/audioEmitter/fxFoliage/fxGrass etc.; all were
1 bit short but those classes scope only at/after pkt181, so the fix raises —
never lowers — the clean count.)

### Result
`tools/replay_s2c.py` clean s2c **1236 -> 1238**; the pkt181/seq132
fxShapeReplicator blocker is GONE; first hard stop moved to **pkt305/seq256** (a
NEW, deeper, *post-login* blocker — see below). Suite **229 pass** (updated
`test_marker_box_only`: Box6F-only Marker = 193 bits). Datablocks unchanged.

### NEW blocker (post-login, NOT relevant to the bot): control-object Player::readPacketData
pkt305's event section reads a bogus classId 15 because its **control header**
mis-sizes the control-object branch. By pkt305 the *real client* has spawned and
is being CONTROLLED, so the control-object update calls **`Player::readPacketData`
(vtable slot 0xec = `0x4699d0`)**, which OVERRIDES the shared `ShapeBase::
readPacketData` (0x47e210, 8 bytes) with a much larger payload (ShapeBase 8B +
readInt(3) + flag?readInt(7) + a Point3F(12B) + more conditional blocks). Our
`phases._read_control_header` only models the 8-byte ShapeBase form. **This does
NOT affect the headless bot**: the bot never spawns/gets controlled, so the
server never sends it a control-object Player update (the control flag stays 0
for a logged-out, unspawned client). It only appears in the golden capture
because the real client played the game. If a spawned bot is ever needed, port
`Player::readPacketData` @ 0x4699d0 (runtime-instrument it the same way). The
login-relevant verbs (LoginSuccess + inventory/gold/abilities) decode at pkt333
*after* this point; the capture shows NO `clientCmdStartLogin` — the real client
logged in earlier in its own (spawned) flow.

---

## Wave-14 — the "213 ghost-event stall" definitively localized to the SERVER's reliable-event ack accounting; client ack/notify/event layer PROVEN correct by capture diff — **CAPTURE-CONFIRMED**

Goal: crack the 213 stall that blocks invalid-login detection (the WarningBox is
gated on the full mission load completing -> MissionStart). Approach: the primary
capture-diff the task specified (decode c2s packet HEADERS + event sections of the
BOT vs the REAL client during heavy event reception). New tools (all in `tools/`):
`ack_diff.py` (per-packet header dump: seq/ack/type/ackByteCount/ackMask/len),
`ack_coverage.py` (simulates the SERVER's mHighestAckedSeq + notify walk over the
bot's c2s acks to find any s2c seq the server would see as dropped),
`decode_c2s_body.py` (c2s control header + move stream + event-presence),
`decode_c2s_events.py` (the events the CLIENT posts), `/tmp/retx.py` (event-seq
retransmission histogram). Fresh bot bad-login capture via the relay:
`/tmp/bot_badlogin_relay.jsonl` (`tools/captures/bot_session_213stall.jsonl` is the
earlier one).

### What the capture diff PROVED about the client (all clean)
- **Packet header ack accounting is byte-identical to the real client.** Both keep
  `ackByteCount=1`, `ackMask=0xff` once warmed up; the bot's seq/ack fields advance
  monotonically with the same cadence. The Wave-13 "ackByteCount must grow / mask
  truncation" hypothesis is FALSE — the real client also caps at 1 byte.
- **`ack_coverage.py`: the bot positively-acks EVERY s2c DATA packet seq the server
  sends — ZERO gaps.** Server's mHighestAckedSeq reaches 1345 and the
  consecutive-delivered walk reaches 1345 with no holes (real client: 2049, also no
  holes). So the server KNOWS each of its event-bearing packets reached the bot ->
  `eventPacketReceived` (netEvent.cc:120) runs for all of them and SHOULD advance
  `mLastAckedEventSeq`.
- **Ack lag is tight:** worst (s2c-received minus reported-ack) = 1 for the bot vs 2
  for the real client. The bot is not under-acking.
- Ruled out live (fresh captures, full suite green): the faithful move stream
  (below), and eager-Phase3-ack on/off — NEITHER changes the stall (still exactly
  213 GhostAlwaysObjectEvents, 135 big s2c packets, identical received clientCmd set).

### The SMOKING GUN (`/tmp/retx.py` on the bot bad-login capture)
The bot's s2c stream carries **566 ordered events for 249 unique seqs**. Breakdown:
- event seqs sent **exactly once**: 112 (the early datablock/setup + first ghost batch),
- event seqs **RE-SENT 2-3x**: 137, spanning seqs ~**90..245**,
- event seqs past ~245: never sent.

This is the engine's reliable-event window behaviour seen from the wire: the server
re-sends the unacked window `[mLastAckedEventSeq+1 .. mLastAckedEventSeq+126]`
(netEvent.cc:203 `mSendEventQueueHead->mSeqCount > mLastAckedEventSeq + 126`)
indefinitely and **cannot advance its `mLastAckedEventSeq` past ~119** (245-126),
so it stops sending new ghost events. The 304 GhostAlwaysObjectEvents posted by
`activateGhosting` (netGhost.cc:838) therefore only get ~213 onto the wire before
the window jams. So 213 ≈ (highest sent ordered seq ~245) − (first ghost seq ~32),
NOT a flat "87+126" — but the mechanism is exactly the reliable-event window.

### The CONTRADICTION -> the cause is SERVER-SIDE and opaque to client RE
`eventPacketReceived` (netEvent.cc:120-156, the SERVER's send-side event-ack) advances
`mLastAckedEventSeq` purely from packet **notify** — i.e. when the server's
event-bearing packets are positively acked by the client (`handleNotify(true)` ->
`eventPacketReceived(notify)` -> consecutive `mNotifyEventList` walk). We PROVED the
bot positively-acks every server packet with zero gaps. So in stock TGE the server's
`mLastAckedEventSeq` WOULD advance fully and the stream WOULD complete. It does not.
Therefore the AoT **server** has a fork-specific divergence in how it ties
packet-notify to the reliable-event ack pointer (or an unrelated server-side scope/
load gate that throttles `activateGhosting`'s posting). This is in the AoT *server*
(remote / production; its scripts + any custom netcode are not in our files and not
in `AgeOfTime.exe`, which is the *client*). Static client RE and the capture diff have
reached their ceiling — exactly the wall Wave-4 and Wave-13 hit, restated with hard
proof that the client side is not at fault.

### Why VALID login still works but BAD-login detection does not
- VALID: `clientCmdLoginSuccess` arrives ~2.3s after `GhostAlwaysStarting` (LIVE
  re-confirmed this wave), NOT gated on the ghost stream. Eager login at Phase2
  (on_phase2_acked) gets the success.
- BAD: the `clientCmdWarningBox("Wrong Password!" / "Character does not exist!")` is
  gated on the FULL load completing (bad_login.jsonl: WarningBox arrives only after
  Phase3 -> MissionStart). The 213 stall prevents the load from completing, so the
  WarningBox never arrives. LIVE this wave: bad creds -> Phase1 -> GhostAlwaysStarting
  -> (stall) -> 90s with NO WarningBox, connection stays up, bot stays <Logged Out>.

### Real fix shipped this wave (correctness, not the stall): faithful move stream
`phases._write_moves` now writes `startMoveId = self.last_move_ack` (the server's
echoed move-ack) and a re-packed unacked-move window (capped at MaxMoveCount=30),
matching the real client's `moveWritePacket` (capture startMoveId trace 0,0,2,3,5,…
tracks mLastMoveAck, NOT a blind +count). The old blind ever-advancing startMoveId
made the server's `skip = mLastMoveAck - start` (gameConnectionMoves.cc moveReadPacket)
snap `mLastMoveAck = start` every packet. This is a genuine divergence fix (the server
move pipeline now settles like the real client's) but it does NOT unblock the 213
stall (re-confirmed live). Suite 239 -> 240 (added move-ack-tracking + MaxMoveCount-cap
regression tests in test_phases.py).

### Decisive next step (needs the AoT SERVER, not client RE)
The only ways left to resolve the server-side reliable-event jam:
(a) run a LOCAL AoT dedicated server (`AgeOfTime.exe -dedicated`, main.cs:82 — SAME
binary, so the server-read code is the one we RE'd) under Wine + winedbg-instrument
its `eventPacketReceived` / `mLastAckedEventSeq` / `mSendEventQueueHead` to see what
fails to advance — but base/server/ here ships ~no serverCmd scripts, so a local
server needs login/mission stubs; or
(b) accept that production's server gates this and implement bad-login detection
heuristically (e.g. "login sent + neither LoginSuccess nor WarningBox within N s after
GhostAlwaysStarting -> probable bad login") — a behavioural inference, not the literal
WarningBox. NOT shipped (the task wants the real WarningBox).

---

## Wave-15 — the "213-ghost stall" CRACKED: it was a CLIENT-side ghost-handshake bug, NOT a server-side reliable-event jam. Full mission load now completes -> WarningBox / LoginSuccess. **LIVE-confirmed.**

Waves 13-14 concluded the 213 stall was a server-side reliable-event-window jam
(`mLastAckedEventSeq` stuck) opaque to client RE. **That conclusion was WRONG.**
This wave re-ran the capture diff + disassembly with the explicit framing "the
REAL client against the SAME server does NOT stall, so find the client-side
difference," and found it.

### What was rigorously RULED OUT (so the prior "server-side" theory is dead)
- **No wire-format reliable-event-ack field exists** in either direction.
  Disassembled `eventReadPacket` (0x548c70) and `eventWritePacket` (0x548ab0):
  the event section is presence-flag | shortcut|7-bit-seq | classId | payload,
  with NO leading/trailing "highest-received-event-seq". Event reliability is
  100% packet-notify-driven (stock TGE), confirmed against netEvent.cc:120-156.
- **The bot's packet acking is impeccable** (better than the real client). Across
  the stall capture: acks every s2c DATA packet gap-free; server-perspective ack
  distance is **0** for essentially every packet (the bot replied to each received
  packet immediately) vs the real client's 0..25; zero mask-window (8-bit ackMask)
  drops; send window never exceeds 6/30. So the server's `mLastAckedEventSeq`
  *would* advance under stock TGE -- yet the server kept re-sending events. That
  contradiction means the stall is NOT the reliable-event window at all.
- **Rate/flood**: the OLD reflexive sender (a DATA packet per received packet +
  100ms keepalive) burst the bot to 40-45/s (165 back-to-back <=5ms packets) over
  the real client's hard 32/s ceiling. Fixed (below) to a 32ms tick -> 30/s, no
  bursts. This did NOT clear the stall (so FloodProtection was not the cause), but
  it is a genuine correctness fix and is kept.

### ROOT CAUSE (the actual client-side bug)
The 213 objects the bot receives are the **complete** scoped ghost-always set
(ids 16383..16080, ending in Sun/Sky/MissionArea -- a coherent full set; the
`GhostAlwaysStarting ghostCount=304` is `ghostAlwaysSet->size()`, the TOTAL set,
not the scoped count). After the burst the server **waits for the client's
`ReadyForNormalGhosts(mGhostingSequence)`** before it completes the load
(netGhost.cc:746 -> sets mGhosting, then Phase3 -> MissionStart -> the login
response). The stock client sends ReadyForNormalGhosts from
`loadNextGhostAlwaysObject` once its ghost-always save list drains, which is
triggered by receiving a **post-stream `GhostAlwaysDone`** connection message
(netGhost.cc:735-744). **The AoT server NEVER sends a post-stream GhostAlwaysDone
to a headless client** (LIVE: the only GhostAlwaysDone it sends is a stale one
*before* GhostAlwaysStarting; raw connmsg trace = GADone(stale), DBDone,
GAStart(304), then nothing). The bot was gating ReadyForNormalGhosts on that
never-arriving GhostAlwaysDone -> it never sent ReadyForNormalGhosts -> the load
stalled forever right after the ghost-always burst. The "server re-sends events
~90-245" the prior waves saw was the server re-sending the (small, already-acked)
GhostAlways window while idling -- a symptom of waiting for ReadyForNormalGhosts,
not a window jam.

**PROOF (LIVE):** injecting `ReadyForNormalGhosts(seq=1)` by hand at 213 ghosts
immediately switched the server to normal ghosting (the bot started receiving
normal-ghost updates), and with the Phase3-order fix below it then sent
clientCmdMissionStartPhase3 -> clientCmdMissionStart -> the login response. The
real client breaks the same cycle implicitly: it registers each ghost-always
object + completes the file-download handshake (it sends ~21
FileDownloadRequestEvents for shapes/sounds it lacks), and that culminates in
its ReadyForNormalGhosts. A headless bot has no objects/files to register, so it
must detect burst completion itself.

### THE FIX (aotbot/, all LIVE-validated)
1. **phases.maybe_send_ready_for_normal_ghosts()** -- after GhostAlwaysStarting,
   `_on_ghost_scoped` timestamps each ghost-always object; when the burst has been
   idle for `ghost_always_idle_timeout` (0.4s) the bot sends exactly one
   `ReadyForNormalGhosts(mGhostingSequence)`. Called from client._read_body after
   every received packet (the server keeps sending packets while waiting, so the
   idle check keeps firing). Idempotent via `_ready_for_normal_sent`; the
   GhostAlwaysDone handler shares the same flag so we never double-send.
2. **phases._on_phase2: removed the eager Phase3 ack.** With ReadyForNormalGhosts
   now correctly completing the burst, the server actually SENDS a real
   clientCmdMissionStartPhase3 (LIVE), which `_on_phase3` acks -- matching the real
   client's order (capture: Phase2Ack seq91, ReadyForNormalGhosts seq149, Phase3Ack
   seq167). The eager ack was a Wave-12..14 crutch for the then-unsolved stall.
3. **netconn: fixed-32ms-tick sender (correctness).** `_process_raw_packet` no
   longer fires a DATA packet per received packet; the 32ms keepalive tick
   (KEEPALIVE_INTERVAL 0.1 -> 0.032) carries the (batched) ack at the real client's
   ~31/s rate. We still flush immediately when events are queued.

### LIVE GATE RESULTS (production 45.148.165.55:28000, polite one-shot sessions)
- **Valid login + chat** ("Mr Poopy Butthole"): burst idle 213 ->
  ReadyForNormalGhosts -> Phase3 -> MissionStart -> **clientCmdLoginSuccess** ->
  "Mr Poopy Butthole logged in." -> chat sent -> clean disconnect.
- **Wrong password** (test/wrongpass, CREATE off): ... -> MissionStart ->
  **clientCmdWarningBox("Wrong Password!","Oops")**.
- **Character does not exist** (fresh name, CREATE off):
  **clientCmdWarningBox("Character does not exist!","Oops")**. (Also observed
  "Invalid Name" for a server-rejected name format.)
- **Auto-create** (fresh "Zaphodbot", CREATE on): WarningBox "Character does not
  exist!" -> auto registerNewUser -> **clientCmdLoginSuccess** + "New character
  created: Zaphodbot" -> LOGGED IN (a REAL character was created).

Suite 241 -> **243** (+2 ReadyForNormalGhosts idle-completion tests; updated the
phase2-no-eager-phase3 test). No relay/docker/winedbg was needed -- the crack came
from capture diff (decode_c2s_events) + disassembly + targeted LIVE behavioral
probes. The invalid-login detection (the task's final blocker) now works.

---

## Wave-16 — live-entity telemetry: value extraction + scoped-object registry + GameBase two-flag fix. **Replay-validated; live-decoded.**

Goal: track scoped game objects and expose their attributes (position / transform /
rotation / shape name / class) behind an `AOT_TRACK_OBJECTS` on/off flag, validated
bit-exact against the captures + live.

### Root correctness fix that unblocked everything: GameBase::unpackUpdate reads TWO masks
**`GameBase::unpackUpdate` @ VA 0x456da0 reads TWO mask-gated blocks, not one** (the
prior `ghosts._unpack_game_base` + the inlined ShapeBase parent read only the first):
- flag (@0x456dd2): if set -> Point3F handed to `[vtbl+0x74]`. **This vtable slot is
  setScale on AoT's SceneObject** -- the values observed LIVE are the 0.9..1.1 character
  scale / the 1,1,1 default, NOT a world position. Recorded as `scale`.
- flag (@0x456e2b): if set -> `readInt(getBinLog2(0x400)=10)+3` = the object's **datablock
  id** (the 0x4244e0/0x424510 getNextPow2/getBinLog2 pair, `add eax,3`
  DataBlockObjectIdFirst, then a Sim::findObject lookup @0x456e73).

`_unpack_shape_base` now CALLS `_unpack_game_base` (matching the exe's `call 0x456da0`
@0x483dbb) instead of inlining a single flag. This second (datablock) flag is part of
every GameBase-derived unpackUpdate; the prior code happened to align on the captures
only because the datablock flag was usually clear, but it desynced deeper updates.
Result: `replay_s2c` real_login first hard stop moved from pkt336 to **pkt822**, clean
count 1269 -> **1274**; bad_login 1755 -> first-blocker-cleared **2055** (the WarningBox
decodes with ZERO desync); bot_session_postfix unchanged at 2461.

### New ghost classes ported (CFG-followed, capture bit-exact)
- **Projectile::unpackUpdate (0x476bf0)** (classId 23): GameBase parent; flag A ->
  [readCompressedPoint position; flag B -> readNormalVector(10)+readInt(13) velocity;
  readInt(12) (getNextPow2(0x1000)); flag C -> readInt(15) (getNextPow2(0x4001))+readInt(3)];
  flag D -> Point3F + Point3F + read(4). Cleared the real_login pkt581 blocker.
- **Precipitation::unpackUpdate (0x4bbf70)** (classId 22): GameBase parent; flag ->
  9 x read(4) storm params + 3 x flag. Cleared the bad_login pkt1286 blocker (-> 0 blockers).
- **Player::readPacketData (0x4699d0)** (vtable slot 0xec; shared by AIPlayer): the
  control-object readPacketData dispatched once the client's own Player is the scoped
  control object (spawned + controlled). ShapeBase::readPacketData (8B) + readInt(3) +
  flag?readInt(7) + flag?readInt(7) + flag?[6 x read(4) pos+rot + readInt(4)] + common
  3 x read(4) + flag?[readInt(14) mounted ghost id + mounted obj readPacketData]. The
  mounted-object dispatch defaults to the 8-byte ShapeBase base (mount class unknown
  from here; not observed mounted-on-non-ShapeBase in the captures).

### Position / transform semantics (the telemetry data model)
- **World position** comes from: the mObjToWorld MATRIX translation (elements 3,7,11) for
  TSStatic (0x4917e0) / InteriorInstance (0x507b50) / ParticleEmitterNode (0x4b6a00);
  the controlled-pose readCompressedPoint for Player (0x46ee6c); the Projectile
  compressed point / Point3F. LIVE these are sane map coordinates (e.g. TSStatic
  [1425, 615, 223], InteriorInstance [385, 350, 218]).
- **Scale** = the GameBase Point3F (setScale, above) -- ShapeBase-derived objects
  (Player/AIPlayer/Item/StaticShape/markers) expose scale + shape here; their initial
  scope carries no world position in unpackUpdate (so position stays None until an
  ongoing update / the controlled-pose point arrives).
- **Rotation** = the ShapeBase/Player readNormalVector orientation words.
- **Shape name** = the object's datablock's `shapeFile` (ShapeBaseData's first readString
  @0x47cad0, captured in datablocks.py) joined via the ghost's datablock id; TSStatic /
  InteriorInstance carry their shape/file string directly in unpackUpdate. LIVE/replay
  resolves real .dts names: horse.dts, player/female.dts, door1.dts, cratea1.dts, etc.
- **Box6F** in StaticShape/MissionMarker is the LOCAL mObjBox (recorded as `world_box`,
  not used as position -- it is unit-ish at scope time, not a world transform).

### Architecture (no bits changed -- value extraction only)
- `aotbot/telemetry.py`: `DecodeSink` (thread-local, decoders push named field values via
  `emit`/`emit_point3f`; no-op when no sink installed -> zero hot-path cost when OFF) +
  `ObjectRegistry` (`ghostId -> ObjectRecord{class, datablock_id, shape_name, position,
  rotation, scale, mount, is_control_object, scoped, last_update}` + a datablock-id ->
  shapeFile resolver with back-fill; tracks the control ghost; handles removal).
- The ghost decoders/primitives now RETURN their decoded values (Point3F bytes, Box6F
  center, normal-vector words, compressed point) and `emit(...)` at the transform /
  datablock / shape sites. Bit consumption is unchanged (the capture-replay regression
  test asserts the clean counts do not drop).
- `phases._read_ghost_section`: OFF -> returns immediately (ghost section untouched, the
  pre-Wave-16 behavior, minimal CPU, chat/login unaffected since the section is last in
  the packet). ON -> decodes fully + per-update DecodeSink feeds the registry; control
  header captures the control ghost id + its readPacketData transform.
- `events._read_ghost_always_object_event` / `_read_sim_datablock_event`: the
  unpackUpdate/unpackData are consumed for alignment EITHER WAY; the registry is only
  POPULATED (initial object state + datablock shapeFile) when ON.
- API: `client.list_objects()` / `client.get_object(id)`; Node-RED inbound `list_objects`
  -> `{"action":"object_list","objects":[...]}` and `get_object <id>` ->
  `{"action":"object","object":{...}}`.
- Flag: `AOT_TRACK_OBJECTS` (bool, default false) in config + .env.example.

### Validation
- Suite 243 -> **262** (+19: telemetry sink/registry/value-extraction + a capture-replay
  bit-exactness regression parametrized over real_login/bad_login/bot_session_postfix).
- `replay_s2c` (now runs with tracking ON): real_login **1274**, bad_login **2055** (0
  blockers), bot_session_postfix **2461** (0 blockers). Flag-OFF replay still reaches
  clientCmdLoginSuccess via the event path (ghost section skipped) -- no regression.
- LIVE (production, one polite session, clean disconnect): connects -> full ghost stream
  decodes with ZERO desync -> `list_objects()` returns 237 scoped objects with plausible
  world coordinates (InteriorInstance/TSStatic map positions) and the registry stays
  aligned. NOTE: login (clientCmdLoginSuccess) did NOT complete in the live test sessions
  this wave (the account appears stuck "online" server-side from repeated test connects,
  the hazard flagged in the memory) -- so the dynamic Player/AIPlayer/Item objects (which
  scope only after the bot spawns post-login) were not observed LIVE this run; they ARE
  fully validated in the offline replay of the real client's logged-in session
  (real_login/bad_login): Player(horse.dts)@[409,233,213], AIPlayer(female.dts)@[342,171,213], etc.

### Remaining desync (documented, non-blocking; needs runtime instrumentation)
`replay_s2c real_login` first hard stop = **pkt822/seq261**: a NEW (ghost-section)
**Projectile** initial-scope update under/over-reads (a downstream ghost then reads a
garbage classId 58). The standalone Projectile decode is 226 bits but in-context the
trace shows 268 consumed -- consistent with the preceding AIPlayer (controlled,
post-spawn) update mis-sizing, the same spawned-control region the docs note needs a
winedbg getCurPos trace (like the fxShapeReplicator / Camera-control-object cases). This
is POST-login (all of LoginSuccess + inventory/gold/chat already decoded) and only occurs
in the real client's spawned-and-playing capture; it does not affect telemetry's
correctness for the classes above or the headless bot's login/chat. Next step: hook the
real client's AIPlayer/Player unpackUpdate (0x46e690) + Projectile (0x476bf0) and log the
BitStream bit cursor per field around the post-spawn controlled-player updates.

---

## Wave-17 — PrecipitationData LOGIN-REGRESSION fix + remaining datablock decoders — **EXE + capture-validated**

A live login regression: the world now streams **PrecipitationData** (datablock
classId 20) -- a class the bot had no `unpackData` decoder for -- so the Phase-1
datablock stream desynced (`SimDataBlockEvent` envelope decoded, then the per-class
`unpackData` raised) and login never completed. Captured a fresh live datablock
stream while it was raining (`tools/captures/real_login3.jsonl`); it confirmed the
blocker: the stream stopped right after the (failed) PrecipitationData with the next
`SimDataBlockEvent` (classId 11) "no generic skip".

### Resolution method (corrected the create()-slot bug in the resolver)
The DataBlock `ConcreteClassRep<T>` vtable's **create()** is **slot 0x04** (the prior
helper used slot 0x14, which on the SHORT template ClassRep vtables read into the
inline class-name string and followed the wrong object). Correct chain:
regthunk -> ClassRep ctor (sets ClassRep vtbl) -> ClassRep vtbl[0x04]=create ->
alloc + ctor -> object vtbl -> slot 0x48 = unpackData.

For PrecipitationData: regthunk @ 0x5e7f50 (classType=1), ClassRep obj 0x670448,
ClassRep ctor 0x4bce70 (vtbl 0x601e24), create 0x4bcf20 (alloc 0x60, ctor 0x4bbd20),
object vtbl **0x601d94**, **unpackData @ 0x4bad60**. (The NetObject `Precipitation`
is a DIFFERENT class: object vtbl 0x601c24, unpackUpdate slot 0x4c = 0x4bbf70.)

### Datablock classes ported this wave (CFG-traced + cross-checked vs TGE)

| class | classId | unpackData VA | wire layout |
|---|---|---|---|
| **PrecipitationData** | 20 | **0x4bad60** | GameBaseData(0); db-ref(soundProfile); 2xreadString(drop/splash tex); 3xread(4)(dropSize,splashSize,splashMS); readFlag(useTrueBillboards). **Bit-identical to TGE precipitation.cc:89** (AoT did not diverge). |
| AudioEnvironment | 1 | 0x58db70 | SimDataBlock(0); flag mUseRoom -> {set: readRangedU32(0,28)=5b; clear: ranged(0,10000)=14, (0,20000)=15, (0,12000)=14, readInt 8,8,8,9,7, ranged(0,10000)=14, readInt 8,9,10,8,10, readInt(6)}. |
| AudioSampleEnvironment | 3 | 0x58de30 | SimDataBlock(0); ranged(0,11000)=14, (0,10000)=14, (0,11000)=14, (0,10000)=14; readInt 9,8,9,8,9,9,9; ranged(0,10000)=14; readInt(3). |
| PathedInteriorData | 18 | 0x515030 | 3x db-ref (MaxSounds=3, BEFORE parent); then GameBaseData(0). |
| WheeledVehicleSpring | 30 | 0x4d1340 | SimDataBlock(0); 4x read(4). |
| WheeledVehicleTire | 31 | 0x4d1230 | SimDataBlock(0); readString(shapeFile); 11x read(4). |
| fxDTSBrickData | 32 | 0x498770 | GameBaseData(0); 7x readString; 3x readInt(6). (AoT-specific; trailing FP math reads no bits.) |
| PathCameraData | 17 | 0x464210 | tail-jmp to ShapeBaseData (0x47cad0), exactly like CameraData. |

### Deliberately UNPORTED (documented, never-guess rule)
FlyingVehicleData(8, 0x4c7510), HoverVehicleData(10, 0x4c9f90),
WheeledVehicleData(29, 0x4d1450) all chain through **VehicleData::unpackData
@ 0x4ccc60** (-> ShapeBaseData) -- a large CFG with multiple inline-flag-gated
db-ref loops whose exact AoT loop counts/widths could not be statically pinned with
certainty. NO vehicle datablock appears in any captured AoT world. They raise
`DataBlockDecodeError` with the class name. To port: CFG-follow VehicleData first,
then each subclass tail; validate against a capture that scopes a vehicle.

### Validation
- `tools/check_datablocks.py tools/captures/real_login3.jsonl` (the rain capture):
  **707 datablocks decode, NO BLOCK -- reached end of stream** (was: blocked at
  PrecipitationData). PrecipitationData is decoded mid-stream.
- `real_login.jsonl` + `bad_login.jsonl`: ZERO regression (real_login's iter-822 stop
  is the pre-existing Wave-16 post-spawn ghost classId-58 unpackUpdate issue, unrelated
  to datablocks/login). 
- Suite 269 -> **280 pass** (+11: round-trip per new class + a rain-capture replay
  regression test asserting PrecipitationData decodes the whole stream).

---

## Wave-17 — LIVE LOGIN REGRESSION fixed (ghost-burst silent misalignment) — EXE + live-confirmed

The telemetry commit (git b002722) broke live login. Root causes, all fixed:

1. **`_read_ghost_section` `track_objects` early-return (the regression trigger).**
   b002722 added `if not self.track_objects: return` to phases.`_read_ghost_section`,
   skipping the ghost section entirely when telemetry is OFF (the LIVE default). That
   left the GhostAlways burst's bits unconsumed and never populated `_ghost_classes`
   -> the s2c stream SILENTLY misaligned a few packets later (garbage classId-15/6
   events; a fake `DataBlocksDone seq=2.7e9`) and the login response was never decoded.
   FIX: always decode the ghost section while ghosting is active; the flag now gates
   only BUILDING the registry (DecodeSink/update_from_sink), never alignment.

2. **`fxFoliageReplicator::unpackUpdate` (0x4a5560) over-read by 4 bits.** The four
   `+0x394/+0x395/+0x39c/+0x39d` fields are CONSECUTIVE `Stream::read(1,&bool)` byte
   reads (call-sites 0x4a5749/0x4a5769/0x4a57a1/0x4a57c1; `push ebx==1; call [edx+4]`).
   The Wave-11 transcription inserted FOUR phantom 1-bit `readFlag`s between them.
   In the older capture worlds the foliage master mask is clear so it never bit; the
   CURRENT (raining) world streams an fxFoliageReplicator with the mask SET, so the
   +4-bit over-read terminated the GhostAlways event burst one object early.

3. **Spawner-subclass unpackUpdate overrides were mis-mapped to MissionMarker.**
   Vtable slot 0x4c (resolved via thunk->ClassRep->create->objVtable):
   - MissionMarker 0x463620; NPCSpawner 0x4638a0 (= `jmp 0x463620`), MazeSpawner /
     RoomMarker also 0x4638a0 -> correctly == MissionMarker.
   - **DestructableSpawner 0x4639e0** = MissionMarker + `flag;[read(4)]` (+33 bits when set).
   - **GoldSpawner 0x4638b0** = MissionMarker + `flag;[6x read(4) + read(1)byte]`.
   - **SpawnSphere 0x4637e0** = MissionMarker + `flag;[4x read(4)]`.
   - **WayPoint 0x4636b0** = MissionMarker + `flag;[readString]` + `flag;[read(4)]` +
     `flag;[flag]`.
   The shared MissionMarker decoder under-read these (their flag was SET in the live
   world), desyncing the burst.

4. **Ported two previously-unported burst classes** (slot 0x4c):
   - **fxSunLight 0x4b2470**: master flag; if set Box6F + read(1)byte + 2 readString +
     2 read(4) + read(1)byte + ColorF + 4 read(4) + 14 flags + 2 ColorF + 10 read(4) +
     8 readString + 6 read(4).
   - **TerrainBlock 0x563bb0**: two mutually-exclusive flag blocks; flag1(InitMask) ->
     read(4) + 3 readString + 4 read(4) + read(4)count + count x read(4); else flag2 ->
     read(4)count + count x read(4).

The **GameBase two-block change (0x456da0)** from the telemetry commit was NOT
implicated — disassembly confirms GameBase::unpackUpdate reads exactly two mask-gated
blocks (Point3F via setTransform @0x456dd2, then readInt(10)+3 datablock id @0x456e2b),
and ShapeBase (0x483dbb) calls the whole function as its parent. That transcription is
bit-exact.

**LIVE (production 45.148.165.55:28000, polite one-shot sessions):**
- fresh non-existent account -> full load (Phase1->Phase2->Phase3->MissionStart) ->
  `clientCmdWarningBox("Character does not exist!","Oops")`.
- valid account (Mr Poopy Butthole) -> `clientCmdLoginSuccess` -> "logged in." -> LOGGED IN.
- AOT_TRACK_OBJECTS=true: same (WarningBox), telemetry registry populated.

**Validation:** all captures (real_login -> LoginSuccess, bad_login -> WarningBox,
real_login3, live_rain_freshacct) decode the full GhostAlways burst with ZERO
AlignmentError at track_objects=False (the live default). Suite 280 -> **294 pass**.
Added VALUE-SANITY regression tests (tests/test_phases.py): replay reaches the login
verb at track OFF, and post-GhostAlwaysStarting ConnectionMessage seqs stay small on
the rain capture (catches the silent-misalignment class a "clean packet count" misses).
New capture fixture tools/captures/live_rain_freshacct.jsonl + tools/live_capture_self.py
(bot-side relay-free s2c/c2s dump). Remaining (pre-existing, non-blocking): real_login
iter-822 post-login spawned-Player ghost classId-58 under-read (Wave-16 TODO; POST login).

---

## Wave-18 — live-PLAYER telemetry: shapeName (NetStringTable name), rotation (yaw), garbage-position fix, and the ShapeBaseImageData state-block over-read — **RUNTIME (winedbg) + LIVE-confirmed**

Goal: make `AOT_TRACK_OBJECTS=true` `/objects player` accurate — real name, correct
class, a getTransform-convention rotation, and no garbage positions. Validated LIVE
against ground truth (the user left "Jeff Bezos" standing at a fixed spot;
getTransform = `292.647 170.091 213.218 0 0 1 0.637333`, getShapeName = "Jeff Bezos";
the bot's own deterministic spawn = `281.797 175.591 213.218 ... 0.989478`).

### winedbg runtime instrumentation (the proven technique; docker image v0.0.5)
Attached gdb (`winedbg --gdb <winPID>`; BitStream curPos = `*(int*)($esi+0xc)`) to the
REAL client running in `skylord123/aot-wine-x11-novnc-docker:v0.0.5` and logged the bit
cursor at every reader call-site of **Player::unpackUpdate (0x46e690)**,
**ShapeBase::unpackUpdate (0x483d90)** and **GameBase::unpackUpdate (0x456da0)**.
- Gotchas learned: the winPID is NOT always 0x10c — read it from `winedbg --command
  "info process"` each time; the harness relaunches the exe (new process) on every
  reconnect; gdb must be fed its script as a FOREGROUND-in-background bash task (a
  detached `docker exec -d ... < script` loses stdin); `timeout -s INT` detaches
  cleanly (a plain SIGKILL/`timeout` leaves the inferior stopped -> kills the client).
- **RESULT: Player/AIPlayer/ShapeBase/GameBase unpackUpdate are already BIT-EXACT.**
  The live per-field curPos deltas matched the Python decoders exactly (GameBase
  scale-P3F+db = 108b; ShapeBase readFloat6=6, readInt2=2, readNormalVector(8)=17,
  mesh cnt8=8, thread n8=8, 20xreadInt8=160; controlled-pose cpoint=50, etc.). The
  hypothesised "Player unpackUpdate slip" was NOT the cause of the garbage telemetry.

### Root causes of the WRONG telemetry (all fixed; none were a Player-decode slip)
1. **rotation = raw `[255,255]`**: `_read_normal_vector` surfaced the two readSignedFloat
   words as raw little-endian byte ints. FIX: decode the real unit vector per the
   0x4216f0 asm — `phi=readSignedFloat(bits+1)*PI; theta=readSignedFloat(bits)*PI/2;
   v=(cos phi sin theta, sin phi sin theta, cos theta)`. BUT the normal vector is the
   head/LOOK direction (near-constant for standing players), NOT the body heading. The
   real heading is the controlled-pose **`readFloat(7) * 2*PI`** (mRot.z, asm 0x46ef03 +
   const 0x5f1d78=2*PI). getTransform reports it with OPPOSITE winding:
   `angle = (2*PI - yaw) mod 2*PI`. LIVE-CONFIRMED: Jeff's wire yaw 5.6400 -> 0.643,
   matching getTransform 0.637333. (`_emit_yaw`; head normal vector still read for
   alignment but no longer surfaced as rotation.)
2. **shapeName missing / = the .dts**: the ShapeBase skin/name block (@0x484732 ->
   ConnectionStringTable read 0x546fc0) stores **mShapeNameTag [ebx+0x948]** — the
   player's NAME, exactly what getShapeName returns. It is a tagged string: literal, or
   a 5-bit slot id resolved via the connection's RECEIVE NetStringTable ([eax+0x1ac] ==
   `events.recv_table`, taught by NetStringEvent). FIX: `_read_tagged_string` now
   RETURNS the resolved string; ShapeBase emits it as `name`; `telemetry.set_string_resolver`
   wires `recv_table.lookup`; ObjectRecord gains `name` (username) + `shape_file` (.dts)
   as DISTINCT fields (`shape_name` = name-or-file alias). LIVE: "Jeff Bezos",
   "Mr Poopy Butthole", "Shop Keeper", "Marshal", "Sword Giver" all resolve.
3. **garbage positions** (e.g. Jeff 2704,3452,-240): `readCompressedPoint` types 0/1/2
   are quantised values relative to an engine compression reference — NOT absolute world
   coords; only **type 3** is a full-precision absolute Point3F. The controlled object
   packs its own pose as type 3 (the bot's own player decodes to the EXACT spawn
   281.791,175.593,213.212); other players send type 0/1/2. FIX: `_read_compressed_point`
   returns `(point, is_world)`; position is surfaced ONLY for type 3. Non-controlled
   standing players therefore report position=None (honest) instead of garbage. (Their
   absolute world position needs the mCompressPoint dequantisation reference — a
   documented remaining gap; never guessed.)
4. **class mislabel** was purely a symptom of the decode DESYNC below; with the stream
   aligned, classIds resolve correctly (Player=21 real players + ridable horses;
   AIPlayer=0 NPC vendors + orcs). NOTE on this server `horse.dts` ghosts as the
   **Player** netclass (named "Horse"), not AIPlayer — faithful to the wire.

### The LIVE decode blocker (why the bot never even reached the Player ghosts)
A fresh live/relay capture (`tools/captures/live_session_dbg.jsonl`) desynced at the
post-login datablock burst: **ShapeBaseImageData::unpackData (0x4859a0) over-read by
~2000 bits** (a state-block readString self-terminated at a garbage offset -> "event
classId 15 (no generic skip)"), stopping the bot BEFORE any Player ghost. Re-walked the
31-iteration state-machine loop CFG (0x485ed3..0x486288): the per-state-block tail is
DONE after the two bare flags [+0x44]/[+0x28]; the **[+0x54] and [+0x58] fields are set
to CONSTANT 0** (`mov [edi+0x54],0` @0x486264; `mov [edi+0x58],eax`=0 @0x48627d) with NO
flag read and NO field read, then `add edi,0x6c` -> next state. The prior transcription
read TWO spurious flag-gated blocks there (flag+readInt(10)+2xread(4); flag+readInt(10)).
Worlds whose images populate these state blocks (the equipped Ball-spell image set)
exercised the spurious reads -> the runaway. FIX: drop both trailing blocks. The whole
live capture now decodes with ZERO blockers (1582 clean); real_login/bad_login/real_login3
unchanged (1274/2055/4651). (Player/AIPlayer/ShapeBase were bit-exact all along — the
"class is wrong / positions are garbage" symptoms were this single datablock desync
cascading into mislabeled ghosts.)

### LIVE comparison (one polite session, clean disconnect)
`/objects` decoded the full 635-object world with zero desync. Bot's own player
`Mr Poopy Butthole` pos=[281.791,175.593,213.212] (== deterministic spawn) ctrl=True;
`Jeff Bezos` (Player) name resolved, rotation angle 0.643 (== getTransform 0.637333),
position=None (honest; remote quantised point). Suite 296 -> **298** (+ a live-capture
regression test asserting name/rotation/control-position + a no-garbage-coords invariant,
+ live_session_dbg in the no-regression set).

### Remaining (documented, non-blocking; never guessed)
- Absolute world position for NON-controlled (remote) players/objects whose pose is a
  quantised compressed point (type 0/1/2) — needs the engine mCompressPoint dequant
  reference / cross-update delta state. Surfaced as None rather than garbage.
- real_login iter-822 post-combat Projectile classId-58 under-read (pre-existing Wave-16
  TODO; only in a spawned-and-fighting capture; not on the standing-player path).
