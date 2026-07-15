"""Per-class ``SimDataBlock::unpackData`` decoders for the AoT datablock stream.

The server streams the mission's datablocks as ``SimDataBlockEvent`` (event
classId 11). After the SimDataBlockEvent envelope (decoded in
``events._read_sim_datablock_event``) the body is the datablock object's
``unpackData(bstream)`` -- a per-class, length-less, bit-packed payload. To stay
bit-aligned through the datablock phase we must reproduce each class's
``unpackData`` exactly.

Each decoder here was reverse-engineered from ``AgeOfTime.exe`` (image base
0x400000) by reading the class's ``unpackData`` (vtable slot 0x48) and validated
bit-exactly by replaying ``tools/captures/real_login.jsonl`` with zero desync.

Bitstream primitive VAs (confirmed in the exe):
  0x420e20 readFlag           0x420f60 readInt(n)
  0x421510 readClassId        0x421a70 readCompressedPoint
  bitstream vtable slot 0x1c = readString (Huffman, uses the per-packet
  stringBuffer dedup path installed by GameConnection::readPacket).

A datablock id reference on the wire is a flag-gated ``readInt(10)+3`` (==
``DataBlockObjectIdFirst`` 3, width ``getBinLog2(0x400)=10``): a present flag,
then (if present) the 10-bit id. This appears throughout (datablock cross-refs).

The base ``SimDataBlock::unpackData`` (@ VA 0x4c99b0) is a bare ``ret`` -- it
reads ZERO bits -- so derived classes whose first call is to it start reading
their own fields immediately.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict

from .bitstream import BitStream
from . import telemetry

logger = logging.getLogger("aotbot.datablocks")

# DataBlock NetClassType class list, sorted by ASCII name == on-wire classId
# (re-deep-findings.md Target 1). Index is the 6-bit datablock classId.
DATABLOCK_CLASS_NAMES = [
    "AudioDescription",        # 0
    "AudioEnvironment",        # 1
    "AudioProfile",            # 2
    "AudioSampleEnvironment",  # 3
    "CameraData",              # 4
    "DebrisData",              # 5
    "DecalData",               # 6
    "ExplosionData",           # 7
    "FlyingVehicleData",       # 8
    "GameBaseData",            # 9
    "HoverVehicleData",        # 10
    "ItemData",                # 11
    "LightningData",           # 12
    "MissionMarkerData",       # 13
    "ParticleData",            # 14
    "ParticleEmitterData",     # 15
    "ParticleEmitterNodeData", # 16
    "PathCameraData",          # 17
    "PathedInteriorData",      # 18
    "PlayerData",              # 19
    "PrecipitationData",       # 20
    "ProjectileData",          # 21
    "ShapeBaseData",           # 22
    "ShapeBaseImageData",      # 23
    "SimDataBlock",            # 24
    "SplashData",              # 25
    "StaticShapeData",         # 26
    "TSShapeConstructor",      # 27
    "TriggerData",             # 28
    "WheeledVehicleData",      # 29
    "WheeledVehicleSpring",    # 30
    "WheeledVehicleTire",      # 31
    "fxDTSBrickData",          # 32
    "fxLightData",             # 33
]

DATABLOCK_OBJECT_ID_FIRST = 3
DATABLOCK_ID_BITS = 10  # getBinLog2(0x400)


class DataBlockDecodeError(Exception):
    """A datablock class whose ``unpackData`` is not implemented appeared.

    Carries the class name/id so the caller can log exactly which class blocks.
    """

    def __init__(self, class_id: int, name: str) -> None:
        super().__init__(f"no unpackData decoder for datablock class {name} (id {class_id})")
        self.class_id = class_id
        self.name = name


def _read_f32(bs: BitStream) -> None:
    """``Stream::read(4, &f)`` -- bitstream vtable slot 0x04, a raw 4-byte read
    (a full-precision F32 or U32). Advances 32 bits.
    """
    bs.read_bytes(4)


def _read_normal_vector(bs: BitStream, bits: int) -> None:
    """``BitStream::readNormalVector(bits)`` (@ VA 0x4216f0).

    Reads TWO quantised angles: the first with ``bits+1`` bits, the second with
    ``bits`` bits (each via the readFloat helper @ 0x4210b0), and reconstructs a
    unit Point3F. Net bits consumed = ``2*bits + 1``.
    """
    bs.read_int(bits + 1)
    bs.read_int(bits)


def _read_signed_float(bs: BitStream, bits: int) -> None:
    """``BitStream::readSignedFloat(bits)`` -- a quantised -1..1 float. On the
    wire it is just ``readInt(bits)`` (the same bit count as readFloat); the
    sign/scale is applied numerically. Advances ``bits`` bits.
    """
    bs.read_int(bits)


def _read_db_ref(bs: BitStream) -> int:
    """A flag-gated datablock id reference: readFlag, then readInt(10)+3 if set.

    Returns the resolved id (or 0 if the present flag was clear). Mirrors the
    ``readFlag`` + ``readInt(getBinLog2(0x400)=10)`` + ``add 3`` sequence the exe
    emits for every datablock cross-reference (e.g. AudioProfile's
    AudioDescription ref @ VA 0x58e3dd..0x58e45d).
    """
    if bs.read_flag():
        return bs.read_int(DATABLOCK_ID_BITS) + DATABLOCK_OBJECT_ID_FIRST
    return 0


# --------------------------------------------------------------------------- #
# Per-class decoders
# --------------------------------------------------------------------------- #


def _unpack_sim_data_block(bs: BitStream) -> None:
    """SimDataBlock::unpackData (@ VA 0x4c99b0): bare ``ret`` -- reads 0 bits."""
    return


def _unpack_game_base_data(bs: BitStream) -> None:
    """GameBaseData::unpackData (@ VA 0x456510): calls SimDataBlock (0 bits) and
    reads NOTHING itself. Present for the inheritance chain."""
    return


def _unpack_shape_base_data(bs: BitStream) -> None:
    """ShapeBaseData::unpackData (@ VA 0x47cad0).

    Parent GameBaseData reads 0 bits. Layout (exe-confirmed, matches stock TGE
    ShapeBaseData::unpackData order; every read cross-checked in the disasm):
      * readFlag computeCRC; if set read(4) mCRC
      * readString shapeName, readString cloakTexName
      * 9 x (readFlag; if set read(4)): mass, drag, density, maxEnergy,
        cameraMaxDist, cameraMinDist, cameraDefaultFov, cameraMinFov, cameraMaxFov
      * readString debrisShapeName
      * readFlag observeThroughObject
      * db-ref debrisID (flag + readInt(10)+3)
      * readFlag emap, isInvincible, renderWhenDestroyed
      * db-ref explosionID
      * db-ref underwaterExplosionID
      * readFlag inheritEnergyFromMount, firstPersonOnly, useEyePoint
    """
    if bs.read_flag():        # computeCRC
        _read_f32(bs)         # mCRC
    shape = bs.read_string()  # shapeName == the .dts shapeFile (the SHAPE NAME)
    if shape:
        telemetry.emit("shape_file", shape)
    bs.read_string()          # cloakTexName
    for _ in range(9):        # mass..cameraMaxFov
        if bs.read_flag():
            _read_f32(bs)
    bs.read_string()          # debrisShapeName
    bs.read_flag()            # observeThroughObject
    _read_db_ref(bs)          # debrisID
    bs.read_flag()            # emap
    bs.read_flag()            # isInvincible
    bs.read_flag()            # renderWhenDestroyed
    _read_db_ref(bs)          # explosionID
    _read_db_ref(bs)          # underwaterExplosionID
    bs.read_flag()            # inheritEnergyFromMount
    bs.read_flag()            # firstPersonOnly
    bs.read_flag()            # useEyePoint


def _unpack_camera_data(bs: BitStream) -> None:
    """CameraData::unpackData (@ VA 0x464210): tail-jumps straight to
    ShapeBaseData::unpackData (no CameraData-specific net fields)."""
    _unpack_shape_base_data(bs)


def _unpack_particle_data(bs: BitStream) -> None:
    """ParticleData::unpackData (@ VA 0x4b76d0).

    Parent SimDataBlock reads 0 bits. Field order matches stock TGE
    ParticleData::unpackData; bit WIDTHS taken from the AoT disassembly (AoT
    widened several from stock):
      * readFloat(12)                      dragCoefficient
      * readFlag; if set read(4)           windCoefficient
      * readSignedFloat(12)                gravityCoefficient
      * readFloat(9)                       inheritedVelFactor
      * readFlag; if set read(4)           constantAcceleration
      * readInt(10)                        lifetimeMS (<<5)
      * readInt(10)                        lifetimeVarianceMS (<<5)
      * readFlag; if set read(4)           spinSpeed
      * readFlag; if set readInt(11),readInt(11)   spinRandomMin/Max (-1000)
      * readFlag                           useInvAlpha
      * count = readInt(2)+1; count x (4 x readFloat(7), readFloat(14),
        readFloat(8))                      colors/sizes/times
      * count = readInt(6); count x readString   textureNameList
    """
    bs.read_float(12)                  # dragCoefficient
    if bs.read_flag():
        _read_f32(bs)                  # windCoefficient
    _read_signed_float(bs, 12)         # gravityCoefficient
    bs.read_float(9)                   # inheritedVelFactor
    if bs.read_flag():
        _read_f32(bs)                  # constantAcceleration
    bs.read_int(10)                    # lifetimeMS
    bs.read_int(10)                    # lifetimeVarianceMS
    if bs.read_flag():
        _read_f32(bs)                  # spinSpeed
    if bs.read_flag():
        bs.read_int(11)                # spinRandomMin
        bs.read_int(11)                # spinRandomMax
    bs.read_flag()                     # useInvAlpha
    count = bs.read_int(2) + 1
    for _ in range(count):
        bs.read_float(7)               # red
        bs.read_float(7)               # green
        bs.read_float(7)               # blue
        bs.read_float(7)               # alpha
        bs.read_float(14)              # size
        bs.read_float(8)               # time
    count = bs.read_int(6)
    for _ in range(count):
        bs.read_string()               # textureNameList[i]


def _unpack_particle_emitter_data(bs: BitStream) -> None:
    """ParticleEmitterData::unpackData (@ VA 0x4b85e0).

    Parent GameBaseData reads 0 bits. CFG-traced from the exe (the compiler
    reorders the flag-default blocks out-of-line; every gated value's
    ``je``-consumer was verified). Field order/widths:
      * readInt(10)                          ejectionPeriodMS              [+0x40]
      * readInt(10)                          periodVarianceMS              [+0x44]
      * readInt(16)                          ejectionVelocity (<<scale)    [+0x48]
      * readInt(14)                          velocityVariance              [+0x4c]
      * flag; if set readInt(16)             ejectionOffset                [+0x50]
      * readRangedU32(0,181) == readInt(8)   thetaMin                      [+0x54]
      * readRangedU32(0,181) == readInt(8)   thetaMax                      [+0x58]
      * flag; if set readRangedU32(0,361)==readInt(9)  phiReferenceVel     [+0x5c]
      * flag; if set readRangedU32(0,361)==readInt(9)  phiVariance         [+0x60]
      * flag (no value)                      overrideAdvance / bool        [+0x6c]
      * flag (no value)                      orientParticles               [+0x6d]
      * flag (no value)                      orientOnVelocity              [+0x6e]
      * readInt(15)                          particleCnt-ish / lifetimeMS  [+0x64]
      * readInt(10) (<<5)                     lifetimeVarianceMS           [+0x68]
      * flag (no value)                      useEmitterSizes               [+0x6f]
      * flag (no value)                      useEmitterColors              [+0x70]
      * flag (no value)                      bool                          [+0x71]
      * read(4) count (U32); count x read(4) (F32)    times[] vector       [+0x84]
    """
    bs.read_int(10)                    # ejectionPeriodMS
    bs.read_int(10)                    # periodVarianceMS
    bs.read_int(16)                    # ejectionVelocity
    bs.read_int(14)                    # velocityVariance
    if bs.read_flag():
        bs.read_int(16)                # ejectionOffset
    bs.read_ranged_u32(0, 181)         # thetaMin  (8 bits)
    bs.read_ranged_u32(0, 181)         # thetaMax  (8 bits)
    if bs.read_flag():
        bs.read_ranged_u32(0, 361)     # phiReferenceVel (9 bits)
    if bs.read_flag():
        bs.read_ranged_u32(0, 361)     # phiVariance (9 bits)
    bs.read_flag()                     # bool [+0x6c]
    bs.read_flag()                     # bool [+0x6d]
    bs.read_flag()                     # bool [+0x6e]
    bs.read_int(15)                    # [+0x64]
    bs.read_int(10)                    # [+0x68] (<<5)
    bs.read_flag()                     # bool [+0x6f]
    bs.read_flag()                     # bool [+0x70]
    bs.read_flag()                     # bool [+0x71]
    count = int.from_bytes(bs.read_bytes(4), "little")  # times[] count
    for _ in range(count):
        bs.read_bytes(4)               # F32 per element


def _unpack_explosion_data(bs: BitStream) -> None:
    """ExplosionData::unpackData (@ VA 0x4959a0).

    Parent GameBaseData reads 0 bits. CFG-traced from the exe (out-of-line flag
    default blocks; ranged reads via getBinLog2(getNextPow2(N)) decoded to fixed
    widths). Field order/widths:
      * readString                              explosionShape           [+0x40]
      * db-ref (flag + readInt(10)+3)           particleEmitter id       [+0x58]
      * db-ref                                  particleEmitter2 id      [+0x5c]
      * readInt(14)                             [+0x48]
      * read(4)                                 [+0x4c]
      * flag (no value)                         [+0x44]
      * flag; if set 3 x readInt(16)            sizes [+0x60,+0x64,+0x68]
      * readInt(14)                             [+0x6c]
      * readRangedU32(0,181)=8                  [+0xac]
      * readRangedU32(0,181)=8                  [+0xb0]
      * readRangedU32(0,361)=9                  [+0xb4]
      * readRangedU32(0,361)=9                  [+0xb8]
      * readRangedU32(0,1001)=10               [+0xbc]
      * readRangedU32(0,1001)=10               [+0xc0]
      * readInt(14)                             [+0xc4]
      * readRangedU32(0,10001)=14              [+0xc8]
      * 4 x readInt(16)                         [+0xf4,+0xf8,+0xfc,+0x100]
      * read(4)                                 [+0x104]
      * flag (no value)                         [+0x148]
      * 9 x read(4)                             [+0x14c .. +0x16c]
      * db-ref                                  emitter id               [+0xa8]
      * 4 x db-ref                              emitter array            [+0x88..]
      * 5 x db-ref                              emitter array            [+0xe0..]
      * count = readRangedU32(0,5)=3
      * count x readFloat(8)                    times[]                  [+0x138]
      * count x (3 x readRangedU32(0,16001)=14) [+0x10c]
      * 2 x readFloat(8)                        [+0x170,+0x174]
      * 6 x readFloat(7)                        [+0x178..+0x190]
    """
    bs.read_string()                   # explosionShape
    _read_db_ref(bs)                   # particleEmitter id  [+0x58]
    _read_db_ref(bs)                   # particleEmitter2 id [+0x5c]
    bs.read_int(14)                    # [+0x48]
    bs.read_bytes(4)                   # [+0x4c]
    bs.read_flag()                     # bool [+0x44]
    if bs.read_flag():                 # sizes present
        bs.read_int(16)                # [+0x60]
        bs.read_int(16)                # [+0x64]
        bs.read_int(16)                # [+0x68]
    bs.read_int(14)                    # [+0x6c]
    bs.read_ranged_u32(0, 181)         # [+0xac]
    bs.read_ranged_u32(0, 181)         # [+0xb0]
    bs.read_ranged_u32(0, 361)         # [+0xb4]
    bs.read_ranged_u32(0, 361)         # [+0xb8]
    bs.read_ranged_u32(0, 1001)        # [+0xbc]
    bs.read_ranged_u32(0, 1001)        # [+0xc0]
    bs.read_int(14)                    # [+0xc4]
    bs.read_ranged_u32(0, 10001)       # [+0xc8]
    bs.read_int(16)                    # [+0xf4]
    bs.read_int(16)                    # [+0xf8]
    bs.read_int(16)                    # [+0xfc]
    bs.read_int(16)                    # [+0x100]
    bs.read_bytes(4)                   # [+0x104]
    bs.read_flag()                     # bool [+0x148]
    for _ in range(9):                 # [+0x14c .. +0x16c]
        bs.read_bytes(4)
    _read_db_ref(bs)                   # emitter id [+0xa8]
    for _ in range(4):                 # emitter db-ref array [+0x88..]
        _read_db_ref(bs)
    for _ in range(5):                 # emitter db-ref array [+0xe0..]
        _read_db_ref(bs)
    count = bs.read_ranged_u32(0, 5)   # [+0x130]
    for _ in range(count):             # times[]
        bs.read_float(8)
    for _ in range(count):             # 3 x readRangedU32(0,16001) each
        bs.read_ranged_u32(0, 16001)
        bs.read_ranged_u32(0, 16001)
        bs.read_ranged_u32(0, 16001)
    bs.read_float(8)                   # [+0x170]
    bs.read_float(8)                   # [+0x174]
    bs.read_float(7)                   # [+0x178]
    bs.read_float(7)                   # [+0x17c]
    bs.read_float(7)                   # [+0x180]
    bs.read_float(7)                   # [+0x188]
    bs.read_float(7)                   # [+0x18c]
    bs.read_float(7)                   # [+0x190]


def _unpack_splash_data(bs: BitStream) -> None:
    """SplashData::unpackData (@ VA 0x4bd990).

    Parent GameBaseData reads 0 bits. CFG-traced from the exe. Helpers:
    ``0x421240`` reads a Point3F (3 x read(4)); ``0x4243f0`` reads a ColorF
    (4 raw bytes). Field order:
      * mathRead Point3F (3 x read(4))          [+0x70]
      * 15 x read(4)                            [+0x60 .. +0xa4]
      * db-ref (flag + readInt(10)+3)           [+0x10c]
      * 3 x db-ref                              [+0x54 array]
      * 4 x ColorF (4 raw bytes each)           [+0xb8 array]
      * 4 x read(4)                             [+0xa8 array]
      * 2 x readString                          [+0xf8,+0xfc]
    """
    bs.read_bytes(12)                  # Point3F [+0x70] (3 x F32)
    for _ in range(15):                # [+0x60 .. +0xa4]
        bs.read_bytes(4)
    _read_db_ref(bs)                   # [+0x10c]
    for _ in range(3):                 # [+0x54 array]
        _read_db_ref(bs)
    for _ in range(4):                 # ColorF array [+0xb8]
        bs.read_bytes(4)
    for _ in range(4):                 # [+0xa8 array]
        bs.read_bytes(4)
    bs.read_string()                   # [+0xf8]
    bs.read_string()                   # [+0xfc]


def _unpack_debris_data(bs: BitStream) -> None:
    """DebrisData::unpackData (@ VA 0x451b70).

    Parent GameBaseData reads 0 bits. CFG-traced from the exe. Note the bool
    fields use ``Stream::read(1, &bool)`` -- a whole BYTE (8 bits), NOT a 1-bit
    readFlag (the ``test al,al; je`` after each is the read-success check, not a
    flag-gate). Field order:
      * 6 x read(4)                             [+0x4c,+0x48,+0x58,+0x5c,+0x60,+0x64]
      * 4 x read(1)  (bool bytes)               [+0x68 .. +0x6b]
      * 6 x read(4)                             [+0x50,+0x54,+0x60,+0x64,+0x40,+0x44]
      * 2 x read(1)                             [+0x6c,+0x6d]
      * 3 x read(4)                             [+0x70,+0x74,+0x78]
      * 1 x read(1)                             [+0x7c]
      * 2 x readString                          [+0x88,+0x80]
      * 2 x db-ref (flag + readInt(10)+3)       [+0xa0 array]
      * 1 x db-ref                              [+0x90]
    """
    for _ in range(6):
        bs.read_bytes(4)
    for _ in range(4):
        bs.read_bytes(1)               # bool byte
    for _ in range(6):
        bs.read_bytes(4)
    for _ in range(2):
        bs.read_bytes(1)
    for _ in range(3):
        bs.read_bytes(4)
    bs.read_bytes(1)
    bs.read_string()                   # [+0x88]
    bs.read_string()                   # [+0x80]
    for _ in range(2):                 # [+0xa0 array]
        _read_db_ref(bs)
    _read_db_ref(bs)                   # [+0x90]


def _unpack_shape_base_image_data(bs: BitStream) -> None:
    """ShapeBaseImageData::unpackData (@ VA 0x4859a0).

    Bit-exact (Wave-8, CFG re-verified at 0x4859a0..0x4863a2). Requires the
    per-packet string buffer to be installed (the engine's GameConnection
    setStringBuffer), as the first field is a dedup-prefix readString.

    Parent GameBaseData reads 0 bits. CFG-traced from the exe (large, with
    out-of-line default blocks and a 31-iteration state-machine loop). NOTE the
    two "shape box" flags have INVERTED polarity -- the box is read when the flag
    bit is CLEAR (the engine reads the explicit box only when the default-flag is
    off); every gated branch's je-target was checked. Helpers: 0x421240 reads a
    Point3F (12 bytes); 0x421800 reads a Box6F (24 bytes); 0x421570 is
    readSignedInt. Field order:
      * flag; if set read(4)                     mounting [+0xd28]
      * flag (bare)                              [+0xc68]
      * readString                               [+0xc6c]
      * read(4)                                  [+0xc70]
      * flag; if CLEAR read Box6F (24 bytes)     eyeOffset [+0xc74]
      * flag; if CLEAR read Box6F (24 bytes)     [+0xcb4]
      * flag (bare)                              [+0xc69]
      * flag (bare)                              [+0xc6a]
      * read(4)                                  [+0xcf8]
      * flag (bare)                              [+0xcfc]
      * read(4)                                  [+0xd00]
      * flag (bare)                              [+0xd89]
      * db-ref                                   [+0xcf4]
      * flag; if set read(4),read(4),4xreadFloat(7)  [+0xd0c..+0xd20]
      * Point3F (12 bytes)                       [+0xd98]
      * read(4)                                  [+0xda4]
      * read(4)                                  [+0xda8]
      * db-ref                                   [+0xd94]
      * 31 x state-block (see below)
      * 4 x readString                           [+0x1ac4..+0x1ad0]

    Each of the 31 state blocks is gated by a state-present flag; if set:
      readString; 11 x readInt(5); flag?read(4); 5 x flag(bare);
      flag?read(4)+3xreadInt(3); flag?readSignedInt(16); flag?readSignedInt(16);
      flag(bare); flag(bare); flag?(db-ref + 2xread(4)); flag?db-ref.
    """
    if bs.read_flag():                 # [+0xd2c] -> [+0xd28]
        bs.read_bytes(4)
    bs.read_flag()                     # bare [+0xc68]
    bs.read_string()                   # [+0xc6c]
    bs.read_bytes(4)                   # [+0xc70]
    if not bs.read_flag():             # box read when flag CLEAR [+0xc74]
        # 0x421800 is 193 BITS (Box6F + trailing sign flag), not 24 bytes --
        # see ghosts._read_box6f (WAVE-12). Reading 192 bits here left the
        # cursor 1 bit short per box.
        bs.read_bytes(24)              # Box6F
        bs.read_flag()                 # trailing sign flag (@0x4218ad)
    if not bs.read_flag():             # [+0xcb4]
        bs.read_bytes(24)
        bs.read_flag()                 # trailing sign flag
    bs.read_flag()                     # bare [+0xc69]
    bs.read_flag()                     # bare [+0xc6a]
    bs.read_bytes(4)                   # [+0xcf8]
    bs.read_flag()                     # bare [+0xcfc]
    bs.read_bytes(4)                   # [+0xd00]
    bs.read_flag()                     # bare [+0xd89]
    _read_db_ref(bs)                   # [+0xcf4]
    # RE-DISASSEMBLED (0x485d47..0x485dfb): after the [+0xcf4] db-ref the exe
    # reads a BARE flag -> [+0xd05] (inline bit test @0x485d47) and then an
    # UNCONDITIONAL readInt(getBinLog2(getNextPow2(4))=2) -> [+0xd08]
    # (@0x485d67..0x485d87). The d0c..d20 group is gated on the 2-bit INT
    # being non-zero (``test eax,eax; je 0x485dfb`` @0x485d85), NOT on a flag.
    # The prior transcription collapsed all this into one gate flag, leaving
    # the cursor 2 bits short on EVERY ShapeBaseImageData -- the head-of-stream
    # desync that shredded the datablock load phase.
    bs.read_flag()                     # bare [+0xd05]
    if bs.read_int(2):                 # [+0xd08] (2-bit int, also the gate)
        bs.read_bytes(4)               # [+0xd20]
        bs.read_bytes(4)               # [+0xd1c]
        bs.read_float(7)               # [+0xd0c]
        bs.read_float(7)               # [+0xd10]
        bs.read_float(7)               # [+0xd14]
        bs.read_float(7)               # [+0xd18]
    bs.read_bytes(12)                  # Point3F [+0xd98]
    bs.read_bytes(4)                   # [+0xda4]
    bs.read_bytes(4)                   # [+0xda8]
    _read_db_ref(bs)                   # [+0xd94]
    for _ in range(31):                # state machine table
        if bs.read_flag():             # state present
            bs.read_string()           # [edi-8]
            for _ in range(11):
                bs.read_int(5)
            if bs.read_flag():         # [edi+0x30]
                bs.read_bytes(4)
            bs.read_flag()             # bare [edi+0x2e]
            bs.read_flag()             # bare [edi+0x29]
            bs.read_flag()             # bare [edi+0x2a]
            bs.read_flag()             # bare [edi+0x2c]
            bs.read_flag()             # bare [edi+0x2d]
            if bs.read_flag():         # [edi+0x34]
                bs.read_bytes(4)
            bs.read_int(3)             # [edi+0x38]
            bs.read_int(3)             # [edi+0x3c]
            bs.read_int(3)             # [edi+0x40]
            if bs.read_flag():         # [edi+0x48]
                bs.read_signed_int(16)
            if bs.read_flag():         # [edi+0x4c]
                bs.read_signed_int(16)
            bs.read_flag()             # bare [edi+0x44]
            bs.read_flag()             # bare [edi+0x28]
            # Per-state tail, RE-DISASSEMBLED (this reverts the WAVE-18 removal,
            # which was itself the regression). At 0x486258 the inline-readFlag
            # bounds check ``jle 0x4862df`` jumps to the OUT-OF-LINE bit test;
            # its flag-SET path (0x486307) reads readInt(getBinLog2(
            # getNextPow2(0x400))=10)+3 -> [edi+0x54] followed by TWO raw
            # read(4) -> [edi+0x5c]/[edi+0x60], then rejoins at 0x48626b. The
            # ``mov [edi+0x54],0`` @0x486264 that WAVE-18 read as "constant
            # store, no bits" is only the flag-CLEAR / past-end DEFAULT path.
            # Same shape for [edi+0x58] (bit test @0x486357, flag-SET path
            # @0x48637f reads readInt(10)+3). Dropping these two flag reads
            # under-consumed >=2 bits for every PRESENT state block, which
            # desynced the next state's readString and shredded the whole
            # datablock load stream on worlds with populated image states --
            # the silent per-packet tail losses that eventually ate
            # GhostAlwaysObjectEvents/NetStringEvents and zombied the session.
            # (Capture-validated: tools/captures/live_session_dbg.jsonl replays
            # clean with these reads restored; it over-read 61 bits at
            # datablock index 72 without them.)
            if bs.read_flag():         # [edi+0x54] present (bit test @0x4862df)
                bs.read_int(10)        # +3 (datablock-id ref @0x48631d)
                bs.read_bytes(4)       # [edi+0x5c] (@0x486333)
                bs.read_bytes(4)       # [edi+0x60] (@0x486348)
            if bs.read_flag():         # [edi+0x58] present (bit test @0x486357)
                bs.read_int(10)        # +3 (@0x486395)
    bs.read_string()                   # [+0x1ac4]
    bs.read_string()                   # [+0x1ac8]
    bs.read_string()                   # [+0x1acc]
    bs.read_string()                   # [+0x1ad0]


def _unpack_player_data(bs: BitStream) -> None:
    """PlayerData::unpackData (@ VA 0x468110).

    Parent **ShapeBaseData** (0x47cad0). CFG-traced from the exe
    (0x468110..0x468864). The AoT fork sends all scalar tunables as raw
    ``read(4)`` U32/F32 (no quantised ``readInt`` for jumpDelay as stock TGE
    did -- TRUST the exe). Layout:

      * 4 x readFlag (bool tunables; first stored [+0x300] = renderFirstPerson,
        the rest are AoT booleans, all bare)
      * 27 x read(4)                            scalar tunables [+0x30c..+0x398]
      * 18 x db-ref (flag + readInt(10)+3)      sound[MaxSounds=18] [+0x3c8..]
      * 3 x read(4)                             boxSize.{x,y,z}  [+0x410..+0x418]
      * flag (bare)                             [+0x41c]
      * flag; if set db-ref                     footPuffID       [+0xbe0]
      * 2 x read(4)                             footPuffNumParts, footPuffRadius
      * flag; if set db-ref                     decalID          [+0xbf0]
      * read(4)                                 decalOffset      [+0x39c]
      * flag; if set db-ref                     dustID           [+0xbf8]
      * flag; if set db-ref                     splashId         [+0xc00]
      * 3 x (flag; if set db-ref)               splashEmitterIDList [+0xc34..]
      * 9 x read(4)                             groundImpact*    [+0x3a0..+0x3c0]
      * flag (bare)                             [+0x3c4]
    """
    _unpack_shape_base_data(bs)
    bs.read_flag()                     # renderFirstPerson [+0x300]
    bs.read_flag()                     # bare bool
    bs.read_flag()                     # bare bool
    bs.read_flag()                     # bare bool
    for _ in range(27):                # scalar tunables [+0x30c..+0x398]
        bs.read_bytes(4)
    for _ in range(18):                # sound[MaxSounds] db-refs [+0x3c8..]
        _read_db_ref(bs)
    bs.read_bytes(4)                   # boxSize.x [+0x410]
    bs.read_bytes(4)                   # boxSize.y [+0x414]
    bs.read_bytes(4)                   # boxSize.z [+0x418]
    bs.read_flag()                     # bare [+0x41c]
    _read_db_ref(bs)                   # footPuffID [+0xbe0]
    bs.read_bytes(4)                   # footPuffNumParts [+0xbe4]
    bs.read_bytes(4)                   # footPuffRadius [+0xbe8]
    _read_db_ref(bs)                   # decalID [+0xbf0]
    bs.read_bytes(4)                   # decalOffset [+0x39c]
    _read_db_ref(bs)                   # dustID [+0xbf8]
    _read_db_ref(bs)                   # splashId [+0xc00]
    for _ in range(3):                 # splashEmitterIDList [+0xc34..]
        _read_db_ref(bs)
    for _ in range(9):                 # groundImpact* [+0x3a0..+0x3c0]
        bs.read_bytes(4)
    bs.read_flag()                     # bare [+0x3c4]


def _unpack_fx_light_data(bs: BitStream) -> None:
    """fxLightData::unpackData (@ VA 0x4aae50).

    Parent GameBaseData (0 bits). CFG-traced from the exe
    (0x4aae50..0x4ab4ef). Two distinct boolean encodings appear and MUST be kept
    distinct:
      * ``read(1, &bool)`` (``push 1; call [vtbl+4]``) = a whole BYTE (8 bits).
      * inline readFlag = 1 bit.
    ColorF (helper 0x4243f0) = 4 raw bytes. Field order:
      * read(1) bool                            mIsEnabled        [+0x44]
      * read(4)                                 [+0x48]
      * read(4)                                 [+0x4c]
      * ColorF (4 bytes)                        [+0x50]
      * readString                              [+0x40]
      * ColorF (4 bytes)                        [+0x64]
      * 3 x read(1) bool                        [+0x60,+0x61,+0x74]
      * 7 x read(4)                             [+0x78..+0x90]
      * 5 x flag (bare)                         [+0x121..+0x125]
      * 2 x flag (bare)                         [+0x94,+0x95]
      * 2 x ColorF (4 bytes each)               [+0x98,+0xa8]
      * 12 x read(4)                            [+0xb8..+0xe4]
      * flag (bare)                             [+0xe8]
      * 7 x readString                          [+0xec..+0x104]
      * 5 x read(4)                             [+0x108..+0x118]
      * 5 x flag (bare)                         [+0x11c..+0x120]
    """
    bs.read_bytes(1)                   # read(1) bool [+0x44]
    bs.read_bytes(4)                   # [+0x48]
    bs.read_bytes(4)                   # [+0x4c]
    bs.read_bytes(4)                   # ColorF [+0x50]
    bs.read_string()                   # [+0x40]
    bs.read_bytes(4)                   # ColorF [+0x64]
    bs.read_bytes(1)                   # read(1) bool [+0x60]
    bs.read_bytes(1)                   # read(1) bool [+0x61]
    bs.read_bytes(1)                   # read(1) bool [+0x74]
    for _ in range(7):                 # [+0x78..+0x90]
        bs.read_bytes(4)
    for _ in range(5):                 # bare flags [+0x121..+0x125]
        bs.read_flag()
    bs.read_flag()                     # bare [+0x94]
    bs.read_flag()                     # bare [+0x95]
    bs.read_bytes(4)                   # ColorF [+0x98]
    bs.read_bytes(4)                   # ColorF [+0xa8]
    for _ in range(12):                # [+0xb8..+0xe4]
        bs.read_bytes(4)
    bs.read_flag()                     # bare [+0xe8]
    for _ in range(7):                 # [+0xec..+0x104]
        bs.read_string()
    for _ in range(5):                 # [+0x108..+0x118]
        bs.read_bytes(4)
    for _ in range(5):                 # bare flags [+0x11c..+0x120]
        bs.read_flag()


def _unpack_lightning_data(bs: BitStream) -> None:
    """LightningData::unpackData (@ VA 0x4b3950).

    Parent GameBaseData (0 bits). Then (exe-confirmed 0x4b3950..0x4b3a78):
      * 8 x db-ref (flag + readInt(10)+3)       thunder/strike sounds [+0x84..]
      * 8 x readString                          texture names         [+0x64..]
      * 1 x db-ref                              [+0xa4]
    """
    for _ in range(8):                 # [+0x84..] db-refs
        _read_db_ref(bs)
    for _ in range(8):                 # [+0x64..] strings
        bs.read_string()
    _read_db_ref(bs)                   # [+0xa4]


def _unpack_decal_data(bs: BitStream) -> None:
    """DecalData::unpackData (@ VA 0x544a40).

    Parent SimDataBlock (0 bits). Then (exe-confirmed): read(4) [+0x34],
    read(4) [+0x38], readString [+0x3c].
    """
    bs.read_bytes(4)                   # [+0x34]
    bs.read_bytes(4)                   # [+0x38]
    bs.read_string()                   # [+0x3c]


def _unpack_ts_shape_constructor(bs: BitStream) -> None:
    """TSShapeConstructor::unpackData (@ VA 0x57fbe0).

    Parent SimDataBlock (0 bits). Then (exe-confirmed 0x57fbe0..0x57fc58):
      * readString                              baseShape           [+0x34]
      * count = readInt(7)
      * count x readString                      sequence names      [+0x38 array]
    (the loop reads exactly ``count`` strings; the trailing rep-stosd just zeroes
    the unused 0x7f-entry array tail and reads no bits).
    """
    bs.read_string()                   # baseShape [+0x34]
    count = bs.read_int(7)
    for _ in range(count):
        bs.read_string()


def _unpack_item_data(bs: BitStream) -> None:
    """ItemData::unpackData (@ VA 0x45d8a0).

    Parent **ShapeBaseData** (0x47cad0), then (CFG-traced from the exe,
    out-of-line default blocks at 0x45d9d4..0x45db43):
      * readFloat(10)                           mass-ish      [+0x300]
      * readFloat(10)                                          [+0x304]
      * flag (bare)                                            [+0x308]
      * flag; if set readFloat(10) (default 1.0)               [+0x30c]
      * flag; if set read(4) (default -1.0)                    [+0x310]
      * flag; if set:                                          [+0x320]
          readInt(2); 4 x readFloat(7) [+0x324..+0x330];
          2 x read(4) [+0x334,+0x338]; flag (bare) [+0x31c]
      * 8 x readString                          [+0x33c..+0x354, +0x3c]
    """
    _unpack_shape_base_data(bs)
    bs.read_float(10)                  # [+0x300]
    bs.read_float(10)                  # [+0x304]
    bs.read_flag()                     # bare [+0x308]
    if bs.read_flag():                 # [+0x30c]
        bs.read_float(10)
    if bs.read_flag():                 # [+0x310]
        bs.read_bytes(4)
    if bs.read_flag():                 # [+0x320]
        bs.read_int(2)                 # [+0x320]
        bs.read_float(7)               # [+0x324]
        bs.read_float(7)               # [+0x328]
        bs.read_float(7)               # [+0x32c]
        bs.read_float(7)               # [+0x330]
        bs.read_bytes(4)               # [+0x334]
        bs.read_bytes(4)               # [+0x338]
        bs.read_flag()                 # bare [+0x31c]
    for _ in range(8):                 # [+0x33c..+0x354, +0x3c]
        bs.read_string()


def _unpack_projectile_data(bs: BitStream) -> None:
    """ProjectileData::unpackData (@ VA 0x475a90).

    Parent GameBaseData reads 0 bits. CFG-traced from the exe
    (0x475a90..0x476190); the compiler emits the out-of-line default blocks for
    every gated value (``je`` to the default-set block, fall-through = read), and
    several ranged reads via ``getBinLog2(getNextPow2(N))``:
    N=0x400 -> 10-bit db-ref id, N=0x1000 -> 12-bit. Field order/widths:

      * readString                              projectileShapeName       [+0x40]
      * flag; if set 3 x read(4)                scale (default 1,1,1)     [+0x74..+0x7c]
      * db-ref (flag + readInt(10)+3)           [+0x128]
      * db-ref                                  [+0x130]
      * db-ref                                  [+0xb0]
      * db-ref                                  [+0xb8]
      * db-ref                                  [+0xc0]
      * db-ref                                  [+0xc8]
      * db-ref                                  [+0xd0]
      * flag (bare)                             [+0x89]
      * read(4)                                 [+0xa8]   (unconditional U32)
      * db-ref                                  [+0xd8]
      * db-ref                                  [+0xe0]
      * 6 x db-ref                              [+0xfc array, stride 4]
      * flag; if set readFloat(8), 3 x readFloat(7)   [+0x44 -> +0x48..+0x54]
      * flag; if set 3 x readFloat(7)           [+0x5c -> +0x60..+0x68]
      * readRangedU32(0,0xfff) == readInt(12)   [+0x98]
      * readInt(12)                             [+0x9c]
      * readInt(12)                             [+0xa0]
      * flag (bare)                             [+0xa4]
      * flag; if set 3 x read(4)                [+0x88 -> +0x94,+0x8c,+0x90]
    """
    bs.read_string()                   # projectileShapeName [+0x40]
    # RE-DISASSEMBLED: [+0x70] is a BARE bool flag (``mov [edi+0x70], al``
    # @0x475ae6, faceViewer-style), and the scale group is gated by a SECOND,
    # separate flag (inline bit test @0x475b14; clear -> defaults 1,1,1
    # @0x475af5). The old transcription fused them into one flag, leaving the
    # cursor 1 bit short on EVERY ProjectileData -- which silently shredded the
    # rest of each load-phase packet carrying one (capture-validated: the solo
    # ProjectileData packet in real_login.jsonl hand-walks to the exact next
    # event boundary with this flag restored, values 0.1/1.0/0.2 decoding
    # bit-clean).
    bs.read_flag()                     # bare bool [+0x70]
    if bs.read_flag():                 # scale present (defaults 1,1,1)
        bs.read_bytes(4)               # [+0x74]
        bs.read_bytes(4)               # [+0x78]
        bs.read_bytes(4)               # [+0x7c]
    _read_db_ref(bs)                   # [+0x128]
    _read_db_ref(bs)                   # [+0x130]
    _read_db_ref(bs)                   # [+0xb0]
    _read_db_ref(bs)                   # [+0xb8]
    _read_db_ref(bs)                   # [+0xc0]
    _read_db_ref(bs)                   # [+0xc8]
    _read_db_ref(bs)                   # [+0xd0]
    bs.read_flag()                     # bare [+0x89]
    bs.read_bytes(4)                   # [+0xa8] U32
    _read_db_ref(bs)                   # [+0xd8]
    _read_db_ref(bs)                   # [+0xe0]
    for _ in range(6):                 # [+0xfc array]
        _read_db_ref(bs)
    if bs.read_flag():                 # [+0x44]
        bs.read_float(8)               # [+0x48]
        bs.read_float(7)               # [+0x4c]
        bs.read_float(7)               # [+0x50]
        bs.read_float(7)               # [+0x54]
    if bs.read_flag():                 # [+0x5c]
        bs.read_float(7)               # [+0x60]
        bs.read_float(7)               # [+0x64]
        bs.read_float(7)               # [+0x68]
    bs.read_int(12)                    # [+0x98] readRangedU32(0,0xfff)
    bs.read_int(12)                    # [+0x9c]
    bs.read_int(12)                    # [+0xa0]
    bs.read_flag()                     # bare [+0xa4]
    if bs.read_flag():                 # [+0x88]
        bs.read_bytes(4)               # [+0x94]
        bs.read_bytes(4)               # [+0x8c]
        bs.read_bytes(4)               # [+0x90]


def _unpack_trigger_data(bs: BitStream) -> None:
    """TriggerData::unpackData (@ VA 0x4b6690).

    Parent GameBaseData (0 bits) then a single ``read(4)`` U32 [+0x40]. The
    trigger's polyhedron/transform is NOT sent over the wire (it is rebuilt
    client-side from the mission file); only this U32 syncs. Capture-validated:
    registering this advanced the datablock stream from 14 to 32 decoded with
    zero desync.

    NOTE: ParticleEmitterNodeData::unpackData (vtable 0x600f5c slot 0x48) is the
    SAME function VA 0x4b6690 (it likewise syncs only one U32; the node geometry
    is rebuilt from the mission), so it dispatches here too.
    """
    _read_f32(bs)


def _unpack_static_shape_data(bs: BitStream) -> None:
    """StaticShapeData::unpackData (@ VA 0x48dc10).

    Parent ShapeBaseData, then: readFlag [+0x300], read(4) U32 [+0x304].
    """
    _unpack_shape_base_data(bs)
    bs.read_flag()
    _read_f32(bs)


def _unpack_precipitation_data(bs: BitStream) -> None:
    """PrecipitationData::unpackData (@ VA 0x4bad60).

    Parent GameBaseData (@ 0x456510) reads 0 bits. CFG-traced from the exe
    (0x4bad60..0x4bae70) and cross-checked bit-for-bit against TGE
    ``precipitation.cc:89`` PrecipitationData::unpackData (AoT did NOT diverge):
      * db-ref (flag + readRangedU32(3, DataBlockObjectIdLast)=readInt(10)+3)
                                                soundProfileId
      * readString                              dropTexture (mDropName)
      * readString                              splashTexture (mSplashName)
      * read(4)                                 mDropSize  (F32)
      * read(4)                                 mSplashSize (F32)
      * read(4)                                 mSplashMS  (S32)
      * readFlag                                mUseTrueBillboards
    """
    _read_db_ref(bs)                   # soundProfileId
    bs.read_string()                   # dropTexture
    bs.read_string()                   # splashTexture
    bs.read_bytes(4)                   # mDropSize
    bs.read_bytes(4)                   # mSplashSize
    bs.read_bytes(4)                   # mSplashMS
    bs.read_flag()                     # mUseTrueBillboards


def _unpack_audio_environment(bs: BitStream) -> None:
    """AudioEnvironment::unpackData (@ VA 0x58db70).

    Parent SimDataBlock (0 bits). CFG-traced from the exe and cross-checked
    against TGE ``audioDataBlock.cc`` AudioEnvironment::unpackData (widths come
    from the AoT ``readRangedU32(getBinLog2(getNextPow2(range+1)))`` reads):
      * readFlag mUseRoom
      * if set: readRangedU32(0, 0x1c) (== readInt(5))  mRoom; return
      * else:
          readRangedU32(0,10000)=14b   mRoomHF
          readRangedU32(0,20000)=15b   mReflections
          readRangedU32(0,12000)=14b   mReverb
          readInt(8),(8),(8),(9),(7)   rolloff,decay,decayHF,reflDelay,reverbDelay
          readRangedU32(0,10000)=14b   mRoomVolume
          readInt(8),(9),(10),(8),(10) effVol,damping,envSize,envDiff,airAbsorb
          readInt(6)                   mFlags
    """
    if bs.read_flag():                 # mUseRoom
        bs.read_ranged_u32(0, 0x1c)    # mRoom (5 bits)
        return
    bs.read_ranged_u32(0, 10000)       # mRoomHF (14)
    bs.read_ranged_u32(0, 20000)       # mReflections (15)
    bs.read_ranged_u32(0, 12000)       # mReverb (14)
    bs.read_int(8)                     # mRoomRolloffFactor
    bs.read_int(8)                     # mDecayTime
    bs.read_int(8)                     # mDecayHFRatio
    bs.read_int(9)                     # mReflectionsDelay
    bs.read_int(7)                     # mReverbDelay
    bs.read_ranged_u32(0, 10000)       # mRoomVolume (14)
    bs.read_int(8)                     # mEffectVolume
    bs.read_int(9)                     # mDamping
    bs.read_int(10)                    # mEnvironmentSize
    bs.read_int(8)                     # mEnvironmentDiffusion
    bs.read_int(10)                    # mAirAbsorption
    bs.read_int(6)                     # mFlags


def _unpack_audio_sample_environment(bs: BitStream) -> None:
    """AudioSampleEnvironment::unpackData (@ VA 0x58de30).

    Parent SimDataBlock (0 bits). CFG-traced + cross-checked vs TGE
    ``audioDataBlock.cc`` AudioSampleEnvironment::unpackData:
      readRangedU32(0,11000)=14b  mDirect
      readRangedU32(0,10000)=14b  mDirectHF
      readRangedU32(0,11000)=14b  mRoom
      readRangedU32(0,10000)=14b  mRoomHF
      readInt(9),(8),(9),(8),(9),(9),(9)  obstruction..airAbsorption
      readRangedU32(0,10000)=14b  mOutsideVolumeHF
      readInt(3)                  mFlags
    """
    bs.read_ranged_u32(0, 11000)       # mDirect (14)
    bs.read_ranged_u32(0, 10000)       # mDirectHF (14)
    bs.read_ranged_u32(0, 11000)       # mRoom (14)
    bs.read_ranged_u32(0, 10000)       # mRoomHF (14)
    bs.read_int(9)                     # mObstruction
    bs.read_int(8)                     # mObstructionLFRatio
    bs.read_int(9)                     # mOcclusion
    bs.read_int(8)                     # mOcclusionLFRatio
    bs.read_int(9)                     # mOcclusionRoomRatio
    bs.read_int(9)                     # mRoomRolloff
    bs.read_int(9)                     # mAirAbsorption
    bs.read_ranged_u32(0, 10000)       # mOutsideVolumeHF (14)
    bs.read_int(3)                     # mFlags


def _unpack_pathed_interior_data(bs: BitStream) -> None:
    """PathedInteriorData::unpackData (@ VA 0x515030).

    CFG-traced from the exe (matches TGE ``pathedInterior.cc``): MaxSounds == 3
    db-refs FIRST, then Parent GameBaseData (@ 0x456510, 0 bits).
      * 3 x (flag + readInt(10)+3)   sound[MaxSounds]
    """
    for _ in range(3):                 # MaxSounds db-refs
        _read_db_ref(bs)


def _unpack_wheeled_vehicle_spring(bs: BitStream) -> None:
    """WheeledVehicleSpring::unpackData (@ VA 0x4d1340).

    Parent SimDataBlock (0 bits) then 4 x read(4) U32/F32 (length, force,
    damping, antiSwayForce). CFG-traced from the exe.
    """
    for _ in range(4):
        bs.read_bytes(4)


def _unpack_wheeled_vehicle_tire(bs: BitStream) -> None:
    """WheeledVehicleTire::unpackData (@ VA 0x4d1230).

    Parent SimDataBlock (0 bits) then readString (shapeFile) + 11 x read(4)
    (mass, kineticFriction, staticFriction, restitution, radius, lateral*,
    longitudinal*). CFG-traced from the exe.
    """
    bs.read_string()                   # shapeFile
    for _ in range(11):
        bs.read_bytes(4)


def _unpack_fx_dts_brick_data(bs: BitStream) -> None:
    """fxDTSBrickData::unpackData (@ VA 0x498770) -- AoT-specific (no TGE source).

    Parent GameBaseData (@ 0x456510, 0 bits). CFG-traced from the exe
    (0x498770..0x498874); the trailing float math operates on already-read
    values and reads no further bits:
      * 7 x readString    [+0x5c,+0x4c,+0x54,+0xb4,+0xb8,+0xbc,+0xc0]
      * 3 x readInt(6)    [+0x64,+0x68,+0x6c]  (each +1 numerically, no extra bits)
    """
    for _ in range(7):
        bs.read_string()
    bs.read_int(6)
    bs.read_int(6)
    bs.read_int(6)


def _unpack_audio_description(bs: BitStream) -> None:
    """AudioDescription::unpackData (@ VA 0x58e1e0).

    Parent SimDataBlock reads 0 bits. Then:
      * readFloat(6)                 -- mVolume                       [+0x34]
      * readFlag isLooping; if set: 3 x read(4) (U32)                 [+0x60..]
      * readFlag is3D                                                 [+0x39]
      * readFlag isStreaming; if set:
          read(4) U32, read(4) U32, readInt(9), readInt(9),
          readFloat(6), readNormalVector(8) (= 17 bits), read(4) U32
      * readInt(3)                                                    [+0x6c]
    """
    bs.read_float(6)                  # mVolume
    if bs.read_flag():                # isLooping
        _read_f32(bs)
        _read_f32(bs)
        _read_f32(bs)
    bs.read_flag()                    # is3D
    if bs.read_flag():                # isStreaming
        _read_f32(bs)
        _read_f32(bs)
        bs.read_int(9)
        bs.read_int(9)
        bs.read_float(6)
        _read_normal_vector(bs, 8)
        _read_f32(bs)
    bs.read_int(3)


def _unpack_audio_profile(bs: BitStream) -> None:
    """AudioProfile::unpackData (@ VA 0x58e3e0).

    Parent (SimDataBlock::unpackData @ 0x4c99b0) reads 0 bits. Then:
      * db-ref (AudioDescription)   -- flag + readInt(10)+3   [edi+0x38]
      * db-ref (AudioProfile/desc)  -- flag + readInt(10)+3   [edi+0x3c]
      * readString (filename)       -- bitstream vtable slot 0x1c
    """
    _read_db_ref(bs)   # AudioDescription
    _read_db_ref(bs)   # second datablock ref
    bs.read_string()   # filename


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

# name -> unpackData decoder. Only classes actually present in the real capture
# need entries; everything else raises DataBlockDecodeError (logged by caller).
DECODERS: Dict[str, Callable[[BitStream], None]] = {
    "AudioDescription": _unpack_audio_description,
    "AudioProfile": _unpack_audio_profile,
    "CameraData": _unpack_camera_data,
    # MissionMarkerData::unpackData (@ VA 0x47cad0) == ShapeBaseData (no override).
    "MissionMarkerData": _unpack_shape_base_data,
    "StaticShapeData": _unpack_static_shape_data,
    "TriggerData": _unpack_trigger_data,
    "ParticleData": _unpack_particle_data,
    "ParticleEmitterData": _unpack_particle_emitter_data,
    "ProjectileData": _unpack_projectile_data,
    "ItemData": _unpack_item_data,
    "TSShapeConstructor": _unpack_ts_shape_constructor,
    "DecalData": _unpack_decal_data,
    "PlayerData": _unpack_player_data,
    # ParticleEmitterNodeData::unpackData == VA 0x4b6690 (same fn as TriggerData):
    # GameBaseData (0 bits) + one read(4) U32.
    "ParticleEmitterNodeData": _unpack_trigger_data,
    "LightningData": _unpack_lightning_data,
    "fxLightData": _unpack_fx_light_data,
    "PrecipitationData": _unpack_precipitation_data,
    "AudioEnvironment": _unpack_audio_environment,
    "AudioSampleEnvironment": _unpack_audio_sample_environment,
    "PathedInteriorData": _unpack_pathed_interior_data,
    "WheeledVehicleSpring": _unpack_wheeled_vehicle_spring,
    "WheeledVehicleTire": _unpack_wheeled_vehicle_tire,
    "fxDTSBrickData": _unpack_fx_dts_brick_data,
    # PathCameraData::unpackData (@ VA 0x464210) tail-jumps to ShapeBaseData
    # (0x47cad0), exactly like CameraData -- no PathCamera-specific net fields.
    "PathCameraData": _unpack_camera_data,
    # Base classes (not normally streamed standalone, but exact and harmless):
    "GameBaseData": _unpack_game_base_data,   # 0 bits
    "ShapeBaseData": _unpack_shape_base_data,
    "SimDataBlock": _unpack_sim_data_block,   # 0 bits
    "ExplosionData": _unpack_explosion_data,
    "SplashData": _unpack_splash_data,
    "DebrisData": _unpack_debris_data,
    # ShapeBaseImageData (VA 0x4859a0): bit-exact (Wave-8). The Wave-7 "1431 vs
    # 1479 overrun" was a measurement artifact -- the probe re-ran the decoder
    # AFTER read_packet_body's `finally: setStringBuffer(None)` had already
    # cleared the per-packet string buffer, so the leading readString took the
    # no-buffer path and swallowed the useStringBuffer flag, desyncing. With the
    # buffer installed (as during a real packet read) the decoder is exact:
    # registering it advances the capture stream past index 72 to ProjectileData.
    "ShapeBaseImageData": _unpack_shape_base_image_data,
    # ------------------------------------------------------------------ #
    # DELIBERATELY UNPORTED (documented; raise cleanly with the class name)
    # ------------------------------------------------------------------ #
    # The three vehicle datablocks chain through VehicleData::unpackData
    # (@ VA 0x4ccc60) -> ShapeBaseData, a large CFG with several inline-flag
    # gated db-ref loops (body.sound[MaxSounds], waterSound[MaxSounds], damage/
    # splash emitter id lists) plus per-subclass tails. The AoT fork's exact
    # loop counts/widths could not be pinned by static CFG with the certainty
    # required (one wrong bit silently desyncs the whole datablock stream), and
    # NO vehicle datablock appears in ANY captured AoT world (the live world has
    # no vehicles), so per the never-guess rule they are left raising:
    #   FlyingVehicleData  (id 8,  unpackData @ 0x4c7510)  parent VehicleData
    #   HoverVehicleData   (id 10, unpackData @ 0x4c9f90)  parent VehicleData
    #   WheeledVehicleData (id 29, unpackData @ 0x4d1450)  parent VehicleData
    # To port: CFG-follow VehicleData::unpackData @ 0x4ccc60 first (it is the
    # shared parent), then each subclass tail; validate against a capture of a
    # world that actually scopes a vehicle.
}


def unpack_datablock(bs: BitStream, class_id: int) -> None:
    """Dispatch to the per-class ``unpackData`` decoder.

    Raises :class:`DataBlockDecodeError` (with the class name) if we have no
    decoder for ``class_id`` -- the bitstream cannot stay aligned past an
    un-decoded datablock (no length prefix).
    """
    name = (
        DATABLOCK_CLASS_NAMES[class_id]
        if 0 <= class_id < len(DATABLOCK_CLASS_NAMES)
        else f"<{class_id}>"
    )
    dec = DECODERS.get(name)
    if dec is None:
        raise DataBlockDecodeError(class_id, name)
    dec(bs)
