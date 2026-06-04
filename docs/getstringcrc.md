# getStringCRC — confirmed algorithm

`getStringCRC(s)` is the TorqueScript console function AoT uses to hash the
login password before sending it:

```ts
commandToServer('login', %user, getStringCRC(%pass));
```

The hash must match the server byte-for-byte, so it had to be confirmed from
the binary, not assumed from the (slightly newer) TGE 1.4 source.

## Result

```
getStringCRC(s) == ~calculateCRC(s, strlen(s), 0xFFFFFFFF)
               == standard finalized CRC-32 (zlib / PKZIP / Ethernet)
```

- Polynomial (reflected): **0xEDB88320**
- Initial value: **0xFFFFFFFF**
- Reflect in / reflect out: **yes** (reflected/LSB-first table implementation)
- **Final XOR: 0xFFFFFFFF** (the console wrapper one's-complements the result)
- Output: unsigned 32-bit

This is bit-for-bit identical to Python's `zlib.crc32(s.encode())`. Implemented
in `aotbot/crc.py` as `get_string_crc()`.

Important subtlety: the *engine* function `calculateCRC` does **not** final-
invert (it returns the running `crcVal`). The **`getStringCRC` console wrapper**
adds the final `~crc`. So the value visible in TorqueScript / on the wire is the
fully-finalized CRC-32. Earlier guesses that "TGE CRC doesn't invert" are true
of `calculateCRC` but **wrong for `getStringCRC`**.

## Methodology

Static disassembly of `AgeOfTime.exe.original` (32-bit PE, image base
0x400000, `.text` flat-mapped so `fileoff == VA - 0x400000`) with
capstone + pefile.

1. Found the `"getStringCRC"` name string at fileoff 0x1F0548 / VA 0x5F0548.
2. Found its single xref at VA 0x5E256E — a console-function registration that
   pushes the callback pointer **0x4158F0**.
3. Disassembled the callback @ **VA 0x4158F0**:

   ```asm
   0x4158f0 push esi
   0x4158f1 mov  esi, [esp+0x10]   ; argv
   0x4158f5 mov  eax, [esi+4]      ; argv[2] = password string
   0x4158f8 push -1                ; (also serves as crcVal init below)
   0x4158fa push eax
   0x4158fb call 0x52dcc0          ; dStrlen -> eax = len
   0x415900 mov  ecx, [esi+4]
   0x415906 push eax               ; len
   0x415907 push ecx               ; str   (3rd/crcVal arg = the earlier -1)
   0x415908 call 0x4226a0          ; calculateCRC(str, len, 0xFFFFFFFF)
   0x41590d or   edx, 0xffffffff   ; edx = 0xFFFFFFFF
   0x415913 sub  edx, eax          ; edx = 0xFFFFFFFF - crc == ~crc
   0x415915 mov  eax, edx          ; <-- FINAL INVERT, returned
   0x415918 ret
   ```

4. Confirmed `0x52dcc0` is `dStrlen` (counts bytes to NUL).
5. Confirmed `0x4226a0` is TGE `calculateCRC`:
   inner loop `crc = table[(crc ^ buf[i]) & 0xff] ^ (crc >> 8)`, table @
   0x65EF00, lazily built by 0x422600. Matches stock TGE `engine/core/crc.cc`
   lines 38–49.
6. Confirmed table generator @ **VA 0x422600** uses the reflected polynomial
   **0xEDB88320** (`if(val&1) val = 0xEDB88320 ^ (val>>1) else val >>= 1`),
   matching TGE `crc.cc:15-33`.
7. The init value 0xFFFFFFFF comes from the `push -1` reused as the 3rd arg to
   `calculateCRC` (TGE `INITIAL_CRC_VALUE`, `crc.h:9`).

## What is certain vs. needs live confirmation

- **Certain (math + EXE):** for any ASCII password, `get_string_crc` equals the
  in-game value. Algorithm, polynomial, init, and final-invert are all
  confirmed from the binary.
- **Needs live confirmation (minor):** the encoding of non-ASCII bytes and
  whether the console trims/normalizes the string before hashing. We encode as
  latin-1 (1 char = 1 byte) to match the engine's byte-wise hashing of the raw
  C string. To pin it down, run in the AoT console:

  ```ts
  echo(getStringCRC("password"));   // expect 901924565
  ```

  and drop the pair into `KNOWN_LIVE` in `tests/test_crc.py` (note: TS `echo`
  of a U32 may print signed; the test accepts either signed or unsigned).
