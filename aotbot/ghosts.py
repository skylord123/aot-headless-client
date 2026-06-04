"""Per-class NetObject ``unpackUpdate`` decoders for the AoT ghost section.

Once the server activates ghosting it streams object state in the ghost section
of each DataPacket (ghostReadPacket @ VA 0x549890; see
``phases._read_ghost_section``). For each ghost the body is the object's
``unpackUpdate(connection, stream)`` -- a per-class, length-less, bit-packed
payload (NetObject vtable slot 0x4c). To stay bit-aligned we must reproduce each
scoped class's ``unpackUpdate`` exactly.

This is the analogue of ``datablocks.py`` for ghosted *objects* (not
datablocks). The NetObject (game) class list, sorted by ASCII name, gives the
6-bit object classId (index == classId).

NOTE: the object ``unpackUpdate`` methods are substantially larger than the
datablock ``unpackData`` (they pack a transform + a sequence of update masks,
each gating its own field block) and must be RE'd per class. None are ported
yet, so any ghost class raises :class:`GhostDecodeError` carrying the class name
-- the caller (phases) turns that into an AlignmentError that logs exactly which
object class blocks, which is the precise next RE target.
"""

from __future__ import annotations

import logging

from .bitstream import BitStream
from . import telemetry

logger = logging.getLogger("aotbot.ghosts")

# NetClassTypeObject (game) class list, sorted by ASCII name == on-wire classId
# (re-deep-findings.md Target 1). Index is the 6-bit object classId.
OBJECT_CLASS_NAMES = [
    "AIPlayer", "AudioEmitter", "Camera", "Debris", "DestructableSpawner",
    "FlyingVehicle", "GameBase", "GoldSpawner", "HoverVehicle",
    "InteriorInstance", "Item", "Lightning", "Marker", "MazeSpawner",
    "MissionArea", "MissionMarker", "NPCSpawner", "ParticleEmitterNode",
    "PathCamera", "PathedInterior", "PhysicalZone", "Player", "Precipitation",
    "Projectile", "RoomMarker", "ScopeAlwaysShape", "ShapeBase",
    "SimpleNetObject", "Sky", "SpawnSphere", "Splash", "StaticShape", "Sun",
    "TSStatic", "TerrainBlock", "Trigger", "VehicleBlocker", "WaterBlock",
    "WayPoint", "WheeledVehicle", "fxBrickBatcher", "fxDTSBrick",
    "fxFoliageReplicator", "fxGrassReplicator", "fxLight",
    "fxShapeReplicatedStatic", "fxShapeReplicator", "fxSunLight",
    "twSurfaceReference", "volumeLight",
]


class GhostDecodeError(Exception):
    """A ghost object class whose ``unpackUpdate`` is not implemented appeared.

    Carries the class name/id so the caller can log exactly which class blocks.
    """

    def __init__(self, class_id: int, name: str) -> None:
        super().__init__(f"no unpackUpdate decoder for object class {name} (id {class_id})")
        self.class_id = class_id
        self.name = name


# --------------------------------------------------------------------------- #
# Shared bitstream primitive shims (mirror the exe helper VAs).
# --------------------------------------------------------------------------- #


def _read_point3f(bs: BitStream) -> bytes:
    """mathRead Point3F (AoT @ VA 0x421240): 3 x raw 4-byte F32 = 12 bytes.

    Returns the 12 raw little-endian bytes so callers can surface the (x,y,z)
    for telemetry; the bit cursor advances identically regardless.
    """
    return bs.read_bytes(12)


def _read_box6f(bs: BitStream) -> bytes:
    """mathRead "Box6F"/sphere (AoT @ VA 0x421800): **193 bits**, NOT 24 bytes.

    WAVE-12 RUNTIME FIX (winedbg-instrumented). Static RE read this as a plain
    24-byte ``Point3F + 3 x read(4)`` (192 bits). The disassembly of 0x421800,
    cross-checked against a live winedbg trace of fxShapeReplicator::unpackUpdate
    (the BitStream bit cursor at ``[esi+0xc]`` advanced by exactly **193** across
    this call in all four observed instances), shows it actually reads:

        Point3F (3 x read(4) = 96 bits)   -- the box/sphere center
        3 x read(4)               = 96 bits   -- extents
        a trailing inline readFlag = 1 bit    -- a sign flag for the derived
                                                 radius/magnitude (the engine
                                                 computes sqrt(x^2+y^2+z^2),
                                                 then reads a sign bit @0x4218ad
                                                 and negates via ``fchs`` if set)

    = 192 + 1 = **193 bits**. The missing sign flag was the entire 17-bit
    fxShapeReplicator slip: omitting it left the cursor 1 bit short entering the
    variable-length ``readString``, which then self-terminated at a garbage
    offset (Huffman) and ran far past the true field end -- the cumulative
    misread that presented as a 17-bit deficit. With the sign flag restored the
    string starts byte-aligned at the correct offset and the whole replicator
    decodes bit-exact.

    Returns the leading 12 bytes (the box/sphere CENTER Point3F) so callers can
    use it as a position for objects whose transform is a worldBox.
    """
    center = bs.read_bytes(24)
    bs.read_flag()
    return center[:12]


def _emit_box6f_position(raw: bytes) -> None:
    """Surface the leading Point3F of a Box6F (the first 12 bytes returned by
    :func:`_read_box6f`) as a ``world_box`` telemetry field.

    For the marker/spawner/light/replicator classes whose ``unpackUpdate`` carries
    no GameBase/controlled-pose Point3F, this leading point is the object's world
    origin on the wire (the AoT captures show the box-max reading ~0). The
    registry uses ``world_box`` as the position fallback (telemetry.update_from_sink)
    only when no authoritative ``position`` was emitted, so emitting it here never
    overrides a real transform."""
    telemetry.emit_point3f("world_box", raw)


def _read_matrix(bs: BitStream) -> bytes:
    """A 16-float (64-byte) MatrixF (AoT @ VA 0x465750: 16 x read(4)).

    Returns the raw 64 bytes; the translation (object position) is the 4th column
    of the row-major MatrixF -- floats at element indices 3, 7, 11."""
    return bs.read_bytes(16 * 4)


def _emit_matrix_position(raw: bytes) -> None:
    """Surface the translation of a 64-byte MatrixF as the telemetry position."""
    sink = telemetry.active_sink()
    if sink is None or raw is None or len(raw) < 64:
        return
    import struct as _struct
    try:
        m = _struct.unpack_from("<16f", raw, 0)
    except _struct.error:  # pragma: no cover - defensive
        return
    sink.set("position", (m[3], m[7], m[11]))


def _read_colorf(bs: BitStream) -> None:
    """ColorF::read (AoT @ VA 0x4243f0): 4 x read(1) raw bytes = 4 bytes."""
    bs.read_bytes(4)


import math as _math


def _read_normal_vector(bs: BitStream, bits: int) -> tuple:
    """BitStream::readNormalVector (AoT @ VA 0x4216f0): two ``readSignedFloat``
    (0x4210b0) of width ``bits+1`` then ``bits`` = ``2*bits+1`` bits total, turned
    into a unit direction vector.

    RUNTIME-CONFIRMED decode (winedbg trace of Player/ShapeBase unpackUpdate; the
    0x4216f0 disassembly cross-checked field-by-field). The exe does:

        phi   = readSignedFloat(bits+1) * PI      (const @ 0x5f1d68 = 3.14159265)
        theta = readSignedFloat(bits)   * PI/2    (const @ 0x5f1d60 = 1.57079632)
        v = ( cos(phi)*sin(theta),
              sin(phi)*sin(theta),
              cos(theta) )                          (the fcos/fsin sequence)

    where ``readSignedFloat(n)`` = ``2*readInt(n)/(2^n - 1) - 1`` (a value in
    [-1, 1], asm 0x4210eb fild/fidiv/fsub).

    PRIOR BUG: this read the two fields as *raw little-endian byte words* and
    surfaced them un-decoded -- that is exactly the bogus ``rot=[255,255]`` the
    telemetry showed for players. We now decode the real unit vector and return
    ``(x, y, z)``; :func:`_emit_rotation` turns it into a Z-yaw heading matching
    ``getTransform``'s ``0 0 1 angle`` axis-angle.
    """
    phi = bs.read_signed_float(bits + 1) * _math.pi
    theta = bs.read_signed_float(bits) * (_math.pi / 2.0)
    st = _math.sin(theta)
    return (
        _math.cos(phi) * st,
        _math.sin(phi) * st,
        _math.cos(theta),
    )


def _emit_rotation(vec: tuple) -> None:
    """Surface a decoded normal-vector orientation as telemetry ``rotation``.

    ``vec`` is the (x, y, z) unit direction from :func:`_read_normal_vector`. We
    record the Z-axis yaw ``angle = atan2(y, x)`` (normalised to [0, 2*pi)) as the
    primary scalar -- the same convention ``getTransform`` reports for an upright
    player as ``0 0 1 angle`` -- plus the raw unit vector for callers that want the
    full direction. The sink stores the first rotation seen per record (the
    object's own orientation, read before any inner/mounted-image fields)."""
    sink = telemetry.active_sink()
    if sink is None or vec is None:
        return
    x, y, z = vec
    angle = _math.atan2(y, x)
    if angle < 0:
        angle += 2.0 * _math.pi
    sink.set("rotation", {
        "angle": round(angle, 6),
        "axis": [0.0, 0.0, 1.0],
        "vector": [round(c, 6) for c in vec],
    })


def _emit_yaw(yaw: float) -> None:
    """Surface a Player's BODY YAW as the authoritative ``rotation``.

    The controlled-pose ``readFloat(7) * 2*PI`` (asm 0x46ef03; const 0x5f1d78 =
    2*PI) is the player's mRot.z. getTransform reports the equivalent Z-axis
    rotation as ``0 0 1 angle`` with the OPPOSITE winding, i.e. ``angle = (2*PI -
    yaw) mod 2*PI`` -- LIVE-CONFIRMED against the known target "Jeff Bezos":
    wire yaw 5.6400 -> 2*PI - 5.6400 = 0.643, matching his getTransform angle
    0.637333 (within the 7-bit yaw quantisation step 2*PI/127 ~= 0.0495). We
    report that getTransform-convention angle as ``angle`` and keep the raw wire
    yaw as ``yaw`` for reference. Unlike :func:`_emit_rotation` (the head/look
    normal vector, near-constant for standing players) this is the real heading,
    so it OVERWRITES any earlier rotation in the sink for this object."""
    sink = telemetry.active_sink()
    if sink is None:
        return
    wire = yaw % (2.0 * _math.pi)
    angle = (2.0 * _math.pi - wire) % (2.0 * _math.pi)
    sink.fields["rotation"] = {
        "angle": round(angle, 6),
        "axis": [0.0, 0.0, 1.0],
        "yaw": round(wire, 6),
    }


_COMPRESSED_POINT_BITS = (16, 18, 20, 32)

# The fixed quantisation scale every readCompressedPoint caller passes (the
# F32 ``0x3c23d70a``, pushed by Player @0x46ee60, Camera @0x44e7c3, Projectile
# @0x476c45, Vehicle @0x4cf2a0). winedbg-confirmed.
_COMPRESSED_POINT_SCALE = 0.01


def _read_compressed_point(bs: BitStream):
    """BitStream::readCompressedPoint (AoT @ VA 0x421a70).

    ``readInt(2) type``; then:
      * type 3  -> 3 x F32 (12 bytes)  = a full-precision ABSOLUTE world point;
      * type 0/1/2 -> 3 x ``readSignedInt(gBitCounts[type])`` (16/18/20/32 bits,
        table @ 0x63c0f8) DEQUANTISED in-engine to a world point as

            component = readSignedInt(bits[type]) * scale + reference[component]

        where ``scale`` = 0.01 (the ``0x3c23d70a`` arg every caller pushes) and
        ``reference`` = the BitStream members ``[this+0x28..0x30]`` -- the
        receiving connection's CONTROL-OBJECT world position.

    WAVE-18 RUNTIME FIX (winedbg-instrumented; static RE was ambiguous on the
    dequant because the engine reloads the same stack slot as int then float).
    Breakpointing 0x421a70/0x421a89/0x421b42 in the LIVE client (BitStream
    ``this``=esi; cursor [esi+0xc]; reference [esi+0x28..0x30]; scale arg
    [esp+0x14]) showed, for the parked target "Jeff Bezos" and several moving
    NPCs, that types 0/1/2 are NOT garbage deltas but quantised world points
    relative to the client's own control-object pose. Evidence (esi=0x7ff7d8,
    scale=0.01, ref=(281.790985,175.593002,213.212006) = the docker client's
    control object "Mr Poopy Butthole"):
        raw (1077,-553,1)      -> (292.564,170.058,213.221)  == Jeff Bezos
        raw (-526,674,0)       -> (276.524,182.333,213.212)  == Sword Giver
        raw (12832,5741,-649)  -> (410.110,233.003,206.722)  == an Orc NPC
    each matching the engine's type-3 absolute / getTransform value. PRIOR CODE
    treated 0/1/2 as non-world and dropped them -> ``position: null`` for every
    PARKED remote player (they only ever send compressed pose updates; only
    moving objects periodically send a type-3 absolute, which is why moving NPCs
    decoded but Jeff/Horse did not).

    Returns ``(point, is_world)``; ``is_world`` is True for type 3 and for
    0/1/2 WHEN a compression reference is available (so the caller can emit it as
    a world position). If no reference is set yet (control object not decoded),
    the dequantised point is returned with ``is_world=False`` (callers skip it)
    -- but the raw signed ints are still consumed so the bit cursor stays exact.
    """
    import struct as _struct
    t = bs.read_int(2)
    if t == 3:
        raw = bs.read_bytes(12)
        try:
            return _struct.unpack_from("<fff", raw, 0), True
        except _struct.error:  # pragma: no cover - defensive
            return None, False
    n = _COMPRESSED_POINT_BITS[t]
    raw_ints = tuple(bs.read_signed_int(n) for _ in range(3))
    ref = telemetry.compression_point()
    if ref is None or len(ref) < 3:
        # No control-object reference yet: keep bit consumption exact but do not
        # surface a (wrong) absolute position.
        return raw_ints, False
    world = tuple(
        raw_ints[i] * _COMPRESSED_POINT_SCALE + ref[i] for i in range(3)
    )
    return world, True


def _read_move(bs: BitStream) -> None:
    """Move::unpack (AoT @ VA 0x45b000): 3 x [flag; if set readInt(16)] angle
    deltas, 3 x readInt(6) clamped position, a freeLook flag, then 6 trigger
    flags (MaxTriggerKeys=6). Idle move = 28 bits (matches the c2s move stream)."""
    for _ in range(3):
        if bs.read_flag():
            bs.read_int(16)
    for _ in range(3):
        bs.read_int(6)
    bs.read_flag()              # freeLook
    for _ in range(6):
        bs.read_flag()          # trigger[i]


def _read_tagged_string(bs: BitStream):
    """ConnectionStringTable tagged-string read (AoT @ VA 0x546fc0).

    inline readFlag present (@ 0x547021); if clear -> 0 bits. If set, an inline
    readFlag isTag (@ 0x5470fc): if set -> ``readInt(5)`` slot id (@ 0x54710e),
    resolved via the connection's *receive* NetStringTable ([eax+0x1ac]); if clear
    -> ``readString`` (the dedup-buffer string literal, vtable slot 0x1c,
    @ 0x547044).

    Returns the resolved string (or ``None`` if the field is absent / a slot we
    have not been taught). The bit cursor advances identically regardless. This is
    how a Player's NAME reaches the wire in ShapeBase::unpackUpdate (the
    skin/name override block @ 0x484732 stores it to ``mShapeNameTag`` [ebx+0x948]
    -- exactly what ``getShapeName`` returns)."""
    if not bs.read_flag():
        return None
    if bs.read_flag():
        slot = bs.read_int(5)               # tag slot id (0x54710e)
        return telemetry.resolve_string(slot)
    return bs.read_string()                 # literal string (0x547044)


def _unpack_shape_base(bs: BitStream, is_new: bool) -> None:
    """ShapeBase::unpackUpdate (AoT @ VA 0x483d90).

    Shared by every ShapeBase subclass (Player/AIPlayer/Item/StaticShape/Camera/
    Projectile etc.). CFG-followed end-to-end (0x483d90..0x484b57); each mask is
    an inline readFlag. Structure (every numeric width EXE-confirmed):

      GameBase::unpackUpdate (flag + Point3F if set);
      MASTER flag (@0x483e06) -- if clear, the whole rest is skipped;
      flag (@0x483e36): if set -> readFloat(6), readInt(2), readNormalVector(8);
      flag (@0x483f13): if set -> 4 x [ flag; if set: readInt(6),readInt(2),flag,flag ];
      flag (@0x4840ce): if set -> 4 x [ flag; if set: flag; if NOT that flag: readInt(10) ];
      flag (@0x4841ea): if set -> 8 x [ flag; if set:
            flag(if set:readInt(10)); flag x5; readInt(3); flag; readInt(6) x4 ];
      flag (@0x484572): if set ->
          flag (@0x4845a2): if set -> flag; flag;
              flag(@0x484646): if set: flag(@0x484680); read(4)[4 bytes];
                               else:    flag(@0x4846f2);
          flag (@0x484732): if set -> tagged-string read (0x546fc0);
          flag (@0x4847ce): if set -> readInt(8) cnt; cnt x [ flag; if set: readFloat(8) x4 ];
          flag (@0x4849d5): if set -> readInt(8) n; n x readInt(8); 20 x readInt(8);
      flag (@0x484aa4): if set -> flag (@0x484af4): if set -> readInt(14), readInt(5).
    """
    # GameBase::unpackUpdate (0x456da0): position mask + datablock mask (called as
    # the parent @ 0x483dbb).
    _unpack_game_base(bs, is_new)

    # MASTER update mask (0x483e06): nothing else if clear.
    if not bs.read_flag():
        return

    # 0x483e36: orientation/move-state block.
    if bs.read_flag():
        bs.read_float(6)            # readFloat(6) (0x483e56)
        bs.read_int(2)              # readInt(2)   (0x483e9a)
        # readNormalVector(8) = 17 bits (0x483eb0): the object's orientation.
        _emit_rotation(_read_normal_vector(bs, 8))

    # 0x483f13: 4-slot image-trigger loop (0x483f30..0x4840a0).
    if bs.read_flag():
        for _ in range(4):
            if bs.read_flag():      # per-slot present (0x483f58)
                bs.read_int(6)      # (0x483f71)
                bs.read_int(2)      # (0x483f7e)
                bs.read_flag()      # (0x483fad)
                bs.read_flag()      # (0x483fdf)

    # 0x4840ce: 4-slot image-skin loop (0x4840f0..0x4841aa).
    if bs.read_flag():
        for _ in range(4):
            if bs.read_flag():      # per-slot present (0x48411d)
                # 0x484147: if this flag is CLEAR, read a 10-bit datablock id
                # (readRangedU32(0,1023) = getBinLog2(getNextPow2(1024)) = 10).
                if not bs.read_flag():
                    bs.read_int(10)  # (0x484170)

    # 0x4841ea: 8-slot mounted-image loop (0x484207..0x48452e).
    if bs.read_flag():
        for _ in range(8):
            if bs.read_flag():       # per-slot present (0x484232)
                if bs.read_flag():   # has-datablock (0x484267)
                    bs.read_int(10)  # image datablock id (0x484276)
                # 5 inline flags (0x4842ed,0x484320,0x484353,0x484386,0x4843b9):
                for _ in range(5):
                    bs.read_flag()
                bs.read_int(3)       # (0x4843c9)
                bs.read_flag()       # (0x484489)
                for _ in range(4):
                    bs.read_int(6)   # (0x4844ba..0x4844db)

    # 0x484572: ShapeBase core-state block.
    if bs.read_flag():
        # 0x4845a2: damage/energy sub-block.
        if bs.read_flag():
            bs.read_flag()           # (0x4845d4)
            bs.read_flag()           # (0x484612)
            if bs.read_flag():       # (0x484646) -- if set:
                bs.read_flag()       # (0x484680)
                bs.read_bytes(4)     # read(4) (0x4846b2)
            else:                    # if clear:
                bs.read_flag()       # (0x4846f2)
        # 0x484732: skin/name override (tagged string) -> mShapeNameTag [+0x948].
        # This is the Player's NAME (what getShapeName returns), resolved via the
        # connection's receive NetStringTable for a tag slot, or a literal.
        if bs.read_flag():
            name = _read_tagged_string(bs)  # (0x546fc0)
            if name:
                telemetry.emit("name", name)
        # 0x4847ce: per-node mesh hidden/scale loop.
        if bs.read_flag():
            cnt = bs.read_int(8)     # (0x4847e1)
            for _ in range(cnt):
                if bs.read_flag():   # (0x4848f2)
                    for _ in range(4):
                        bs.read_float(8)  # (0x484901..0x484928)
        # 0x4849d5: thread/animation state.
        if bs.read_flag():
            n = bs.read_int(8)       # (0x4849f4)
            for _ in range(n):
                bs.read_int(8)       # (0x484a08)
            for _ in range(20):      # 0x14 unconditional threads (0x484a50..)
                bs.read_int(8)       # (0x484a54)

    # 0x484aa4: mount block.
    if bs.read_flag():
        # 0x484af4: if set -> mount object id + node.
        if bs.read_flag():
            telemetry.emit("mount", bs.read_int(14))  # mount ghost id (0x484b03)
            bs.read_int(5)           # mount node (0x484b2f)


def _unpack_fx_shape_replicator(bs: BitStream, is_new: bool) -> None:
    """fxShapeReplicator::unpackUpdate (AoT @ VA 0x4aef80).

    Parent (0x485790 = bare ``ret 8``, 0 bits), then a SINGLE update-mask flag
    (inline readFlag @ 0x4aefaa); if clear the method ends (0 further bits).
    If set, the whole replicator state follows, in this exact order
    (CFG-followed 0x4aefd6..0x4af2e2; each ``flag`` is an inline readFlag):

      Box6F(24B); 3 x readInt(32); readString; 4 x readInt(32);
      4 x Point3F(12B); readSignedInt(32); 5 x flag; readSignedInt(32);
      flag; Point3F(12B); 3 x flag; readInt(32); ColorF(4B); flag.

    After the reads it calls setTransform / rebuild helpers (no bitstream reads).
    """
    if not bs.read_flag():
        return
    _emit_box6f_position(_read_box6f(bs))  # +0x10 area box origin (0x4aefdd)
    for _ in range(3):
        bs.read_int(32)                   # +0x268,+0x270,+0x274
    bs.read_string()                      # +0x26c (0x4af013)
    for _ in range(4):
        bs.read_int(32)                   # +0x2ac,+0x2b0,+0x2b4,+0x2b8
    for _ in range(4):
        _read_point3f(bs)                 # +0x27c,+0x288,+0x294,+0x2a0
    bs.read_signed_int(32)                # +0x2bc (0x4af095)
    for _ in range(5):
        bs.read_flag()                    # +0x2c0,+0x2c1,+0x2c2,+0x2c3,+0x2c9
    bs.read_signed_int(32)                # +0x2c4 (0x4af1a8)
    bs.read_flag()                        # +0x2c8 (0x4af1df)
    _read_point3f(bs)                     # +0x2cc (0x4af1ef)
    for _ in range(3):
        bs.read_flag()                    # +0x2d9,+0x2d8,+0x2da
    bs.read_int(32)                       # +0x2dc (0x4af299)
    _read_colorf(bs)                      # +0x2e0 (0x4af2ad)
    bs.read_flag()                        # +0x278 (0x4af2e2)


def _unpack_fx_foliage_replicator(bs: BitStream, is_new: bool) -> None:
    """fxFoliageReplicator::unpackUpdate (AoT @ VA 0x4a5560).

    Parent (0x485790 = bare ``ret 8``, 0 bits), then a SINGLE master update-mask
    flag (inline readFlag @ 0x4a558b); if clear the method ends (``je 0x4a5c2b``,
    0 further bits). If set, the whole replicator state follows in this exact
    order (CFG-followed 0x4a55db..0x4a5b86 -- entirely sequential, the inner
    ``je`` after each ``read(1)`` skips only a field STORE, not a read; every
    intervening FLAG is an unconditional 1-bit bool read stored to a field):

      Box6F(24B); flag; 4 x read(4); readString; 8 x read(4);
      2 x read(1)byte; read(4); 2 x read(1)byte; 7 x read(4);
      2 x flag; 4 x read(4); 2 x flag; 3 x read(4);
      5 x flag; read(4); 2 x flag; read(4); ColorF(4B).

    After the reads it computes derived fields + rebuilds (no bitstream reads:
    the trailing 0x42e670/0x42d340/0x42d350 calls operate on the read string,
    and 0x4a4810 is a local rebuild gated on a non-wire field)."""
    if not bs.read_flag():            # master mask (0x4a558b -> je 0x4a5c2b)
        return
    # CFG-followed 0x4a55db..0x4a5b86 and re-verified read-by-read against the exe.
    # WAVE-17 FIX (live current-world regression): the four ``read(1)`` byte fields
    # at +0x394/+0x395/+0x39c/+0x39d are CONSECUTIVE (call-sites 0x4a5749, 0x4a5769,
    # 0x4a57a1, 0x4a57c1 -- each ``push ebx(==1); call [edx+4]; test al,al; je``,
    # i.e. Stream::read(1,&bool) reading a whole BYTE with NO 1-bit flag between
    # them). The previous (Wave-11) transcription inserted FOUR spurious
    # ``read_flag()`` (1 bit each) between these byte-reads -> a +4-bit over-read.
    # That over-read terminated the GhostAlways event burst one object early (the
    # next event's presence bit was misread as a 0 terminator), so the rest of the
    # ghost-always stream silently misaligned and the login response was never
    # decoded. (fxFoliageReplicator's master mask is clear in the older capture
    # worlds, so the spurious flags were never exercised there -- it only bites the
    # current world, which streams an fxFoliageReplicator with the mask SET.)
    _emit_box6f_position(_read_box6f(bs))  # area box origin (0x4a55db)
    bs.read_flag()                    # +0x358 (0x4a55e0)
    for _ in range(4):
        bs.read_bytes(4)              # (0x4a561b..0x4a5663)
    bs.read_string()                  # +0x364 (0x4a5674)
    for _ in range(8):
        bs.read_bytes(4)              # (0x4a568a..0x4a5732)
    bs.read_bytes(1)                  # +0x394 read(1) byte (0x4a5749)
    bs.read_bytes(1)                  # +0x395 read(1) byte (0x4a5769)
    bs.read_bytes(4)                  # +0x398 (0x4a578a)
    bs.read_bytes(1)                  # +0x39c read(1) byte (0x4a57a1)
    bs.read_bytes(1)                  # +0x39d read(1) byte (0x4a57c1)
    for _ in range(7):
        bs.read_bytes(4)              # (0x4a57e2..0x4a5872)
    bs.read_flag()                    # +0x3bc (0x4a587f)
    bs.read_flag()                    # +0x3bd (0x4a58b1)
    for _ in range(4):
        bs.read_bytes(4)              # (0x4a58ee..0x4a5936)
    bs.read_flag()                    # +0x3d0 (0x4a5943)
    bs.read_flag()                    # +0x3d1 (0x4a5975)
    for _ in range(3):
        bs.read_bytes(4)              # (0x4a59b2..0x4a59e2)
    for _ in range(5):
        bs.read_flag()                # (0x4a59ef..0x4a5ab7)
    bs.read_bytes(4)                  # (0x4a5af4)
    bs.read_flag()                    # (0x4a5b01)
    bs.read_flag()                    # (0x4a5b33)
    bs.read_bytes(4)                  # (0x4a5b70)
    _read_colorf(bs)                  # +0x3f4 (0x4a5b86)


def _unpack_fx_grass_replicator(bs: BitStream, is_new: bool) -> None:
    """fxGrassReplicator::unpackUpdate (AoT @ VA 0x4a9dd0).

    Same shape as fxFoliageReplicator (parent 0 bits; single master flag @
    0x4a9dfb -> ``je 0x4aa5f7`` end if clear), then one fully-sequential block
    (CFG-followed 0x4a9e4b..0x4aa54a; ``read(1)``=1 byte, intervening FLAGs are
    unconditional 1-bit bool fields):

      Box6F(24B); flag; 4 x read(4); readString; 8 x read(4);
      2 x read(1)byte; read(4); 2 x read(1)byte; 7 x read(4);
      2 x flag; 4 x read(4); 2 x flag; 3 x read(4); 6 x flag; read(4);
      2 x flag; read(4); 3 x ColorF(4B); 3 x flag; read(4); flag; read(4)."""
    if not bs.read_flag():            # master mask (0x4a9dfb -> je 0x4aa5f7)
        return
    # CFG-followed 0x4a9e4b..0x4aa54a. WAVE-11 FIX: the previous transcription
    # dropped FOUR interleaved flags (@0x4a9fc6, 0x4a9fe6, 0x4aa01e, 0x4aa03e)
    # between the byte-reads, under-reading by 4 bits and desyncing every packet
    # that scoped an fxGrassReplicator (e.g. capture pkt212+). Re-derived from the
    # disassembly: read(4)=slot-4 push 4; read(1)=slot-4 push ebx(==1) followed by
    # test al/setne/mov byte; flag=inline readFlag.
    _emit_box6f_position(_read_box6f(bs))  # area box origin (0x4a9e4b)
    bs.read_flag()                    # (0x4a9e73)
    for _ in range(4):
        bs.read_bytes(4)              # (0x4a9e8b..0x4a9ed3)
    bs.read_string()                  # (0x4a9ee4)
    for _ in range(8):
        bs.read_bytes(4)              # (0x4a9efa..0x4a9fa2)
    bs.read_bytes(1)                  # read(1) byte (0x4a9fb9)
    bs.read_flag()                    # flag (0x4a9fc6)
    bs.read_bytes(1)                  # read(1) byte (0x4a9fd9)
    bs.read_flag()                    # flag (0x4a9fe6)
    bs.read_bytes(4)                  # (0x4a9ffa)
    bs.read_bytes(1)                  # read(1) byte (0x4aa011)
    bs.read_flag()                    # flag (0x4aa01e)
    bs.read_bytes(1)                  # read(1) byte (0x4aa031)
    bs.read_flag()                    # flag (0x4aa03e)
    for _ in range(7):
        bs.read_bytes(4)              # (0x4aa052..0x4aa0e2)
    bs.read_flag()                    # (0x4aa112)
    bs.read_flag()                    # (0x4aa144)
    for _ in range(4):
        bs.read_bytes(4)              # (0x4aa15e..0x4aa1a6)
    bs.read_flag()                    # (0x4aa1d6)
    bs.read_flag()                    # (0x4aa208)
    for _ in range(3):
        bs.read_bytes(4)              # (0x4aa222..0x4aa252)
    for _ in range(6):
        bs.read_flag()                # (0x4aa282..0x4aa387)
    bs.read_bytes(4)                  # (0x4aa3a1)
    bs.read_flag()                    # (0x4aa3d1)
    bs.read_flag()                    # (0x4aa403)
    bs.read_bytes(4)                  # (0x4aa41d)
    _read_colorf(bs)                  # (0x4aa433)
    _read_colorf(bs)                  # (0x4aa441)
    _read_colorf(bs)                  # (0x4aa44f)
    bs.read_flag()                    # (0x4aa477)
    bs.read_flag()                    # (0x4aa4a9)
    bs.read_flag()                    # (0x4aa4db)
    bs.read_bytes(4)                  # (0x4aa4f5)
    bs.read_flag()                    # (0x4aa525)
    bs.read_bytes(4)                  # (0x4aa54a)


def _unpack_fx_dts_brick(bs: BitStream, is_new: bool) -> None:
    """fxDTSBrick::unpackUpdate (AoT @ VA 0x49f9c0; vtable 0x5fe63c slot 0x4c).

    Parent SceneObject (0x485790 = bare ``ret 8``, 0 bits), then an outer
    InitMask flag A (@0x49fa00 -> ``je 0x49ff04`` epilogue if clear). CFG-followed
    0x49fa00..0x49ff04; each ``flag`` is an inline readFlag.

    A-set block:
      flag B (@0x49fa37): if set readInt(10)   (brick datablock id, getNextPow2(0x400)
                                                 -> getBinLog2 -> 10 bits, +3 stored);
      3 x readSignedInt(20)   (@0x49faf0/0x49fb01/0x49fb1c, helper 0x421570 = sign +
                               19 magnitude = 20 bits; grid position);
      readInt(2)              (@0x49fb43, orientation -> a no-read switch);
      6 x readInt(10)         (loop @0x49fbf5, edi=6; the +0x2ec colour/angle array);
      3 x readInt(8)          (@0x49fc06/0x49fc11/0x49fc22);
      4 x flag                (@0x49fc56/0x49fc89/0x49fcbf/0x49fcf5 -> +0x261/298/299/29a);
      flag C (@0x49fd95): if clear -> epilogue. If set:
          flag             (@0x49fde6 -> +0x260);
          readInt(6)       (@0x49fdec -> +0x308, colour index);
          flag             (@0x49fe70 -> +0x304);
          flag             (@0x49fea6 -> +0x305).
    (The intervening 0x49fa9d matrix rep-movsd, the transform math, and the
    0x49fd1b..0x49fd73 / 0x49fe0f..0x49fe59 datablock-lookup blocks read NO
    bitstream bits -- they operate on already-read fields.)"""
    # parent SceneObject (0x485790, 0 bits)
    if not bs.read_flag():            # InitMask flag A (0x49fa00)
        return
    if bs.read_flag():                # flag B (0x49fa37)
        bs.read_int(10)               # brick datablock id (0x49fa42..0x49fa58)
    for _ in range(3):
        bs.read_signed_int(20)        # grid position (0x421570, 0x49faf0..)
    bs.read_int(2)                    # orientation (0x49fb43)
    for _ in range(6):
        bs.read_int(10)               # +0x2ec array (0x49fbf5 loop)
    for _ in range(3):
        bs.read_int(8)                # (0x49fc06/0x49fc11/0x49fc22)
    bs.read_flag()                    # +0x261 (0x49fc56)
    bs.read_flag()                    # +0x298 (0x49fc89)
    bs.read_flag()                    # +0x299 (0x49fcbf)
    bs.read_flag()                    # +0x29a (0x49fcf5)
    if bs.read_flag():                # flag C (0x49fd95)
        bs.read_flag()                # +0x260 (0x49fde6)
        bs.read_int(6)                # +0x308 colour index (0x49fdec)
        bs.read_flag()                # +0x304 (0x49fe70)
        bs.read_flag()                # +0x305 (0x49fea6)


def _unpack_particle_emitter_node(bs: BitStream, is_new: bool) -> None:
    """ParticleEmitterNode::unpackUpdate (AoT @ VA 0x4b6a00; vtable 0x600e64 slot
    0x4c). CFG-followed 0x4b6a00..0x4b6ab7:

      GameBase::unpackUpdate (0x456da0: flag; if set Point3F);
      matrix (64 bytes, 0x465750)   -- mObjToWorld;
      Point3F (12 bytes, 0x421240)  -- mObjScale;
      flag (@0x4b6a80): if set readInt(10) (emitter datablock id, +3 @0x26c).
    """
    _unpack_game_base(bs, is_new)     # parent (0x456da0)
    _emit_matrix_position(_read_matrix(bs))  # mObjToWorld matrix (0x465750)
    _read_point3f(bs)                 # mObjScale (0x421240)
    if bs.read_flag():                # (0x4b6a80)
        telemetry.emit("datablock_id", bs.read_int(10))  # emitter db id (0x4b6aa9)


def _unpack_game_base(bs: BitStream, is_new: bool) -> None:
    """GameBase::unpackUpdate (AoT @ VA 0x456da0).

    CFG-followed 0x456da0..0x456e9e -- it reads **TWO** mask-gated blocks, not one:

      flag (@0x456dd2): if set -> Point3F handed to setTransform (call [vtbl+0x74]
                        @0x456dfb) = the object's world POSITION.
      flag (@0x456e2b): if set -> readInt(getBinLog2(0x400)=10)+3 = the object's
                        DATABLOCK id (the 0x4244e0/0x424510 getNextPow2/getBinLog2
                        pair @0x456e42 yields width 10; `add eax,3` @0x456e64 is
                        DataBlockObjectIdFirst, then a Sim::findObject lookup).

    The second (datablock) flag is part of every GameBase-derived unpackUpdate
    (ShapeBase calls this whole function as its parent @0x483dbb). For the
    connect/login captures it is typically clear (datablock already known from the
    initial scope), but it MUST be read or any update that carries it desyncs.
    Debris does not override slot 0x4c (== 0x456da0)."""
    # The first masked Point3F is handed to [vtbl+0x74] (setScale on AoT's
    # SceneObject -- the values observed live are the 0.9..1.1 character scale /
    # the 1,1,1 default, NOT a world position), so it is recorded as ``scale``,
    # not ``position``. The authoritative world position for ShapeBase-derived
    # objects is the mObjToWorld matrix (TSStatic/Interior/ParticleEmitterNode) or
    # the controlled-pose compressed point (Player); markers/items expose only
    # scale + shape here.
    if bs.read_flag():                       # scale mask (0x456dd2)
        telemetry.emit_point3f("scale", _read_point3f(bs))
    if bs.read_flag():                       # datablock mask (0x456e2b)
        telemetry.emit("datablock_id", bs.read_int(10) + 3)  # (0x456e53 + add 3)


def _unpack_sun(bs: BitStream, is_new: bool) -> None:
    """Sun::unpackUpdate (AoT @ VA 0x55d4e0): a single mask flag (@0x55d523);
    if set, a Point3F(12B) then 8 x read(4) (direction colours/elevation)."""
    if bs.read_flag():
        _read_point3f(bs)             # (0x55d52e)
        bs.read_bytes(8 * 4)          # 8 x read(4) (0x55d541..0x55d5d7)


def _unpack_terrain_block(bs: BitStream, is_new: bool) -> None:
    """TerrainBlock::unpackUpdate (AoT @ VA 0x563bb0; vtable slot 0x4c).

    No parent read. Two MUTUALLY-EXCLUSIVE mask-gated blocks (CFG-followed
    0x563bb0..0x563dd0; the count fields are raw 4-byte ``read(4)`` U32s, each
    followed by a ``count`` x ``read(4)`` loop -- the +0x30c VECTOR length then
    that many +0x314 entries):

      flag1 (@0x563bf9): if SET (the InitMask/full update) ->
          read(4); 3 x readString; 4 x read(4);
          read(4) count1; count1 x read(4);   (the loop @0x563cf1)
          [returns here -- flag2 is NOT read on the SET path]
      else (flag1 CLEAR) -> flag2 (@0x563d3b): if SET ->
          read(4) count2; count2 x read(4).   (the loop @0x563d90)

    The two ``getNextPow2/getBinLog2``-free ``read(4)`` counts are full 32-bit
    lengths; guard against absurd values from an already-desynced stream."""
    if bs.read_flag():                        # flag1 InitMask (0x563bf9)
        bs.read_bytes(4)                      # +0x374 (0x563c16)
        bs.read_string()                      # +0x308 (0x563c27)
        bs.read_string()                      # +0x2e8 (0x563c36)
        bs.read_string()                      # +0x2f0 (0x563c45)
        for _ in range(4):
            bs.read_bytes(4)                  # +0x2fc..+0x378 (0x563c5b..0x563ca3)
        count = int.from_bytes(bs.read_bytes(4), "little")  # +0x30c (0x563cbb)
        if count > (1 << 20):
            raise GhostDecodeError(34, "TerrainBlock material count overflow")
        for _ in range(count):
            bs.read_bytes(4)                  # (0x563d09 loop)
        return
    if bs.read_flag():                        # flag2 (0x563d3b)
        count = int.from_bytes(bs.read_bytes(4), "little")  # +0x30c (0x563d5c)
        if count > (1 << 20):
            raise GhostDecodeError(34, "TerrainBlock material count overflow")
        for _ in range(count):
            bs.read_bytes(4)                  # (0x563d90 loop)


def _unpack_fx_sun_light(bs: BitStream, is_new: bool) -> None:
    """fxSunLight::unpackUpdate (AoT @ VA 0x4b2470; vtable slot 0x4c).

    Parent (0x485790 = bare ``ret 8``, 0 bits), then a master update-mask flag
    (inline readFlag @ 0x4b24bb; ``je 0x4b2ad0`` epilogue if clear). If set, the
    whole light state follows in this exact order (CFG-followed
    0x4b24cc..0x4b2ad0; every read counted read-by-read against the exe;
    ``read(1)`` = a whole BYTE via Stream::read(1,&bool), FLAG = a 1-bit inline
    readFlag, the 0x4b01a0 ``setNames`` call after the 2 strings reads no bits):

      Box6F(24B); read(1)byte; 2 x readString; 2 x read(4); read(1)byte;
      ColorF(4B); 4 x read(4); 14 x flag; 2 x ColorF(4B); 10 x read(4);
      8 x readString; 6 x read(4).
    """
    if not bs.read_flag():                # master mask (0x4b24bb -> je 0x4b2ad0)
        return
    _emit_box6f_position(_read_box6f(bs))  # light box origin (0x4b24d3)
    bs.read_bytes(1)                      # +0x275 read(1) byte (0x4b24e2)
    bs.read_string()                      # (0x4b24fc)
    bs.read_string()                      # (0x4b2507) -> setNames 0x4b01a0 (0 bits)
    bs.read_bytes(4)                      # +0x280 (0x4b2520)
    bs.read_bytes(4)                      # +0x284 (0x4b2538)
    bs.read_bytes(1)                      # read(1) byte (0x4b254f)
    _read_colorf(bs)                      # (0x4b256e)
    for _ in range(4):
        bs.read_bytes(4)                  # (0x4b257e..0x4b25c6)
    for _ in range(14):
        bs.read_flag()                    # +0x2ac.. (0x4b25fb..0x4b289d)
    _read_colorf(bs)                      # (0x4b28b2)
    _read_colorf(bs)                      # (0x4b28c0)
    for _ in range(10):
        bs.read_bytes(4)                  # (0x4b28d0..0x4b29a8)
    for _ in range(8):
        bs.read_string()                  # (0x4b29b9..0x4b2a22)
    for _ in range(6):
        bs.read_bytes(4)                  # (0x4b2a38..0x4b2ab0)


def _unpack_mission_marker(bs: BitStream, is_new: bool) -> None:
    """MissionMarker::unpackUpdate (AoT @ VA 0x463620).

    ShapeBase parent, then a flag (@0x46366a): if set Box6F(24B) + Point3F(12B).

    Shared ONLY by classes whose vtable slot 0x4c is 0x463620 (or a bare
    ``jmp 0x463620``): NPCSpawner (0x4638a0 = jmp), MazeSpawner (0x4638a0),
    RoomMarker (0x4638a0). The other spawner/marker subclasses OVERRIDE
    unpackUpdate with extra trailing fields and have their own decoders below
    (DestructableSpawner/GoldSpawner/SpawnSphere/WayPoint) -- mapping them to this
    base under-read their tail and silently desynced the ghost stream."""
    _unpack_shape_base(bs, is_new)
    if bs.read_flag():                # (0x46366a)
        # mObjBox (local bounding box) + scale -- NOT a world position; the
        # position is the GameBase Point3F in the ShapeBase parent.
        telemetry.emit_point3f("world_box", _read_box6f(bs))  # mObjBox (0x46367d)
        _read_point3f(bs)             # mObjScale (0x463694)


def _unpack_destructable_spawner(bs: BitStream, is_new: bool) -> None:
    """DestructableSpawner::unpackUpdate (AoT @ VA 0x4639e0; vtable slot 0x4c):
    MissionMarker::unpackUpdate (0x463620) parent, then a flag (@0x463a24): if set
    ``read(4)`` (the +0xa7c field). The trailing read4 is exactly the 33-bit tail
    (1 flag + 32-bit read) that the shared MissionMarker decoder was missing."""
    _unpack_mission_marker(bs, is_new)       # parent (0x463620)
    if bs.read_flag():                        # (0x463a24)
        bs.read_bytes(4)                      # +0xa7c (0x463a3b)


def _unpack_gold_spawner(bs: BitStream, is_new: bool) -> None:
    """GoldSpawner::unpackUpdate (AoT @ VA 0x4638b0; vtable slot 0x4c):
    MissionMarker parent, then a flag (@0x4638f4): if set ->
    6 x read(4) (+0xa7c..+0xa94) then read(1)byte (+0xa98)."""
    _unpack_mission_marker(bs, is_new)       # parent (0x463620)
    if bs.read_flag():                        # (0x4638f4)
        for _ in range(6):
            bs.read_bytes(4)                  # +0xa7c..+0xa94 (0x46390f..0x4639a2)
        bs.read_bytes(1)                      # +0xa98 read(1) byte (0x4639b7)


def _unpack_spawn_sphere(bs: BitStream, is_new: bool) -> None:
    """SpawnSphere::unpackUpdate (AoT @ VA 0x4637e0; vtable slot 0x4c):
    MissionMarker parent, then a flag (@0x463824): if set -> 4 x read(4)
    (+0xa7c..+0xa88, 0x46383b..0x463883)."""
    _unpack_mission_marker(bs, is_new)       # parent (0x463620)
    if bs.read_flag():                        # (0x463824)
        for _ in range(4):
            bs.read_bytes(4)                  # +0xa7c..+0xa88


def _unpack_way_point(bs: BitStream, is_new: bool) -> None:
    """WayPoint::unpackUpdate (AoT @ VA 0x4636b0; vtable slot 0x4c):
    MissionMarker parent, then THREE independent flag-gated blocks:
      flag (@0x4636f4): if set -> readString (+0xa7c, 0x424230 @0x463702);
      flag (@0x463738): if set -> read(4)    (+0xa80, 0x46374e);
      flag (@0x46378a): if set -> flag       (+0xa38, a 1-bit bool @0x4637bc)."""
    _unpack_mission_marker(bs, is_new)       # parent (0x463620)
    if bs.read_flag():                        # (0x4636f4)
        bs.read_string()                      # +0xa7c (0x463702)
    if bs.read_flag():                        # (0x463738)
        bs.read_bytes(4)                      # +0xa80 (0x46374e)
    if bs.read_flag():                        # (0x46378a)
        bs.read_flag()                        # +0xa38 (0x4637bc)


def _unpack_simple_net_object(bs: BitStream, is_new: bool) -> None:
    """SimpleNetObject::unpackUpdate (AoT @ VA 0x4c2fc0): a single readString
    (vtable slot 0x1c @ 0x4c2fcb), no parent, no mask."""
    bs.read_string()


def _read_planef(bs: BitStream) -> None:
    """PlaneF::read (AoT @ VA 0x4656d0): 4 x read(4) = 16 bytes."""
    bs.read_bytes(16)


def _read_polyhedron(bs: BitStream) -> None:
    """Polyhedron read tail shared by Trigger/PhysicalZone: readInt(32) point
    count + that many Point3F(12B), then readInt(32) plane count + that many
    PlaneF(16B). (vecResize calls in between read no bits.)"""
    n_points = bs.read_int(32)
    if n_points > (1 << 20):
        raise GhostDecodeError(-1, "polyhedron point count overflow")
    for _ in range(n_points):
        _read_point3f(bs)
    n_planes = bs.read_int(32)
    if n_planes > (1 << 20):
        raise GhostDecodeError(-1, "polyhedron plane count overflow")
    for _ in range(n_planes):
        _read_planef(bs)


def _unpack_fx_brick_batcher(bs: BitStream, is_new: bool) -> None:
    """fxBrickBatcher::unpackUpdate (AoT @ VA 0x4a0d60): ``jmp 0x485790`` (bare
    ``ret 8``) -> reads ZERO bits. The brick batch carries no per-ghost wire
    state (it is rebuilt locally from the brick datablocks)."""
    return


def _unpack_trigger(bs: BitStream, is_new: bool) -> None:
    """Trigger::unpackUpdate (AoT @ VA 0x48ffb0): GameBase parent, then
    Box6F(24B), Point3F(12B), and a polyhedron (point list + plane list)."""
    _unpack_game_base(bs, is_new)
    _read_box6f(bs)                   # (0x490020)
    _read_point3f(bs)                 # (0x49002b)
    _read_polyhedron(bs)              # (0x49003e..0x490119)


def _unpack_physical_zone(bs: BitStream, is_new: bool) -> None:
    """PhysicalZone::unpackUpdate (AoT @ VA 0x4667d0): parent (0 bits), then a
    flag (@0x466845): if set -> a 16-float matrix (64B), Point3F(12B), and a
    polyhedron."""
    if bs.read_flag():                # (0x466845)
        bs.read_bytes(16 * 4)         # matrix (0x465750: 16 x read(4))
        _read_point3f(bs)             # (0x466892)
        _read_polyhedron(bs)          # (0x4668a5..0x46694a)


def _unpack_pathed_interior(bs: BitStream, is_new: bool) -> None:
    """PathedInterior::unpackUpdate (AoT @ VA 0x5157a0): GameBase parent, then a
    flag (@0x5157ee): if set -> readString, read(4), Box6F(24B), Point3F(12B),
    read(4)."""
    _unpack_game_base(bs, is_new)
    if bs.read_flag():                # (0x5157ee)
        bs.read_string()              # (0x515801)
        bs.read_bytes(4)              # (0x515818)
        _read_box6f(bs)               # (0x51582c)
        _read_point3f(bs)             # (0x515837)
        bs.read_bytes(4)              # (0x515876)


def _unpack_ts_static(bs: BitStream, is_new: bool) -> None:
    """TSStatic::unpackUpdate (AoT @ VA 0x4917e0; shared by
    fxShapeReplicatedStatic): parent (0 bits), then a 16-float matrix (64B),
    Point3F(12B), readString."""
    _emit_matrix_position(_read_matrix(bs))  # matrix (0x4917fc, 0x465750)
    _read_point3f(bs)                 # mObjScale (0x491807)
    name = bs.read_string()           # shapeName (0x49182b)
    if name:
        telemetry.emit("shape_file", name)


def _unpack_interior_instance(bs: BitStream, is_new: bool) -> None:
    """InteriorInstance::unpackUpdate (AoT @ VA 0x507b50).

    parent (0x485790, 0 bits), then the InitMask flag A (@0x507bc0): the standard
    NetObject init/non-init selector. CONFIRMED bit-exact against the WRITE side
    ``InteriorInstance::packUpdate`` @ VA 0x5084a0 (vtable slot 0x48): the A-set
    branch writes its fields UNCONDITIONALLY and the two trailing audio blocks
    (F = mAudioProfile, G = mAudioEnvironment) are ALWAYS-written 1-bit flags --
    there is NO outer gate.

    A-set (InitMask) path -- read in this exact order (10 fields):
      read(4)            mCRC                         (0x507bd6, slot 4)
      readString         mInteriorFileName            (0x507be8 -> +0x250)
      flag               mShowTerrainInside           (0x507c1c -> +0x278)
      flag               flagD                        (0x507c52 -> +0x290)
      matrix(64B)        mObjToWorld                  (0x507c65, 0x465750)
      Point3F(12B)       mObjScale                    (0x507c70, 0x421240)
      flag               mAlarmState                  (0x507cb9 -> +0x2ac)
      readString         mSkinBase                    (0x507cc9 -> +0x268)
      flag F; if set readInt(10)  mAudioProfile id    (0x507d16 -> +0x280)
      flag G; if set readInt(10)  mAudioEnvironment id (0x507d7e -> +0x284)
    (AoT dropped stock TGE's trailing mUseGLLighting flag -- packUpdate's init
    branch returns right after G at 0x508829.)

    A-clear (normal update) path (@ 0x507dd4..): flagX -> matrix+Point3F;
    flag (mAlarmState); the mUpdateGrouper light loop (data-driven, never on the
    wire for the connect window); flag -> readString (skinBase); flag -> audio
    block (flag F int10 + flag G int10). All 134 InteriorInstance updates in the
    golden capture are A-set (initial scope), so the light loop is never reached;
    if a normal update ever arrives with active light groups we'd need the
    grouper state -- left raising via GhostDecodeError below is NOT triggered here
    because the loop iterators are empty for freshly-scoped interiors.

    WAVE-11 FIX: the previous form read the wrong A-set sequence (it gated
    mCRC/mInteriorFileName behind flag A, dropped mAlarmState + the unconditional
    mSkinBase, and omitted F/G). The Wave-10 "F/G absent" conclusion was a
    misdiagnosis of a desync caused by that wrong sequence. F and G ARE always on
    the wire (as flags) for every A-set update; reading the correct sequence below
    aligns pkt180 AND pkt191+."""
    if bs.read_flag():                # InitMask flag A (0x507bc0)
        # --- A-set: initial update ---
        bs.read_bytes(4)              # mCRC (0x507bd6)
        fname = bs.read_string()      # mInteriorFileName (0x507be8)
        if fname:
            telemetry.emit("shape_file", fname)
        bs.read_flag()                # mShowTerrainInside (0x507c1c) +0x278
        bs.read_flag()                # flagD (0x507c52) +0x290
        _emit_matrix_position(_read_matrix(bs))  # mObjToWorld matrix (0x507c65)
        _read_point3f(bs)             # mObjScale (0x507c70)
        bs.read_flag()                # mAlarmState (0x507cb9) +0x2ac
        bs.read_string()              # mSkinBase (0x507cc9)
        if bs.read_flag():            # mAudioProfile present (F, 0x507d16)
            bs.read_int(10)           # readRangedU32(3,..) id (0x507d37)
        if bs.read_flag():            # mAudioEnvironment present (G, 0x507d7e)
            bs.read_int(10)           # readRangedU32(3,..) id (0x507da6)
    else:
        # --- A-clear: normal (non-initial) update ---
        if bs.read_flag():            # TransformMask flag (0x507df4)
            bs.read_bytes(16 * 4)     # mObjToWorld matrix (0x507dfc)
            _read_point3f(bs)         # mObjScale (0x507e07)
        bs.read_flag()                # mAlarmState (0x507e37) +0x2ac
        # mUpdateGrouper light loop (0x507e90..0x508006): iterators are empty for
        # freshly-scoped interiors, so it reads 0 bits in the connect window.
        if bs.read_flag():            # SkinBaseMask flag (0x508006)
            bs.read_string()          # mSkinBase (0x50802c)
        if bs.read_flag():            # AudioMask flag (0x50804e)
            if bs.read_flag():        # mAudioProfile present (0x5080d1)
                bs.read_int(10)
            if bs.read_flag():        # mAudioEnvironment present
                bs.read_int(10)


def _unpack_volume_light(bs: BitStream, is_new: bool) -> None:
    """volumeLight::unpackUpdate (AoT @ VA 0x4c1700): parent (0 bits), then
    Box6F(24B), read(1) (a whole BYTE), readString, 6 x read(4), 2 x ColorF(4B).
    (The je @0x4c1732 only skips a field store; no extra bitstream read.)"""
    _emit_box6f_position(_read_box6f(bs))  # light box origin (0x4c171d)
    bs.read_bytes(1)                  # read(1) byte (0x4c172d)
    bs.read_string()                  # (0x4c1747)
    for _ in range(6):
        bs.read_bytes(4)              # (0x4c175f..0x4c17d7)
    _read_colorf(bs)                  # (0x4c17ed)
    _read_colorf(bs)                  # (0x4c17fb)


def _unpack_mission_area(bs: BitStream, is_new: bool) -> None:
    """MissionArea::unpackUpdate (AoT @ VA 0x4620a0): a flag (@0x4620d9): if set
    a RectI (4 x read(4) via 0x461e60) then 2 x read(4)."""
    if bs.read_flag():                # (0x4620d9)
        bs.read_bytes(4 * 4)          # RectI (0x461e60)
        bs.read_bytes(4)              # (0x4620fd)
        bs.read_bytes(4)              # (0x462112)


def _unpack_audio_emitter(bs: BitStream, is_new: bool) -> None:
    """AudioEmitter::unpackUpdate (AoT @ VA 0x44d500 -> field reader 0x44d010).

    Parent (0x485790 = bare ret, 0 bits). The field reader 0x44d010 starts with
    ONE unconditional inline readFlag (@0x44d04e, the mute/state bit), then a
    long sequence of per-field ``fnFlag`` masks (each ``call 0x44c3a0`` reads one
    flag and returns it). Each set flag reads its field; the optional readInt(10)
    fields are in compiler-out-of-line blocks but read in this exact order
    (CFG-followed 0x44d010..0x44d4f9; ``read`` = raw 4-byte F32 via slot 4):

      iflag;
      0x10000: Box6F(24B);
      1:      iflag; if set readInt(10);     (datablock 0/AudioDescription idx)
      2:      iflag; if set readInt(10);     (datablock 1/AudioProfile idx)
      4:      readString;
      8:      iflag;                          (1 bit, looping flag)
      0x10:   read(4);
      0x20:   iflag;                          (1 bit)
      0x40:   iflag;                          (1 bit)
      0x80:   read(4);  0x100: read(4);  0x200: read(4);  0x400: read(4);
      0x800:  read(4);
      0x1000: read(4) x3;                     (a Point3F as 3 separate fields)
      0x2000: read(4);  0x4000: read(4);  0x8000: read(4);  0x20000: read(4);
      0x40000: iflag.                         (1 bit)
    """
    if bs.read_flag():                # leading inline flag (0x44d04e)
        pass
    if bs.read_flag():                # mask 0x10000 (0x44d061)
        _read_box6f(bs)               # (0x44d071)
    if bs.read_flag():                # mask 1 (0x44d087)
        if bs.read_flag():            # inner flag (0x44d133)
            bs.read_int(10)           # readRangedU32(0,1023) (0x44d158)
    if bs.read_flag():                # mask 2 (0x44d0ab)
        if bs.read_flag():            # inner flag (0x44d18a)
            bs.read_int(10)           # (0x44d1af)
    if bs.read_flag():                # mask 4 (0x44d0d3)
        bs.read_string()              # (0x44d0e0)
    if bs.read_flag():                # mask 8 (0x44d0f0)
        bs.read_flag()                # inner flag (0x44d1e1)
    if bs.read_flag():                # mask 0x10 (0x44d1f3)
        bs.read_bytes(4)              # (0x44d207)
    if bs.read_flag():                # mask 0x20 (0x44d219)
        bs.read_flag()                # inner flag (0x44d251)
    if bs.read_flag():                # mask 0x40 (0x44d263)
        bs.read_flag()                # inner flag (0x44d29b)
    for _ in range(5):                # masks 0x80,0x100,0x200,0x400,0x800
        if bs.read_flag():
            bs.read_bytes(4)
    if bs.read_flag():                # mask 0x1000 (0x44d37d)
        bs.read_bytes(12)             # 3 x read(4) (0x44d391,0x44d3a9,0x44d3c1)
    for _ in range(4):                # masks 0x2000,0x4000,0x8000,0x20000
        if bs.read_flag():
            bs.read_bytes(4)
    if bs.read_flag():                # mask 0x40000 (0x44d49f)
        bs.read_flag()                # inner flag (0x44d4d7)


def _unpack_water_block(bs: BitStream, is_new: bool) -> None:
    """WaterBlock::unpackUpdate (AoT @ VA 0x56f300). No master flag -- the whole
    body is unconditional except two ``flag; readInt(10)`` object-id blocks and
    a trailing 1-bit flag (CFG-followed 0x56f328..0x56f8ca):

      Box6F(24B); Point3F(12B); 7 x readString;
      6 x read(4); read(1)byte;
      flag @0x56f73c: if set readInt(10) (+0x534, readRangedU32(0,1023));
      read(1)byte; 12 x read(4); ColorF(4B); read(4);
      flag @0x56f8ca (1 bit; set-path rebuilds from already-read fields).

    The 7th string is read via a 2-iteration loop (ebp=2 @ 0x56f39c..0x56f3b0,
    so 5 explicit + 2 looped = 7 total)."""
    _emit_box6f_position(_read_box6f(bs))  # water box origin (0x56f336)
    _read_point3f(bs)                 # (0x56f343)
    for _ in range(7):
        bs.read_string()              # (0x56f34f..0x56f3a5 loop)
    for _ in range(6):
        bs.read_bytes(4)              # (0x56f6a0..0x56f70e)
    bs.read_bytes(1)                  # read(1) byte +0x530 (0x56f726)
    if bs.read_flag():                # flag F1 (0x56f73c)
        bs.read_int(10)               # +0x534 readRangedU32(0,1023) (0x56f920)
    bs.read_bytes(1)                  # read(1) byte +0x54c (0x56f76b)
    for _ in range(12):
        bs.read_bytes(4)              # (0x56f78c..0x56f894)
    _read_colorf(bs)                  # (0x56f8aa)
    bs.read_bytes(4)                  # +0x590 (0x56f8ba)
    bs.read_flag()                    # flag F2 (0x56f8ca)


def _unpack_vehicle(bs: BitStream, is_new: bool) -> None:
    """Vehicle::unpackUpdate (AoT @ VA 0x4cf130) -- the shared base for
    HoverVehicle/FlyingVehicle/WheeledVehicle. CFG-followed 0x4cf142..0x4cf783:

      ShapeBase parent (0x483d90);
      flag A (@0x4cf147, 1 bit -> +0xb7c, no branch);
      flag B (@0x4cf185, MASTER): if SET -> return (``jne 0x4cf77d``);
      readFloat(9); readFloat(9); Move::unpack (0x45b000);
      flag C (@0x4cf215): if set ->
          readCompressedPoint; PlaneF(16B); Point3F(12B); Point3F(12B);
          readFlag (0x421200, 1 bit -> +0x22e4);
      flag D (@0x4cf71e): if set -> readFloat(8)."""
    _unpack_shape_base(bs, is_new)    # parent (0x483d90)
    bs.read_flag()                    # flag A +0xb7c (0x4cf147)
    if bs.read_flag():                # flag B master (0x4cf185); set -> return
        return
    bs.read_float(9)                  # (0x4cf1c5)
    bs.read_float(9)                  # (0x4cf1d2)
    _read_move(bs)                    # Move::unpack (0x4cf210)
    if bs.read_flag():                # flag C (0x4cf215)
        _read_compressed_point(bs)    # (0x4cf2bd)
        _read_planef(bs)              # (0x4cf2c4)
        _read_point3f(bs)             # (0x4cf2d1)
        _read_point3f(bs)             # (0x4cf2de)
        bs.read_flag()                # +0x22e4 (0x4cf2e8, fn readFlag)
    if bs.read_flag():                # flag D (0x4cf71e)
        bs.read_float(8)              # (0x4cf763)


def _unpack_hover_vehicle(bs: BitStream, is_new: bool) -> None:
    """HoverVehicle::unpackUpdate (AoT @ VA 0x4c9aa0): Vehicle parent (0x4cf130)
    then an UNCONDITIONAL readInt(3) (+0x23a4, 0x4c9ab7)."""
    _unpack_vehicle(bs, is_new)       # parent (0x4cf130)
    bs.read_int(3)                    # +0x23a4 (0x4c9ab7)


def _unpack_sky(bs: BitStream, is_new: bool) -> None:
    """Sky::unpackUpdate (AoT @ VA 0x55c1b0). No parent. CFG-followed
    0x55c1b0..0x55cc0c.

    A master flag (@0x55c1bf) gates the big "settings" block; BOTH the set and
    clear paths then fall into a COMMON tail of 7 ``flag; if set {...}`` blocks
    (the clear branch of each flag jumps straight to the next, confirmed by the
    CFG convergence at 0x55c77e/0x55c823/.../0x55cb31).

    Master-flag SET block (0x55c1cd..0x55c724):
      readString; (CALL 0x55b9d0, no read);
      4 x read(4)  [the 4th @0x55c25e is the cloud-layer COUNT -> +0xe40];
      2 x read(1)byte; 3 x read(4); 2 x read(1)byte; read(4);
      flag (+0xec4, 1 bit, no branch);
      loop1: COUNT x [ 6 x read(4); read(1)byte ];        (count from +0xe40)
      loop2: 3 x [ readString; read(4); read(4) ];        (fixed count 3)
      (CALL 0x55b500); 3 x read(4); read(4) (+0xeb0);
      flag @0x55c55e: if set 5 x read(4) (+0xeac.. then FP, no more reads).

    Common tail (0x55c725, reached for BOTH master states):
      flag; if set read(1)byte;
      flag; if set read(1)byte;
      flag; if set 2 x read(4);
      flag; if set 2 x read(4);
      flag; if set 3 x read(4);
      flag; if set 4 x read(4);
      flag; if set 3 x read(4)."""
    if bs.read_flag():                    # master flag (0x55c1bf)
        bs.read_string()                  # (0x55c1f9)
        bs.read_bytes(4)                  # (0x55c216)
        bs.read_bytes(4)                  # (0x55c22e)
        bs.read_bytes(4)                  # +0xe1c (0x55c246)
        count = int.from_bytes(bs.read_bytes(4), "little")  # +0xe40 (0x55c25e)
        bs.read_bytes(1)                  # +0xe24 (0x55c276)
        bs.read_bytes(1)                  # +0xe25 (0x55c297)
        bs.read_bytes(4)                  # +0xe28 (0x55c2b8)
        bs.read_bytes(4)                  # +0xe2c (0x55c2d0)
        bs.read_bytes(4)                  # +0xe30 (0x55c2e8)
        bs.read_bytes(1)                  # (0x55c300)
        bs.read_bytes(1)                  # (0x55c321)
        bs.read_bytes(4)                  # (0x55c342)
        bs.read_flag()                    # +0xec4 (0x55c34f)
        if count > (1 << 20):
            raise GhostDecodeError(28, "Sky cloud-layer count overflow")
        for _ in range(count):            # loop1 (0x55c3a8..0x55c45e)
            for _ in range(6):
                bs.read_bytes(4)
            bs.read_bytes(1)              # read(1) byte (0x55c431)
        for _ in range(3):                # loop2 fixed 3 (0x55c470..0x55c4aa)
            bs.read_string()              # (0x55c474)
            bs.read_bytes(4)              # (0x55c487)
            bs.read_bytes(4)              # (0x55c49c)
        bs.read_bytes(4)                  # (0x55c4be)
        bs.read_bytes(4)                  # (0x55c4d6)
        bs.read_bytes(4)                  # (0x55c4f0)
        bs.read_bytes(4)                  # +0xeb0 (0x55c551)
        if bs.read_flag():                # flag (0x55c55e)
            bs.read_bytes(4)              # +0xeac (0x55c5a2)
            bs.read_bytes(4)              # (0x55c5ba)
            bs.read_bytes(4)              # (0x55c5d2)
            bs.read_bytes(4)              # (0x55c5e0)
            bs.read_bytes(4)              # (0x55c5ee)
    # --- common tail (0x55c725) -------------------------------------------- #
    if bs.read_flag():                    # (0x55c725)
        bs.read_bytes(1)                  # (0x55c768)
    if bs.read_flag():                    # (0x55c77e)
        bs.read_bytes(1)                  # (0x55c7c4)
    if bs.read_flag():                    # (0x55c823)
        bs.read_bytes(4)                  # (0x55c866)
        bs.read_bytes(4)                  # (0x55c87e)
    if bs.read_flag():                    # (0x55c892)
        bs.read_bytes(4)                  # (0x55c8dc)
        bs.read_bytes(4)                  # (0x55c8f4)
    if bs.read_flag():                    # (0x55c966)
        bs.read_bytes(4)                  # (0x55c9ac)
        bs.read_bytes(4)                  # (0x55c9c4)
        bs.read_bytes(4)                  # (0x55c9dc)
    if bs.read_flag():                    # (0x55ca10)
        bs.read_bytes(4)                  # (0x55ca5a)
        bs.read_bytes(4)                  # (0x55ca72)
        bs.read_bytes(4)                  # (0x55ca8a)
        bs.read_bytes(4)                  # (0x55caa2)
    if bs.read_flag():                    # (0x55cb31)
        bs.read_bytes(4)                  # (0x55cb7f)
        bs.read_bytes(4)                  # (0x55cb97)
        bs.read_bytes(4)                  # (0x55cbb1)


def _unpack_marker(bs: BitStream, is_new: bool) -> None:
    """Marker::unpackUpdate (AoT @ VA 0x551d30): parent (0x485790 = bare ret,
    0 bits) then a single unconditional Box6F(24B) (0x551d4d). No mask."""
    _emit_box6f_position(_read_box6f(bs))  # marker box origin (0x551d4d)


def _unpack_lightning(bs: BitStream, is_new: bool) -> None:
    """Lightning::unpackUpdate (AoT @ VA 0x4b3d80): parent = GameBase::unpackUpdate
    (0x456da0: flag; if set Point3F), then a master flag (@0x4b3d96 -> ret if
    clear). If set (CFG 0x4b3de0..0x4b3f20):
      Point3F(12B); Point3F(12B); 10 x read(4); read(1)byte; read(4)."""
    _unpack_game_base(bs, is_new)     # parent (0x456da0)
    if not bs.read_flag():            # master flag (0x4b3d96)
        return
    _read_point3f(bs)                 # (0x4b3de0)
    _read_point3f(bs)                 # (0x4b3dfc)
    for _ in range(10):
        bs.read_bytes(4)              # (0x4b3e0f..0x4b3ee7)
    bs.read_bytes(1)                  # read(1) byte +0x298 (0x4b3eff)
    bs.read_bytes(4)                  # +0x264 (0x4b3f20)


def _unpack_static_shape(bs: BitStream, is_new: bool) -> None:
    """StaticShape::unpackUpdate (AoT @ VA 0x48df30): ShapeBase parent, then a
    flag (@0x48df6d): if set Box6F(24B) + Point3F(12B); then one more flag
    (@0x48dfe5, the static-shape bool, 1 bit)."""
    _unpack_shape_base(bs, is_new)
    if bs.read_flag():            # (0x48df6d)
        # The Box6F here is the object's mObjBox (a LOCAL bounding box, not a
        # world transform) -- recorded as world_box, NOT position. The authoritative
        # position comes from the GameBase Point3F in the ShapeBase parent above.
        telemetry.emit_point3f("world_box", _read_box6f(bs))  # mObjBox (0x48df86)
        _read_point3f(bs)         # mObjScale (0x48dfa9)
    bs.read_flag()                # (0x48dfe5) static-shape bool


def _unpack_camera(bs: BitStream, is_new: bool) -> None:
    """Camera::unpackUpdate (AoT @ VA 0x44e8b0). CFG-followed 0x44e8b0..0x44ea43:

      ShapeBase parent (0x483d90);
      flag A (@0x44e8ef): if SET -> ``jne 0x44ea3d`` = END (no further reads);
      else flag B (@0x44e932): if set -> 5 x read(4) = 20 bytes
        (0x44e953/0x44e969/0x44e97f/0x44e995/0x44e9ab; a transform pos+rot pair),
        else END.

    The previous transcription read only ONE flag and stopped, missing flag B and
    its 20-byte payload -- which silently dropped/added bits whenever the camera
    sent a transform update (control object in the logged-out lobby), desyncing
    the ghost section right after gid 0."""
    _unpack_shape_base(bs, is_new)
    if bs.read_flag():            # flag A (0x44e8ef): SET -> end
        return
    if bs.read_flag():            # flag B (0x44e932)
        bs.read_bytes(5 * 4)      # 5 x read(4) transform (0x44e953..0x44e9ab)


def _unpack_item(bs: BitStream, is_new: bool) -> None:
    """Item::unpackUpdate (AoT @ VA 0x45e5f0): ShapeBase parent, then a flag
    (@0x45e643): if set -> 3 flags; a rotation while-loop
    (``while readFlag(): readPoint3F()``); a flag; read(4); readString."""
    _unpack_shape_base(bs, is_new)
    if bs.read_flag():            # (0x45e643)
        bs.read_flag()            # (0x45e677) +0xaa6
        bs.read_flag()            # (0x45e6ad) +0xaa5
        bs.read_flag()            # (0x45e6e3) +0xaa4
        # rotation while-loop (0x45e713..0x45e757): read a flag each iteration;
        # if set read a Point3F and repeat; a clear flag ends the loop.
        while bs.read_flag():     # (0x45e73c)
            _read_point3f(bs)     # (0x45e74f)
        bs.read_flag()            # (0x45e772)
        bs.read_bytes(4)          # read(4) (0x45e7b5)
        bs.read_string()          # readString (0x45e7fb)


def _unpack_projectile(bs: BitStream, is_new: bool) -> None:
    """Projectile::unpackUpdate (AoT @ VA 0x476bf0). CFG-followed
    0x476bf0..0x476ed9; each ``flag`` is an inline readFlag.

      GameBase::unpackUpdate (0x456da0: pos mask + datablock mask);
      flag A (@0x476c34): if set ->
          readCompressedPoint                       (initial position, 0x476c51);
          flag B (@0x476d72): if set -> readNormalVector(10), readInt(13)
                                        (velocity direction + magnitude);
          readInt(12)   (getNextPow2(0x1000)=4096 -> 12, @0x476d04);
          flag C (@0x476ddc): if set -> readInt(15)
                                        (getNextPow2(0x4001)=0x8000 -> 15, @0x476e06),
                                        readInt(3) (getNextPow2(8)=8 -> 3, @0x476e24);
      flag D (@0x476e8e): if set -> Point3F(12B), Point3F(12B), read(4)
                                    (position + velocity + a U32, 0x476e9f..0x476ebd).
    """
    _unpack_game_base(bs, is_new)         # parent (0x456da0)
    if bs.read_flag():                    # flag A (0x476c34)
        cp, cp_world = _read_compressed_point(bs)   # initial position (0x476c51)
        if cp is not None and cp_world:
            telemetry.emit("position", cp)
        if bs.read_flag():                # flag B (0x476d72)
            _emit_rotation(_read_normal_vector(bs, 10))  # (0x476d91)
            bs.read_int(13)               # (0x476d9a)
        bs.read_int(12)                   # (0x476d04)
        if bs.read_flag():                # flag C (0x476ddc)
            bs.read_int(15)               # (0x476e06)
            bs.read_int(3)                # (0x476e24)
    if bs.read_flag():                    # flag D (0x476e8e)
        telemetry.emit_point3f("position", _read_point3f(bs))  # (0x476e9f)
        _read_point3f(bs)                 # velocity (0x476eaa)
        bs.read_bytes(4)                  # read(4) U32 (0x476ebd)


def _unpack_precipitation(bs: BitStream, is_new: bool) -> None:
    """Precipitation::unpackUpdate (AoT @ VA 0x4bbf70). CFG-followed
    0x4bbf70..0x4bc143:

      GameBase::unpackUpdate (0x456da0: pos mask + datablock mask);
      flag (@0x4bbfc6): if set ->
          9 x read(4)   (storm params -> +0x4fc..+0x520, 0x4bbfe2..0x4bc0a2);
          3 x flag      (+0x524/+0x525/+0x526, 0x4bc0c6/0x4bc0fc/0x4bc130).

    Precipitation carries no transform of its own (it follows the camera); only
    storm parameters + 3 toggle bits."""
    _unpack_game_base(bs, is_new)     # parent (0x456da0)
    if bs.read_flag():                # (0x4bbfc6)
        for _ in range(9):
            bs.read_bytes(4)          # 9 x read(4) (0x4bbfe2..0x4bc0a2)
        bs.read_flag()                # +0x524 (0x4bc0c6)
        bs.read_flag()                # +0x525 (0x4bc0fc)
        bs.read_flag()                # +0x526 (0x4bc130)


def _unpack_player(bs: BitStream, is_new: bool) -> None:
    """Player::unpackUpdate (AoT @ VA 0x46e690; shared by AIPlayer).

    ShapeBase parent, then a series of mask-gated blocks (CFG-followed
    0x46e690..0x46f136; each ``flag`` is an inline readFlag, ``rflag`` is the
    function readFlag @ 0x421200):

      flag (@0x46e6d8): if set -> read(4) x4;
      flag (@0x46e76d): if set ->
          readFloat(6) x2;
          flag (@0x46e802): if set -> read(4) x7, readInt(7), read(4) x2;
          read(4) x2;                         (unconditional within this block)
      flag (@0x46e987): if set -> readInt(3);
      flag (@0x46e9cb): if set ->
          readInt(8); flag; flag;
          flag (@0x46ea7e): if NOT set -> rflag; if set -> readSignedFloat(6);
          flag (@0x46ec28);
      flag (@0x46ed05): if set -> readInt(8);
      flag (@0x46ed61): if set -> (no further reads, early-out path);
      flag (@0x46eda5): if set ->                      (controlled-player pose)
          rflag; rflag;
          readInt(3);
          rflag (@0x46ee3c): if set -> readInt(7);
          readCompressedPoint;
          rflag (@0x46eea4): if set -> readNormalVector(10), readInt(13);
          readFloat(7); readSignedFloat(6); readSignedFloat(6);
          Move::unpack;
          rflag (@0x46ef88).
    """
    _unpack_shape_base(bs, is_new)

    if bs.read_flag():                 # (0x46e6d8) position/velocity
        for _ in range(4):
            bs.read_bytes(4)

    if bs.read_flag():                 # (0x46e76d)
        bs.read_float(6)               # (0x46e780)
        bs.read_float(6)               # (0x46e78f)
        if bs.read_flag():             # (0x46e802)
            for _ in range(7):
                bs.read_bytes(4)       # (0x46e81c..0x46e8ac)
            bs.read_int(7)             # (0x46e8bd)
            for _ in range(2):
                bs.read_bytes(4)       # (0x46e8d3,0x46e8eb)
        for _ in range(2):
            bs.read_bytes(4)           # (0x46e935,0x46e94d) unconditional

    if bs.read_flag():                 # (0x46e987)
        bs.read_int(3)                 # (0x46e996)

    if bs.read_flag():                 # (0x46e9cb)
        bs.read_int(8)                 # damage state idx (0x46e9de)
        # THREE unconditional flags are read in order, stored to [esp+0x4c],
        # [esp+0x50], bl (0x46ea15, 0x46ea4b, 0x46ea7e). Then a conditional
        # readSignedFloat(6) is gated on the SECOND flag ([esp+0x50]) being
        # CLEAR: 0x46ea87 `test al,al; jne 0x46eaaf` skips the read when it is
        # SET. The trailing instructions (0x46eaaf..0x46ecd2, incl. the
        # 0x46ec28 setne) operate on object fields, NOT the bitstream -- there
        # is NO fourth flag here. (Prior transcription gated on the third flag
        # and read a spurious trailing flag, slipping the cursor.)
        bs.read_flag()                 # f1 [esp+0x4c] (0x46ea15)
        f2 = bs.read_flag()            # f2 [esp+0x50] (0x46ea4b)
        bs.read_flag()                 # f3 bl (0x46ea7e)
        if not f2:                     # 0x46ea87 jne 0x46eaaf when f2 set
            if bs.read_flag():         # rflag (0x46ea99, fn 0x421200)
                bs.read_signed_float(6)  # readSignedFloat(6) (0x4210b0 @0x46eaa6)

    if bs.read_flag():                 # (0x46ed05)
        bs.read_int(8)                 # (0x46ed14)

    if bs.read_flag():                 # (0x46ed61) -- if set, early-out, no reads
        return

    if bs.read_flag():                 # (0x46eda5) controlled-player pose
        bs.read_flag()                 # rflag (0x46edc1)
        bs.read_flag()                 # rflag (0x46edce)
        bs.read_int(3)                 # (0x46ee33)
        if bs.read_flag():             # rflag (0x46ee3c)
            bs.read_int(7)             # (0x46ee49)
        # Player world position (readCompressedPoint @ 0x46ee6c). The CONTROLLED
        # object (the bot's own ghost) packs its pose as a type-3 absolute point;
        # REMOTE players pack it as a type-0/1/2 quantised point relative to the
        # client's control-object position. _read_compressed_point dequantises
        # both to world floats (type 0/1/2 require the compression reference; see
        # its docstring). This block runs for both moving AND parked players (the
        # 0x46ed61 early-out only skips it for the no-pose-change updates), so it
        # is the authoritative position for every Player. cp_world is True once a
        # world point is available (always for type 3; for 0/1/2 once the control
        # reference is known).
        cp, cp_world = _read_compressed_point(bs)  # (0x46ee6c)
        if cp is not None and cp_world:
            telemetry.emit("position", cp)
        if bs.read_flag():             # rflag (0x46eea4): head/look pitch vector
            # readNormalVector(10) here is the player's HEAD/LOOK direction (used
            # for aim pitch), NOT the body yaw -- we keep reading it for alignment
            # but do NOT surface it as the object rotation (it is near-constant for
            # standing NPCs and is not what getTransform reports).
            _read_normal_vector(bs, 10)  # (0x46eeb2)
            bs.read_int(13)            # (0x46eebb)
        # readFloat(7) * 2*PI (const @ 0x5f1d78) = the player's BODY YAW (mRot.z),
        # i.e. the rotation getTransform reports as `0 0 1 angle`. THIS is the
        # authoritative heading; emit it as the rotation. (asm 0x46ef03 readFloat(7)
        # then fmul [0x5f1d78]=6.2831853.)
        yaw = bs.read_float(7) * (2.0 * _math.pi)   # (0x46ef03)
        _emit_yaw(yaw)
        bs.read_signed_float(6)        # (0x46ef16) head x
        bs.read_signed_float(6)        # (0x46ef31) head z
        _read_move(bs)                 # Move::unpack (0x46ef4f)
        bs.read_flag()                 # rflag (0x46ef88)


# name -> unpackUpdate decoder(bs, is_new). Per-class NetObject unpackUpdate
# (vtable slot 0x4c) decoders, CFG-followed from the exe and capture-validated.
# AIPlayer shares Player::unpackUpdate (slot 0x4c == 0x46e690).
DECODERS: dict = {
    "fxShapeReplicator": _unpack_fx_shape_replicator,
    "fxFoliageReplicator": _unpack_fx_foliage_replicator,
    "fxGrassReplicator": _unpack_fx_grass_replicator,
    "WaterBlock": _unpack_water_block,
    "Marker": _unpack_marker,
    "Lightning": _unpack_lightning,
    "HoverVehicle": _unpack_hover_vehicle,
    "Sky": _unpack_sky,
    "ShapeBase": _unpack_shape_base,
    "StaticShape": _unpack_static_shape,
    "Camera": _unpack_camera,
    "Item": _unpack_item,
    "Player": _unpack_player,
    "AIPlayer": _unpack_player,
    "Projectile": _unpack_projectile,
    "Precipitation": _unpack_precipitation,
    "GameBase": _unpack_game_base,
    "Debris": _unpack_game_base,          # no override (slot 0x4c == GameBase)
    "Sun": _unpack_sun,
    "fxSunLight": _unpack_fx_sun_light,   # slot 0x4c 0x4b2470
    "TerrainBlock": _unpack_terrain_block,  # slot 0x4c 0x563bb0
    "MissionMarker": _unpack_mission_marker,
    "RoomMarker": _unpack_mission_marker,  # slot 0x4c 0x4638a0 = jmp 0x463620
    "WayPoint": _unpack_way_point,         # slot 0x4c 0x4636b0 (own override)
    "SpawnSphere": _unpack_spawn_sphere,   # slot 0x4c 0x4637e0 (own override)
    "ScopeAlwaysShape": _unpack_static_shape,  # shared (slot 0x4c == 0x48df30)
    "SimpleNetObject": _unpack_simple_net_object,
    "AudioEmitter": _unpack_audio_emitter,
    "fxBrickBatcher": _unpack_fx_brick_batcher,
    "fxDTSBrick": _unpack_fx_dts_brick,
    "ParticleEmitterNode": _unpack_particle_emitter_node,
    "Trigger": _unpack_trigger,
    "PhysicalZone": _unpack_physical_zone,
    "PathedInterior": _unpack_pathed_interior,
    "TSStatic": _unpack_ts_static,
    "fxShapeReplicatedStatic": _unpack_ts_static,  # shared (slot 0x4c == 0x4917e0)
    "InteriorInstance": _unpack_interior_instance,
    "volumeLight": _unpack_volume_light,
    "MissionArea": _unpack_mission_area,
    # Spawner classes: NPCSpawner/MazeSpawner are a bare jmp to MissionMarker
    # (slot 0x4c == 0x4638a0 -> 0x463620); DestructableSpawner (0x4639e0) and
    # GoldSpawner (0x4638b0) OVERRIDE it with extra trailing fields.
    "NPCSpawner": _unpack_mission_marker,   # 0x4638a0 = jmp 0x463620
    "MazeSpawner": _unpack_mission_marker,  # 0x4638a0 = jmp 0x463620
    "DestructableSpawner": _unpack_destructable_spawner,  # 0x4639e0
    "GoldSpawner": _unpack_gold_spawner,    # 0x4638b0
}


# --------------------------------------------------------------------------- #
# Control/camera-object readPacketData (NetObject vtable slot 0xec).
#
# GameConnection::readPacket dispatches the control object's readPacketData on
# the RESOLVED ghost's actual class (call [edx+0xec] @ 0x459593). ShapeBase
# provides the shared 8-byte base (0x47e210); ShapeBase subclasses (Camera,
# Player, ...) OVERRIDE it with a larger payload. We must call the right one or
# the rest of the packet (ghost section) desyncs.
# --------------------------------------------------------------------------- #


def _read_packet_data_shapebase(bs: BitStream) -> None:
    """ShapeBase::readPacketData (AoT @ VA 0x47e210): GameBase parent (0x485790,
    0 bits) + 2 x read(4) = 8 raw bytes (a control angle F32 + an F32). Shared by
    every ShapeBase subclass that does not override slot 0xec."""
    bs.read_bytes(8)


def _read_packet_data_camera(bs: BitStream) -> None:
    """Camera::readPacketData (AoT @ VA 0x44e680; Camera vtable 0x5f5894 slot
    0xec). CFG-followed 0x44e680..0x44e8a1:

      ShapeBase::readPacketData (0x47e210, 8 bytes);
      Point3F (12 bytes, mathRead 0x421240) -- camera position;
      2 x read(4)                          -- (a rot pair, 0x44e6bb/0x44e6cd);
      readInt(3) mode                      -- getNextPow2(5)->getBinLog2->3 bits;
      if mode in {3,4}: 3 x read(4)        -- (0x44e71b/0x44e733/0x44e74b);
      if mode == 3:     flag, readInt(14)  -- (0x44e78d flag + 0x44e7a9 ghost id);
      if mode == 4:     readCompressedPoint (0x421a70 @0x44e7cf).
    (Everything from 0x44e7d6 is object-field copies -- no further reads.)"""
    _read_packet_data_shapebase(bs)   # 0x47e210 (8 bytes)
    _read_point3f(bs)                 # Point3F (0x421240)
    bs.read_bytes(4)                  # (0x44e6bb)
    bs.read_bytes(4)                  # (0x44e6cd)
    mode = bs.read_int(3)             # (0x44e6f5)
    if mode in (3, 4):
        bs.read_bytes(4)              # (0x44e71b)
        bs.read_bytes(4)              # (0x44e733)
        bs.read_bytes(4)              # (0x44e74b)
        if mode == 3:
            bs.read_flag()            # (0x44e78d) +0xacc
            bs.read_int(14)           # ghost id (0x44e7a9)
        elif mode == 4:
            _read_compressed_point(bs)  # (0x44e7cf)


def _read_packet_data_player(bs: BitStream) -> None:
    """Player::readPacketData (AoT @ VA 0x4699d0; Player vtable slot 0xec).

    Dispatched by GameConnection::readPacket's control-object branch once the
    client's own Player is the scoped control object (i.e. the bot has SPAWNED
    and is being controlled). CFG-followed 0x4699d0..0x469cdc; each ``flag`` is an
    inline readFlag, ``read(4)`` is a raw 4-byte F32 (bitstream vtable slot 4):

      ShapeBase::readPacketData (0x47e210, 8 bytes);
      readInt(3)                                  pose/state (@0x4699eb -> +0xbb0);
      flag (@0x469a23): if set -> readInt(7)      (@0x469a32 -> +0xc24);
      flag (@0x469b66): if set -> readInt(7)      (@0x469b79 -> +0xbb8);
      flag (@0x469ba8):
          if set -> 6 x read(4) (pos Point3F + rot Point3F, +0xb30..+0xb50),
                    readInt(4)  (@0x469c69 -> +0xbcc);
          (both set and clear paths converge on:)
          3 x read(4)           (a velocity/secondary transform, +0xb30..+0xb38
                                 via 0x469a8c/0x469aa4/0x469abc; 0x4690b0 = no read);
      flag (@0x469c9a): if set -> readInt(14) mounted ghost id (@0x469cac), then
                        that object's readPacketData (call [edx+0xec] @0x469cd2).

    NOTE: the trailing mounted-object dispatch resolves a ghost id we cannot map
    to a class from here, so we default it to the shared ShapeBase 8-byte
    readPacketData (the common case -- a player mounted on a simple ShapeBase).
    If a Player is ever found mounted on a Camera/Player the mount block would
    need that object's class; not observed in the captures.
    """
    _read_packet_data_shapebase(bs)   # 0x47e210 (8 bytes)
    bs.read_int(3)                    # pose/state (0x4699eb)
    if bs.read_flag():                # (0x469a23)
        bs.read_int(7)                # (0x469a32)
    if bs.read_flag():                # (0x469b66)
        bs.read_int(7)                # (0x469b79)
    if bs.read_flag():                # (0x469ba8) transform-present
        pos = _read_point3f(bs)       # position (0x469bc2/0x469bd8/0x469bee)
        telemetry.emit_point3f("position", pos)
        _read_point3f(bs)             # rotation pair (0x469c04/0x469c1c/0x469c34)
        bs.read_int(4)                # (0x469c69)
    # Common transform tail (0x469a81): 3 x read(4) (0x469a8c/0x469aa4/0x469abc).
    _read_point3f(bs)
    if bs.read_flag():                # mount flag (0x469c9a)
        telemetry.emit("mount", bs.read_int(14))  # mounted ghost id (0x469cac)
        # Mounted object's readPacketData (call [edx+0xec]); default to the
        # ShapeBase 8-byte base (mounted-on class unknown here).
        _read_packet_data_shapebase(bs)


# class name -> control-object readPacketData (slot 0xec) decoder.
PACKET_DATA_DECODERS: dict = {
    "Camera": _read_packet_data_camera,
    "Player": _read_packet_data_player,
    "AIPlayer": _read_packet_data_player,  # shares slot 0xec == 0x4699d0
}


def read_packet_data(bs: BitStream, class_id: int | None) -> None:
    """Dispatch a control/camera object's ``readPacketData`` (vtable slot 0xec).

    ``class_id`` is the resolved ghost's object classId (or None if unknown, e.g.
    the ghost was never scoped to us). Classes that override readPacketData are
    in :data:`PACKET_DATA_DECODERS`; everything else uses the shared ShapeBase
    8-byte base. An unknown class falls back to the ShapeBase base (the common
    case for a control object whose ghost has not yet been seen in our section).
    """
    if class_id is None:
        # Unknown control object class: default to the shared ShapeBase 8-byte
        # readPacketData. This is the correct base for ShapeBase and every
        # subclass that does not override slot 0xec; only Camera/Player (tracked
        # via _ghost_classes once scoped) need the larger override.
        _read_packet_data_shapebase(bs)
        return
    name = (
        OBJECT_CLASS_NAMES[class_id]
        if 0 <= class_id < len(OBJECT_CLASS_NAMES)
        else f"<{class_id}>"
    )
    dec = PACKET_DATA_DECODERS.get(name)
    if dec is not None:
        dec(bs)
        return
    # Default: the shared ShapeBase 8-byte readPacketData. This is correct for
    # ShapeBase and any subclass that does NOT override slot 0xec. Subclasses
    # that DO override (Camera, Player, ...) must be in PACKET_DATA_DECODERS.
    _read_packet_data_shapebase(bs)


def unpack_update(bs: BitStream, class_id: int, is_new: bool) -> None:
    """Dispatch to the per-class NetObject ``unpackUpdate`` decoder.

    Raises :class:`GhostDecodeError` (with the class name) if we have no decoder
    for ``class_id`` -- the bitstream cannot stay aligned past an un-decoded
    ghost (no length prefix).
    """
    name = (
        OBJECT_CLASS_NAMES[class_id]
        if 0 <= class_id < len(OBJECT_CLASS_NAMES)
        else f"<{class_id}>"
    )
    dec = DECODERS.get(name)
    if dec is None:
        raise GhostDecodeError(class_id, name)
    dec(bs, is_new)
