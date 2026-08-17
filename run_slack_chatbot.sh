#!/bin/zsh
set -euo pipefail

# Slack twin of run_discord_chatbot.sh. Shares the chat server (won't start a
# second one if a healthy server is already up, e.g. from the Discord launcher).
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_ENV_FILE="$ROOT_DIR/.env"
ENV_FILE="$ROOT_DIR/.slack.env"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
START_LOCAL_CHAT_SERVER="${START_LOCAL_CHAT_SERVER:-true}"
SUPERVISE_SLACK_BRIDGE="${SUPERVISE_SLACK_BRIDGE:-true}"
MYBOT_SINGLE_INSTANCE="${MYBOT_SINGLE_INSTANCE:-true}"
MYBOT_RUN_DIR="${MYBOT_RUN_DIR:-$ROOT_DIR/state/run}"
MYBOT_LOCK_DIR="${MYBOT_SLACK_LOCK_DIR:-$MYBOT_RUN_DIR/mybot-slack.lock}"
MYBOT_RESTART_DELAY_SECONDS="${MYBOT_RESTART_DELAY_SECONDS:-5}"
MYBOT_MAX_RESTART_DELAY_SECONDS="${MYBOT_MAX_RESTART_DELAY_SECONDS:-60}"
MYBOT_SERVER_START_TIMEOUT_SECONDS="${MYBOT_SERVER_START_TIMEOUT_SECONDS:-30}"
MYBOT_SERVER_HEALTH_INTERVAL_SECONDS="${MYBOT_SERVER_HEALTH_INTERVAL_SECONDS:-15}"
MYBOT_SERVER_HUNG_SECONDS="${MYBOT_SERVER_HUNG_SECONDS:-1800}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE (copy .slack.env.example to .slack.env and fill in tokens)"
  exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing $PYTHON_BIN — create the venv first and install dependencies."
  exit 1
fi

if [[ -f "$SERVER_ENV_FILE" ]]; then
  set -a; source "$SERVER_ENV_FILE"; set +a
fi
set -a; source "$ENV_FILE"; set +a

if [[ -z "${SLACK_BOT_TOKEN:-}" || "${SLACK_BOT_TOKEN}" == xoxb-REPLACE-ME ]]; then
  echo "Set SLACK_BOT_TOKEN in $ENV_FILE"; exit 1
fi
if [[ -z "${SLACK_APP_TOKEN:-}" || "${SLACK_APP_TOKEN}" == xapp-REPLACE-ME ]]; then
  echo "Set SLACK_APP_TOKEN in $ENV_FILE"; exit 1
fi

: "${CHATBOT_HOST:=127.0.0.1}"
: "${CHATBOT_PORT:=8788}"
: "${CHATBOT_BASE_URL:=http://${CHATBOT_HOST}:${CHATBOT_PORT}}"
export CHATBOT_BASE_URL
if [[ -n "${SYNC_TOKENS_PATH:-}" && ! -e "$SYNC_TOKENS_PATH" ]]; then
  export SYNC_TOKENS_PATH="$ROOT_DIR/config/sync_tokens.json"
fi

SERVER_PID=""
SERVER_UNHEALTHY_SINCE=""
BRIDGE_PID=""
STOP_REQUESTED="false"

truthy() { case "${1:l}" in 1|true|yes|on) return 0 ;; *) return 1 ;; esac }
health_url() { echo "${CHATBOT_BASE_URL%/}/health"; }
chat_server_healthy() { curl -fsS --max-time 2 "$(health_url)" >/dev/null 2>&1; }

existing_bridge_pids() {
  ps -axo pid=,command= | awk -v self="$$" -v bridge="$ROOT_DIR/slack_bridge.py" '
    $1 != self && $2 ~ /python[0-9.]*$/ && $3 == bridge {print $1}'
}
# Chat servers from this checkout we did not start (orphan or manual run).
existing_server_pids() {
  ps -axo pid=,command= | awk -v self="$$" -v mine="${SERVER_PID:-0}" -v srv="$ROOT_DIR/standalone_agent_backbone.py" '
    $1 != self && $1 != mine && $2 ~ /python[0-9.]*$/ && $3 == srv {print $1}'
}

acquire_single_instance_lock() {
  truthy "$MYBOT_SINGLE_INSTANCE" || return 0
  mkdir -p "$MYBOT_RUN_DIR"
  local existing_pids
  existing_pids="$(existing_bridge_pids | tr '\n' ' ' | xargs 2>/dev/null || true)"
  if [[ -n "$existing_pids" ]]; then
    echo "mybot Slack bridge already running: $existing_pids"; exit 0
  fi
  if mkdir "$MYBOT_LOCK_DIR" 2>/dev/null; then
    echo "$$" > "$MYBOT_LOCK_DIR/pid"; return 0
  fi
  local lock_pid=""
  [[ -f "$MYBOT_LOCK_DIR/pid" ]] && lock_pid="$(cat "$MYBOT_LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
    echo "mybot Slack launcher already running with pid $lock_pid."; exit 0
  fi
  rm -rf "$MYBOT_LOCK_DIR"; mkdir "$MYBOT_LOCK_DIR" || { echo "Could not acquire lock"; exit 1; }
  echo "$$" > "$MYBOT_LOCK_DIR/pid"
}

release_single_instance_lock() {
  truthy "$MYBOT_SINGLE_INSTANCE" || return 0
  if [[ -f "$MYBOT_LOCK_DIR/pid" && "$(cat "$MYBOT_LOCK_DIR/pid" 2>/dev/null || true)" == "$$" ]]; then
    rm -rf "$MYBOT_LOCK_DIR"
  fi
}

wait_for_chat_server() {
  local waited=0
  while (( waited < MYBOT_SERVER_START_TIMEOUT_SECONDS )); do
    chat_server_healthy && return 0
    if [[ -n "$SERVER_PID" ]] && ! kill -0 "$SERVER_PID" 2>/dev/null; then
      wait "$SERVER_PID" 2>/dev/null || true; SERVER_PID=""; return 1
    fi
    sleep 1; waited=$(( waited + 1 ))
  done
  return 1
}

# 0 = healthy; 1 = not yet (callers keep going and re-check — never fatal).
ensure_chat_server() {
  truthy "$START_LOCAL_CHAT_SERVER" || return 0
  chat_server_healthy && { SERVER_UNHEALTHY_SINCE=""; return 0; }
  [[ -z "$SERVER_UNHEALTHY_SINCE" ]] && SERVER_UNHEALTHY_SINCE="$(date +%s)"
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    wait_for_chat_server && { SERVER_UNHEALTHY_SINCE=""; return 0; }
    if [[ -n "$SERVER_PID" ]]; then
      # Alive but not healthy: still loading (index + model), unless it's been ages.
      local age=$(( $(date +%s) - ${SERVER_UNHEALTHY_SINCE:-0} ))
      if (( age < MYBOT_SERVER_HUNG_SECONDS )); then
        echo "Chat server (pid $SERVER_PID) is still starting after ${age}s; leaving it to finish."; return 1
      fi
      echo "Chat server process $SERVER_PID unhealthy for ${age}s; restarting it."
      kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; SERVER_PID=""
    fi
  fi
  local foreign
  foreign="$(existing_server_pids | tr '\n' ' ' | xargs 2>/dev/null || true)"
  if [[ -n "$foreign" ]]; then
    echo "A chat server we did not start is running (pid $foreign) but not healthy yet; waiting for it."; return 1
  fi
  echo "Starting local mybot chat server on ${CHATBOT_HOST}:${CHATBOT_PORT}"
  "$PYTHON_BIN" "$ROOT_DIR/standalone_agent_backbone.py" --host "$CHATBOT_HOST" --port "$CHATBOT_PORT" &
  SERVER_PID=$!
  SERVER_UNHEALTHY_SINCE="$(date +%s)"
  if ! wait_for_chat_server; then
    if [[ -n "$SERVER_PID" ]]; then
      echo "Chat server not healthy yet at $(health_url) after ${MYBOT_SERVER_START_TIMEOUT_SECONDS}s; still loading, will keep checking."
    else
      echo "Chat server exited before becoming healthy at $(health_url)"
    fi
    return 1
  fi
  SERVER_UNHEALTHY_SINCE=""
  echo "Local mybot chat server is healthy at $(health_url)"
}

cleanup() {
  STOP_REQUESTED="true"
  [[ -n "$BRIDGE_PID" ]] && kill -0 "$BRIDGE_PID" 2>/dev/null && { kill "$BRIDGE_PID" 2>/dev/null || true; wait "$BRIDGE_PID" 2>/dev/null || true; }
  [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null && { kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; }
  release_single_instance_lock
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

acquire_single_instance_lock
# Never fatal: under set -e a failing call would exit, and zsh skips the EXIT
# trap on that path — orphaning the server.
ensure_chat_server || true

restart_delay="$MYBOT_RESTART_DELAY_SECONDS"
while true; do
  ensure_chat_server || true
  echo "Starting Slack bridge (Socket Mode). Supervision: $SUPERVISE_SLACK_BRIDGE"
  "$PYTHON_BIN" "$ROOT_DIR/slack_bridge.py" &
  BRIDGE_PID=$!
  while kill -0 "$BRIDGE_PID" 2>/dev/null; do
    sleep "$MYBOT_SERVER_HEALTH_INTERVAL_SECONDS"
    [[ "$STOP_REQUESTED" == "true" ]] && break
    ensure_chat_server || true
  done
  set +e; wait "$BRIDGE_PID"; bridge_status=$?; set -e
  BRIDGE_PID=""
  { [[ "$STOP_REQUESTED" == "true" ]] || ! truthy "$SUPERVISE_SLACK_BRIDGE"; } && exit "$bridge_status"
  echo "Slack bridge exited with status $bridge_status. Restarting in ${restart_delay}s."
  sleep "$restart_delay"
  restart_delay=$(( restart_delay * 2 ))
  (( restart_delay > MYBOT_MAX_RESTART_DELAY_SECONDS )) && restart_delay="$MYBOT_MAX_RESTART_DELAY_SECONDS"
done
