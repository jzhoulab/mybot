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
export CODEX_COMMAND="/Applications/Codex.app/Contents/Resources/codex"
export CODEX_PERMISSION_PROFILE="mybot_restricted"
export CODEX_NETWORK_ACCESS="true"
export CODEX_IGNORE_USER_CONFIG="true"
export CODEX_DISABLE_BACKEND_RESUME="true"
export CODEX_EPHEMERAL="true"
# optional:
# export MODEL_NAME="gpt-5.4"
# export CODEX_CWD="/absolute/path/to/Code/mybot-runtime/default"
# export MYBOT_TOOL_PYTHON="/usr/bin/python3"
```

`CODEX_PERMISSION_PROFILE` enables a restricted Codex permission profile for agentic tool routing. The subprocess runs from an isolated runtime directory outside the mybot repo and can read the generated read-only mybot tool wrapper, but it is not granted repo-wide read access. Raw `~/.codex`, `~/.claude`, mybot config, and mybot state are denied so trajectory visibility is enforced by mybot's access layer.
Use a Codex CLI build with permission-profile support; on macOS the bundled desktop binary above is preferred over older `codex` binaries on `PATH`.

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
export TRAJECTORY_ACCESS_CONFIG_PATH="/absolute/path/to/config/access.json"
export MEMORY_IMPORTED_OWNER_ID="000000000000000000"
export EMBEDDING_MODEL_NAME="sentence-transformers/all-MiniLM-L6-v2"
export TRAJECTORY_MAX_FILES_PER_TOOL="1000"
export TRAJECTORY_INVESTIGATION_MODE="auto"
export TRAJECTORY_SEARCH_LIMIT="5"
export TRAJECTORY_EVIDENCE_LIMIT="2"
export TRAJECTORY_EVIDENCE_CHARS="18000"
export TRAJECTORY_INDEX_AUTOBUILD="true"
export TRAJECTORY_INDEX_AUTOBUILD_VECTORS="false"
export TRAJECTORY_INDEX_AUTOBUILD_MIN_INTERVAL_SECONDS="300"
export TRAJECTORY_INDEX_BACKGROUND_REFRESH_SECONDS="300"
export TRAJECTORY_INDEX_REFRESH_MAX_SESSIONS="0"
export HISTORY_MAX_MESSAGES="24"
export SESSION_TAIL_PAIRS="12"
export COMPACTION_TRIGGER_MESSAGES="40"
export COMPACTION_TRIGGER_CHARS="16000"
export MEMORY_MATCH_LIMIT="5"
export SYNC_TOKENS_PATH="/absolute/path/to/config/sync_tokens.json"
```

`SYNC_TOKENS_PATH` is used only for the sync endpoints. Normal chat and Discord reply handling continue working even if sync auth is not configured yet.

## Trajectory access setup

The bot only imports registered trajectory sources. In v1 those sources are:

- Codex trajectory JSONL under configured Codex roots such as `~/.codex`
- Claude Code project JSONL under configured Claude roots such as `~/.claude/projects`

Access is permissive by default for enabled roots. Each root can use `visibility_mode: "blacklist"` to allow everything except exclusions, or `visibility_mode: "whitelist"` to allow only included workdirs/classes.

Create or edit the local access config with the setup TUI:

```bash
python -m client.setup_access
```

The TUI writes `config/access.json` by default. This file is gitignored because it describes local root paths and access choices.

Example config:

```bash
cp config/access.example.json config/access.json
```

If you have multiple Codex or Claude roots, add them in the TUI and explicitly enable the roots mybot may scan. For each enabled root, choose blacklist or whitelist mode. Workdir filters can be path fragments, full paths, or visibility classes.

For Codex, `codex_no_project` matches sessions that appear to come from Chats rather than a project workspace. In the current data this means empty workdirs, `/Users/you`, or generated `/Users/you/Documents/Codex/...` workdirs. `codex_project` matches the remaining Codex workdirs.

For Claude trajectories, the TUI also shows detected `entrypoint` values. Programmatic SDK calls usually show up as `sdk-cli`, while interactive sessions are usually `cli`, `claude-desktop`, or older files with no entrypoint. Add `sdk-cli` to `excluded_entrypoints` if you want mybot to ignore app-generated Claude invocations while keeping interactive Claude Code sessions.

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
python3 standalone_agent_backbone.py --host 127.0.0.1 --port 8788
```

Important endpoints:

- `POST /chat`
- `POST /memory/rebuild`
- `POST /memory/search`
- `POST /trajectory/search`
- `POST /trajectory/read`
- `POST /trajectory/index/rebuild`
- `POST /trajectory/index/embed`
- `POST /trajectory/index/stats`
- `POST /memory/promote`
- `POST /memory/import-batch`
- `POST /memory/sync-status`
- `POST /sessions/history`
- `POST /sessions/reset`
- `GET /health`
- `GET /gui`

### Local GUI

Open the local dashboard while the server is running:

```bash
open http://127.0.0.1:8788/gui
```

The dashboard shows the indexed trajectory pool, active visibility policy, runtime hardening status, recently indexed trajectories, recent retrieval activity, and whether raw trajectory files are newer than the searchable index.

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

When `TRAJECTORY_INVESTIGATION_MODE=auto`, chat requests can automatically promote likely trajectory candidates into a `Full Trajectory Evidence` prompt section. The automatic path uses the reduced trajectory chunk index when available, falls back to the semantic DB plus lexical/proximity candidate search, then reopens only the top allowed trajectory files for parsed evidence.

With `AGENTIC_TOOL_ROUTING=true`, chat requests expose a generated read-only tool wrapper in the isolated runtime directory to the Codex agent and let the agent decide whether memory or trajectory lookup is needed. The server passes the actor and memory scope through environment variables so tool calls use the same access policy as direct API calls.

Trajectory investigation modes:

- `auto`: default; use full-read evidence when a query looks trajectory-related or memory retrieval finds trajectory candidates
- `off`: disable automatic full-read evidence
- `full_scan`: slower comparison mode; scan allowed trajectory files directly

### `/trajectory/search`

Search allowed local trajectories without answering through the model:

```bash
curl -sS http://127.0.0.1:8788/trajectory/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "figure compression",
    "actor_id": "000000000000000000",
    "limit": 5
  }'
```

The fast path uses an agentic multi-step retrieval loop: it searches the persistent reduced trajectory chunk index, semantic memory, lexical memory, and local parsed candidates; then it tries focused query variants and title/path refinements when recall looks trajectory-related or ambiguous. Set `"return_trace": true` to inspect the retrieval steps. Set `"full_scan": true` to force slower full parsed-file search instead of the fast candidate path.

### `/trajectory/index/rebuild`

Rebuild the persistent reduced trajectory chunk index from the currently allowed Codex and Claude trajectory roots:

```bash
curl -sS http://127.0.0.1:8788/trajectory/index/rebuild \
  -H 'Content-Type: application/json' \
  -d '{
    "actor_id": "000000000000000000",
    "include_vectors": false
  }'
```

The index stores reduced conversation chunks, not raw arbitrary filesystem content. Exact search uses SQLite FTS phrase/token search plus literal/proximity fallback for identifier-like queries. Hybrid search reranks bounded FTS candidates with vectors when chunk embeddings exist, and full vector search uses an in-process cache instead of reparsing every embedding on each query.

With `TRAJECTORY_INDEX_AUTOBUILD=true`, startup and trajectory lookup refresh the reduced chunk index when raw trajectory files are newer than the index. Refresh is incremental: unchanged sessions are left alone, hidden or removed sessions are deleted, and only changed sessions are re-chunked. `TRAJECTORY_INDEX_AUTOBUILD_VECTORS=false` keeps that refresh fast by rebuilding exact/FTS chunks without embedding every chunk. `TRAJECTORY_INDEX_AUTOBUILD_MIN_INTERVAL_SECONDS` throttles repeated refreshes during active sessions, and `TRAJECTORY_INDEX_BACKGROUND_REFRESH_SECONDS` keeps the index warm even before the next user asks a trajectory question.

### `/trajectory/index/embed`

Backfill vector embeddings for existing reduced chunks in small batches:

```bash
curl -sS http://127.0.0.1:8788/trajectory/index/embed \
  -H 'Content-Type: application/json' \
  -d '{
    "actor_id": "000000000000000000",
    "limit": 256
  }'
```

This is useful after rebuilding with `"include_vectors": false`; repeat it until `missing_embeddings` from `/trajectory/index/stats` reaches `0`.

### `/trajectory/index/stats`

Inspect the current chunk index:

```bash
curl -sS http://127.0.0.1:8788/trajectory/index/stats \
  -H 'Content-Type: application/json' \
  -d '{"actor_id": "000000000000000000"}'
```

### `/trajectory/read`

Read a parsed trajectory by `source_ref`, bounded by `max_chars`:

```bash
curl -sS http://127.0.0.1:8788/trajectory/read \
  -H 'Content-Type: application/json' \
  -d '{
    "source_ref": "codex:example-session-id",
    "actor_id": "000000000000000000",
    "query": "figure compression",
    "max_chars": 12000
  }'
```

### `/memory/import-batch`

This endpoint requires `Authorization: Bearer <token>`.

Example:

```bash
curl -sS http://127.0.0.1:8788/memory/import-batch \
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
curl -sS http://127.0.0.1:8788/memory/sync-status \
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
  --server http://private-host:8788 \
  --actor-id 000000000000000000 \
  --sources codex,claude \
  --token your-real-sync-token
```

Check current server-side sync status:

```bash
python -m client.sync_trajectories status \
  --server http://private-host:8788 \
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

The launcher is supervised by default:

- Discord.py reconnects after ordinary Discord/network interruptions.
- If the Discord bridge process exits, `run_discord_chatbot.sh` restarts it with exponential backoff.
- If the local chat server dies or fails health checks, the launcher starts it again.
- A local lock under `state/run/mybot.lock` prevents accidental duplicate bridge instances.

Launcher knobs:

```bash
START_LOCAL_CHAT_SERVER=true
SUPERVISE_DISCORD_BRIDGE=true
MYBOT_SINGLE_INSTANCE=true
MYBOT_RESTART_DELAY_SECONDS=5
MYBOT_MAX_RESTART_DELAY_SECONDS=60
MYBOT_SERVER_START_TIMEOUT_SECONDS=30
MYBOT_SERVER_HEALTH_INTERVAL_SECONDS=15
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

The bridge coalesces rapid follow-up chat messages for the same user/session before sending them to the model. Set `DISCORD_MESSAGE_COALESCE_SECONDS` to tune the quiet period. Messages that arrive while a model call is already in flight are serialized into the next response instead of racing the same session history.

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
