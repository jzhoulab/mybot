#!/bin/zsh
# Start mybot. This is the one entry point the menu app, the agent skill, and
# deploy.sh use; it needs neither Discord nor Slack.
#
#   .discord.env present  -> hands off to run_discord_chatbot.sh (chat server +
#                            Discord bridge, supervised)
#   .slack.env present    -> hands off to run_slack_chatbot.sh (chat server +
#                            Slack bridge, supervised)
#   neither               -> runs the chat server alone (standalone_agent_backbone.py)
#                            and restarts it if it dies
#
# With both bridges configured, this starts Discord; run ./run_slack_chatbot.sh
# alongside it (the Slack launcher shares an already-healthy server).
# MYBOT_BRIDGE=auto|discord|slack|none overrides the choice (default auto).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
MYBOT_BRIDGE="${MYBOT_BRIDGE:-auto}"

case "${MYBOT_BRIDGE:l}" in
  auto)
    if [[ -f "$ROOT_DIR/.discord.env" ]]; then
      exec /bin/zsh "$ROOT_DIR/run_discord_chatbot.sh" "$@"
    fi
    if [[ -f "$ROOT_DIR/.slack.env" ]]; then
      exec /bin/zsh "$ROOT_DIR/run_slack_chatbot.sh" "$@"
    fi
    ;;
  discord) exec /bin/zsh "$ROOT_DIR/run_discord_chatbot.sh" "$@" ;;
  slack)   exec /bin/zsh "$ROOT_DIR/run_slack_chatbot.sh" "$@" ;;
  none)    ;;
  *) echo "MYBOT_BRIDGE must be auto, discord, slack, or none (got '$MYBOT_BRIDGE')"; exit 2 ;;
esac

# ---- server-only mode ------------------------------------------------------
SERVER_ENV_FILE="$ROOT_DIR/.env"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
MYBOT_SINGLE_INSTANCE="${MYBOT_SINGLE_INSTANCE:-true}"
MYBOT_RUN_DIR="${MYBOT_RUN_DIR:-$ROOT_DIR/state/run}"
MYBOT_LOCK_DIR="${MYBOT_SERVER_LOCK_DIR:-$MYBOT_RUN_DIR/mybot-server.lock}"
MYBOT_RESTART_DELAY_SECONDS="${MYBOT_RESTART_DELAY_SECONDS:-5}"
MYBOT_MAX_RESTART_DELAY_SECONDS="${MYBOT_MAX_RESTART_DELAY_SECONDS:-60}"
MYBOT_SERVER_START_TIMEOUT_SECONDS="${MYBOT_SERVER_START_TIMEOUT_SECONDS:-30}"
MYBOT_SERVER_HEALTH_INTERVAL_SECONDS="${MYBOT_SERVER_HEALTH_INTERVAL_SECONDS:-15}"
# A server that is alive but not yet healthy is loading (index rebuild + model
# load can take many minutes on a big history). Only treat it as hung — and
# restart it — after this long without a healthy check.
MYBOT_SERVER_HUNG_SECONDS="${MYBOT_SERVER_HUNG_SECONDS:-1800}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing $PYTHON_BIN — create the venv first and install dependencies."
  exit 1
fi

if [[ -f "$SERVER_ENV_FILE" ]]; then
  set -a; source "$SERVER_ENV_FILE"; set +a
fi

: "${CHATBOT_HOST:=127.0.0.1}"
: "${CHATBOT_PORT:=8788}"
: "${CHATBOT_BASE_URL:=http://${CHATBOT_HOST}:${CHATBOT_PORT}}"
if [[ -n "${SYNC_TOKENS_PATH:-}" && ! -e "$SYNC_TOKENS_PATH" ]]; then
  export SYNC_TOKENS_PATH="$ROOT_DIR/config/sync_tokens.json"
fi

SERVER_PID=""
SERVER_UNHEALTHY_SINCE=""
STOP_REQUESTED="false"

truthy() {
  case "${1:l}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

health_url() { echo "${CHATBOT_BASE_URL%/}/health"; }

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

# Chat servers from this checkout we did not start (an orphan from an earlier
# launcher, or a manual run). Starting a second one on top would race for the
# port and rebuild the index twice.
existing_server_pids() {
  ps -axo pid=,command= | awk -v self="$$" -v mine="${SERVER_PID:-0}" -v srv="$ROOT_DIR/standalone_agent_backbone.py" '
    $1 != self && $1 != mine && $2 ~ /python[0-9.]*$/ && $3 == srv {print $1}
  '
}

acquire_single_instance_lock() {
  truthy "$MYBOT_SINGLE_INSTANCE" || return 0
  mkdir -p "$MYBOT_RUN_DIR"
  if mkdir "$MYBOT_LOCK_DIR" 2>/dev/null; then
    echo "$$" > "$MYBOT_LOCK_DIR/pid"
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$MYBOT_LOCK_DIR/started_at"
    return 0
  fi
  local lock_pid=""
  [[ -f "$MYBOT_LOCK_DIR/pid" ]] && lock_pid="$(cat "$MYBOT_LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
    echo "mybot server launcher already appears to be running with pid $lock_pid."
    echo "Not starting another instance."
    exit 0
  fi
  echo "Removing stale mybot lock at $MYBOT_LOCK_DIR"
  rm -rf "$MYBOT_LOCK_DIR"
  mkdir "$MYBOT_LOCK_DIR" 2>/dev/null || { echo "Could not acquire mybot lock at $MYBOT_LOCK_DIR"; exit 1; }
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
    chat_server_healthy && return 0
    if [[ -n "$SERVER_PID" ]] && ! kill -0 "$SERVER_PID" 2>/dev/null; then
      return 1
    fi
    sleep 1
    waited=$(( waited + 1 ))
  done
  return 1
}

cleanup() {
  STOP_REQUESTED="true"
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  release_single_instance_lock
}

trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

if chat_server_healthy; then
  echo "mybot chat server is already healthy at $(health_url); nothing to do."
  exit 0
fi

acquire_single_instance_lock

start_chat_server() {
  echo "Starting local mybot chat server on ${CHATBOT_HOST}:${CHATBOT_PORT} (no chat bridge configured)"
  "$PYTHON_BIN" "$ROOT_DIR/standalone_agent_backbone.py" --host "$CHATBOT_HOST" --port "$CHATBOT_PORT" &
  SERVER_PID=$!
  SERVER_UNHEALTHY_SINCE="$(date +%s)"
  if wait_for_chat_server; then
    SERVER_UNHEALTHY_SINCE=""
    echo "Local mybot chat server is healthy at $(health_url)"
  elif [[ -n "$SERVER_PID" ]]; then
    echo "Local mybot chat server is not healthy yet at $(health_url) after ${MYBOT_SERVER_START_TIMEOUT_SECONDS}s; still loading, will keep checking."
  fi
}

# Log a status line only when it changes, so the watch loop stays quiet.
last_note=""
note() { [[ "$1" == "$last_note" ]] && return 0; last_note="$1"; echo "$1"; }

restart_delay="$MYBOT_RESTART_DELAY_SECONDS"
while true; do
  [[ "$STOP_REQUESTED" == "true" ]] && break

  if [[ -z "$SERVER_PID" ]]; then
    foreign="$(existing_server_pids | tr '\n' ' ' | xargs 2>/dev/null || true)"
    if [[ -n "$foreign" ]]; then
      if chat_server_healthy; then
        note "A mybot chat server we did not start (pid $foreign) is healthy at $(health_url); watching it."
      else
        note "A mybot chat server we did not start (pid $foreign) is running but not healthy yet; waiting for it rather than starting a second one."
      fi
      sleep "$MYBOT_SERVER_HEALTH_INTERVAL_SECONDS"
      continue
    fi
    if chat_server_healthy; then
      note "Something is already healthy at $(health_url); watching it."
      sleep "$MYBOT_SERVER_HEALTH_INTERVAL_SECONDS"
      continue
    fi
    last_note=""
    start_chat_server
    continue
  fi

  # We own a server process: restart it if it died, or if it has been
  # unhealthy for longer than the hung threshold.
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    set +e
    wait "$SERVER_PID"
    exit_code=$?
    set -e
    SERVER_PID=""
    [[ "$STOP_REQUESTED" == "true" ]] && break
    echo "mybot chat server exited with code ${exit_code}; restarting in ${restart_delay}s"
    sleep "$restart_delay"
    restart_delay=$(( restart_delay * 2 ))
    (( restart_delay > MYBOT_MAX_RESTART_DELAY_SECONDS )) && restart_delay="$MYBOT_MAX_RESTART_DELAY_SECONDS"
    continue
  fi
  if chat_server_healthy; then
    SERVER_UNHEALTHY_SINCE=""
    restart_delay="$MYBOT_RESTART_DELAY_SECONDS"
  else
    [[ -z "$SERVER_UNHEALTHY_SINCE" ]] && SERVER_UNHEALTHY_SINCE="$(date +%s)"
    age=$(( $(date +%s) - SERVER_UNHEALTHY_SINCE ))
    if (( age >= MYBOT_SERVER_HUNG_SECONDS )); then
      echo "mybot chat server (pid $SERVER_PID) has been unhealthy for ${age}s; restarting it."
      kill "$SERVER_PID" 2>/dev/null || true
      wait "$SERVER_PID" 2>/dev/null || true
      SERVER_PID=""
      continue
    fi
  fi
  sleep "$MYBOT_SERVER_HEALTH_INTERVAL_SECONDS"
done
