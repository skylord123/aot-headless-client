set pagination off
set width 0
set confirm off
set height 0

handle SIGSEGV nostop noprint pass

# --- readCompressedPoint @ 0x421a70: type 0/1/2 (compressed) branch entry ---
# esi = BitStream this; [esi+0x28/0x2c/0x30] = compression reference floats.
# scale arg at [esp+0x14] (after prologue pushes, before the ebx push at 0x421add).
break *0x421a89
commands
  silent
  printf "RCP type=%d scale=%f ref=(%f,%f,%f) esi=%#x\n", (*(int*)($esp+8))&3, *(float*)($esp+0x14), *(float*)($esi+0x28), *(float*)($esi+0x2c), *(float*)($esi+0x30), $esi
  cont
end

# After dequant, just before pop edi (0x421b42): edi = out point ptr (3 floats).
break *0x421b42
commands
  silent
  printf "RCP result world=(%f,%f,%f)\n", *(float*)($edi), *(float*)($edi+4), *(float*)($edi+8)
  cont
end

# type-3 absolute branch result (0x421ad7 before pop edi). edi=out ptr.
break *0x421ad7
commands
  silent
  printf "RCP3 abs=(%f,%f,%f)\n", *(float*)($edi), *(float*)($edi+4), *(float*)($edi+8)
  cont
end

printf "cpoint breakpoints armed.\n"
cont
