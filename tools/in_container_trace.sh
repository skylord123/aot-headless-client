#!/bin/bash
# Runs INSIDE the aot-wine docker image. Installs gdb, starts a headless X
# server, then launches AgeOfTime.exe under `winedbg --gdb` with the
# fxShapeReplicator trace script. The game connects through the host UDP relay
# (ServerIP set to 127.0.0.1 in prefs, container uses --network host).
#
# Output: the gdb printf trace goes to /trace/fxshape_trace.log (a bind mount).
set -x
export WINEPREFIX=/root/prefix32
export WINEARCH=win64
export HOME=/root
export DISPLAY=:0
export WINEDEBUG=-all

# 1. gdb (the winedbg --gdb proxy needs a real gdb).
apt-get update >/dev/null 2>&1
apt-get install -y gdb >/dev/null 2>&1

# 2. headless X.
Xvfb :0 -screen 0 1024x768x24 >/tmp/xvfb.log 2>&1 &
sleep 3
fluxbox >/tmp/fluxbox.log 2>&1 &
sleep 1

cd "/root/prefix32/drive_c/Program Files (x86)/AgeOfTime"

# 3. gdb command file: winedbg --gdb spawns gdb and feeds it our script via -x.
#    We point winedbg at gdb with WINEDBG gdb args through the env trick: winedbg
#    --gdb honors the `WINE_GDB` style? Instead we use the documented two-step:
#    winedbg --gdb --no-start prints a `target remote :PORT` line; we connect a
#    plain gdb with our script. Simplest reliable path: use `winedbg --gdb` and
#    have gdb auto-source the script via the GDB init.
cp /trace/fxshape_trace.gdb /root/fxshape_trace.gdb

# winedbg --gdb launches gdb; we tell that gdb to source our script + log to file
# via a small ~/.gdbinit-like wrapper passed through GDB_SCRIPT.
cat > /root/gdb_wrapper.txt <<'EOF'
set logging file /trace/fxshape_trace.log
set logging overwrite on
set logging on
handle SIGSEGV nostop noprint pass
source /root/fxshape_trace.gdb
EOF

# winedbg --gdb accepts extra gdb args after the program? It launches its own
# gdb. We use the `--gdb` "proxy" then pipe commands. Use expect-free approach:
# launch winedbg --gdb with stdin = our command stream.
( printf 'source /root/gdb_wrapper.txt\n'; sleep 600; printf 'quit\n' ) | \
    timeout 600 winedbg --gdb AgeOfTime.exe > /trace/winedbg_stdout.log 2>&1

echo "TRACE DONE"
