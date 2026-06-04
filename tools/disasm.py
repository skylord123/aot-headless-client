#!/usr/bin/env python3
"""Linear disassembly of a VA range in AgeOfTime.exe.original (base 0x400000).
Annotates known helper call targets. Usage: disasm.py 0x46e690 0x46f140"""
import sys
import capstone
import pefile

EXE = "/home/skylar/Projects/ageoftime-bot/AgeOfTime/AgeOfTime.exe.original"
BASE = 0x400000

HELPERS = {
    0x420e20: "writeFlag",
    0x420e80: "readBits",
    0x420f60: "readInt",
    0x421000: "readFloat",
    0x4210b0: "readSignedFloat_inner",
    0x421200: "readFlag_fn",
    0x421240: "readPoint3F(12B)",
    0x421510: "readClassId",
    0x421570: "readSignedInt",
    0x4216f0: "readNormalVector",
    0x421800: "readBox6F(193b)",
    0x421a70: "readCompressedPoint",
    0x424230: "readString",
    0x4244e0: "getNextPow2",
    0x424510: "getBinLog2",
    0x4243f0: "ColorF::read(4B)",
    0x45b000: "Move::unpack",
    0x456da0: "GameBase::unpackUpdate",
    0x483d90: "ShapeBase::unpackUpdate",
    0x465750: "readMatrix(64B)",
    0x4656d0: "PlaneF::read(16B)",
    0x47e210: "ShapeBase::readPacketData",
}


def main():
    start = int(sys.argv[1], 16)
    end = int(sys.argv[2], 16)
    pe = pefile.PE(EXE, fast_load=True)
    data = pe.get_memory_mapped_image(ImageBase=BASE)
    code = data[start - BASE:end - BASE]
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    md.detail = True
    for ins in md.disasm(code, start):
        annot = ""
        if ins.mnemonic == "call":
            op = ins.op_str
            if op.startswith("0x"):
                t = int(op, 16)
                if t in HELPERS:
                    annot = f"  ; -> {HELPERS[t]}"
                else:
                    annot = f"  ; -> sub_{t:x}"
        elif ins.mnemonic.startswith("j"):
            annot = "  ; JUMP"
        print(f"0x{ins.address:06x}: {ins.mnemonic:<7} {ins.op_str}{annot}")


if __name__ == "__main__":
    main()
