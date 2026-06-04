"""Age of Time getStringCRC reproduction.

AoT's TorqueScript console function ``getStringCRC(s)`` is used to hash the
login password before it is sent over the wire::

    commandToServer('login', %user, getStringCRC(%pass));

The hash MUST match the server byte-for-byte or login fails, so this module
reproduces the EXACT algorithm the shipped client uses.

ALGORITHM (confirmed by disassembling AgeOfTime.exe.original, see below):
    getStringCRC(s) == ~calculateCRC(s, strlen(s), 0xFFFFFFFF)

which is the *standard* CRC-32 (the same one used by zlib / PKZIP / Ethernet):
    polynomial (reflected) = 0xEDB88320
    initial value          = 0xFFFFFFFF
    reflect in / out       = yes (this is a reflected/LSB-first table impl)
    final XOR              = 0xFFFFFFFF   (the one's-complement at the end)
    result                 = unsigned 32-bit

Crucially, AoT's *getStringCRC console wrapper* DOES final-invert. The engine's
underlying ``calculateCRC`` does NOT (it just returns the running crcVal), but
the console function wraps it with ``~crc``. So the value seen in TorqueScript /
on the wire is the fully-finalized standard CRC-32.

-------------------------------------------------------------------------------
EXE EVIDENCE (AgeOfTime.exe.original, image base 0x400000, .text flat-mapped so
fileoff == VA - 0x400000 for code):

getStringCRC console callback @ VA 0x4158F0 (registered at VA 0x5E2560 which
pushes fn ptr 0x4158F0 alongside the "getStringCRC" name string @ VA 0x5F0548):

    0x4158f0 push esi
    0x4158f1 mov  esi, [esp+0x10]       ; argv
    0x4158f5 mov  eax, [esi+4]          ; argv[2] = the string (password)
    0x4158f8 push -1                    ; strlen's 2nd arg / sentinel
    0x4158fa push eax
    0x4158fb call 0x52dcc0              ; dStrlen(str)  -> eax = len
    0x415900 mov  ecx, [esi+4]          ; str again
    0x415903 add  esp, 4
    0x415906 push eax                   ; push len
    0x415907 push ecx                   ; push str    (crcVal defaults to -1)
    0x415908 call 0x4226a0              ; calculateCRC(str, len, 0xFFFFFFFF)
    0x41590d or   edx, 0xffffffff       ; edx = 0xFFFFFFFF
    0x415910 add  esp, 0xc
    0x415913 sub  edx, eax              ; edx = 0xFFFFFFFF - crc  ==  ~crc
    0x415915 mov  eax, edx              ; return ~crc   <-- FINAL INVERT
    0x415917 pop  esi
    0x415918 ret

calculateCRC @ VA 0x4226A0 (matches stock TGE engine/core/crc.cc:38-49):
    inner loop:  crc = table[(crc ^ buf[i]) & 0xff] ^ (crc >> 8)
    table @ 0x65EF00, lazily built by 0x422600.

CRC table generator @ VA 0x422600 (matches stock TGE crc.cc:15-33):
    val = i; for 8 bits: if(val & 1) val = 0xEDB88320 ^ (val>>1) else val >>= 1
    => reflected polynomial 0xEDB88320.

The initial crcVal of 0xFFFFFFFF comes from the ``push -1`` at 0x4158F8/0x415906
(INITIAL_CRC_VALUE in stock TGE engine/core/crc.h:9 is likewise 0xffffffff).

This is mathematically identical to Python's ``zlib.crc32(s.encode())``.
-------------------------------------------------------------------------------
"""

from __future__ import annotations

# Reflected CRC-32 polynomial (confirmed at EXE VA 0x422600 and TGE crc.cc:25).
CRC_POLYNOMIAL = 0xEDB88320
INITIAL_CRC_VALUE = 0xFFFFFFFF  # EXE: push -1 ; TGE crc.h:9
FINAL_XOR = 0xFFFFFFFF          # EXE: 0xFFFFFFFF - crc (one's complement)


def _build_table() -> list[int]:
    table = []
    for i in range(256):
        val = i
        for _ in range(8):
            if val & 1:
                val = CRC_POLYNOMIAL ^ (val >> 1)
            else:
                val >>= 1
        table.append(val & 0xFFFFFFFF)
    return table


CRC_TABLE = _build_table()


def calculate_crc(data: bytes, crc_val: int = INITIAL_CRC_VALUE) -> int:
    """Reproduce TGE/AoT ``calculateCRC`` (NO final inversion).

    This is the raw running CRC used by the engine internally. Note the engine
    treats bytes via ``char`` (signed) but ``& 0xff`` masks that out, so plain
    unsigned bytes are correct.
    """
    crc = crc_val & 0xFFFFFFFF
    for b in data:
        crc = CRC_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc & 0xFFFFFFFF


def get_string_crc(s: str) -> int:
    """Reproduce AoT's ``getStringCRC`` console function exactly.

    Returns an unsigned 32-bit standard CRC-32 of the UTF-8/latin1 bytes of
    ``s`` (final-inverted). Pass the password string here and send the result
    as the second arg to ``commandToServer('login', user, <this>)``.

    The engine hashes the raw C string bytes up to the NUL terminator. AoT
    strings are 8-bit; we encode as latin-1 so each char maps to one byte
    (matching the engine's byte-wise hashing). ASCII passwords are unaffected.
    """
    data = s.encode("latin-1", errors="replace")
    crc = calculate_crc(data, INITIAL_CRC_VALUE)
    return (crc ^ FINAL_XOR) & 0xFFFFFFFF


if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    print(get_string_crc(arg))
