"""Age of Time / Torque network protocol constants.

Source legend per constant:
  EXE  = confirmed by disassembling AgeOfTime.exe.original
         (image base 0x400000; for .text, fileoff == VA - 0x400000).
  TGE  = stock TorqueGameEngine2005 source (TGE 1.4) at
         /home/skylar/Projects/TorqueGameEngine2005
  Constants marked  # UNCONFIRMED  are stock TGE 1.4 values that were NOT
  re-verified against the AoT exe; they are very likely correct (the handshake
  structurally matches stock TGE) but should be diffed against a real packet
  capture before being fully trusted.

AoT runs a custom fork of TGE ~1.2/1.3 (DSO bytecode v33), compiled Jan 2009.
Two confirmed divergences from stock TGE 1.4: GAME_STRING and PROTOCOL_VERSION.

Deep-RE pass (see docs/re-deep-findings.md) additionally CONFIRMED from the exe:
  * the full networkable-class manifest (50 Object / 34 DataBlock / 14 Event
    classes in NetClassGroupGame), hence the per-type classId bit widths and the
    specific event classIds (RemoteCommandEvent=7, NetStringEvent=5);
  * the data-packet header bit layout (buildSendPacketHeader @ VA 0x422920):
    1|1|9|9|2|3|ackByteCount*8, identical to stock TGE;
  * the ConnectRequest classCRC: AoT does NOT recompute the class CRC -- the
    table AbstractClassRep::classCRC[] @ VA 0x638d4c is the static initializer
    {0xFFFFFFFF, 0, 0, 0} and nothing writes it, so classCRC[group 0] is the
    constant 0xFFFFFFFF and netClassGroup is 0.
A read-only live UDP probe of 45.148.165.55:28000 also confirmed the OOB
challenge handshake (type 26 -> reply type 30, seq echoed, 16-byte digest).
"""

# =============================================================================
# GameConnection connect-request identity
# =============================================================================

# The game string written by GameConnection::writeConnectRequest and checked by
# readConnectRequest. STOCK TGE 1.4 uses "Torque Game Engine Demo"; AoT changed
# it. EXE: string literal @ VA 0x5F6D48 (fileoff 0x1F6D48); pushed in
# writeConnectRequest @ VA 0x457AB6 and compared in readConnectRequest @
# VA 0x457BA2 (mismatch -> "CHR_GAME" error @ VA 0x5F6DD4).
# Source: TGE engine/game/gameConnection.h:39 (#define GameString ...).
GAME_STRING = "Age Of Time Demo"  # EXE-confirmed (differs from stock TGE)

# Protocol version. Both CurrentProtocolVersion and MinRequiredProtocolVersion
# are written as U32 immediately after the game string. STOCK TGE 1.4 == 12;
# AoT uses 11. EXE: GameConnection::writeConnectRequest @ VA 0x457AC7 sets
# `mov ebx, 0xb` then writes that 4-byte value twice (current then min) at
# VA 0x457AD4 / 0x457AE6. Source: TGE gameConnection.cc:27-28 (=12 in 1.4).
PROTOCOL_VERSION = 11      # EXE-confirmed CurrentProtocolVersion (stock=12)
MIN_PROTOCOL_VERSION = 11  # EXE-confirmed MinRequiredProtocolVersion (stock=12)

# Connect-request wire order (GameConnection::writeConnectRequest), after the
# NetInterface header. Source: TGE gameConnection.cc:213-224, EXE @ 0x457AA0:
#   1. writeString(GameString)            -> "Age Of Time Demo"
#   2. write(U32 CurrentProtocolVersion)  -> 11
#   3. write(U32 MinRequiredProtocolVersion) -> 11
#   4. writeString(mJoinPassword)         -> server join password ("" if none)
#   5. write(U32 mConnectArgc)
#   6. mConnectArgc * writeString(argv[i])
# NetConnection::writeConnectRequest (Parent) writes nothing extra in 1.4.

# Max number of connect args (mConnectArgv). TGE gameConnection.h:109.
MAX_CONNECT_ARGS = 16  # UNCONFIRMED (stock TGE 1.4 value; verify against AoT)

# The connect args the genuine AoT client passes. CONFIRMED from the decompiled
# client scripts: MM_Connect() / MJ_connect() / JoinServerGui do
#     $conn.setConnectArgs($version, $pref::Player::Name)
# so connectArgc=2 and argv = [ "<$version>", "<PlayerName>" ]. The server's
# onConnect validates argv[0] against its own version and otherwise rejects with
# the in-game "You do not have the newest version" Disconnect. $version is the
# integer global set in AgeOfTime/main.cs:6 -> `$version = 29;`. (The engine's
# getVersionNumber() returns 3 and getBuildString() formats 0.003, but the
# script-level $version used for the connect handshake is 29 -- confirmed live:
# sending argc=0 gets ConnectAccept then an immediate version Disconnect.)
CLIENT_VERSION = "29"  # AgeOfTime/main.cs: $version = 29 (sent as connect arg 0)

# Connect arg 1 is the PRE-LOGIN display name $pref::Player::Name, NOT the
# account username. CAPTURE-CONFIRMED: the genuine client's ConnectRequest in
# tools/captures/real_login.jsonl sends argv = ["29", "Fresh Meat"] (the prefs
# default, AgeOfTime/base/client/prefs.cs:78), then logs in to the account
# "Mr Poopy Butthole" via a separate commandToServer('login', user, crc). Sending
# the ACCOUNT name as the connect display name made the AoT server refuse to
# advance past MissionStartPhase1 (it never streamed datablocks / sent Phase2),
# so login never started -- the real "events dropped after Phase1" gate. The
# display name is cosmetic pre-login; "Fresh Meat" mirrors the real client.
DEFAULT_PLAYER_NAME = "Fresh Meat"  # $pref::Player::Name default (prefs.cs:78)


# =============================================================================
# NetInterface out-of-band packet types (first byte of non-data packets)
# =============================================================================
# These are the U8 written as the FIRST byte of every handshake/info packet.
# A packet is a "data/connection" packet iff (firstByte & 0x01) != 0; the data
# packet's first bit is writeFlag(true)=1. The handshake types below are all
# even, so their low bit is 0 and they route to the handshake dispatch.
# Source: TGE engine/sim/netInterface.h:14-34 (enum PacketTypes).
# EXE-confirmed: sendConnectChallengeRequest @ VA 0x54B6E4 writes byte 0x1A (26).
MASTER_SERVER_GAME_TYPES_REQUEST = 2    # TGE netInterface.h:16
MASTER_SERVER_GAME_TYPES_RESPONSE = 4   # TGE netInterface.h:17
MASTER_SERVER_LIST_REQUEST = 6          # TGE netInterface.h:18
MASTER_SERVER_LIST_RESPONSE = 8         # TGE netInterface.h:19
GAME_HEARTBEAT = 22                     # TGE netInterface.h:26 (<=this => info packet)
CONNECT_CHALLENGE_REQUEST = 26          # EXE 0x1A @ VA 0x54B6E4 ; TGE :28
CONNECT_CHALLENGE_RESPONSE = 30         # TGE netInterface.h:30
CONNECT_REQUEST = 32                    # TGE netInterface.h:31
CONNECT_REJECT = 34                     # TGE netInterface.h:32
CONNECT_ACCEPT = 36                     # TGE netInterface.h:33
DISCONNECT = 38                         # TGE netInterface.h:34

# Handshake packet payloads (all multi-byte fields are little-endian, written
# whole-byte via Stream::write, NOT bit-packed -- these are raw out-of-band
# packets, distinct from the bit-packed connection data packets).
# Source: TGE engine/sim/netInterface.cc:152-237. EXE structure confirmed at
# sendConnectChallengeRequest 0x54B6C0 and handleConnectChallengeResponse
# 0x54BC20 (reads connectSeq U32 then 4x U32 digest, then sends ConnectRequest).
#
#   ConnectChallengeRequest  : U8 type(26) | U32 connectSequence
#   ConnectChallengeResponse : U8 type(30) | U32 connectSequence | U32[4] addressDigest
#   ConnectRequest           : U8 type(32) | U32 connectSequence | U32[4] addressDigest
#                              | writeString(className) | <GameConnection::writeConnectRequest>
#   ConnectAccept            : U8 type(36) | U32 connectSequence | <writeConnectAccept: U32 protocolVersion>
#   ConnectReject            : U8 type(34) | U32 connectSequence | writeString(reason)
#   Disconnect               : U8 type(38) | U32 connectSequence | writeString(reason)

# The NetConnection subclass class-name string written in the ConnectRequest
# (out->writeString(conn->getClassName())). For the game client this is the
# GameConnection class. EXE: "GameConnection" literal @ VA 0x5F6EC8.
# Source: TGE netInterface.cc:232.
CONNECTION_CLASS_NAME = "GameConnection"  # EXE string present; usage path matches stock


# =============================================================================
# NetConnection::writeConnectRequest base payload (netClassGroup + classCRC)
# =============================================================================
# Before GameConnection appends its game-string/version block, the base
# NetConnection::writeConnectRequest (EXE @ VA 0x547170, called from the top of
# GameConnection::writeConnectRequest @ 0x457AAA) writes TWO U32s:
#     write(U32 mNetClassGroup)                 -> [this+0xf4]
#     write(U32 AbstractClassRep::classCRC[grp])-> table @ VA 0x638D4C, indexed [grp]
#
# So the FULL connect-request body order is:
#     write(U32 netClassGroup=0)
#     write(U32 classCRC=0xFFFFFFFF)
#     writeString("Age Of Time Demo")
#     write(U32 currentProtocol=11)
#     write(U32 minProtocol=11)
#     writeString(joinPassword)
#     write(U32 connectArgc)
#     connectArgc * writeString(argv[i])
#
# classCRC: EXE-CONFIRMED that AoT does NOT recompute the class manifest CRC.
# The static table AbstractClassRep::classCRC[4] @ VA 0x638D4C is initialised to
# {0xFFFFFFFF, 0, 0, 0} in the file image, and the ONLY two .text references to
# it (VA 0x54719A in writeConnectRequest, VA 0x5471F9 in readConnectRequest) are
# both READS -- nothing ever writes the table. So the value the AoT client sends
# is the constant INITIAL_CRC_VALUE = 0xFFFFFFFF, exactly like stock TGE 1.4
# (where classCRC stays at its initializer and the server-side check is a no-op
# unless the group differs). readConnectRequest mismatch -> "CHR_INVALID"
# (string @ VA 0x612E18).
NET_CLASS_GROUP = 0                 # EXE: mNetClassGroup [this+0xf4]; NetClassGroupGame
CONNECT_CLASS_CRC = 0xFFFFFFFF      # EXE-confirmed constant @ classCRC[0] VA 0x638D4C


# =============================================================================
# Networkable class manifest: classId bit widths + specific event classIds
# =============================================================================
# Recovered by enumerating every ConcreteClassRep<T> registration in the exe.
# Method (see docs/re-deep-findings.md): registerClassRep is @ VA 0x4179E0 (it
# prepends to the classLinkList head @ VA 0x65A040). Each registration thunk does
#     push <netEventDir>; push <classType>; push <groupMaskBIT>; push <name*>;
#     mov ecx, <ClassRep .data obj>; call <ConcreteClassRep ctor>
# (e.g. RemoteCommandEvent @ VA 0x5E8160 -> obj 0x670C40). ConcreteClassRep ctor
# layout was verified @ VA 0x4C2B90 (ret 0x10; mClassId[4] field => 4 net groups).
#
# AbstractClassRep::initialize() (the engine's stock algorithm) groups classes by
# (NetClassGroup, NetClassType), SORTS them by dStrcmp(name) (ASCII byte order),
# numbers them 0..count-1 -> classId, and sets the per-(group,type) wire width to
#     NetClassBitSize = getBinLog2(getNextPow2(count + 1))   == ceil(log2(count+1))
# (bitStream.writeClassId/readClassId use this width). All AoT networkable classes
# live in NetClassGroupGame (mask BIT(0)=1); no class uses the Community group.
#
# Per-type counts found in the exe (group Game):
#   NetClassTypeObject    (0): 50 classes  -> 6-bit classId  (getNextPow2(51)=64)
#   NetClassTypeDataBlock (1): 34 classes  -> 6-bit classId  (getNextPow2(35)=64)
#   NetClassTypeEvent     (2): 14 classes  -> 4-bit classId  (getNextPow2(15)=16)
NET_CLASS_GROUP_GAME = 0

NET_CLASS_BITS_OBJECT = 6      # EXE: 50 game NetClassTypeObject classes
NET_CLASS_BITS_DATABLOCK = 6   # EXE: 34 game NetClassTypeDataBlock classes
NET_CLASS_BITS_EVENT = 4       # EXE: 14 game NetClassTypeEvent classes

NET_CLASS_COUNT_OBJECT = 50    # EXE-confirmed
NET_CLASS_COUNT_DATABLOCK = 34 # EXE-confirmed
NET_CLASS_COUNT_EVENT = 14     # EXE-confirmed

# The 14 NetClassTypeEvent classes in NetClassGroupGame, sorted by ASCII name as
# AbstractClassRep::initialize does; the index IS the on-wire classId (4 bits).
# All EXE-confirmed registrations. The two that matter for chat/login are marked.
EVENT_CLASS_IDS = {
    "ConnectionMessageEvent": 0,
    "FileChunkEvent": 1,
    "FileDownloadRequestEvent": 2,
    "GhostAlwaysObjectEvent": 3,
    "LightningStrikeEvent": 4,
    "NetStringEvent": 5,          # <- tagged-string-table teach event
    "PathManagerEvent": 6,
    "RemoteCommandEvent": 7,      # <- commandToServer / clientCmd* (chat & login)
    "SetMissionCRCEvent": 8,
    "Sim2DAudioEvent": 9,
    "Sim3DAudioEvent": 10,
    "SimDataBlockEvent": 11,
    "SimpleMessageEvent": 12,
    "StaticBrickDataEvent": 13,
}

# The two event ids the bot actually needs to encode/decode (4-bit classId):
REMOTE_COMMAND_EVENT_CLASS_ID = 7  # EXE-confirmed (sorted-name index of RemoteCommandEvent)
NET_STRING_EVENT_CLASS_ID = 5      # EXE-confirmed (sorted-name index of NetStringEvent)


# =============================================================================
# ConnectionProtocol data-packet header (bit-packed, little-endian bit order)
# =============================================================================
# Built by ConnectionProtocol::buildSendPacketHeader and parsed by
# processRawPacket. Source: TGE engine/core/dnet.cc (buildSendPacketHeader and
# processRawPacket). Field WIDTHS (in bits), in write/read order:
#
#   writeFlag(true)                 -> 1 bit  (set => this IS a data/conn packet,
#                                              i.e. firstByte low bit = 1)
#   writeInt(mConnectSequence & 1, 1) -> 1 bit  connect-sequence parity
#   writeInt(mLastSendSeq, 9)         -> 9 bits packet sequence number
#   writeInt(mLastSeqRecvd, 9)        -> 9 bits highest received seq (ack start)
#   writeInt(packetType, 2)           -> 2 bits 0=Data 1=Ping 2=Ack
#   writeInt(ackByteCount, 3)         -> 3 bits number of ack-mask bytes (0..4)
#   writeInt(mAckMask, ackByteCount*8)-> ackByteCount*8 bits ack bitmask
#
# Header size is 3 + ackByteCount bytes (so 3..7 bytes; the source COMMENT in
# dnet.cc says "4-9" / "2 bits ack byte count" but the actual code writes the
# count in 3 bits and the fixed part is 3 bytes -- trust the code, not comment).
#
# EXE-CONFIRMED (deep-RE pass). ConnectionProtocol::buildSendPacketHeader was
# disassembled @ VA 0x422920 (found via the "build hdr %d %d" debug string @
# VA 0x5F1E00). The exact emitted sequence, where 0x420E20 == writeFlag and
# 0x420FA0 == writeInt(value, numBits):
#     writeFlag(true)                                  -> 1 bit
#     writeInt(mConnectSequence & 1, 1)                -> 1 bit  ([this+0x94]&1)
#     writeInt(mLastSendSeq, 9)                        -> 9 bits ([this+0x8c])
#     writeInt(mLastSeqRecvd, 9)                       -> 9 bits ([this+0x84])
#     writeInt(packetType, 2)                          -> 2 bits
#     writeInt(ackByteCount, 3)                        -> 3 bits
#     writeInt(mAckMask, ackByteCount*8)               -> count*8 bits ([this+0x90])
# ackByteCount = ((mLastSeqRecvd - mLastRecvAckAck + 7) >> 3)  (mLastRecvAckAck
# @ [this+0x98]). This is byte-identical to stock TGE 1.4 dnet.cc -- AoT's older
# fork did NOT change the data-packet header widths or field order.
PACKET_HEADER_GAME_FLAG_BITS = 1       # EXE 0x422946 writeFlag(true)
PACKET_HEADER_CONNECT_SEQ_BITS = 1     # EXE 0x42295b writeInt(seq&1, 1)
PACKET_HEADER_SEQ_BITS = 9             # EXE 0x42296c writeInt(mLastSendSeq, 9)
PACKET_HEADER_ACK_START_BITS = 9       # EXE 0x42297c writeInt(mLastSeqRecvd, 9)
PACKET_HEADER_TYPE_BITS = 2            # EXE 0x422986 writeInt(packetType, 2)
PACKET_HEADER_ACK_BYTE_COUNT_BITS = 3  # EXE 0x422990 writeInt(ackByteCount, 3)

# Data-packet sub-types (the 2-bit packetType field above).
# Source: TGE engine/core/dnet.cc:15-20 (enum NetPacketType). The read path
# rejects packetType >= 3 (InvalidPacketType). Order matches stock TGE; the 2-bit
# field width is EXE-confirmed (above), so only 0/1/2 are valid.
PACKET_TYPE_DATA = 0   # TGE dnet.cc:17 (2-bit field EXE-confirmed)
PACKET_TYPE_PING = 1   # TGE dnet.cc:18
PACKET_TYPE_ACK = 2    # TGE dnet.cc:19

# Sliding-window / ack parameters. Source: TGE dnet.h:30-37 + dnet.cc logic.
PACKET_WINDOW_SIZE = 32      # UNCONFIRMED (mLastSeqRecvdAtSend[32]; window of 32)
MAX_ACK_BYTE_COUNT = 4       # UNCONFIRMED (AssertFatal ackByteCount <= 4)
SEQ_NUMBER_WRAP = 0x200      # UNCONFIRMED (9-bit seq wraps at 512; dnet.cc)
SEQ_WINDOW_SLACK = 31        # UNCONFIRMED (reject if seq > lastRecvd+31)


# =============================================================================
# Networked string table (tagged-string negotiation)
# =============================================================================
# Source: TGE engine/sim/connectionStringTable.h:15-17 (enum Constants).
# EXE-confirmed: NetStringEvent::unpack @ VA 0x544306 reads the slot with
# `push 5; call readInt` (=readInt(5)); validated live -- the server teaches us
# tag slots 0,1,2,... and we decode them with this 5-bit width into coherent
# verbs (disableCompass, ServerMessage, MsgConnectionError, ...).
STRING_TABLE_ENTRY_BIT_SIZE = 5  # EXE-confirmed (NetStringEvent unpack @ 0x544306)

# In-packet "compressed string" type tags (NetConnection::packString,
# netConnection.cc:869-875 enum NetStringConstants). 2-bit selector then payload.
# CORRECTED: the previous values here (Integer=2, CString=3) were a transcription
# error. The stock TGE enum order -- and what both docs/handshake.md and
# docs/event-system.md document -- is:
#     NullString = 0, CString = 1, TagString = 2, Integer = 3
# Payloads: NullString -> nothing; CString -> writeString(Huffman); TagString ->
# writeInt(connStringId, EntryBitSize=5); Integer -> writeFlag(neg) then
# writeFlag-gated 7/15/31-bit magnitude. (Not re-verified in the AoT exe -- the
# packString writeInt(...,2) calls did not surface in a width-signature scan;
# but this is plain engine code and matches the TGE source + the wire docs. If
# event payloads desync at the first RemoteCommandEvent arg, re-check these two.)
STRING_TAG_NULL = 0      # NullString -> empty (TGE netConnection.cc:871)
STRING_TAG_CSTRING = 1   # CString    -> writeString(str) (TGE :872)
STRING_TAG_TAGSTRING = 2 # TagString  -> writeInt(idx, EntryBitSize=5) (TGE :873)
STRING_TAG_INTEGER = 3   # Integer    -> sign flag + 7/15/31-bit int (TGE :874)

# Back-compat aliases (older code referenced STRING_TAG_STRING for the tag case).
# Point them at the CORRECT numeric values now.
STRING_TAG_STRING = STRING_TAG_CSTRING  # =1; was wrongly 1 meaning "TagString"


# =============================================================================
# Ghost / object-replication constants
# =============================================================================
# Source: TGE engine/sim/netConnection.h:771-778 (enum GhostConstants).
# EXE-CONFIRMED: AoT's GhostIdBitSize is **14** (not stock 12). Read directly from
# GameConnection::readPacket @ VA 0x4593c0: the control-object and camera-object
# ghost ids are read with `readInt(0xe=14)` (control @ 0x45956d, camera @ 0x45969e),
# and ConnectionMessageEvent::unpack reads ghostCount as `readInt(0xf=15)` =
# GhostIdBitSize+1 (ConnMsg unpack @ VA 0x5464a0, `push 0xf`). So GHOST_ID_BIT_SIZE=14.
GHOST_ID_BIT_SIZE = 14          # EXE-confirmed (AoT fork; stock TGE = 12)
MAX_GHOST_COUNT = 1 << 14       # 16384 (GhostIdBitSize=14)
GHOST_LOOKUP_TABLE_SIZE = 1 << 14
# EXE-confirmed: ghostReadPacket @ VA 0x5498e0 reads the per-packet index size as
# `push 4; call readInt` then `add eax,3` => idSize = readInt(4)+3.
GHOST_INDEX_BIT_SIZE = 4        # EXE-confirmed (ghostReadPacket @ 0x5498e0)
# Connection control messages multiplexed in the ghost stream (3-bit "message"):
# Source: TGE netConnection.cc:48-55 + netConnection.h:722-731 (GhostStates).
# NB: AoT added a member mGhostsActive to NetConnection (divergence from stock
# 1.4 layout) -- it does NOT change the wire enum but proves the fork differs.

# =============================================================================
# Connection timing
# =============================================================================
# Source: TGE engine/sim/netConnection.cc:23-26 (enum NetConnectionConstants).
PING_TIMEOUT_MS = 4500            # UNCONFIRMED (TGE netConnection.cc:24)
DEFAULT_PING_RETRY_COUNT = 15     # UNCONFIRMED (TGE netConnection.cc:25)
