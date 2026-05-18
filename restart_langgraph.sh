#!/usr/bin/env bash
# restart_langgraph.sh — control local langgraph dev process.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

HOST="${LG_HOST:-0.0.0.0}"
PORT="${LG_PORT:-2024}"
CONFIG="${LG_CONFIG:-langgraph.json}"
LOG_FILE="$ROOT_DIR/logs/langgraph-dev.log"
PID_FILE="$ROOT_DIR/logs/langgraph-dev.pid"

mkdir -p "$ROOT_DIR/logs"

is_running() {
  ss -ltn | grep -q ":${PORT} "
}

find_pid() {
  local pid
  pid="$(pgrep -f "langgraph dev --config ${CONFIG}.*--port ${PORT}" | head -n 1 || true)"
  echo "$pid"
}

stop_langgraph() {
  local pid
  pid="$(find_pid)"
  if [[ -n "$pid" ]]; then
    kill "$pid" 2>/dev/null || true
    for _ in {1..20}; do
      if ! kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 0.2
    done
    kill -9 "$pid" 2>/dev/null || true
  fi

  if [[ -f "$PID_FILE" ]]; then
    local p
    p="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$p" ]]; then
      kill "$p" 2>/dev/null || true
      kill -9 "$p" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
  fi
}

start_langgraph() {
  nohup "$ROOT_DIR/venv/bin/langgraph" dev --config "$CONFIG" --host "$HOST" --port "$PORT" --no-browser >> "$LOG_FILE" 2>&1 &
  echo "$!" > "$PID_FILE"

  for _ in {1..40}; do
    if is_running; then
      return 0
    fi
    sleep 0.25
  done
  echo "failed to start langgraph on ${HOST}:${PORT}" >&2
  return 1
}

status_langgraph() {
  local running="false"
  local pid=""
  if is_running; then
    running="true"
  fi
  pid="$(find_pid)"
  echo "running=${running}"
  echo "pid=${pid}"
  echo "host=${HOST}"
  echo "port=${PORT}"
  echo "log_file=${LOG_FILE}"
}

ACTION="${1:-restart}"
case "$ACTION" in
  start)
    start_langgraph
    status_langgraph
    ;;
  stop)
    stop_langgraph
    status_langgraph
    ;;
  restart)
    stop_langgraph
    start_langgraph
    status_langgraph
    ;;
  status)
    status_langgraph
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}" >&2
    exit 2
    ;;
esac
