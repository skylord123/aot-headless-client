"""Live-entity telemetry: scoped-object registry + a decode-time value sink.

When object tracking is ON (``AOT_TRACK_OBJECTS``), the ghost-section /
GhostAlwaysObjectEvent / control-object decoders (ghosts.py) and the datablock
decoders (datablocks.py) record the *values* they read -- in addition to merely
consuming the bits to stay aligned -- so the bot can answer "what objects are
scoped to me and where are they?".

Two pieces live here:

* :class:`DecodeSink` -- a tiny per-``unpackUpdate`` / per-``unpackData`` value
  collector. The decoders, while ON, push named fields (``position``,
  ``rotation``, ``datablock_id``, ``mount``, ``shape_file`` ...) into the active
  sink via :func:`emit`. ``emit`` is a no-op when no sink is installed (tracking
  OFF) so it costs nothing on the hot path. Crucially the decoders still read
  the SAME bits either way -- the sink only observes values, never changes the
  cursor -- so bit-exactness (the capture-replay regression) is preserved.

* :class:`ObjectRegistry` -- ``ghostId -> ObjectRecord`` plus a datablock-id ->
  shapeFile map, populated from the initial GhostAlwaysObjectEvent, the ongoing
  ghost-section updates, and the control object. Resolves each ghost's shape
  name via its datablock id. Tracks which ghost is the bot's own control object,
  and handles ghost removal.

Position / transform semantics (per TGE shapeBase.cc / player.cc, VAs verified in
``AgeOfTime.exe.original``):

* ShapeBase-derived objects (Player, AIPlayer, Item, StaticShape, Camera, ...):
  ``GameBase::unpackUpdate`` (VA 0x456da0) reads a present flag then, if set, a
  Point3F it hands to ``setTransform`` (``call [vtbl+0x74]`` @ 0x456dfb) -- this
  is the object's world POSITION. We label it ``position``.
* Player/AIPlayer controlled-pose block (VA 0x46ee6c) carries the controlled
  player's position as a ``readCompressedPoint`` -- labelled ``position`` too
  (the controlled object, e.g. the bot's own Player, reports here).
* StaticShape / Item / Trigger / Marker etc. carry a worldBox (Box6F center) +
  scale (Point3F); we record the Box6F center as ``position`` when no GameBase
  Point3F was present.
* Rotation is the ``readNormalVector``-encoded orientation (ShapeBase @ 0x483eb0,
  Player @ 0x46eeb2) and/or the controlled-pose head/body angles. We surface the
  decoded normal vector as ``rotation`` when present.

* Shape name = the object's datablock's ``shapeFile`` (ShapeBaseData's first
  readString, VA 0x47cad0): captured in datablocks.py and joined here.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("aotbot.telemetry")


# --------------------------------------------------------------------------- #
# Decode-time value sink (thread-local so concurrent decodes never cross).
# --------------------------------------------------------------------------- #


class DecodeSink:
    """Collects named field values read during ONE unpackUpdate / unpackData.

    The decoders call :func:`emit` as they read transform/datablock/shape fields;
    those calls land here when this sink is the active one. ``fields`` keeps the
    first value seen for a given key (the engine reads outer/position fields
    before inner ones, and a single record only needs one position/rotation).
    """

    __slots__ = ("fields",)

    def __init__(self) -> None:
        self.fields: Dict[str, Any] = {}

    def set(self, key: str, value: Any) -> None:
        # Keep the first meaningful value for position/rotation (outermost =
        # the object's own world transform); later keys (datablock_id, shape_file,
        # mount) are always recorded/overwritten with the latest.
        if key in ("position", "rotation", "scale") and key in self.fields:
            return
        self.fields[key] = value


_local = threading.local()


def active_sink() -> Optional[DecodeSink]:
    return getattr(_local, "sink", None)


def set_sink(sink: Optional[DecodeSink]) -> None:
    _local.sink = sink


def set_compression_point(point: Optional[Tuple[float, float, float]]) -> None:
    """Install the connection's point-compression REFERENCE (the engine's
    ``BitStream`` member at ``[this+0x28..0x30]``).

    ``BitStream::readCompressedPoint`` (VA 0x421a70) dequantises types 0/1/2 as
    ``component = readSignedInt(bits[type]) * scale + reference[component]``
    (winedbg-confirmed; see :func:`aotbot.ghosts._read_compressed_point`). The
    reference is the receiving client's CONTROL-OBJECT world position -- the
    server packs every other object's pose as a small signed delta from the
    client's own player position, then the client adds its control pos back.
    A headless bot recovers the same reference from its control object's
    decoded (type-3 absolute) pose. Thread-local like the sink."""
    _local.compression_point = point


def compression_point() -> Optional[Tuple[float, float, float]]:
    return getattr(_local, "compression_point", None)


def set_string_resolver(resolver) -> None:
    """Install a ``slot:int -> Optional[str]`` resolver for the connection's
    *receive* string table (the tags the server taught us via NetStringEvent).

    The ShapeBase ``unpackUpdate`` skin/name tagged-string block (VA 0x484732 ->
    ConnectionStringTable read 0x546fc0) packs a player's NAME either as a literal
    string or as a 5-bit slot id into this table -- ``getShapeName`` resolves the
    same way (mShapeNameTag via the connection's NetStringTable @ +0x1ac). The
    ghost decoders use this resolver to turn a slot id into the real name. Set
    once per connection (phases), thread-local like the sink."""
    _local.string_resolver = resolver


def resolve_string(slot: int) -> Optional[str]:
    r = getattr(_local, "string_resolver", None)
    if r is None:
        return None
    try:
        return r(slot)
    except Exception:  # pragma: no cover - defensive
        return None


def emit(key: str, value: Any) -> None:
    """Record a decoded field value into the active sink (no-op if tracking OFF).

    Called from the bit-exact decoders. Does NOT touch the bitstream.
    """
    sink = getattr(_local, "sink", None)
    if sink is not None:
        sink.set(key, value)


def emit_point3f(key: str, raw: bytes) -> None:
    """Record a Point3F (12 raw little-endian F32 bytes) as an (x,y,z) tuple."""
    sink = getattr(_local, "sink", None)
    if sink is None:
        return
    if raw is not None and len(raw) >= 12:
        try:
            x, y, z = struct.unpack_from("<fff", raw, 0)
            sink.set(key, (x, y, z))
        except struct.error:  # pragma: no cover - defensive
            pass


# --------------------------------------------------------------------------- #
# Object registry
# --------------------------------------------------------------------------- #


@dataclass
class ObjectRecord:
    """A scoped game object's tracked telemetry."""

    ghost_id: int
    class_name: str = ""
    datablock_id: Optional[int] = None
    # ``name`` = the object's in-game NAME (ShapeBase mShapeNameTag, what
    # getShapeName returns -- for a Player this is the username, e.g. "Jeff
    # Bezos"). DISTINCT from ``shape_file`` (the datablock's .dts model file, e.g.
    # horse.dts). ``shape_name`` is kept as a back-compat alias that prefers the
    # name and falls back to the shape file.
    name: Optional[str] = None
    shape_file: Optional[str] = None
    shape_name: Optional[str] = None
    position: Optional[Tuple[float, float, float]] = None
    rotation: Optional[Any] = None
    scale: Optional[Tuple[float, float, float]] = None
    mount: Optional[int] = None
    is_control_object: bool = False
    scoped: bool = True
    last_update: float = field(default_factory=time.monotonic)

    def to_dict(self) -> Dict[str, Any]:
        pos = (
            [round(c, 4) for c in self.position]
            if self.position is not None
            else None
        )
        rot = self.rotation
        if isinstance(rot, (tuple, list)):
            rot = [round(c, 6) for c in rot]
        scale = (
            [round(c, 4) for c in self.scale]
            if self.scale is not None
            else None
        )
        # shape_name = the human-facing label: the in-game NAME if known (player
        # username), else the datablock model file (.dts).
        shape_name = self.name or self.shape_file or self.shape_name
        return {
            "ghost_id": self.ghost_id,
            "class_name": self.class_name,
            "datablock_id": self.datablock_id,
            "name": self.name,
            "shape_file": self.shape_file,
            "shape_name": shape_name,
            "position": pos,
            "rotation": rot,
            "scale": scale,
            "mount": self.mount,
            "is_control_object": self.is_control_object,
            "scoped": self.scoped,
            "age": round(time.monotonic() - self.last_update, 3),
        }


class ObjectRegistry:
    """``ghostId -> ObjectRecord`` + a datablock-id -> shapeFile resolver.

    Populated by the phases/events layer from the GhostAlwaysObjectEvent (initial
    state), the ongoing ghost section, and the control object. Only maintained
    when tracking is ON.
    """

    def __init__(self) -> None:
        self._objects: Dict[int, ObjectRecord] = {}
        # datablock id -> {"class": str, "shape_file": str, "name": str|None}
        self._datablocks: Dict[int, Dict[str, Any]] = {}
        self._control_ghost_id: Optional[int] = None

    # -- datablock side ---------------------------------------------------- #

    def record_datablock(
        self,
        db_id: int,
        class_name: str,
        shape_file: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        rec = self._datablocks.setdefault(db_id, {})
        rec["class"] = class_name
        if shape_file:
            rec["shape_file"] = shape_file
        if name:
            rec["name"] = name
        # Back-fill any already-scoped objects pointing at this datablock.
        for obj in self._objects.values():
            if obj.datablock_id == db_id and shape_file and not obj.shape_file:
                obj.shape_file = shape_file
                if not obj.shape_name:
                    obj.shape_name = shape_file

    def shape_for_datablock(self, db_id: Optional[int]) -> Optional[str]:
        if db_id is None:
            return None
        rec = self._datablocks.get(db_id)
        if rec is None:
            return None
        return rec.get("shape_file") or rec.get("name")

    # -- object side ------------------------------------------------------- #

    def update_from_sink(
        self,
        ghost_id: int,
        class_name: str,
        sink: DecodeSink,
        *,
        is_new: bool = False,
        is_control: bool = False,
    ) -> ObjectRecord:
        rec = self._objects.get(ghost_id)
        if rec is None:
            rec = ObjectRecord(ghost_id=ghost_id)
            self._objects[ghost_id] = rec
        if class_name:
            rec.class_name = class_name
        rec.scoped = True
        rec.last_update = time.monotonic()

        f = sink.fields
        if "datablock_id" in f and f["datablock_id"] is not None:
            rec.datablock_id = f["datablock_id"]
            shape = self.shape_for_datablock(rec.datablock_id)
            if shape:
                rec.shape_file = shape
                rec.shape_name = shape
        # Player/AIPlayer in-game NAME (ShapeBase mShapeNameTag tagged string).
        if f.get("name"):
            rec.name = f["name"]
        # TSStatic / InteriorInstance carry their model/file string directly in
        # unpackUpdate (emitted as "shape_file"); the datablock-less classes thus
        # still get a shape file.
        if f.get("shape_file"):
            rec.shape_file = f["shape_file"]
            if not rec.shape_name:
                rec.shape_name = f["shape_file"]
        if f.get("position") is not None:
            rec.position = f["position"]
        elif rec.position is None and f.get("world_box") is not None:
            # Many marker/spawner/light classes (MissionMarker family,
            # volumeLight, fxSunLight, WaterBlock, fx*Replicator) carry no
            # GameBase/controlled-pose Point3F in their unpackUpdate; their only
            # transform on the wire is the leading Point3F of the Box6F field
            # (mObjBox/area box, read via _read_box6f @ VA 0x421800). In the AoT
            # captures that leading point is the object's WORLD origin (the box
            # max reads ~0), so when no other position is present we surface the
            # Box6F point as the object's position. (StaticShape/Item already
            # carry their world transform via the ShapeBase GameBase point when
            # present; the Box6F fallback only fills the otherwise-"?" classes.)
            rec.position = f["world_box"]
        if f.get("rotation") is not None:
            rec.rotation = f["rotation"]
        if f.get("scale") is not None:
            rec.scale = f["scale"]
        if f.get("mount") is not None:
            rec.mount = f["mount"]
        if is_control:
            self.set_control_object(ghost_id)
        return rec

    def set_control_object(self, ghost_id: int) -> None:
        if self._control_ghost_id is not None and self._control_ghost_id != ghost_id:
            old = self._objects.get(self._control_ghost_id)
            if old is not None:
                old.is_control_object = False
        self._control_ghost_id = ghost_id
        rec = self._objects.get(ghost_id)
        if rec is not None:
            rec.is_control_object = True

    def remove(self, ghost_id: int) -> None:
        rec = self._objects.get(ghost_id)
        if rec is not None:
            rec.scoped = False
            rec.last_update = time.monotonic()
        if self._control_ghost_id == ghost_id:
            self._control_ghost_id = None

    # -- query ------------------------------------------------------------- #

    @property
    def control_ghost_id(self) -> Optional[int]:
        return self._control_ghost_id

    def get(self, ghost_id: int) -> Optional[ObjectRecord]:
        return self._objects.get(ghost_id)

    def list_objects(self, include_removed: bool = False) -> list[Dict[str, Any]]:
        return [
            rec.to_dict()
            for rec in self._objects.values()
            if include_removed or rec.scoped
        ]

    def clear(self) -> None:
        self._objects.clear()
        self._datablocks.clear()
        self._control_ghost_id = None
