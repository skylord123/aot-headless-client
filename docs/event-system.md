# Event system — NetEvent, RemoteCommandEvent, tagged strings

This documents how `NetEvent`s ride inside DataPacket bodies, the exact
`RemoteCommandEvent` wire format (`commandToServer` / `clientCmd*`), and the
`NetStringTable` tagged-string negotiation in both directions. It ends with the
concrete AoT login/chat flows.

Citations are `file:line` into `/home/skylar/Projects/TorqueGameEngine2005`
(`$TGE`). AoT = custom fork, DSO v33. Divergence risks flagged `[CONFIRM-EXE]`.

Prereq: read handshake.md first — events live in the DataPacket body that
`handlePacket` reaches after the header + rate block (handshake.md §4.5).
Packet body order is: `eventReadPacket` **then** `ghostReadPacket`
(`netConnection.cc:660-664`); for GameConnection the move/control header comes
first (mission-phases.md). To reach the events you must consume the move/control
header exactly.

---

## 1. Where events sit in the packet

`NetConnection::readPacket` → `eventReadPacket(bstream)` → `ghostReadPacket`
(`netConnection.cc:660-664`). On the client (`GameConnection::readPacket`,
`gameConnection.cc:741-867`) there is a control/move header *before*
`Parent::readPacket`, so the read order in a server→client DataPacket body is:

```
[rate block: 2 flags ± rate ints]      (handshake.md §4.5)
[GameConnection control header]        (mission-phases.md §3)
[event section]                        (this doc, §2)
[ghost section]                        (mission-phases.md §5)
```

`[CONFIRM-EXE]` Note `TORQUE_DEBUG_NET` is **off** in release builds (it's
`#define`d but commented out, `netConnection.h:39`). So the 32-bit `DebugChecksum`
that `eventReadPacket`/`eventWritePacket`/`ghostReadPacket` would emit
(`netEvent.cc:160-162,243-246`, `netGhost.cc:448-451`) is **NOT on the wire** in a
normal AoT build. Do not read/write it. (If alignment fails, a debug-net server
build is one thing to check, but assume release.)

---

## 2. Event section wire format — `eventReadPacket` (`netEvent.cc:241-342`)

Events come in two phases in one packet: first all **unguaranteed** events, then
**guaranteed/guaranteed-ordered** events. The phase boundary and per-event
presence are encoded with single flag bits. Exact reader logic:

```
prevSeq = -2
unguaranteedPhase = true
loop:
    bit = readFlag()
    if unguaranteedPhase and bit == 0:        # end of unguaranteed phase
        unguaranteedPhase = false
        bit = readFlag()                       # now a guaranteed-event presence bit
    if not unguaranteedPhase and bit == 0:     # 0 here = end of event section
        break
    # bit == 1 -> an event follows
    seq = -1
    if not unguaranteedPhase:                  # read the ordered sequence number
        if readFlag():  seq = (prevSeq + 1) & 0x7F      # "same as +1" shortcut
        else:           seq = readInt(7)
        prevSeq = seq
    classId = readClassId(NetClassTypeEvent, netClassGroup)   # see §3
    ... instantiate event by classId, call evt->unpack(...) ...
    if unguaranteedPhase:
        process immediately
    else:
        queue by seq for in-order processing
```

So the section is: `(presence-bit, [seq], classId, payload)*` with a `0`
separator between the unguaranteed and guaranteed phases and a trailing `0`.
This is the loop at `netEvent.cc:252-328`. The **write** side mirror is
`eventWritePacket` (`netEvent.cc:158-239`): writes all unordered events each
prefixed by `writeFlag(true)`, then `writeFlag(false)` to end phase 1, then
ordered events each `writeFlag(true)` + a seq, then `writeFlag(false)` to end,
then **one more `writeFlag(0)`** (`netEvent.cc:238`). To align reads we must
faithfully reproduce both phases even when we send no events (just the two
terminating `0` bits — i.e. the minimum event section a client emits is
`writeFlag(false)` (end unguaranteed) then `writeFlag(false)` (end guaranteed)).

### 2.1 classId — `readClassId` / `writeClassId` (`bitStream.cc:170-184`)

`writeInt(classId, NetClassBitSize[group][NetClassTypeEvent])` — a **variable
bit width** determined by how many event classes are registered in the group.
The width is `ceil(log2(count))`-ish (the engine stores it per group/type at
class-registration time). `readClassId` returns −1 if the value exceeds the
registered count (→ `setLastError`, drops the connection).

`[CONFIRM-EXE]` **The event class-ID bit width and the ID↔class mapping are
intrinsic to the exe** (registration order of every `IMPLEMENT_CO_*EVENT`). We
must learn, for AoT specifically:
- the bit width of an event classId, and
- the classId of `RemoteCommandEvent` and `NetStringEvent`.

In stock 1.4 these are assigned by link order and are not a stable documented
number. **Capture a known event** (e.g. our own `commandToServer('Talk',...)`
echoed, or the server's first `clientCmd*`) and read the bits to recover the
width + RemoteCommandEvent id. This is the second-biggest unknown after classCRC.

Event direction is enforced (`netEvent.cc:286-291`): a `NetEventDirServerToClient`
event arriving on a connection that *is* "to server" is fine; the asserts only
reject wrong-direction. `RemoteCommandEvent` is `NetEventDirAny`
(`IMPLEMENT_CO_NETEVENT_V1`, net.cc:156) so it flows both ways.

### 2.2 Ordered sequence numbers

Guaranteed-ordered events carry a 7-bit seq (mod 128). Receiver reconstructs the
full seq (`netEvent.cc:312-314`) and only `process()`es them in order once the
contiguous run from `mNextRecvEventSeq` is present (`netEvent.cc:329-341`). For a
read-only/align-only consumer we still must parse each event's payload to advance
the bitstream, even if we don't act on it. `RemoteCommandEvent` is
GuaranteedOrdered (NetEvent default, `netConnection.h:254`).

---

## 3. RemoteCommandEvent — the heart of chat & login

Source: `$TGE/engine/game/net/net.cc:24-195`. This is the class behind both
`commandToServer(verb, args...)` (client→server) and the server's `clientCmd*`
(server→client). Constants (`net.cc:27-30`):
- `MaxRemoteCommandArgs = 20`
- `CommandArgsBits = 5` (argc is written in 5 bits → max 31, capped at 20)

### 3.1 pack (send) — `net.cc:74-83`

After the event header (presence bit + seq + classId from §2), the payload is:

| # | field | bits | value |
|---|-------|------|-------|
| 1 | argc | `writeInt(argc, 5)` | number of args **including the verb** |
| 2.. | each arg | `packString` | args[0]=verb, args[1..]=params, in order |

Each arg is `conn->packString(bstream, mArgv[i+1])` (`net.cc:81-82`). The args
are NOT reversed on the wire despite the misleading comment — the loop is
`for i in 0..argc: packString(argv[i+1])` in natural order.

**`commandToServer('login', %user, %hash)`** therefore packs argc=3 then three
packStrings: the tagged verb, `%user`, `%hash`.

### 3.2 packString — string-or-tag-or-int encoding (`netConnection.cc:886-928`)

This is how each argument is encoded. A 2-bit type prefix
(`NetStringConstants`, `netConnection.cc:869-875`):

```
NullString = 0, CString = 1, TagString = 2, Integer = 3
```

| input | wire |
|-------|------|
| empty string | `writeInt(0,2)` (NullString) — done |
| starts with `StringTagPrefixByte` (a tagged-string literal `\x01<digits>`) | `writeInt(2,2)` then `writeInt(tagId, 5)` — the **5-bit connection-local string-table id** (`ConnectionStringTable::EntryBitSize=5`, connectionStringTable.h:17) |
| looks like an integer (`-`/digit, round-trips through %d) | `writeInt(3,2)` then sign flag, then `writeFlag(num<128)?writeInt(num,7) : writeFlag(num<32768)?writeInt(num,15) : writeInt(num,31)` |
| any other string | `writeInt(1,2)` then `writeString(str)` (Huffman, handshake.md §0) |

`StringTagPrefixByte` is `0x01` (`[CONFIRM-EXE]` — it's `StringTable`'s prefix;
1.x uses 0x01. A tagged-string literal in script looks like `\x01"123"`). The
verb (`'login'`, `'Talk'`, `'MessageSent'`) is a **tagged string**: a script
single-quote literal becomes a NetStringTable tag. So argv[0]/the verb encodes
as TagString (type 2 + 5-bit id), and the server resolves the id via the string
table (§4). User/pass args are normal CStrings or Integers.

### 3.3 unpack (receive) — `net.cc:90-100` + `unpackString` (`netConnection.cc:930-961`)

```
argc = readInt(5)
for i in 0..argc:
    argv[i] = unpackString()
```

`unpackString` reads the 2-bit type and reverses §3.2:
- 0 → empty string
- 1 → `readString()` (Huffman)
- 2 → `readInt(5)` → a tag; reconstruct the literal as `StringTagPrefixByte` +
  decimal id (`netConnection.cc:941-946`). Resolution to text happens in
  `process()`.
- 3 → sign flag, then `readFlag()`-gated 7/15/31-bit int → decimal string.

### 3.4 process — dispatch to clientCmd* / serverCmd* (`net.cc:102-150`)

On `process()`, every **tagged** arg is de-tagged via the string table and
expanded (`%1..%9` substitution from following args, `net.cc:108-124`,
`NetStringTable::expandString` at netStringTable.cc:182-225). Then:

- **On the client** (`conn->isConnectionToServer()` true, `net.cc:126-136`): the
  verb (the de-tagged argv[1], stripped to text after the first space) is
  prefixed with **`clientCmd`** and the resulting console function is executed
  with the remaining args:
  ```
  clientCmd<Verb>(arg1, arg2, ...)
  ```
  So when the server sends a RemoteCommandEvent with verb `ChatMessage`, the
  client runs `clientCmdChatMessage(...)`. With verb `ServerMessage` →
  `clientCmdServerMessage(...)`. With verb `WarningBox` →
  `clientCmdWarningBox(...)`. With verb `LoginSuccess` → `clientCmdLoginSuccess()`.
- **On the server** (the symmetric case): verb gets `serverCmd` prefix and is
  called as `serverCmd<Verb>(%clientId, args...)`. This is how our
  `commandToServer('login', u, h)` becomes the server's `serverCmdLogin(%client,
  u, h)`. We never run this side; documented for completeness.

So **our Python bot, acting as the client**, must:
- To send: build a RemoteCommandEvent payload: `writeInt(argc,5)` then packString
  each of `[verbTag, args...]`.
- To receive: read argc, unpackString each, de-tag argv[0] via our string table,
  and route on the verb name → emit a structured `clientCmd<Verb>(args)` event.

---

## 4. NetStringTable / ConnectionStringTable negotiation (both directions)

There are **two** tables:
- **Global `NetStringTable`** (`netStringTable.{cc,h}`): the process-wide pool of
  "tagged strings". `getTaggedString`/`addTaggedString` (net.cc:197-219) map
  text ↔ a global index. A script tagged literal carries this global index.
- **Per-connection `ConnectionStringTable`** (`connectionStringTable.{cc,h}`): a
  32-entry LRU window (`EntryCount=32`, `EntryBitSize=5`) that maps a global
  string to a **5-bit on-the-wire id** for THIS connection, negotiated lazily.

### 4.1 Send side (how a verb's tag gets a 5-bit id)

When we `packString` a TagString, the 5-bit id is the **connection-local** id,
obtained by `getNetSendId` (`net.cc:49`, `connectionStringTable.cc:86-95`). Before
sending, `validateSendString`→`checkString` (`netConnection.cc:877-884`,
`connectionStringTable.cc:97-139`) ensures the string is in the connection table;
if it isn't, it allocates an LRU slot AND **posts a `NetStringEvent`** to teach
the peer that mapping. So:

1. First time we reference verb `'Talk'`: `checkString` finds it absent, picks an
   LRU slot (say id 7), posts `NetStringEvent(index=7, "Talk")`.
2. That `NetStringEvent` (its own event class, §4.3) travels guaranteed-ordered
   to the server; on delivery the server `mapString(7,"Talk")`.
3. Our subsequent `RemoteCommandEvent` packs the verb as TagString id=7; the
   server resolves id 7 → "Talk" in its `mRemoteStringTable`.

`receiveConfirmed` (set on `notifyDelivered`, connectionStringTable.cc:57-61)
tracks whether the peer has the mapping yet. **Our implementation must maintain a
32-slot LRU send table and emit a NetStringEvent the first time we use any
tagged string (every verb).** The id is `string.getIndex() % 32` for the hash
bucket but the actual slot is LRU-assigned.

### 4.2 Receive side (resolving an incoming 5-bit id)

Incoming TagString id → `mRemoteStringTable[id]` (`unpackStringHandleU`/
`translateRemoteStringId`, `netConnection.cc:976-991`, `net.cc:115`). That table
is filled by **incoming `NetStringEvent`s** from the server
(`connectionStringTable.cc:141-145`, process at netStringTable... actually
`NetStringEvent::process` → `connection->mapString`, connectionStringTable.cc).
So: when the server first sends us `clientCmdChatMessage`, it will first (or in
the same window) send a `NetStringEvent` mapping the verb tag `ChatMessage` → an
id, then the RemoteCommandEvent referencing that id. **We must process incoming
NetStringEvents and keep a 32-entry remote table so we can de-tag verbs.**

### 4.3 NetStringEvent wire format (`connectionStringTable.cc:13-63`)

A `NetEvent` (its own classId, NetEventDirAny via `IMPLEMENT_CO_NETEVENT_V1`).
pack/unpack (`connectionStringTable.cc:23-39`):

| # | field | bits |
|---|-------|------|
| 1 | index | `writeInt/readInt(EntryBitSize=5)` |
| 2 | string | `writeString`/`readString` (Huffman, full text) |

So a NetStringEvent is just `(5-bit slot, Huffman string)`. process →
`mapString(index, string)` into the receiver's remote table
(`connectionStringTable.cc:45-49`).

`[CONFIRM-EXE]` NetStringEvent's classId (and whether it's even a separate class
in AoT's fork) must be recovered from a capture — but since strings are negotiated
this way for every verb, you'll see them right before the first `clientCmd*`.

### 4.4 Tagged vs plain on send/receive (from the AoT scripts)

- **Send side**: a script literal in single quotes (`'login'`, `'Talk'`) is a
  tagged string → TagString on the wire. Plain string args (`%user`,
  `getStringCRC(%pass)` result) are CString/Integer.
- **Receive side**: incoming tagged args are resolved with `detag()` in script
  (login.cs:58,88 — `detag(%warnText)`, `detag(%msgString)`). On our side the
  equivalent is: TagString id → remote table lookup → text.

---

## 5. Concrete AoT flows

### 5.1 Login — `base/skylord/helpers/login.cs`

Send: `commandToServer('login', %user, getStringCRC(%pass))` (login.cs:42).
On the wire: RemoteCommandEvent, argc=3, args = [TagString(`login`),
CString(user), Integer-or-CString(crc)].

- `getStringCRC(%pass)` is an **AoT-added console function** — it does **not**
  exist in the 1.4 source. The engine CRC primitive is `calculateCRC`
  (`$TGE/engine/core/crc.cc:38-49`): a standard **CRC-32 (poly 0xEDB88320,
  little-endian/reflected), init `0xFFFFFFFF`** (crc.h:9). The result here is
  returned **without final XOR/inversion** in `calculateCRC` itself (it returns
  `crcVal` directly, no `^ 0xFFFFFFFF`). `[CONFIRM-EXE]` Whether AoT's
  `getStringCRC` wraps `calculateCRC` over the raw password bytes and whether it
  final-inverts MUST be confirmed by reproducing a known value from the live
  console (e.g. `getStringCRC("test")`) and matching byte-for-byte. The login
  hash must match the server exactly, so get this from the running game, not
  from this doc. The CRC value then encodes as packString Integer (type 3) if it
  round-trips as a signed %d, else CString — note a 32-bit CRC > 0x7FFFFFFF
  prints as a negative or large decimal; verify how the client stringifies it
  (it's the return of a console function, so it's already a decimal string by
  the time packString sees it).

Success: the SPEC names `clientCmdLoginSuccess` as authoritative. **But the AoT
bot script actually detects success via the broadcast chat/server message**
`"<user> logged in."` (login.cs:80-107, hooking `clientCmdServerMessage` →
`onServerMessage`). So watch for BOTH:
- `clientCmdLoginSuccess()` RemoteCommandEvent (verb `LoginSuccess`), and
- a `clientCmdServerMessage`/`clientCmdChatMessage` whose de-tagged text equals
  `"<user> logged in."`.

Failure: `clientCmdWarningBox(%warnText, %btnText)` (verb `WarningBox`,
login.cs:51). `%warnText` is a **tagged string** — de-tag it for the error text
(e.g. `"Character does not exist!"`). login.cs does `trim(detag(%warnText))`.

`[CONFIRM-EXE]` The exact server verbs (`LoginSuccess`, `WarningBox`,
`ServerMessage`, `ChatMessage`) are AoT server-script driven, inferred from the
client handlers. They're almost certainly correct (the client `clientCmd*`
handler names prove the verb names), but confirm `LoginSuccess` actually fires
vs. only the chat broadcast.

### 5.2 Chat send — `base/skylord/helpers/generic.cs:225-234`

```
say(%local, %x):
    if %x == lastMsg: %x = %x @ "-"     # dedup guard
    if %local: commandToServer('Talk', %x)        # local/proximity
    else:      commandToServer('MessageSent', %x)  # global
```

Wire: RemoteCommandEvent argc=2, args=[TagString(`Talk`|`MessageSent`),
CString(text)]. Replicate the trailing-`-` dedup only if you want exact parity.

### 5.3 Chat receive — `base/skylord/helpers/chat.cs`, `base/MoAScripts.cs:138`

Server sends `clientCmdChatMessage(<chatHudLine>)` (verb `ChatMessage`,
argc=2). The AoT client routes it to `onChatMessage(%x)` (MoAScripts.cs:138 is
the stock handler; chat.cs:8 `Chat_onChatMessage` hooks the same). The single arg
is a **preformatted HUD line** (possibly carrying ML control chars; AoT strips
them with `StripMLControlChars`). Parse exactly like the script:

```
m = StripMLControlChars(line)
isLocal = (strStr(m,":") > strStr(m,",") && both >= 0) || strStr(m,":") <= 0
if isLocal:
    name = m[0 : indexof(' says, "')]
    msg  = substring between the first '"' and the matching closing '"'
else:                          # global
    name = m[0 : indexof(':')]
    msg  = m[indexof(':')+2 : end]
emit {scope: isLocal?'local':'global', name, message: msg, raw: line}
```

(chat.cs:10-30 / MoAScripts.cs:141-157.) `[CONFIRM-EXE]` whether `ChatMessage`
carries extra trailing args beyond the HUD line — parse defensively (argc may be
> 2). Note local lines look like `Name says, "text"`; global like `Name: text`.

### 5.4 System / server messages — `clientCmdServerMessage` / `onServerMessage`

Verb `ServerMessage`. Used for login/logout announcements and system notices
(login.cs hooks it). Signature seen in script:
`clientCmdServerMessage(%msgType, %msgString, %a1..%a10)` (login.cs:80). The
`%msgString` is a **tagged string** → de-tag it. Forward `{type: msgType, text:
detag(msgString)}` to Node-RED. The `"<user> logged in."` login confirmation
arrives here.

---

## 6. Node-RED bridge mapping (from NodeRED.cs)

(Listed here because chat/login events feed it; full grammar is the
implementation agent's to define per SPEC.) `base/skylord/NodeRED.cs`:
- Bot is a **TCP client** to `localhost:1881` default.
- **Outbound** (bot→Node-RED): `send(msg @ "\n\n\n")` — message + three newlines
  terminator (NodeRED.cs:87). Forward parsed chat, server messages, and
  connection/login state changes.
- **Inbound** (Node-RED→bot): line-based `onLine(%line)` (NodeRED.cs:146). Each
  line is a bot command; map to the verbs above (e.g. `say`→`Talk`,
  `global`→`MessageSent`, `login`, `raw <verb> <args>`→`commandToServer`).
- Auto-reconnect with backoff: 1 s for first 5 attempts then 5 s (NodeRED.cs:95).

---

## Open questions / needs live confirmation

1. **Event classId bit width + RemoteCommandEvent / NetStringEvent ids** — must
   be recovered from a capture (or AoT.exe class registration). Without it you
   cannot decode any event. Second-biggest blocker after classCRC.
2. **`getStringCRC` exact algorithm** — confirm it's `calculateCRC` (CRC-32
   reflected, init 0xFFFFFFFF) over the raw password bytes, and whether it
   final-inverts. Validate against a known value from the live console.
3. **`StringTagPrefixByte` value** — assumed `0x01`; confirm.
4. **Server verb names** (`LoginSuccess`, `WarningBox`, `ServerMessage`,
   `ChatMessage`, and whether login success fires `clientCmdLoginSuccess` vs only
   the chat broadcast).
5. **`ChatMessage` / `ServerMessage` argc and arg layout** — parse defensively;
   confirm extra args.
6. **`TORQUE_DEBUG_NET` off** in AoT's build (assumed; means no per-event 32-bit
   checksums). If event reads desync immediately, this is the first thing to test.
7. **CommandArgsBits=5 / EntryBitSize=5** widths — likely unchanged but verify on
   the older fork.
