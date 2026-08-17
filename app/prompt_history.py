"""Durable prompt history: every prompt the owner typed, with when and where.

The CLIs keep an append-only log of typed prompts (`~/.claude/history.jsonl`,
`~/.codex/history.jsonl`) that outlives their transcripts — Claude Code deletes
transcripts after `cleanupPeriodDays`, the history file it keeps. Indexing it
gives mybot a cheap, durable "what did I ask, when, in which project" layer:
enough to place a piece of work in time and pick the right session even when
the full transcript is gone or was never indexed.

Rows live in the trajectory index database so the read-only SQL tool reaches
them, and `search()` feeds trajectory-search and the chat context.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from sources.models import normalize_text

STOPWORDS = frozenset(
    "a an the and or of to in on for with is are was were be been it this that "
    "these those i me my we our you your can could would should do did does have "
    "has had what which who when where why how about into from at as by not no".split()
)


def _iso(ts: Any) -> str:
    """History timestamps: Claude uses epoch milliseconds, Codex epoch seconds."""
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return ""
    if value > 1e11:
        value /= 1000.0
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]


class PromptHistoryStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS prompt_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_name TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    cwd TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    UNIQUE(source_name, session_id, ts, text_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_prompt_history_ts ON prompt_history(ts);
                CREATE INDEX IF NOT EXISTS idx_prompt_history_session ON prompt_history(source_name, session_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS prompt_history_fts USING fts5(
                    text, cwd, content='prompt_history', content_rowid='id'
                );
                CREATE TABLE IF NOT EXISTS prompt_history_files (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    policy_key TEXT NOT NULL DEFAULT ''
                );
                """
            )

    # ---- import ------------------------------------------------------------

    @staticmethod
    def _iter_history_file(path: str, source_name: str):
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            return
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if source_name == "claude":
                    text = str(entry.get("display") or "")
                    session_id = str(entry.get("sessionId") or "")
                    cwd = str(entry.get("project") or "")
                    ts = _iso(entry.get("timestamp"))
                else:  # codex
                    text = str(entry.get("text") or "")
                    session_id = str(entry.get("session_id") or "")
                    cwd = str(entry.get("cwd") or entry.get("project") or "")
                    ts = _iso(entry.get("ts"))
                text = text.strip()
                if not text or not ts:
                    continue
                if text.startswith("/") and " " not in text.strip():
                    continue  # slash commands (/agents, /clear) are noise
                yield session_id, ts, cwd, text

    def refresh(
        self,
        *,
        history_paths: dict[str, list[str]],
        is_allowed: Callable[[str, str, str], bool] | None = None,
        policy_key: str = "",
    ) -> dict[str, Any]:
        """Import new prompts from every history file (idempotent: rows are
        keyed by source/session/ts/text) and drop rows the current access
        policy no longer allows. `history_paths` maps source_name -> paths;
        `policy_key` should change whenever the access policy changes so an
        unchanged file is re-filtered under the new policy."""
        inserted = 0
        skipped_policy = 0
        seen_files = 0
        unchanged_files = 0
        # The logs are append-only; skip a file whose size/mtime we already
        # imported under the same policy so the periodic refresh costs ~nothing.
        policy_key = policy_key or ""
        with self._lock, self._connect() as conn:
            for source_name, paths in history_paths.items():
                for path in paths:
                    if not os.path.isfile(path):
                        continue
                    seen_files += 1
                    try:
                        stat = os.stat(path)
                    except OSError:
                        continue
                    prior = conn.execute(
                        "SELECT size, mtime, policy_key FROM prompt_history_files WHERE path = ?", (path,)
                    ).fetchone()
                    if (
                        prior is not None
                        and int(prior["size"]) == int(stat.st_size)
                        and abs(float(prior["mtime"]) - float(stat.st_mtime)) < 1e-6
                        and str(prior["policy_key"]) == policy_key
                    ):
                        unchanged_files += 1
                        continue
                    for session_id, ts, cwd, text in self._iter_history_file(path, source_name):
                        if is_allowed is not None and not is_allowed(source_name, session_id, cwd):
                            skipped_policy += 1
                            continue
                        clean = normalize_text(text, 4000)
                        cursor = conn.execute(
                            "INSERT OR IGNORE INTO prompt_history(source_name, session_id, ts, cwd, text, text_hash) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (source_name, session_id, ts, cwd, clean, _text_hash(clean)),
                        )
                        if cursor.rowcount:
                            conn.execute(
                                "INSERT INTO prompt_history_fts(rowid, text, cwd) VALUES (?, ?, ?)",
                                (cursor.lastrowid, clean, cwd),
                            )
                            inserted += 1
                    conn.execute(
                        "INSERT OR REPLACE INTO prompt_history_files(path, size, mtime, policy_key) VALUES (?, ?, ?, ?)",
                        (path, int(stat.st_size), float(stat.st_mtime), policy_key),
                    )
            purged = 0
            if is_allowed is not None and (inserted or unchanged_files < seen_files):
                rows = conn.execute(
                    "SELECT DISTINCT source_name, session_id, cwd FROM prompt_history"
                ).fetchall()
                for row in rows:
                    if is_allowed(str(row["source_name"]), str(row["session_id"]), str(row["cwd"])):
                        continue
                    ids = [
                        int(r["id"])
                        for r in conn.execute(
                            "SELECT id FROM prompt_history WHERE source_name = ? AND session_id = ? AND cwd = ?",
                            (row["source_name"], row["session_id"], row["cwd"]),
                        )
                    ]
                    for chunk_start in range(0, len(ids), 500):
                        batch = ids[chunk_start : chunk_start + 500]
                        marks = ",".join("?" for _ in batch)
                        conn.execute(f"DELETE FROM prompt_history_fts WHERE rowid IN ({marks})", batch)
                        conn.execute(f"DELETE FROM prompt_history WHERE id IN ({marks})", batch)
                    purged += len(ids)
            total = int(conn.execute("SELECT COUNT(*) FROM prompt_history").fetchone()[0])
        return {
            "files": seen_files,
            "unchanged_files": unchanged_files,
            "inserted": inserted,
            "skipped_policy": skipped_policy,
            "purged": purged,
            "total": total,
        }

    # ---- search ------------------------------------------------------------

    @staticmethod
    def _tokens(query: str, limit: int = 8) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in query.split():
            token = raw.strip('.,;:!?"\'()[]{}<>`').replace('"', '""')
            key = token.lower()
            if len(token) < 2 or key in STOPWORDS or key in seen:
                continue
            seen.add(key)
            out.append(token)
        return out[:limit]

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        after: str = "",
        before: str = "",
        source_name: str = "",
    ) -> list[dict[str, Any]]:
        """Prompts matching the query. Tries all terms first (precise: file
        names, identifiers), then falls back to any-term ranked by how many
        distinct terms a prompt matches, then bm25. Newest first on ties."""
        tokens = self._tokens(query)
        if not tokens:
            return []
        filters = []
        params: list[Any] = []
        if after:
            filters.append("p.ts >= ?")
            params.append(after)
        if before:
            filters.append("p.ts <= ?")
            params.append(before + ("T23:59:59" if len(before) == 10 else ""))
        if source_name:
            filters.append("p.source_name = ?")
            params.append(source_name)
        extra = (" AND " + " AND ".join(filters)) if filters else ""

        def run(match: str, cap: int) -> list[sqlite3.Row]:
            with self._connect() as conn:
                try:
                    return conn.execute(
                        f"""
                        SELECT p.id, p.source_name, p.session_id, p.ts, p.cwd, p.text,
                               bm25(prompt_history_fts) AS rank
                        FROM prompt_history_fts
                        JOIN prompt_history AS p ON p.id = prompt_history_fts.rowid
                        WHERE prompt_history_fts MATCH ?{extra}
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (match, *params, cap),
                    ).fetchall()
                except sqlite3.OperationalError:
                    return []

        quoted = [f'"{token}"' for token in tokens]
        rows = run(" AND ".join(quoted), limit * 3) if len(quoted) > 1 else []
        if not rows:
            rows = run(" OR ".join(quoted), limit * 12)
        lowered_tokens = [t.lower().replace('""', '"') for t in tokens]

        def score(row: sqlite3.Row) -> tuple[int, float, str]:
            text = f"{row['text']} {row['cwd']}".lower()
            hits = sum(1 for t in lowered_tokens if t in text)
            return (-hits, float(row["rank"]), "")

        ranked = sorted(rows, key=score)
        out: list[dict[str, Any]] = []
        seen_text: set[tuple[str, str]] = set()
        for row in ranked:
            key = (str(row["session_id"]), str(row["text"])[:200])
            if key in seen_text:
                continue
            seen_text.add(key)
            out.append(
                {
                    "source_type": "prompt_history",
                    "source_name": str(row["source_name"]),
                    "source_ref": f"{row['source_name']}:{row['session_id']}",
                    "session_id": str(row["session_id"]),
                    "ts": str(row["ts"]),
                    "cwd": str(row["cwd"]),
                    "text": normalize_text(str(row["text"]), 600),
                }
            )
            if len(out) >= limit:
                break
        return out

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM prompt_history"
            ).fetchone()
            by_source = {
                str(r["source_name"]): int(r["n"])
                for r in conn.execute("SELECT source_name, COUNT(*) AS n FROM prompt_history GROUP BY 1")
            }
        return {
            "prompts": int(row["n"] or 0),
            "first_ts": str(row["first_ts"] or ""),
            "last_ts": str(row["last_ts"] or ""),
            "by_source": by_source,
        }
