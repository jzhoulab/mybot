#!/bin/sh
# Install the mybot-memory skill for Claude Code and Codex.
#   - `mybot` CLI wrapper -> ~/.local/bin
#   - SKILL.md            -> ~/.claude/skills/mybot-memory/   (Claude Code: lazy-loaded)
#   - a compact pointer   -> ~/.codex/AGENTS.md               (Codex: always-loaded)
# Idempotent. Re-run after editing SKILL.md to refresh.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"

# 1) Claude Code
mkdir -p "$HOME/.local/bin" "$HOME/.claude/skills/mybot-memory"
install -m 0755 "$HERE/mybot" "$HOME/.local/bin/mybot"
install -m 0644 "$HERE/SKILL.md" "$HOME/.claude/skills/mybot-memory/SKILL.md"
echo "Claude Code: installed ~/.claude/skills/mybot-memory/SKILL.md and ~/.local/bin/mybot"

case ":$PATH:" in
  *":$HOME/.local/bin:"*) : ;;
  *) echo "  NOTE: add ~/.local/bin to your PATH so the agent can run 'mybot'." ;;
esac

# 2) Codex — append a compact managed block to the global AGENTS.md (once)
CODEX_AGENTS="$HOME/.codex/AGENTS.md"
mkdir -p "$HOME/.codex"
if [ -f "$CODEX_AGENTS" ] && grep -q "BEGIN mybot-memory skill (managed)" "$CODEX_AGENTS"; then
  echo "Codex: mybot block already present in $CODEX_AGENTS (leaving as-is)"
else
  cat >> "$CODEX_AGENTS" <<'BLOCK'

<!-- BEGIN mybot-memory skill (managed) -->
## mybot memory — search the user's own past coding sessions
When the user asks about their OWN prior work — "have I done X before", "what was the config/command for Y", "did that job finish", "what did I decide about Z" — search their mybot memory (indexed past Claude Code + Codex sessions) instead of guessing. You are the driver. Requires the mybot server running (http://127.0.0.1:8788). Commands (owner id + URL preset):
- `mybot trajectory-search -q "<query>" [--limit N] [--after YYYY-MM-DD] [--source codex|claude]` — hybrid search; hits carry source_ref, metadata.chunk_id, event_start.
- `mybot sql -q "<SELECT …>"` — read-only SQL over trajectory_chunks(id,source_ref,source_name,cwd,title,updated_at,event_start,event_end,text,metadata_json) + FTS5, with text REGEXP, regexp_extract, json_extract, GROUP BY.
- `mybot trajectory-read --source-ref <ref> [--chunk-id C | --around-event N]` — zoom to the exact spot; reports total_events (tail is freshest).
Full guide: ~/Code/mybot/skills/mybot-memory/SKILL.md
<!-- END mybot-memory skill (managed) -->
BLOCK
  echo "Codex: appended mybot block to $CODEX_AGENTS"
fi

echo "Done. Ensure the mybot server is running, then ask your agent about your past work."
