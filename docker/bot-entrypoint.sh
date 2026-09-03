#!/usr/bin/env bash
set -u

# main.py lists plugins via the relative path "plugins" from CWD (/data), but the
# plugin source ships inside the image at /app/plugins. Symlink it into the
# persistent working dir so the relative lookup resolves while sessions and temp
# media still write to /data. Idempotent: refresh on every start.
ln -sfn /app/plugins /data/plugins
mkdir -p /data/logs

bot_pid=""
cleanup_pid=""

shutdown_children() {
  trap - TERM INT
  for pid in "$bot_pid" "$cleanup_pid"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$bot_pid" "$cleanup_pid"; do
    if [ -n "$pid" ]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
}

on_signal() {
  shutdown_children
  exit 143
}

trap on_signal TERM INT

# The welcome page (/) and /healthz are served in-process by main.py's aiohttp
# HealthServer — the standalone Flask process is gone.
python /app/main.py &
bot_pid=$!

( while sleep 3600; do /usr/local/bin/cleanup-runtime.sh /data || true; done ) &
cleanup_pid=$!

wait -n "$bot_pid" "$cleanup_pid"
status=$?
shutdown_children
exit "$status"
