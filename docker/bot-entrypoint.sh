#!/usr/bin/env bash
set -u

# main.py lists plugins via the relative path "plugins" from CWD (/data), but the
# plugin source ships inside the image at /app/plugins. Symlink it into the
# persistent working dir so the relative lookup resolves while sessions and temp
# media still write to /data. Idempotent: refresh on every start.
ln -sfn /app/plugins /data/plugins

flask_pid=""
bot_pid=""
cleanup_pid=""

shutdown_children() {
  trap - TERM INT
  for pid in "$flask_pid" "$bot_pid" "$cleanup_pid"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$flask_pid" "$bot_pid" "$cleanup_pid"; do
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

python -m flask --app /app/app.py run --host 0.0.0.0 --port 5000 &
flask_pid=$!

python /app/main.py &
bot_pid=$!

( while sleep 3600; do /usr/local/bin/cleanup-runtime.sh /data || true; done ) &
cleanup_pid=$!

wait -n "$flask_pid" "$bot_pid"
status=$?
shutdown_children
exit "$status"
