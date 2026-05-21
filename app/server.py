#!/usr/bin/env python3
"""Standalone chat API inspired by OpenClaw-style structure, without runtime dependency."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from app.auth import SyncAuthStore
from app.semantic_memory import SemanticMemoryStore, VALID_MEMORY_SCOPES, parse_tags
from trajectory_memory import load_index


WORKSPACE_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "IDENTITY.md", "MEMORY.md"]
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
    history_max_messages: int
    history_max_chars: int
    autobuild_memory: bool
    codex_command: str
    codex_cwd: str
    codex_sandbox: str
    embedding_model_name: str
    imported_owner_actor_id: str
    session_tail_pairs: int
    compaction_trigger_message_count: int
    compaction_trigger_char_count: int
    memory_match_limit: int
    trajectory_max_files_per_tool: int
    trajectory_sources: list[str]
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
    ) -> dict[str, Any]:
        cmd = [self.config.codex_command, "-C", self.config.codex_cwd, "exec"]
        if backend_session_id:
            cmd.append("resume")
            cmd.extend(["--skip-git-repo-check", "--json"])
        else:
            cmd.extend(["--skip-git-repo-check", "--json"])
            if ephemeral:
                cmd.append("--ephemeral")
            cmd.extend(["--sandbox", self.config.codex_sandbox])
        if self.config.model_name:
            cmd.extend(["--model", self.config.model_name])
        if backend_session_id:
            cmd.append(backend_session_id)
        cmd.append(prompt)

        proc = subprocess.run(cmd, capture_output=True, text=True)
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
            "backend_session_id": thread_id,
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
    ) -> dict[str, Any]:
        if self.config.provider_backend == "codex_cli":
            prompt = self._build_codex_prompt(system_prompt=system_prompt, history=history, message=message)
            return self._run_codex(prompt=prompt, backend_session_id=backend_session_id, ephemeral=ephemeral)

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
    ) -> dict[str, Any]:
        return self.complete(
            system_prompt=system_prompt,
            history=history,
            message=message,
            backend_session_id=backend_session_id,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            ephemeral=False,
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
        self.memory_index: dict[str, Any] | None = None

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
        return {"index": index, "semantic": semantic}

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
            "say that the system does not have grounded memory for it instead of guessing."
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

        sources: list[dict[str, Any]] = []
        if use_memory:
            sources = self.memory_store.search(
                query=query,
                actor_id=actor_id,
                memory_scope=memory_scope,
                target_user_id=target_user_id,
                limit=match_limit,
            )

        if sources:
            lines = ["## Retrieved Memory"]
            for source in sources:
                lines.append(
                    "- "
                    f"[{source['scope']}][{source['source_type']}] "
                    f"{source['title']}: {source['text_preview']}"
                )
            sections.append("\n".join(lines))
        else:
            sections.append("## Retrieved Memory\nNo strong retrieved memory matches were found for this query.")

        sections.append(
            "## Response Rule\n"
            "Answer naturally and concisely. Mention uncertainty when the answer is not grounded "
            "in the retrieved memory or current session context."
        )
        return "\n\n".join(section for section in sections if section), sources, session_summary_used

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
        if self.path == "/health":
            cfg = self.server.state.config
            self.respond_json(
                200,
                {
                    "ok": True,
                    "provider_backend": cfg.provider_backend,
                    "model_base_url": cfg.model_base_url,
                    "model_name": cfg.model_name,
                    "model_api_style": cfg.model_api_style,
                    "workspace_dir": cfg.workspace_dir,
                    "memory_loaded": self.server.state.get_memory_index() is not None,
                    "semantic_memory_stats": self.server.state.memory_store.get_stats(),
                    "sync_auth_configured": self.server.state.sync_auth.configured(),
                },
            )
            return

        if self.path == "/memory":
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

        if self.path == "/chat":
            self.handle_chat(body)
            return
        if self.path == "/memory/rebuild":
            self.handle_memory_rebuild(body)
            return
        if self.path == "/memory/search":
            self.handle_memory_search(body)
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
            self.respond_json(500, {"ok": False, "error": str(exc)})
            return

        try:
            provider_result = self.server.state.provider.chat(
                system_prompt=system_prompt,
                history=history_for_model,
                message=message,
                backend_session_id=(self.server.state.sessions.get_backend_state(session_key) or {}).get("backend_session_id"),
                max_output_tokens=int(body["max_output_tokens"]) if body.get("max_output_tokens") is not None else None,
                temperature=float(body["temperature"]) if body.get("temperature") is not None else None,
            )
        except RuntimeError as exc:
            self.respond_json(502, {"ok": False, "error": str(exc)})
            return

        answer = provider_result["text"]
        response_hash = hashlib.sha256(answer.encode("utf-8")).hexdigest()[:16]

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
        except ValueError as exc:
            self.respond_json(400, {"ok": False, "error": str(exc)})
            return
        except RuntimeError as exc:
            self.respond_json(500, {"ok": False, "error": str(exc)})
            return
        self.respond_json(200, {"ok": True, "matches": matches})

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

    workspace_dir = args.workspace_dir
    project_root = Path(args.state_dir).resolve().parent
    codex_cwd = os.environ.get("CODEX_CWD", str(project_root))
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
        history_max_messages=history_max_messages,
        history_max_chars=int(os.environ.get("HISTORY_MAX_CHARS", "24000")),
        autobuild_memory=not args.no_autobuild_memory,
        codex_command=os.environ.get("CODEX_COMMAND", "codex"),
        codex_cwd=codex_cwd,
        codex_sandbox=os.environ.get("CODEX_SANDBOX", "read-only"),
        embedding_model_name=os.environ.get("EMBEDDING_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2"),
        imported_owner_actor_id=os.environ.get("MEMORY_IMPORTED_OWNER_ID", "local-owner"),
        session_tail_pairs=session_tail_pairs,
        compaction_trigger_message_count=int(os.environ.get("COMPACTION_TRIGGER_MESSAGES", "40")),
        compaction_trigger_char_count=int(os.environ.get("COMPACTION_TRIGGER_CHARS", "16000")),
        memory_match_limit=int(os.environ.get("MEMORY_MATCH_LIMIT", "5")),
        trajectory_max_files_per_tool=int(os.environ.get("TRAJECTORY_MAX_FILES_PER_TOOL", "200")),
        trajectory_sources=[name.strip().lower() for name in os.environ.get("TRAJECTORY_SOURCES", "codex,claude").split(",") if name.strip()],
        sync_tokens_path=sync_tokens_path,
    )


def main() -> None:
    args = parse_args()
    config = load_config(args)
    state = AppState(config)
    state.load_or_build_memory()
    server = StandaloneServer((config.host, config.port), ChatHandler, state)
    print(f"Listening on http://{config.host}:{config.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
