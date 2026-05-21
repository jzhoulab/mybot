#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE_PATH="$BACKUP_DIR/chatbot-state-$STAMP.tar.gz"

mkdir -p "$BACKUP_DIR"

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

if [[ -f "$ROOT_DIR/state/semantic_memory.sqlite3" ]]; then
  cp "$ROOT_DIR/state/semantic_memory.sqlite3" "$TMP_DIR/semantic_memory.sqlite3"
fi

if [[ -d "$ROOT_DIR/state/sessions" ]]; then
  mkdir -p "$TMP_DIR/state"
  cp -R "$ROOT_DIR/state/sessions" "$TMP_DIR/state/"
fi

if [[ -f "$ROOT_DIR/config/sync_tokens.json" ]]; then
  mkdir -p "$TMP_DIR/config"
  cp "$ROOT_DIR/config/sync_tokens.json" "$TMP_DIR/config/"
fi

tar -C "$TMP_DIR" -czf "$ARCHIVE_PATH" .
echo "Wrote backup to $ARCHIVE_PATH"
