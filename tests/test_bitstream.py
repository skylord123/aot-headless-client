"""Round-trip and exact-byte-vector tests for the TGE-compatible BitStream.

The exact-byte vectors lock down bit ordering: TGE fills bits LSB-first within
each byte and lays multi-bit integers out little-endian (see bitstream.py module
docstring citing bitStream.cc:186/234/304).
"""

import random

import pytest

from aotbot.bitstream import BitStream


# --------------------------------------------------------------------------- #
# Exact byte vectors (hand-computed against TGE semantics)
# --------------------------------------------------------------------------- #


def test_single_flag_true_is_lsb():
    bs = BitStream()
    bs.write_flag(True)
    # First bit -> bit 0 of byte 0 -> 0x01. getPosition rounds up to 1 byte.
    assert bs.get_bytes() == b"\x01"
    assert bs.get_byte_position() == 1
    assert bs.get_bit_position() == 1


def test_single_flag_false():
    bs = BitStream()
    bs.write_flag(False)
    assert bs.get_bytes() == b"\x00"
    assert bs.get_bit_position() == 1


def test_flag_then_int3():
    # flag(True) -> bit0 = 1 (0x01)
    # writeInt(5, 3): 5 = 0b101, LSB-first into bits 1,2,3 -> bit1=1,bit2=0,bit3=1
    #   => 0x02 | 0x08 = 0x0A.  Total byte0 = 0x0B.
    bs = BitStream()
    bs.write_flag(True)
    bs.write_int(5, 3)
    assert bs.get_bytes() == b"\x0b"
    assert bs.get_bit_position() == 4


def test_int16_little_endian_aligned():
    bs = BitStream()
    bs.write_int(0xABCD, 16)
    # LE byte layout: CD AB.
    assert bs.get_bytes() == b"\xcd\xab"


def test_int8_value():
    bs = BitStream()
    bs.write_int(0x3C, 8)
    assert bs.get_bytes() == b"\x3c"


def test_three_flags_pack_into_one_byte():
    bs = BitStream()
    bs.write_flag(True)   # bit0
    bs.write_flag(False)  # bit1
    bs.write_flag(True)   # bit2
    # 0x01 | 0x04 = 0x05
    assert bs.get_bytes() == b"\x05"
    assert bs.get_bit_position() == 3


def test_write_bytes_aligned():
    bs = BitStream()
    bs.write_bytes(b"\xde\xad\xbe\xef")
    assert bs.get_bytes() == b"\xde\xad\xbe\xef"


def test_int_crossing_byte_boundary():
    # Write a 4-bit value then a 12-bit value; ensure spanning is correct.
    bs = BitStream()
    bs.write_int(0xA, 4)       # bits 0..3 = 0b1010 LSB-first -> 0x0A in low nibble
    bs.write_int(0x123, 12)    # bits 4..15
    data = bs.get_bytes()
    # Recompute by hand: byte0 low nibble = 0xA. 0x123 = 0b0001_0010_0011 -> LSB-first
    # bits: starting at bit4. value LE = 0x23 0x01.
    # byte0 high nibble = low 4 bits of 0x123 = 0x3 -> byte0 = 0x3A
    # next 8 bits (bits 8..15) = 0x12 -> byte1 = 0x12
    assert data == b"\x3a\x12"


# --------------------------------------------------------------------------- #
# Round-trip tests
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bits", [1, 2, 3, 4, 5, 7, 8, 9, 15, 16, 17, 24, 31, 32])
def test_int_roundtrip_widths(bits):
    rng = random.Random(bits)
    for _ in range(50):
        maxv = (1 << bits) - 1
        v = rng.randint(0, maxv)
        bs = BitStream()
        bs.write_int(v, bits)
        bs.set_byte_position(0)
        assert bs.read_int(bits) == v


def test_signed_int_roundtrip():
    bs = BitStream()
    values = [0, 1, -1, 5, -5, 100, -100, 32000, -32000]
    for v in values:
        bs.write_signed_int(v, 20)
    bs.set_byte_position(0)
    for v in values:
        assert bs.read_signed_int(20) == v


def test_flag_sequence_roundtrip():
    rng = random.Random(1234)
    flags = [rng.random() < 0.5 for _ in range(200)]
    bs = BitStream()
    for f in flags:
        bs.write_flag(f)
    bs.set_byte_position(0)
    for f in flags:
        assert bs.read_flag() == f


def test_ranged_u32_roundtrip():
    bs = BitStream()
    cases = [(5, 0, 10), (300, 256, 512), (1000, 1000, 2000), (0, 0, 0)]
    for v, lo, hi in cases:
        bs.write_ranged_u32(v, lo, hi)
    bs.set_byte_position(0)
    for v, lo, hi in cases:
        assert bs.read_ranged_u32(lo, hi) == v


def test_mixed_sequence_roundtrip():
    bs = BitStream()
    bs.write_flag(True)
    bs.write_int(42, 6)
    bs.write_flag(False)
    bs.write_int(0xDEADBEEF, 32)
    bs.write_int(7, 3)
    bs.write_string("hello world")
    bs.write_int(0x55, 8)

    bs.set_byte_position(0)
    assert bs.read_flag() is True
    assert bs.read_int(6) == 42
    assert bs.read_flag() is False
    assert bs.read_int(32) == 0xDEADBEEF
    assert bs.read_int(3) == 7
    assert bs.read_string() == "hello world"
    assert bs.read_int(8) == 0x55


# --------------------------------------------------------------------------- #
# String / Huffman tests
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "s",
    [
        "",
        "a",
        "hello",
        "Character does not exist!",
        "MissionStartPhase2Ack",
        "The quick brown fox jumps over the lazy dog.",
        "login skylord123",
        "<Name> logged in.",
        'Bob says, "hi there"',
    ],
)
def test_string_roundtrip(s):
    bs = BitStream()
    bs.write_string(s)
    bs.set_byte_position(0)
    assert bs.read_string() == s


def test_string_roundtrip_with_string_buffer_dedup():
    # Enable the dedup-prefix path on both sides and write similar strings.
    wbs = BitStream()
    wbs.set_string_buffer(bytearray(256))
    strings = ["PlayerName", "PlayerNamf", "PlayerXYZ", "PlayerName"]
    for s in strings:
        wbs.write_string(s)

    rbs = BitStream(wbs.get_bytes())
    rbs.set_string_buffer(bytearray(256))
    for s in strings:
        assert rbs.read_string() == s


def test_string_with_binary_chars():
    # Latin-1 round-trips arbitrary bytes 0..255 (except NUL which terminates).
    s = "".join(chr(c) for c in range(1, 256))
    bs = BitStream()
    bs.write_string(s)
    bs.set_byte_position(0)
    assert bs.read_string() == s


def test_huffman_compresses_lowercase_text():
    # Common lowercase English should compress below the uncompressed size.
    s = "the quick brown fox jumps over the lazy dog"
    bs = BitStream()
    bs.write_string(s)
    # Uncompressed would be flag(1) + 8 bits len + len*8 bits.
    uncompressed_bits = 1 + 8 + len(s) * 8
    assert bs.get_bit_position() < uncompressed_bits


# --------------------------------------------------------------------------- #
# Position / helper tests
# --------------------------------------------------------------------------- #


def test_get_byte_position_rounds_up():
    bs = BitStream()
    bs.write_flag(True)
    assert bs.get_bit_position() == 1
    assert bs.get_byte_position() == 1  # (1 + 7) >> 3


def test_align_byte():
    bs = BitStream()
    bs.write_flag(True)
    bs.write_int(0xFF, 8)  # now at bit 9
    bs.set_byte_position(0)
    bs.read_flag()
    bs.align_byte()
    assert bs.get_bit_position() == 8


def test_float_roundtrip_approx():
    bs = BitStream()
    bs.write_float(0.5, 16)
    bs.set_byte_position(0)
    assert abs(bs.read_float(16) - 0.5) < 1e-3


def test_signed_float_roundtrip_approx():
    bs = BitStream()
    for v in (-1.0, -0.5, 0.0, 0.5, 1.0):
        b2 = BitStream()
        b2.write_signed_float(v, 16)
        b2.set_byte_position(0)
        assert abs(b2.read_signed_float(16) - v) < 1e-3


def test_hexdump():
    bs = BitStream()
    bs.write_int(0xAB, 8)
    bs.write_int(0xCD, 8)
    assert bs.hexdump() == "abcd"
