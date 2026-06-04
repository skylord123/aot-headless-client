"""Bit-packed reader/writer matching Torque Game Engine's ``BitStream``.

This is a faithful Python reimplementation of TGE's
``engine/core/bitStream.{cc,h}``. Torque networking does **not** byte-align
its data: integers, flags, strings, etc. are packed at the bit level, and the
bit ordering must match the engine byte-for-byte or the whole protocol falls
apart downstream.

Bit-ordering rules (from ``bitStream.cc`` / ``bitStream.h``)
-----------------------------------------------------------
* Bits within a byte are filled **LSB-first**. ``writeFlag`` /
  ``readFlag`` use ``mask = 1 << (bitNum & 0x7)`` and index the byte with
  ``bitNum >> 3`` (see ``BitStream::writeFlag`` line 234 and the inline
  ``BitStream::readFlag`` in ``bitStream.h`` line 241). So the first flag
  written lands in bit 0 (value ``0x01``) of byte 0.
* Multi-bit integers are written **little-endian** at the byte level and
  then LSB-first within the bit window. ``writeInt`` (line 304) does
  ``val = convertHostToLEndian(val); writeBits(bitCount, &val)`` and
  ``readInt`` (line 292) does ``readBits(...); ret = convertLEndianToHost(ret);
  ret &= (1 << bitCount) - 1``. Because the engine targets little-endian x86,
  ``convertHostToLEndian`` is a no-op there, so the wire format is simply the
  low ``bitCount`` bits of the value emitted LSB-first.
* ``getPosition()`` returns a **byte** count rounded up:
  ``(bitNum + 7) >> 3`` (line 137). ``setPosition(pos)`` sets
  ``bitNum = pos << 3`` (line 143). The raw bit cursor is ``getCurPos`` /
  ``setCurPos`` (``bitStream.h`` lines 230/235).

Endianness note
---------------
TGE's ``convertHostToLEndian`` / ``convertLEndianToHost`` are identity on the
little-endian platforms AoT ships for. We therefore treat the value's low
``bitCount`` bits as a little-endian bit field. This module is written for
little-endian wire semantics only (which is what the engine produces).
"""

from __future__ import annotations

from typing import Optional


# Mirrors TGE's MaxPacketDataSize default packet buffer. Not a hard limit here;
# our writer grows dynamically, but it documents the engine's expectation.
MAX_PACKET_DATA_SIZE = 1500


def _next_pow2(value: int) -> int:
    """Smallest power of two >= ``value`` (matches TGE ``getNextPow2``).

    TGE's ``getNextPow2(0)`` returns 1; we keep that behaviour.
    """
    if value <= 1:
        return 1
    n = 1
    while n < value:
        n <<= 1
    return n


def _bin_log2(value: int) -> int:
    """Integer log2 of a power of two (matches TGE ``getBinLog2``)."""
    bits = 0
    v = value
    while v > 1:
        v >>= 1
        bits += 1
    return bits


class BitStream:
    """LSB-first, little-endian bit packer/unpacker à la TGE ``BitStream``.

    A single instance can both read and write. Writing appends at the current
    bit cursor and grows the backing buffer as needed; reading consumes from the
    current bit cursor. Use :meth:`set_byte_position` / :meth:`set_bit_position`
    (or the TGE-named aliases) to seek.

    The backing store is a Python ``bytearray``. Unlike the C++ engine — which
    operates on a fixed external buffer — we resize on demand so callers don't
    have to pre-size packets.
    """

    def __init__(self, data: Optional[bytes] = None):
        """Create a stream.

        :param data: if provided, initialise the buffer for reading; the bit
            cursor starts at 0. If ``None``, start with an empty buffer for
            writing.
        """
        if data is None:
            self._buf = bytearray()
        else:
            self._buf = bytearray(data)
        self._bit_num = 0
        self.error = False
        # TGE keeps a per-stream string buffer used for the writeString /
        # readString dedup-prefix path. None disables that path (plain Huffman).
        self._string_buffer: Optional[bytearray] = None

    # ------------------------------------------------------------------ #
    # Buffer / cursor management
    # ------------------------------------------------------------------ #

    def _ensure_byte(self, byte_index: int) -> None:
        """Grow the backing buffer so ``byte_index`` is addressable."""
        if byte_index >= len(self._buf):
            self._buf.extend(b"\x00" * (byte_index + 1 - len(self._buf)))

    def get_bit_position(self) -> int:
        """Raw bit cursor (TGE ``getCurPos``)."""
        return self._bit_num

    def set_bit_position(self, bit_pos: int) -> None:
        """Set the raw bit cursor (TGE ``setCurPos``)."""
        self._bit_num = int(bit_pos)

    # TGE-named aliases.
    getCurPos = get_bit_position
    setCurPos = set_bit_position

    def get_byte_position(self) -> int:
        """Byte length consumed, rounded up (TGE ``getPosition``):
        ``(bitNum + 7) >> 3``.
        """
        return (self._bit_num + 7) >> 3

    def set_byte_position(self, byte_pos: int) -> None:
        """Seek to ``byte_pos`` bytes (TGE ``setPosition``): ``bitNum = pos << 3``."""
        self._bit_num = int(byte_pos) << 3

    # TGE-named aliases.
    getPosition = get_byte_position
    setPosition = set_byte_position

    getBytePosition = get_byte_position

    def get_bytes(self) -> bytes:
        """Return the populated portion of the buffer (rounded up to a byte).

        This is what you'd hand to ``sendto`` — equivalent to
        ``getBuffer()[0:getPosition()]`` in the engine.
        """
        return bytes(self._buf[: self.get_byte_position()])

    def get_buffer(self) -> bytes:
        """Return the entire backing buffer (TGE ``getBuffer``)."""
        return bytes(self._buf)

    def clear(self) -> None:
        """Reset cursor and zero the buffer (TGE ``clear`` + rewind)."""
        self._buf = bytearray()
        self._bit_num = 0
        self.error = False

    def hexdump(self) -> str:
        """Hex string of the populated bytes, for debugging packet contents."""
        return self.get_bytes().hex()

    def is_valid(self) -> bool:
        """False once an out-of-range read/write set the error flag."""
        return not self.error

    # ------------------------------------------------------------------ #
    # Core bit-level primitives
    # ------------------------------------------------------------------ #

    def write_flag(self, val: bool) -> bool:
        """Write a single bit (TGE ``writeFlag``, bitStream.cc:234).

        Sets bit ``1 << (bitNum & 0x7)`` of byte ``bitNum >> 3``; returns the
        value written (the engine uses the return as a convenience predicate).
        """
        byte_index = self._bit_num >> 3
        self._ensure_byte(byte_index)
        if val:
            self._buf[byte_index] |= 1 << (self._bit_num & 0x7)
        else:
            self._buf[byte_index] &= ~(1 << (self._bit_num & 0x7)) & 0xFF
        self._bit_num += 1
        return bool(val)

    def read_flag(self) -> bool:
        """Read a single bit (TGE ``readFlag``, bitStream.h:241)."""
        if (self._bit_num >> 3) >= len(self._buf):
            self.error = True
            self._bit_num += 1
            return False
        mask = 1 << (self._bit_num & 0x7)
        ret = (self._buf[self._bit_num >> 3] & mask) != 0
        self._bit_num += 1
        return ret

    # TGE-named aliases.
    writeFlag = write_flag
    readFlag = read_flag

    def write_bits(self, bit_count: int, data: bytes) -> None:
        """Write ``bit_count`` bits from ``data`` (TGE ``writeBits``, line 186).

        ``data`` is a little-endian byte sequence; bits are consumed LSB-first
        from ``data[0]`` upward. The engine's clever shift-merge loop produces
        exactly the same layout as writing the bits one at a time LSB-first, so
        we implement it the straightforward (and verifiably equivalent) way.
        """
        if not bit_count:
            return
        for i in range(bit_count):
            byte = data[i >> 3] if (i >> 3) < len(data) else 0
            bit = (byte >> (i & 0x7)) & 1
            self.write_flag(bool(bit))

    def read_bits(self, bit_count: int) -> bytes:
        """Read ``bit_count`` bits into a little-endian byte sequence
        (TGE ``readBits``, line 250). Bits fill the output LSB-first.
        """
        out = bytearray((bit_count + 7) >> 3)
        for i in range(bit_count):
            if self.read_flag():
                out[i >> 3] |= 1 << (i & 0x7)
        return bytes(out)

    # TGE-named aliases.
    writeBits = write_bits
    readBits = read_bits

    # ------------------------------------------------------------------ #
    # Integers
    # ------------------------------------------------------------------ #

    def write_int(self, value: int, bit_count: int) -> None:
        """Write the low ``bit_count`` bits of ``value`` (TGE ``writeInt``).

        Engine: ``val = convertHostToLEndian(val); writeBits(bitCount, &val)``.
        On little-endian that's just the low ``bit_count`` bits, LSB-first.
        """
        if bit_count <= 0:
            return
        masked = value & ((1 << bit_count) - 1) if bit_count < 32 else value & 0xFFFFFFFF
        data = masked.to_bytes(4, "little")
        self.write_bits(bit_count, data)

    def read_int(self, bit_count: int) -> int:
        """Read ``bit_count`` bits as an unsigned int (TGE ``readInt``).

        Engine masks to ``(1 << bitCount) - 1`` (no mask for 32). Result is the
        unsigned interpretation of the bit field.
        """
        if bit_count <= 0:
            return 0
        raw = self.read_bits(bit_count)
        # raw is LSB-first little-endian; reassemble.
        val = int.from_bytes(raw, "little")
        if bit_count < 32:
            val &= (1 << bit_count) - 1
        return val & 0xFFFFFFFF

    # TGE-named aliases.
    writeInt = write_int
    readInt = read_int

    def write_signed_int(self, value: int, bit_count: int) -> None:
        """Sign-flag + magnitude (TGE ``writeSignedInt``, line 330)."""
        if self.write_flag(value < 0):
            self.write_int(-value, bit_count - 1)
        else:
            self.write_int(value, bit_count - 1)

    def read_signed_int(self, bit_count: int) -> int:
        """Read a sign-flag + magnitude int (TGE ``readSignedInt``, line 338)."""
        if self.read_flag():
            return -self.read_int(bit_count - 1)
        return self.read_int(bit_count - 1)

    writeSignedInt = write_signed_int
    readSignedInt = read_signed_int

    def write_ranged_u32(self, value: int, range_start: int, range_end: int) -> None:
        """Write a value in ``[range_start, range_end]`` using the minimal
        number of bits (TGE ``writeRangedU32``, bitStream.h:255).
        """
        range_size = range_end - range_start + 1
        range_bits = _bin_log2(_next_pow2(range_size))
        self.write_int(value - range_start, range_bits)

    def read_ranged_u32(self, range_start: int, range_end: int) -> int:
        """Read a ranged value (TGE ``readRangedU32``, bitStream.h:266)."""
        range_size = range_end - range_start + 1
        range_bits = _bin_log2(_next_pow2(range_size))
        return self.read_int(range_bits) + range_start

    writeRangedU32 = write_ranged_u32
    readRangedU32 = read_ranged_u32

    # ------------------------------------------------------------------ #
    # Floats (0..1 unsigned, -1..1 signed) — TGE lines 310-328
    # ------------------------------------------------------------------ #

    def write_float(self, f: float, bit_count: int) -> None:
        """Write a 0..1 float quantised to ``bit_count`` bits."""
        self.write_int(int(f * ((1 << bit_count) - 1)), bit_count)

    def read_float(self, bit_count: int) -> float:
        """Read a 0..1 float of ``bit_count`` bits."""
        return self.read_int(bit_count) / float((1 << bit_count) - 1)

    def write_signed_float(self, f: float, bit_count: int) -> None:
        """Write a -1..1 float quantised to ``bit_count`` bits."""
        self.write_int(int(((f + 1) * 0.5) * ((1 << bit_count) - 1)), bit_count)

    def read_signed_float(self, bit_count: int) -> float:
        """Read a -1..1 float of ``bit_count`` bits."""
        return self.read_int(bit_count) * 2 / float((1 << bit_count) - 1) - 1.0

    writeFloat = write_float
    readFloat = read_float
    writeSignedFloat = write_signed_float
    readSignedFloat = read_signed_float

    # ------------------------------------------------------------------ #
    # Byte-aligned helpers
    # ------------------------------------------------------------------ #

    def align_byte(self) -> None:
        """Advance the bit cursor to the next byte boundary (no-op if aligned).

        Useful for reads where the protocol resumes on a byte boundary.
        """
        if self._bit_num & 0x7:
            self._bit_num = (self._bit_num + 7) & ~0x7

    def write_bytes(self, data: bytes) -> None:
        """Write raw bytes 8 bits at a time (each byte LSB-first), matching
        ``Stream::write(size, ptr)`` which calls ``writeBits(size << 3, ptr)``.
        """
        self.write_bits(len(data) * 8, data)

    def read_bytes(self, count: int) -> bytes:
        """Read ``count`` raw bytes (each byte LSB-first), matching
        ``Stream::read`` -> ``readBits(size << 3, ptr)``.
        """
        return self.read_bits(count * 8)

    def write_u8(self, value: int) -> None:
        self.write_int(value & 0xFF, 8)

    def read_u8(self) -> int:
        return self.read_int(8)

    # ------------------------------------------------------------------ #
    # Strings
    # ------------------------------------------------------------------ #

    def set_string_buffer(self, buf: Optional[bytearray]) -> None:
        """Install the per-stream string buffer used by the dedup-prefix path
        in ``writeString`` / ``readString`` (TGE ``setStringBuffer``).

        Pass a fresh ``bytearray(256)`` to enable the prefix-compression path
        (matching strings only emit the differing suffix); pass ``None`` to use
        the plain Huffman path. For most AoT traffic the buffer is left unset.
        """
        self._string_buffer = buf

    def write_string(self, string, max_len: int = 255) -> None:
        """Write a C-string using TGE's Huffman codec (TGE ``writeString``,
        line 594) with the optional dedup-prefix path.

        Accepts ``str`` (encoded latin-1, matching the engine's byte-oriented
        ``char*``) or ``bytes``.
        """
        if string is None:
            data = b""
        elif isinstance(string, str):
            data = string.encode("latin-1", errors="replace")
        else:
            data = bytes(string)

        if self._string_buffer is not None:
            sb = self._string_buffer
            # Length of the common prefix with the previously written string.
            j = 0
            while j < max_len and j < len(sb) and j < len(data) and sb[j] == data[j] and data[j] != 0:
                j += 1
            # Update the string buffer (dStrncpy + NUL-terminate at max_len).
            new_sb = bytearray(256)
            copy_len = min(len(data), max_len)
            new_sb[:copy_len] = data[:copy_len]
            self._string_buffer = new_sb
            if self.write_flag(j > 2):
                self.write_int(j, 8)
                _huffman.write_buffer(self, data[j:], max_len - j)
                return
        _huffman.write_buffer(self, data, max_len)

    def read_string(self) -> str:
        """Read a C-string written by :meth:`write_string` (TGE ``readString``,
        line 577). Returns a ``str`` (decoded latin-1).
        """
        if self._string_buffer is not None:
            if self.read_flag():
                offset = self.read_int(8)
                tail = _huffman.read_buffer(self)
                merged = bytearray(self._string_buffer[:offset]) + tail
                self._string_buffer = bytearray(256)
                copy_len = min(len(merged), 256)
                self._string_buffer[:copy_len] = merged[:copy_len]
                return bytes(merged).decode("latin-1")
            data = _huffman.read_buffer(self)
            self._string_buffer = bytearray(256)
            copy_len = min(len(data), 256)
            self._string_buffer[:copy_len] = data[:copy_len]
            return data.decode("latin-1")
        return _huffman.read_buffer(self).decode("latin-1")

    writeString = write_string
    readString = read_string


# ---------------------------------------------------------------------- #
# Huffman codec (TGE HuffmanProcessor, bitStream.cc:72-1058)
# ---------------------------------------------------------------------- #

# Character frequency table copied verbatim from bitStream.cc (csm_charFreqs).
# The tree is built deterministically from these counts, so any peer using the
# same table produces identical codes.
_CHAR_FREQS = [
    0, 0, 0, 0, 0, 0, 0, 0, 0, 329, 21, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    2809, 68, 0, 27, 0, 58, 3, 62, 4, 7, 0, 0, 15, 65, 554, 3,
    394, 404, 189, 117, 30, 51, 27, 15, 34, 32, 80, 1, 142, 3, 142, 39,
    0, 144, 125, 44, 122, 275, 70, 135, 61, 127, 8, 12, 113, 246, 122, 36,
    185, 1, 149, 309, 335, 12, 11, 14, 54, 151, 0, 0, 2, 0, 0, 211,
    0, 2090, 344, 736, 993, 2872, 701, 605, 646, 1552, 328, 305, 1240, 735, 1533, 1713,
    562, 3, 1775, 1149, 1469, 979, 407, 553, 59, 279, 31, 0, 0, 0, 68, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
]


class _HuffmanProcessor:
    """Builds TGE's Huffman tree and (de)serialises strings exactly as
    ``HuffmanProcessor`` does in bitStream.cc.

    Tree-build algorithm (``buildTables``, line 616): start with 256 leaves
    whose population is ``charFreq + 1``. Repeatedly merge the two lowest-pop
    wraps into a node (``index0`` = lowest, ``index1`` = second-lowest), using
    the engine's exact tie-breaking and array-compaction order so the resulting
    codes match bit-for-bit. ``generateCodes`` (line 692) then assigns a code to
    each leaf: descending to ``index0`` writes a ``0`` bit, ``index1`` writes a
    ``1`` bit, LSB-first into a code word.
    """

    def __init__(self):
        self._built = False
        # Leaf code/len arrays, indexed by symbol 0..255.
        self._code = [0] * 256       # code bits, LSB-first
        self._numbits = [0] * 256
        # Node arrays used for decoding: index0/index1 per node.
        self._node_index0: list[int] = []
        self._node_index1: list[int] = []
        self._leaf_symbol = [i for i in range(256)]

    def _build(self) -> None:
        # Leaves: pop = freq + 1.
        leaf_pop = [_CHAR_FREQS[i] + 1 for i in range(256)]

        # Node storage. Node 0 is reserved (the engine reserves index 0 and
        # overwrites it with the final root after merging).
        node_pop: list[int] = [0]
        node_index0: list[int] = [0]
        node_index1: list[int] = [0]

        # A "wrap" references either a leaf (negative index encoding) or a node
        # (non-negative index). We mirror determineIndex():
        #   leaf i  -> -(i + 1)
        #   node i  ->  i
        # Each wrap entry: (is_leaf, idx, pop)
        wraps: list[tuple[bool, int, int]] = [
            (True, i, leaf_pop[i]) for i in range(256)
        ]

        curr = 256
        while curr != 1:
            min1 = 0xFFFFFFFE
            min2 = 0xFFFFFFFF
            index1 = -1
            index2 = -1
            for i in range(curr):
                pop = wraps[i][2]
                if pop < min1:
                    min2 = min1
                    index2 = index1
                    min1 = pop
                    index1 = i
                elif pop < min2:
                    min2 = pop
                    index2 = i

            # Create a node merging index1 (->index0) and index2 (->index1).
            w1 = wraps[index1]
            w2 = wraps[index2]
            ei0 = -(w1[1] + 1) if w1[0] else w1[1]
            ei1 = -(w2[1] + 1) if w2[0] else w2[1]
            new_node_idx = len(node_pop)
            node_pop.append(w1[2] + w2[2])
            node_index0.append(ei0)
            node_index1.append(ei1)

            merge_index = index2 if index1 > index2 else index1
            nuke_index = index1 if index1 > index2 else index2
            wraps[merge_index] = (False, new_node_idx, node_pop[new_node_idx])

            if index2 != (curr - 1):
                wraps[nuke_index] = wraps[curr - 1]
            curr -= 1

        # The surviving wrap is the root node; copy it into node 0.
        root = wraps[0]
        assert not root[0], "root wrap should be a node"
        root_idx = root[1]
        node_pop[0] = node_pop[root_idx]
        node_index0[0] = node_index0[root_idx]
        node_index1[0] = node_index1[root_idx]

        self._node_index0 = node_index0
        self._node_index1 = node_index1

        # generateCodes: walk from node 0, LSB-first code accumulation.
        # We do it iteratively with an explicit stack carrying (index, depth,
        # code) where code is the bits accumulated so far (bit `depth-1` is the
        # most-recently-added). The engine writes the branch bit at position
        # `depth` (the current cursor) via writeFlag, i.e. LSB-first.
        stack = [(0, 0, 0)]
        while stack:
            index, depth, code = stack.pop()
            if index < 0:
                leaf = -(index + 1)
                self._code[leaf] = code
                self._numbits[leaf] = depth
            else:
                # index0 -> writeFlag(false): bit `depth` stays 0.
                stack.append((node_index0[index], depth + 1, code))
                # index1 -> writeFlag(true): set bit `depth`.
                stack.append((node_index1[index], depth + 1, code | (1 << depth)))

        self._built = True

    def write_buffer(self, bs: "BitStream", data: bytes, max_len: int) -> None:
        """Mirror ``HuffmanProcessor::writeHuffBuffer`` (line 762)."""
        if data is None:
            bs.write_flag(False)
            bs.write_int(0, 8)
            return
        if not self._built:
            self._build()

        length = len(data)
        if length > max_len:
            length = max_len
        if length > 255:
            length = 255

        num_bits = 0
        for i in range(length):
            num_bits += self._numbits[data[i]]

        if num_bits >= (length * 8):
            # Uncompressed path.
            bs.write_flag(False)
            bs.write_int(length, 8)
            bs.write_bytes(bytes(data[:length]))
        else:
            bs.write_flag(True)
            bs.write_int(length, 8)
            for i in range(length):
                sym = data[i]
                bits = self._numbits[sym]
                code = self._code[sym]
                bs.write_bits(bits, code.to_bytes(4, "little"))

    def read_buffer(self, bs: "BitStream") -> bytes:
        """Mirror ``HuffmanProcessor::readHuffBuffer`` (line 729)."""
        if not self._built:
            self._build()

        if bs.read_flag():
            length = bs.read_int(8)
            out = bytearray()
            for _ in range(length):
                index = 0
                while True:
                    if index >= 0:
                        if bs.read_flag():
                            index = self._node_index1[index]
                        else:
                            index = self._node_index0[index]
                    else:
                        out.append(self._leaf_symbol[-(index + 1)])
                        break
            return bytes(out)
        else:
            length = bs.read_int(8)
            return bytes(bs.read_bytes(length))


# Module-global processor, matching TGE's single static g_huffProcessor.
_huffman = _HuffmanProcessor()
