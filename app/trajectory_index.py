#!/usr/bin/env python3
"""Persistent reduced-trajectory chunk index for exact and vector search."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.semantic_memory import EMBEDDING_PREFIX, QUERY_PREFIX, LocalEmbedder, cosine_similarity
from sources.access import (
    account_patterns,
    discover_claude_metadata,
    discover_codex_workdir,
    load_access_config,
)
from sources.common import recent_files
from sources.models import normalize_text
from sources.trajectory_lookup import (
    TOKEN_RE,
    TrajectoryLookup,
    event_snippets_for_query,
    focus_query,
    generated_session_penalty,
    likely_trajectory_question,
    proximity_score,
    query_tokens,
    render_events,
    snippet_around,
    snippets_for_query,
    token_match_is_strong_enough,
)


DEFAULT_CHUNK_CHARS = 4800
DEFAULT_CHUNK_OVERLAP_CHARS = 800
# Command output (tool_result) and the commands themselves (actions) carry the
# live infrastructure state — SU balances, disk usage, job status — that prose
# turns only paraphrase. They must be searchable, not dropped at index time.
SEARCHABLE_EVENT_ROLES = {"user", "assistant", "tool_result", "actions"}
# Half-life (days) for the recency boost applied at ranking time.
RECENCY_HALF_LIFE_DAYS = 30.0
RECENCY_INTENT_RE = re.compile(
    r"\b(current|currently|now|today|latest|most recent|recent|recently|"
    r"up to date|up-to-date|updated|still|as of|these days|right now|"
    r"newest|this week|so far|by now)\b",
    re.IGNORECASE,
)
VECTOR_BATCH_SIZE = 32
# Chunks that quote this assistant's own machinery — test queries typed while
# building mybot, mybot source read into tool output, retrieval traces — echo
# whatever entities the code uses as examples ("cluster", "showusage", a balance
# line) and would otherwise outrank the session holding the real data. The
# penalty keys off the chunk BODY, not the session title: a genuine data chunk
# inside a mybot-titled session must escape it, and an echo chunk inside an
# innocently-titled session must not.
SELF_ECHO_MARKERS = (
    "mybot",
    "trajectory-search",
    "trajectory-read",
    "trajectory_chunk",  # trajectory_chunks table, trajectory_chunk_index
    "trajectory_memory",
    "semantic_memory",
    # Quoted retrieval output: genuine work never reprints another session's
    # header or the tool's JSON fields, so these mark result dumps captured
    # while testing this assistant.
    "source ref: claude:",
    "source ref: codex:",
    '"match_kind"',
    '"source_ref"',
)


def _chunk_body(text: str) -> str:
    """Chunk text after the header (Title/Source/Source ref/CWD/Updated) that
    _chunk_document prepends. normalize_text flattens the stored text to one
    line, so locate the header's final `Updated: <timestamp>` field and cut
    through its value. Content-keyed penalties must not trip on header echoes
    of the session title or ref."""
    if not text.startswith("Title: "):
        return text
    marker = " Updated: "
    index = text.find(marker)
    if index < 0:
        return text
    rest = text[index + len(marker):]
    space = rest.find(" ")
    return rest[space + 1:] if space >= 0 else ""



SELF_ECHO_QUERY_EXEMPT_RE = re.compile(
    r"\b(mybot|bot|chatbot|memory|memories|retrieval|retrieve\w*|trajector\w*|"
    r"index\w*|chunk\w*|embedding\w*|discord|menu bar|menu app|gui)\b",
    re.IGNORECASE,
)
CORRECTION_MARKERS = (
    "better word",
    "correction",
    "corrected",
    "rename",
    "renamed",
    "agreed",
)
EXACT_PREFIX_PATTERNS = (
    r"^what happened when i asked(?: you)? to\s+",
    r"^what happened when i asked(?: you)?\s+",
    r"^what happened(?: in| with| about)?\s+",
    r"^what did we decide(?: about| on)?\s+",
    r"^what was the final status of\s+",
    r"^what was\s+",
    r"^what were\s+",
    r"^can you summarize\s+",
    r"^summarize\s+",
    r"^search(?: for| through)?\s+",
    r"^find(?: the| a)?\s+",
)

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional runtime acceleration
    np = None

# A chunk counts as embedded if it has either the compact blob or legacy JSON.
EMBEDDED_WHERE = "(embedding_blob IS NOT NULL OR embedding_json != '[]')"
NOT_EMBEDDED_WHERE = "(embedding_blob IS NULL AND embedding_json = '[]')"


def encode_embedding_blob(vector: Any) -> bytes:
    if np is not None:
        return np.asarray(vector, dtype="float32").tobytes()
    import struct

    values = [float(value) for value in vector]
    return struct.pack(f"<{len(values)}f", *values)


def decode_embedding(blob: Any, json_text: Any) -> list[float] | None:
    if blob:
        if np is not None:
            return np.frombuffer(blob, dtype="float32").tolist()
        import struct

        count = len(blob) // 4
        return list(struct.unpack(f"<{count}f", blob))
    text = str(json_text or "")
    if text and text != "[]":
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def stable_hash(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def fts_query(text: str) -> str:
    tokens = []
    expanded = query_tokens(focus_query(text) or text)
    raw_tokens = expanded or TOKEN_RE.findall(focus_query(text) or text)
    for token in raw_tokens:
        for part in re.findall(r"[A-Za-z0-9_]{3,}", token.lower()):
            tokens.append(f'"{part}"')
    return " OR ".join(tokens[:24])


def fts_phrase_queries(text: str) -> list[str]:
    candidates: list[str] = []

    def add(value: str) -> None:
        value = normalize_text(value, 300).strip(" ?.'\"")
        if value and value.lower() not in {candidate.lower() for candidate in candidates}:
            candidates.append(value)

    raw = focus_query(text) or text
    add(raw)
    lowered = raw.lower()
    for pattern in EXACT_PREFIX_PATTERNS:
        stripped = re.sub(pattern, "", lowered, count=1, flags=re.IGNORECASE).strip(" ?.'\"")
        if stripped != lowered:
            add(stripped)

    queries: list[str] = []
    for candidate in candidates:
        tokens = re.findall(r"[A-Za-z0-9_]{2,}", candidate.lower())
        if len(tokens) < 2:
            continue
        phrase = " ".join(tokens[:18])
        queries.append(f'"{phrase}"')
    return queries[:4]


def needs_literal_fallback(text: str) -> bool:
    focused = focus_query(text) or text
    lowered = focused.lower()
    if not lowered or len(lowered) > 320:
        return False
    if any(char in lowered for char in "/._:-"):
        return True
    if re.search(r'"[^"]{8,}"', text or ""):
        return True
    return bool(re.search(r"\b[a-z]+[0-9][a-z0-9_]*\b", lowered))


@dataclass
class ChunkDraft:
    source_ref: str
    source_name: str
    session_id: str
    chunk_index: int
    title: str
    cwd: str
    updated_at: str
    file_path: str
    event_start: int
    event_end: int
    char_start: int
    char_end: int
    text: str
    metadata: dict[str, Any]


class TrajectoryChunkIndex:
    def __init__(
        self,
        *,
        db_path: str,
        model_name: str,
        source_names: list[str],
        max_files_per_tool: int,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
        max_chunks_per_session: int = 2,
    ) -> None:
        self.db_path = db_path
        self.lookup = TrajectoryLookup(
            source_names=source_names,
            max_files_per_tool=max_files_per_tool,
        )
        self.embedder = LocalEmbedder(model_name)
        self.chunk_chars = max(1200, chunk_chars)
        self.overlap_chars = max(0, min(overlap_chars, self.chunk_chars // 2))
        self.max_chunks_per_session = max(1, max_chunks_per_session)
        self._lock = threading.Lock()
        self._vector_cache_lock = threading.Lock()
        self._vector_cache_key: tuple[int, int, str] | None = None
        self._vector_cache_ids: list[int] = []
        self._vector_cache_matrix: Any = None
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trajectory_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_ref TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    cwd TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    file_path TEXT NOT NULL DEFAULT '',
                    event_start INTEGER NOT NULL DEFAULT 0,
                    event_end INTEGER NOT NULL DEFAULT 0,
                    char_start INTEGER NOT NULL DEFAULT 0,
                    char_end INTEGER NOT NULL DEFAULT 0,
                    text TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    embedding_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    indexed_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(source_ref, chunk_index)
                );

                CREATE INDEX IF NOT EXISTS idx_trajectory_chunks_source_ref
                ON trajectory_chunks(source_ref);

                CREATE INDEX IF NOT EXISTS idx_trajectory_chunks_updated
                ON trajectory_chunks(updated_at);

                CREATE VIRTUAL TABLE IF NOT EXISTS trajectory_chunks_fts
                USING fts5(title, cwd, text);
                """
            )
            # Compact embedding storage: float32 blob is ~5x smaller than the
            # JSON text form. Added lazily so existing DBs upgrade in place.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(trajectory_chunks)")}
            if "embedding_blob" not in columns:
                conn.execute("ALTER TABLE trajectory_chunks ADD COLUMN embedding_blob BLOB DEFAULT NULL")

    def clear(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM trajectory_chunks")
            conn.execute("DELETE FROM trajectory_chunks_fts")
        self._clear_vector_cache()

    def _clear_vector_cache(self) -> None:
        with self._vector_cache_lock:
            self._vector_cache_key = None
            self._vector_cache_ids = []
            self._vector_cache_matrix = None

    def _embeddings_for_drafts(self, drafts: list[ChunkDraft], *, include_vectors: bool) -> list[list[float]]:
        if not include_vectors or not drafts:
            return [[] for _ in drafts]

        embeddings: list[list[float]] = []
        for index in range(0, len(drafts), VECTOR_BATCH_SIZE):
            batch = drafts[index : index + VECTOR_BATCH_SIZE]
            embeddings.extend(
                self.embedder.encode(
                    [draft.text for draft in batch],
                    prefix=EMBEDDING_PREFIX,
                )
            )
        return embeddings

    def _delete_source_refs(self, conn: sqlite3.Connection, source_refs: list[str]) -> int:
        deleted_chunks = 0
        for source_ref in source_refs:
            rows = conn.execute(
                "SELECT id FROM trajectory_chunks WHERE source_ref = ?",
                (source_ref,),
            ).fetchall()
            for row in rows:
                conn.execute("DELETE FROM trajectory_chunks_fts WHERE rowid = ?", (int(row["id"]),))
            conn.execute("DELETE FROM trajectory_chunks WHERE source_ref = ?", (source_ref,))
            deleted_chunks += len(rows)
        return deleted_chunks

    def _insert_drafts(
        self,
        conn: sqlite3.Connection,
        drafts: list[ChunkDraft],
        embeddings: list[list[float]],
        *,
        indexed_at: str,
    ) -> None:
        for draft, embedding in zip(drafts, embeddings):
            content_hash = stable_hash(
                draft.source_ref,
                str(draft.chunk_index),
                draft.updated_at,
                draft.text,
            )
            cursor = conn.execute(
                """
                INSERT INTO trajectory_chunks (
                    source_ref, source_name, session_id, chunk_index, title, cwd,
                    updated_at, file_path, event_start, event_end, char_start,
                    char_end, text, content_hash, embedding_json, metadata_json,
                    indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.source_ref,
                    draft.source_name,
                    draft.session_id,
                    draft.chunk_index,
                    draft.title,
                    draft.cwd,
                    draft.updated_at,
                    draft.file_path,
                    draft.event_start,
                    draft.event_end,
                    draft.char_start,
                    draft.char_end,
                    draft.text,
                    content_hash,
                    json.dumps(embedding),
                    json.dumps(draft.metadata),
                    indexed_at,
                ),
            )
            rowid = cursor.lastrowid
            conn.execute(
                "INSERT INTO trajectory_chunks_fts(rowid, title, cwd, text) VALUES (?, ?, ?, ?)",
                (rowid, draft.title, draft.cwd, draft.text),
            )

    def rebuild(self, *, include_vectors: bool = True) -> dict[str, Any]:
        started = utc_now()
        self.lookup.clear()
        sessions = self.lookup.sessions()
        drafts: list[ChunkDraft] = []
        counts_by_source: dict[str, int] = {}
        for session in sessions:
            doc = self.lookup.document_for(session)
            session_drafts = self._chunk_document(doc)
            drafts.extend(session_drafts)
            counts_by_source[doc.source_name] = counts_by_source.get(doc.source_name, 0) + 1

        embeddings = self._embeddings_for_drafts(drafts, include_vectors=include_vectors)

        indexed_at = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM trajectory_chunks")
            conn.execute("DELETE FROM trajectory_chunks_fts")
            self._insert_drafts(conn, drafts, embeddings, indexed_at=indexed_at)
        self._clear_vector_cache()

        return {
            "started_at": started,
            "indexed_at": indexed_at,
            "sessions": len(sessions),
            "chunks": len(drafts),
            "by_source": counts_by_source,
            "db_path": self.db_path,
            "include_vectors": include_vectors,
            "chunk_chars": self.chunk_chars,
            "overlap_chars": self.overlap_chars,
        }

    def refresh_changed(self, *, include_vectors: bool = False, max_sessions: int = 0) -> dict[str, Any]:
        started = utc_now()
        self.lookup.clear()
        sessions = self.lookup.sessions()
        session_by_ref = {
            f"{session.source_name}:{session.session_id}": session
            for session in sessions
        }
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_ref, MAX(updated_at) AS updated_at, COUNT(*) AS chunks
                FROM trajectory_chunks
                GROUP BY source_ref
                """
            ).fetchall()
        indexed_by_ref = {
            str(row["source_ref"]): {
                "updated_at": str(row["updated_at"] or ""),
                "chunks": int(row["chunks"] or 0),
            }
            for row in rows
        }

        stale_refs = [
            source_ref
            for source_ref, session in session_by_ref.items()
            if indexed_by_ref.get(source_ref, {}).get("updated_at") != session.updated_at
        ]
        stale_refs.sort(key=lambda source_ref: session_by_ref[source_ref].updated_at, reverse=True)
        if max_sessions > 0:
            selected_refs = stale_refs[:max_sessions]
            remaining_stale = max(0, len(stale_refs) - len(selected_refs))
        else:
            selected_refs = stale_refs
            remaining_stale = 0

        removed_refs = sorted(set(indexed_by_ref) - set(session_by_ref))
        drafts: list[ChunkDraft] = []
        counts_by_source: dict[str, int] = {}
        for source_ref in selected_refs:
            session = session_by_ref[source_ref]
            doc = self.lookup.document_for(session)
            session_drafts = self._chunk_document(doc)
            drafts.extend(session_drafts)
            counts_by_source[doc.source_name] = counts_by_source.get(doc.source_name, 0) + 1

        embeddings = self._embeddings_for_drafts(drafts, include_vectors=include_vectors)
        indexed_at = utc_now()
        with self._lock, self._connect() as conn:
            deleted_chunks = self._delete_source_refs(conn, selected_refs + removed_refs)
            self._insert_drafts(conn, drafts, embeddings, indexed_at=indexed_at)

        if selected_refs or removed_refs:
            self._clear_vector_cache()

        stats = self.stats()
        return {
            "started_at": started,
            "indexed_at": indexed_at,
            "sessions_seen": len(sessions),
            "sessions_updated": len(selected_refs),
            "sessions_removed": len(removed_refs),
            "remaining_stale_sessions": remaining_stale,
            "chunks_inserted": len(drafts),
            "chunks_deleted": deleted_chunks,
            "by_source": counts_by_source,
            "db_path": self.db_path,
            "include_vectors": include_vectors,
            "chunk_chars": self.chunk_chars,
            "overlap_chars": self.overlap_chars,
            "stats": stats,
        }

    def project_breakdown(self, *, limit: int = 300) -> list[dict[str, Any]]:
        """Indexed sessions grouped by working directory — the 'what is included'
        view, read straight from the index so it reflects reality, not intent."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_name,
                       cwd,
                       COUNT(DISTINCT source_ref) AS sessions,
                       COUNT(*) AS chunks,
                       MAX(updated_at) AS updated_at
                FROM trajectory_chunks
                GROUP BY source_name, cwd
                ORDER BY sessions DESC, chunks DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def purge_disallowed(self, is_allowed: Any) -> dict[str, Any]:
        """Delete every indexed session the current policy no longer permits.

        `is_allowed(source_name, session_id, cwd) -> bool`. Reconciles the index
        with the access policy in one pass, so an exclusion is a real deletion —
        the content leaves the searchable store, not just the query results.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_ref,
                       MAX(source_name) AS source_name,
                       MAX(session_id) AS session_id,
                       MAX(cwd) AS cwd
                FROM trajectory_chunks
                GROUP BY source_ref
                """
            ).fetchall()
        to_delete = [
            str(row["source_ref"])
            for row in rows
            if not is_allowed(
                str(row["source_name"] or ""),
                str(row["session_id"] or ""),
                str(row["cwd"] or ""),
            )
        ]
        deleted_chunks = 0
        if to_delete:
            with self._lock, self._connect() as conn:
                deleted_chunks = self._delete_source_refs(conn, to_delete)
            self._clear_vector_cache()
            self.lookup.clear()
        return {
            "purged_sessions": len(to_delete),
            "purged_chunks": deleted_chunks,
            "source_refs": to_delete,
        }

    def purge_by_origin(self, origin: str, *, source_names: list[str] | None = None) -> dict[str, Any]:
        """Delete every indexed session whose metadata origin matches (FTS-safe).

        Used to bulk-remove automated/agent-driven sessions the user doesn't want
        in their memory. Requires origin to be populated in metadata_json.
        """
        return self._purge_by_metadata("$.origin", origin, source_names=source_names)

    def purge_by_origin_detail(
        self, detail: str, *, source_names: list[str] | None = None
    ) -> dict[str, Any]:
        """Delete one automated cluster ("subagent"/"sdk"/"exec"/"no-user-turns")."""
        return self._purge_by_metadata("$.origin_detail", detail, source_names=source_names)

    def _purge_by_metadata(
        self, json_path: str, value: str, *, source_names: list[str] | None = None
    ) -> dict[str, Any]:
        query = (
            "SELECT DISTINCT source_ref FROM trajectory_chunks "
            f"WHERE json_extract(metadata_json, '{json_path}') = ?"
        )
        params: list[Any] = [value]
        if source_names:
            placeholders = ",".join("?" for _ in source_names)
            query += f" AND source_name IN ({placeholders})"
            params.extend(source_names)
        with self._connect() as conn:
            refs = [str(row["source_ref"]) for row in conn.execute(query, params).fetchall()]
        deleted = 0
        if refs:
            with self._lock, self._connect() as conn:
                deleted = self._delete_source_refs(conn, refs)
            self._clear_vector_cache()
            self.lookup.clear()
        return {"purged_sessions": len(refs), "purged_chunks": deleted}

    def compact_embeddings(self, *, batch: int = 2000) -> dict[str, Any]:
        """Convert legacy JSON embeddings to the compact float32 blob column.

        ~5x smaller on disk. Idempotent; reads already handle both formats, so
        this can run any time without touching the retrieval path's behavior.
        """
        converted = 0
        while True:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, embedding_json FROM trajectory_chunks "
                    "WHERE embedding_blob IS NULL AND embedding_json != '[]' LIMIT ?",
                    (batch,),
                ).fetchall()
            if not rows:
                break
            with self._lock, self._connect() as conn:
                for row in rows:
                    embedding = decode_embedding(None, row["embedding_json"])
                    if not embedding:
                        continue
                    conn.execute(
                        "UPDATE trajectory_chunks SET embedding_blob = ?, embedding_json = '[]' WHERE id = ?",
                        (encode_embedding_blob(embedding), int(row["id"])),
                    )
                    converted += 1
        if converted:
            self._clear_vector_cache()
        return {"converted_chunks": converted}

    def vacuum(self) -> dict[str, Any]:
        """Reclaim disk left behind by deletions."""
        before = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
        after = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        return {"bytes_before": before, "bytes_after": after, "bytes_reclaimed": max(0, before - after)}

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM trajectory_chunks").fetchone()[0]
            embedded = conn.execute(
                f"SELECT COUNT(*) FROM trajectory_chunks WHERE {EMBEDDED_WHERE}"
            ).fetchone()[0]
            fts_rows = conn.execute("SELECT COUNT(*) FROM trajectory_chunks_fts").fetchone()[0]
            source_rows = conn.execute(
                "SELECT source_name, COUNT(DISTINCT source_ref) AS sessions, COUNT(*) AS chunks, "
                f"SUM(CASE WHEN {EMBEDDED_WHERE} THEN 1 ELSE 0 END) AS embedded_chunks "
                "FROM trajectory_chunks GROUP BY source_name"
            ).fetchall()
            latest = conn.execute("SELECT MAX(indexed_at) FROM trajectory_chunks").fetchone()[0] or ""
        return {
            "total_chunks": total,
            "embedded_chunks": embedded,
            "missing_embeddings": max(0, total - embedded),
            "fts_rows": fts_rows,
            "vector_cache_loaded": bool(self._vector_cache_ids),
            "indexed_at": latest,
            "by_source": {
                row["source_name"]: {
                    "sessions": row["sessions"],
                    "chunks": row["chunks"],
                    "embedded_chunks": row["embedded_chunks"] or 0,
                }
                for row in source_rows
            },
            "db_path": self.db_path,
        }

    def source_file_snapshot(self) -> dict[str, Any]:
        access_config = load_access_config()
        paths_by_source: list[tuple[str, Any, str]] = []
        for source_name in self.lookup.source_names:
            for account in access_config.accounts_for_source(source_name):
                for path in recent_files(account_patterns(account), self.lookup.max_files_per_tool):
                    paths_by_source.append((source_name, account, path))
        latest_path = ""
        latest_mtime = 0.0
        seen: set[str] = set()
        allowed_seen: set[str] = set()
        for source_name, account, path in paths_by_source:
            real_path = os.path.realpath(path)
            if real_path in seen:
                continue
            seen.add(real_path)
            if source_name == "codex":
                _, workdir = discover_codex_workdir(path)
                if not account.include_workdir(workdir):
                    continue
            elif source_name == "claude":
                _, workdir, entrypoints = discover_claude_metadata(path)
                if not account.include_workdir(workdir) or not account.include_entrypoints(entrypoints):
                    continue
            allowed_seen.add(real_path)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_path = path
        latest_source_mtime = (
            datetime.fromtimestamp(latest_mtime, timezone.utc).isoformat()
            if latest_mtime
            else ""
        )
        return {
            "source_file_count": len(allowed_seen),
            "latest_source_file_mtime": latest_source_mtime,
            "latest_source_file_path": latest_path,
        }

    def freshness(self) -> dict[str, Any]:
        stats = self.stats()
        snapshot = self.source_file_snapshot()
        indexed_at = str(stats.get("indexed_at") or "")
        indexed_dt = parse_iso_datetime(indexed_at)
        latest_source = str(snapshot.get("latest_source_file_mtime") or "")
        latest_source_dt = parse_iso_datetime(latest_source)
        stale = bool(
            snapshot.get("source_file_count")
            and latest_source_dt is not None
            and (indexed_dt is None or latest_source_dt > indexed_dt)
        )
        now = datetime.now(timezone.utc)
        indexed_age_seconds = (
            max(0.0, (now - indexed_dt).total_seconds())
            if indexed_dt is not None
            else None
        )
        return {
            "stale": stale,
            "indexed_at": indexed_at,
            "indexed_age_seconds": indexed_age_seconds,
            **snapshot,
        }

    def backfill_vectors(self, *, limit: int = 256) -> dict[str, Any]:
        limit = max(1, min(int(limit), 4096))
        started = utc_now()
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, text
                FROM trajectory_chunks
                WHERE {NOT_EMBEDDED_WHERE}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        if not rows:
            stats = self.stats()
            return {
                "started_at": started,
                "finished_at": utc_now(),
                "updated_chunks": 0,
                "remaining_missing_embeddings": stats["missing_embeddings"],
                "db_path": self.db_path,
            }

        updated = 0
        for index in range(0, len(rows), VECTOR_BATCH_SIZE):
            batch = rows[index : index + VECTOR_BATCH_SIZE]
            embeddings = self.embedder.encode(
                [str(row["text"] or "") for row in batch],
                prefix=EMBEDDING_PREFIX,
            )
            with self._lock, self._connect() as conn:
                for row, embedding in zip(batch, embeddings):
                    conn.execute(
                        "UPDATE trajectory_chunks SET embedding_json = ? WHERE id = ?",
                        (json.dumps(embedding), int(row["id"])),
                    )
                    updated += 1
        self._clear_vector_cache()
        stats = self.stats()
        return {
            "started_at": started,
            "finished_at": utc_now(),
            "updated_chunks": updated,
            "remaining_missing_embeddings": stats["missing_embeddings"],
            "db_path": self.db_path,
        }

    def _eligible_chunk_ids(self, *, after: str, before: str, source: str) -> set[int] | None:
        """Rowids passing the hard updated_at/source filters, or None when no
        filter is active. ISO-prefix comparisons; a date-only `before` bound is
        made inclusive of that whole day."""
        after = (after or "").strip()
        before = (before or "").strip()
        source = (source or "").strip().lower()
        if not (after or before or source):
            return None
        clauses: list[str] = []
        params: list[str] = []
        if source:
            clauses.append("lower(source_name) = ?")
            params.append(source)
        if after:
            clauses.append("updated_at >= ?")
            params.append(after)
        if before:
            clauses.append("updated_at <= ?")
            params.append(before if len(before) > 10 else before + "~")
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM trajectory_chunks WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchall()
        return {int(row["id"]) for row in rows}

    # Above this, restricting the vector search to the filtered rowids would
    # decode embeddings row-by-row (no cache); a window this wide behaves like
    # an unfiltered search anyway, so use the full matrix and post-filter.
    _VECTOR_CANDIDATE_FILTER_MAX = 4000

    def search(
        self,
        *,
        query: str,
        limit: int = 5,
        mode: str = "hybrid",
        exact_limit: int = 300,
        vector_limit: int = 50,
        after: str = "",
        before: str = "",
        source: str = "",
    ) -> list[dict[str, Any]]:
        mode = (mode or "hybrid").lower()
        eligible_ids = self._eligible_chunk_ids(after=after, before=before, source=source)
        if eligible_ids is not None and not eligible_ids:
            return []
        scores: dict[int, dict[str, float]] = {}
        exact_rowids: set[int] = set()
        if mode in {"hybrid", "exact", "fts", "lexical"}:
            for rowid, score in self._exact_scores(query, limit=exact_limit):
                if eligible_ids is not None and rowid not in eligible_ids:
                    continue
                scores.setdefault(rowid, {})["exact"] = max(scores.get(rowid, {}).get("exact", 0.0), score)
                exact_rowids.add(rowid)
        if mode in {"hybrid", "vector", "semantic"}:
            if mode == "hybrid" and exact_rowids:
                vector_candidates = exact_rowids
            elif eligible_ids is not None and len(eligible_ids) <= self._VECTOR_CANDIDATE_FILTER_MAX:
                vector_candidates = eligible_ids
            else:
                vector_candidates = None
            for rowid, score in self._vector_scores(
                query,
                limit=vector_limit,
                candidate_rowids=vector_candidates,
            ):
                if eligible_ids is not None and rowid not in eligible_ids:
                    continue
                scores.setdefault(rowid, {})["vector"] = max(scores.get(rowid, {}).get("vector", 0.0), score)
        if not scores:
            return []

        rowids = list(scores)
        placeholders = ",".join("?" for _ in rowids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM trajectory_chunks WHERE id IN ({placeholders})",
                tuple(rowids),
            ).fetchall()

        now = datetime.now(timezone.utc)
        recency_intent = bool(RECENCY_INTENT_RE.search(query or ""))
        payloads: list[dict[str, Any]] = []
        chunk_texts: dict[int, str] = {}
        for row in rows:
            parts = scores.get(row["id"], {})
            exact = parts.get("exact", 0.0)
            vector = parts.get("vector", 0.0)
            if mode in {"exact", "fts", "lexical"}:
                score = exact
                match_kind = "exact"
            elif mode in {"vector", "semantic"}:
                score = vector
                match_kind = "vector"
            else:
                score = exact + vector
                match_kind = "hybrid"
            score = self._adjust_score(
                score, row=row, query=query, now=now, recency_intent=recency_intent
            )
            payload = self._row_to_payload(row, score=score, match_kind=match_kind)
            payload["metadata"]["exact_score"] = round(exact, 4)
            payload["metadata"]["vector_score"] = round(vector, 4)
            chunk_texts[int(row["id"])] = str(row["text"] or "")
            payloads.append(payload)

        payloads.sort(
            key=lambda item: (
                float(item.get("score") or 0.0),
                str(item.get("updated_at") or ""),
            ),
            reverse=True,
        )
        # Allow a long session to contribute more than one chunk: the fresh
        # tail of a 2000+ chunk session should not be shut out just because an
        # earlier chunk of the same session also matched.
        per_session = max(1, self.max_chunks_per_session)
        deduped: list[dict[str, Any]] = []
        seen_counts: dict[str, int] = {}
        for payload in payloads:
            ref = str(payload.get("source_ref") or "")
            if seen_counts.get(ref, 0) >= per_session:
                continue
            seen_counts[ref] = seen_counts.get(ref, 0) + 1
            deduped.append(payload)
            if len(deduped) >= limit:
                break
        # text_preview is the chunk HEAD (mostly header); the line that actually
        # matched can sit anywhere in the chunk. Surface it, or a zoomed read
        # becomes a blind guess and "no grounded record" a false conclusion.
        for payload in deduped:
            text = chunk_texts.get(int(payload["metadata"].get("chunk_id") or 0), "")
            snippets = snippets_for_query(_chunk_body(text), query, limit=2, width=500) if text else []
            if snippets:
                payload["snippets"] = snippets
        return deduped

    def _recency_bonus(
        self, updated_at: str, *, now: datetime, recency_intent: bool
    ) -> float:
        updated_dt = parse_iso_datetime(updated_at)
        if updated_dt is None:
            return 0.0
        age_days = max(0.0, (now - updated_dt).total_seconds() / 86400.0)
        freshness = math.exp(-age_days / RECENCY_HALF_LIFE_DAYS)
        # Weight recency heavily for "what is the current ..." style queries and
        # modestly otherwise, so a fresher snapshot outranks an equally-matching
        # stale one without swamping a much stronger lexical/semantic match.
        weight = 90.0 if recency_intent else 30.0
        return freshness * weight

    def _adjust_score(
        self,
        score: float,
        *,
        row: sqlite3.Row,
        query: str,
        now: datetime | None = None,
        recency_intent: bool = False,
    ) -> float:
        tokens = set(query_tokens(query))
        title_tokens = set(TOKEN_RE.findall(str(row["title"] or "").lower()))
        title_hits = len(tokens & title_tokens)
        if title_hits:
            score += title_hits * 35.0
        if int(row["chunk_index"]) <= 1:
            score += 25.0
        # A gentle nudge, not a guillotine: continued/compacted sessions carry
        # genuinely fresh evidence deep in their chunk stream.
        if int(row["chunk_index"]) > 120 and title_hits == 0:
            score *= 0.8
        if not SELF_ECHO_QUERY_EXEMPT_RE.search(query or ""):
            body_lower = _chunk_body(str(row["text"] or "")).lower()
            if any(marker in body_lower for marker in SELF_ECHO_MARKERS):
                score *= 0.35
        score *= generated_session_penalty(title=str(row["title"] or ""), cwd=str(row["cwd"] or ""))
        if now is not None:
            score += self._recency_bonus(
                str(row["updated_at"] or ""), now=now, recency_intent=recency_intent
            )
        return score

    def read_window(
        self,
        *,
        source_ref: str,
        event_index: int,
        chunk_id: int | None = None,
        query: str = "",
        before: int = 3,
        after: int = 5,
        max_chars: int = 12000,
    ) -> dict[str, Any] | None:
        session = self.lookup.find_session(source_ref)
        if session is None:
            return None
        doc = self.lookup.document_for(session)
        chunk_row: sqlite3.Row | None = None
        if chunk_id is not None:
            with self._connect() as conn:
                chunk_row = conn.execute(
                    "SELECT * FROM trajectory_chunks WHERE id = ? AND source_ref = ?",
                    (chunk_id, source_ref),
                ).fetchone()
        if chunk_row is not None:
            start = int(chunk_row["event_start"])
            end = int(chunk_row["event_end"]) + 1
        else:
            if doc.events:
                event_index = max(0, min(event_index, len(doc.events) - 1))
            start = max(0, event_index - before)
            end = min(len(doc.events), event_index + after + 1)
        windows: list[tuple[str, int, int]] = []
        if chunk_row is None:
            windows.append(("Matched context", start, end))
        if query and likely_trajectory_question(query) and doc.events:
            tail_start = max(0, len(doc.events) - 10)
            tail_end = len(doc.events)
            overlaps_match = tail_start <= start and tail_end >= end
            indexed_view_is_current = chunk_row is None or str(chunk_row["updated_at"] or "") == doc.updated_at
            if indexed_view_is_current and not overlaps_match and tail_start < tail_end:
                windows.append(("Trajectory tail", tail_start, tail_end))

        transcript_sections: list[str] = []

        def append_related_sections() -> None:
            if chunk_row is None or not query:
                return
            related_rows = self._related_chunk_rows(
                source_ref=source_ref,
                query=query,
                exclude_ids={int(chunk_row["id"])},
                limit=2,
            )
            for related_index, related in enumerate(related_rows, start=1):
                used_chars = sum(len(section) for section in transcript_sections)
                remaining = max_chars - used_chars - 160
                if remaining < 1000:
                    break
                related_budget = max(1000, min(2400, remaining // max(1, len(related_rows) - related_index + 1)))
                raw_related_text = str(related["text"] or "")
                related_lower = raw_related_text.lower()
                correction_snippets = [
                    snippet for marker in CORRECTION_MARKERS
                    if marker in related_lower
                    for snippet in [snippet_around(raw_related_text, marker, width=related_budget)]
                    if snippet
                ]
                related_text = "\n\n---\n\n".join(correction_snippets[:1])
                if not related_text:
                    related_text = "\n\n---\n\n".join(
                        snippets_for_query(raw_related_text, query=query, limit=2, width=max(700, related_budget // 2))
                    )
                if not related_text:
                    related_text = normalize_text(raw_related_text, related_budget)
                elif len(related_text) > related_budget:
                    related_text = normalize_text(related_text, related_budget)
                transcript_sections.append(
                    "Additional matching indexed chunk "
                    f"{related_index} (events {related['event_start']}-{related['event_end']}, "
                    f"indexed {related['indexed_at']})\n{related_text}"
                )

        if chunk_row is not None:
            if query:
                indexed_budget = max(1400, int(max_chars * 0.58))
            elif windows:
                indexed_budget = max(1400, int(max_chars * 0.64))
            else:
                indexed_budget = max_chars
            indexed_text = normalize_text(str(chunk_row["text"] or ""), indexed_budget)
            transcript_sections.append(
                f"Matched indexed chunk (events {start}-{end - 1}, indexed {chunk_row['indexed_at']})\n{indexed_text}"
            )
            if not windows:
                append_related_sections()

        if len(windows) == 0:
            per_window_chars = []
        elif len(windows) == 1 and not transcript_sections:
            per_window_chars = [max_chars]
        elif len(windows) == 1:
            per_window_chars = [max(1200, max_chars - sum(len(section) for section in transcript_sections) - 160)]
        else:
            first_budget = max(1200, int(max_chars * 0.58))
            second_budget = max(1200, max_chars - first_budget - 160)
            per_window_chars = [first_budget, second_budget]

        for (label, window_start, window_end), window_chars in zip(windows, per_window_chars):
            rendered = render_events(doc.events[window_start:window_end], max_chars=window_chars)
            if not rendered:
                continue
            transcript_sections.append(
                f"{label} (events {window_start}-{window_end - 1})\n{rendered}"
            )
        if windows:
            append_related_sections()
        transcript = "\n\n---\n\n".join(transcript_sections)
        if max_chars and len(transcript) > max_chars:
            transcript = transcript[: max_chars - 30].rstrip() + "\n\n[truncated]"
        snippets = event_snippets_for_query(
            doc.events,
            query=query,
            title=doc.title,
            cwd=doc.cwd,
            limit=4,
        ) if query else []
        return {
            "source_type": "trajectory",
            "source_name": doc.source_name,
            "source_ref": doc.source_ref,
            "session_id": doc.session_id,
            "title": doc.title,
            "updated_at": doc.updated_at,
            "cwd": doc.cwd,
            "metadata": doc.metadata,
            "event_start": start,
            "event_end": end - 1,
            "total_events": len(doc.events),
            "snippets": snippets,
            "transcript": transcript,
            "transcript_chars": len(doc.transcript),
        }

    def _related_chunk_rows(
        self,
        *,
        source_ref: str,
        query: str,
        exclude_ids: set[int],
        limit: int,
    ) -> list[sqlite3.Row]:
        scores: dict[int, float] = {}
        rows_by_id: dict[int, sqlite3.Row] = {}
        focused = focus_query(query)
        literal = focused.lower()
        tokens = query_tokens(query)
        broad_anchor_tokens = {
            "assistant",
            "claude",
            "codex",
            "project",
            "projects",
            "trajectory",
            "trajectories",
        }
        with self._connect() as conn:
            if literal:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM trajectory_chunks
                    WHERE source_ref = ? AND lower(text) LIKE ?
                    ORDER BY chunk_index
                    LIMIT 24
                    """,
                    (source_ref, f"%{literal}%"),
                ).fetchall()
                for row in rows:
                    rowid = int(row["id"])
                    if rowid in exclude_ids:
                        continue
                    count = str(row["text"] or "").lower().count(literal)
                    scores[rowid] = max(scores.get(rowid, 0.0), 100.0 + min(count, 5) * 10.0)
                    rows_by_id[rowid] = row

            match_query = fts_query(query)
            if match_query:
                try:
                    rows = conn.execute(
                        """
                        SELECT c.*, bm25(trajectory_chunks_fts) AS rank
                        FROM trajectory_chunks_fts
                        JOIN trajectory_chunks c ON c.id = trajectory_chunks_fts.rowid
                        WHERE trajectory_chunks_fts MATCH ? AND c.source_ref = ?
                        ORDER BY rank
                        LIMIT 48
                        """,
                        (match_query, source_ref),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                for row in rows:
                    rowid = int(row["id"])
                    if rowid in exclude_ids:
                        continue
                    token_list = TOKEN_RE.findall(str(row["text"] or "").lower())
                    distinct = len(set(tokens) & set(token_list))
                    token_score = float(distinct * distinct * 3)
                    near_score = proximity_score(token_list, tokens)
                    bm25_score = 50.0 / (1.0 + abs(float(row["rank"])))
                    score = bm25_score + token_score + near_score
                    scores[rowid] = max(scores.get(rowid, 0.0), score)
                    rows_by_id[rowid] = row

            anchor_tokens = [
                token for token in tokens
                if len(token) >= 8 and token not in broad_anchor_tokens
            ][:8]
            for token in anchor_tokens:
                # Bias toward the most RECENT mentions of the anchor entity (a
                # session may reference "MCB25009" hundreds of times; the current
                # balance is the last one), then let query-term density pick the
                # richest chunk so a live data hit outranks a passing mention.
                rows = conn.execute(
                    """
                    SELECT *
                    FROM trajectory_chunks
                    WHERE source_ref = ? AND lower(text) LIKE ?
                    ORDER BY chunk_index DESC
                    LIMIT 24
                    """,
                    (source_ref, f"%{token}%"),
                ).fetchall()
                for position, row in enumerate(rows):
                    rowid = int(row["id"])
                    if rowid in exclude_ids:
                        continue
                    row_text = str(row["text"] or "").lower()
                    row_tokens = set(TOKEN_RE.findall(row_text))
                    density = len(set(tokens) & row_tokens)
                    correction_bonus = 360.0 if any(marker in row_text for marker in CORRECTION_MARKERS) else 0.0
                    # Recency dominates: the current state of an entity is its most
                    # recent mention, not its densest. Density only breaks ties so a
                    # terse command-output line (a balance, a job table) still wins
                    # over an older paragraph that merely name-drops the entity.
                    recency_bonus = max(0.0, 300.0 - position * 20.0)
                    scores[rowid] = max(
                        scores.get(rowid, 0.0),
                        recency_bonus + min(len(token), 24) + min(density, 5) * 8.0 + correction_bonus,
                    )
                    rows_by_id[rowid] = row

        ranked_ids = sorted(scores, key=lambda rowid: scores[rowid], reverse=True)
        return [rows_by_id[rowid] for rowid in ranked_ids[:limit]]

    def _chunk_document(self, doc: Any) -> list[ChunkDraft]:
        event_blocks: list[tuple[int, str]] = []
        char_cursor = 0
        for index, event in enumerate(doc.events):
            if event.role.lower() not in SEARCHABLE_EVENT_ROLES:
                continue
            role = event.role.upper()
            label = f"{role} [{event.timestamp}]" if event.timestamp else role
            block = f"{label}\n{event.text}\n"
            event_blocks.append((index, block))

        chunks: list[ChunkDraft] = []
        current: list[tuple[int, str]] = []
        current_chars = 0
        chunk_start_char = 0

        def flush() -> None:
            nonlocal current, current_chars, chunk_start_char
            if not current:
                return
            body = "\n".join(block for _, block in current).strip()
            if not body:
                current = []
                current_chars = 0
                return
            event_start = current[0][0]
            event_end = current[-1][0]
            header = "\n".join(
                [
                    f"Title: {doc.title}",
                    f"Source: {doc.source_name}",
                    f"Source ref: {doc.source_ref}",
                    f"CWD: {doc.cwd}",
                    f"Updated: {doc.updated_at}",
                    "",
                ]
            )
            chunk_text = normalize_text(header + body, self.chunk_chars + 1200)
            chunk = ChunkDraft(
                source_ref=doc.source_ref,
                source_name=doc.source_name,
                session_id=doc.session_id,
                chunk_index=len(chunks),
                title=doc.title,
                cwd=doc.cwd,
                updated_at=doc.updated_at,
                file_path=doc.file_path,
                event_start=event_start,
                event_end=event_end,
                char_start=chunk_start_char,
                char_end=chunk_start_char + current_chars,
                text=chunk_text,
                metadata={
                    **doc.metadata,
                    "event_start": event_start,
                    "event_end": event_end,
                    "chunk_index": len(chunks),
                },
            )
            chunks.append(chunk)

            if self.overlap_chars <= 0:
                chunk_start_char = chunk.char_end
                current = []
                current_chars = 0
                return
            overlap: list[tuple[int, str]] = []
            overlap_size = 0
            for item in reversed(current):
                size = len(item[1])
                if overlap and overlap_size + size > self.overlap_chars:
                    break
                overlap.insert(0, item)
                overlap_size += size
            chunk_start_char = max(0, chunk.char_end - overlap_size)
            current = overlap
            current_chars = overlap_size

        for event_index, block in event_blocks:
            if len(block) > self.chunk_chars:
                if current:
                    flush()
                start = 0
                while start < len(block):
                    end = min(len(block), start + self.chunk_chars)
                    piece = block[start:end]
                    current = [(event_index, piece)]
                    current_chars = len(piece)
                    chunk_start_char = char_cursor + start
                    flush()
                    if end >= len(block):
                        break
                    start = max(end - self.overlap_chars, start + 1)
                char_cursor += len(block)
                current = []
                current_chars = 0
                chunk_start_char = char_cursor
                continue
            if current and current_chars + len(block) > self.chunk_chars:
                flush()
            if not current:
                chunk_start_char = char_cursor
            current.append((event_index, block))
            current_chars += len(block)
            char_cursor += len(block)
        if current:
            flush()
        return chunks

    def _exact_scores(self, query: str, *, limit: int) -> list[tuple[int, float]]:
        focused = focus_query(query)
        literal = focused.lower()
        scores: dict[int, float] = {}
        with self._connect() as conn:
            for phrase_query in fts_phrase_queries(query):
                try:
                    rows = conn.execute(
                        """
                        SELECT rowid, bm25(trajectory_chunks_fts) AS rank, text
                        FROM trajectory_chunks_fts
                        WHERE trajectory_chunks_fts MATCH ?
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (phrase_query, limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                for row in rows:
                    phrase_bonus = 180.0
                    bm25_score = 80.0 / (1.0 + abs(float(row["rank"])))
                    score = phrase_bonus + bm25_score
                    scores[row["rowid"]] = max(scores.get(row["rowid"], 0.0), score)

            if literal and needs_literal_fallback(query):
                rows = conn.execute(
                    """
                    SELECT id, text, title, updated_at
                    FROM trajectory_chunks
                    WHERE lower(text) LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (f"%{literal}%", limit),
                ).fetchall()
                for row in rows:
                    count = row["text"].lower().count(literal)
                    scores[row["id"]] = max(scores.get(row["id"], 0.0), 100.0 + min(count, 5) * 10.0)

            match_query = fts_query(query)
            if match_query:
                try:
                    rows = conn.execute(
                        """
                        SELECT rowid, bm25(trajectory_chunks_fts) AS rank, text
                        FROM trajectory_chunks_fts
                        WHERE trajectory_chunks_fts MATCH ?
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (match_query, limit),
                    ).fetchall()
                    tokens = query_tokens(query)
                    for row in rows:
                        bm25_score = 50.0 / (1.0 + abs(float(row["rank"])))
                        token_list = TOKEN_RE.findall(str(row["text"] or "").lower())
                        matched = set(tokens) & set(token_list)
                        if not token_match_is_strong_enough(tokens, matched):
                            continue
                        distinct = len(matched)
                        token_score = float(distinct * distinct * 3)
                        near_score = proximity_score(token_list, tokens)
                        score = bm25_score + token_score + near_score
                        scores[row["rowid"]] = max(scores.get(row["rowid"], 0.0), score)
                except sqlite3.OperationalError:
                    pass
        return sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]

    def _vector_scores(
        self,
        query: str,
        *,
        limit: int,
        candidate_rowids: set[int] | None = None,
    ) -> list[tuple[int, float]]:
        query_text = focus_query(query) or query
        if not query_text:
            return []
        query_embedding = self.embedder.encode([query_text], prefix=QUERY_PREFIX)[0]
        if candidate_rowids:
            return self._score_vector_rows(
                query_embedding=query_embedding,
                rows=self._select_vector_rows(candidate_rowids=candidate_rowids),
                limit=limit,
            )
        cached = self._vector_cache_data()
        if cached is not None:
            ids, matrix = cached
            if np is not None and hasattr(matrix, "shape"):
                query_vector = np.asarray(query_embedding, dtype="float32")
                scores = matrix @ query_vector
                if scores.size == 0:
                    return []
                take = min(limit, scores.size)
                if take <= 0:
                    return []
                if take < scores.size:
                    indexes = np.argpartition(scores, -take)[-take:]
                else:
                    indexes = np.arange(scores.size)
                ranked_indexes = indexes[np.argsort(scores[indexes])[::-1]]
                return [
                    (ids[int(index)], float(scores[int(index)]) * 100.0)
                    for index in ranked_indexes
                    if float(scores[int(index)]) > 0
                ][:limit]
            scored: list[tuple[int, float]] = []
            for rowid, embedding in zip(ids, matrix):
                score = cosine_similarity(query_embedding, embedding)
                if score > 0:
                    scored.append((rowid, score * 100.0))
            scored.sort(key=lambda item: item[1], reverse=True)
            return scored[:limit]
        return []

    def _score_vector_rows(
        self,
        *,
        query_embedding: list[float],
        rows: list[sqlite3.Row],
        limit: int,
    ) -> list[tuple[int, float]]:
        scored: list[tuple[int, float]] = []
        for row in rows:
            embedding = decode_embedding(row["embedding_blob"], row["embedding_json"])
            if not embedding:
                continue
            score = cosine_similarity(query_embedding, embedding)
            if score > 0:
                scored.append((row["id"], score * 100.0))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]

    def _vector_cache_signature(self) -> tuple[int, int, str]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(MAX(id), 0) AS max_id, COALESCE(MAX(indexed_at), '') AS indexed_at
                FROM trajectory_chunks
                WHERE {predicate}
                """.format(predicate=EMBEDDED_WHERE)
            ).fetchone()
        return (int(row["count"] or 0), int(row["max_id"] or 0), str(row["indexed_at"] or ""))

    def _vector_cache_data(self) -> tuple[list[int], Any] | None:
        key = self._vector_cache_signature()
        if key[0] == 0:
            return None
        with self._vector_cache_lock:
            if self._vector_cache_key == key and self._vector_cache_ids and self._vector_cache_matrix is not None:
                return self._vector_cache_ids, self._vector_cache_matrix
            rows = self._select_vector_rows()
            ids: list[int] = []
            vectors: list[list[float]] = []
            for row in rows:
                embedding = decode_embedding(row["embedding_blob"], row["embedding_json"])
                if not embedding:
                    continue
                ids.append(int(row["id"]))
                vectors.append([float(value) for value in embedding])
            if not ids:
                self._vector_cache_key = key
                self._vector_cache_ids = []
                self._vector_cache_matrix = None
                return None
            matrix: Any = vectors
            if np is not None:
                matrix = np.asarray(vectors, dtype="float32")
            self._vector_cache_key = key
            self._vector_cache_ids = ids
            self._vector_cache_matrix = matrix
            return ids, matrix

    def _select_vector_rows(self, *, candidate_rowids: set[int] | None = None) -> list[sqlite3.Row]:
        with self._connect() as conn:
            if candidate_rowids:
                rowids = sorted(candidate_rowids)
                placeholders = ",".join("?" for _ in rowids)
                return conn.execute(
                    f"""
                    SELECT id, embedding_blob, embedding_json
                    FROM trajectory_chunks
                    WHERE {EMBEDDED_WHERE} AND id IN ({placeholders})
                    """,
                    tuple(rowids),
                ).fetchall()
            return conn.execute(
                f"SELECT id, embedding_blob, embedding_json FROM trajectory_chunks WHERE {EMBEDDED_WHERE}"
            ).fetchall()

    def _row_to_payload(self, row: sqlite3.Row, *, score: float, match_kind: str) -> dict[str, Any]:
        try:
            metadata = json.loads(row["metadata_json"])
        except json.JSONDecodeError:
            metadata = {}
        metadata.update(
            {
                "chunk_id": row["id"],
                "chunk_index": row["chunk_index"],
                "event_start": row["event_start"],
                "event_end": row["event_end"],
                "char_start": row["char_start"],
                "char_end": row["char_end"],
                "file_path": row["file_path"],
            }
        )
        return {
            "source_type": "trajectory",
            "source_name": row["source_name"],
            "source_ref": row["source_ref"],
            "parent_source_ref": None,
            "record_kind": "trajectory_chunk",
            "title": row["title"],
            "updated_at": row["updated_at"],
            "cwd": row["cwd"],
            "score": round(score, 4),
            "match_score": round(score, 4),
            "match_kind": match_kind,
            "text_preview": normalize_text(row["text"], 500),
            "summary_short": normalize_text(row["text"], 240),
            "metadata": metadata,
        }
