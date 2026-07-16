#!/bin/zsh
# One-command sync so the three copies of mybot can never drift:
#   repo (main)  ->  GitHub (origin/main)
#                ->  installed menu app        (/Applications, or ~/Applications
#                                               for a non-admin account)
#                ->  running services          (server + Discord bridge restarted)
#                ->  agent skill               (~/.local/bin/mybot, ~/.claude/skills)
# Run after committing. Flags: --no-push (skip GitHub), --no-services (leave
# server/bridge alone), --allow-dirty (build from a dirty tree, stamped -dirty).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PUSH=true
SERVICES=true
ALLOW_DIRTY=false
for arg in "$@"; do
  case "$arg" in
    --no-push) PUSH=false ;;
    --no-services) SERVICES=false ;;
    --allow-dirty) ALLOW_DIRTY=true ;;
    *) echo "unknown flag: $arg (use --no-push / --no-services / --allow-dirty)"; exit 2 ;;
  esac
done

step() { print -P "%F{cyan}==> $1%f"; }

HEAD_SHA="$(git rev-parse --short HEAD)"
if [[ -n "$(git status --porcelain)" ]]; then
  if ! $ALLOW_DIRTY; then
    echo "Working tree is dirty — commit first (or pass --allow-dirty)."
    git status --short
    exit 1
  fi
  echo "(deploying a dirty tree; app will be stamped ${HEAD_SHA}-dirty)"
fi

# 1) GitHub
if $PUSH; then
  step "Pushing main to GitHub"
  git push origin main
else
  step "Skipping GitHub push (--no-push)"
fi

# 2) Build the menu app (stamps the commit into Info.plist)
step "Building menu app"
./macapp/build.sh release

# 3) Install the app, replacing the running copy if there is one.
# /Applications needs admin; a non-admin account installs to ~/Applications
# (Spotlight and Launchpad index both the same).
if [[ -w /Applications ]]; then
  APP_DEST="/Applications/mybot.app"
else
  mkdir -p "$HOME/Applications"
  APP_DEST="$HOME/Applications/mybot.app"
fi
step "Installing $APP_DEST"
if pgrep -xq mybot; then
  osascript -e 'tell application "mybot" to quit' >/dev/null 2>&1 || pkill -x mybot || true
  for _ in {1..20}; do pgrep -xq mybot || break; sleep 0.5; done
fi
rm -rf "$APP_DEST"
ditto macapp/dist/mybot.app "$APP_DEST"
open "$APP_DEST"
echo "  menu app running from $APP_DEST"

# 4) Restart server + Discord bridge on the new code
if $SERVICES; then
  step "Restarting mybot server + Discord bridge"
  pkill -f "$ROOT/discord_bridge.py" 2>/dev/null || true
  pkill -f "$ROOT/standalone_agent_backbone.py" 2>/dev/null || true
  pkill -f "$ROOT/run_discord_chatbot.sh" 2>/dev/null || true
  for _ in {1..20}; do
    pgrep -f "$ROOT/(discord_bridge.py|standalone_agent_backbone.py)" >/dev/null 2>&1 || break
    sleep 0.5
  done
  rm -rf "$ROOT/state/run/mybot.lock"
  # Model + index load can outlast the launcher's default 30s health window.
  MYBOT_SERVER_START_TIMEOUT_SECONDS=180 nohup "$ROOT/run_discord_chatbot.sh" \
    >> "$ROOT/state/run/mybot-launcher.log" 2>&1 &
  disown
  HEALTH_URL="http://127.0.0.1:8788/health"
  printf "  waiting for %s " "$HEALTH_URL"
  HEALTHY=false
  for _ in {1..90}; do
    if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then HEALTHY=true; break; fi
    printf "."; sleep 2
  done
  echo ""
  if $HEALTHY; then
    echo "  server healthy"
  else
    echo "  WARNING: server not healthy yet — check state/run/mybot-launcher.log"
  fi
else
  step "Skipping service restart (--no-services)"
fi

# 5) Agent skill (Claude Code + Codex)
step "Refreshing agent skill"
sh "$ROOT/skills/mybot-memory/install.sh" | sed 's/^/  /'

# 6) Drift report
step "Sync summary"
REMOTE_SHA="$(git rev-parse --short origin/main 2>/dev/null || echo '?')"
APP_SHA="$(defaults read "$APP_DEST/Contents/Info" MybotGitSHA 2>/dev/null || echo '?')"
echo "  repo HEAD:          $HEAD_SHA"
echo "  GitHub origin/main: $REMOTE_SHA"
echo "  installed app:      $APP_SHA ($APP_DEST)"
if $SERVICES; then
  curl -fsS --max-time 3 http://127.0.0.1:8788/health 2>/dev/null \
    | python3 -c 'import json,sys; h=json.load(sys.stdin); print(f"  server:             healthy ({h.get(\"provider_backend\")}/{h.get(\"active_model\")}), index battery-paused: {h.get(\"index_refresh_paused_on_battery\")}")' \
    || echo "  server:             NOT RESPONDING"
fi
sqlite3 "$ROOT/state/trajectory_index.sqlite3" \
  "SELECT '  index:              ' || COUNT(*) || ' chunks, ' || SUM(CASE WHEN embedding_blob IS NOT NULL OR embedding_json != '[]' THEN 1 ELSE 0 END) || ' embedded, ' || COUNT(DISTINCT source_ref) || ' sessions' FROM trajectory_chunks;" 2>/dev/null || true
if [[ "$REMOTE_SHA" != "$HEAD_SHA" && "$PUSH" == "true" ]]; then
  echo "  NOTE: origin/main != HEAD — push did not land?"
fi
echo "Done."
