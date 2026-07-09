#!/usr/bin/env python3
"""Standalone chat API inspired by OpenClaw-style structure, without runtime dependency."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from app.auth import SyncAuthStore
from app.gui import GUI_HTML, build_gui_state
from app.logging_setup import get_logger
from app.semantic_memory import SemanticMemoryStore, VALID_MEMORY_SCOPES, parse_tags
from app.trajectory_index import TrajectoryChunkIndex
from sources.access import (
    default_config_path,
    load_access_config,
    normalize_visibility_mode,
    save_access_config,
)
from sources.trajectory_lookup import (
    TrajectoryLookup,
    candidate_refs_from_memory,
    focus_query,
    likely_trajectory_question,
    query_tokens,
    read_payload_from_file,
    render_evidence,
)
from trajectory_memory import load_index


log = get_logger("mybot.server", "server.log")

WORKSPACE_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "IDENTITY.md", "MEMORY.md"]
LOCAL_LOOKUP_PRIORITY_THRESHOLD = 50.0
LOCAL_LOOKUP_RANK_BOOST = 60.0
CHUNK_INDEX_RANK_BOOST = 15.0
STATUS_QUERY_RE = re.compile(r"\b(current|latest|recent|status|progress|state|where are we)\b", re.IGNORECASE)
# Deixis / follow-up markers: queries that lean on prior conversation context
# ("more up to date info about the SU?") rather than standing on their own.
FOLLOWUP_QUERY_RE = re.compile(
    r"\b(more|updates?|up to date|up-to-date|latest|still|again|so far|by now|"
    r"any (?:news|update|change)|anything else|recap|remind|that one|those|these|it\b)\b",
    re.IGNORECASE,
)
DEFAULT_SESSION_MAIN_KEY = "main"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-")
    return cleaned or "user"


def normalize_text(text: str, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if limit is None or len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def compact_count(value: int | float) -> str:
    number = float(value)
    if abs(number) >= 1000:
        return f"{number / 1000:.1f}k"
    return str(int(round(number)))


def format_retrieval_budget_summary(budgets: list[dict[str, Any]]) -> str:
    valid = [budget for budget in budgets if isinstance(budget, dict)]
    if not valid:
        return ""

    total_calls = sum(int(budget.get("tool_calls") or 1) for budget in valid)
    total_seconds = sum(float(budget.get("seconds") or 0.0) for budget in valid)
    total_tokens = sum(int(budget.get("tokens_estimate") or 0) for budget in valid)

    details: list[str] = []
    for budget in valid[:4]:
        tool = normalize_text(str(budget.get("tool") or "lookup"), 32)
        seconds = float(budget.get("seconds") or 0.0)
        tokens = int(budget.get("tokens_estimate") or 0)
        result_count = budget.get("result_count")
        suffix = ""
        if isinstance(result_count, int):
            suffix = f", {result_count} result{'s' if result_count != 1 else ''}"
        details.append(f"{tool}: {seconds:.2f}s, ~{compact_count(tokens)} tokens{suffix}")
    if len(valid) > 4:
        details.append(f"{len(valid) - 4} more")

    return (
        f"Retrieval budget: {total_calls} tool call{'s' if total_calls != 1 else ''}, "
        f"{total_seconds:.2f}s, ~{compact_count(total_tokens)} tokens estimated"
        + (f" ({'; '.join(details)})." if details else ".")
    )


def append_retrieval_budget_summary(answer: str, budgets: list[dict[str, Any]]) -> str:
    summary = format_retrieval_budget_summary(budgets)
    if not summary:
        return answer
    if not answer.strip():
        return summary
    return f"{answer.rstrip()}\n\n{summary}"


def trajectory_status_query(query: str) -> bool:
    return bool(STATUS_QUERY_RE.search(query or ""))


def trajectory_rank_score(source: dict[str, Any], *, query: str) -> float:
    score = float(source.get("match_score") or source.get("score") or 0.0)
    tokens = set(query_tokens(query))
    title_tokens = set(re.findall(r"[A-Za-z0-9_]{3,}", str(source.get("title") or "").lower()))
    cwd_tokens = set(re.findall(r"[A-Za-z0-9_]{3,}", str(source.get("cwd") or "").lower()))
    score += len(tokens & title_tokens) * 28.0
    score += len(tokens & cwd_tokens) * 8.0
    if likely_trajectory_question(query):
        if source.get("_local_lookup"):
            score += LOCAL_LOOKUP_RANK_BOOST
        if source.get("_chunk_index"):
            score += CHUNK_INDEX_RANK_BOOST
    return score


def trajectory_source_ref(source: dict[str, Any]) -> str:
    source_ref = str(source.get("parent_source_ref") or source.get("source_ref") or "")
    if ":chunk:" in source_ref:
        source_ref = source_ref.split(":chunk:", 1)[0]
    return source_ref


def raw_match_score(source: dict[str, Any]) -> float:
    return float(source.get("match_score") or source.get("score") or 0.0)


def merge_trajectory_source(
    by_ref: dict[str, dict[str, Any]],
    source: dict[str, Any],
    *,
    query: str,
    channel: str,
) -> None:
    if source.get("source_type") != "trajectory":
        return
    source_ref = trajectory_source_ref(source)
    if not source_ref:
        return

    item = dict(source)
    item["source_ref"] = source_ref
    item["parent_source_ref"] = None
    item_metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    effective_cwd = str(item.get("cwd") or item_metadata.get("cwd") or "")
    item["cwd"] = effective_cwd
    channels = set(item.get("_retrieval_channels") or [])
    if channel:
        channels.add(channel)
    item["_retrieval_channels"] = sorted(channels)

    existing = by_ref.get(source_ref)
    if existing is None:
        by_ref[source_ref] = item
        return

    existing_metadata = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
    if existing_metadata or item_metadata:
        item["metadata"] = {**existing_metadata, **item_metadata}
        existing["metadata"] = {**item_metadata, **existing_metadata}
    existing_metadata = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
    existing["cwd"] = str(existing.get("cwd") or existing_metadata.get("cwd") or "")
    item_metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    item["cwd"] = str(item.get("cwd") or item_metadata.get("cwd") or "")

    merged_channels = set(existing.get("_retrieval_channels") or []) | set(item.get("_retrieval_channels") or [])
    existing["_retrieval_channels"] = sorted(merged_channels)
    item["_retrieval_channels"] = sorted(merged_channels)

    existing_is_chunk = bool(existing.get("record_kind") == "trajectory_chunk" or existing.get("_chunk_index"))
    item_is_chunk = bool(item.get("record_kind") == "trajectory_chunk" or item.get("_chunk_index"))
    if existing.get("_local_lookup") or item.get("_local_lookup"):
        existing["_local_lookup"] = True
        item["_local_lookup"] = True
    if existing.get("_chunk_index") or item.get("_chunk_index"):
        existing["_chunk_index"] = True
        item["_chunk_index"] = True

    if existing_is_chunk and not item_is_chunk:
        return
    if item_is_chunk and not existing_is_chunk:
        by_ref[source_ref] = item
        return

    if raw_match_score(item) > raw_match_score(existing):
        by_ref[source_ref] = item


def sort_trajectory_sources(sources: list[dict[str, Any]], *, query: str, limit: int | None = None) -> list[dict[str, Any]]:
    ranked = sorted(
        sources,
        key=lambda item: (
            trajectory_rank_score(item, query=query),
            str(item.get("updated_at") or ""),
        ),
        reverse=True,
    )
    return ranked[:limit] if limit is not None else ranked


def trajectory_query_variants(query: str) -> list[str]:
    variants: list[str] = []

    def add(value: str) -> None:
        value = normalize_text(value, 300).strip(" ?.'\"")
        if value and value.lower() not in {variant.lower() for variant in variants}:
            variants.append(value)

    add(query)
    add(focus_query(query))
    lowered = query.lower()
    prefix_patterns = [
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
    ]
    for pattern in prefix_patterns:
        stripped = re.sub(pattern, "", lowered, count=1, flags=re.IGNORECASE).strip(" ?.'\"")
        if stripped != lowered:
            add(stripped)
    quoted = re.findall(r'"([^"]{6,})"', query)
    for phrase in quoted[:3]:
        add(phrase)
    tokens = query_tokens(query)
    if len(tokens) >= 3:
        add(" ".join(tokens[:10]))
    focused = focus_query(query) or query
    if trajectory_status_query(query):
        add(f"latest current status progress {focused}")
    if "allocation" in lowered or "allocations" in lowered:
        add(f"{focused} allocation balance remaining current balance insufficient balance out of allocation")
        if "cluster" in lowered:
            add("Cluster allocation current balance insufficient balance out of allocation no new jobs")
    if "platform2" in lowered and ("migration" in lowered or "progress" in lowered or "status" in lowered):
        add(f"{focused} Platform2 migration running works validated only platform")
        add("Platform2 migration progress only platform training queued running")
    return variants[:8]


def coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def safe_session_key(user: str, requested: str | None = None, new_session: bool = False) -> str:
    if requested:
        return slugify(requested)
    base = slugify(user or DEFAULT_SESSION_MAIN_KEY)
    if new_session:
        return f"{base}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    return base


def parse_memory_scope(raw: Any) -> str:
    value = normalize_text(str(raw or "private"), 32).lower()
    if value not in VALID_MEMORY_SCOPES:
        raise ValueError("memory_scope must be 'private', 'shared', or 'target_user'")
    return value


def render_history_transcript(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user").upper()
        content = normalize_text(str(message.get("content") or ""), 4000)
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


@dataclass
class AppConfig:
    host: str
    port: int
    provider_backend: str
    model_base_url: str
    model_api_key: str | None
    model_name: str | None
    model_api_style: str
    workspace_dir: str
    state_dir: str
    memory_index_path: str
    memory_db_path: str
    trajectory_index_db_path: str
    history_max_messages: int
    history_max_chars: int
    autobuild_memory: bool
    codex_command: str
    codex_cwd: str
    codex_sandbox: str
    codex_network_access: bool
    codex_permission_profile: str
    codex_ignore_user_config: bool
    codex_disable_backend_resume: bool
    codex_ephemeral: bool
    codex_service_tier: str
    mybot_tool_python: str
    embedding_model_name: str
    imported_owner_actor_id: str
    session_tail_pairs: int
    compaction_trigger_message_count: int
    compaction_trigger_char_count: int
    memory_match_limit: int
    trajectory_max_files_per_tool: int
    trajectory_sources: list[str]
    trajectory_investigation_mode: str
    trajectory_search_limit: int
    trajectory_evidence_limit: int
    trajectory_evidence_chars: int
    trajectory_index_chunk_chars: int
    trajectory_index_overlap_chars: int
    trajectory_index_autobuild: bool
    trajectory_index_autobuild_vectors: bool
    trajectory_index_autobuild_min_interval_seconds: int
    trajectory_index_refresh_max_sessions: int
    trajectory_index_background_refresh_seconds: int
    trajectory_agentic_search: bool
    trajectory_agentic_max_steps: int
    trajectory_query_planner: bool
    trajectory_query_planner_max_rounds: int
    trajectory_query_planner_max_queries: int
    trajectory_search_time_budget_seconds: float
    agentic_tool_routing: bool
    mybot_tool_path: str
    sync_tokens_path: str


class PromptWorkspace:
    def __init__(self, workspace_dir: str) -> None:
        self.workspace_dir = Path(workspace_dir)

    def _read(self, name: str) -> str:
        path = self.workspace_dir / name
        if not path.exists():
            return ""
        return path.read_text().strip()

    def render(self) -> str:
        sections: list[str] = []
        for name in WORKSPACE_FILES:
            content = self._read(name)
            if content:
                sections.append(f"## {name}\n{content}")
        return "\n\n".join(sections)


class SessionStore:
    def __init__(self, state_dir: str) -> None:
        self.state_dir = Path(state_dir)
        self.sessions_dir = self.state_dir / "sessions"
        self.archived_dir = self.state_dir / "archived_sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.archived_dir.mkdir(parents=True, exist_ok=True)

    def session_path(self, session_key: str) -> Path:
        return self.sessions_dir / f"{slugify(session_key)}.jsonl"

    def _iter_entries(self, session_key: str) -> list[dict[str, Any]]:
        path = self.session_path(session_key)
        if not path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with path.open() as handle:
            for line in handle:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries

    def _ensure_meta(self, session_key: str, user: str) -> None:
        path = self.session_path(session_key)
        if path.exists():
            return
        meta = {
            "type": "session_meta",
            "session_key": session_key,
            "user": user,
            "created_at": utc_now(),
        }
        with path.open("a") as handle:
            handle.write(json.dumps(meta) + "\n")

    def _append_event(self, session_key: str, user: str, event: dict[str, Any]) -> None:
        self._ensure_meta(session_key, user)
        with self.session_path(session_key).open("a") as handle:
            handle.write(json.dumps(event) + "\n")

    def append_message(
        self,
        session_key: str,
        user: str,
        role: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self._append_event(
            session_key,
            user,
            {
                "type": "message",
                "role": role,
                "content": content,
                "timestamp": utc_now(),
                "meta": meta or {},
            },
        )

    def set_backend_state(self, session_key: str, user: str, provider: str, backend_session_id: str) -> None:
        self._append_event(
            session_key,
            user,
            {
                "type": "session_backend",
                "provider": provider,
                "backend_session_id": backend_session_id,
                "timestamp": utc_now(),
            },
        )

    def get_backend_state(self, session_key: str) -> dict[str, Any] | None:
        latest: dict[str, Any] | None = None
        for entry in self._iter_entries(session_key):
            if entry.get("type") == "session_backend":
                latest = entry
        return latest

    def set_session_summary(
        self,
        session_key: str,
        user: str,
        *,
        summary: str,
        compacted_message_count: int,
    ) -> dict[str, Any]:
        event = {
            "type": "session_summary",
            "summary": summary,
            "compacted_message_count": compacted_message_count,
            "timestamp": utc_now(),
        }
        self._append_event(session_key, user, event)
        return event

    def get_session_summary(self, session_key: str) -> dict[str, Any] | None:
        latest: dict[str, Any] | None = None
        for entry in self._iter_entries(session_key):
            if entry.get("type") == "session_summary":
                latest = entry
        return latest

    def load_messages(self, session_key: str, limit: int | None = None) -> list[dict[str, Any]]:
        messages = [entry for entry in self._iter_entries(session_key) if entry.get("type") == "message"]
        if limit is None:
            return messages
        return messages[-limit:]

    def archive_session(self, session_key: str) -> str | None:
        path = self.session_path(session_key)
        if not path.exists():
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archived = self.archived_dir / f"{slugify(session_key)}-{stamp}.jsonl"
        os.replace(path, archived)
        return str(archived)


class ProviderClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    @staticmethod
    def _toml_string(value: str) -> str:
        return json.dumps(value)

    @classmethod
    def _toml_inline_table(cls, mapping: dict[str, str | bool]) -> str:
        items: list[str] = []
        for key, value in mapping.items():
            if isinstance(value, bool):
                rendered_value = "true" if value else "false"
            else:
                rendered_value = cls._toml_string(value)
            items.append(f"{cls._toml_string(key)}={rendered_value}")
        return "{" + ",".join(items) + "}"

    def _codex_permission_workspace_roots(self) -> dict[str, bool]:
        root = Path(self.config.codex_cwd).expanduser().resolve()
        return {str(root): False} if root.exists() and root.is_dir() else {}

    def _codex_permission_profile_args(self) -> list[str]:
        profile = self.config.codex_permission_profile.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]+", profile):
            raise RuntimeError("CODEX_PERMISSION_PROFILE must contain only letters, numbers, and underscores")

        tool_path = Path(self.config.mybot_tool_path).expanduser().resolve()
        project_root = Path(__file__).resolve().parent.parent
        filesystem = {
            str(project_root): "deny",
            str(tool_path): "read",
            str(project_root / ".env"): "deny",
            str(project_root / ".discord.env"): "deny",
            str(project_root / "config"): "deny",
            str(project_root / "state"): "deny",
            "~/.codex": "deny",
            "~/.claude": "deny",
        }
        workspace_roots = self._codex_permission_workspace_roots()
        args = [
            "-c",
            'approval_policy="never"',
            "-c",
            f"default_permissions={self._toml_string(profile)}",
            "-c",
            f'permissions.{profile}.extends=":workspace"',
            "-c",
            f"permissions.{profile}.workspace_roots={self._toml_inline_table(workspace_roots)}",
            "-c",
            f"permissions.{profile}.filesystem={self._toml_inline_table(filesystem)}",
        ]
        if self.config.codex_network_access:
            args.extend(
                [
                    "-c",
                    f"permissions.{profile}.network.enabled=true",
                    "-c",
                    f"permissions.{profile}.network.domains="
                    + self._toml_inline_table({"localhost": "allow", "127.0.0.1": "allow"}),
                ]
            )
        return args

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(url=url, data=json.dumps(payload).encode("utf-8"), method="POST")
        request.add_header("Content-Type", "application/json")
        if self.config.model_api_key:
            request.add_header("Authorization", f"Bearer {self.config.model_api_key}")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Provider HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Provider connection failed: {exc}") from exc

    def _extract_chat_text(self, response: dict[str, Any]) -> str:
        choices = response.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            chunks = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        chunks.append(text.strip())
            return "\n\n".join(chunks).strip()
        return ""

    def _extract_responses_text(self, response: dict[str, Any]) -> str:
        if isinstance(response.get("output_text"), str) and response["output_text"].strip():
            return response["output_text"].strip()
        chunks: list[str] = []
        for item in response.get("output", []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if isinstance(content, dict):
                        text = content.get("text")
                        if isinstance(text, str) and text.strip():
                            chunks.append(text.strip())
        return "\n\n".join(chunks).strip()

    def _build_codex_prompt(self, system_prompt: str, history: list[dict[str, Any]], message: str) -> str:
        parts: list[str] = []
        if system_prompt:
            parts.append(f"System and memory context for this chat service:\n{system_prompt}")
        transcript = render_history_transcript(history)
        if transcript:
            parts.append(f"Conversation so far:\n{transcript}")
        parts.append(f"User message:\n{message}")
        parts.append("Respond directly to the user.")
        return "\n\n".join(parts)

    def _run_codex(
        self,
        *,
        prompt: str,
        backend_session_id: str | None,
        ephemeral: bool = False,
        tool_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self.config.codex_disable_backend_resume:
            backend_session_id = None

        cmd = [self.config.codex_command]
        if self.config.codex_service_tier:
            cmd.extend(["-c", f'service_tier="{self.config.codex_service_tier}"'])
        if self.config.codex_permission_profile:
            cmd.extend(self._codex_permission_profile_args())
        elif self.config.codex_sandbox == "workspace-write" and self.config.codex_network_access:
            cmd.extend(["-c", "sandbox_workspace_write.network_access=true"])
        cmd.extend(["-C", self.config.codex_cwd, "exec"])
        if backend_session_id:
            cmd.append("resume")
            cmd.extend(["--skip-git-repo-check", "--json"])
            if self.config.codex_ignore_user_config:
                cmd.append("--ignore-user-config")
                cmd.append("--ignore-rules")
        else:
            cmd.extend(["--skip-git-repo-check", "--json"])
            if self.config.codex_ignore_user_config:
                cmd.append("--ignore-user-config")
                cmd.append("--ignore-rules")
            if ephemeral or self.config.codex_ephemeral:
                cmd.append("--ephemeral")
            if not self.config.codex_permission_profile:
                cmd.extend(["--sandbox", self.config.codex_sandbox])
        if self.config.model_name:
            cmd.extend(["--model", self.config.model_name])
        if backend_session_id:
            cmd.append(backend_session_id)
        cmd.append(prompt)

        env = os.environ.copy()
        if tool_env:
            env.update(tool_env)
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            stderr = normalize_text(proc.stderr, 1500)
            stdout = normalize_text(proc.stdout, 1500)
            raise RuntimeError(f"Codex CLI failed (exit {proc.returncode}). stderr={stderr} stdout={stdout}")

        events = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        thread_id = backend_session_id
        text = ""
        usage = None
        for event in events:
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id", thread_id)
            elif event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    text = item.get("text", text)
            elif event.get("type") == "turn.completed":
                usage = event.get("usage")

        return {
            "text": text.strip(),
            "raw": events,
            "provider_style": "codex_cli",
            "backend_session_id": None if self.config.codex_disable_backend_resume else thread_id,
            "usage": usage,
        }

    def complete(
        self,
        *,
        system_prompt: str,
        history: list[dict[str, Any]],
        message: str,
        backend_session_id: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        ephemeral: bool = False,
        tool_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self.config.provider_backend == "codex_cli":
            prompt = self._build_codex_prompt(system_prompt=system_prompt, history=history, message=message)
            return self._run_codex(
                prompt=prompt,
                backend_session_id=backend_session_id,
                ephemeral=ephemeral,
                tool_env=tool_env,
            )

        base_url = self.config.model_base_url.rstrip("/")
        if self.config.model_api_style == "responses":
            transcript = render_history_transcript(history)
            instructions = system_prompt
            if transcript:
                instructions = f"{system_prompt}\n\nConversation so far:\n{transcript}"
            payload: dict[str, Any] = {
                "model": self.config.model_name,
                "input": message,
                "instructions": instructions,
            }
            if max_output_tokens is not None:
                payload["max_output_tokens"] = max_output_tokens
            raw = self._post_json(f"{base_url}/responses", payload)
            return {"text": self._extract_responses_text(raw), "raw": raw, "provider_style": "responses"}

        messages = [{"role": "system", "content": system_prompt}]
        for entry in history:
            messages.append({"role": entry["role"], "content": entry["content"]})
        messages.append({"role": "user", "content": message})
        payload = {"model": self.config.model_name, "messages": messages}
        if max_output_tokens is not None:
            payload["max_tokens"] = max_output_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        raw = self._post_json(f"{base_url}/chat/completions", payload)
        return {"text": self._extract_chat_text(raw), "raw": raw, "provider_style": "chat_completions"}

    def chat(
        self,
        *,
        system_prompt: str,
        history: list[dict[str, Any]],
        message: str,
        backend_session_id: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        tool_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self.complete(
            system_prompt=system_prompt,
            history=history,
            message=message,
            backend_session_id=backend_session_id,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            ephemeral=False,
            tool_env=tool_env,
        )

    def summarize_conversation(self, *, existing_summary: str, new_messages: list[dict[str, Any]]) -> str:
        transcript = render_history_transcript(new_messages)
        prompt = (
            "Existing summary:\n"
            f"{existing_summary or '(none)'}\n\n"
            "Newly compacted conversation turns:\n"
            f"{transcript or '(none)'}\n\n"
            "Write an updated rolling summary for future chat turns. "
            "Preserve decisions, user preferences, factual claims, unresolved questions, "
            "and technical identifiers. Do not invent anything. Keep it concise and useful."
        )
        result = self.complete(
            system_prompt=(
                "You summarize prior chat context for a grounded assistant. "
                "Return plain text only."
            ),
            history=[],
            message=prompt,
            backend_session_id=None,
            max_output_tokens=500,
            ephemeral=True,
        )
        return normalize_text(result["text"], 2500)


class AppState:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.ensure_runtime_tool_wrapper()
        self.lock = threading.Lock()
        self.workspace = PromptWorkspace(config.workspace_dir)
        self.sessions = SessionStore(config.state_dir)
        self.provider = ProviderClient(config)
        self.sync_auth = SyncAuthStore(config.sync_tokens_path)
        self.memory_store = SemanticMemoryStore(
            db_path=config.memory_db_path,
            model_name=config.embedding_model_name,
            imported_owner_actor_id=config.imported_owner_actor_id,
        )
        self.trajectory_lookup = TrajectoryLookup(
            source_names=config.trajectory_sources,
            max_files_per_tool=config.trajectory_max_files_per_tool,
        )
        self.trajectory_chunk_index = TrajectoryChunkIndex(
            db_path=config.trajectory_index_db_path,
            model_name=config.embedding_model_name,
            source_names=config.trajectory_sources,
            max_files_per_tool=config.trajectory_max_files_per_tool,
            chunk_chars=config.trajectory_index_chunk_chars,
            overlap_chars=config.trajectory_index_overlap_chars,
        )
        self.access_config = load_access_config()
        self.memory_index: dict[str, Any] | None = None
        self.trajectory_index_maintenance_lock = threading.Lock()
        self.trajectory_index_background_started = False
        self.maintenance_lock = threading.Lock()
        self.maintenance_status: dict[str, Any] = {
            "running": False,
            "action": "",
            "started_at": "",
            "finished_at": "",
            "result": None,
            "error": None,
        }
        self.known_projects_lock = threading.Lock()
        self.known_projects_path = Path(config.state_dir) / "known_projects.json"
        self.background_refresh_error: dict[str, Any] | None = None
        self.background_refresh_last_ok: str = ""

    def ensure_runtime_tool_wrapper(self) -> None:
        codex_cwd = Path(self.config.codex_cwd).expanduser().resolve()
        codex_cwd.mkdir(parents=True, exist_ok=True)

        source = Path(__file__).resolve().parent.parent / "scripts" / "mybot_tool.py"
        destination = Path(self.config.mybot_tool_path).expanduser().resolve()
        if destination == source:
            return

        destination.parent.mkdir(parents=True, exist_ok=True)
        source_text = source.read_text(encoding="utf-8")
        try:
            existing_text = destination.read_text(encoding="utf-8")
        except OSError:
            existing_text = None
        if existing_text != source_text:
            destination.write_text(source_text, encoding="utf-8")
            shutil.copymode(source, destination)

    def rebuild_memory(
        self,
        *,
        owner_actor_id: str | None = None,
        recent_limit: int = 8,
        older_limit: int = 32,
        max_files_per_tool: int | None = None,
    ) -> dict[str, Any]:
        max_files = max_files_per_tool or self.config.trajectory_max_files_per_tool
        semantic = self.memory_store.rebuild_trajectory_memory(
            owner_actor_id=owner_actor_id or self.config.imported_owner_actor_id,
            max_files_per_tool=max_files,
            source_names=self.config.trajectory_sources,
            memory_index_path=self.config.memory_index_path,
            recent_limit=recent_limit,
            older_limit=older_limit,
        )
        index = load_index(self.config.memory_index_path)
        with self.lock:
            self.memory_index = index
        self.trajectory_lookup.clear()
        return {"index": index, "semantic": semantic}

    def rebuild_trajectory_chunk_index(self, *, include_vectors: bool = True) -> dict[str, Any]:
        result = self.trajectory_chunk_index.rebuild(include_vectors=include_vectors)
        self.trajectory_lookup.clear()
        return result

    def refresh_trajectory_chunk_index(self, *, include_vectors: bool = False) -> dict[str, Any]:
        result = self.trajectory_chunk_index.refresh_changed(
            include_vectors=include_vectors,
            max_sessions=self.config.trajectory_index_refresh_max_sessions,
        )
        if result.get("sessions_updated") or result.get("sessions_removed"):
            self.trajectory_lookup.clear()
        return result

    def backfill_trajectory_chunk_vectors(self, *, limit: int = 256) -> dict[str, Any]:
        return self.trajectory_chunk_index.backfill_vectors(limit=limit)

    def _load_known_projects(self) -> dict[str, Any]:
        try:
            with self.known_projects_path.open() as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("projects", {})
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"baselined": False, "projects": {}}

    def _save_known_projects(self, data: dict[str, Any]) -> None:
        self.known_projects_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.known_projects_path.with_suffix(".json.tmp")
        with tmp.open("w") as handle:
            json.dump(data, handle, indent=2)
        tmp.replace(self.known_projects_path)

    def detect_new_projects(self) -> list[dict[str, Any]]:
        """Diff currently-indexed projects against what we've seen before.

        Under blacklist-by-default a new project is silently indexed the moment
        you open a coding session in a new directory; this surfaces that as a
        pending-review event (the signal a macOS app turns into a notification).
        The first run baselines everything so existing projects don't all flag.
        """
        try:
            breakdown = self.trajectory_chunk_index.project_breakdown()
        except Exception:  # pragma: no cover - detection stays best-effort
            return []
        with self.known_projects_lock:
            data = self._load_known_projects()
            projects = data.setdefault("projects", {})
            first_run = not data.get("baselined")
            changed = False
            for entry in breakdown:
                cwd = str(entry.get("cwd") or "").strip()
                if not cwd:
                    continue
                source_name = str(entry.get("source_name") or "")
                key = f"{source_name}:{cwd}"
                sessions = int(entry.get("sessions") or 0)
                existing = projects.get(key)
                if existing is None:
                    projects[key] = {
                        "source_name": source_name,
                        "cwd": cwd,
                        "first_seen": utc_now(),
                        "sessions": sessions,
                        "reviewed": bool(first_run),
                    }
                    changed = True
                elif existing.get("sessions") != sessions:
                    existing["sessions"] = sessions
                    changed = True
            if first_run:
                data["baselined"] = True
                changed = True
            if changed:
                self._save_known_projects(data)
            pending = [entry for entry in projects.values() if not entry.get("reviewed")]
        pending.sort(key=lambda item: str(item.get("first_seen") or ""), reverse=True)
        return pending

    def review_project(self, *, source_name: str, cwd: str, decision: str) -> dict[str, Any]:
        source_name = (source_name or "").strip().lower()
        cwd = (cwd or "").strip()
        decision = (decision or "").strip().lower()
        key = f"{source_name}:{cwd}"
        with self.known_projects_lock:
            data = self._load_known_projects()
            entry = data.get("projects", {}).get(key)
            if entry is not None:
                entry["reviewed"] = True
                self._save_known_projects(data)
        result: dict[str, Any] = {"ok": True, "decision": decision, "key": key}
        if decision == "exclude":
            result["scope_update"] = self.update_scope(
                action="exclude", source_name=source_name, kind="workdir", value=cwd
            )
        result["new_projects"] = self.detect_new_projects()
        return result

    def reload_access_config(self) -> None:
        """Re-read the access policy at runtime and drop cached session lists so
        the next discovery honors the change without a server restart."""
        self.access_config = load_access_config()
        self.trajectory_lookup.clear()
        self.trajectory_chunk_index.lookup.clear()

    def _session_allowed_by_policy(self, source_name: str, session_id: str, cwd: str) -> bool:
        accounts = self.access_config.accounts_for_source(source_name)
        if not accounts:
            return False
        ref_id = session_id.split(":", 1)[1] if ":" in session_id else session_id
        return any(
            account.include_workdir(cwd)
            and not account.is_session_blocked(session_id, ref_id)
            for account in accounts
        )

    def purge_index_to_policy(self) -> dict[str, Any]:
        return self.trajectory_chunk_index.purge_disallowed(self._session_allowed_by_policy)

    def scope_summary(self) -> dict[str, Any]:
        accounts: list[dict[str, Any]] = []
        for source_name, source_accounts in sorted(self.access_config.sources.items()):
            for account in source_accounts:
                payload = account.to_json()
                payload["source_name"] = source_name
                accounts.append(payload)
        try:
            projects = self.trajectory_chunk_index.project_breakdown()
        except Exception:  # pragma: no cover - status surface stays available
            projects = []
        try:
            new_projects = self.detect_new_projects()
        except Exception:  # pragma: no cover - status surface stays available
            new_projects = []
        return {
            "config_path": str(default_config_path()),
            "accounts": accounts,
            "indexed_projects": projects,
            "new_projects": new_projects,
        }

    def update_scope(
        self,
        *,
        action: str,
        source_name: str,
        account_name: str = "default",
        kind: str = "",
        value: str = "",
    ) -> dict[str, Any]:
        """Mutate the access policy, persist it, reload live, and reconcile the
        index. Exclusions purge immediately; re-inclusions kick a re-index."""
        action = (action or "").strip().lower()
        source_name = (source_name or "").strip().lower()
        config = load_access_config()
        source_accounts = config.sources.get(source_name) or []
        if not source_accounts:
            return {"ok": False, "error": f"unknown source '{source_name}'"}
        account = next(
            (item for item in source_accounts if item.name == account_name),
            source_accounts[0],
        )
        value = str(value or "").strip()

        if action == "set_visibility":
            account.visibility_mode = normalize_visibility_mode(value)
        elif action in {"exclude", "unexclude"}:
            field_by_kind = {
                "workdir": account.excluded_workdirs,
                "session": account.excluded_session_ids,
                "workdir_class": account.excluded_workdir_classes,
                "entrypoint": account.excluded_entrypoints,
            }
            target = field_by_kind.get(kind)
            if target is None:
                return {"ok": False, "error": f"unknown kind '{kind}'"}
            if not value:
                return {"ok": False, "error": "value is required"}
            normalized = value if kind in {"workdir", "session"} else value.lower()
            if kind == "session" and ":" in normalized:
                normalized = normalized.split(":", 1)[1]
            if action == "exclude":
                if normalized not in target:
                    target.append(normalized)
            else:
                lowered = normalized.lower()
                target[:] = [item for item in target if item.lower() != lowered]
        else:
            return {"ok": False, "error": f"unknown action '{action}'"}

        save_access_config(config)
        self.reload_access_config()
        purge = self.purge_index_to_policy()
        # A re-inclusion or a visibility loosening can make sessions newly
        # visible; re-index them in the background so they come back.
        reindex = action == "unexclude" or action == "set_visibility"
        if reindex:
            self.start_trajectory_maintenance("refresh")
        return {
            "ok": True,
            "action": action,
            "purge": purge,
            "reindexing": reindex,
            "scope": self.scope_summary(),
        }

    def backfill_all_trajectory_vectors(
        self, *, batch: int = 512, max_batches: int = 400
    ) -> dict[str, Any]:
        """Fill every missing embedding, batch by batch, until caught up."""
        total = 0
        for _ in range(max_batches):
            result = self.backfill_trajectory_chunk_vectors(limit=batch)
            updated = int(result.get("updated_chunks") or 0)
            total += updated
            if updated == 0:
                break
        return {
            "updated_chunks_total": total,
            "stats": self.trajectory_chunk_index.stats(),
        }

    def start_trajectory_maintenance(self, action: str) -> dict[str, Any]:
        """Kick off a slow index job (rebuild/refresh/embed) in the background.

        Returns immediately; the GUI polls stats to observe progress.
        """
        action = (action or "").strip().lower()
        if action not in {"rebuild", "refresh", "embed", "compact", "compact_embeddings"}:
            return {"ok": False, "error": f"unknown maintenance action: {action}"}
        with self.maintenance_lock:
            if self.maintenance_status.get("running"):
                return {
                    "ok": False,
                    "error": "a maintenance job is already running",
                    "status": dict(self.maintenance_status),
                }
            self.maintenance_status = {
                "running": True,
                "action": action,
                "started_at": utc_now(),
                "finished_at": "",
                "result": None,
                "error": None,
            }

        def worker() -> None:
            result: Any = None
            error: str | None = None
            try:
                if action == "rebuild":
                    result = self.rebuild_trajectory_chunk_index(include_vectors=True)
                elif action == "refresh":
                    result = self.refresh_trajectory_chunk_index(
                        include_vectors=self.config.trajectory_index_autobuild_vectors
                    )
                    self.backfill_all_trajectory_vectors()
                elif action == "embed":
                    result = self.backfill_all_trajectory_vectors()
                elif action == "compact_embeddings":
                    converted = self.trajectory_chunk_index.compact_embeddings()
                    reclaimed = self.trajectory_chunk_index.vacuum()
                    result = {**converted, **reclaimed}
                else:  # compact
                    result = self.trajectory_chunk_index.vacuum()
            except Exception as exc:  # pragma: no cover - best-effort background job
                error = str(exc)
            with self.maintenance_lock:
                started_at = self.maintenance_status.get("started_at", "")
                self.maintenance_status = {
                    "running": False,
                    "action": action,
                    "started_at": started_at,
                    "finished_at": utc_now(),
                    "result": result,
                    "error": error,
                }

        threading.Thread(
            target=worker, name=f"trajectory-maintenance-{action}", daemon=True
        ).start()
        with self.maintenance_lock:
            return {"ok": True, "status": dict(self.maintenance_status)}

    def ensure_trajectory_chunk_index_current(self) -> dict[str, Any]:
        freshness = self.trajectory_chunk_index.freshness()
        if not self.config.trajectory_index_autobuild or not freshness.get("stale"):
            return {"rebuilt": False, "freshness": freshness}
        indexed_age = freshness.get("indexed_age_seconds")
        min_interval = max(0, self.config.trajectory_index_autobuild_min_interval_seconds)
        if isinstance(indexed_age, (int, float)) and indexed_age < min_interval:
            return {
                "rebuilt": False,
                "deferred": True,
                "freshness": freshness,
                "min_interval_seconds": min_interval,
            }

        with self.trajectory_index_maintenance_lock:
            freshness = self.trajectory_chunk_index.freshness()
            if not freshness.get("stale"):
                return {"rebuilt": False, "freshness": freshness}
            indexed_age = freshness.get("indexed_age_seconds")
            if isinstance(indexed_age, (int, float)) and indexed_age < min_interval:
                return {
                    "rebuilt": False,
                    "deferred": True,
                    "freshness": freshness,
                    "min_interval_seconds": min_interval,
                }
            result = self.refresh_trajectory_chunk_index(
                include_vectors=self.config.trajectory_index_autobuild_vectors
            )
            return {
                "rebuilt": True,
                "freshness_before": freshness,
                "trajectory_index": result,
            }

    def start_trajectory_index_background_refresh(self) -> None:
        interval = max(0, self.config.trajectory_index_background_refresh_seconds)
        if interval <= 0 or self.trajectory_index_background_started:
            return
        self.trajectory_index_background_started = True

        def worker() -> None:
            while True:
                time.sleep(interval)
                try:
                    refresh = self.ensure_trajectory_chunk_index_current()
                    self.background_refresh_last_ok = utc_now()
                    self.background_refresh_error = None
                except Exception as exc:  # pragma: no cover - best-effort background maintenance
                    self.background_refresh_error = {"at": utc_now(), "error": str(exc)}
                    print(f"Trajectory index background refresh failed: {exc}")
                    continue
                if refresh.get("rebuilt"):
                    result = refresh.get("trajectory_index") or {}
                    print(
                        "Refreshed trajectory chunk index in background: "
                        f"{result.get('sessions_updated', 0)} sessions, "
                        f"{result.get('chunks_inserted', result.get('chunks', 0))} chunks"
                    )
                # Progressively fill embeddings so semantic search stays live
                # without a blocking full rebuild. Bounded per cycle.
                try:
                    stats = self.trajectory_chunk_index.stats()
                    if int(stats.get("missing_embeddings") or 0) > 0:
                        embedded = self.backfill_trajectory_chunk_vectors(limit=512)
                        if embedded.get("updated_chunks"):
                            print(
                                "Backfilled trajectory embeddings in background: "
                                f"{embedded.get('updated_chunks', 0)} chunks, "
                                f"{embedded.get('remaining_missing_embeddings', 0)} remaining"
                            )
                except Exception as exc:  # pragma: no cover - best-effort background maintenance
                    print(f"Trajectory embedding backfill failed: {exc}")

        thread = threading.Thread(target=worker, name="trajectory-index-refresh", daemon=True)
        thread.start()

    def load_or_build_memory(self) -> None:
        if self.memory_index is not None:
            return
        if os.path.exists(self.config.memory_index_path):
            with open(self.config.memory_index_path) as handle:
                with self.lock:
                    self.memory_index = json.load(handle)
            return
        if self.config.autobuild_memory:
            self.rebuild_memory()

    def get_memory_index(self) -> dict[str, Any] | None:
        self.load_or_build_memory()
        with self.lock:
            return self.memory_index

    def source_allowed_by_visibility(self, source: dict[str, Any]) -> bool:
        source_type = str(source.get("source_type") or "")
        source_name = str(source.get("source_name") or "")
        if source_type and source_type != "trajectory":
            return True
        if source_name not in {"codex", "claude"}:
            return True
        metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
        account_name = str(metadata.get("account") or "default")
        cwd = str(source.get("cwd") or metadata.get("cwd") or "")
        source_ref = str(source.get("source_ref") or "")
        session_id = str(metadata.get("raw_session_id") or metadata.get("session_id") or "")
        ref_id = source_ref.split(":", 1)[1] if ":" in source_ref else source_ref
        accounts = self.access_config.accounts_for_source(source_name)
        matching = [account for account in accounts if account.name == account_name]
        candidates = matching or accounts
        if not candidates:
            return False
        return any(
            account.include_workdir(cwd)
            and not account.is_session_blocked(session_id, ref_id)
            for account in candidates
        )

    def filter_sources_by_visibility(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [source for source in sources if self.source_allowed_by_visibility(source)]

    def trajectory_overview(self) -> str:
        index = self.get_memory_index() or {}
        counts = index.get("counts", {}) if isinstance(index, dict) else {}
        by_tool = counts.get("by_tool", {}) if isinstance(counts, dict) else {}
        total = int(counts.get("total") or 0) if isinstance(counts, dict) else 0
        if not total and not by_tool:
            return "Trajectory memory index is not built yet."

        tool_parts = [
            f"{tool}: {count}"
            for tool, count in sorted(by_tool.items())
        ]
        generated_at = index.get("generated_at") if isinstance(index, dict) else None
        lines = [
            "Configured trajectory memory is available from the local Codex and Claude sources.",
            f"Accessible trajectory sessions in the current index: {total}"
            + (f" ({', '.join(tool_parts)})" if tool_parts else ""),
        ]
        if generated_at:
            lines.append(f"Index generated at: {generated_at}")
        lines.append(
            "Use these counts for availability/count questions. Use the recent catalog for latest/recent-session questions. "
            "Use retrieved memory snippets below for detailed claims about specific prior work."
        )
        return "\n".join(lines)

    def recent_trajectory_catalog(self, limit: int = 8) -> str:
        index = self.get_memory_index() or {}
        recent_sessions = index.get("recent_sessions", []) if isinstance(index, dict) else []
        if not recent_sessions:
            return "No recent trajectory sessions are present in the current index."

        lines = [
            "Recent indexed trajectory sessions, ordered newest first. "
            "Use this list to identify the most recent trajectory and to answer brief recent-session summary questions."
        ]
        for session in recent_sessions[:limit]:
            if not isinstance(session, dict):
                continue
            tool = normalize_text(str(session.get("tool") or "unknown"), 32)
            updated_at = normalize_text(str(session.get("updated_at") or ""), 40)
            title = normalize_text(str(session.get("title") or "Untitled trajectory"), 120)
            summary = normalize_text(str(session.get("short_summary") or ""), 260)
            if summary:
                lines.append(f"- [{tool}][{updated_at}] {title}: {summary}")
            else:
                lines.append(f"- [{tool}][{updated_at}] {title}")
        return "\n".join(lines)

    def can_use_local_trajectory_lookup(
        self,
        *,
        actor_id: str,
        memory_scope: str,
        target_user_id: str | None,
    ) -> bool:
        owner_id = normalize_text(self.config.imported_owner_actor_id, 128)
        if not owner_id:
            return False
        if memory_scope == "private":
            return normalize_text(actor_id, 128) == owner_id
        if memory_scope == "target_user":
            return normalize_text(target_user_id or "", 128) == owner_id
        return False

    def collect_trajectory_candidates(
        self,
        *,
        query: str,
        actor_id: str,
        memory_scope: str,
        target_user_id: str | None,
        limit: int,
        seed_sources: list[dict[str, Any]] | None = None,
        include_semantic: bool = True,
        include_lexical: bool = True,
        include_local: bool = True,
        include_index: bool = True,
        index_modes: tuple[str, ...] = ("hybrid",),
        trace: list[dict[str, Any]] | None = None,
        stage: str = "search",
    ) -> list[dict[str, Any]]:
        channel_limit = max(limit * 2, self.config.trajectory_search_limit, 8)
        by_ref: dict[str, dict[str, Any]] = {}
        counts: dict[str, int] = {}

        for source in seed_sources or []:
            merge_trajectory_source(by_ref, source, query=query, channel="seed")
        if seed_sources:
            counts["seed"] = len(seed_sources)

        if include_index:
            for mode in index_modes:
                index_matches = self.trajectory_chunk_index.search(
                    query=query,
                    limit=channel_limit,
                    mode=mode,
                )
                counts[f"index:{mode}"] = len(index_matches)
                for match in index_matches:
                    if not self.source_allowed_by_visibility(match):
                        continue
                    match["_chunk_index"] = True
                    match["_local_lookup"] = float(match.get("score") or 0.0) >= LOCAL_LOOKUP_PRIORITY_THRESHOLD
                    merge_trajectory_source(by_ref, match, query=query, channel=f"index:{mode}")

        if include_semantic:
            semantic_matches = self.memory_store.search(
                query=query,
                actor_id=actor_id,
                memory_scope=memory_scope,
                target_user_id=target_user_id,
                limit=channel_limit,
            )
            counts["semantic"] = len(semantic_matches)
            for match in semantic_matches:
                if not self.source_allowed_by_visibility(match):
                    continue
                merge_trajectory_source(by_ref, match, query=query, channel="semantic")

        if include_lexical:
            lexical_matches = self.memory_store.lexical_trajectory_search(
                query=query,
                actor_id=actor_id,
                memory_scope=memory_scope,
                target_user_id=target_user_id,
                limit=channel_limit,
            )
            counts["lexical"] = len(lexical_matches)
            for match in lexical_matches:
                if not self.source_allowed_by_visibility(match):
                    continue
                merge_trajectory_source(by_ref, match, query=query, channel="lexical")

        if include_local:
            local_matches = [
                result.to_payload()
                for result in self.trajectory_lookup.search(
                    query=query,
                    limit=channel_limit,
                    full_scan=False,
                )
            ]
            counts["local"] = len(local_matches)
            for match in local_matches:
                if not self.source_allowed_by_visibility(match):
                    continue
                match["match_score"] = match.get("score")
                match["_local_lookup"] = float(match.get("score") or 0.0) >= LOCAL_LOOKUP_PRIORITY_THRESHOLD
                merge_trajectory_source(by_ref, match, query=query, channel="local")

        matches = sort_trajectory_sources(list(by_ref.values()), query=query, limit=limit)
        if trace is not None:
            trace.append(
                {
                    "stage": stage,
                    "query": query,
                    "counts": counts,
                    "top": [
                        {
                            "source_ref": item.get("source_ref"),
                            "title": normalize_text(str(item.get("title") or ""), 120),
                            "rank_score": round(trajectory_rank_score(item, query=query), 4),
                            "score": item.get("match_score") or item.get("score"),
                            "channels": item.get("_retrieval_channels") or [],
                        }
                        for item in matches[:3]
                    ],
                }
            )
        return matches

    def trajectory_search_needs_iteration(self, query: str, matches: list[dict[str, Any]]) -> bool:
        if not self.config.trajectory_agentic_search:
            return False
        if not matches:
            return True
        top = trajectory_rank_score(matches[0], query=query)
        likely = likely_trajectory_question(query)
        status_query = trajectory_status_query(query)
        if status_query and top < 500:
            return True
        if likely and not matches[0].get("_chunk_index") and top < 420:
            return True
        if likely and top < 420:
            return True
        if top < 180:
            return True
        if len(matches) > 1:
            second = trajectory_rank_score(matches[1], query=query)
            if likely and top - second < 80:
                return True
            if top - second < 45:
                return True
        return False

    def trajectory_refinement_queries(
        self,
        *,
        query: str,
        candidates: list[dict[str, Any]],
    ) -> list[str]:
        variants = trajectory_query_variants(query)
        focused = focus_query(query)

        def add(value: str) -> None:
            value = normalize_text(value, 300).strip(" ?.'\"")
            if value and value.lower() not in {variant.lower() for variant in variants}:
                variants.append(value)

        for candidate in candidates[:4]:
            title = normalize_text(str(candidate.get("title") or ""), 140)
            title_lower = title.lower()
            if title and not title_lower.startswith("system and memory context"):
                add(title)
                if focused and focused.lower() not in title_lower:
                    add(f"{focused} {title}")
            cwd = str(candidate.get("cwd") or "")
            cwd_parts = [part for part in re.split(r"[/\\s_-]+", cwd) if len(part) >= 4]
            if cwd_parts:
                add(" ".join(cwd_parts[-4:]))
                if focused:
                    add(f"{focused} {' '.join(cwd_parts[-3:])}")
        return variants[: max(1, self.config.trajectory_agentic_max_steps)]

    def _planner_should_engage(
        self, *, query: str, context: str, current: list[dict[str, Any]]
    ) -> bool:
        """Engage the planner when results are weak OR the query is a vague,
        context-dependent follow-up that the raw text can't resolve on its own."""
        if self.trajectory_search_needs_iteration(query, current):
            return True
        focused = focus_query(query) or query
        anchors = [token for token in query_tokens(focused) if len(token) >= 5]
        return bool(context and len(anchors) < 3 and FOLLOWUP_QUERY_RE.search(query or ""))

    def plan_trajectory_queries(
        self,
        *,
        query: str,
        context: str,
        candidates: list[dict[str, Any]],
        prior_queries: list[str],
        attempt: int,
    ) -> list[str]:
        """Ask the model to turn a vague request into precise search queries.

        Returns new queries not already tried; empty on any failure so callers
        fall back to the deterministic heuristics.
        """
        title_lines: list[str] = []
        for candidate in candidates[:6]:
            title = normalize_text(str(candidate.get("title") or ""), 120)
            # Junk titles (injected prompts, tag noise) poison the planner: it
            # echoes them back as "queries" and the search never converges.
            if title.lower().startswith("system and memory context"):
                continue
            if title.startswith(("<", "#")) or title.lower().startswith("you are "):
                continue
            cwd = normalize_text(str(candidate.get("cwd") or ""), 80)
            title_lines.append(f"- {title}" + (f"  [{cwd}]" if cwd else ""))

        prior_block = "\n".join(f"- {normalize_text(q, 160)}" for q in prior_queries[:12]) or "- (none)"
        context_block = normalize_text(context or "", 900) or "(no additional context)"
        candidate_block = "\n".join(title_lines) or "(no sessions retrieved yet)"
        max_queries = max(2, self.config.trajectory_query_planner_max_queries)

        system_prompt = (
            "You plan retrieval queries for a personal search engine over the user's own past "
            "AI coding sessions (Claude Code and Codex trajectories on their machine). The user's "
            "request may be vague or use shorthand. Using the request, the conversation context, "
            "and the titles of sessions found so far, propose concrete search queries that would "
            "locate the specific evidence. Prefer concrete entities: project names, hostnames "
            "(e.g. cluster, platform2), tool or command names (showusage, sbatch, squeue), identifiers "
            "(allocation or job codes), and file names. Expand shorthand into likely full terms. "
            "Do not repeat queries already tried. "
            f"Output ONLY a JSON array of {max_queries} or fewer short query strings, nothing else."
        )
        message = (
            f"User request:\n{normalize_text(query, 400)}\n\n"
            f"Conversation context:\n{context_block}\n\n"
            f"Sessions found so far (titles):\n{candidate_block}\n\n"
            f"Queries already tried:\n{prior_block}\n\n"
            f"The results so far are weak (attempt {attempt}). "
            f"Return up to {max_queries} better search queries as a JSON array of strings."
        )
        try:
            result = self.provider.complete(
                system_prompt=system_prompt,
                history=[],
                message=message,
                max_output_tokens=220,
                temperature=0.2,
                ephemeral=True,
            )
        except Exception:  # pragma: no cover - planner is best-effort
            return []

        text = str(result.get("text") or "")
        planned = self._parse_planned_queries(text)
        prior_lower = {q.lower() for q in prior_queries}
        deduped: list[str] = []
        seen: set[str] = set()
        for candidate_query in planned:
            cleaned = normalize_text(candidate_query, 200).strip(" ?.'\"")
            key = cleaned.lower()
            if not cleaned or len(cleaned) < 3 or key in prior_lower or key in seen:
                continue
            # Degenerate echoes of junk titles/tags are never useful queries.
            if "<" in cleaned or ">" in cleaned or len(cleaned) > 90:
                continue
            seen.add(key)
            deduped.append(cleaned)
            if len(deduped) >= max_queries:
                break
        return deduped

    @staticmethod
    def _parse_planned_queries(text: str) -> list[str]:
        text = (text or "").strip()
        if not text:
            return []
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            snippet = text[start : end + 1]
            try:
                parsed = json.loads(snippet)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item) for item in parsed if isinstance(item, (str, int, float)) and str(item).strip()]
        # Fallback: newline/bullet list
        lines: list[str] = []
        for raw_line in text.splitlines():
            cleaned = raw_line.strip().lstrip("-*0123456789. ").strip().strip('"')
            if cleaned and not cleaned.startswith("["):
                lines.append(cleaned)
        return lines[:8]

    def trajectory_agentic_search(
        self,
        *,
        query: str,
        actor_id: str,
        memory_scope: str,
        target_user_id: str | None,
        limit: int,
        seed_sources: list[dict[str, Any]] | None = None,
        trace: list[dict[str, Any]] | None = None,
        context: str = "",
        discovered: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        search_started = time.monotonic()
        refresh = self.ensure_trajectory_chunk_index_current()
        if trace is not None and refresh.get("rebuilt"):
            trace.append(
                {
                    "stage": "index_refresh",
                    "rebuilt": True,
                    "freshness_before": refresh.get("freshness_before"),
                    "trajectory_index": refresh.get("trajectory_index"),
                }
            )
        status_query = trajectory_status_query(query)
        combined_by_ref: dict[str, dict[str, Any]] = {}
        initial = self.collect_trajectory_candidates(
            query=query,
            actor_id=actor_id,
            memory_scope=memory_scope,
            target_user_id=target_user_id,
            limit=max(limit * 2, self.config.trajectory_search_limit, 8),
            seed_sources=seed_sources,
            include_semantic=not seed_sources and not status_query,
            include_lexical=not status_query,
            include_local=False,
            include_index=True,
            index_modes=("hybrid",),
            trace=trace,
            stage="initial",
        )
        for match in initial:
            merge_trajectory_source(combined_by_ref, match, query=query, channel="initial")

        needs_iteration = self.trajectory_search_needs_iteration(query, initial)
        use_local_fallback = likely_trajectory_question(query) and (not status_query or not initial)
        if needs_iteration and use_local_fallback:
            local_matches = self.collect_trajectory_candidates(
                query=query,
                actor_id=actor_id,
                memory_scope=memory_scope,
                target_user_id=target_user_id,
                limit=max(limit * 2, self.config.trajectory_search_limit, 8),
                seed_sources=None,
                include_semantic=False,
                include_lexical=False,
                include_local=True,
                include_index=False,
                trace=trace,
                stage="fallback:local",
            )
            for match in local_matches:
                merge_trajectory_source(combined_by_ref, match, query=query, channel="fallback:local")
            initial = sort_trajectory_sources(
                list(combined_by_ref.values()),
                query=query,
                limit=max(limit * 2, self.config.trajectory_search_limit, 8),
            )
            needs_iteration = self.trajectory_search_needs_iteration(query, initial)

        prior_queries = [query]
        if needs_iteration:
            variants = self.trajectory_refinement_queries(query=query, candidates=initial)
            prior_queries = list(dict.fromkeys(prior_queries + variants))
            if discovered is not None:
                discovered.extend(variants)
            for step_index, variant in enumerate(variants[1:], start=1):
                step_matches = self.collect_trajectory_candidates(
                    query=variant,
                    actor_id=actor_id,
                    memory_scope=memory_scope,
                    target_user_id=target_user_id,
                    limit=max(limit * 2, self.config.trajectory_search_limit, 8),
                    seed_sources=None,
                    include_semantic=False,
                    include_lexical=not status_query,
                    include_local=False,
                    include_index=True,
                    index_modes=("exact", "hybrid"),
                    trace=trace,
                    stage=f"refine:{step_index}",
                )
                for match in step_matches:
                    merge_trajectory_source(combined_by_ref, match, query=query, channel=f"refine:{step_index}")

        # LLM-driven planner: when heuristics still leave results weak, ask the
        # model for precise queries and iterate until satisfying or budget spent.
        current = sort_trajectory_sources(
            list(combined_by_ref.values()),
            query=query,
            limit=max(limit * 2, self.config.trajectory_search_limit, 8),
        )
        if self.config.trajectory_query_planner:
            rounds = 0
            max_rounds = max(0, self.config.trajectory_query_planner_max_rounds)
            engage = self._planner_should_engage(query=query, context=context, current=current)
            # Each planner round spawns a model call; without a wall-clock cap a
            # weak query cascades into minutes of retries and the client times out.
            time_budget = max(1.0, self.config.trajectory_search_time_budget_seconds)
            while rounds < max_rounds and engage:
                if time.monotonic() - search_started > time_budget:
                    log.info(
                        "trajectory search planner stopped: time budget %.0fs spent (query=%r rounds=%d)",
                        time_budget, query[:60], rounds,
                    )
                    if trace is not None:
                        trace.append({"stage": "planner:budget", "seconds": round(time.monotonic() - search_started, 1)})
                    break
                before_top = trajectory_rank_score(current[0], query=query) if current else 0.0
                planned = self.plan_trajectory_queries(
                    query=query,
                    context=context,
                    candidates=current,
                    prior_queries=prior_queries,
                    attempt=rounds + 1,
                )
                if trace is not None:
                    trace.append(
                        {
                            "stage": f"planner:{rounds + 1}",
                            "planned_queries": planned,
                            "top_before": round(before_top, 2),
                        }
                    )
                if not planned:
                    break
                prior_queries = list(dict.fromkeys(prior_queries + planned))
                if discovered is not None:
                    discovered.extend(planned)
                for step_index, variant in enumerate(planned, start=1):
                    step_matches = self.collect_trajectory_candidates(
                        query=variant,
                        actor_id=actor_id,
                        memory_scope=memory_scope,
                        target_user_id=target_user_id,
                        limit=max(limit * 2, self.config.trajectory_search_limit, 8),
                        seed_sources=None,
                        include_semantic=True,
                        include_lexical=not status_query,
                        include_local=False,
                        include_index=True,
                        index_modes=("exact", "hybrid"),
                        trace=trace,
                        stage=f"planner:{rounds + 1}:{step_index}",
                    )
                    for match in step_matches:
                        merge_trajectory_source(
                            combined_by_ref, match, query=query, channel=f"planner:{rounds + 1}"
                        )
                current = sort_trajectory_sources(
                    list(combined_by_ref.values()),
                    query=query,
                    limit=max(limit * 2, self.config.trajectory_search_limit, 8),
                )
                after_top = trajectory_rank_score(current[0], query=query) if current else 0.0
                rounds += 1
                # Stop early if a round barely moved the needle — the planner has
                # stalled and further rounds would just burn model calls.
                if after_top - before_top < 15.0:
                    break
                # Subsequent rounds continue only while results are still weak.
                engage = self.trajectory_search_needs_iteration(query, current)

        return sort_trajectory_sources(list(combined_by_ref.values()), query=query, limit=limit)

    @staticmethod
    def _compose_read_query(query: str, discovered: list[str]) -> str:
        """Fold the entity-rich terms the planner/heuristics discovered into the
        query used to extract evidence, so snippet and related-chunk selection
        can find command output the vague original query would never match."""
        seen: set[str] = set()
        extra: list[str] = []
        for candidate in discovered:
            for token in query_tokens(candidate):
                key = token.lower()
                if key in seen or len(token) < 4:
                    continue
                seen.add(key)
                extra.append(token)
        if not extra:
            return query
        return normalize_text(f"{query} {' '.join(extra)}", 400)

    def trajectory_investigation(
        self,
        *,
        query: str,
        actor_id: str,
        memory_scope: str,
        target_user_id: str | None,
        memory_sources: list[dict[str, Any]],
        context: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        mode = normalize_text(self.config.trajectory_investigation_mode, 24).lower()
        if mode in {"", "off", "none", "false", "0"}:
            return "", []
        if not self.can_use_local_trajectory_lookup(
            actor_id=actor_id,
            memory_scope=memory_scope,
            target_user_id=target_user_id,
        ):
            return "", []

        discovered_queries: list[str] = []
        combined_sources = self.trajectory_agentic_search(
            query=query,
            actor_id=actor_id,
            memory_scope=memory_scope,
            target_user_id=target_user_id,
            limit=max(self.config.trajectory_search_limit, self.config.trajectory_evidence_limit * 2),
            seed_sources=memory_sources,
            context=context,
            discovered=discovered_queries,
        )
        read_query = self._compose_read_query(query, discovered_queries)

        candidate_refs = candidate_refs_from_memory(combined_sources)
        if mode == "auto" and not candidate_refs and not likely_trajectory_question(query):
            return "", []

        evidence_payloads: list[dict[str, Any]] = []
        source_records: list[dict[str, Any]] = []
        per_trajectory_chars = max(1200, self.config.trajectory_evidence_chars // max(1, self.config.trajectory_evidence_limit))

        if mode == "full_scan":
            results = self.trajectory_lookup.search(
                query=query,
                limit=self.config.trajectory_search_limit,
                candidate_refs=candidate_refs,
                full_scan=True,
            )
            source_iter: list[tuple[dict[str, Any], dict[str, Any] | None]] = [
                (result.to_payload(), None) for result in results
            ]
        else:
            source_iter = [(source, source.get("metadata") if isinstance(source.get("metadata"), dict) else {}) for source in combined_sources]

        if not source_iter:
            return "", []

        for source, metadata in source_iter[: self.config.trajectory_evidence_limit]:
            metadata = metadata if isinstance(metadata, dict) else {}
            source_ref = str(source.get("parent_source_ref") or source.get("source_ref") or "")
            if ":chunk:" in source_ref:
                source_ref = source_ref.split(":chunk:", 1)[0]
            payload = None
            if source.get("_chunk_index") and source_ref:
                event_start = int(metadata.get("event_start") or 0)
                payload = self.trajectory_chunk_index.read_window(
                    source_ref=source_ref,
                    event_index=event_start,
                    chunk_id=int(metadata["chunk_id"]) if metadata.get("chunk_id") is not None else None,
                    query=read_query,
                    max_chars=per_trajectory_chars,
                )
            file_path = str(metadata.get("file_path") or "")
            if payload is None and file_path:
                payload = read_payload_from_file(
                    source_name=str(source.get("source_name") or metadata.get("tool") or ""),
                    source_ref=source_ref,
                    session_id=str(metadata.get("session_id") or source_ref.split(":", 1)[-1]),
                    title=str(source.get("title") or "Untitled trajectory"),
                    updated_at=str(source.get("updated_at") or ""),
                    cwd=str(metadata.get("cwd") or ""),
                    file_path=file_path,
                    metadata=metadata,
                    query=read_query,
                    max_chars=per_trajectory_chars,
                )
            if payload is None and source_ref:
                payload = self.trajectory_lookup.read(source_ref, query=read_query, max_chars=per_trajectory_chars)
            if payload is None:
                continue

            evidence_payloads.append(payload)
            snippets = payload.get("snippets") or [normalize_text(payload.get("transcript") or "", 300)]
            score = source.get("score")
            if score is None:
                score = source.get("match_score")
            source_records.append(
                {
                    "memory_id": None,
                    "record_kind": "trajectory_evidence",
                    "scope": memory_scope,
                    "owner_actor_id": target_user_id if memory_scope == "target_user" else actor_id,
                    "author_actor_id": actor_id,
                    "source_type": "trajectory",
                    "source_name": payload.get("source_name"),
                    "source_ref": payload.get("source_ref"),
                    "parent_source_ref": None,
                    "title": payload.get("title"),
                    "summary_short": normalize_text(snippets[0] if snippets else str(payload.get("title") or ""), 220),
                    "summary_detailed": normalize_text(payload.get("transcript") or "", 500),
                    "text_preview": normalize_text(snippets[0] if snippets else payload.get("transcript") or "", 300),
                    "created_at": payload.get("updated_at"),
                    "updated_at": payload.get("updated_at"),
                    "imported_at": None,
                    "last_seen_at": None,
                    "content_hash": None,
                    "tags": ["trajectory", "full_read", str(payload.get("source_name") or "")],
                    "metadata": {
                        "cwd": payload.get("cwd"),
                        "match_kind": source.get("match_kind") or "candidate",
                        "score": score,
                        "transcript_chars": payload.get("transcript_chars"),
                    },
                    "match_score": score,
                }
            )

        evidence = render_evidence(evidence_payloads, max_chars=self.config.trajectory_evidence_chars)
        return evidence, source_records

    def tool_env_for_request(
        self,
        *,
        actor_id: str,
        memory_scope: str,
        target_user_id: str | None,
        retrieval_budget_log_path: str | None = None,
    ) -> dict[str, str]:
        env = {
            "MYBOT_TOOL_BASE_URL": f"http://{self.config.host}:{self.config.port}",
            "MYBOT_TOOL_ACTOR_ID": actor_id,
            "MYBOT_TOOL_MEMORY_SCOPE": memory_scope,
        }
        if target_user_id:
            env["MYBOT_TOOL_TARGET_USER_ID"] = target_user_id
        if retrieval_budget_log_path:
            env["MYBOT_TOOL_BUDGET_LOG"] = retrieval_budget_log_path
        return env

    def new_retrieval_budget_log_path(self) -> Path:
        if self.config.codex_permission_profile or self.config.codex_sandbox == "workspace-write":
            directory = Path(self.config.codex_cwd) / ".mybot" / "retrieval_budgets"
        else:
            directory = Path(self.config.state_dir) / "retrieval_budgets"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return directory / f"{stamp}-{uuid.uuid4().hex}.jsonl"

    def read_retrieval_budget_log(self, path: Path | None) -> list[dict[str, Any]]:
        if path is None or not path.exists():
            return []
        budgets: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    budgets.append(record)
        return budgets

    def build_system_prompt(
        self,
        *,
        query: str,
        actor_id: str,
        memory_scope: str,
        target_user_id: str | None,
        session_summary: str,
        use_memory: bool,
        match_limit: int,
    ) -> tuple[str, list[dict[str, Any]], bool]:
        sections: list[str] = []
        workspace_prompt = self.workspace.render()
        if workspace_prompt:
            sections.append(workspace_prompt)

        sections.append(
            "## Grounding Policy\n"
            "Use retrieved memory snippets and the current session summary as grounded context. "
            "If no relevant memory is provided for a claim about prior work, a person, or a prior decision, "
            "say that the system does not have grounded memory for it instead of guessing. "
            "When trajectory evidence contains later corrections or renames, use the later corrected wording."
        )

        if memory_scope == "shared":
            sections.append(
                "## Active Memory Scope\n"
                "Use only shared team memory for cross-session recall in this answer."
            )
        elif memory_scope == "target_user":
            sections.append(
                "## Active Memory Scope\n"
                f"Use only private memory retrieved for target user `{target_user_id}`. "
                "Do not mix in other users' private history."
            )
        else:
            sections.append(
                "## Active Memory Scope\n"
                f"Use only private memory retrieved for actor `{actor_id}`."
            )

        session_summary_used = bool(session_summary)
        if session_summary:
            sections.append(f"## Session Summary\n{session_summary}")

        if use_memory and self.config.agentic_tool_routing:
            tool_command = f"{shlex.quote(self.config.mybot_tool_python)} {shlex.quote(self.config.mybot_tool_path)}"
            sections.append(
                "## Local Tool Routing\n"
                "Decide from the user's message whether local memory or trajectory lookup is needed. "
                "If it is needed, use the mybot tool and iterate until you have enough grounded evidence; "
                "Do not bypass the configured trajectory access layer. "
                "Treat search results as candidates and read focused trajectory evidence before making claims about what a trajectory contains. "
                "otherwise answer directly without lookup. "
                f"The tool command is: `{tool_command}`. "
                "The request actor and memory scope are already provided through environment variables."
            )
        elif use_memory:
            sections.append(f"## Trajectory Memory Overview\n{self.trajectory_overview()}")
            sections.append(f"## Recent Trajectory Catalog\n{self.recent_trajectory_catalog()}")

        memory_sources: list[dict[str, Any]] = []
        if use_memory and not self.config.agentic_tool_routing:
            memory_sources = self.memory_store.search(
                query=query,
                actor_id=actor_id,
                memory_scope=memory_scope,
                target_user_id=target_user_id,
                limit=match_limit,
            )
            memory_sources = self.filter_sources_by_visibility(memory_sources)

        if memory_sources:
            lines = ["## Retrieved Memory"]
            for source in memory_sources:
                updated_at = normalize_text(str(source.get("updated_at") or ""), 40)
                source_name = normalize_text(str(source.get("source_name") or source.get("source_type") or "memory"), 40)
                record_kind = normalize_text(str(source.get("record_kind") or ""), 40)
                lines.append(
                    "- "
                    f"[{source['scope']}][{source_name}][{record_kind}][{updated_at}] "
                    f"{source['title']}: {source['text_preview']}"
                )
            sections.append("\n".join(lines))
        elif use_memory and not self.config.agentic_tool_routing:
            sections.append("## Retrieved Memory\nNo strong retrieved memory matches were found for this query.")

        trajectory_sources: list[dict[str, Any]] = []
        if use_memory and not self.config.agentic_tool_routing:
            evidence, trajectory_sources = self.trajectory_investigation(
                query=query,
                actor_id=actor_id,
                memory_scope=memory_scope,
                target_user_id=target_user_id,
                memory_sources=memory_sources,
                context=session_summary,
            )
            if evidence:
                sections.append(f"## Full Trajectory Evidence\n{evidence}")

        sections.append(
            "## Response Rule\n"
            "Answer naturally and concisely. Mention uncertainty when the answer is not grounded "
            "in the retrieved memory or current session context."
        )
        return (
            "\n\n".join(section for section in sections if section),
            trajectory_sources + memory_sources,
            session_summary_used,
        )

    def maybe_compact_session(self, *, session_key: str, user: str) -> dict[str, Any] | None:
        all_messages = self.sessions.load_messages(session_key, limit=None)
        if not all_messages:
            return None

        total_chars = sum(len(normalize_text(str(message.get("content") or ""))) for message in all_messages)
        if (
            len(all_messages) <= self.config.compaction_trigger_message_count
            and total_chars <= self.config.compaction_trigger_char_count
        ):
            return None

        compactable_count = max(0, len(all_messages) - (self.config.session_tail_pairs * 2))
        if compactable_count <= 0:
            return None

        summary_state = self.sessions.get_session_summary(session_key) or {}
        already_compacted = int(summary_state.get("compacted_message_count") or 0)
        if compactable_count <= already_compacted:
            return summary_state or None

        new_messages = all_messages[already_compacted:compactable_count]
        if not new_messages:
            return summary_state or None

        new_summary = self.provider.summarize_conversation(
            existing_summary=str(summary_state.get("summary") or ""),
            new_messages=new_messages,
        )
        if not new_summary:
            return summary_state or None

        return self.sessions.set_session_summary(
            session_key,
            user,
            summary=new_summary,
            compacted_message_count=compactable_count,
        )


class ChatHandler(BaseHTTPRequestHandler):
    server: "StandaloneServer"

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/gui", "/gui/"}:
            self.respond_html(200, GUI_HTML)
            return
        if path == "/gui/state":
            self.respond_json(200, build_gui_state(self.server.state))
            return
        if path == "/health":
            cfg = self.server.state.config
            self.respond_json(
                200,
                {
                    "ok": True,
                    "provider_backend": cfg.provider_backend,
                    "model_base_url": cfg.model_base_url,
                    "model_name": cfg.model_name,
                    "model_api_style": cfg.model_api_style,
                    "codex_permission_profile": cfg.codex_permission_profile,
                    "codex_cwd": cfg.codex_cwd,
                    "workspace_dir": cfg.workspace_dir,
                    "memory_loaded": self.server.state.get_memory_index() is not None,
                    "semantic_memory_stats": self.server.state.memory_store.get_stats(),
                    "sync_auth_configured": self.server.state.sync_auth.configured(),
                },
            )
            return

        if path == "/memory":
            index = self.server.state.get_memory_index()
            self.respond_json(
                200,
                {
                    "ok": True,
                    "memory_index": index,
                    "semantic_memory_stats": self.server.state.memory_store.get_stats(),
                },
            )
            return

        self.respond_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.respond_json(400, {"ok": False, "error": str(exc)})
            return

        try:
            self._dispatch_post(body)
        except Exception as exc:  # never drop the connection silently
            log.exception("unhandled error handling POST %s", self.path)
            try:
                self.respond_json(500, {"ok": False, "error": f"internal error: {exc}"})
            except Exception:
                log.exception("failed to send 500 response for %s", self.path)

    def _dispatch_post(self, body: dict[str, Any]) -> None:
        if self.path == "/chat":
            self.handle_chat(body)
            return
        if self.path == "/gui/query":
            self.handle_gui_query(body)
            return
        if self.path == "/gui/maintenance":
            self.handle_gui_maintenance(body)
            return
        if self.path == "/gui/scope":
            self.handle_gui_scope(body)
            return
        if self.path == "/memory/rebuild":
            self.handle_memory_rebuild(body)
            return
        if self.path == "/memory/search":
            self.handle_memory_search(body)
            return
        if self.path == "/trajectory/search":
            self.handle_trajectory_search(body)
            return
        if self.path == "/trajectory/read":
            self.handle_trajectory_read(body)
            return
        if self.path == "/trajectory/index/rebuild":
            self.handle_trajectory_index_rebuild(body)
            return
        if self.path == "/trajectory/index/embed":
            self.handle_trajectory_index_embed(body)
            return
        if self.path == "/trajectory/index/stats":
            self.handle_trajectory_index_stats(body)
            return
        if self.path == "/memory/promote":
            self.handle_memory_promote(body)
            return
        if self.path == "/memory/import-batch":
            self.handle_memory_import_batch(body)
            return
        if self.path == "/memory/sync-status":
            self.handle_memory_sync_status(body)
            return
        if self.path == "/sessions/history":
            self.handle_session_history(body)
            return
        if self.path == "/sessions/reset":
            self.handle_session_reset(body)
            return
        self.respond_json(404, {"ok": False, "error": "not found"})

    def handle_chat(self, body: dict[str, Any]) -> None:
        started = time.time()
        message = normalize_text(str(body.get("message") or body.get("input") or ""))
        if not message:
            self.respond_json(400, {"ok": False, "error": "message is required"})
            return

        user = str(body.get("user") or body.get("actor_id") or DEFAULT_SESSION_MAIN_KEY)
        actor_id = normalize_text(str(body.get("actor_id") or user), 128)
        try:
            memory_scope = parse_memory_scope(body.get("memory_scope"))
        except ValueError as exc:
            self.respond_json(400, {"ok": False, "error": str(exc)})
            return

        target_user_id = normalize_text(str(body.get("target_user_id") or ""), 128) or None
        if memory_scope == "target_user" and not target_user_id:
            self.respond_json(400, {"ok": False, "error": "target_user_id is required when memory_scope=target_user"})
            return

        session_key = safe_session_key(
            user=user,
            requested=str(body["session_key"]) if body.get("session_key") else None,
            new_session=coerce_bool(body.get("new_session")),
        )
        use_memory = coerce_bool(body.get("use_trajectory_memory"), True)
        log.info(
            "chat start user=%s scope=%s session=%s msg_chars=%d use_memory=%s",
            user, memory_scope, session_key, len(message), use_memory,
        )
        all_messages = self.server.state.sessions.load_messages(session_key, limit=None)
        summary_state = self.server.state.sessions.get_session_summary(session_key) or {}
        summary_text = str(summary_state.get("summary") or "")
        compacted_message_count = int(summary_state.get("compacted_message_count") or 0)
        visible_messages = all_messages[compacted_message_count:]
        if self.server.state.config.history_max_messages:
            visible_messages = visible_messages[-self.server.state.config.history_max_messages :]

        history_for_model: list[dict[str, Any]] = []
        total_chars = 0
        for entry in reversed(visible_messages):
            text = normalize_text(str(entry.get("content") or ""))
            total_chars += len(text)
            if total_chars > self.server.state.config.history_max_chars and history_for_model:
                break
            history_for_model.append({"role": entry.get("role", "user"), "content": text})
        history_for_model.reverse()

        try:
            system_prompt, memory_sources, session_summary_used = self.server.state.build_system_prompt(
                query=message,
                actor_id=actor_id,
                memory_scope=memory_scope,
                target_user_id=target_user_id,
                session_summary=summary_text,
                use_memory=use_memory,
                match_limit=int(body.get("memory_match_limit", self.server.state.config.memory_match_limit)),
            )
        except ValueError as exc:
            self.respond_json(400, {"ok": False, "error": str(exc)})
            return
        except RuntimeError as exc:
            log.warning("chat build_system_prompt failed user=%s: %s", user, exc)
            self.respond_json(500, {"ok": False, "error": str(exc)})
            return

        retrieval_budget_log_path = (
            self.server.state.new_retrieval_budget_log_path()
            if use_memory and self.server.state.config.agentic_tool_routing
            else None
        )
        tool_env = self.server.state.tool_env_for_request(
            actor_id=actor_id,
            memory_scope=memory_scope,
            target_user_id=target_user_id,
            retrieval_budget_log_path=str(retrieval_budget_log_path) if retrieval_budget_log_path else None,
        )

        try:
            provider_result = self.server.state.provider.chat(
                system_prompt=system_prompt,
                history=history_for_model,
                message=message,
                backend_session_id=(self.server.state.sessions.get_backend_state(session_key) or {}).get("backend_session_id"),
                max_output_tokens=int(body["max_output_tokens"]) if body.get("max_output_tokens") is not None else None,
                temperature=float(body["temperature"]) if body.get("temperature") is not None else None,
                tool_env=tool_env,
            )
        except RuntimeError as exc:
            log.warning("chat provider failed user=%s: %s", user, exc)
            self.respond_json(502, {"ok": False, "error": str(exc)})
            return

        retrieval_budgets = self.server.state.read_retrieval_budget_log(retrieval_budget_log_path)
        answer = append_retrieval_budget_summary(provider_result["text"], retrieval_budgets)
        response_hash = hashlib.sha256(answer.encode("utf-8")).hexdigest()[:16]
        log.info(
            "chat done user=%s session=%s reply_chars=%d sources=%d elapsed=%.1fs",
            user, session_key, len(answer), len(memory_sources), time.time() - started,
        )

        source_records: list[dict[str, Any]] = []
        if session_summary_used:
            source_records.append(
                {
                    "memory_id": None,
                    "record_kind": "session_summary",
                    "scope": "private",
                    "owner_actor_id": actor_id,
                    "author_actor_id": actor_id,
                    "source_type": "session_summary",
                    "source_name": "session",
                    "source_ref": session_key,
                    "parent_source_ref": None,
                    "title": "Current session summary",
                    "summary_short": normalize_text(summary_text, 160),
                    "summary_detailed": normalize_text(summary_text, 220),
                    "text_preview": normalize_text(summary_text, 220),
                    "created_at": summary_state.get("timestamp"),
                    "updated_at": summary_state.get("timestamp"),
                    "imported_at": summary_state.get("timestamp"),
                    "last_seen_at": summary_state.get("timestamp"),
                    "content_hash": None,
                    "tags": ["session_summary"],
                    "metadata": {
                        "compacted_message_count": compacted_message_count,
                    },
                    "match_score": None,
                }
            )
        source_records.extend(memory_sources)

        self.server.state.sessions.append_message(
            session_key,
            user,
            "user",
            message,
            meta={
                "source": "external_api",
                "actor_id": actor_id,
                "memory_scope": memory_scope,
                "target_user_id": target_user_id,
            },
        )
        self.server.state.sessions.append_message(
            session_key,
            user,
            "assistant",
            answer,
            meta={
                "source": "model_provider",
                "provider_style": provider_result["provider_style"],
                "response_hash": response_hash,
                "actor_id": actor_id,
                "memory_scope_used": memory_scope,
                "target_user_id": target_user_id,
                "session_summary_used": session_summary_used,
                "retrieval_budget": retrieval_budgets,
                "sources": source_records,
            },
        )

        backend_session_id = provider_result.get("backend_session_id")
        if backend_session_id:
            existing = self.server.state.sessions.get_backend_state(session_key) or {}
            if existing.get("backend_session_id") != backend_session_id:
                self.server.state.sessions.set_backend_state(
                    session_key,
                    user,
                    provider_result["provider_style"],
                    backend_session_id,
                )

        try:
            self.server.state.maybe_compact_session(session_key=session_key, user=user)
        except RuntimeError as exc:
            print(f"Session compaction failed for {session_key}: {exc}")

        return_sources = coerce_bool(body.get("return_sources"), True)
        payload: dict[str, Any] = {
            "ok": True,
            "session_key": session_key,
            "text": answer,
            "provider_backend": self.server.state.config.provider_backend,
            "backend_session_id": backend_session_id,
            "memory_scope_used": memory_scope,
            "session_summary_used": session_summary_used,
            "retrieval_budget": retrieval_budgets,
            "sources": source_records if return_sources else [],
            "memory_matches": memory_sources,
        }
        if body.get("include_raw"):
            payload["raw"] = provider_result["raw"]
        self.respond_json(200, payload)

    def handle_memory_rebuild(self, body: dict[str, Any]) -> None:
        result = self.server.state.rebuild_memory(
            owner_actor_id=normalize_text(str(body.get("actor_id") or ""), 128) or None,
            recent_limit=int(body.get("recent_limit", 8)),
            older_limit=int(body.get("older_limit", 32)),
            max_files_per_tool=int(body.get("max_files_per_tool", self.server.state.config.trajectory_max_files_per_tool)),
        )
        self.respond_json(
            200,
            {
                "ok": True,
                "counts": result["index"].get("counts"),
                "generated_at": result["index"].get("generated_at"),
                "memory_index_path": self.server.state.config.memory_index_path,
                "semantic_memory": result["semantic"],
            },
        )

    def handle_memory_search(self, body: dict[str, Any]) -> None:
        query = normalize_text(str(body.get("query") or ""))
        if not query:
            self.respond_json(400, {"ok": False, "error": "query is required"})
            return
        actor_id = normalize_text(str(body.get("actor_id") or body.get("user") or DEFAULT_SESSION_MAIN_KEY), 128)
        try:
            memory_scope = parse_memory_scope(body.get("memory_scope"))
        except ValueError as exc:
            self.respond_json(400, {"ok": False, "error": str(exc)})
            return
        target_user_id = normalize_text(str(body.get("target_user_id") or ""), 128) or None
        try:
            matches = self.server.state.memory_store.search(
                query=query,
                actor_id=actor_id,
                memory_scope=memory_scope,
                target_user_id=target_user_id,
                limit=int(body.get("limit", self.server.state.config.memory_match_limit)),
            )
            matches = self.server.state.filter_sources_by_visibility(matches)
        except ValueError as exc:
            self.respond_json(400, {"ok": False, "error": str(exc)})
            return
        except RuntimeError as exc:
            self.respond_json(500, {"ok": False, "error": str(exc)})
            return
        self.respond_json(200, {"ok": True, "matches": matches})

    def handle_gui_query(self, body: dict[str, Any]) -> None:
        """Local, owner-implicit retrieval probe for the dashboard.

        Runs the chunk index directly so the owner can inspect exactly what the
        retriever ranks for a query, with scores and provenance.
        """
        query = normalize_text(str(body.get("query") or ""))
        if not query:
            self.respond_json(400, {"ok": False, "error": "query is required"})
            return
        limit = max(1, min(int(body.get("limit", 8)), 25))
        mode = str(body.get("mode") or "hybrid").lower()
        if mode not in {"hybrid", "exact", "vector", "semantic", "fts", "lexical"}:
            mode = "hybrid"
        try:
            results = self.server.state.trajectory_chunk_index.search(
                query=query, limit=limit, mode=mode
            )
        except Exception as exc:  # pragma: no cover - diagnostic endpoint stays available
            self.respond_json(500, {"ok": False, "error": str(exc)})
            return
        self.respond_json(
            200,
            {
                "ok": True,
                "query": query,
                "mode": mode,
                "count": len(results),
                "results": results,
            },
        )

    def handle_gui_maintenance(self, body: dict[str, Any]) -> None:
        action = str(body.get("action") or "").strip().lower()
        result = self.server.state.start_trajectory_maintenance(action)
        status = 200 if result.get("ok") else 409
        self.respond_json(status, result)

    def handle_gui_scope(self, body: dict[str, Any]) -> None:
        action = str(body.get("action") or "").strip().lower()
        if not action:
            # No action: just return the current scope for the panel to render.
            self.respond_json(200, {"ok": True, "scope": self.server.state.scope_summary()})
            return
        if action == "review":
            try:
                result = self.server.state.review_project(
                    source_name=str(body.get("source_name") or ""),
                    cwd=str(body.get("value") or body.get("cwd") or ""),
                    decision=str(body.get("decision") or "keep"),
                )
            except Exception as exc:  # pragma: no cover
                self.respond_json(500, {"ok": False, "error": str(exc)})
                return
            result["scope"] = self.server.state.scope_summary()
            self.respond_json(200, result)
            return
        try:
            result = self.server.state.update_scope(
                action=action,
                source_name=str(body.get("source_name") or ""),
                account_name=str(body.get("account") or body.get("account_name") or "default"),
                kind=str(body.get("kind") or ""),
                value=str(body.get("value") or ""),
            )
        except Exception as exc:  # pragma: no cover - control endpoint stays available
            self.respond_json(500, {"ok": False, "error": str(exc)})
            return
        self.respond_json(200 if result.get("ok") else 400, result)

    def handle_trajectory_search(self, body: dict[str, Any]) -> None:
        query = normalize_text(str(body.get("query") or ""))
        if not query:
            self.respond_json(400, {"ok": False, "error": "query is required"})
            return
        actor_id = normalize_text(str(body.get("actor_id") or body.get("user") or self.server.state.config.imported_owner_actor_id), 128)
        try:
            memory_scope = parse_memory_scope(body.get("memory_scope"))
        except ValueError as exc:
            self.respond_json(400, {"ok": False, "error": str(exc)})
            return
        target_user_id = normalize_text(str(body.get("target_user_id") or ""), 128) or None
        if not self.server.state.can_use_local_trajectory_lookup(
            actor_id=actor_id,
            memory_scope=memory_scope,
            target_user_id=target_user_id,
        ):
            self.respond_json(403, {"ok": False, "error": "local trajectory lookup is not allowed for this actor/scope"})
            return
        limit = int(body.get("limit", self.server.state.config.trajectory_search_limit))
        if coerce_bool(body.get("full_scan"), False):
            results = self.server.state.trajectory_lookup.search(
                query=query,
                limit=limit,
                full_scan=True,
            )
            matches = [
                payload
                for result in results
                if self.server.state.source_allowed_by_visibility(payload := result.to_payload())
            ]
            self.respond_json(
                200,
                {
                    "ok": True,
                    "mode": "full_scan",
                    "matches": matches,
                },
            )
            return

        trace: list[dict[str, Any]] = []
        matches = self.server.state.trajectory_agentic_search(
            query=query,
            actor_id=actor_id,
            memory_scope=memory_scope,
            target_user_id=target_user_id,
            limit=limit,
            seed_sources=None,
            trace=trace if coerce_bool(body.get("return_trace"), False) else None,
            context=normalize_text(str(body.get("context") or ""), 1200),
        )
        response = {"ok": True, "mode": "agentic_candidate", "matches": matches}
        if coerce_bool(body.get("return_trace"), False):
            response["trace"] = trace
        self.respond_json(200, response)

    def handle_trajectory_index_rebuild(self, body: dict[str, Any]) -> None:
        actor_id = normalize_text(str(body.get("actor_id") or body.get("user") or self.server.state.config.imported_owner_actor_id), 128)
        try:
            memory_scope = parse_memory_scope(body.get("memory_scope"))
        except ValueError as exc:
            self.respond_json(400, {"ok": False, "error": str(exc)})
            return
        target_user_id = normalize_text(str(body.get("target_user_id") or ""), 128) or None
        if not self.server.state.can_use_local_trajectory_lookup(
            actor_id=actor_id,
            memory_scope=memory_scope,
            target_user_id=target_user_id,
        ):
            self.respond_json(403, {"ok": False, "error": "local trajectory index rebuild is not allowed for this actor/scope"})
            return
        include_vectors = coerce_bool(body.get("include_vectors"), True)
        result = self.server.state.rebuild_trajectory_chunk_index(include_vectors=include_vectors)
        self.respond_json(200, {"ok": True, "trajectory_index": result})

    def handle_trajectory_index_embed(self, body: dict[str, Any]) -> None:
        actor_id = normalize_text(str(body.get("actor_id") or body.get("user") or self.server.state.config.imported_owner_actor_id), 128)
        try:
            memory_scope = parse_memory_scope(body.get("memory_scope"))
        except ValueError as exc:
            self.respond_json(400, {"ok": False, "error": str(exc)})
            return
        target_user_id = normalize_text(str(body.get("target_user_id") or ""), 128) or None
        if not self.server.state.can_use_local_trajectory_lookup(
            actor_id=actor_id,
            memory_scope=memory_scope,
            target_user_id=target_user_id,
        ):
            self.respond_json(403, {"ok": False, "error": "local trajectory index embedding is not allowed for this actor/scope"})
            return
        limit = int(body.get("limit", 256))
        result = self.server.state.backfill_trajectory_chunk_vectors(limit=limit)
        self.respond_json(200, {"ok": True, "trajectory_index": result})

    def handle_trajectory_index_stats(self, body: dict[str, Any]) -> None:
        actor_id = normalize_text(str(body.get("actor_id") or body.get("user") or self.server.state.config.imported_owner_actor_id), 128)
        try:
            memory_scope = parse_memory_scope(body.get("memory_scope"))
        except ValueError as exc:
            self.respond_json(400, {"ok": False, "error": str(exc)})
            return
        target_user_id = normalize_text(str(body.get("target_user_id") or ""), 128) or None
        if not self.server.state.can_use_local_trajectory_lookup(
            actor_id=actor_id,
            memory_scope=memory_scope,
            target_user_id=target_user_id,
        ):
            self.respond_json(403, {"ok": False, "error": "local trajectory index stats are not allowed for this actor/scope"})
            return
        self.respond_json(
            200,
            {
                "ok": True,
                "trajectory_index": self.server.state.trajectory_chunk_index.stats(),
                "freshness": self.server.state.trajectory_chunk_index.freshness(),
            },
        )

    def handle_trajectory_read(self, body: dict[str, Any]) -> None:
        source_ref = normalize_text(str(body.get("source_ref") or body.get("session_id") or ""), 300)
        if not source_ref:
            self.respond_json(400, {"ok": False, "error": "source_ref is required"})
            return
        actor_id = normalize_text(str(body.get("actor_id") or body.get("user") or self.server.state.config.imported_owner_actor_id), 128)
        try:
            memory_scope = parse_memory_scope(body.get("memory_scope"))
        except ValueError as exc:
            self.respond_json(400, {"ok": False, "error": str(exc)})
            return
        target_user_id = normalize_text(str(body.get("target_user_id") or ""), 128) or None
        if not self.server.state.can_use_local_trajectory_lookup(
            actor_id=actor_id,
            memory_scope=memory_scope,
            target_user_id=target_user_id,
        ):
            self.respond_json(403, {"ok": False, "error": "local trajectory lookup is not allowed for this actor/scope"})
            return
        self.server.state.ensure_trajectory_chunk_index_current()
        query = normalize_text(str(body.get("query") or ""), 300)
        max_chars = int(body.get("max_chars", self.server.state.config.trajectory_evidence_chars))
        source = self.server.state.memory_store.get_trajectory_source(
            source_ref=source_ref,
            actor_id=actor_id,
            memory_scope=memory_scope,
            target_user_id=target_user_id,
        )
        result = None
        if source is not None:
            if not self.server.state.source_allowed_by_visibility(source):
                self.respond_json(403, {"ok": False, "error": "trajectory is hidden by visibility policy"})
                return
            metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
            file_path = str(metadata.get("file_path") or "")
            if file_path:
                result = read_payload_from_file(
                    source_name=str(source.get("source_name") or metadata.get("tool") or ""),
                    source_ref=str(source.get("source_ref") or source_ref),
                    session_id=str(metadata.get("session_id") or source_ref.split(":", 1)[-1]),
                    title=str(source.get("title") or "Untitled trajectory"),
                    updated_at=str(source.get("updated_at") or ""),
                    cwd=str(metadata.get("cwd") or ""),
                    file_path=file_path,
                    metadata=metadata,
                    query=query,
                    max_chars=max_chars,
                )
        if result is None:
            result = self.server.state.trajectory_lookup.read(
                source_ref,
                query=query,
                max_chars=max_chars,
            )
        if result is None:
            self.respond_json(404, {"ok": False, "error": "trajectory not found in allowed sources"})
            return
        if not self.server.state.source_allowed_by_visibility(result):
            self.respond_json(403, {"ok": False, "error": "trajectory is hidden by visibility policy"})
            return
        self.respond_json(200, {"ok": True, "trajectory": result})

    def handle_memory_promote(self, body: dict[str, Any]) -> None:
        actor_id = normalize_text(str(body.get("actor_id") or body.get("user") or ""), 128)
        scope = normalize_text(str(body.get("scope") or "private"), 32).lower()
        text = str(body.get("text") or "")
        if not actor_id:
            self.respond_json(400, {"ok": False, "error": "actor_id is required"})
            return
        try:
            record = self.server.state.memory_store.promote_memory(
                actor_id=actor_id,
                scope=scope,
                text=text,
                tags=parse_tags(body.get("tags")),
            )
        except ValueError as exc:
            self.respond_json(400, {"ok": False, "error": str(exc)})
            return
        except RuntimeError as exc:
            self.respond_json(500, {"ok": False, "error": str(exc)})
            return
        self.respond_json(200, {"ok": True, "memory": record})

    def handle_memory_import_batch(self, body: dict[str, Any]) -> None:
        actor_id = normalize_text(str(body.get("actor_id") or ""), 128)
        source_name = normalize_text(str(body.get("source_name") or ""), 64).lower()
        items = body.get("items")
        if not actor_id:
            self.respond_json(400, {"ok": False, "error": "actor_id is required"})
            return
        if not source_name:
            self.respond_json(400, {"ok": False, "error": "source_name is required"})
            return
        if not isinstance(items, list):
            self.respond_json(400, {"ok": False, "error": "items must be a list"})
            return

        try:
            auth_record = self.authenticate_sync_request(actor_id)
        except PermissionError as exc:
            status = 503 if "config is not present" in str(exc) else 403
            self.respond_json(status, {"ok": False, "error": str(exc)})
            return

        try:
            result = self.server.state.memory_store.import_records(
                actor_id=actor_id,
                source_name=source_name,
                items=items,
                client_id=normalize_text(str(body.get("client_id") or "unknown-client"), 128),
                batch_id=normalize_text(str(body.get("batch_id") or utc_now()), 200),
                allowed_sources=auth_record.allowed_sources or self.server.state.config.trajectory_sources,
            )
        except ValueError as exc:
            self.respond_json(400, {"ok": False, "error": str(exc)})
            return
        except RuntimeError as exc:
            self.respond_json(500, {"ok": False, "error": str(exc)})
            return

        self.respond_json(200, {"ok": True, **result})

    def handle_memory_sync_status(self, body: dict[str, Any]) -> None:
        actor_id = normalize_text(str(body.get("actor_id") or ""), 128)
        if not actor_id:
            self.respond_json(400, {"ok": False, "error": "actor_id is required"})
            return

        try:
            auth_record = self.authenticate_sync_request(actor_id)
        except PermissionError as exc:
            status = 503 if "config is not present" in str(exc) else 403
            self.respond_json(status, {"ok": False, "error": str(exc)})
            return

        raw_sources = body.get("sources")
        requested_sources: list[str]
        if isinstance(raw_sources, list):
            requested_sources = [
                normalize_text(str(value), 64).lower()
                for value in raw_sources
                if normalize_text(str(value), 64)
            ]
        else:
            requested_sources = auth_record.allowed_sources or self.server.state.config.trajectory_sources
        if auth_record.allowed_sources:
            requested_sources = [source for source in requested_sources if source in auth_record.allowed_sources]

        try:
            status = self.server.state.memory_store.get_sync_status(
                actor_id=actor_id,
                sources=requested_sources,
            )
        except ValueError as exc:
            self.respond_json(400, {"ok": False, "error": str(exc)})
            return
        self.respond_json(200, {"ok": True, **status})

    def handle_session_history(self, body: dict[str, Any]) -> None:
        session_key = safe_session_key(
            user=str(body.get("user") or DEFAULT_SESSION_MAIN_KEY),
            requested=str(body["session_key"]) if body.get("session_key") else None,
        )
        history = self.server.state.sessions.load_messages(session_key, limit=int(body.get("limit", 50)))
        self.respond_json(
            200,
            {
                "ok": True,
                "session_key": session_key,
                "history": history,
                "summary": self.server.state.sessions.get_session_summary(session_key),
            },
        )

    def handle_session_reset(self, body: dict[str, Any]) -> None:
        session_key = safe_session_key(
            user=str(body.get("user") or DEFAULT_SESSION_MAIN_KEY),
            requested=str(body["session_key"]) if body.get("session_key") else None,
        )
        archived_path = self.server.state.sessions.archive_session(session_key)
        if archived_path is None:
            self.respond_json(404, {"ok": False, "error": "session not found"})
            return
        self.respond_json(200, {"ok": True, "session_key": session_key, "archived_path": archived_path})

    def read_json_body(self) -> dict[str, Any]:
        length = self.headers.get("Content-Length")
        if not length:
            return {}
        try:
            size = int(length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        raw = self.rfile.read(size)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def authenticate_sync_request(self, actor_id: str):
        return self.server.state.sync_auth.authenticate(
            self.headers.get("Authorization"),
            actor_id,
        )

    def respond_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def respond_html(self, status: int, payload: str) -> None:
        encoded = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


class StandaloneServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], state: AppState) -> None:
        super().__init__(address, handler)
        self.state = state


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8787, type=int)
    parser.add_argument("--workspace-dir", default=str(root / "profiles" / "default"))
    parser.add_argument("--state-dir", default=str(root / "state"))
    parser.add_argument("--memory-index-path", default=str(root / "state" / "trajectory_memory.json"))
    parser.add_argument("--memory-db-path", default=str(root / "state" / "semantic_memory.sqlite3"))
    parser.add_argument("--trajectory-index-db-path", default=str(root / "state" / "trajectory_index.sqlite3"))
    parser.add_argument("--sync-tokens-path", default=str(root / "config" / "sync_tokens.json"))
    parser.add_argument("--no-autobuild-memory", action="store_true")
    return parser.parse_args()


def load_config(args: argparse.Namespace) -> AppConfig:
    provider_backend = os.environ.get("MODEL_BACKEND", "openai_compatible").strip().lower()
    if provider_backend not in {"openai_compatible", "codex_cli"}:
        raise SystemExit("MODEL_BACKEND must be 'openai_compatible' or 'codex_cli'")

    model_api_key = os.environ.get("MODEL_API_KEY", "").strip() or None
    model_api_style = os.environ.get("MODEL_API_STYLE", "chat_completions").strip().lower()
    if provider_backend == "openai_compatible" and model_api_style not in {"chat_completions", "responses"}:
        raise SystemExit("MODEL_API_STYLE must be 'chat_completions' or 'responses'")
    if provider_backend == "openai_compatible" and not model_api_key:
        raise SystemExit("MODEL_API_KEY is required for MODEL_BACKEND=openai_compatible")

    project_root = Path(args.state_dir).resolve().parent
    workspace_dir = args.workspace_dir
    codex_cwd = os.environ.get("CODEX_CWD", str(project_root.parent / "mybot-runtime" / "default"))
    codex_permission_profile = os.environ.get("CODEX_PERMISSION_PROFILE", "").strip()
    session_tail_pairs = int(os.environ.get("SESSION_TAIL_PAIRS", "12"))
    history_max_messages = int(os.environ.get("HISTORY_MAX_MESSAGES", str(session_tail_pairs * 2)))
    if history_max_messages < session_tail_pairs * 2:
        history_max_messages = session_tail_pairs * 2

    model_name = os.environ.get("MODEL_NAME", "").strip() or None
    if provider_backend == "openai_compatible" and model_name is None:
        model_name = "gpt-4.1-mini"

    sync_tokens_path = os.environ.get("SYNC_TOKENS_PATH", "").strip() or args.sync_tokens_path
    if sync_tokens_path != args.sync_tokens_path and not os.path.exists(sync_tokens_path):
        sync_tokens_path = args.sync_tokens_path

    return AppConfig(
        host=args.host,
        port=args.port,
        provider_backend=provider_backend,
        model_base_url=os.environ.get("MODEL_BASE_URL", "https://api.openai.com/v1"),
        model_api_key=model_api_key,
        model_name=model_name,
        model_api_style=model_api_style,
        workspace_dir=workspace_dir,
        state_dir=args.state_dir,
        memory_index_path=args.memory_index_path,
        memory_db_path=args.memory_db_path,
        trajectory_index_db_path=os.environ.get("TRAJECTORY_INDEX_DB_PATH", args.trajectory_index_db_path),
        history_max_messages=history_max_messages,
        history_max_chars=int(os.environ.get("HISTORY_MAX_CHARS", "24000")),
        autobuild_memory=not args.no_autobuild_memory,
        codex_command=os.environ.get("CODEX_COMMAND", "codex"),
        codex_cwd=codex_cwd,
        codex_sandbox=os.environ.get("CODEX_SANDBOX", "read-only"),
        codex_network_access=coerce_bool(os.environ.get("CODEX_NETWORK_ACCESS"), False),
        codex_permission_profile=codex_permission_profile,
        codex_ignore_user_config=coerce_bool(
            os.environ.get("CODEX_IGNORE_USER_CONFIG"),
            bool(codex_permission_profile),
        ),
        codex_disable_backend_resume=coerce_bool(
            os.environ.get("CODEX_DISABLE_BACKEND_RESUME"),
            bool(codex_permission_profile),
        ),
        codex_ephemeral=coerce_bool(os.environ.get("CODEX_EPHEMERAL"), False),
        codex_service_tier=os.environ.get("CODEX_SERVICE_TIER", "fast").strip(),
        mybot_tool_python=os.environ.get("MYBOT_TOOL_PYTHON", sys.executable or "python3"),
        embedding_model_name=os.environ.get("EMBEDDING_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2"),
        imported_owner_actor_id=os.environ.get("MEMORY_IMPORTED_OWNER_ID", "local-owner"),
        session_tail_pairs=session_tail_pairs,
        compaction_trigger_message_count=int(os.environ.get("COMPACTION_TRIGGER_MESSAGES", "40")),
        compaction_trigger_char_count=int(os.environ.get("COMPACTION_TRIGGER_CHARS", "16000")),
        memory_match_limit=int(os.environ.get("MEMORY_MATCH_LIMIT", "5")),
        trajectory_max_files_per_tool=int(os.environ.get("TRAJECTORY_MAX_FILES_PER_TOOL", "200")),
        trajectory_sources=[name.strip().lower() for name in os.environ.get("TRAJECTORY_SOURCES", "codex,claude").split(",") if name.strip()],
        trajectory_investigation_mode=os.environ.get("TRAJECTORY_INVESTIGATION_MODE", "auto"),
        trajectory_search_limit=int(os.environ.get("TRAJECTORY_SEARCH_LIMIT", "5")),
        trajectory_evidence_limit=int(os.environ.get("TRAJECTORY_EVIDENCE_LIMIT", "2")),
        trajectory_evidence_chars=int(os.environ.get("TRAJECTORY_EVIDENCE_CHARS", "18000")),
        trajectory_index_chunk_chars=int(os.environ.get("TRAJECTORY_INDEX_CHUNK_CHARS", "4800")),
        trajectory_index_overlap_chars=int(os.environ.get("TRAJECTORY_INDEX_OVERLAP_CHARS", "800")),
        trajectory_index_autobuild=coerce_bool(os.environ.get("TRAJECTORY_INDEX_AUTOBUILD"), True),
        trajectory_index_autobuild_vectors=coerce_bool(os.environ.get("TRAJECTORY_INDEX_AUTOBUILD_VECTORS"), False),
        trajectory_index_autobuild_min_interval_seconds=int(
            os.environ.get("TRAJECTORY_INDEX_AUTOBUILD_MIN_INTERVAL_SECONDS", "300")
        ),
        trajectory_index_refresh_max_sessions=int(os.environ.get("TRAJECTORY_INDEX_REFRESH_MAX_SESSIONS", "0")),
        trajectory_index_background_refresh_seconds=int(
            os.environ.get("TRAJECTORY_INDEX_BACKGROUND_REFRESH_SECONDS", "300")
        ),
        trajectory_agentic_search=coerce_bool(os.environ.get("TRAJECTORY_AGENTIC_SEARCH"), True),
        trajectory_agentic_max_steps=int(os.environ.get("TRAJECTORY_AGENTIC_MAX_STEPS", "6")),
        trajectory_query_planner=coerce_bool(os.environ.get("TRAJECTORY_QUERY_PLANNER"), True),
        trajectory_query_planner_max_rounds=int(os.environ.get("TRAJECTORY_QUERY_PLANNER_MAX_ROUNDS", "2")),
        trajectory_query_planner_max_queries=int(os.environ.get("TRAJECTORY_QUERY_PLANNER_MAX_QUERIES", "4")),
        trajectory_search_time_budget_seconds=float(os.environ.get("TRAJECTORY_SEARCH_TIME_BUDGET_SECONDS", "15")),
        agentic_tool_routing=coerce_bool(os.environ.get("AGENTIC_TOOL_ROUTING"), True),
        mybot_tool_path=os.environ.get("MYBOT_TOOL_PATH", str(Path(codex_cwd) / "bin" / "mybot_tool.py")),
        sync_tokens_path=sync_tokens_path,
    )


def main() -> None:
    args = parse_args()
    config = load_config(args)
    state = AppState(config)
    state.load_or_build_memory()
    refresh = state.ensure_trajectory_chunk_index_current()
    if refresh.get("rebuilt"):
        print("Refreshed stale trajectory chunk index before startup")
    state.start_trajectory_index_background_refresh()
    server = StandaloneServer((config.host, config.port), ChatHandler, state)
    print(f"Listening on http://{config.host}:{config.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
