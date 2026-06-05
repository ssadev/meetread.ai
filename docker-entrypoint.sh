#!/usr/bin/env bash
set -euo pipefail

runtime_dir="${XDG_RUNTIME_DIR:-/tmp/runtime-bot}"

mkdir -p "$runtime_dir" /app/meetings /app/state
chmod 700 "$runtime_dir"

# Docker restarts preserve the container filesystem, including stale PulseAudio
# sockets and pid files under XDG_RUNTIME_DIR. Clear only this ephemeral runtime
# state so a previous daemon does not prevent the next container boot.
rm -rf "$runtime_dir/pulse"

pulseaudio --daemonize=yes --exit-idle-time=-1 --disallow-exit --log-target=stderr

for _ in $(seq 1 30); do
    if pactl info >/dev/null 2>&1; then
        break
    fi
    sleep 0.2
done

pactl info >/dev/null

exec "$@"
