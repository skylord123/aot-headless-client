# Real-client capture analysis (login wall)

Capture: `real_login.jsonl` (2376 datagrams) = the REAL AoT client's full
connect→load→phase-ack→login→chat session vs 45.148.165.55:28000, taken through
`tools/udp_relay.py`. The client logged in successfully (clientCmdLoginSuccess
for "Mr Poopy Butthole"). Decoder: `tools/decode_capture.py`.

Goal: find why our bot's events (MissionStartPhase1Ack / login) are dropped by
the server (server acks the packet but runs no serverCmd*).

## Established (client→server, c2s) — from the capture
- Packet header (1|1|9|9|2|3|ackmask) parses correctly both directions.
- After header: rate block = 2 flags (both 0 in capture).
- **GameConnection c2s control header** (server-read perspective):
  `camFlag(1) | checksum(32) | startMoveId(32) | moveCount(5) | fovFlag(1) [+ fov8]`
  - camFlag is **1** in 1077/1081 packets; there is **NO camera point** after it
    (reading a compressed point yields garbage; reading none yields checksum=0,
    startMoveId=small move-ack — clean). So camFlag=1 with no payload here.
  - checksum is 0 in the early/pre-login packets.
  - **moveCount is 2–3 in essentially every packet (1079/1081); our bot sends 0.**
- Moves use the **stock Move layout**, `MaxTriggerKeys=6`:
  `yawFlag[+16] | pitchFlag[+16] | rollFlag[+16] | px(6) | py(6) | pz(6) | freeLook(1) | 6×trigger(1)`.
  This byte-aligns the idle/empty-event packets (idx 58/62/64) under trig=6.

## The open diff (where it still breaks)
- Idle packets (empty event section) decode/byte-align fine with the above.
- Packets carrying real events do NOT decode with our event reader. e.g. idx56
  (first connected packet, cnt=2): after the 2 moves, our reader reads a
  NetStringEvent at slot 0 but the string comes out as garbage and overruns.
- That means the cursor entering the event section is off by a small number of
  bits on event-bearing packets — i.e. the move format is subtly non-stock
  (an extra/missing bit per move, or per-packet), OR the event/string framing
  diverges. Byte-padding tolerance (0–7 bits) is too coarse to have caught a
  1–2 bit move-size error on the idle packets.

## Suspect packets to decode first
- **idx56** (seq=1, cnt=2): the first guaranteed event the client sends —
  almost certainly `MissionStartPhase1Ack` (a RemoteCommandEvent, classId 7).
  Use this as the calibration target: adjust the move bit-count until idx56's
  event section decodes to a RemoteCommandEvent whose verb resolves to
  "MissionStartPhase1Ack". That pins the exact move size.
- The login packet (NetStringEvent teaching "login" + RemoteCommandEvent with
  args ["login", "Mr Poopy Butthole", "<passcrc>"]) is a slightly larger early
  c2s packet (look around idx 68 / the 55-byte packets). passcrc =
  getStringCRC("poopy") = zlib.crc32 (decimal).

## RESOLVED at the wire level (calibration + 4 fixes) — server gate persists

### Calibrated c2s move layout — CONFIRMED
`moveReadPacket` @ VA 0x45b5f0: `readInt(32)` startMoveId + `readInt(5)` count,
then count × `Move::unpack` @ VA 0x45b000. Disassembly: AoT's `Move::unpack` is
**byte-identical to stock TGE**:
`3×(readFlag rot [+readInt(16)]) | readInt(6)px | readInt(6)py | readInt(6)pz |
readFlag freeLook | 6×readFlag trigger` (MaxTriggerKeys=6, confirmed by the 7
flag stores at [edi+0x38..0x3e]; the `call 0x45ad70` is Move::unclamp = pure FPU,
0 bits). An **idle Move = 28 bits**, px=py=pz=16 (clamp maps 0.0→16). With this,
**all 1077 c2s data packets decode with zero errors**; seq=1 → MissionStartPhase1Ack.
moveCount is 1..29 (modal 2–3), **never 0**; startMoveId monotonically advances.

### Real login packet (c2s seq=92)
camFlag=1 | chk=0 | startMoveId=99 | moveCount=2 | 2 moves (1 idle, 1 real look) |
fov=0 | NetStringEvent(seq25, slot2→"login") | RemoteCommandEvent(seq26, argc=3:
TagString slot2, CString "Mr Poopy Butthole", Integer 433638644). The crc =
`getStringCRC("poopy") = zlib.crc32 = 433638644` (verified == wire).

### Fixes made (the diff vs the bot)
1. **moveCount 0 → ≥1 idle move stream** (phases.py `_write_moves`/`_write_idle_move`,
   28-bit idle Move, advancing startMoveId).
2. **No rate block → declare (updateDelay=32, packetSize=450) once** (netconn.py),
   mirroring the client's first c2s packet (stock default 102/200).
3. **Connect display name was the ACCOUNT name → "Fresh Meat"**
   ($pref::Player::Name default, prefs.cs:78) (client.py / protocol_constants.py).
4. **ROOT-CAUSE event-reliability bug** (events.py): `write_events` re-emitted
   every queued guaranteed event in EVERY packet and overwrote `sent_in_packet`,
   so the delivery notify never matched — the event was resent ~30×/s forever and
   never cleared. Fixed to emit only `sent_in_packet < 0` (new/NACKed) events;
   in-flight events wait for their notify. Also batched the verb-teach
   NetStringEvent + RemoteCommandEvent into ONE flush/packet (as the client does).
   Keepalive now sends a move-bearing DATA packet every 100 ms (not a move-less
   ping).

After these, the bot's Phase1Ack/login bodies decode byte-structurally identical
to the real client's, and the event is delivered+cleared exactly once.

### LIVE result — STILL blocked at MissionStartPhase1 (server-side)
handshake/ConnectAccept/CONNECTED PASS; receives clientCmdMissionStartPhase1,
sends a byte-perfect Phase1Ack, the **server acks it delivered** — then sends
**only empty 10-byte DATA packets** (301 server data packets, only the first 2
carry content; 299 empty). No datablocks, no DataBlocksDone, no Phase2/MissionStart,
so login never starts. Tried additionally: proactively sending
Phase2Ack/Phase3Ack/DataBlocksDownloadDone(7), connect_sequence=0 — no change.

**Conclusion:** the bot's connect→Phase1Ack bytes now match the genuine client's,
yet the live AoT *server* withholds the mission stream and never runs
`serverCmdMissionStartPhase1Ack`. The gate is **server-side, not represented by
any byte difference in this client-side capture** (the real client, same bytes,
succeeds). Hypotheses: per-account/per-IP gate or stale "online" session; the
live server is newer than the capture; or an out-of-band art/resource validation.
Next: a FRESH capture of the genuine client vs the CURRENT live server, or the AoT
server binary/scripts (not in our files).

## Wave-6 — Phase-2 (datablock + ghost) decode progress

Replay the REAL client's s2c stream through the production read path to validate
the per-class decoders bit-exactly:

    .venv/bin/python tools/replay_s2c.py        # full s2c progress + blocker histogram
    .venv/bin/python tools/check_datablocks.py  # datablock-stream progress + first block

Clean s2c packet decode: **131 -> 1042** this wave (GhostAlwaysObjectEvent +
ghost-section framing + 9 datablock unpackData classes). Datablock stream decodes
the first **33 of 436** in order with zero desync; first un-ported datablock is
ParticleEmitterData (classId 15). Ghost section framing is correct; per-class
NetObject unpackUpdate is the remaining ghost work (aotbot/ghosts.py). See
docs/re-deep-findings.md "Wave-6" for the full method table, VAs, and the exact
remaining classes/next steps.

Run the bot live (LIVE server — clean connects only, one session at a time):

    .venv/bin/python -m aotbot.main            # uses .env (creds, host/port)
    # or against the relay for capture:
    .venv/bin/python -m aotbot.main --host 127.0.0.1 --port 28000

## Wave-23 — combat_session.jsonl (live combat NACK-storm capture)

`combat_session.jsonl` (4367 lines, 2173 s2c DATA packets): a live session with
active melee combat near the bot — the capture that reproduced the "173 NACKed
packets in 150s" bitstream-overrun storm. Two independent findings, both fixed:

1. **ShapeBase sound-thread polarity bug (ghosts.py)**: the 4-slot loop @
   0x4840f0..0x4841aa (previously mislabeled "image-skin") is TGE's SoundMaskN.
   The 10-bit sound datablock id @0x484170 is read when the per-slot *play*
   flag (setne @0x484147) is **SET** (`cmp al,1; jne 0x48417f` @0x484150
   skips the read when clear) — the transcription had it inverted, silently
   under-reading 10 bits per playing sound. Harmless pre-combat (no sounds),
   fatal the moment weapon sounds fire on any ShapeBase-derived ghost.
2. **Ack-faithful replay (tools/ack_replay.py)**: from line 4119 the capture is
   only decodable when the replay honors the REAL client's c2s ack verdicts —
   the then-buggy live client NACKed whole windows (ackMask=0), so the server
   re-sent ghost CREATES; a replay that commits every cleanly decoded packet
   parses the re-sent create as an update. replay_s2c_audit / trace_capture_pkt
   now stage each packet's ghost-table diff and roll it back on a NACK verdict
   (server handleNotify -> packetDropped mirror). Captures without c2s lines
   are unaffected.

Audit after both fixes: 0 exceptions, 0 bs.error, 11 benign 8-bit pad
residuals (was 151 exceptions / 21 bs.error / 91 residuals up to 3197 bits).
