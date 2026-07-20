---
name: mybot-memory
description: Search the user's own past Claude Code and Codex coding sessions (their "mybot" trajectory memory). Use when the user asks about their own prior work — "have I done X before", "what was the config/command for Y", "did I already try Z", "what did I decide about W", "where's that error I hit", or wants grounded evidence from earlier sessions rather than a guess. Provides hybrid semantic + exact search, read-only SQL with regex over the cleaned index, and windowed transcript reads.
---

# mybot memory: search your own coding history

You have a searchable index of the user's past **Claude Code and Codex sessions**
on this machine — every prior conversation, command, and tool output, cleaned and
deduplicated. Use it to answer "have I done this before / what was that config /
did that job finish / what did I decide" with **real evidence from their history**,
not a guess. **You drive the search yourself** — plan queries, read the exact
window you need, compose SQL, and iterate until the answer is grounded.

**Prerequisite:** the local mybot server must be running (default
`http://127.0.0.1:8788`). If commands report "chat server is not running", tell
the user to start it (`./run_discord_chatbot.sh` in their mybot install) and stop.

All commands go through the `mybot` CLI (owner identity + server URL are preset):

## Search

```
mybot trajectory-search -q "<query>" [--limit N] [--after YYYY-MM-DD] [--before YYYY-MM-DD] [--source codex|claude]
```
Hybrid lexical + semantic search over sessions. Each hit reports `source_ref`,
`title`, `cwd`, `updated_at`, a `snippet`, and in `metadata`: `event_start`,
`event_end`, and `chunk_id` — the coordinates for a follow-up windowed read.
`--after/--before` filter by date; `--source` restricts to one tool.

```
mybot memory-search -q "<query>" [--limit N]
```
Search promoted long-term notes (semantic memory), separate from raw sessions.

## Read the exact spot

```
mybot trajectory-read --source-ref "<codex|claude:id>" [--around-event N --events-before 3 --events-after 6] [--chunk-id C] [-q "<query>"]
```
Zoom into a session. `--around-event N` (from a hit's `event_start`) reads a
window; `--chunk-id C` (from a hit's `metadata.chunk_id`) reads that exact chunk
with related context. Reads report `total_events`, so you can page — **the tail
is the freshest** (continued sessions carry live state at the end).

## Exact / enumeration SQL (with grep/sed/awk/jq built in)

```
mybot sql -q "<SELECT …>" [--limit N]
```
Read-only SQL over the cleaned, included-only index. Tables:
- `trajectory_chunks(id, source_ref, source_name, cwd, title, updated_at, event_start, event_end, text, metadata_json)`
- FTS5 `trajectory_chunks_fts(title, cwd, text)`

**Column types:** `updated_at` is the ISO timestamp and the ONLY date column —
filter/sort time with it (`WHERE updated_at >= '2026-07-06'`, `ORDER BY
updated_at DESC`). `event_start`/`event_end` are **integer event indices** within
a session (0..total_events), **not** dates — never compare them to a date; feed
them to `trajectory-read --around-event`. For a recent-window search use
`--after/--before`.

Your text-tool equivalents run **inside** the query:
- `text REGEXP '...'` — case-insensitive grep
- `regexp_extract(text, pattern[, group])` — grep -o / sed capture
- `regexp_count(text, pattern)` — count matches
- `json_extract(metadata_json, '$.key')` — jq over metadata
- `GROUP BY` / `COUNT` — aggregation

A row's `id` is the `--chunk-id` for `trajectory-read`, so SQL-find → read-context
is one flow. Only `SELECT` runs (writes, `ATTACH`, multi-statement are refused;
results are capped).

## How to work

- **Be relentless and grounded.** Dig with these until the answer rests on real
  evidence, then say what's certain vs. not — with **dates** for time-sensitive
  values.
- **Live numbers hide in command output.** Balances, quotas, job states,
  benchmark results were usually printed by a past command — hunt for the literal
  line (`mybot sql -q "SELECT text FROM trajectory_chunks WHERE text LIKE '%Current Balance%' ORDER BY updated_at DESC LIMIT 3"`)
  before concluding there's no record.
- **Find, then zoom.** Search or SQL to locate the session/chunk, then
  `trajectory-read` the exact window for full context rather than trusting a
  snippet.
- **Prefer SQL for enumeration** ("every project that mentions X", "count by
  source/month") and search for fuzzy/semantic recall.
- Compose freely with `grep`, `sort`, `uniq`, `jq` on the CLI output when that's
  sharper than another query.

## Example flows

Recall a past decision:
```
mybot trajectory-search -q "why we chose baseline-model over the baseline" --limit 5
mybot trajectory-read --source-ref "codex:019e…" --chunk-id 637926 -q "baseline-model baseline"
```

Enumerate + aggregate:
```
mybot sql -q "SELECT source_name, substr(updated_at,1,7) AS month, COUNT(DISTINCT source_ref) AS sessions FROM trajectory_chunks WHERE text REGEXP 'platform2' GROUP BY 1,2 ORDER BY 2 DESC"
```

Find a literal value with its date:
```
mybot sql -q "SELECT substr(updated_at,1,10) AS day, regexp_extract(text,'Current Balance:? [0-9,]+ ?SUs?') AS bal FROM trajectory_chunks WHERE text REGEXP 'Current Balance' ORDER BY updated_at DESC LIMIT 8"
```
