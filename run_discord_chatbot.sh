#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_ENV_FILE="$ROOT_DIR/.env"
ENV_FILE="$ROOT_DIR/.discord.env"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
START_LOCAL_CHAT_SERVER="${START_LOCAL_CHAT_SERVER:-true}"
SUPERVISE_DISCORD_BRIDGE="${SUPERVISE_DISCORD_BRIDGE:-true}"
MYBOT_SINGLE_INSTANCE="${MYBOT_SINGLE_INSTANCE:-true}"
MYBOT_RUN_DIR="${MYBOT_RUN_DIR:-$ROOT_DIR/state/run}"
MYBOT_LOCK_DIR="${MYBOT_LOCK_DIR:-$MYBOT_RUN_DIR/mybot.lock}"
MYBOT_RESTART_DELAY_SECONDS="${MYBOT_RESTART_DELAY_SECONDS:-5}"
MYBOT_MAX_RESTART_DELAY_SECONDS="${MYBOT_MAX_RESTART_DELAY_SECONDS:-60}"
MYBOT_SERVER_START_TIMEOUT_SECONDS="${MYBOT_SERVER_START_TIMEOUT_SECONDS:-30}"
MYBOT_SERVER_HEALTH_INTERVAL_SECONDS="${MYBOT_SERVER_HEALTH_INTERVAL_SECONDS:-15}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE"
  exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing $PYTHON_BIN"
  echo "Create the venv first and install dependencies."
  exit 1
fi

if [[ -f "$SERVER_ENV_FILE" ]]; then
  set -a
  source "$SERVER_ENV_FILE"
  set +a
fi

set -a
source "$ENV_FILE"
set +a

if [[ -z "${DISCORD_BOT_TOKEN:-}" || "${DISCORD_BOT_TOKEN}" == "REGENERATE_AND_PASTE_FRESH_TOKEN_HERE" ]]; then
  echo "Set DISCORD_BOT_TOKEN in $ENV_FILE"
  exit 1
fi

: "${CHATBOT_HOST:=127.0.0.1}"
: "${CHATBOT_PORT:=8788}"
: "${CHATBOT_BASE_URL:=http://${CHATBOT_HOST}:${CHATBOT_PORT}}"
if [[ -n "${SYNC_TOKENS_PATH:-}" && ! -e "$SYNC_TOKENS_PATH" ]]; then
  export SYNC_TOKENS_PATH="$ROOT_DIR/config/sync_tokens.json"
fi

SERVER_PID=""
BRIDGE_PID=""
STOP_REQUESTED="false"

truthy() {
  case "${1:l}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

health_url() {
  echo "${CHATBOT_BASE_URL%/}/health"
}

chat_server_healthy() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 2 "$(health_url)" >/dev/null 2>&1
  else
    "$PYTHON_BIN" - "$CHATBOT_BASE_URL" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

base_url = sys.argv[1].rstrip("/")
with urllib.request.urlopen(f"{base_url}/health", timeout=2) as response:
    raise SystemExit(0 if 200 <= response.status < 300 else 1)
PY
  fi
}

existing_bridge_pids() {
  ps -axo pid=,comm=,command= | awk -v self="$$" -v bridge="$ROOT_DIR/discord_bridge.py" '
    $1 != self && index($2, "python") && index($0, bridge) {print $1}
  '
}

acquire_single_instance_lock() {
  truthy "$MYBOT_SINGLE_INSTANCE" || return 0
  mkdir -p "$MYBOT_RUN_DIR"

  local existing_pids
  existing_pids="$(existing_bridge_pids | tr '\n' ' ' | xargs 2>/dev/null || true)"
  if [[ -n "$existing_pids" ]]; then
    echo "mybot Discord bridge already appears to be running: $existing_pids"
    echo "Not starting another instance."
    exit 0
  fi

  if mkdir "$MYBOT_LOCK_DIR" 2>/dev/null; then
    echo "$$" > "$MYBOT_LOCK_DIR/pid"
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$MYBOT_LOCK_DIR/started_at"
    return 0
  fi

  local lock_pid=""
  if [[ -f "$MYBOT_LOCK_DIR/pid" ]]; then
    lock_pid="$(cat "$MYBOT_LOCK_DIR/pid" 2>/dev/null || true)"
  fi
  if [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
    echo "mybot launcher already appears to be running with pid $lock_pid."
    echo "Not starting another instance."
    exit 0
  fi

  echo "Removing stale mybot lock at $MYBOT_LOCK_DIR"
  rm -rf "$MYBOT_LOCK_DIR"
  if ! mkdir "$MYBOT_LOCK_DIR" 2>/dev/null; then
    echo "Could not acquire mybot lock at $MYBOT_LOCK_DIR"
    exit 1
  fi
  echo "$$" > "$MYBOT_LOCK_DIR/pid"
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$MYBOT_LOCK_DIR/started_at"
}

release_single_instance_lock() {
  truthy "$MYBOT_SINGLE_INSTANCE" || return 0
  if [[ -f "$MYBOT_LOCK_DIR/pid" ]] && [[ "$(cat "$MYBOT_LOCK_DIR/pid" 2>/dev/null || true)" == "$$" ]]; then
    rm -rf "$MYBOT_LOCK_DIR"
  fi
}

wait_for_chat_server() {
  local waited=0
  while (( waited < MYBOT_SERVER_START_TIMEOUT_SECONDS )); do
    if chat_server_healthy; then
      return 0
    fi
    if [[ -n "$SERVER_PID" ]] && ! kill -0 "$SERVER_PID" 2>/dev/null; then
      wait "$SERVER_PID" 2>/dev/null || true
      SERVER_PID=""
      return 1
    fi
    sleep 1
    waited=$(( waited + 1 ))
  done
  return 1
}

ensure_chat_server() {
  truthy "$START_LOCAL_CHAT_SERVER" || return 0
  if chat_server_healthy; then
    return 0
  fi
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    if wait_for_chat_server; then
      return 0
    fi
    echo "Local mybot chat server process $SERVER_PID is not healthy; restarting it."
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    SERVER_PID=""
  fi

  echo "Starting local mybot chat server on ${CHATBOT_HOST}:${CHATBOT_PORT}"
  "$PYTHON_BIN" "$ROOT_DIR/standalone_agent_backbone.py" --host "$CHATBOT_HOST" --port "$CHATBOT_PORT" &
  SERVER_PID=$!
  if ! wait_for_chat_server; then
    echo "Local mybot chat server did not become healthy at $(health_url)"
    return 1
  fi
  echo "Local mybot chat server is healthy at $(health_url)"
}

cleanup() {
  STOP_REQUESTED="true"
  if [[ -n "$BRIDGE_PID" ]] && kill -0 "$BRIDGE_PID" 2>/dev/null; then
    kill "$BRIDGE_PID" 2>/dev/null || true
    wait "$BRIDGE_PID" 2>/dev/null || true
  fi
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  release_single_instance_lock
}

trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

acquire_single_instance_lock
ensure_chat_server

restart_delay="$MYBOT_RESTART_DELAY_SECONDS"

while true; do
  ensure_chat_server
  echo "Starting Discord bridge. Supervision: $SUPERVISE_DISCORD_BRIDGE"
  "$PYTHON_BIN" "$ROOT_DIR/discord_bridge.py" --chatbot-base-url "$CHATBOT_BASE_URL" &
  BRIDGE_PID=$!

  while kill -0 "$BRIDGE_PID" 2>/dev/null; do
    sleep "$MYBOT_SERVER_HEALTH_INTERVAL_SECONDS"
    if [[ "$STOP_REQUESTED" == "true" ]]; then
      break
    fi
    ensure_chat_server || true
  done

  set +e
  wait "$BRIDGE_PID"
  bridge_status=$?
  set -e
  BRIDGE_PID=""

  if [[ "$STOP_REQUESTED" == "true" ]] || ! truthy "$SUPERVISE_DISCORD_BRIDGE"; then
    exit "$bridge_status"
  fi

  echo "Discord bridge exited with status $bridge_status. Restarting in ${restart_delay}s."
  sleep "$restart_delay"
  restart_delay=$(( restart_delay * 2 ))
  if (( restart_delay > MYBOT_MAX_RESTART_DELAY_SECONDS )); then
    restart_delay="$MYBOT_MAX_RESTART_DELAY_SECONDS"
  fi
done
