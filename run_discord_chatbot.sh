#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_ENV_FILE="$ROOT_DIR/.env"
ENV_FILE="$ROOT_DIR/.discord.env"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
START_LOCAL_CHAT_SERVER="${START_LOCAL_CHAT_SERVER:-true}"

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
: "${CHATBOT_PORT:=8787}"
: "${CHATBOT_BASE_URL:=http://${CHATBOT_HOST}:${CHATBOT_PORT}}"
if [[ -n "${SYNC_TOKENS_PATH:-}" && ! -e "$SYNC_TOKENS_PATH" ]]; then
  export SYNC_TOKENS_PATH="$ROOT_DIR/config/sync_tokens.json"
fi

SERVER_PID=""

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

if [[ "$START_LOCAL_CHAT_SERVER" == "true" ]]; then
  "$PYTHON_BIN" "$ROOT_DIR/standalone_agent_backbone.py" --host "$CHATBOT_HOST" --port "$CHATBOT_PORT" &
  SERVER_PID=$!
  sleep 2
fi

exec "$PYTHON_BIN" "$ROOT_DIR/discord_bridge.py" --chatbot-base-url "$CHATBOT_BASE_URL"
