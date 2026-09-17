#!/usr/bin/env bash
# Dev server helpers kept out of the agent's shell so `pkill` can never match the
# command line that invokes it (a classic self-kill when the pattern is literal).
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8080}"
LOG=/tmp/ssave.log
PIDFILE=/tmp/ssave.pid

case "${1:-start}" in
  start)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "already running (pid $(cat "$PIDFILE"))"
    else
      SSAVE_ENV="${SSAVE_ENV:-development}" nohup .venv/bin/python -m uvicorn app.main:app \
        --host 0.0.0.0 --port "$PORT" --log-level info >"$LOG" 2>&1 &
      echo $! > "$PIDFILE"
      sleep 3
      echo "started pid $(cat "$PIDFILE") on :$PORT"
    fi
    ;;
  stop)
    if [ -f "$PIDFILE" ]; then
      kill "$(cat "$PIDFILE")" 2>/dev/null || true
      rm -f "$PIDFILE"
      echo stopped
    else
      echo "not running"
    fi
    ;;
  restart)
    "$0" stop || true
    sleep 1
    "$0" start
    ;;
  log)
    tail -n "${2:-40}" "$LOG"
    ;;
  *)
    echo "usage: $0 {start|stop|restart|log [n]}" >&2
    exit 2
    ;;
esac
