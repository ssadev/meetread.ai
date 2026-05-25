#!/usr/bin/env bash
set -euo pipefail

mkdir -p "${XDG_RUNTIME_DIR:-/tmp/runtime-bot}" /app/meetings /app/state
chmod 700 "${XDG_RUNTIME_DIR:-/tmp/runtime-bot}"

pulseaudio --daemonize=yes --exit-idle-time=-1 --disallow-exit --log-target=stderr

for _ in $(seq 1 30); do
    if pactl info >/dev/null 2>&1; then
        break
    fi
    sleep 0.2
done

pactl info >/dev/null

exec "$@"
