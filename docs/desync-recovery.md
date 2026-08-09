# Desync recovery: NACKing undecodable packets

How the client survives a packet body it cannot fully decode without either
tearing the connection down (the old behavior: a reconnect every few minutes
whenever a decoder gap was hit) or silently diverging (ACKing content it never
processed — the old "session zombie" / ghost-table poison cascade).

All engine citations are into /home/skylar/Projects/TorqueGameEngine2005.

## The mechanism

Packet loss is a first-class, fully-recoverable event in the Torque dnet
protocol, and the receiver is the one who reports it: each outgoing packet
header carries `lastSeqRecvd` + an ack bitmask, and a **0 bit for a seq is the
"that packet was lost" signal** (dnet.cc:183-187 shifts the mask and only ORs
in a 1 for received DataPackets). The sender's `handleNotify` walk
(dnet.cc:190-206) then fires `packetDropped` for holes, which:

* re-queues guaranteed(-ordered) events **with their original seq**
  (netEvent.cc:81-95) for retransmission;
* re-marks the dropped ghost states dirty (netGhost.cc:150-210), restoring
  `NotYetGhosted` for an un-acked new ghost (netGhost.cc:192-196) so its
  one-shot **classId is sent again** (netGhost.cc:401-411). Ghost removals are
  reliable the same way (netGhost.cc:201-205).

So when `client._read_body` fails (an `AlignmentError` from a decoder gap, or
`bs.error` = the body ran past the stream end), it returns False and
`netconn._process_raw_packet` clears the just-set ack bit (`ack_mask &= ~1`).
The window still advances (`last_seq_recvd` = the failed seq — the engine does
this unconditionally too, dnet.cc:230). The server re-sends everything reliable
that rode that packet, and we get a clean retry a tick later. Transient decode
failures (e.g. a rare state of one object tripping a decoder bug) heal
completely; unguaranteed events (audio cues etc.) are the only loss
(netEvent.cc:110-115), by design.

## What makes the retry safe

The engine never re-sends acked reliable content, so stock Torque has **no
duplicate protection at all** — its receive path would alias a re-delivered
ordered seq +128 and fire it late (netEvent.cc:312-314). Our deliberate NACKs
can re-deliver events we DID already process (the failure may have hit later in
the packet), so two guards make the retry exactly-once:

1. **Ordered-event duplicate filter** (events.py `read_events`): the server
   sends ordered events strictly in seq order and re-queues dropped ones by
   their original seq, so the receiver's expected next seq identifies dups
   unambiguously. A dup is parsed bit-exactly (alignment) but not dispatched
   (no clientCmd handlers, no connection-message hooks — the things that emit
   user events or send replies). Idempotent state (string-table mappings, ghost
   scoping, registry updates) is re-applied harmlessly. An event's seq is only
   consumed after its payload parses fully, so a mid-payload failure leaves it
   eligible for a fresh dispatch on re-delivery.

2. **Transactional ghost section** (phases.py `_read_ghost_section`): all
   `_ghost_classes` / registry mutations are staged and committed only when the
   whole section parses. The client's new-vs-existing branch is decided purely
   by local-table presence (netGhost.cc:481), mirrored server-side by
   `NotYetGhosted` — an eagerly-committed entry from a failed packet permanently
   diverged the two views and misparsed every later update for that ghost id
   (the 127k-line "no unpackUpdate decoder for object class <52>" flood came
   from exactly one such poisoned entry). Staging + NACK keeps both sides in
   lockstep: an un-acked create is re-sent with classId, and the server sends
   NO updates for a ghost whose create is un-acked (netGhost.cc:369-370), so
   there is no race.

## Escalation

A packet the server re-sends that we can *never* decode would otherwise loop
forever (there is no retransmit backoff). `client.BODY_FAIL_STREAK_LIMIT`
consecutive undecodable packets (~5 s at the server's packet rate — re-sent
undecodable content fails every packet, transient trouble does not) escalates
to the old behavior: drop + auto-reconnect. Every failure logs at WARNING with
the cause, per-cause count, raw packet hex and failing bit position, so the
remaining decoder gap can be replayed offline (tools/trace_capture_pkt.py,
tools/replay_s2c_audit.py) and fixed.
