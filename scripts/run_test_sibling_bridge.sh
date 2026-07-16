#!/bin/zsh
# Test-only: run a SECOND Discord bridge (a stand-in teammate bot) against the
# already-running mybot server, so the bot<->bot relay can be exercised end to
# end with two bots in one shared server. It shares Alex's server/index — we're
# testing the relay TRANSPORT, not a second person's memory.
#
# Prereqs:
#   1. Create a second Discord bot (see `python scripts/mybot_admin.py connect
#      discord` for the click-path), enable Message Content Intent, invite it to
#      the SAME server as the primary bot.
#   2. Put its token in .discord2.env:   DISCORD_BOT_TOKEN=xoxb...  -> actually
#      Discord tokens are just DISCORD_BOT_TOKEN=<token>
#   3. Primary server must be up (http://127.0.0.1:8788/health).
#
# The primary bot's id is passed as the sibling so this test bot answers its
# relays. Nickname-setting is off so it keeps its own Discord app name.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"

[[ -f "$ROOT_DIR/.discord2.env" ]] || { echo "Missing $ROOT_DIR/.discord2.env (put the 2nd bot token there)"; exit 1; }

# Base Discord config (channels, intents) from the primary env, then override
# the token + siblings + nickname for the test bot.
set -a
[[ -f "$ROOT_DIR/.discord.env" ]] && source "$ROOT_DIR/.discord.env"
source "$ROOT_DIR/.discord2.env"
set +a

: "${PRIMARY_BOT_ID:=000000000000000000}"   # mybot#8481
export MYBOT_SIBLING_BOT_IDS="$PRIMARY_BOT_ID"
export DISCORD_SET_GUILD_NICKNAME=false
export CHATBOT_BASE_URL="${CHATBOT_BASE_URL:-http://127.0.0.1:8788}"

echo "Starting TEST sibling bridge -> $CHATBOT_BASE_URL (siblings: $MYBOT_SIBLING_BOT_IDS)"
exec "$PYTHON_BIN" "$ROOT_DIR/discord_bridge.py" --chatbot-base-url "$CHATBOT_BASE_URL"
