"""Tests for aotbot.crc.get_string_crc (AoT getStringCRC reproduction).

The algorithm was confirmed by disassembling AgeOfTime.exe.original to be the
standard CRC-32 (poly 0xEDB88320, init 0xFFFFFFFF, reflected, final XOR
0xFFFFFFFF) -- identical to zlib.crc32. The vectors below are therefore
MATHEMATICALLY CERTAIN to be what the engine computes for these inputs.

NOTE on live confirmation: the only remaining (tiny) risk is the *encoding* of
non-ASCII password chars and whether the in-game console trims/normalizes the
string before hashing. For ASCII passwords (the overwhelming common case) the
result is certain. To pin it down for real, run in the AoT console:
    echo(getStringCRC("password"));
and confirm it prints 901924565 (see KNOWN_LIVE vector below).
"""

import zlib

import pytest

from aotbot.crc import get_string_crc, calculate_crc, INITIAL_CRC_VALUE


# --- Mathematically certain vectors (== zlib.crc32) -------------------------
# get_string_crc(s) must equal zlib.crc32(s) for all ASCII s, because both are
# the standard finalized CRC-32. These do NOT need live-game confirmation.
CERTAIN_VECTORS = [
    ("", 0x00000000),
    ("a", 0xE8B7BE43),
    ("abc", 0x352441C2),
    ("123456", 0x0972D361),
    ("password", 0x35C246D5),  # decimal 901924565
    ("Hello, World!", 0xEC4AC3D0),
]


@pytest.mark.parametrize("text,expected", CERTAIN_VECTORS)
def test_known_vectors(text, expected):
    assert get_string_crc(text) == expected


@pytest.mark.parametrize("text,_expected", CERTAIN_VECTORS)
def test_matches_zlib_crc32(text, _expected):
    # The whole point: AoT getStringCRC == standard finalized CRC-32 == zlib.
    assert get_string_crc(text) == zlib.crc32(text.encode("latin-1"))


def test_result_is_unsigned_32bit():
    for text, _ in CERTAIN_VECTORS:
        v = get_string_crc(text)
        assert 0 <= v <= 0xFFFFFFFF


def test_calculate_crc_is_not_inverted():
    # The raw engine calculateCRC (no final invert) is the complement of the
    # finalized value. Sanity-check the relationship the EXE wrapper encodes:
    #   getStringCRC = ~calculateCRC = 0xFFFFFFFF - calculateCRC
    s = "password"
    raw = calculate_crc(s.encode("latin-1"), INITIAL_CRC_VALUE)
    assert get_string_crc(s) == (raw ^ 0xFFFFFFFF) & 0xFFFFFFFF


# --- Live-game confirmation hook --------------------------------------------
# Mark which vectors should be re-confirmed against the running game console
# before trusting login end-to-end. These are EXPECTED to pass already (they
# are the certain vectors), but having them here makes it a one-liner to paste
# a real console value and prove byte-for-byte parity.
#
# To confirm: in the AoT TorqueScript console run e.g.
#     echo(getStringCRC("password"));   // expect 901924565
# and (if it differs) record the (input, value) pair here.
KNOWN_LIVE = [
    # (password_string, decimal_value_printed_by_in_game_getStringCRC)
    # ("password", 901924565),   # <-- uncomment after confirming in-game
]


@pytest.mark.skipif(not KNOWN_LIVE, reason="no live-game getStringCRC values recorded yet")
@pytest.mark.parametrize("text,expected_decimal", KNOWN_LIVE)
def test_live_game_parity(text, expected_decimal):
    # TorqueScript echo() of a U32 may print as signed or unsigned; accept both.
    got = get_string_crc(text)
    signed = got - (1 << 32) if got >= (1 << 31) else got
    assert expected_decimal in (got, signed)
