from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import ceil
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(text: str, limit: int | None = None) -> str:
    collapsed = " ".join((text or "").replace("\r", "\n").split()).strip()
    if limit is None or len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def stable_hash(*parts: str) -> str:
    payload = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass
class NormalizedTurn:
    role: str
    text: str
    timestamp: str | None = None


@dataclass
class NormalizedTrajectory:
    source_name: str
    session_id: str
    title: str
    file_path: str
    updated_at: str
    turns: list[NormalizedTurn]
    short_summary: str
    detailed_summary: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def user_turns(self) -> list[str]:
        return [turn.text for turn in self.turns if turn.role == "user"]

    @property
    def assistant_turns(self) -> list[str]:
        return [turn.text for turn in self.turns if turn.role == "assistant"]

    @property
    def full_transcript(self) -> str:
        return "\n".join(f"{turn.role.upper()}: {turn.text}" for turn in self.turns if turn.text)

    @property
    def compact_transcript(self) -> str:
        if len(self.turns) <= 10:
            return self.full_transcript
        preview = self.turns[:4] + self.turns[-4:]
        return "\n".join(f"{turn.role.upper()}: {turn.text}" for turn in preview if turn.text)

    @property
    def search_text(self) -> str:
        return normalize_text(
            "\n".join(
                [
                    f"Title: {self.title}",
                    f"Short summary: {self.short_summary}",
                    f"Detailed summary: {self.detailed_summary}",
                    f"Transcript: {self.compact_transcript}",
                ]
            ),
            4000,
        )

    def chunk_records(self, *, actor_id: str, now: str, max_chunks: int = 4) -> list["ImportRecord"]:
        if not self.turns:
            return []
        chunk_size = max(1, ceil(len(self.turns) / max_chunks))
        chunks: list[ImportRecord] = []
        session_ref = f"{self.source_name}:{self.session_id}"
        for chunk_index in range(max_chunks):
            start = chunk_index * chunk_size
            end = min(len(self.turns), start + chunk_size)
            if start >= len(self.turns):
                break
            chunk_turns = self.turns[start:end]
            raw_text = "\n".join(f"{turn.role.upper()}: {turn.text}" for turn in chunk_turns if turn.text)
            text = normalize_text(
                "\n".join(
                    [
                        f"Source: {self.source_name}",
                        f"Session title: {self.title}",
                        f"Session ref: {session_ref}",
                        f"Chunk {chunk_index + 1}",
                        raw_text,
                    ]
                ),
                4000,
            )
            content_hash = stable_hash(
                self.source_name,
                self.session_id,
                "chunk",
                str(chunk_index),
                self.updated_at,
                raw_text,
            )
            chunks.append(
                ImportRecord(
                    unique_key=f"trajectory_chunk:{self.source_name}:{self.session_id}:{chunk_index}:{actor_id}",
                    record_kind="chunk",
                    scope="private",
                    source_type="trajectory",
                    source_name=self.source_name,
                    source_ref=f"{session_ref}:chunk:{chunk_index}",
                    parent_source_ref=session_ref,
                    title=f"{self.title} [chunk {chunk_index + 1}]",
                    updated_at=self.updated_at,
                    content_hash=content_hash,
                    summary_short=self.short_summary,
                    summary_detailed=self.detailed_summary,
                    text=text,
                    raw_text=raw_text,
                    tags=[self.source_name, "trajectory", "chunk"],
                    metadata={
                        **self.metadata,
                        "session_id": self.session_id,
                        "chunk_index": chunk_index,
                        "chunk_total": max_chunks,
                        "file_path": self.file_path,
                        "is_recent": True,
                    },
                    owner_actor_id=actor_id,
                    author_actor_id=actor_id,
                    created_at=self.updated_at,
                    imported_at=now,
                    last_seen_at=now,
                )
            )
        return chunks


@dataclass
class ImportRecord:
    unique_key: str
    record_kind: str
    scope: str
    source_type: str
    source_name: str
    source_ref: str
    parent_source_ref: str | None
    title: str
    updated_at: str
    content_hash: str
    summary_short: str
    summary_detailed: str
    text: str
    raw_text: str
    tags: list[str]
    metadata: dict[str, Any]
    owner_actor_id: str | None
    author_actor_id: str | None
    created_at: str
    imported_at: str
    last_seen_at: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "unique_key": self.unique_key,
            "record_kind": self.record_kind,
            "scope": self.scope,
            "source_type": self.source_type,
            "source_name": self.source_name,
            "source_ref": self.source_ref,
            "parent_source_ref": self.parent_source_ref,
            "title": self.title,
            "updated_at": self.updated_at,
            "content_hash": self.content_hash,
            "summary_short": self.summary_short,
            "summary_detailed": self.summary_detailed,
            "text": self.text,
            "raw_text": self.raw_text,
            "tags": self.tags,
            "metadata": self.metadata,
        }


class TrajectorySourceAdapter(ABC):
    def __init__(self, *, account_name: str = "default") -> None:
        self.account_name = account_name
        self._sessions: list[NormalizedTrajectory] = []

    @abstractmethod
    def source_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def discover_sessions(self, max_files: int = 200) -> list[NormalizedTrajectory]:
        raise NotImplementedError

    def to_export_records(
        self,
        *,
        actor_id: str,
        now: str | None = None,
        recent_limit: int = 20,
        chunk_limit: int = 4,
    ) -> list[ImportRecord]:
        stamp = now or utc_now()
        sessions = sorted(self._sessions, key=lambda session: session.updated_at, reverse=True)
        records: list[ImportRecord] = []
        for index, session in enumerate(sessions):
            is_recent = index < recent_limit
            session_ref = f"{session.source_name}:{session.session_id}"
            content_hash = stable_hash(
                session.source_name,
                session.session_id,
                "session",
                session.updated_at,
                session.search_text,
                session.full_transcript,
            )
            records.append(
                ImportRecord(
                    unique_key=f"trajectory_session:{session.source_name}:{session.session_id}:{actor_id}",
                    record_kind="session",
                    scope="private",
                    source_type="trajectory",
                    source_name=session.source_name,
                    source_ref=session_ref,
                    parent_source_ref=None,
                    title=session.title,
                    updated_at=session.updated_at,
                    content_hash=content_hash,
                    summary_short=session.short_summary,
                    summary_detailed=session.detailed_summary,
                    text=session.search_text,
                    raw_text=session.full_transcript,
                    tags=[session.source_name, "trajectory", "session"],
                    metadata={
                        **session.metadata,
                        "session_id": session.session_id,
                        "file_path": session.file_path,
                        "is_recent": is_recent,
                    },
                    owner_actor_id=actor_id,
                    author_actor_id=actor_id,
                    created_at=session.updated_at,
                    imported_at=stamp,
                    last_seen_at=stamp,
                )
            )
            if is_recent:
                records.extend(
                    session.chunk_records(
                        actor_id=actor_id,
                        now=stamp,
                        max_chunks=chunk_limit,
                    )
                )
        return records
