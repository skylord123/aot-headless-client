# Handshake + packet/notify layer

This documents the connectionless UDP handshake and the `NetConnection` packet
header / reliability layer, as a field-by-field wire spec to code a `BitStream`
reader/writer against.

All citations are `file:line` into the local TGE 1.4 source tree
`/home/skylar/Projects/TorqueGameEngine2005` (referred to below as `$TGE`).
**Age of Time runs a custom fork on DSO bytecode v33 (≈ TGE 1.2/1.3, compiled
Jan 2009), so the 1.4 source is close-but-not-exact.** Every spot that AoT
likely diverges is flagged `[CONFIRM-EXE]`.

---

## 0. BitStream fundamentals (must implement first)

Source: `$TGE/engine/core/bitStream.cc`.

- A packet buffer is read/written **bit by bit, LSB-first within each byte**.
  `writeFlag(true)` ORs `1 << (bitNum & 7)` into `dataPtr[bitNum>>3]` and
  increments `bitNum` (`bitStream.cc:234-248`). So bit 0 of the stream is bit 0
  (the 0x01 bit) of byte 0, bit 8 is the 0x01 bit of byte 1, etc.
- `writeInt(val, n)` / `readInt(n)`: the value is **little-endian byte order**
  (`convertHostToLEndian`), then `n` low bits are written LSB-first
  (`bitStream.cc:292-308`). `readInt` masks to `n` bits unless `n==32`.
  So a multi-byte int written with 32 bits comes out as the LE encoding of the
  value, bit-packed starting at the current bit position.
- `write(U8/U32/F32 x)` (templated `_write`) just does `writeBits(sizeof*8, &x)`
  i.e. raw little-endian bytes, bit-packed (`bitStream.cc:286-290`). `write(F32)`
  is the raw 4 IEEE-754 LE bytes.
- `getPosition()` returns **byte count** = `(bitNum+7)>>3` — i.e. the current
  bit cursor rounded UP to a whole byte (`bitStream.cc:137-140`). This is the
  number of bytes actually transmitted (`sendto` uses `getPosition()`).
- Strings: `writeString`/`readString` use a **Huffman coder** over a fixed
  256-entry frequency table (`bitStream.cc:577-798`, table at `:800-1057`).
  Layout per string:
  - 1 flag bit `useStringBuffer` — only set when a `stringBuffer` is installed
    (the per-packet 256-byte scratch buffer GameConnection installs, see
    mission-phases.md). When set: `readInt(8)` offset, then the Huffman buffer
    is decoded into `stringBuffer+offset`. The connect handshake packets do
    **not** install a stringBuffer, so on handshake strings this outer flag is
    absent — go straight to the Huffman buffer.
  - Huffman buffer (`readHuffBuffer`, `:729`): 1 flag bit `compressed`; then
    `readInt(8)` = length; then if compressed, `length` Huffman-coded symbols
    (walk the tree, MSB-of-code first per `writeBits`), else `length` raw bytes.
  - **You must port the exact Huffman table** (`csm_charFreqs`, `bitStream.cc:800`)
    and tree-build (`buildTables`, `:616`) to read/write strings. The tree is
    deterministic from that table. `[CONFIRM-EXE]` the table is identical in
    AoT (it has been stable across all TGE 1.x, but verify since strings appear
    in the very first connect-request).
- Endianness: x86 build, `convertLEndianToHost` is identity. Implement BitStream
  as LE throughout.

---

## 1. UDP framing & packet dispatch

Source: `$TGE/engine/sim/netInterface.cc:63-119`.

Every UDP datagram's **first byte LSB decides the family**:

- `data[0] & 0x01 == 1` → a **connected data/protocol packet** (game data); hand
  to the matching `NetConnection` by source address and run the packet/notify
  layer (section 4). (`netInterface.cc:71-80`)
- `data[0] & 0x01 == 0` → a **connectionless / out-of-band packet**. The whole
  first byte is read as `U8 packetType` (`netInterface.cc:86-87`). Note this
  read consumes 8 bits, so the dispatch byte is also the first field.

The OOB `packetType` enum (`netInterface.h:14-35`) — note all values are **even**
(LSB 0) so they never collide with the data-packet family:

```
MasterServer*/Game* query types   = 2..22  (handled by handleInfoPacket; ignore)
ConnectChallengeRequest           = 26
ConnectChallengeReject            = 28   (defined but unused in 1.4 send path)
ConnectChallengeResponse          = 30
ConnectRequest                    = 32
ConnectReject                     = 34
ConnectAccept                     = 36
Disconnect                        = 38
```

`packetType <= GameHeartbeat (22)` is routed to `handleInfoPacket` (a no-op stub
in 1.4) — the master/query protocol. The MVP skips the master server, so we only
emit/parse 26/30/32/36/34/38.

`[CONFIRM-EXE]` These numeric type constants are an enum baked into the exe.
Older TGE used the same values, but verify against a real AoT capture — a single
off-by-N here makes the server silently drop every handshake packet.

---

## 2. Connection handshake sequence (client side)

Overview comment: `netInterface.cc:121-149`. Two-phase challenge to defeat
spoofed-source DoS. The client (us) drives:

```
us -> server : ConnectChallengeRequest
server -> us : ConnectChallengeResponse   (carries a 16-byte address digest)
us -> server : ConnectRequest             (echoes digest + game string/version/args)
server -> us : ConnectAccept              (carries server protocol version)
            or ConnectReject (reason string) / Disconnect
```

The whole pending-connection state is keyed on a 32-bit **connectSequence** that
WE choose. In `startConnection` it is set to `Platform::getVirtualMilliseconds()`
(`netInterface.cc:420`) — i.e. an arbitrary 32-bit nonce. Reuse the same value
across all four packets of one attempt. Connection state machine:
`AwaitingChallengeResponse → AwaitingConnectResponse → Connected`
(`netConnection.h:595-602`, set at `netInterface.cc:421,211`).

### 2.1 ConnectChallengeRequest (send) — `netInterface.cc:152-162`

Bit order, all via `out->write(...)`:

| # | field | type / bits | value |
|---|-------|-------------|-------|
| 1 | packetType | `U8` (8 bits) | `26` (ConnectChallengeRequest) |
| 2 | connectSequence | `U32` (32 bits, LE) | our nonce |

That's the entire packet. Retransmit every `ChallengeRetryTime = 2500` ms, up to
`ChallengeRetryCount = 4` tries (`netInterface.h:47-48`, `netInterface.cc:456-468`).

### 2.2 ConnectChallengeResponse (receive) — `netInterface.cc:194-215`

| # | field | type / bits |
|---|-------|-------------|
| 1 | packetType | `U8` = `30` (already consumed by dispatch in §1) |
| 2 | connectSequence | `U32` — must equal our nonce, else drop |
| 3 | addressDigest[0] | `U32` |
| 4 | addressDigest[1] | `U32` |
| 5 | addressDigest[2] | `U32` |
| 6 | addressDigest[3] | `U32` |

Store the 16-byte digest verbatim (`setAddressDigest`). We do **not** compute or
validate it — only the server does (it's `computeNetMD5` of our address+sequence+
server's secret, `netInterface.cc:513-611`). We just echo it back. On receipt,
advance to `AwaitingConnectResponse` and send ConnectRequest immediately.

### 2.3 ConnectRequest (send) — `netInterface.cc:219-238` + GameConnection override

Base `NetConnection` part (`netInterface.cc:221-233`):

| # | field | type / bits | value |
|---|-------|-------------|-------|
| 1 | packetType | `U8` (8) | `32` (ConnectRequest) |
| 2 | connectSequence | `U32` (32) | our nonce |
| 3 | addressDigest[0] | `U32` | echo from challenge response |
| 4 | addressDigest[1] | `U32` | echo |
| 5 | addressDigest[2] | `U32` | echo |
| 6 | addressDigest[3] | `U32` | echo |
| 7 | className | `writeString` (Huffman) | the NetConnection subclass name |

`className = conn->getClassName()` (`netInterface.cc:232`). For the game client
this is **`"GameConnection"`** (the connection object the client creates). The
server does `ConsoleObject::create(className)` (`netInterface.cc:284`), so the
string must match the registered class name exactly. `[CONFIRM-EXE]` AoT may
subclass GameConnection under a different registered name — confirm via capture.

Then `conn->writeConnectRequest(out)` appends the subclass payload. The base
`NetConnection::writeConnectRequest` (`netConnection.cc:1059-1063`) writes:

| # | field | type / bits | value |
|---|-------|-------------|-------|
| 8 | netClassGroup | `U32` | `NetClassGroupGame` = `0` (default, `netConnection.cc:215`) |
| 9 | classCRC | `U32` | `AbstractClassRep::getClassCRC(NetClassGroupGame)` |

**`classCRC` is a CRC of the entire networked-class manifest** (the set of
ghostable/event/datablock classes and their net IDs). The server compares it to
its own (`netConnection.cc:1065-1076`); a mismatch → ConnectReject `"CHR_INVALID"`.
`[CONFIRM-EXE]` This value is intrinsic to the exe's class registration order
and **must be extracted from AoT.exe or captured** — we cannot compute it from
script. It is a fixed 32-bit constant for a given build. **This is the single
most likely thing to block a from-scratch connect.** Capture a real client's
ConnectRequest and copy the 4 bytes.

Then `GameConnection::writeConnectRequest` (`gameConnection.cc:213-224`) appends:

| # | field | type / bits | value |
|---|-------|-------------|-------|
| 10 | gameString | `writeString` | `GameString` |
| 11 | currentProtocolVersion | `U32` | `CurrentProtocolVersion` |
| 12 | minRequiredProtocolVersion | `U32` | `MinRequiredProtocolVersion` |
| 13 | joinPassword | `writeString` | server join password (usually `""`) |
| 14 | connectArgc | `U32` | count of connect args |
| 15.. | connectArgv[i] | `writeString` × argc | connect args |

In stock 1.4: `GameString = "Torque Game Engine Demo"` (`gameConnection.h:39`),
`CurrentProtocolVersion = MinRequiredProtocolVersion = 12` (`gameConnection.cc:27-28`).

`[CONFIRM-EXE]` **All four of these are AoT-specific:**
- `GameString` — AoT will have its own product string; mismatch → `"CHR_GAME"`
  reject (`gameConnection.cc:233-237`).
- protocol versions — likely not 12 on AoT's older fork; mismatch →
  `"CHR_PROTOCOL_LESS"`/`"CHR_PROTOCOL_GREATER"` (`gameConnection.cc:245-253`).
- `connectArgc`/`connectArgv` — the client passes these from `MM_Connect()` /
  the join screen; they land in the server's `onConnect(%client, ...)` callback
  (`gameConnection.cc:155-159`). AoT likely sends nothing here, OR a guid/
  username — capture the real ConnectRequest to see. Login itself is a *later*
  RemoteCommandEvent, not a connect arg (see event-system.md), so argc is
  probably 0, but **verify**.

Retransmit every `ConnectRetryTime = 2500` ms up to `ConnectRetryCount = 4`
(`netInterface.cc:469-481`).

### 2.4 ConnectAccept (receive) — `netInterface.cc:312-342`

| # | field | type / bits |
|---|-------|-------------|
| 1 | packetType | `U8` = `36` (consumed by dispatch) |
| 2 | connectSequence | `U32` — must match our nonce |
| 3.. | subclass payload | `readConnectAccept` |

Base `readConnectAccept` reads nothing (`netConnection.cc:1083-1088`).
`GameConnection::readConnectAccept` (`gameConnection.cc:198-211`) reads:

| # | field | type / bits | check |
|---|-------|-------------|-------|
| 3 | serverProtocolVersion | `U32` | must be in `[MinRequired, Current]` |

On success: `removePendingConnection`, `onConnectionEstablished(true)`,
`setEstablished()` (installs in the address table; pings/timeouts begin),
`setConnectSequence(connectSequence)` (`netInterface.cc:337-341`). **The
connection is now live and the packet/notify layer (section 4) takes over.**
`onConnectionEstablished(true)` also flips on the runtime behaviors we rely on:
`setGhostTo(true)`, `setSendingEvents(true)`, `setTranslatesStrings(true)`,
`setIsConnectionToServer()` (`gameConnection.cc:136-146`) — these mean the
connection *receives* ghosts, *sends* events, and runs the per-connection string
table. Replicate all of them in our state.

### 2.5 ConnectReject (receive) — `netInterface.cc:356-369`

| # | field | type / bits |
|---|-------|-------------|
| 1 | packetType | `U8` = `34` |
| 2 | connectSequence | `U32` |
| 3 | reason | `readString` |

Reasons are short codes: `CHR_INVALID`, `CHR_GAME`, `CHR_PROTOCOL*`,
`CHR_PASSWORD`, `CR_INVALID_ARGS` (`gameConnection.cc`), or anything the server's
`onConnectRequest` script returns. Surface the reason and stop.

### 2.6 Disconnect (receive OR send) — `netInterface.cc:371-388,432-445`

| # | field | type / bits |
|---|-------|-------------|
| 1 | packetType | `U8` = `38` |
| 2 | connectSequence | `U32` — must match |
| 3 | reason | `readString` |

To leave cleanly, send this with our nonce + a reason string.

---

## 3. Address digest = opaque blob (do NOT compute it)

`computeNetMD5` (`netInterface.cc:513-611`) hashes (addr.type, addr.netNum[4],
addr.port, connectSequence, + 12 words of server-private random) with a one-shot
MD5 transform. **The 12 random words are server-secret**, so a client cannot
reproduce the digest — it just echoes the 16 bytes back unchanged. We never call
this. (Documented only so we know the 4 U32s in §2.2/§2.3 are opaque.)

---

## 4. NetConnection packet/notify layer (the reliability protocol)

This is the sliding-window notify protocol that runs on **every connected packet**
(`data[0] & 1 == 1`) after ConnectAccept. Source: `$TGE/engine/core/dnet.cc`
(`ConnectionProtocol`). 32-deep window; each packet header acks the last up-to-32
packets via a shifting bitmask. **The bitstream MUST round-trip exactly or the
server drops us.**

### 4.1 Connection-level state (per side)

From `ConnectionProtocol` ctor (`dnet.cc:39-46`) and members (`dnet.h:31-39`):
- `mLastSeqRecvd` (last seq we received), init 0
- `mHighestAckedSeq` (highest of our sends the peer acked), init 0
- `mLastSendSeq` (last seq we sent; "start sending at 1" — first DataPacket is 1),
  init 0
- `mAckMask` (U32 bitmask of recently-received data packets), init 0
- `mConnectSequence` (the handshake nonce; only its LSB goes on the wire), via
  `setConnectSequence`
- `mLastRecvAckAck`, `mLastSeqRecvdAtSend[32]` (per-slot record for ack-of-acks)
- `mConnectionEstablished` (becomes true once a send of ours is acked, `dnet.cc:200-204`)

### 4.2 Send header — `buildSendPacketHeader` (`dnet.cc:47-75`)

Computed first: `ackByteCount = ((mLastSeqRecvd - mLastRecvAckAck + 7) >> 3)`
(0..4 bytes of ack bitmask we need to convey). For a `DataPacket`, **increment
`mLastSendSeq` before writing** (`dnet.cc:54-55`).

Bit-exact header, in order:

| # | field | bits | value |
|---|-------|------|-------|
| 1 | gamePacketFlag | `writeFlag(true)` = 1 bit | always `1` (makes byte0 LSB = 1 → data family) |
| 2 | connectSeqBit | `writeInt(mConnectSequence & 1, 1)` | LSB of handshake nonce |
| 3 | packetSeq | `writeInt(mLastSendSeq, 9)` | low 9 bits of our send seq |
| 4 | highestAck | `writeInt(mLastSeqRecvd, 9)` | low 9 bits of last seq we received |
| 5 | packetType | `writeInt(packetType, 2)` | 0=Data,1=Ping,2=Ack (`dnet.cc:15-21`) |
| 6 | ackByteCount | `writeInt(ackByteCount, 3)` | 0..4 |
| 7 | ackMask | `writeInt(mAckMask, ackByteCount*8)` | the ack bitmask (variable width) |

So the header is 1+1+9+9+2+3 = 25 bits fixed, plus `ackByteCount*8` bits of mask
= **between 25 and 57 bits (≈ 4 to 8 bytes)**. The 1.4 comment (`dnet.cc:107-124`)
says "Fixed packet header: 3 bytes" then "next 1-4 bytes are ack flags" — the
25 fixed bits round to 4 bytes once the next field starts, hence "average 4 byte
header". For a DataPacket also record `mLastSeqRecvdAtSend[mLastSendSeq & 0x1F] =
mLastSeqRecvd` (`dnet.cc:73-74`).

`[CONFIRM-EXE]` Field **widths** (9-bit seq, 2-bit type, 3-bit ackByteCount) are
the part most likely tweaked in a fork. Older TGE has historically used these
exact widths, but a capture diff is the only way to be sure — a wrong width
desyncs every subsequent bit.

### 4.3 Ping / Ack packets — `dnet.cc:77-97`

`sendPingPacket` / `sendAckPacket` write just the header above with `packetType`
= Ping(1) / Ack(2) into a 16-byte buffer; no body. Ping/Ack do **not** bump
`mLastSendSeq`. We send a Ping as our keep-alive; we must reply with an Ack when
we receive a Ping (see §4.4). DataPackets carry the body (rate block + events +
ghosts, see below).

### 4.4 Receive / parse — `processRawPacket` (`dnet.cc:103-231`)

Read the header in the same order:

1. `readFlag()` — discard (the game-info bit, already known 1).
2. `pkConnectSeqBit = readInt(1)` — **must equal `mConnectSequence & 1`** else
   drop the packet (`dnet.cc:136-137`).
3. `pkSequenceNumber = readInt(9)`
4. `pkHighestAck = readInt(9)`
5. `pkPacketType = readInt(2)` — must be `< 3` (InvalidPacketType).
6. `pkAckByteCount = readInt(3)` — must be `<= 4`, else drop.
7. `pkAckMask = readInt(8 * pkAckByteCount)`

Window reconstruction (`dnet.cc:148-171`):
- `pkSequenceNumber |= (mLastSeqRecvd & 0xFFFFFE00)`; if `< mLastSeqRecvd` add
  `0x200` (wrap). If `> mLastSeqRecvd + 31` → out of window, **discard**.
- `pkHighestAck |= (mHighestAckedSeq & 0xFFFFFE00)`; if `< mHighestAckedSeq` add
  `0x200`. If `> mLastSendSeq` → bogus, **discard**.

Ack/notify processing (`dnet.cc:183-212`):
- `mAckMask <<= (pkSequenceNumber - mLastSeqRecvd)` (shift left by the gap — this
  nacks any packets we missed).
- If `pkPacketType == DataPacket`: `mAckMask |= 1` (we will ack this one).
- For `i` in `(mHighestAckedSeq+1 .. pkHighestAck]`: a packet of ours is
  considered delivered iff `pkAckMask & (1 << (pkHighestAck - i))`. Call
  `handleNotify(delivered)` — this drives event resend/confirm and ghost cleanup
  (see event-system.md). On the first successful ack, mark connection
  established (`dnet.cc:200-204`).
- Clamp `mLastRecvAckAck` if `pkSequenceNumber - mLastRecvAckAck > 32`.
- `mHighestAckedSeq = pkHighestAck`.

Post-actions (`dnet.cc:217-230`):
- If `pkPacketType == PingPacket`: **`sendAckPacket()`** (reply to keep-alive).
- `keepAlive()` — reset our own ping timer (`netConnection.cc:335-339`).
- If `mLastSeqRecvd != pkSequenceNumber && pkPacketType == DataPacket`:
  `handlePacket(pstream)` — parse the body (rate block, then events+ghosts). The
  guard means a *retransmitted* header (same seq) is acked but not double-processed.
- `mLastSeqRecvd = pkSequenceNumber`.

`windowFull()` (`dnet.cc:233-236`): stop sending when `mLastSendSeq -
mHighestAckedSeq >= 30`. We must respect this on our send side.

### 4.5 Packet body, top of `handlePacket` — `netConnection.cc:497-529`

Immediately after the header, on a DataPacket only:

| field | bits | meaning |
|-------|------|---------|
| rateChangedFlag | `readFlag()` | if 1: `mCurRate.updateDelay = readInt(10)`, `mCurRate.packetSize = readInt(10)` |
| maxRateChangedFlag | `readFlag()` | if 1: `omaxDelay = readInt(10)`, `omaxSize = readInt(10)` (then clamped) |

Then `readPacket(bstream)` → subclass body (`GameConnection::readPacket`, see
mission-phases.md §move/control header) → `eventReadPacket` →
`ghostReadPacket`. **We must emit the mirrored two rate flags on our send side**
(`checkPacketSend`/`writePacket`, `netConnection.cc:601-612`): we'll normally
write both flags as 0 once rates are settled, but must write at least the two
`0` bits.

### 4.6 Rates / timing (so we don't get dropped)

- Default packet send delay to server: `gPacketRateToServer = 32` →
  `gPacketUpdateDelayToServer = 1024/32 = 32` ms (`netConnection.cc:142-143,177`).
  So the real client sends ~32 packets/sec to the server. We can send much slower
  (e.g. 10/s) as long as we ack and ping.
- `PingTimeout = 4500` ms, `DefaultPingRetryCount = 15` (`netConnection.cc:23-26`).
  If we send nothing for 4.5 s the engine would ping; if 15 pings go unanswered
  the peer times us out. **Send a DataPacket or Ping at least every few seconds**,
  and always Ack received Pings, to stay alive.
- `[CONFIRM-EXE]` rate constants (32/10/200) live in `pref::Net::*` cvars and the
  clamp in `checkMaxRate` (`netConnection.cc:157-186`); AoT may ship different
  prefs but the clamp ranges (updateDelay from packetRate 8..32, packetSize
  100..450) are intrinsic.

---

## 5. Minimal client send/receive loop (summary)

1. Pick a 32-bit `connectSequence` nonce.
2. Send ConnectChallengeRequest (§2.1); on ChallengeResponse (§2.2) store digest.
3. Send ConnectRequest (§2.3) with the AoT GameString / version / classCRC /
   args `[CONFIRM-EXE]`; on ConnectAccept (§2.4) go Connected, mirror the
   `onConnectionEstablished(true)` flags.
4. Run the packet/notify loop (§4): build headers, ack received data packets,
   reply to pings, notify on acks. Parse the rate block (§4.5) then hand the
   remaining bitstream to the event/ghost readers (event-system.md,
   mission-phases.md).

---

## Open questions / needs live confirmation

1. **`classCRC` (ConnectRequest field 9)** — must be captured from a real AoT
   ConnectRequest or extracted from AoT.exe's class-registration CRC. Cannot be
   derived from script. Top blocker.
2. **OOB packet-type enum values (26/30/32/36/34/38)** — verify unchanged in
   AoT's fork.
3. **`GameString` and protocol versions** (fields 10–12) — AoT-specific; capture.
4. **`connectArgc`/`connectArgv`** — what (if anything) AoT puts in connect args;
   and whether the registered connection className is `"GameConnection"`.
5. **Packet-header field widths** (1/1/9/9/2/3 + ackByteCount*8) — AoT is an older
   fork; diff a capture to confirm the 9-bit seq / 2-bit type / 3-bit count.
6. **Huffman string table** — confirm `csm_charFreqs` is byte-identical (strings
   appear in the first ConnectRequest, so a wrong table breaks the handshake).
7. **netClassGroup default** — assumed `NetClassGroupGame = 0`; confirm.
