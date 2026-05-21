#!/usr/bin/env python3
"""Semantic memory store for the shared-memory chatbot."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sources.models import ImportRecord
from sources.registry import get_source_adapters, list_source_names
from trajectory_memory import build_index, write_index


VALID_MEMORY_SCOPES = {"private", "shared", "target_user"}
EMBEDDING_PREFIX = "search_document"
QUERY_PREFIX = "search_query"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(text: str, limit: int = 240) -> str:
    collapsed = " ".join((text or "").split()).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    return float(sum(a * b for a, b in zip(left, right)))


def tokenize(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_./:-]{3,}", text or "")
    }


def parse_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, str):
        values = [chunk.strip() for chunk in raw.split(",")]
    else:
        values = [str(raw).strip()]

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = normalize_text(str(value), 64).lower()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


@dataclass
class MemoryRecord:
    unique_key: str
    record_kind: str
    scope: str
    owner_actor_id: str | None
    author_actor_id: str | None
    source_type: str
    source_name: str
    source_ref: str
    parent_source_ref: str | None
    title: str
    summary_short: str
    summary_detailed: str
    text: str
    raw_text: str
    tags: list[str]
    content_hash: str
    created_at: str
    updated_at: str
    imported_at: str
    last_seen_at: str
    metadata: dict[str, Any]
    embedding: list[float]


class LocalEmbedder:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model: Any | None = None
        self._lock = threading.Lock()

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - runtime dependency check
            raise RuntimeError(
                "sentence-transformers is required for semantic memory. "
                "Install it in the project venv: python3 -m pip install -U sentence-transformers"
            ) from exc
        self._model = SentenceTransformer(self.model_name)
        return self._model

    def encode(self, texts: list[str], *, prefix: str) -> list[list[float]]:
        if not texts:
            return []
        prepared = [f"{prefix}: {normalize_text(text, 4000)}" for text in texts]
        with self._lock:
            model = self._load_model()
            vectors = model.encode(
                prepared,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        return [[float(value) for value in vector] for vector in vectors.tolist()]


class SemanticMemoryStore:
    def __init__(
        self,
        *,
        db_path: str,
        model_name: str,
        imported_owner_actor_id: str,
    ) -> None:
        self.db_path = db_path
        self.imported_owner_actor_id = imported_owner_actor_id
        self.embedder = LocalEmbedder(model_name)
        self._lock = threading.Lock()
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
                CREATE TABLE IF NOT EXISTS memory_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    unique_key TEXT NOT NULL UNIQUE,
                    record_kind TEXT NOT NULL DEFAULT 'manual_note',
                    scope TEXT NOT NULL,
                    owner_actor_id TEXT,
                    author_actor_id TEXT,
                    source_type TEXT NOT NULL,
                    source_name TEXT NOT NULL DEFAULT 'manual',
                    source_ref TEXT NOT NULL,
                    parent_source_ref TEXT,
                    title TEXT NOT NULL,
                    summary_short TEXT NOT NULL DEFAULT '',
                    summary_detailed TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL,
                    raw_text TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    content_hash TEXT NOT NULL DEFAULT '',
                    embedding_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    imported_at TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_memory_scope_owner
                ON memory_items (scope, owner_actor_id, updated_at);

                CREATE INDEX IF NOT EXISTS idx_memory_source
                ON memory_items (source_name, source_ref);

                CREATE INDEX IF NOT EXISTS idx_memory_parent_source
                ON memory_items (parent_source_ref);

                CREATE INDEX IF NOT EXISTS idx_memory_record_kind
                ON memory_items (record_kind, updated_at);
                """
            )
            self._ensure_columns(
                conn,
                "memory_items",
                {
                    "record_kind": "TEXT NOT NULL DEFAULT 'manual_note'",
                    "source_name": "TEXT NOT NULL DEFAULT 'manual'",
                    "parent_source_ref": "TEXT",
                    "summary_short": "TEXT NOT NULL DEFAULT ''",
                    "summary_detailed": "TEXT NOT NULL DEFAULT ''",
                    "raw_text": "TEXT NOT NULL DEFAULT ''",
                    "content_hash": "TEXT NOT NULL DEFAULT ''",
                    "imported_at": "TEXT NOT NULL DEFAULT ''",
                    "last_seen_at": "TEXT NOT NULL DEFAULT ''",
                },
            )

    def _ensure_columns(self, conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def rebuild_trajectory_memory(
        self,
        *,
        owner_actor_id: str | None = None,
        max_files_per_tool: int = 200,
        source_names: list[str] | None = None,
        memory_index_path: str | None = None,
        recent_limit: int = 8,
        older_limit: int = 32,
    ) -> dict[str, Any]:
        owner_id = normalize_text(owner_actor_id or self.imported_owner_actor_id, 128) or "local-owner"
        selected_sources = source_names or list_source_names()
        if memory_index_path:
            index = build_index(
                max_files_per_tool=max_files_per_tool,
                recent_limit=recent_limit,
                older_limit=older_limit,
                source_names=selected_sources,
            )
            write_index(index, memory_index_path)

        records: list[ImportRecord] = []
        counts_by_source: dict[str, int] = {name: 0 for name in selected_sources}
        stamp = utc_now()
        for adapter in get_source_adapters(selected_sources):
            sessions = adapter.discover_sessions(max_files=max_files_per_tool)
            counts_by_source[adapter.source_name()] = len(sessions)
            records.extend(
                adapter.to_export_records(
                    actor_id=owner_id,
                    now=stamp,
                    recent_limit=20,
                    chunk_limit=4,
                )
            )

        result = self.import_records(
            actor_id=owner_id,
            source_name="*",
            items=[record.to_payload() for record in records],
            client_id="local-rebuild",
            batch_id=f"local-{stamp}",
            allowed_sources=selected_sources,
        )
        result["imported_sessions_by_source"] = counts_by_source
        return result

    def promote_memory(
        self,
        *,
        actor_id: str,
        scope: str,
        text: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        if scope not in {"private", "shared"}:
            raise ValueError("scope must be 'private' or 'shared'")
        actor = normalize_text(actor_id, 128)
        if not actor:
            raise ValueError("actor_id is required")
        body = normalize_text(text, 4000)
        if not body:
            raise ValueError("text is required")

        stamp = utc_now()
        record = MemoryRecord(
            unique_key=f"manual:{scope}:{actor}:{stamp}",
            record_kind="manual_note",
            scope=scope,
            owner_actor_id=actor if scope == "private" else None,
            author_actor_id=actor,
            source_type="manual_note",
            source_name="manual",
            source_ref=f"manual:{stamp}",
            parent_source_ref=None,
            title=normalize_text(body, 90) or "memory note",
            summary_short=normalize_text(body, 160),
            summary_detailed=body,
            text=body,
            raw_text=body,
            tags=parse_tags(tags),
            content_hash=f"manual:{stamp}",
            created_at=stamp,
            updated_at=stamp,
            imported_at=stamp,
            last_seen_at=stamp,
            metadata={"promoted_via": "memory_promote"},
            embedding=[],
        )
        record.embedding = self.embedder.encode([record.text], prefix=EMBEDDING_PREFIX)[0]

        with self._lock, self._connect() as conn:
            self._upsert_record(conn, record)
            row = conn.execute(
                "SELECT * FROM memory_items WHERE unique_key = ?",
                (record.unique_key,),
            ).fetchone()
        assert row is not None
        return self._row_to_source(row, match_score=None)

    def import_records(
        self,
        *,
        actor_id: str,
        source_name: str,
        items: list[dict[str, Any]],
        client_id: str,
        batch_id: str,
        allowed_sources: list[str] | None = None,
    ) -> dict[str, Any]:
        actor = normalize_text(actor_id, 128)
        if not actor:
            raise ValueError("actor_id is required")
        if not items:
            return {
                "actor_id": actor,
                "client_id": client_id,
                "batch_id": batch_id,
                "accepted": 0,
                "inserted": 0,
                "updated": 0,
                "unchanged": 0,
                "rejected": 0,
                "last_imported_at": utc_now(),
            }

        source_filter = set(allowed_sources or list_source_names())
        stamp = utc_now()
        parsed_records: list[MemoryRecord] = []
        rejected = 0
        for item in items:
            item_source_name = normalize_text(str(item.get("source_name") or source_name), 64).lower()
            if item_source_name not in source_filter:
                rejected += 1
                continue
            try:
                parsed_records.append(
                    self._coerce_import_record(
                        actor_id=actor,
                        source_name=item_source_name,
                        payload=item,
                        stamp=stamp,
                    )
                )
            except ValueError:
                rejected += 1

        inserted = 0
        updated = 0
        unchanged = 0
        accepted = len(parsed_records)

        with self._lock, self._connect() as conn:
            for record in parsed_records:
                existing = conn.execute(
                    "SELECT id, content_hash FROM memory_items WHERE unique_key = ?",
                    (record.unique_key,),
                ).fetchone()
                if existing and existing["content_hash"] == record.content_hash:
                    conn.execute(
                        """
                        UPDATE memory_items
                        SET imported_at = ?, last_seen_at = ?, updated_at = ?, metadata_json = ?
                        WHERE unique_key = ?
                        """,
                        (
                            record.imported_at,
                            record.last_seen_at,
                            record.updated_at,
                            json.dumps(record.metadata),
                            record.unique_key,
                        ),
                    )
                    unchanged += 1
                    continue

                record.embedding = self.embedder.encode([record.text], prefix=EMBEDDING_PREFIX)[0]
                self._upsert_record(conn, record)
                if existing is None:
                    inserted += 1
                else:
                    updated += 1

        return {
            "actor_id": actor,
            "client_id": client_id,
            "batch_id": batch_id,
            "accepted": accepted,
            "inserted": inserted,
            "updated": updated,
            "unchanged": unchanged,
            "rejected": rejected,
            "last_imported_at": stamp,
        }

    def get_sync_status(self, *, actor_id: str, sources: list[str] | None = None) -> dict[str, Any]:
        actor = normalize_text(actor_id, 128)
        if not actor:
            raise ValueError("actor_id is required")
        source_filter = set(source.lower() for source in (sources or list_source_names()))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_name, source_ref, content_hash, imported_at, updated_at, record_kind
                FROM memory_items
                WHERE scope = 'private' AND owner_actor_id = ? AND source_type = 'trajectory'
                ORDER BY imported_at DESC, updated_at DESC
                """,
                (actor,),
            ).fetchall()

        by_source: dict[str, dict[str, Any]] = {}
        for row in rows:
            source = row["source_name"]
            if source not in source_filter:
                continue
            bucket = by_source.setdefault(
                source,
                {
                    "source_name": source,
                    "record_count": 0,
                    "last_imported_at": "",
                    "latest_by_source_ref": {},
                },
            )
            bucket["record_count"] += 1
            if row["imported_at"] > bucket["last_imported_at"]:
                bucket["last_imported_at"] = row["imported_at"]
            if row["source_ref"] not in bucket["latest_by_source_ref"]:
                bucket["latest_by_source_ref"][row["source_ref"]] = {
                    "content_hash": row["content_hash"],
                    "updated_at": row["updated_at"],
                    "record_kind": row["record_kind"],
                }

        return {
            "actor_id": actor,
            "sources": [by_source[name] for name in sorted(by_source)],
        }

    def search(
        self,
        *,
        query: str,
        actor_id: str,
        memory_scope: str,
        target_user_id: str | None = None,
        limit: int = 5,
        min_score: float = 0.20,
    ) -> list[dict[str, Any]]:
        if memory_scope not in VALID_MEMORY_SCOPES:
            raise ValueError("memory_scope must be 'private', 'shared', or 'target_user'")
        query_text = normalize_text(query, 4000)
        if not query_text:
            return []

        if memory_scope == "private":
            owner_id = normalize_text(actor_id, 128)
            rows = self._select_rows(
                "SELECT * FROM memory_items WHERE scope = ? AND owner_actor_id = ?",
                ("private", owner_id),
            )
        elif memory_scope == "shared":
            rows = self._select_rows(
                "SELECT * FROM memory_items WHERE scope = ?",
                ("shared",),
            )
        else:
            owner_id = normalize_text(target_user_id or "", 128)
            if not owner_id:
                raise ValueError("target_user_id is required when memory_scope=target_user")
            rows = self._select_rows(
                "SELECT * FROM memory_items WHERE scope = ? AND owner_actor_id = ?",
                ("private", owner_id),
            )

        if not rows:
            return []

        query_embedding = self.embedder.encode([query_text], prefix=QUERY_PREFIX)[0]
        query_tokens = tokenize(query_text)
        ranked: list[tuple[float, sqlite3.Row]] = []
        all_scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            try:
                embedding = json.loads(row["embedding_json"])
            except json.JSONDecodeError:
                continue
            semantic_score = cosine_similarity(query_embedding, embedding)
            row_tokens = tokenize(
                " ".join(
                    [
                        str(row["title"]),
                        str(row["text"]),
                        str(row["summary_short"]),
                        str(row["summary_detailed"]),
                        str(row["tags_json"]),
                    ]
                )
            )
            lexical_overlap = 0.0
            if query_tokens and row_tokens:
                lexical_overlap = len(query_tokens & row_tokens) / len(query_tokens)
            hybrid_score = (semantic_score * 0.8) + (lexical_overlap * 0.4)
            if row["record_kind"] == "chunk":
                hybrid_score += 0.03
            all_scored.append((hybrid_score, row))
            if hybrid_score >= min_score or lexical_overlap >= 0.25:
                ranked.append((hybrid_score, row))

        if not ranked and len(rows) <= 20:
            fallback = [item for item in all_scored if item[0] >= 0.05]
            fallback.sort(
                key=lambda item: (
                    item[0],
                    item[1]["updated_at"],
                ),
                reverse=True,
            )
            ranked = fallback[:limit * 2]

        ranked.sort(
            key=lambda item: (
                item[0],
                item[1]["updated_at"],
            ),
            reverse=True,
        )

        deduped: list[dict[str, Any]] = []
        seen_refs: set[str] = set()
        for score, row in ranked:
            source = self._row_to_source(row, match_score=score)
            dedupe_key = str(source.get("parent_source_ref") or source.get("source_ref"))
            if dedupe_key in seen_refs:
                continue
            seen_refs.add(dedupe_key)
            deduped.append(source)
            if len(deduped) >= limit:
                break
        return deduped

    def get_stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
            scope_rows = conn.execute(
                "SELECT scope, COUNT(*) AS count FROM memory_items GROUP BY scope"
            ).fetchall()
            source_rows = conn.execute(
                "SELECT source_name, COUNT(*) AS count FROM memory_items GROUP BY source_name"
            ).fetchall()
            kind_rows = conn.execute(
                "SELECT record_kind, COUNT(*) AS count FROM memory_items GROUP BY record_kind"
            ).fetchall()

        return {
            "total": total,
            "by_scope": {row["scope"]: row["count"] for row in scope_rows},
            "by_source_name": {row["source_name"]: row["count"] for row in source_rows},
            "by_record_kind": {row["record_kind"]: row["count"] for row in kind_rows},
            "db_path": self.db_path,
        }

    def _coerce_import_record(
        self,
        *,
        actor_id: str,
        source_name: str,
        payload: dict[str, Any],
        stamp: str,
    ) -> MemoryRecord:
        unique_key = normalize_text(str(payload.get("unique_key") or ""), 300)
        record_kind = normalize_text(str(payload.get("record_kind") or ""), 32).lower()
        source_ref = normalize_text(str(payload.get("source_ref") or ""), 300)
        title = normalize_text(str(payload.get("title") or ""), 200)
        updated_at = normalize_text(str(payload.get("updated_at") or ""), 64)
        content_hash = normalize_text(str(payload.get("content_hash") or ""), 128)
        text = normalize_text(str(payload.get("text") or ""), 4000)
        raw_text = str(payload.get("raw_text") or "")
        summary_short = normalize_text(str(payload.get("summary_short") or ""), 500)
        summary_detailed = normalize_text(str(payload.get("summary_detailed") or ""), 3000)
        parent_source_ref = normalize_text(str(payload.get("parent_source_ref") or ""), 300) or None
        if not unique_key or record_kind not in {"session", "chunk"} or not source_ref or not title or not updated_at or not content_hash or not text:
            raise ValueError("invalid import record")

        return MemoryRecord(
            unique_key=unique_key,
            record_kind=record_kind,
            scope="private",
            owner_actor_id=actor_id,
            author_actor_id=actor_id,
            source_type="trajectory",
            source_name=source_name,
            source_ref=source_ref,
            parent_source_ref=parent_source_ref,
            title=title,
            summary_short=summary_short,
            summary_detailed=summary_detailed,
            text=text,
            raw_text=raw_text,
            tags=parse_tags(payload.get("tags")),
            content_hash=content_hash,
            created_at=updated_at,
            updated_at=updated_at,
            imported_at=stamp,
            last_seen_at=stamp,
            metadata=dict(payload.get("metadata") or {}),
            embedding=[],
        )

    def _select_rows(self, query: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def _upsert_record(self, conn: sqlite3.Connection, record: MemoryRecord) -> None:
        conn.execute(
            """
            INSERT INTO memory_items (
                unique_key,
                record_kind,
                scope,
                owner_actor_id,
                author_actor_id,
                source_type,
                source_name,
                source_ref,
                parent_source_ref,
                title,
                summary_short,
                summary_detailed,
                text,
                raw_text,
                tags_json,
                content_hash,
                embedding_json,
                created_at,
                updated_at,
                imported_at,
                last_seen_at,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(unique_key) DO UPDATE SET
                record_kind = excluded.record_kind,
                scope = excluded.scope,
                owner_actor_id = excluded.owner_actor_id,
                author_actor_id = excluded.author_actor_id,
                source_type = excluded.source_type,
                source_name = excluded.source_name,
                source_ref = excluded.source_ref,
                parent_source_ref = excluded.parent_source_ref,
                title = excluded.title,
                summary_short = excluded.summary_short,
                summary_detailed = excluded.summary_detailed,
                text = excluded.text,
                raw_text = excluded.raw_text,
                tags_json = excluded.tags_json,
                content_hash = excluded.content_hash,
                embedding_json = excluded.embedding_json,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                imported_at = excluded.imported_at,
                last_seen_at = excluded.last_seen_at,
                metadata_json = excluded.metadata_json
            """,
            (
                record.unique_key,
                record.record_kind,
                record.scope,
                record.owner_actor_id,
                record.author_actor_id,
                record.source_type,
                record.source_name,
                record.source_ref,
                record.parent_source_ref,
                record.title,
                record.summary_short,
                record.summary_detailed,
                record.text,
                record.raw_text,
                json.dumps(record.tags),
                record.content_hash,
                json.dumps(record.embedding),
                record.created_at,
                record.updated_at,
                record.imported_at,
                record.last_seen_at,
                json.dumps(record.metadata),
            ),
        )

    def _row_to_source(self, row: sqlite3.Row, *, match_score: float | None) -> dict[str, Any]:
        try:
            tags = json.loads(row["tags_json"])
        except json.JSONDecodeError:
            tags = []
        try:
            metadata = json.loads(row["metadata_json"])
        except json.JSONDecodeError:
            metadata = {}
        return {
            "memory_id": row["id"],
            "record_kind": row["record_kind"],
            "scope": row["scope"],
            "owner_actor_id": row["owner_actor_id"],
            "author_actor_id": row["author_actor_id"],
            "source_type": row["source_type"],
            "source_name": row["source_name"],
            "source_ref": row["source_ref"],
            "parent_source_ref": row["parent_source_ref"],
            "title": row["title"],
            "summary_short": row["summary_short"],
            "summary_detailed": normalize_text(row["summary_detailed"], 220),
            "text_preview": normalize_text(row["text"], 220),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "imported_at": row["imported_at"],
            "last_seen_at": row["last_seen_at"],
            "content_hash": row["content_hash"],
            "tags": tags,
            "metadata": metadata,
            "match_score": round(match_score, 4) if match_score is not None else None,
        }
