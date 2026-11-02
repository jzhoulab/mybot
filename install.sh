#!/bin/sh
# mybot in one line:
#
#   curl -fsSL https://raw.githubusercontent.com/jzhoulab/mybot/main/install.sh | sh
#
# Gets the code (or updates it), makes its Python environment, writes a
# working .env (the agent CLI you have, absolute paths, you as the owner),
# asks what mybot may read, and starts it — kept running at login on macOS.
# Run it again any time: it updates and restarts, and keeps your settings.
#
#   MYBOT_DIR=…            where it lives (default ~/Code/mybot)
#   MYBOT_BACKEND=…        claude_cli or codex_cli (default: whichever is installed)
#   MYBOT_ACCESS=default   accept the default reading scope without asking
#   MYBOT_NO_START=1       install only
set -eu

REPO="https://github.com/jzhoulab/mybot.git"
DIR="${MYBOT_DIR:-$HOME/Code/mybot}"
PORT=8788
LABEL="io.github.jzhoulab.mybot"

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mmybot install:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- what it needs --------------------------------------------------------
command -v git >/dev/null 2>&1 || die "git is required"
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
[ -n "$PY" ] || die "Python 3.11 or newer is required (macOS: brew install python@3.12)"
CLAUDE="$(command -v claude 2>/dev/null || true)"
CODEX="$(command -v codex 2>/dev/null || true)"
[ -z "$CODEX" ] && [ -x /Applications/Codex.app/Contents/Resources/codex ] && CODEX=/Applications/Codex.app/Contents/Resources/codex
BACKEND="${MYBOT_BACKEND:-}"
if [ -z "$BACKEND" ]; then
  if [ -n "$CLAUDE" ]; then BACKEND=claude_cli
  elif [ -n "$CODEX" ]; then BACKEND=codex_cli
  else die "mybot reads Claude Code or Codex sessions and needs one of their CLIs (claude or codex) on PATH"
  fi
fi

# ---- the code -------------------------------------------------------------
if [ -d "$DIR/.git" ]; then
  say "Updating $DIR"
  git -C "$DIR" pull --ff-only --quiet || say "  (local changes there: kept as they are, not updated)"
else
  say "Getting mybot into $DIR"
  mkdir -p "$(dirname "$DIR")"
  git clone --quiet "$REPO" "$DIR"
fi
cd "$DIR"

say "Python environment (the first time downloads the embedding model's libraries; a few minutes)"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

# ---- settings -------------------------------------------------------------
setenv() {  # setenv KEY VALUE: set (or add) KEY=VALUE in .env
  if grep -q "^$1=" .env; then
    tmp="$(mktemp)"; awk -v k="$1" -v v="$2" 'BEGIN{FS=OFS="="} $1==k{print k "=" v; next} {print}' .env > "$tmp" && mv "$tmp" .env
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}
if [ ! -f .env ]; then
  say "Writing .env ($BACKEND)"
  cp .env.example .env
  chmod 600 .env
  setenv MODEL_BACKEND "$BACKEND"
  [ -n "$CLAUDE" ] && setenv CLAUDE_COMMAND "$CLAUDE"
  [ -n "$CODEX" ] && setenv CODEX_COMMAND "$CODEX"
  setenv TRAJECTORY_ACCESS_CONFIG_PATH "$DIR/config/access.json"
  setenv TRAJECTORY_INDEX_DB_PATH "$DIR/state/trajectory_index.sqlite3"
  setenv MEMORY_IMPORTED_OWNER_ID "$(id -un)"
else
  say "Keeping your .env"
fi

# ---- what it may read -----------------------------------------------------
if [ ! -f config/access.json ]; then
  if [ "${MYBOT_ACCESS:-}" = default ]; then
    .venv/bin/python -m client.setup_access --init-default
  elif [ -r /dev/tty ] && (: </dev/tty) 2>/dev/null; then
    say "Choose what mybot may read (your answers stay on this machine)"
    .venv/bin/python -m client.setup_access </dev/tty
  else
    say "Not started: choose what mybot may read first —"
    echo "    cd $DIR && .venv/bin/python -m client.setup_access && sh install.sh"
    exit 0
  fi
fi

[ "${MYBOT_NO_START:-}" = 1 ] && { say "Installed (not started). Start with: $DIR/run_chatbot.sh"; exit 0; }

# ---- running --------------------------------------------------------------
if [ "$(uname)" = Darwin ]; then
  # A LaunchAgent: up at login, restarted if it stops. run_chatbot.sh keeps a
  # single instance, so the menu app starting it too is harmless.
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/zsh</string><string>$DIR/run_chatbot.sh</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$(dirname "${CLAUDE:-${CODEX:-/usr/bin/true}}"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.log</string>
</dict></plist>
PLIST
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  say "Started (and kept running at login)"
else
  command -v zsh >/dev/null 2>&1 || die "zsh is required to run mybot (apt install zsh)"
  mkdir -p logs
  nohup zsh "$DIR/run_chatbot.sh" >> logs/server.out 2>&1 &
  say "Started in the background (logs: $DIR/logs/server.out)"
fi

i=0
until curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; do
  i=$((i + 1)); [ $i -gt 90 ] && { say "Still starting — check $DIR/logs"; break; }
  sleep 2
done
cat <<DONE

  mybot is running on http://127.0.0.1:$PORT  (settings: $DIR/.env)
  It is indexing your history now; the first pass takes a while.
  The pulse arrives each morning — in hop's Pulse page and phone app, if hop
  runs on this machine. To have one now:
      curl -s -X POST -H "Authorization: Bearer \$(cat ~/.mybot/api-key)" \\
           http://127.0.0.1:$PORT/briefing/run -d '{"force":true}'
DONE
