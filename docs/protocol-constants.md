# Protocol constants — findings

All values live in `aotbot/protocol_constants.py` with per-constant source
comments. This doc summarizes methodology, what was confirmed from the EXE vs.
assumed from stock TGE 1.4, and what still needs a packet capture.

## Environment

- Target: `AgeOfTime.exe.original` — 32-bit PE, image base **0x400000**,
  non-relocatable. `.text` is flat-mapped (`fileoff == VA - 0x400000`).
- Engine: custom fork of TGE ~1.2/1.3 (DSO bytecode v33), compiled Jan 2009.
- Reference source: `TorqueGameEngine2005` (TGE **1.4**) — close but newer.
- Tools: capstone 5.x + pefile (static disassembly).

## Confirmed FROM THE EXE (high confidence)

| Constant | Value | Where in EXE |
|---|---|---|
| `GAME_STRING` | `"Age Of Time Demo"` | literal @ VA 0x5F6D48; pushed in `writeConnectRequest` @ 0x457AB6, compared in `readConnectRequest` @ 0x457BA2 (mismatch → "CHR_GAME" @ 0x5F6DD4) |
| `PROTOCOL_VERSION` | `11` | `writeConnectRequest` @ 0x457AC7 `mov ebx,0xb`, written @ 0x457AD4 |
| `MIN_PROTOCOL_VERSION` | `11` | same `0xb` written again @ 0x457AE6 |
| `CONNECT_CHALLENGE_REQUEST` | `26` (0x1A) | `sendConnectChallengeRequest` @ 0x54B6E4 writes byte 0x1A |
| `CONNECTION_CLASS_NAME` | `"GameConnection"` | literal @ VA 0x5F6EC8 |

**Two real divergences from stock TGE 1.4** were found and corrected:

1. `GAME_STRING`: stock is `"Torque Game Engine Demo"`; AoT is
   `"Age Of Time Demo"`.
2. `PROTOCOL_VERSION` / `MIN_PROTOCOL_VERSION`: stock is `12`; AoT is `11`.

The connect-request wire order (verified against the EXE's `writeConnectRequest`
@ 0x457AA0) is:

```
writeString(GameString="Age Of Time Demo")
write(U32 currentProtocol=11)
write(U32 minProtocol=11)
writeString(joinPassword)        ; server join password, "" if none
write(U32 connectArgc)
connectArgc * writeString(argv[i])
```

The handshake itself (challenge-request / challenge-response / connect-request
with 4-dword address digest) was confirmed to **structurally match stock TGE**
by disassembling `sendConnectChallengeRequest` (0x54B6C0),
`handleConnectChallengeResponse` (0x54BC20), and `sendConnectRequest`
(0x54BBxx). The handshake packet-type enum byte values (26/30/32/34/36/38) are
therefore taken as unchanged.

## Assumed from stock TGE 1.4 (UNCONFIRMED against AoT EXE)

These were NOT re-verified byte-for-byte in the binary. They are likely correct
(the fork is close and the handshake matched), but **diff against a real packet
capture before fully trusting them**, especially the bit-packed data-packet
header widths since AoT's fork is *older* than the 1.4 source:

- **Data-packet header bit layout** (from `engine/core/dnet.cc`
  `buildSendPacketHeader`): `1 game-flag | 1 connect-seq | 9 seq | 9 ackStart |
  2 type | 3 ackByteCount | (ackByteCount*8) ackMask`. Header = 3 + ackByteCount
  bytes. (The dnet.cc *comment* says "2 bits ack byte count / 4-9 byte header";
  the *code* uses 3 bits and a 3-byte fixed part — trust the code.)
- Packet sub-types: Data=0, Ping=1, Ack=2.
- Window/ack: window size 32, max ack bytes 4, 9-bit seq wraps at 0x200,
  reject if seq > lastRecvd + 31.
- String table: `EntryBitSize = 5`; packString type tags Null=0/String=1/
  Integer=2/CString=3.
- Ghosting: `GhostIdBitSize=12`, MaxGhostCount=4096, GhostIndexBitSize=4.
- Timing: PingTimeout=4500 ms, DefaultPingRetryCount=15.
- `MAX_CONNECT_ARGS = 16`.

Note: AoT added a `mGhostsActive` member to `NetConnection` (seen in the 1.4
header diff / fork). It changes the in-memory layout, not the wire enums, but it
confirms the fork is not a byte-exact match — another reason to verify widths
live.

## Prioritized list — STILL NEEDS LIVE / PACKET-CAPTURE CONFIRMATION

1. **Data-packet header bit widths & field order** (P0). Everything past the
   handshake depends on round-tripping this exactly. Highest risk because AoT's
   engine predates the 1.4 source. Capture a real client data packet and
   bit-decode.
2. **`MIN_PROTOCOL_VERSION` acceptance behavior** (P1). We confirmed the client
   *writes* 11/11; confirm the server accepts 11 and what it sends back in
   `ConnectAccept` (`writeConnectAccept` writes a U32 protocol version).
3. **Join password / connectArgc** (P1). Confirm whether AoT's server requires
   a non-empty join password or specific connect args (stock writes argc=0).
4. **String-table `EntryBitSize`** (P1). Login/chat verbs travel as
   RemoteCommandEvents through the tagged-string table; if AoT changed this the
   event payloads desync.
5. **Handshake packet-type values 30/32/34/36/38** (P2). 26 confirmed; the rest
   assumed sequential. Low risk but cheap to confirm in a capture.
6. **Ghost/string-table bit sizes** (P3). Only needed once you parse past the
   datablock phase.

## How the confirmed constants were located

1. String search in the EXE for `"getStringCRC"`, `"CHR_GAME"`, `"GameConnection"`,
   handshake log strings ("Sending Connect challenge Request", etc.).
2. For each interesting string, computed its VA and scanned `.text` for
   `push imm32` / `mov [..], imm32` references to that VA to find the function.
3. Disassembled the referencing functions with capstone and read the
   `writeString`/`write(U32)` sequence and immediate operands.
