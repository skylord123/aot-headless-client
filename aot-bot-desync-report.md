# aot-headless-client: protocol desync forcing a reconnect every ~15 min

## Symptom

The bot drops and re-establishes its game connection roughly every 15 minutes —
**17 reconnects in 4.0 hours** (23:55:19Z → 03:54:38Z on 2026-08-08/09).

This is **not** a container or process restart. The container reported
`RestartCount: 0` with 5h+ uptime, and the log contains a single process start.
The bot is deliberately tearing down its own connection.

## Root cause (fatal path)

`aotbot.client` hits an event `classId` it cannot decode. Because it also cannot
determine that event's length, it cannot skip it — the message literally says
`no generic skip` — so the bitstream stays misaligned and the only recovery is a
full reconnect.

Verbatim, one complete cycle:

```
21:54:38 ERROR aotbot.client: unrecoverable event-stream desync (cannot decode event classId 15 (no generic skip)); dropping connection to force a clean reconnect
21:54:38 INFO  aotbot.netconn: connection state -> disconnected
21:54:38 INFO  aotbot.main: connection closed
21:54:38 INFO  aotbot.transport: UDP transport closed
21:54:38 INFO  aotbot.main: reconnecting in 2.0s
21:54:40 INFO  aotbot.main: connecting to AoT server (resolve via master server https://master.ageoftime.com/server.txt) port 28000
21:54:40 INFO  aotbot.transport: UDP transport bound to ('0.0.0.0', 57971)
21:54:40 INFO  aotbot.netconn: connection state -> awaiting_challenge_response
21:54:40 INFO  aotbot.netconn: connection state -> awaiting_connect_response
21:54:40 INFO  aotbot.netconn: ConnectAccept (server protocol=11)
```

Fatal event classIds observed (17 total):

| event classId | occurrences |
|---:|---:|
| 15 | 9 |
| 14 | 4 |
| 6  | 3 |
| 11 | 1 |

Note: **every** `cannot decode event classId` occurrence was fatal — counts match
the desync counts exactly. There is no non-fatal path for an unknown event today.

Reconnect gaps in minutes: `45, 9, 3, 43, 2, 4, 10, 41, 6, 4, 15, 14, 2, 24, 9, 9`
(min 2, median 9, max 45).

## The server says the client is out of date

On **every** connect, immediately after `ConnectAccept`:

```
clientCmdServerMessage('MsgConnectionError', '', 'You do not have the correct
version of the Age of Time or the related art needed to play on this server,
please contact the server operator for more information.')
```

Handshake reports `ConnectAccept (server protocol=11)`. The undecodable events
and missing ghost decoders below are almost certainly downstream of this version
gap — the server is sending packet formats this client does not implement.

## Second issue: ghost `unpackUpdate` gaps (non-fatal, but 97.5% of all log output)

These do not drop the connection, but **127,219 of 130,418 log lines** are a
single repeated message for object class id 52. Measured growth: **~462 MB/day**.

Named in the class table but no `unpackUpdate` implementation (7):

| class | id | count |
|---|---:|---:|
| twSurfaceReference | 48 | 73 |
| WheeledVehicle | 39 | 38 |
| Splash | 30 | 25 |
| fxLight | 44 | 22 |
| FlyingVehicle | 5 | 11 |
| VehicleBlocker | 36 | 9 |
| PathCamera | 18 | 5 |

Not mapped to a name at all — the class-ID table appears stale (14 ids):

```
50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63
```

Counts for the unmapped ones are dominated by id 52 (127,219), then 63 (243),
56 (72), 55 (40), 51 (35), 50 (32), 57 (27), 61/54/53 (22 each), 60 (21),
62/58 (19), 59 (14).

Message format for reference:

```
WARNING aotbot.client: packet body alignment limit: ghost unpackUpdate not ported: no unpackUpdate decoder for object class <52> (id 52)
```

## Requested fixes, in priority order

1. **Give the event reader a length-prefixed generic skip.** An unknown `classId`
   should degrade to "skip this event and continue" rather than desyncing the
   stream. This is the durable fix — it also immunises the bot against future
   unknown events without another code change.
2. Implement/port decoders for event classIds **6, 11, 14, 15**.
3. Extend the ghost class table to cover ids **50–63**, and add `unpackUpdate`
   for the 7 named-but-unimplemented classes above.
4. Rate-limit or demote the `no unpackUpdate decoder` warning (dedupe per class,
   or log once per class per connection). At 462 MB/day it is the dominant
   consumer of disk and drowns out real events.
5. Investigate the client-version mismatch itself — if the protocol/art version
   can be brought in line with `server protocol=11`, items 2 and 3 may resolve
   at the source.

## Explicitly ruled out (do not chase these)

- **Container restarts** — `RestartCount: 0`, 5h+ continuous uptime.
- **The autoheal watchdog** — fired exactly once, during a deliberate test, and
  logs every action it takes.
- **Docker/host/network** — the bot's own WebSocket server (`:1234`) and its
  node-red client link stayed up continuously across all 17 game reconnects.

## Environment

- Image `ghcr.io/skylord123/aot-headless-client:latest`, Python 3.12.13, linux/arm64
- Runtime config: `auto_reconnect=True`, `auto_reconnect_interval=2.0`,
  `aot_server_port=28000`, `aot_track_objects=True`, `aot_skip_lighting=True`,
  `dump_packets=False`, `log_level='info'`,
  master server `https://master.ageoftime.com/server.txt`
- Reproduce: run normally and watch for `ERROR aotbot.client: unrecoverable
  event-stream desync`. With `log_level=debug` / `dump_packets=True` you should
  be able to capture the raw bytes of a classId 15 event, which is the highest
  value artifact for implementing either the decoder or the skip.
