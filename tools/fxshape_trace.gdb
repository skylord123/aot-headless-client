# gdb script (driven via `winedbg --gdb`) to instrument
# fxShapeReplicator::unpackUpdate (VA 0x4aef80) in AgeOfTime.exe.
#
# BitStream `this` is held in ESI for the whole function; the bit cursor
# (curPos) lives at [esi+0xc] (confirmed from readBits @ 0x420e80). We log the
# cursor at the entry and at every field-read call site, so the per-field bit
# deltas reveal exactly which field width diverges from the Python decoder.
#
# Image base is 0x400000 (wine maps AgeOfTime.exe at its preferred base), so the
# static VAs map directly to process addresses.

set pagination off
set width 0
set confirm off

# --- helper: print cursor at a labelled site, then continue ---
# (each breakpoint gets a commands block that prints *(int*)(esi+0xc))

# entry
break *0x4aef80
commands
  silent
  printf "FXSR ENTRY      esi=%#x curPos=%d\n", $esi, *(int*)($esi+0xc)
  cont
end

# after parent (master-flag region begins). cursor BEFORE master flag.
break *0x4aefaa
commands
  silent
  printf "  pre-masterflag curPos=%d\n", *(int*)($esi+0xc)
  cont
end

# Box6F call
break *0x4aefdd
commands
  silent
  printf "  @box6f         curPos=%d\n", *(int*)($esi+0xc)
  cont
end
# readInt #0 (+0x268)
break *0x4aefe6
commands
  silent
  printf "  @int32a#0      curPos=%d\n", *(int*)($esi+0xc)
  cont
end
# readInt #1 (+0x270)
break *0x4aeff5
commands
  silent
  printf "  @int32a#1      curPos=%d\n", *(int*)($esi+0xc)
  cont
end
# readInt #2 (+0x274)
break *0x4af004
commands
  silent
  printf "  @int32a#2      curPos=%d\n", *(int*)($esi+0xc)
  cont
end
# readString (+0x26c)
break *0x4af013
commands
  silent
  printf "  @readString    curPos=%d\n", *(int*)($esi+0xc)
  cont
end
# readInt after string (+0x2ac)
break *0x4af022
commands
  silent
  printf "  @int32b#0(post-str) curPos=%d\n", *(int*)($esi+0xc)
  cont
end
break *0x4af031
commands
  silent
  printf "  @int32b#1      curPos=%d\n", *(int*)($esi+0xc)
  cont
end
break *0x4af040
commands
  silent
  printf "  @int32b#2      curPos=%d\n", *(int*)($esi+0xc)
  cont
end
break *0x4af04f
commands
  silent
  printf "  @int32b#3      curPos=%d\n", *(int*)($esi+0xc)
  cont
end
# Point3F #0 (+0x27c)
break *0x4af062
commands
  silent
  printf "  @point3f#0     curPos=%d\n", *(int*)($esi+0xc)
  cont
end
break *0x4af06f
commands
  silent
  printf "  @point3f#1     curPos=%d\n", *(int*)($esi+0xc)
  cont
end
break *0x4af07c
commands
  silent
  printf "  @point3f#2     curPos=%d\n", *(int*)($esi+0xc)
  cont
end
break *0x4af089
commands
  silent
  printf "  @point3f#3     curPos=%d\n", *(int*)($esi+0xc)
  cont
end
# readSignedInt a (+0x2bc)
break *0x4af095
commands
  silent
  printf "  @sint32_a      curPos=%d\n", *(int*)($esi+0xc)
  cont
end
# readSignedInt b (+0x2c4)
break *0x4af1a8
commands
  silent
  printf "  @sint32_b      curPos=%d\n", *(int*)($esi+0xc)
  cont
end
# Point3F last (+0x2cc)
break *0x4af1ef
commands
  silent
  printf "  @point3f_last  curPos=%d\n", *(int*)($esi+0xc)
  cont
end
# readInt tail (+0x2dc)
break *0x4af299
commands
  silent
  printf "  @int32_tail    curPos=%d\n", *(int*)($esi+0xc)
  cont
end
# ColorF (+0x2e0)
break *0x4af2ad
commands
  silent
  printf "  @colorf        curPos=%d\n", *(int*)($esi+0xc)
  cont
end
# function tail / final flag site (+0x278). After final flag, ret.
break *0x4af2e2
commands
  silent
  printf "  @final_flag    curPos=%d\n", *(int*)($esi+0xc)
  cont
end
# ret sites (function returns)
break *0x4aefa7
commands
  silent
  printf "  RET(overflow)  curPos=%d\n", *(int*)($esi+0xc)
  cont
end

printf "fxShapeReplicator breakpoints armed.\n"
cont
