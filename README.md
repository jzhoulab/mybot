# Shared Memory Chatbot

This project is a lightweight multi-user chatbot stack built from the useful structural ideas behind OpenClaw, but it runs independently:

1. one shared HTTP chat server
2. one Discord bot transport
3. local JSONL chat sessions
4. local SQLite semantic memory
5. manual per-user trajectory sync into shared private memory

The intended v1 deployment is a **single bot and single server** running on one teammate’s always-on machine, reachable only on a private path such as Tailscale, LAN, or an SSH tunnel.

## Layout

- `app/`
  - `server.py`: shared HTTP server
  - `semantic_memory.py`: SQLite semantic memory with embeddings and import upserts
  - `auth.py`: sync-token auth
  - `discord_bridge.py`: Discord transport
- `client/`
  - `sync_trajectories.py`: local manual sync CLI
- `sources/`
  - registry-backed local trajectory adapters
  - `codex` and `claude` are first-class in v1
- `profiles/default/`
  - prompt and persona files for the current bot
- `scripts/backup_state.sh`
  - host-side backup snapshot script
- `config/sync_tokens.example.json`
  - example sync-token mapping file
- `.env.example`
  - shared server/runtime environment template
- `.discord.env.example`
  - Discord transport environment template
- `pyproject.toml`
  - minimal project metadata for packaging and installs

The top-level files `standalone_agent_backbone.py` and `discord_bridge.py` are compatibility entrypoints that call into `app/`.

## Memory model

There are three memory layers:

- session hot context
  - rolling session summary
  - latest detailed turns preserved in full
- private semantic memory
  - per-user promoted notes
  - per-user imported trajectories
- shared team memory
  - only explicitly promoted shared notes

Retrieval is private-first:

- normal chat searches only the requesting user’s private memory
- `shared` mode searches only shared memory
- `target_user` mode searches only the named user’s private memory

Trajectory import granularity is mixed:

- every source session creates one `session` record
- only recent sessions create extra `chunk` records
- recent means the newest 20 sessions per source
- each recent session creates up to 4 chunk records

Embeddings are local:

- `sentence-transformers/all-MiniLM-L6-v2`

Storage is local to the host machine:

- transcripts: `state/sessions/*.jsonl`
- semantic memory: `state/semantic_memory.sqlite3`
- trajectory memory index: `state/trajectory_memory.json`
- prompt files: `profiles/default/*.md`

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install -r requirements.txt
cp .env.example .env
cp .discord.env.example .discord.env
```

`.discord.env` is needed only for the Discord bridge. `.env` holds shared server/runtime settings and is sourced automatically by `run_discord_chatbot.sh` when present.

## Model backend

For local Codex auth:

```bash
export MODEL_BACKEND="codex_cli"
export CODEX_SANDBOX="read-only"
# optional:
# export MODEL_NAME="gpt-5.4"
# export CODEX_CWD="/absolute/path/to/project"
```

For an OpenAI-compatible backend:

```bash
export MODEL_BACKEND="openai_compatible"
export MODEL_BASE_URL="https://api.openai.com/v1"
export MODEL_API_KEY="your-key"
export MODEL_NAME="gpt-4.1-mini"
```

## Server config

Important environment variables:

```bash
export TRAJECTORY_SOURCES="codex,claude"
export MEMORY_IMPORTED_OWNER_ID="000000000000000000"
export EMBEDDING_MODEL_NAME="sentence-transformers/all-MiniLM-L6-v2"
export HISTORY_MAX_MESSAGES="24"
export SESSION_TAIL_PAIRS="12"
export COMPACTION_TRIGGER_MESSAGES="40"
export COMPACTION_TRIGGER_CHARS="16000"
export MEMORY_MATCH_LIMIT="5"
export SYNC_TOKENS_PATH="/absolute/path/to/config/sync_tokens.json"
```

`SYNC_TOKENS_PATH` is used only for the sync endpoints. Normal chat and Discord reply handling continue working even if sync auth is not configured yet.

## Sync token config

Copy the example file and fill in one token record per teammate:

```bash
cp config/sync_tokens.example.json config/sync_tokens.json
```

Each record binds one bearer token hash to one Discord `actor_id`.

To generate a token hash:

```bash
python3 - <<'PY'
import hashlib
token = "paste-a-real-secret-token-here"
print(hashlib.sha256(token.encode("utf-8")).hexdigest())
PY
```

Then place that hash in `config/sync_tokens.json`.

## Start the shared server

```bash
python3 standalone_agent_backbone.py --host 127.0.0.1 --port 8787
```

Important endpoints:

- `POST /chat`
- `POST /memory/rebuild`
- `POST /memory/search`
- `POST /memory/promote`
- `POST /memory/import-batch`
- `POST /memory/sync-status`
- `POST /sessions/history`
- `POST /sessions/reset`
- `GET /health`

### `/chat`

Request fields:

- `actor_id`
- `user`
- `session_key`
- `message`
- `memory_scope`
  - `private`
  - `shared`
  - `target_user`
- `target_user_id` when `memory_scope=target_user`
- `return_sources`

Response fields include:

- `memory_scope_used`
- `session_summary_used`
- `sources`

### `/memory/import-batch`

This endpoint requires `Authorization: Bearer <token>`.

Example:

```bash
curl -sS http://127.0.0.1:8787/memory/import-batch \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer your-real-sync-token' \
  -d '{
    "actor_id": "000000000000000000",
    "client_id": "alice-macbook",
    "batch_id": "alice-codex-20260508",
    "source_name": "codex",
    "items": []
  }'
```

### `/memory/sync-status`

This endpoint also requires the bearer token:

```bash
curl -sS http://127.0.0.1:8787/memory/sync-status \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer your-real-sync-token' \
  -d '{
    "actor_id": "000000000000000000",
    "sources": ["codex", "claude"]
  }'
```

## Manual local sync

Run this on each teammate’s own machine:

```bash
python -m client.sync_trajectories sync \
  --server http://private-host:8787 \
  --actor-id 000000000000000000 \
  --sources codex,claude \
  --token your-real-sync-token
```

Check current server-side sync status:

```bash
python -m client.sync_trajectories status \
  --server http://private-host:8787 \
  --actor-id 000000000000000000 \
  --sources codex,claude \
  --token your-real-sync-token
```

Manual sync behavior:

- reads local trajectories through the registered source adapters
- uploads full normalized transcript text plus summaries and metadata
- creates one session record for every discovered session
- creates chunk records only for recent sessions
- is idempotent via `unique_key` plus `content_hash`
- does not upload embeddings; the server computes them

## Discord

Use `.discord.env` for local Discord + server settings, then launch:

```bash
chmod +x run_discord_chatbot.sh
./run_discord_chatbot.sh
```

Current Discord behavior:

- plain messages in the dedicated auto-reply channel use `private` memory scope
- `!team <question>` uses shared memory only
- `!ask @user <question>` uses the mentioned user’s private memory only
- `!remember <note>` stores a private note
- `!remember-shared <note>` stores a shared note
- `!sources` shows the last assistant reply’s grounding sources
- `!new` resets the current session
- `/chat` and `/new` still exist as slash-command fallbacks

If you want direct message-content chat in guild channels, enable **Message Content Intent** for the bot in the Discord Developer Portal.

## Backup

Create a host-side snapshot of the SQLite DB, session JSONL files, and sync-token config:

```bash
chmod +x scripts/backup_state.sh
./scripts/backup_state.sh
```

Set `BACKUP_DIR` if you want the archive written somewhere else.

## Notes

- Imported trajectories are always private by owner unless promoted to shared memory.
- `/memory/import-batch` and `/memory/sync-status` are the only endpoints that require sync-token auth.
- The system stores the actual retrieved memory records used for each answer in assistant-message metadata so `!sources` can explain grounding later.
- Runtime state and secrets should stay out of git: `state/`, `.env`, `.discord.env`, `config/sync_tokens.json`, backups, and logs are gitignored.
- The tracked prompt/profile source of truth is `profiles/default/`. The older `workspace/` fallback has been removed.
