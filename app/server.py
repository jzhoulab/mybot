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
import sqlite3
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
from app.prompt_history import PromptHistoryStore
from app.trajectory_index import TrajectoryChunkIndex
from sources.access import (
    default_config_path,
    load_access_config,
    normalize_visibility_mode,
    save_access_config,
)
from sources.common import derive_bot_handle, format_bot_name
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

# USER.md is handled separately as the owner profile (prominent, generated).
WORKSPACE_FILES = ["AGENTS.md", "SOUL.md", "IDENTITY.md", "MEMORY.md"]
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


def on_battery_power() -> bool:
    """True only when we can POSITIVELY confirm the machine is on battery
    (macOS). Any error or non-macOS host returns False, so background indexing
    is only ever suppressed when we're sure — never accidentally paused on a
    server or when the power state can't be read."""
    if sys.platform != "darwin":
        return False
    try:
        proc = subprocess.run(
            ["/usr/bin/pmset", "-g", "ps"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "Battery Power" in proc.stdout


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
    records = [budget for budget in budgets if isinstance(budget, dict)]
    # Budget extensions are decisions, not retrievals: surface them separately
    # so the owner always sees when and why the agent took extra time.
    extensions = [r for r in records if r.get("tool") == "budget-extend"]
    valid = [r for r in records if r.get("tool") != "budget-extend"]
    if not valid and not extensions:
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

    summary = (
        f"Retrieval budget: {total_calls} tool call{'s' if total_calls != 1 else ''}, "
        f"{total_seconds:.2f}s, ~{compact_count(total_tokens)} tokens estimated"
        + (f" ({'; '.join(details)})." if details else ".")
    )
    for extension in extensions:
        granted = float(extension.get("extension_seconds") or 0.0)
        reason = normalize_text(str(extension.get("reason") or ""), 200)
        summary += f"\nBudget extended +{granted:.0f}s by the agent" + (f": {reason}" if reason else ".")
    return summary


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


def date_filter_keys(after: str, before: str) -> tuple[str, str]:
    """ISO-prefix comparison keys for hard time filters. A date-only `before`
    is made inclusive of that whole day ('~' sorts after every ISO char)."""
    after_key = (after or "").strip()
    before_key = (before or "").strip()
    if before_key and len(before_key) <= 10:
        before_key += "~"
    return after_key, before_key


def trajectory_source_in_window(
    source: dict[str, Any],
    *,
    after_key: str = "",
    before_key: str = "",
    source_filter: str = "",
) -> bool:
    if source_filter and str(source.get("source_name") or "").lower() != source_filter:
        return False
    if not (after_key or before_key):
        return True
    updated = str(source.get("updated_at") or "")
    if not updated:
        return False
    if after_key and updated < after_key:
        return False
    if before_key and updated > before_key:
        return False
    return True


# What an agent needs from a search hit: identity, freshness, where the match
# lives (chunk/event coordinates for a follow-up windowed read), and preview
# text. Internal bookkeeping (file paths, char offsets, channel flags, duplicate
# summaries) only burns the agent's context.
COMPACT_MATCH_METADATA_KEYS = (
    "session_id",
    "raw_session_id",
    "chunk_id",
    "chunk_index",
    "event_start",
    "event_end",
    "origin",
    "origin_detail",
)


def compact_trajectory_match(match: dict[str, Any]) -> dict[str, Any]:
    metadata = match.get("metadata") if isinstance(match.get("metadata"), dict) else {}
    compact: dict[str, Any] = {
        "source_ref": match.get("source_ref"),
        "source_name": match.get("source_name"),
        "title": match.get("title"),
        "updated_at": match.get("updated_at"),
        "cwd": match.get("cwd"),
        "score": match.get("match_score") or match.get("score"),
        "match_kind": match.get("match_kind"),
        "channels": match.get("_retrieval_channels") or [],
        "text_preview": match.get("text_preview") or match.get("summary_short") or "",
        "metadata": {
            key: metadata[key]
            for key in COMPACT_MATCH_METADATA_KEYS
            if metadata.get(key) not in (None, "")
        },
    }
    snippets = [normalize_text(str(snippet), 700) for snippet in (match.get("snippets") or [])[:3]]
    if snippets:
        compact["snippets"] = snippets
    return {key: value for key, value in compact.items() if value not in (None, "", [], {})}


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


def load_query_hints() -> list[tuple[str, str]]:
    """Per-deployment query expansions for jargon a general model can't guess.

    Set TRAJECTORY_QUERY_HINTS to `keyword=extra search terms` pairs separated
    by `;`. A query mentioning the keyword also gets searched with the extra
    terms appended, which is how you teach retrieval that your cluster's
    "balance" questions are phrased "insufficient allocation, no new jobs".

        TRAJECTORY_QUERY_HINTS="proddb=replica lag failover;billing=invoice dunning"
    """
    hints: list[tuple[str, str]] = []
    for entry in os.environ.get("TRAJECTORY_QUERY_HINTS", "").split(";"):
        keyword, _, expansion = entry.partition("=")
        keyword, expansion = keyword.strip().lower(), expansion.strip()
        if keyword and expansion:
            hints.append((keyword, expansion))
    return hints


QUERY_HINTS = load_query_hints()


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
    for keyword, expansion in QUERY_HINTS:
        if keyword in lowered:
            add(f"{focused} {expansion}")
            add(expansion)
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


def parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def looks_like_followup(query: str, prev_user_turn: str = "") -> bool:
    """Does this query lean on the prior conversation rather than stand alone?
    True for deixis/continuation markers, very short queries, or ones that
    share a distinctive anchor token with the previous user turn. Used to keep
    genuine follow-ups in the same session while letting topic-shifts start
    fresh."""
    text = query or ""
    if FOLLOWUP_QUERY_RE.search(text):
        return True
    tokens = query_tokens(text)
    if len(tokens) <= 2:
        return True
    if prev_user_turn:
        prev_anchors = {t for t in query_tokens(prev_user_turn) if len(t) >= 5}
        if prev_anchors and prev_anchors & {t for t in tokens if len(t) >= 5}:
            return True
    return False


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


class _ClientGone(Exception):
    """Raised when an SSE client disconnects mid-stream so we can stop cleanly."""


def _describe_tool(block: dict[str, Any]) -> str:
    """Friendly one-liner for a tool_use block, shown as live activity while the
    agent works. The mybot tool is invoked via Bash, so parse its subcommand."""
    name = str(block.get("name") or "tool")
    inp = block.get("input") or {}
    command = str(inp.get("command") or "") if isinstance(inp, dict) else ""
    if "mybot_tool" in command:
        m = re.search(r"mybot_tool\.py\s+([a-z-]+)", command)
        sub = (m.group(1) if m else "").replace("-", " ").strip()
        q = re.search(r'-q\s+["\']([^"\']+)', command) or re.search(r'--query\s+["\']([^"\']+)', command)
        if "read" in sub:
            return "Reading trajectory evidence…"
        if "search" in sub:
            return f"Searching memory: {q.group(1)[:50]}" if q else "Searching memory…"
        return "Running memory tool…" if not sub else f"Running {sub}…"
    if "sqlite3" in command:
        like = re.search(r"LIKE\s+'%([^%']+)%'", command, re.IGNORECASE)
        if like:
            return f"Grepping index: {like.group(1)[:50]}"
        if re.search(r"\bCOUNT\b|GROUP BY", command, re.IGNORECASE):
            return "Enumerating sessions…"
        return "Querying index (SQL)…"
    return f"Running {name}…"


def _tool_result_hits(content: Any) -> list[dict[str, str]]:
    """Openable trajectories a retrieval call surfaced, so the UI can show them
    the moment the tool returns instead of only in the final answer. Best-effort
    over possibly-truncated output; deduped, capped."""
    if isinstance(content, list):
        text = "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    else:
        text = str(content or "")
    text = text.strip()
    if not text:
        return []
    parsed: Any = None
    for candidate in (text, text[min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=0):]):
        try:
            parsed = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    records: list[dict[str, Any]] = []
    if isinstance(parsed, dict):
        if isinstance(parsed.get("matches"), list):
            records = [m for m in parsed["matches"] if isinstance(m, dict)]
        elif isinstance(parsed.get("trajectory"), dict):
            records = [parsed["trajectory"]]
    hits: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(ref: str, source: str, title: str) -> None:
        ref = ref.strip()
        if not ref or ref in seen:
            return
        seen.add(ref)
        source = (source or (ref.split(":", 1)[0] if ":" in ref else "")).strip()
        hits.append({"source_ref": ref, "source_name": source, "title": normalize_text(title, 90)})

    for rec in records:
        add(str(rec.get("source_ref") or ""), str(rec.get("source_name") or ""), str(rec.get("title") or ""))
        if len(hits) >= 6:
            return hits

    if not hits:
        # Truncated output: the objects before the cut are still intact, so
        # pull each source_ref and the title in its object. Keys are sorted, so
        # title trails source_ref (past a possibly-long text_preview) but before
        # the next record — bound the window to the next source_ref.
        refs = list(re.finditer(r'"source_ref"\s*:\s*"([^"]+)"', text))
        for i, m in enumerate(refs):
            ref = m.group(1)
            end = refs[i + 1].start() if i + 1 < len(refs) else len(text)
            window = text[m.end():end]
            title_m = re.search(r'"title"\s*:\s*"([^"]{0,90})"', window)
            add(ref, "", title_m.group(1) if title_m else "")
            if len(hits) >= 6:
                break
    return hits


def _summarize_tool_result(content: Any, *, is_error: bool = False) -> str:
    """One-line outcome for a finished tool call, streamed to the chat UI so
    retrieval is visible as it happens instead of only after the answer."""
    if isinstance(content, list):
        parts = [str(b.get("text") or "") for b in content if isinstance(b, dict)]
        text = "\n".join(part for part in parts if part)
    else:
        text = str(content or "")
    text = text.strip()
    if not text:
        return "no output" if not is_error else "failed"
    if is_error:
        return f"error: {normalize_text(text, 120)}"
    parsed: Any = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        if start > 0:
            try:
                parsed = json.loads(text[start:])
            except json.JSONDecodeError:
                parsed = None
    def salvage() -> str:
        """Long tool output gets truncated by the CLI before we see it, so the
        payload can be a fragment. Count surviving record markers rather than
        echoing a stray brace or our own per-call accounting."""
        hits = len(re.findall(r'"source_ref"\s*:', text))
        if hits:
            titles = re.findall(r'"title"\s*:\s*"([^"]{1,70})"', text)[:2]
            head = " · ".join(titles)
            return f"{hits}+ matches — {head}" if head else f"{hits}+ matches"
        budget_keys = (
            "_budget", "tool", "endpoint", "seconds", "tool_calls", "result_count",
            "timestamp", "tokens_estimate", "input_tokens_estimate",
            "output_tokens_estimate", "retrieval_steps", "query",
        )
        for line in text.splitlines():
            stripped = line.strip().strip(",").lstrip("{[").strip()
            if len(stripped) <= 3 or stripped in ("{", "}", "[", "]"):
                continue
            key = re.match(r'"([^"]+)"\s*:\s*(.*)$', stripped)
            if key and (key.group(1) in budget_keys or not key.group(2).strip(" {[")):
                continue  # accounting, or a container opener carrying no value
            return normalize_text(stripped, 120)
        return "completed"

    if parsed is None:
        return salvage()
    if isinstance(parsed, dict):
        matches = parsed.get("matches")
        if isinstance(matches, list):
            if not matches:
                return "no matches"
            titles = [
                str(m.get("title") or m.get("source_ref") or "").strip()
                for m in matches[:2]
                if isinstance(m, dict)
            ]
            head = " · ".join(t for t in titles if t)
            label = f"{len(matches)} match{'' if len(matches) == 1 else 'es'}"
            return f"{label} — {normalize_text(head, 90)}" if head else label
        rows = parsed.get("rows")
        if isinstance(rows, list):
            return f"{len(rows)} row{'' if len(rows) == 1 else 's'}"
        trajectory = parsed.get("trajectory")
        if isinstance(trajectory, dict):
            total = trajectory.get("total_events")
            title = normalize_text(str(trajectory.get("title") or ""), 70)
            span = f" of {total} events" if total else ""
            return f"read{span}{f' — {title}' if title else ''}"
        if parsed.get("error"):
            return f"error: {normalize_text(str(parsed['error']), 110)}"
    return salvage()


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
    codex_reasoning_effort: str
    codex_planner_reasoning_effort: str
    claude_command: str
    claude_model: str
    claude_thinking: str
    claude_planner_thinking: str
    model_config_path: str
    mybot_tool_python: str
    embedding_model_name: str
    imported_owner_actor_id: str
    owner_display_name: str
    team_name: str
    bot_name_template: str
    bot_handle_override: str
    guest_owner_access: bool
    claude_bash_sandbox: bool
    session_tail_pairs: int
    compaction_trigger_message_count: int
    compaction_trigger_char_count: int
    session_idle_rollover_seconds: float
    session_topic_shift_seconds: float
    group_context_window_seconds: float
    group_observe_max_messages: int
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
    trajectory_index_pause_on_battery: bool
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
        self._pointers_path = self.state_dir / "session_pointers.json"
        self._pointers_lock = threading.Lock()

    def session_path(self, session_key: str) -> Path:
        return self.sessions_dir / f"{slugify(session_key)}.jsonl"

    # -- Session routing: a stable LOGICAL key (per DM / channel / menu) points
    # at a rolling ACTIVE storage key. Idle conversations roll to a fresh active
    # session so perpetual threads stop growing unbounded and topics stop
    # bleeding, while a one-line carry-forward preserves continuity. --

    def _load_pointers(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._pointers_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_pointers(self, pointers: dict[str, dict[str, Any]]) -> None:
        try:
            self._pointers_path.write_text(
                json.dumps(pointers, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            log.warning("session pointer write failed", exc_info=True)

    def _carry_forward_from(self, active_key: str) -> str:
        summary = (self.get_session_summary(active_key) or {}).get("summary") or ""
        if summary:
            return normalize_text(summary, 600)
        # No summary yet — synthesize a crude gist from recent user turns.
        turns = [
            normalize_text(str(m.get("content") or ""), 160)
            for m in self.load_messages(active_key, limit=6)
            if m.get("role") == "user"
        ]
        return normalize_text(" / ".join(turns[-3:]), 600)

    def resolve_active_session(
        self, logical_key: str, *, idle_seconds: float | None, force_new: bool = False
    ) -> tuple[str, str]:
        """Map a logical key to its active storage key, rolling over when idle
        (or when force_new). Returns (active_key, carry_forward)."""
        logical = slugify(logical_key)
        now = datetime.now(timezone.utc)
        with self._pointers_lock:
            pointers = self._load_pointers()
            rec = dict(pointers.get(logical) or {})
            active = str(rec.get("active_key") or logical_key)
            carry = str(rec.get("carry_forward") or "")
            last = parse_iso(str(rec.get("last_activity") or ""))
            idle = (
                idle_seconds is not None
                and last is not None
                and (now - last).total_seconds() > idle_seconds
            )
            if force_new:
                # Explicit reset: fresh key, forget the carry-forward.
                epoch = int(rec.get("epoch") or 0) + 1
                active, carry = f"{logical_key}#e{epoch}", ""
                rec = {"active_key": active, "epoch": epoch, "carry_forward": ""}
            elif idle and bool(self.load_messages(active, limit=1)):
                carry = self._carry_forward_from(active)
                epoch = int(rec.get("epoch") or 0) + 1
                active = f"{logical_key}#e{epoch}"
                rec = {"active_key": active, "epoch": epoch, "carry_forward": carry}
            rec["active_key"] = active
            rec["last_activity"] = utc_now()
            rec.setdefault("carry_forward", carry)
            pointers[logical] = rec
            self._save_pointers(pointers)
        return active, carry

    def touch_session(self, logical_key: str) -> None:
        with self._pointers_lock:
            pointers = self._load_pointers()
            rec = pointers.get(slugify(logical_key))
            if isinstance(rec, dict):
                rec["last_activity"] = utc_now()
                self._save_pointers(pointers)

    def peek_active_key(self, logical_key: str) -> str:
        """Current active storage key without rolling over (for pre-resolve
        inspection, e.g. reading the last turn to judge a follow-up)."""
        rec = self._load_pointers().get(slugify(logical_key))
        if isinstance(rec, dict) and rec.get("active_key"):
            return str(rec["active_key"])
        return logical_key

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

    def list_sessions(self, prefix: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Enumerate stored chat threads whose logical key starts with `prefix`
        (e.g. all of the menu's 'menuapp-…' chats), newest first. Each entry
        carries a title (first user turn), a preview (last turn), the message
        count, and updated_at — enough to render a chat list without loading
        every transcript. Epoch suffixes (#eN) collapse to their logical key."""
        slug_prefix = slugify(prefix)
        threads: dict[str, dict[str, Any]] = {}
        for path in self.sessions_dir.glob("*.jsonl"):
            name = path.stem
            if not (name == slug_prefix or name.startswith(slug_prefix)):
                continue
            logical = re.sub(r"#e\d+$", "", name)
            messages = [e for e in self._iter_entries(name) if e.get("type") == "message"]
            if not messages:
                continue
            first_user = next(
                (str(m.get("content") or "") for m in messages if m.get("role") == "user"), ""
            )
            last = messages[-1]
            updated_at = str(last.get("timestamp") or "")
            existing = threads.get(logical)
            if existing and existing["updated_at"] >= updated_at and updated_at:
                existing["message_count"] += len(messages)
                continue
            merged_count = (existing["message_count"] if existing else 0) + len(messages)
            threads[logical] = {
                "session_key": logical,
                "title": normalize_text(first_user, 80) or "New chat",
                "preview": normalize_text(str(last.get("content") or ""), 120),
                "last_role": str(last.get("role") or ""),
                "message_count": merged_count,
                "updated_at": updated_at,
            }
        ordered = sorted(threads.values(), key=lambda t: t["updated_at"], reverse=True)
        return ordered[:limit]

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

        # Allow-list posture (matches the claude sandbox): deny ALL of home,
        # then re-allow only the runtime workspace (where mybot_tool lives).
        # The earlier deny-list named a few sensitive paths but left the rest
        # of home readable — the agent could read ~/.ssh, ~/.aws, and other
        # repos' secrets. Most-specific-path wins, so the narrow runtime read
        # beats the broad ~/ deny; the workspace_roots grant still supplies
        # write (verified: budget log under runtime/.mybot works). Everything
        # sensitive — the repo, .env, config/, state/, ~/.codex, ~/.claude —
        # sits under ~/ and is covered by the single deny.
        runtime_dir = Path(self.config.codex_cwd).expanduser().resolve()
        filesystem = {
            "~/": "deny",
            str(runtime_dir): "read",
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

    def active_model(self) -> dict[str, str]:
        """Effective backend/model/thinking, honoring a live override file so the
        menu app can switch models without a server restart. Falls back to the
        startup config. mtime-cached to avoid re-reading every request."""
        path = self.config.model_config_path
        override: dict[str, Any] = {}
        try:
            mtime = os.path.getmtime(path)
            cached = getattr(self, "_model_override_cache", None)
            if cached and cached[0] == mtime:
                override = cached[1]
            else:
                with open(path, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                override = loaded if isinstance(loaded, dict) else {}
                self._model_override_cache = (mtime, override)
        except (OSError, json.JSONDecodeError):
            override = {}

        backend = str(override.get("backend") or self.config.provider_backend).strip().lower()
        if backend == "claude_cli":
            model = str(override.get("model") or self.config.claude_model)
            thinking = str(override.get("thinking") or self.config.claude_thinking)
        elif backend == "codex_cli":
            model = str(override.get("model") or self.config.model_name or "gpt-5.5")
            thinking = str(override.get("thinking") or self.config.codex_reasoning_effort)
        else:
            model = str(override.get("model") or self.config.model_name or "")
            thinking = str(override.get("thinking") or "")
        return {"backend": backend, "model": model, "thinking": thinking}

    def _claude_sandbox_settings_json(self) -> str:
        """Per-invocation sandbox policy for the answer agent's Bash. Allow-list
        posture: all of home is deny-read except the runtime workspace (the
        mybot tool lives there and writes its budget log there); network is
        localhost-only so the tool can reach this server and nothing else."""
        runtime_dir = str(Path(self.config.codex_cwd).expanduser().resolve())
        return json.dumps(
            {
                "sandbox": {
                    "enabled": True,
                    "failIfUnavailable": True,
                    "autoAllowBashIfSandboxed": True,
                    "allowUnsandboxedCommands": False,
                    # Writes: the sandbox default (working directory + session
                    # tmp only) is exactly right because the subprocess cwd IS
                    # the runtime workspace. An explicit denyWrite("~/") is NOT
                    # used — it overrides allowWrite and silently broke the
                    # tool's budget log (and with it the anti-spiral guard).
                    "filesystem": {
                        "denyRead": ["~/"],
                        "allowRead": [runtime_dir],
                        "allowWrite": [runtime_dir],
                    },
                    "network": {
                        "allowedDomains": ["localhost", "127.0.0.1"],
                    },
                },
            }
        )

    def _run_claude(
        self,
        *,
        system_prompt: str,
        message: str,
        model: str,
        thinking: str,
        ephemeral: bool,
        tool_env: dict[str, str] | None,
        on_event: Any = None,
    ) -> dict[str, Any]:
        # Claude has named effort levels (low/medium/high/xhigh/max) via --effort.
        # The internal planner (ephemeral) runs tool-free + cheap; the user-facing
        # answer gets restricted Bash to run mybot_tool + the configured effort.
        effort = (self.config.claude_planner_thinking if ephemeral else thinking).strip().lower()
        streaming = on_event is not None

        cmd = [self.config.claude_command, "-p", "--model", model,
               "--permission-mode", "default"]
        if streaming:
            cmd.extend(["--output-format", "stream-json", "--include-partial-messages", "--verbose"])
        else:
            cmd.extend(["--output-format", "json"])
        if effort:
            cmd.extend(["--effort", effort])
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])
        # The answer agent's shell: with CLAUDE_BASH_SANDBOX (default, needs
        # claude >= 2.1.154) Bash runs inside the OS sandbox (macOS Seatbelt) —
        # broad text tools and pipes are allowed because the FILESYSTEM is the
        # boundary: home is deny-read except the runtime workspace, so grep/sed
        # /awk magic works on tool output but ~/.env, ~/.claude/projects (raw
        # jsonl with excluded sessions), and the repo's config/state stay
        # unreadable; network is localhost-only. Sandboxed commands auto-run
        # (autoAllowBashIfSandboxed), and failIfUnavailable means we fail loud
        # rather than silently running Bash unsandboxed. The legacy fallback is
        # the strict single-prefix whitelist. Planner (ephemeral) gets no tools.
        if not ephemeral:
            if self.config.claude_bash_sandbox:
                cmd.extend(["--settings", self._claude_sandbox_settings_json()])
                cmd.extend(["--allowedTools", "Bash"])
            else:
                tool_prefix = f"{self.config.mybot_tool_python} {self.config.mybot_tool_path}"
                cmd.extend(["--allowedTools", f"Bash({tool_prefix}:*)"])
        # NOTE: prompt goes via stdin, NOT as a positional arg — the variadic
        # --allowedTools/--append flags would otherwise swallow it.

        env = os.environ.copy()
        if tool_env:
            env.update(tool_env)

        # Run from the runtime workspace, not the repo: the sandbox implicitly
        # trusts the working directory, and the repo (code, .env, state) must
        # stay outside the boundary.
        claude_cwd = str(Path(self.config.codex_cwd).expanduser())

        if streaming:
            return self._run_claude_stream(cmd, message, env, on_event, cwd=claude_cwd)

        proc = subprocess.run(cmd, input=message, capture_output=True, text=True, env=env, cwd=claude_cwd)
        if proc.returncode != 0:
            stderr = normalize_text(proc.stderr, 1500)
            stdout = normalize_text(proc.stdout, 1500)
            raise RuntimeError(f"Claude CLI failed (exit {proc.returncode}). stderr={stderr} stdout={stdout}")

        text = ""
        raw: Any = None
        try:
            raw = json.loads(proc.stdout)
            if isinstance(raw, dict):
                text = str(raw.get("result") or "")
                if raw.get("is_error"):
                    raise RuntimeError(f"Claude CLI reported error: {text[:500]}")
        except json.JSONDecodeError:
            text = proc.stdout.strip()
        return {
            "text": text.strip(),
            "raw": raw,
            "provider_style": "claude_cli",
            "backend_session_id": None,
            "usage": (raw or {}).get("usage") if isinstance(raw, dict) else None,
        }

    def _run_claude_stream(self, cmd, message, env, on_event, cwd: str | None = None) -> dict[str, Any]:
        """Popen the claude CLI in stream-json mode and forward events via
        on_event({kind, ...}) as they arrive. Accumulates the final answer text.
        on_event kinds: "tool" (label), "delta" (text), "usage" (dict)."""
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, bufsize=1, cwd=cwd,
        )
        assert proc.stdin and proc.stdout
        try:
            proc.stdin.write(message)
            proc.stdin.close()
        except BrokenPipeError:
            pass

        text_parts: list[str] = []
        result_text = ""
        usage: Any = None
        tool_blocks: dict[int, dict[str, Any]] = {}  # index -> {name, input_json}
        tool_labels: dict[str, str] = {}  # tool_use_id -> label, to pair results
        break_before_text = False  # separate narration segments across tool calls
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")
            if etype == "stream_event":
                inner = evt.get("event", {})
                itype = inner.get("type")
                idx = inner.get("index", 0)
                if itype == "content_block_delta":
                    delta = inner.get("delta") or {}
                    if delta.get("type") == "input_json_delta":
                        # tool_use input streams here — accumulate for the label
                        if idx in tool_blocks:
                            tool_blocks[idx]["input_json"] += str(delta.get("partial_json") or "")
                    else:
                        text = delta.get("text") or ""
                        if text:
                            if break_before_text and text_parts:
                                text_parts.append("\n\n")
                                on_event({"kind": "delta", "text": "\n\n"})
                            break_before_text = False
                            text_parts.append(text)
                            on_event({"kind": "delta", "text": text})
                elif itype == "content_block_start":
                    block = inner.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        tool_blocks[idx] = {"name": block.get("name") or "tool", "input_json": ""}
                elif itype == "content_block_stop" and idx in tool_blocks:
                    tb = tool_blocks.pop(idx)
                    try:
                        parsed_input = json.loads(tb["input_json"] or "{}")
                    except json.JSONDecodeError:
                        parsed_input = {}
                    break_before_text = True
                    on_event({"kind": "tool",
                              "label": _describe_tool({"name": tb["name"], "input": parsed_input})})
            elif etype == "assistant":
                # Full tool_use blocks carry the id the matching result will
                # reference; remember the label so the result can name itself.
                for block in (evt.get("message", {}) or {}).get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_id = str(block.get("id") or "")
                        if tool_id:
                            tool_labels[tool_id] = _describe_tool(block)
            elif etype == "user":
                # Tool results come back as a synthetic user turn. Stream a
                # compact outcome per call so the UI can show retrieval
                # landing while the answer is still being composed.
                for block in (evt.get("message", {}) or {}).get("content", []) or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    tool_id = str(block.get("tool_use_id") or "")
                    is_error = bool(block.get("is_error"))
                    on_event({
                        "kind": "tool_result",
                        "label": tool_labels.pop(tool_id, "Tool"),
                        "summary": _summarize_tool_result(block.get("content"), is_error=is_error),
                        "ok": not is_error,
                        "hits": [] if is_error else _tool_result_hits(block.get("content")),
                    })
            elif etype == "result":
                result_text = str(evt.get("result") or "")
                usage = evt.get("usage")
                if evt.get("is_error"):
                    raise RuntimeError(f"Claude CLI reported error: {result_text[:500]}")
        proc.wait()
        if proc.returncode not in (0, None):
            err = normalize_text(proc.stderr.read() if proc.stderr else "", 1000)
            raise RuntimeError(f"Claude CLI failed (exit {proc.returncode}). stderr={err}")
        # The result field is the CLI's own final answer — the last assistant
        # message only. The accumulated deltas span the WHOLE agentic loop
        # (planning narration between tool calls included), so preferring them
        # glued pre-search monologue onto the answer ("…Let me dig into
        # both.Both are…"). Deltas remain the live-streaming feed; the final
        # reply is the final message.
        text = result_text.strip() or ("".join(text_parts)).strip()
        if usage:
            on_event({"kind": "usage", "usage": usage})
        return {"text": text, "raw": None, "provider_style": "claude_cli",
                "backend_session_id": None, "usage": usage}

    def _run_codex(
        self,
        *,
        prompt: str,
        backend_session_id: str | None,
        ephemeral: bool = False,
        tool_env: dict[str, str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        if self.config.codex_disable_backend_resume:
            backend_session_id = None

        cmd = [self.config.codex_command]
        if self.config.codex_service_tier:
            cmd.extend(["-c", f'service_tier="{self.config.codex_service_tier}"'])
        # Reasoning effort per call: the internal query planner (ephemeral) runs
        # cheap so retrieval stays fast; user-facing answers think at full depth.
        effort = reasoning_effort or (
            self.config.codex_planner_reasoning_effort
            if ephemeral
            else self.config.codex_reasoning_effort
        )
        if effort:
            cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
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
        effective_model = model or self.config.model_name
        if effective_model:
            cmd.extend(["--model", effective_model])
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
        on_event: Any = None,
    ) -> dict[str, Any]:
        active = self.active_model()
        if active["backend"] == "claude_cli":
            # Claude gets the mybot context via --append-system-prompt and the
            # history+message folded into the positional prompt (same shape as codex).
            user_prompt = self._build_codex_prompt(system_prompt="", history=history, message=message)
            return self._run_claude(
                system_prompt=system_prompt,
                message=user_prompt,
                model=active["model"],
                thinking=active["thinking"],
                ephemeral=ephemeral,
                tool_env=tool_env,
                on_event=on_event,
            )

        if active["backend"] == "codex_cli":
            prompt = self._build_codex_prompt(system_prompt=system_prompt, history=history, message=message)
            return self._run_codex(
                prompt=prompt,
                backend_session_id=backend_session_id,
                ephemeral=ephemeral,
                tool_env=tool_env,
                model=active["model"],
                reasoning_effort=(active["thinking"] if not ephemeral else None),
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
        on_event: Any = None,
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
            on_event=on_event,
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


class PersonRegistry:
    """Durable per-person record of who has talked to this bot: display name,
    contact cadence, and recent topics. This is what lets the bot greet a lab
    member as a known person months later, on Discord today and Slack later —
    keyed by transport-agnostic actor_id."""

    MAX_RECENT_TOPICS = 10

    def __init__(self, state_dir: str) -> None:
        self.path = Path(state_dir) / "people.json"
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, actor_id: str) -> dict[str, Any] | None:
        record = self._load().get(normalize_text(actor_id, 128))
        return dict(record) if isinstance(record, dict) else None

    def note_interaction(
        self,
        *,
        actor_id: str,
        display_name: str = "",
        channel_label: str = "",
        topic: str = "",
        observed: bool = False,
    ) -> None:
        """Best-effort update; never let bookkeeping break a chat."""
        actor_id = normalize_text(actor_id, 128)
        if not actor_id:
            return
        now = utc_now()
        try:
            with self._lock:
                data = self._load()
                record = data.get(actor_id)
                if not isinstance(record, dict):
                    record = {"first_seen": now, "chat_count": 0, "observed_count": 0}
                if display_name:
                    record["display_name"] = normalize_text(display_name, 80)
                record["last_seen"] = now
                if observed:
                    record["observed_count"] = int(record.get("observed_count") or 0) + 1
                else:
                    record["chat_count"] = int(record.get("chat_count") or 0) + 1
                    if topic:
                        topics = record.get("recent_topics")
                        topics = topics if isinstance(topics, list) else []
                        topics.append(
                            {
                                "at": now,
                                "channel": normalize_text(channel_label, 80),
                                "topic": normalize_text(topic, 140),
                            }
                        )
                        record["recent_topics"] = topics[-self.MAX_RECENT_TOPICS:]
                data[actor_id] = record
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
        except OSError:
            log.warning("person registry write failed for actor %s", actor_id, exc_info=True)

    def set_conversation_gist(self, *, actor_id: str, gist: str) -> None:
        """Store the rolling gist of what this person and the bot have discussed,
        so long-term continuity survives session rollover without living inside
        the transcript. Best-effort."""
        actor_id = normalize_text(actor_id, 128)
        gist = normalize_text(gist, 900)
        if not actor_id or not gist:
            return
        try:
            with self._lock:
                data = self._load()
                record = data.get(actor_id)
                if not isinstance(record, dict):
                    record = {"first_seen": utc_now(), "chat_count": 0, "observed_count": 0}
                record["conversation_gist"] = gist
                record["gist_updated_at"] = utc_now()
                data[actor_id] = record
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
        except OSError:
            log.warning("person gist write failed for actor %s", actor_id, exc_info=True)


class AppState:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.ensure_runtime_tool_wrapper()
        self.lock = threading.Lock()
        self.workspace = PromptWorkspace(config.workspace_dir)
        self.sessions = SessionStore(config.state_dir)
        self.people = PersonRegistry(config.state_dir)
        self._identity_lock = threading.Lock()
        self._identity_investigating = False
        self._identity_error = ""
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
        # Durable "what did I ask, when, where" layer: the CLIs' typed-prompt
        # logs outlive their transcripts, so this answers even when a session
        # file was cleaned up. Same DB as the chunk index so SQL reaches it.
        self.prompt_history = PromptHistoryStore(config.trajectory_index_db_path)
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
        self.background_refresh_paused_on_battery: bool = False

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

    def prompt_history_hits(self, query: str, *, limit: int = 6, after: str = "", before: str = "", source_name: str = "") -> list[dict[str, Any]]:
        try:
            return self.prompt_history.search(query, limit=limit, after=after, before=before, source_name=source_name)
        except Exception as exc:  # pragma: no cover - never let the side index break a turn
            log.warning("prompt history search failed: %s", exc)
            return []

    def refresh_trajectory_chunk_index(self, *, include_vectors: bool = False) -> dict[str, Any]:
        result = self.trajectory_chunk_index.refresh_changed(
            include_vectors=include_vectors,
            max_sessions=self.config.trajectory_index_refresh_max_sessions,
            is_allowed=self._session_allowed_by_policy,
        )
        if result.get("sessions_updated") or result.get("sessions_removed"):
            self.trajectory_lookup.clear()
        result["prompt_history"] = self.refresh_prompt_history()
        return result

    def prompt_history_paths(self) -> dict[str, list[str]]:
        """Each enabled source root's typed-prompt log: `<base_dir>/history.jsonl`
        (Codex: ~/.codex/history.jsonl) or its parent's (Claude: base_dir is
        ~/.claude/projects, the log is ~/.claude/history.jsonl)."""
        out: dict[str, list[str]] = {}
        for source_name in self.config.trajectory_sources:
            seen: list[str] = []
            for account in self.access_config.accounts_for_source(source_name):
                if not getattr(account, "enabled", True):
                    continue
                base = os.path.expanduser(str(getattr(account, "expanded_base_dir", "") or account.base_dir))
                for candidate in (
                    os.path.join(base, "history.jsonl"),
                    os.path.join(os.path.dirname(base.rstrip(os.sep)), "history.jsonl"),
                ):
                    if os.path.isfile(candidate) and candidate not in seen:
                        seen.append(candidate)
            if seen:
                out[source_name] = seen
        return out

    def refresh_prompt_history(self) -> dict[str, Any]:
        try:
            try:
                policy_path = default_config_path()
                policy_key = (
                    hashlib.sha1(policy_path.read_bytes()).hexdigest()[:12] if policy_path.exists() else ""
                )
            except OSError:
                policy_key = ""
            return self.prompt_history.refresh(
                history_paths=self.prompt_history_paths(),
                is_allowed=self._session_allowed_by_policy,
                policy_key=policy_key,
            )
        except Exception as exc:  # pragma: no cover - best-effort side index
            log.warning("prompt history refresh failed: %s", exc)
            return {"error": str(exc)}

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
                human_sessions = int(entry.get("human_sessions") or 0)
                # Only a human session makes a project review-worthy; dedicated
                # agent-chat workspaces and private (owner-only) chat homes
                # never are — those sessions stay indexed, they just aren't a
                # project the owner opened.
                review_worthy = (
                    human_sessions > 0
                    and not self.access_config.is_agent_workspace(cwd)
                    and not self.access_config.is_private_workdir(source_name, cwd)
                )
                existing = projects.get(key)
                if existing is None:
                    if not review_worthy and not first_run:
                        continue
                    projects[key] = {
                        "source_name": source_name,
                        "cwd": cwd,
                        "first_seen": utc_now(),
                        "sessions": sessions,
                        "reviewed": bool(first_run),
                    }
                    changed = True
                elif not existing.get("reviewed") and not review_worthy:
                    del projects[key]
                    changed = True
                elif existing.get("sessions") != sessions:
                    existing["sessions"] = sessions
                    changed = True
            # A pending card must not outlive its evidence: if the project has
            # left the index entirely (sessions excluded, reclassified, or
            # deleted), drop the unreviewed entry instead of showing a card
            # whose detail view is empty.
            breakdown_keys = {
                f"{entry.get('source_name')}:{str(entry.get('cwd') or '').strip()}"
                for entry in breakdown
            }
            for key in [
                k for k, v in projects.items()
                if not v.get("reviewed") and k not in breakdown_keys
            ]:
                del projects[key]
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

    def maybe_run_deep_backfill(self) -> None:
        """One-time full-history indexing pass. The steady-state scan window is
        the most-recent N files per tool (energy: every cycle re-parses the
        window), so sessions older than the window were never indexed. Raise
        the window once, index everything, and let the purge-only-when-file-
        gone rule keep the old sessions from then on. Runs only on AC power
        (the caller gates on battery) and only once (marker file)."""
        marker = Path(self.config.state_dir) / "deep_backfill_done.json"
        if marker.exists():
            return
        lookup = self.trajectory_chunk_index.lookup
        original_cap = lookup.max_files_per_tool
        deep_cap = max(original_cap, 10000)
        print(f"Deep backfill: one-time indexing pass over up to {deep_cap} files per tool…")
        try:
            lookup.max_files_per_tool = deep_cap
            result = self.trajectory_chunk_index.refresh_changed(
                include_vectors=False,
                max_sessions=0,
                is_allowed=self._session_allowed_by_policy,
            )
            marker.write_text(json.dumps({"at": utc_now(), "result": {
                "sessions_seen": result.get("sessions_seen"),
                "sessions_updated": result.get("sessions_updated"),
                "chunks_inserted": result.get("chunks_inserted"),
            }}, indent=2))
            print(
                "Deep backfill complete: "
                f"{result.get('sessions_updated', 0)} sessions indexed, "
                f"{result.get('chunks_inserted', 0)} chunks (embeddings fill in gradually)."
            )
            self.trajectory_lookup.clear()
        except Exception as exc:  # pragma: no cover - best-effort maintenance
            print(f"Deep backfill failed (will retry next cycle): {exc}")
        finally:
            lookup.max_files_per_tool = original_cap

    def start_trajectory_index_background_refresh(self) -> None:
        interval = max(0, self.config.trajectory_index_background_refresh_seconds)
        if interval <= 0 or self.trajectory_index_background_started:
            return
        self.trajectory_index_background_started = True

        def worker() -> None:
            while True:
                time.sleep(interval)
                # Don't drain the battery indexing in the background. On-demand
                # refresh during a user's search still runs; only this unattended
                # loop backs off. Resumes automatically on AC power.
                if self.config.trajectory_index_pause_on_battery and on_battery_power():
                    if not self.background_refresh_paused_on_battery:
                        self.background_refresh_paused_on_battery = True
                        print(
                            "Trajectory index background refresh paused (on battery). "
                            "Set TRAJECTORY_INDEX_PAUSE_ON_BATTERY=false to override."
                        )
                    continue
                if self.background_refresh_paused_on_battery:
                    self.background_refresh_paused_on_battery = False
                    print("Trajectory index background refresh resumed (on AC power).")
                self.maybe_run_deep_backfill()
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

    def actor_is_owner(self, actor_id: str) -> bool:
        owner_id = normalize_text(self.config.imported_owner_actor_id, 128)
        return bool(owner_id) and normalize_text(actor_id, 128) == owner_id

    def trajectory_payload_is_private(self, payload: dict[str, Any]) -> bool:
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        if metadata.get("visibility") == "private":
            return True
        # Also honor the LIVE policy by cwd: chunks indexed before a workdir
        # was marked private carry no stamp, and a config edit must take
        # effect immediately, not at next re-index.
        source_name = str(payload.get("source_name") or metadata.get("tool") or "")
        cwd = str(payload.get("cwd") or metadata.get("cwd") or "")
        return bool(source_name and cwd) and self.access_config.is_private_workdir(source_name, cwd)

    def strip_private_trajectory_payloads(
        self, payloads: list[dict[str, Any]], actor_id: str
    ) -> list[dict[str, Any]]:
        """Owner-only sessions (visibility=private) never leave the machine for
        another actor, even with guest owner-access enabled. Row filtering works
        for search/read; raw SQL cannot be filtered, so /trajectory/sql is
        owner-only outright."""
        if self.actor_is_owner(actor_id):
            return payloads
        return [p for p in payloads if not self.trajectory_payload_is_private(p)]

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
            # The bot is the owner's representative: with guest access on
            # (default), teammates query the owner's history through it —
            # that's the collaboration point. Set GUEST_OWNER_ACCESS=false to
            # restrict trajectory lookup to the owner themself.
            return self.config.guest_owner_access or normalize_text(actor_id, 128) == owner_id
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
        after: str = "",
        before: str = "",
        source_filter: str = "",
    ) -> list[dict[str, Any]]:
        channel_limit = max(limit * 2, self.config.trajectory_search_limit, 8)
        after_key, before_key = date_filter_keys(after, before)
        source_filter = (source_filter or "").strip().lower()
        filters_active = bool(after_key or before_key or source_filter)
        if filters_active:
            # Channels without SQL-level filters get post-filtered below, so
            # over-fetch to keep the surviving candidate pool comparable.
            channel_limit *= 3

        def in_window(match: dict[str, Any]) -> bool:
            return not filters_active or trajectory_source_in_window(
                match, after_key=after_key, before_key=before_key, source_filter=source_filter
            )

        by_ref: dict[str, dict[str, Any]] = {}
        counts: dict[str, int] = {}

        for source in seed_sources or []:
            if not in_window(source):
                continue
            merge_trajectory_source(by_ref, source, query=query, channel="seed")
        if seed_sources:
            counts["seed"] = len(seed_sources)

        if include_index:
            for mode in index_modes:
                index_matches = self.trajectory_chunk_index.search(
                    query=query,
                    limit=channel_limit,
                    mode=mode,
                    after=after,
                    before=before,
                    source=source_filter,
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
                if not self.source_allowed_by_visibility(match) or not in_window(match):
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
                if not self.source_allowed_by_visibility(match) or not in_window(match):
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
                if not self.source_allowed_by_visibility(match) or not in_window(match):
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
            "locate the specific evidence. Prefer concrete entities: project, service, and host "
            "names, tool or command names, identifiers such as job or ticket codes, and file "
            "names. Expand shorthand into likely full terms. "
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
        after: str = "",
        before: str = "",
        source_filter: str = "",
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
            after=after,
            before=before,
            source_filter=source_filter,
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
                after=after,
                before=before,
                source_filter=source_filter,
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
                    after=after,
                    before=before,
                    source_filter=source_filter,
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
                        after=after,
                        before=before,
                        source_filter=source_filter,
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

        results = sort_trajectory_sources(list(combined_by_ref.values()), query=query, limit=limit)
        return self.strip_private_trajectory_payloads(results, actor_id)

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
        budget_seconds: float | None = None,
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
        if budget_seconds is not None:
            # Asker's per-request override of the default retrieval budget
            # (clamped to the owner-set ceiling; the tool clamps again).
            env["MYBOT_TOOL_TOTAL_BUDGET_SECONDS"] = str(
                max(10.0, min(float(budget_seconds), self.retrieval_budget_ceiling()))
            )
        return env

    @staticmethod
    def retrieval_budget_ceiling() -> float:
        try:
            return float(os.environ.get("MYBOT_TOOL_MAX_BUDGET_SECONDS", "600"))
        except ValueError:
            return 600.0

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

    def sources_from_retrieval_budgets(
        self, budgets: list[dict[str, Any]], actor_id: str
    ) -> list[dict[str, Any]]:
        """In agentic-tool-routing mode the agent retrieves through the CLI
        subprocess, so the server never sees the hits directly — the client's
        'grounding sources' list would be empty. The tool logs the (ref, title)
        of everything it opened/matched into the budget log; reconstruct the
        source chips from there. A session read is stronger evidence than a
        mere search match, so reads win when a ref appears as both."""
        best: dict[str, dict[str, Any]] = {}
        for budget in budgets:
            for source in budget.get("sources") or []:
                if not isinstance(source, dict):
                    continue
                ref = str(source.get("source_ref") or "")
                if not ref:
                    continue
                kind = str(source.get("record_kind") or "match")
                prior = best.get(ref)
                if prior is not None and prior.get("record_kind") == "read" and kind != "read":
                    continue
                best[ref] = {
                    "record_kind": kind,
                    "scope": "private",
                    "source_type": "trajectory",
                    "source_name": str(source.get("source_name") or ref.split(":", 1)[0]),
                    "source_ref": ref,
                    "parent_source_ref": None,
                    "title": normalize_text(str(source.get("title") or ""), 160),
                    "summary_short": "",
                    "text_preview": "",
                    "updated_at": "",
                    "metadata": {},
                    "match_score": source.get("match_score"),
                }
        ordered = sorted(
            best.values(),
            key=lambda record: (record.get("record_kind") != "read", -(record.get("match_score") or 0)),
        )
        # Reads are what the agent actually zoomed into — always keep them; cap
        # the trailing search matches so multi-query answers don't sprawl into
        # a dozen loosely-related badges.
        reads = [r for r in ordered if r.get("record_kind") == "read"]
        matches = [r for r in ordered if r.get("record_kind") != "read"]
        capped = reads + matches[: max(0, 6 - len(reads))]
        return self.strip_private_trajectory_payloads(capped, actor_id)

    OWNER_PROFILE_FILE = "USER.md"
    OWNER_INVESTIGATION_PROMPT = (
        "Investigate my own trajectory memory and write a concise profile of ME, the owner "
        "of this assistant — so future answers are grounded in who I am. Search across my "
        "sessions and cover: my role/field, the domains and systems I work on, my active "
        "projects and tools, and my working style/preferences. Be specific and grounded in "
        "evidence; do not invent. Output ONLY the profile as 4-8 short markdown bullets, no preamble."
    )

    def owner_profile_path(self) -> Path:
        return Path(self.config.workspace_dir) / self.OWNER_PROFILE_FILE

    def get_owner_profile(self) -> str:
        try:
            return self.owner_profile_path().read_text().strip()
        except OSError:
            return ""

    def save_owner_profile(self, text: str) -> None:
        path = self.owner_profile_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.strip() + "\n")

    def generate_owner_profile(self) -> str:
        """Run the agent to investigate trajectories and draft an owner profile.
        Returns the draft; the caller decides whether to save it."""
        actor_id = normalize_text(self.config.imported_owner_actor_id, 128)
        system_prompt, _sources, _summary = self.build_system_prompt(
            query=self.OWNER_INVESTIGATION_PROMPT,
            actor_id=actor_id,
            memory_scope="private",
            target_user_id=None,
            session_summary="",
            use_memory=True,
            match_limit=self.config.memory_match_limit,
        )
        budget_log = (
            self.new_retrieval_budget_log_path()
            if self.config.agentic_tool_routing else None
        )
        tool_env = self.tool_env_for_request(
            actor_id=actor_id, memory_scope="private", target_user_id=None,
            retrieval_budget_log_path=str(budget_log) if budget_log else None,
        )
        result = self.provider.chat(
            system_prompt=system_prompt, history=[],
            message=self.OWNER_INVESTIGATION_PROMPT, tool_env=tool_env,
        )
        text = str(result.get("text") or "").strip()
        # Drop any preamble before the first bullet/heading (the model sometimes
        # adds "Here's the profile:" despite instructions).
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.lstrip().startswith(("-", "*", "#")):
                return "\n".join(lines[i:]).strip()
        return text

    OWNER_IDENTITY_FILE = "owner_identity.json"
    OWNER_IDENTITY_PROMPT = (
        "Sherlock session: deduce who the OWNER of this assistant is from their trajectory "
        "memory alone. Hunt for the owner's real name and the handles they go by — git author "
        "lines, home-directory and cluster usernames, self-introductions, how collaborators "
        "address them. Cross-check at least two independent pieces of evidence; do not guess "
        "from a single path segment. Output ONLY strict JSON, nothing else:\n"
        '{"display_name": "<most natural full name>", "aliases": ["<handle or short name>", ...], '
        '"evidence": ["<one short line each, max 4>"], "confidence": "high|medium|low"}'
    )

    def owner_identity_path(self) -> Path:
        return Path(self.config.state_dir) / self.OWNER_IDENTITY_FILE

    def get_owner_identity(self) -> dict[str, Any]:
        try:
            data = json.loads(self.owner_identity_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_owner_identity(
        self,
        *,
        display_name: str,
        aliases: list[str] | None = None,
        evidence: list[str] | None = None,
        confidence: str = "",
        confirmed: bool = False,
    ) -> dict[str, Any]:
        identity = {
            "display_name": normalize_text(display_name, 80),
            "aliases": [normalize_text(str(alias), 60) for alias in (aliases or []) if str(alias).strip()][:8],
            "evidence": [normalize_text(str(item), 160) for item in (evidence or [])][:4],
            "confidence": normalize_text(confidence, 16),
            "confirmed": confirmed,
            "updated_at": utc_now(),
        }
        path = self.owner_identity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(identity, indent=1, ensure_ascii=False), encoding="utf-8")
        return identity

    def owner_name(self) -> str:
        """Discovered/confirmed identity first; the env var is only a fallback
        for installs that predate identity discovery."""
        name = normalize_text(str(self.get_owner_identity().get("display_name") or ""), 80)
        return name or self.config.owner_display_name

    def bot_handle(self) -> str:
        """Short handle for naming this instance — from the discovered owner
        identity so each teammate's bot is legible in a shared server."""
        identity = self.get_owner_identity()
        aliases = [str(a) for a in (identity.get("aliases") or []) if str(a).strip()]
        return derive_bot_handle(
            override=self.config.bot_handle_override,
            aliases=aliases,
            display_name=self.owner_name(),
        )

    def bot_name(self) -> str:
        """Display name for this bot instance, e.g. 'alice-mybot'."""
        return format_bot_name(self.config.bot_name_template, self.bot_handle())

    def investigate_owner_identity(self) -> dict[str, Any]:
        """Sherlock run: the agent deduces the owner's name/handles from the
        trajectories. Returns the parsed identity draft (not saved)."""
        actor_id = normalize_text(self.config.imported_owner_actor_id, 128)
        system_prompt, _sources, _summary = self.build_system_prompt(
            query=self.OWNER_IDENTITY_PROMPT,
            actor_id=actor_id,
            memory_scope="private",
            target_user_id=None,
            session_summary="",
            use_memory=True,
            match_limit=self.config.memory_match_limit,
        )
        budget_log = (
            self.new_retrieval_budget_log_path()
            if self.config.agentic_tool_routing else None
        )
        tool_env = self.tool_env_for_request(
            actor_id=actor_id, memory_scope="private", target_user_id=None,
            retrieval_budget_log_path=str(budget_log) if budget_log else None,
        )
        result = self.provider.chat(
            system_prompt=system_prompt, history=[],
            message=self.OWNER_IDENTITY_PROMPT, tool_env=tool_env,
        )
        text = str(result.get("text") or "")
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict) or not str(parsed.get("display_name") or "").strip():
            return {}
        return parsed

    def identity_status(self) -> dict[str, Any]:
        with self._identity_lock:
            return {"investigating": self._identity_investigating, "error": self._identity_error}

    def start_owner_identity_investigation(self, *, save: bool = True) -> bool:
        """Run the Sherlock session in the background. Returns False if one is
        already in flight."""
        with self._identity_lock:
            if self._identity_investigating:
                return False
            self._identity_investigating = True
            self._identity_error = ""

        def worker() -> None:
            error = ""
            try:
                draft = self.investigate_owner_identity()
                if draft and save:
                    self.save_owner_identity(
                        display_name=str(draft.get("display_name") or ""),
                        aliases=draft.get("aliases") if isinstance(draft.get("aliases"), list) else [],
                        evidence=draft.get("evidence") if isinstance(draft.get("evidence"), list) else [],
                        confidence=str(draft.get("confidence") or ""),
                        confirmed=False,
                    )
                    log.info("owner identity discovered: %s", draft.get("display_name"))
                elif not draft:
                    error = "investigation returned no usable identity"
            except Exception as exc:  # pragma: no cover - background best-effort
                log.warning("owner identity investigation failed: %s", exc)
                error = normalize_text(str(exc), 300)
            finally:
                with self._identity_lock:
                    self._identity_investigating = False
                    self._identity_error = error

        threading.Thread(target=worker, daemon=True, name="owner-identity-sherlock").start()
        return True

    def maybe_start_identity_onboarding(self) -> None:
        """First-boot onboarding: a fresh install doesn't ask who you are — it
        deduces the owner from the trajectories, then confirms in conversation
        or in the menu app."""
        if self.get_owner_identity().get("display_name"):
            return

        def delayed() -> None:
            # Let the HTTP server come up first: the investigating agent's tool
            # calls loop back into this same server.
            time.sleep(20)
            try:
                total_chunks = int(self.trajectory_chunk_index.stats().get("total_chunks") or 0)
            except Exception:
                return
            if total_chunks <= 0:
                log.info("identity onboarding skipped: no indexed trajectories yet")
                return
            log.info("identity onboarding: no owner identity on file — starting sherlock session")
            self.start_owner_identity_investigation(save=True)

        threading.Thread(target=delayed, daemon=True, name="owner-identity-onboarding").start()

    def is_owner_actor(self, actor_id: str) -> bool:
        owner_id = normalize_text(self.config.imported_owner_actor_id, 128)
        return bool(owner_id) and normalize_text(actor_id, 128) == owner_id

    def asker_section(
        self,
        *,
        actor_id: str,
        actor_display_name: str,
        channel_kind: str,
        channel_label: str,
    ) -> str:
        """Tell the model who it serves vs who is talking right now — the core
        of multi-person operation over Discord/Slack."""
        identity = self.get_owner_identity()
        owner_name = self.owner_name() or "your owner"
        aliases = [str(a) for a in (identity.get("aliases") or []) if str(a).strip()]
        alias_note = f" (also goes by: {', '.join(aliases[:5])})" if aliases else ""
        team = f" on {self.config.team_name}" if self.config.team_name else ""
        record = self.people.get(actor_id) or {}
        name = actor_display_name or str(record.get("display_name") or "") or f"actor {actor_id}"
        lines = [
            "## Who Is Asking",
            f"You are {owner_name}'s assistant and representative{team}: a plain, factual "
            "information source over their work history that helps the team collaborate. "
            f"No persona needed.{alias_note}",
        ]
        if self.is_owner_actor(actor_id):
            lines.append(
                f"Asker: {name} — this IS {owner_name}, the owner. First-person references "
                "('what did I do', 'my runs') mean the owner's own work."
            )
            if not identity.get("display_name"):
                lines.append(
                    "You have not yet learned the owner's name. If it fits naturally, briefly "
                    "ask what to call them (one line; they can also set it with `!iam <name>`)."
                )
            elif not identity.get("confirmed"):
                lines.append(
                    f"'{owner_name}' is your own deduction from their history, not yet confirmed. "
                    "If it fits naturally, confirm it once (they can correct with `!iam <name>`)."
                )
        else:
            lines.append(
                f"Asker: {name} (id {actor_id}) — a teammate of {owner_name}, not the owner. "
                f"Answer their questions about {owner_name}'s work from the owner's history — "
                "that is your job as representative. Reference resolution: the owner's name or "
                f"'your owner' means {owner_name}; but THIS asker's first-person references "
                "('my notes', 'what did I ask you') mean the asker themself, whose own private "
                "notes are separate from the owner's history."
            )
        if channel_kind == "group":
            where = f" in {channel_label}" if channel_label else ""
            lines.append(
                f"Setting: group channel{where}. Multiple people talk here; user turns are "
                "prefixed with the speaker's name in [brackets]. Address the current asker, and "
                "use the channel conversation as shared context everyone present can see."
            )
        elif channel_kind:
            lines.append("Setting: direct message — a private one-on-one conversation.")
        first_seen = str(record.get("first_seen") or "")[:10]
        chat_count = int(record.get("chat_count") or 0)
        if chat_count > 1:
            history_line = f"Prior contact: {chat_count} chats since {first_seen or 'recently'}."
            topics = [
                normalize_text(str(item.get("topic") or ""), 90)
                for item in (record.get("recent_topics") or [])[-4:-1]
                if isinstance(item, dict) and item.get("topic")
            ]
            if topics:
                history_line += " Recent topics: " + "; ".join(topics)
            lines.append(history_line)
        gist = normalize_text(str(record.get("conversation_gist") or ""), 700)
        if gist:
            lines.append(f"What you've discussed with them before: {gist}")
        return "\n".join(lines)

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
        actor_display_name: str = "",
        channel_kind: str = "",
        channel_label: str = "",
        carry_forward: str = "",
    ) -> tuple[str, list[dict[str, Any]], bool]:
        sections: list[str] = []
        workspace_prompt = self.workspace.render()
        if workspace_prompt:
            sections.append(workspace_prompt)

        owner_profile = self.get_owner_profile()
        if owner_profile:
            sections.append(
                "## Who You Serve (Owner Profile)\n"
                "This assistant answers for a single owner. Ground interpretation of vague "
                "requests, shorthand, and 'my/our' references in this profile:\n"
                f"{owner_profile}"
            )

        sections.append(
            self.asker_section(
                actor_id=actor_id,
                actor_display_name=actor_display_name,
                channel_kind=channel_kind,
                channel_label=channel_label,
            )
        )

        sections.append(
            "## Grounding Policy\n"
            "Use retrieved memory snippets and the current session summary as grounded context. "
            "If no relevant memory is provided for a claim about prior work, a person, or a prior decision, "
            "say that the system does not have grounded memory for it instead of guessing. "
            "When trajectory evidence contains later corrections or renames, use the later corrected wording. "
            "The session summary and earlier turns are conversation context, NOT retrieval results: "
            "never treat items mentioned there as the candidate set for a question about past work."
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
        elif carry_forward:
            # This is a fresh session that rolled over from an idle one; the
            # transcript is empty but continuity is worth keeping.
            sections.append(
                "## Continuing From Earlier\n"
                "This is a new conversation, but here's the gist of what you were last discussing "
                f"with this person (use only if they pick it back up):\n{carry_forward}"
            )

        trajectory_allowed = self.can_use_local_trajectory_lookup(
            actor_id=actor_id,
            memory_scope=memory_scope,
            target_user_id=target_user_id,
        )
        if use_memory and self.config.agentic_tool_routing and not trajectory_allowed:
            tool_command = f"{shlex.quote(self.config.mybot_tool_python)} {shlex.quote(self.config.mybot_tool_path)}"
            sections.append(
                "## Local Tools\n"
                f"- `{tool_command} memory-search -q <query>` — search long-term memory available "
                "to this asker (their own private notes + shared team memory; actor and scope are "
                "preset via env vars). Trajectory search/read are owner-only and will refuse for "
                "this request — don't attempt them."
            )
        elif use_memory and self.config.agentic_tool_routing:
            tool_command = f"{shlex.quote(self.config.mybot_tool_python)} {shlex.quote(self.config.mybot_tool_path)}"
            sections.append(
                "## Local Tools\n"
                "Your shell runs in a read-restricted sandbox (no secrets, no raw session files, "
                "localhost-only network). The mybot tool below is your data source (actor + scope "
                "preset via env vars); you may compose its output with standard text tools — "
                "grep, sed, awk, jq, sort, uniq, pipes — when that's sharper than SQL:\n"
                f"- `{tool_command}` — memory-search, trajectory-search, trajectory-read, trajectory-stats "
                "(semantic/fuzzy search + focused evidence reads). trajectory-search takes --after/--before "
                "(ISO date) and --source codex|claude; hits report event_start/event_end + chunk_id. "
                "trajectory-read --around-event N (± --events-before/--events-after) or --chunk-id C zooms "
                "to that exact spot; reads report total_events so you can page (the tail is the freshest).\n"
                f"- `{tool_command} sql -q \"<SELECT …>\" [--limit N]` — exact/enumeration SQL over the "
                "cleaned, included-only index: `trajectory_chunks(id, source_ref, source_name, cwd, title, "
                "updated_at, event_start, event_end, text, metadata_json)` and FTS5 "
                "`trajectory_chunks_fts(title, cwd, text)`; plus `prompt_history(id, source_name, "
                "session_id, ts, cwd, text)` — every prompt the owner ever typed (Claude Code and Codex), "
                "with ISO `ts` and the project dir, and FTS5 `prompt_history_fts(text, cwd)`. Prompt "
                "history is DURABLE: it survives after a transcript is deleted or was never indexed, "
                "so it places work in time and names the session (`source_name:session_id` = source_ref) "
                "even when trajectory_chunks has nothing. COLUMN TYPES MATTER: `updated_at` is the ISO "
                "timestamp — the ONLY date column; filter/sort time with it (`WHERE updated_at >= '2026-07-06'`, "
                "`ORDER BY updated_at DESC`). `event_start`/`event_end` are INTEGER event indices within a "
                "session (0..total_events), NOT dates — never compare them to a date. For a date window on "
                "search, use --after/--before. Your grep/sed/awk/jq equivalents live INSIDE "
                "this sandbox: `text REGEXP '...'` (case-insensitive), `regexp_extract(text, pattern[, group])`, "
                "`regexp_count(text, pattern)`, SQLite JSON1 (`json_extract(metadata_json,'$.key')`), plus "
                "GROUP BY/COUNT for aggregation. A row's `id` works as `--chunk-id` in trajectory-read to "
                "pull full context around a SQL hit.\n"
                f"- `{tool_command} budget` — your retrieval-time budget for THIS request (default 90s "
                "across all searches; a per-request override may apply). It is a pacing default the "
                "owner and you jointly control, not a wall: when a search reports budget_exhausted, "
                "either answer from the evidence you have, or — if the question clearly warrants it "
                "and you have a specific next lead — extend once with "
                f"`{tool_command} budget --extend <seconds> --reason \"<the specific lead>\"`. "
                "Every extension and its reason is shown to the owner in your reply's budget "
                "summary, so extend deliberately, never to repeat a failed query.\n"
                "Be relentlessly proactive: dig with these until the answer is grounded in real evidence, "
                "then say what's certain vs not (with dates for time-sensitive values). Live numbers "
                "(balances, quotas, job states) usually ARE recorded in past command output — hunt for the "
                "literal output line (e.g. text LIKE '%Current Balance%') before concluding there's no "
                "record. Don't touch raw session files directly.\n"
                "Recall protocol — when asked what they did/asked/decided before:\n"
                "1. Search FIRST, anchored to the asker's own words. Candidates you already hold from "
                "the conversation are hypotheses to test, not the answer set: don't grep for phrasings "
                "only you used, and don't restrict SQL to directories you suspect until a fresh, "
                "unrestricted search has confirmed them.\n"
                "2. Arbitrate on the full hit list before deep-reading. Skim titles/dates of ALL hits "
                "and check every cue in the request — timeframe ('recent' → prefer the newest), which "
                "tool (claude vs codex), topic. A hit that fits all cues beats a familiar one that "
                "fits fewer.\n"
                "3. After reading, verify the session actually answers the question asked. If a cue "
                "still mismatches (wrong week, wrong tool, wrong subject), say so and go back to the "
                "hit list or rephrase the search — do not settle for the best already-open candidate."
            )
            instant = self.strip_private_trajectory_payloads(
                self.trajectory_chunk_index.instant_hits(query, limit=10), actor_id
            )
            if instant:
                hit_lines = [
                    "## Instant Index Hits",
                    "Lexical prefix matches for the asker's literal words — the same list their "
                    "UI shows while typing. These are CANDIDATES to arbitrate by title/date/tool, "
                    "not conclusions, and not complete: paraphrased evidence still needs your own "
                    "semantic trajectory-search. When the question asks about past work, your "
                    "answer must ACCOUNT FOR every hit below — present it as a candidate, or "
                    "dismiss it with a one-clause reason (wrong topic/time/tool). Silently "
                    "omitting a plausible hit is the exact failure this list exists to prevent; "
                    "when several hits fit, show them all and let the asker pick.",
                ]
                for hit in instant:
                    hit_lines.append(
                        f"- [{hit['source_name']}][{hit['updated_at'][:10]}] {hit['source_ref']} — "
                        f"{normalize_text(hit['title'], 100)} ({normalize_text(hit['cwd'], 80)})"
                    )
                sections.append("\n".join(hit_lines))
            prompt_hits = self.prompt_history_hits(query, limit=6)
            if prompt_hits:
                prompt_lines = [
                    "## Prompt History Hits",
                    "Prompts the asker actually typed that match their words (durable log; it "
                    "survives even when the session transcript is gone). Each names the session, "
                    "the day, and the project dir — use them to place the work in time, then "
                    "trajectory-search/read that session_ref, or query `prompt_history` via SQL "
                    "for the surrounding prompts. If the transcript is missing, say so and answer "
                    "from what the prompts and any indexed chunks establish.",
                ]
                for hit in prompt_hits:
                    prompt_lines.append(
                        f"- [{hit['source_name']}][{hit['ts'][:10]}] {hit['source_ref']} "
                        f"({normalize_text(hit['cwd'], 60)}) — {normalize_text(hit['text'], 220)}"
                    )
                sections.append("\n".join(prompt_lines))
        elif use_memory and trajectory_allowed:
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
            "Answer concisely and factually — you are an information source, not a persona. "
            "Mention uncertainty when the answer is not grounded in the retrieved memory or "
            "current session context."
        )
        return (
            "\n\n".join(section for section in sections if section),
            trajectory_sources + memory_sources,
            session_summary_used,
        )

    def maybe_compact_session(
        self, *, session_key: str, user: str, actor_id: str | None = None
    ) -> dict[str, Any] | None:
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

        result = self.sessions.set_session_summary(
            session_key,
            user,
            summary=new_summary,
            compacted_message_count=compactable_count,
        )
        # Promote the durable gist OUT of the (disposable, rollover-prone)
        # transcript into the person's long-term record, so continuity survives
        # session rollover. Single-actor sessions only — group summaries conflate
        # multiple people.
        if actor_id:
            self.people.set_conversation_gist(actor_id=actor_id, gist=new_summary)
        return result


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
            active = self.server.state.provider.active_model()
            self.respond_json(
                200,
                {
                    "ok": True,
                    "provider_backend": active["backend"],
                    "active_model": active["model"],
                    "active_thinking": active["thinking"],
                    "bot_name": self.server.state.bot_name(),
                    "bot_handle": self.server.state.bot_handle(),
                    "model_base_url": cfg.model_base_url,
                    "model_name": cfg.model_name,
                    "model_api_style": cfg.model_api_style,
                    "codex_permission_profile": cfg.codex_permission_profile,
                    "codex_cwd": cfg.codex_cwd,
                    "workspace_dir": cfg.workspace_dir,
                    "memory_loaded": self.server.state.get_memory_index() is not None,
                    "semantic_memory_stats": self.server.state.memory_store.get_stats(),
                    "sync_auth_configured": self.server.state.sync_auth.configured(),
                    "index_refresh_paused_on_battery": self.server.state.background_refresh_paused_on_battery,
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
        if self.path == "/chat/stream":
            self.handle_chat_stream(body)
            return
        if self.path == "/owner/profile":
            self.handle_owner_profile(body)
            return
        if self.path == "/owner/identity":
            self.handle_owner_identity(body)
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
        if self.path == "/trajectory/sql":
            self.handle_trajectory_sql(body)
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
        if self.path == "/sessions/observe":
            self.handle_session_observe(body)
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
        if self.path == "/sessions/list":
            self.handle_session_list(body)
            return
        if self.path == "/sessions/reset":
            self.handle_session_reset(body)
            return
        self.respond_json(404, {"ok": False, "error": "not found"})

    def handle_owner_profile(self, body: dict[str, Any]) -> None:
        action = str(body.get("action") or "get").strip().lower()
        state = self.server.state
        if action == "get":
            self.respond_json(200, {"ok": True, "profile": state.get_owner_profile()})
            return
        if action == "save":
            text = str(body.get("text") or "").strip()[:8000]  # keep markdown newlines
            if not text:
                self.respond_json(400, {"ok": False, "error": "text is required to save"})
                return
            state.save_owner_profile(text)
            self.respond_json(200, {"ok": True, "profile": state.get_owner_profile()})
            return
        if action == "generate":
            started = time.time()
            log.info("owner profile generation started")
            try:
                draft = state.generate_owner_profile()
            except RuntimeError as exc:
                log.warning("owner profile generation failed: %s", exc)
                self.respond_json(502, {"ok": False, "error": str(exc)})
                return
            save = coerce_bool(body.get("save"), False)
            if save and draft:
                state.save_owner_profile(draft)
            log.info("owner profile generated in %.1fs (saved=%s)", time.time() - started, save and bool(draft))
            self.respond_json(200, {"ok": True, "draft": draft, "saved": save and bool(draft)})
            return
        self.respond_json(400, {"ok": False, "error": f"unknown action {action!r}"})

    def handle_owner_identity(self, body: dict[str, Any]) -> None:
        """Who the bot serves. `investigate` runs the Sherlock session (agent
        deduces the owner's name from trajectories); `set` is the owner
        confirming or correcting it themselves."""
        action = str(body.get("action") or "get").strip().lower()
        state = self.server.state
        if action == "get":
            self.respond_json(200, {"ok": True, "identity": state.get_owner_identity(), **state.identity_status()})
            return
        if action == "set":
            actor_id = normalize_text(str(body.get("actor_id") or ""), 128)
            if not state.is_owner_actor(actor_id):
                self.respond_json(403, {"ok": False, "error": "only the owner can set their identity"})
                return
            display_name = normalize_text(str(body.get("display_name") or ""), 80)
            if not display_name:
                self.respond_json(400, {"ok": False, "error": "display_name is required"})
                return
            existing = state.get_owner_identity()
            aliases = body.get("aliases") if isinstance(body.get("aliases"), list) else existing.get("aliases") or []
            identity = state.save_owner_identity(
                display_name=display_name,
                aliases=aliases,
                evidence=existing.get("evidence") or [],
                confidence="confirmed",
                confirmed=True,
            )
            self.respond_json(200, {"ok": True, "identity": identity})
            return
        if action == "investigate":
            if coerce_bool(body.get("background"), False):
                started_bg = state.start_owner_identity_investigation(
                    save=coerce_bool(body.get("save"), True)
                )
                self.respond_json(200, {"ok": True, "started": started_bg, **state.identity_status()})
                return
            started = time.time()
            log.info("owner identity investigation (sherlock) started")
            try:
                draft = state.investigate_owner_identity()
            except RuntimeError as exc:
                log.warning("owner identity investigation failed: %s", exc)
                self.respond_json(502, {"ok": False, "error": str(exc)})
                return
            if not draft:
                self.respond_json(502, {"ok": False, "error": "investigation returned no usable identity"})
                return
            saved = False
            if coerce_bool(body.get("save"), False):
                state.save_owner_identity(
                    display_name=str(draft.get("display_name") or ""),
                    aliases=draft.get("aliases") if isinstance(draft.get("aliases"), list) else [],
                    evidence=draft.get("evidence") if isinstance(draft.get("evidence"), list) else [],
                    confidence=str(draft.get("confidence") or ""),
                    confirmed=False,
                )
                saved = True
            log.info("owner identity investigated in %.1fs (saved=%s)", time.time() - started, saved)
            self.respond_json(200, {"ok": True, "draft": draft, "saved": saved})
            return
        self.respond_json(400, {"ok": False, "error": f"unknown action {action!r}"})

    def _chat_context(self, body: dict[str, Any]):
        """Shared prep for /chat and /chat/stream. Returns (ctx, None) or
        (None, (status, error)). ctx holds everything both paths need."""
        message = normalize_text(str(body.get("message") or body.get("input") or ""))
        if not message:
            return None, (400, "message is required")
        user = str(body.get("user") or body.get("actor_id") or DEFAULT_SESSION_MAIN_KEY)
        actor_id = normalize_text(str(body.get("actor_id") or user), 128)
        try:
            memory_scope = parse_memory_scope(body.get("memory_scope"))
        except ValueError as exc:
            return None, (400, str(exc))
        target_user_id = normalize_text(str(body.get("target_user_id") or ""), 128) or None
        if memory_scope == "target_user" and not target_user_id:
            return None, (400, "target_user_id is required when memory_scope=target_user")
        actor_display_name = normalize_text(str(body.get("actor_display_name") or ""), 80)
        channel_kind = normalize_text(str(body.get("channel_kind") or ""), 16).lower()
        if channel_kind not in ("", "dm", "group"):
            return None, (400, "channel_kind must be 'dm' or 'group'")
        channel_label = normalize_text(str(body.get("channel_label") or ""), 120)
        author_label = normalize_text(str(body.get("author_label") or actor_display_name), 80)
        raw_message = message
        if channel_kind == "group" and author_label:
            # Attributed turns keep a shared channel session readable when
            # several people talk to the bot in the same conversation.
            message = f"[{author_label}] {message}"

        sessions = self.server.state.sessions
        config = self.server.state.config
        is_group = channel_kind == "group"
        # Stable logical key per surface; the router maps it to a rolling active
        # storage key (idle rollover). Freshness is the router's job now, so we
        # don't append a timestamp here.
        logical_key = safe_session_key(
            user=user,
            requested=str(body["session_key"]) if body.get("session_key") else None,
            new_session=False,
        )
        force_new = coerce_bool(body.get("new_session"))
        if is_group or coerce_bool(body.get("pin_session")):
            # Channels are ongoing; don't roll them on idle — the recent-window
            # filter below bounds context instead. Pinned sessions are explicit
            # named chats (the menu's chat list): one key stays one thread, so a
            # reopened chat continues in place instead of rolling to a new epoch.
            idle_seconds: float | None = None
        else:
            prev_active = sessions.peek_active_key(logical_key)
            last_user_turn = ""
            for entry in reversed(sessions.load_messages(prev_active, limit=8)):
                if entry.get("role") == "user":
                    last_user_turn = str(entry.get("content") or "")
                    break
            followup = looks_like_followup(raw_message, last_user_turn)
            idle_seconds = (
                config.session_idle_rollover_seconds if followup else config.session_topic_shift_seconds
            )
        session_key, carry_forward = sessions.resolve_active_session(
            logical_key, idle_seconds=idle_seconds, force_new=force_new
        )

        use_memory = coerce_bool(body.get("use_trajectory_memory"), True)
        all_messages = sessions.load_messages(session_key, limit=None)
        summary_state = sessions.get_session_summary(session_key) or {}
        # Group sessions blend many people's parallel threads; a rolling summary
        # of that conflates them, so groups rely on the recent window only.
        summary_text = "" if is_group else str(summary_state.get("summary") or "")
        compacted_message_count = int(summary_state.get("compacted_message_count") or 0)
        visible_messages = all_messages if is_group else all_messages[compacted_message_count:]
        if is_group and config.group_context_window_seconds > 0:
            cutoff = datetime.now(timezone.utc).timestamp() - config.group_context_window_seconds
            visible_messages = [
                entry for entry in visible_messages
                if (parse_iso(str(entry.get("timestamp") or "")) or datetime.now(timezone.utc)).timestamp() >= cutoff
            ]
        if config.history_max_messages:
            visible_messages = visible_messages[-config.history_max_messages :]

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
                actor_display_name=actor_display_name,
                channel_kind=channel_kind,
                channel_label=channel_label,
                carry_forward=carry_forward if not summary_text else "",
            )
        except ValueError as exc:
            return None, (400, str(exc))
        except RuntimeError as exc:
            log.warning("chat build_system_prompt failed user=%s: %s", user, exc)
            return None, (500, str(exc))

        retrieval_budget_log_path = (
            self.server.state.new_retrieval_budget_log_path()
            if use_memory and self.server.state.config.agentic_tool_routing
            else None
        )
        budget_seconds: float | None = None
        raw_budget = body.get("budget_seconds")
        if raw_budget is not None:
            try:
                budget_seconds = float(raw_budget)
            except (TypeError, ValueError):
                budget_seconds = None
        tool_env = self.server.state.tool_env_for_request(
            actor_id=actor_id,
            memory_scope=memory_scope,
            target_user_id=target_user_id,
            retrieval_budget_log_path=str(retrieval_budget_log_path) if retrieval_budget_log_path else None,
            budget_seconds=budget_seconds,
        )
        return {
            "message": message, "raw_message": raw_message, "user": user, "actor_id": actor_id,
            "actor_display_name": actor_display_name, "channel_kind": channel_kind,
            "channel_label": channel_label, "logical_key": logical_key, "is_group": is_group,
            "memory_scope": memory_scope, "target_user_id": target_user_id,
            "session_key": session_key, "use_memory": use_memory,
            "history_for_model": history_for_model, "system_prompt": system_prompt,
            "memory_sources": memory_sources, "session_summary_used": session_summary_used,
            "summary_text": summary_text, "summary_state": summary_state,
            "compacted_message_count": compacted_message_count,
            "retrieval_budget_log_path": retrieval_budget_log_path, "tool_env": tool_env,
        }, None

    def _chat_persist(self, body, ctx, provider_result, answer, retrieval_budgets) -> dict[str, Any]:
        """Shared finalize: build source records, persist the turn, compact,
        return the response payload."""
        user = ctx["user"]; actor_id = ctx["actor_id"]; session_key = ctx["session_key"]
        memory_scope = ctx["memory_scope"]; target_user_id = ctx["target_user_id"]
        session_summary_used = ctx["session_summary_used"]; memory_sources = ctx["memory_sources"]
        summary_text = ctx["summary_text"]; summary_state = ctx["summary_state"]
        compacted_message_count = ctx["compacted_message_count"]
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
        # Agentic tool routing retrieves via the CLI subprocess, so the hits
        # aren't in memory_sources — recover them from the budget log the tool
        # wrote, so the grounding-sources list (and its clickable chips) works
        # in tool-routing mode too. Dedup by ref against what we already have.
        seen_refs = {str(record.get("source_ref") or "") for record in source_records}
        for record in self.server.state.sources_from_retrieval_budgets(retrieval_budgets, actor_id):
            if record["source_ref"] not in seen_refs:
                source_records.append(record)
                seen_refs.add(record["source_ref"])

        self.server.state.sessions.append_message(
            session_key, user, "user", ctx["message"],
            meta={
                "source": "external_api",
                "actor_id": actor_id,
                "memory_scope": memory_scope,
                "target_user_id": target_user_id,
            },
        )
        self.server.state.sessions.append_message(
            session_key, user, "assistant", answer,
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
                    session_key, user, provider_result["provider_style"], backend_session_id,
                )

        self.server.state.people.note_interaction(
            actor_id=actor_id,
            display_name=ctx.get("actor_display_name", ""),
            channel_label=ctx.get("channel_label", "") or session_key,
            topic=ctx.get("raw_message", ctx["message"]),
        )
        # Keep the logical session's idle clock fresh (the router uses it to
        # decide the next rollover).
        self.server.state.sessions.touch_session(ctx.get("logical_key") or session_key)

        try:
            self.server.state.maybe_compact_session(
                session_key=session_key,
                user=user,
                actor_id=None if ctx.get("is_group") else actor_id,
            )
        except RuntimeError as exc:
            print(f"Session compaction failed for {session_key}: {exc}")

        return_sources = coerce_bool(body.get("return_sources"), True)
        payload: dict[str, Any] = {
            "ok": True,
            "session_key": ctx.get("logical_key") or session_key,
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
        return payload

    def handle_chat(self, body: dict[str, Any]) -> None:
        started = time.time()
        ctx, err = self._chat_context(body)
        if err:
            self.respond_json(err[0], {"ok": False, "error": err[1]})
            return
        log.info("chat start user=%s session=%s msg_chars=%d",
                 ctx["user"], ctx["session_key"], len(ctx["message"]))
        try:
            provider_result = self.server.state.provider.chat(
                system_prompt=ctx["system_prompt"], history=ctx["history_for_model"],
                message=ctx["message"],
                backend_session_id=(self.server.state.sessions.get_backend_state(ctx["session_key"]) or {}).get("backend_session_id"),
                tool_env=ctx["tool_env"],
            )
        except RuntimeError as exc:
            log.warning("chat provider failed user=%s: %s", ctx["user"], exc)
            self.respond_json(502, {"ok": False, "error": str(exc)})
            return
        retrieval_budgets = self.server.state.read_retrieval_budget_log(ctx["retrieval_budget_log_path"])
        answer = append_retrieval_budget_summary(provider_result["text"], retrieval_budgets)
        log.info("chat done user=%s session=%s reply_chars=%d elapsed=%.1fs",
                 ctx["user"], ctx["session_key"], len(answer), time.time() - started)
        payload = self._chat_persist(body, ctx, provider_result, answer, retrieval_budgets)
        self.respond_json(200, payload)

    def handle_chat_stream(self, body: dict[str, Any]) -> None:
        """Server-Sent Events variant: streams tool activity + answer text as the
        agent produces it, then a final `done` event with sources + budget."""
        started = time.time()
        ctx, err = self._chat_context(body)
        if err:
            self.respond_json(err[0], {"ok": False, "error": err[1]})
            return
        log.info("chat stream start user=%s session=%s", ctx["user"], ctx["session_key"])

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(event: dict[str, Any]) -> None:
            try:
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, OSError):
                raise _ClientGone()

        try:
            provider_result = self.server.state.provider.chat(
                system_prompt=ctx["system_prompt"], history=ctx["history_for_model"],
                message=ctx["message"],
                backend_session_id=(self.server.state.sessions.get_backend_state(ctx["session_key"]) or {}).get("backend_session_id"),
                tool_env=ctx["tool_env"],
                on_event=emit,
            )
        except _ClientGone:
            log.info("chat stream: client disconnected user=%s", ctx["user"])
            return
        except RuntimeError as exc:
            log.warning("chat stream provider failed user=%s: %s", ctx["user"], exc)
            try:
                emit({"kind": "error", "error": str(exc)})
            except _ClientGone:
                pass
            return

        retrieval_budgets = self.server.state.read_retrieval_budget_log(ctx["retrieval_budget_log_path"])
        answer = append_retrieval_budget_summary(provider_result["text"], retrieval_budgets)
        payload = self._chat_persist(body, ctx, provider_result, answer, retrieval_budgets)
        log.info("chat stream done user=%s session=%s reply_chars=%d elapsed=%.1fs",
                 ctx["user"], ctx["session_key"], len(answer), time.time() - started)
        try:
            emit({"kind": "done", **payload})
        except _ClientGone:
            pass

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
        after = normalize_text(str(body.get("after") or ""), 32)
        before = normalize_text(str(body.get("before") or ""), 32)
        for label, value in (("after", after), ("before", before)):
            if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ].*)?", value):
                self.respond_json(
                    400,
                    {"ok": False, "error": f"{label} must be an ISO date (YYYY-MM-DD or full timestamp), got {value!r}"},
                )
                return
        source_filter = normalize_text(str(body.get("source") or ""), 32).lower()
        compact = coerce_bool(body.get("compact"), False)
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
            matches = self.server.state.strip_private_trajectory_payloads(matches, actor_id)
            self.respond_json(
                200,
                {
                    "ok": True,
                    "mode": "full_scan",
                    "matches": matches,
                    "prompt_history": self.server.state.prompt_history_hits(
                        query, limit=8, after=after, before=before, source_name=source_filter
                    ),
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
            after=after,
            before=before,
            source_filter=source_filter,
        )
        if compact:
            matches = [compact_trajectory_match(match) for match in matches]
        response = {"ok": True, "mode": "agentic_candidate", "matches": matches}
        # Typed-prompt matches ride along: they survive transcript deletion and
        # pin work to a day + session even when no chunk matched.
        response["prompt_history"] = self.server.state.prompt_history_hits(
            query, limit=8, after=after, before=before, source_name=source_filter
        )
        if after or before or source_filter:
            response["filters"] = {
                key: value
                for key, value in (("after", after), ("before", before), ("source", source_filter))
                if value
            }
        if coerce_bool(body.get("return_trace"), False):
            response["trace"] = trace
        self.respond_json(200, response)

    TRAJECTORY_SQL_MAX_ROWS = 200
    TRAJECTORY_SQL_MAX_CELL_CHARS = 500
    TRAJECTORY_SQL_TIMEOUT_SECONDS = 10.0

    @staticmethod
    def _register_sql_regex_functions(conn: sqlite3.Connection) -> None:
        """grep/sed/awk-class text surgery INSIDE the read-only sandbox, so the
        agent never needs raw shell text tools (which could read arbitrary
        files). Case-insensitive by default; use (?-i:...) to force case."""

        def _compile(pattern: Any) -> "re.Pattern[str]":
            return re.compile(str(pattern or "")[:500], re.IGNORECASE | re.DOTALL)

        def regexp(pattern: Any, value: Any) -> int:  # WHERE text REGEXP '...'
            if value is None:
                return 0
            return 1 if _compile(pattern).search(str(value)) else 0

        def regexp_extract(value: Any, pattern: Any, group: Any = 0) -> str | None:
            if value is None:
                return None
            match = _compile(pattern).search(str(value))
            if match is None:
                return None
            try:
                return match.group(int(group))
            except (IndexError, ValueError):
                return None

        def regexp_count(value: Any, pattern: Any) -> int:
            if value is None:
                return 0
            return len(_compile(pattern).findall(str(value)))

        conn.create_function("regexp", 2, regexp, deterministic=True)
        conn.create_function("regexp_extract", 2, regexp_extract, deterministic=True)
        conn.create_function("regexp_extract", 3, regexp_extract, deterministic=True)
        conn.create_function("regexp_count", 2, regexp_count, deterministic=True)

    def handle_trajectory_sql(self, body: dict[str, Any]) -> None:
        """Read-only SQL over the trajectory index, exposed through mybot_tool
        so the agent needs exactly ONE preapproved command prefix. Replaces the
        raw `sqlite3 -readonly` allowlist entry, which (a) auto-denied any
        command-shape deviation (bare `sqlite3`, extra flags, pipes) and
        (b) still allowed `.system`-style dot-command shell escapes."""
        query = normalize_text(str(body.get("query") or ""), 4000).strip().rstrip(";").strip()
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
        # Owner-only, stricter than search/read: arbitrary SELECTs cannot be
        # row-filtered for private (owner-only) sessions, so guests never get
        # SQL even when guest owner-access is enabled.
        if not self.server.state.actor_is_owner(actor_id) or not self.server.state.can_use_local_trajectory_lookup(
            actor_id=actor_id,
            memory_scope=memory_scope,
            target_user_id=target_user_id,
        ):
            self.respond_json(403, {"ok": False, "error": "trajectory SQL is owner-only"})
            return
        # mode=ro + query_only stop writes; ATTACH could still read OTHER
        # database files (memories, anything on disk), so it is refused.
        if re.search(r"\b(attach|detach)\b", query, re.IGNORECASE):
            self.respond_json(400, {"ok": False, "error": "ATTACH/DETACH are not allowed"})
            return
        limit = max(1, min(int(body.get("limit", 50)), self.TRAJECTORY_SQL_MAX_ROWS))
        db_path = self.server.state.config.trajectory_index_db_path
        started = time.monotonic()
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            self._register_sql_regex_functions(conn)
            conn.set_progress_handler(
                lambda: 1 if time.monotonic() - started > self.TRAJECTORY_SQL_TIMEOUT_SECONDS else 0,
                100_000,
            )
            cursor = conn.execute(query)
            rows = cursor.fetchmany(limit + 1)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
        except (sqlite3.Error, sqlite3.Warning) as exc:
            self.respond_json(400, {"ok": False, "error": f"SQL error: {exc}"})
            return
        finally:
            if conn is not None:
                conn.close()

        cell_truncated = False

        def render_cell(value: Any) -> Any:
            nonlocal cell_truncated
            if value is None or isinstance(value, (int, float)):
                return value
            text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
            if len(text) > self.TRAJECTORY_SQL_MAX_CELL_CHARS:
                cell_truncated = True
                return text[: self.TRAJECTORY_SQL_MAX_CELL_CHARS - 1] + "…"
            return text

        rendered = [[render_cell(value) for value in row] for row in rows[:limit]]
        self.respond_json(
            200,
            {
                "ok": True,
                "columns": columns,
                "rows": rendered,
                "row_count": len(rendered),
                "truncated": len(rows) > limit or cell_truncated,
                "seconds": round(time.monotonic() - started, 3),
            },
        )

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
        event_index = body.get("event_index")
        chunk_id = body.get("chunk_id")
        if event_index is not None or chunk_id is not None:
            try:
                window = self.server.state.trajectory_chunk_index.read_window(
                    source_ref=source_ref,
                    event_index=int(event_index) if event_index is not None else 0,
                    chunk_id=int(chunk_id) if chunk_id is not None else None,
                    query=query,
                    before=max(0, int(body.get("events_before", 3))),
                    after=max(0, int(body.get("events_after", 6))),
                    max_chars=max_chars,
                )
            except (TypeError, ValueError):
                self.respond_json(
                    400,
                    {"ok": False, "error": "event_index, chunk_id, events_before and events_after must be integers"},
                )
                return
            if window is None:
                self.respond_json(404, {"ok": False, "error": "trajectory not found in allowed sources"})
                return
            if not self.server.state.source_allowed_by_visibility(window):
                self.respond_json(403, {"ok": False, "error": "trajectory is hidden by visibility policy"})
                return
            if not self.server.state.strip_private_trajectory_payloads([window], actor_id):
                self.respond_json(403, {"ok": False, "error": "trajectory is private to the owner"})
                return
            self.respond_json(200, {"ok": True, "trajectory": window})
            return
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
        if not self.server.state.strip_private_trajectory_payloads([result], actor_id):
            self.respond_json(403, {"ok": False, "error": "trajectory is private to the owner"})
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

    def handle_session_observe(self, body: dict[str, Any]) -> None:
        """Record a channel message into a session WITHOUT invoking the model.
        This is how the bot has context over everything said in a group channel
        while only replying when addressed."""
        message = normalize_text(str(body.get("message") or ""), 4000)
        if not message:
            self.respond_json(400, {"ok": False, "error": "message is required"})
            return
        user = str(body.get("user") or body.get("actor_id") or DEFAULT_SESSION_MAIN_KEY)
        requested_key = str(body.get("session_key") or "")
        if not requested_key:
            self.respond_json(400, {"ok": False, "error": "session_key is required"})
            return
        session_key = safe_session_key(user=user, requested=requested_key)
        actor_id = normalize_text(str(body.get("actor_id") or user), 128)
        author_label = normalize_text(
            str(body.get("author_label") or body.get("actor_display_name") or ""), 80
        )
        content = f"[{author_label}] {message}" if author_label else message
        # Rotate the observe log before it grows unbounded. Deep channel history
        # is not the durable memory (person registry + !remember are), so the
        # old transcript is archived and observation continues in a fresh file.
        cap = self.server.state.config.group_observe_max_messages
        if cap > 0 and len(self.server.state.sessions.load_messages(session_key, limit=cap + 1)) > cap:
            self.server.state.sessions.archive_session(session_key)
        self.server.state.sessions.append_message(
            session_key, user, "user", content,
            meta={"source": "observed", "actor_id": actor_id},
        )
        self.server.state.people.note_interaction(
            actor_id=actor_id,
            display_name=normalize_text(str(body.get("actor_display_name") or ""), 80),
            observed=True,
        )
        self.respond_json(200, {"ok": True, "session_key": session_key})

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

    def handle_session_list(self, body: dict[str, Any]) -> None:
        prefix = normalize_text(str(body.get("prefix") or body.get("session_key") or ""), 128)
        if not prefix:
            self.respond_json(400, {"ok": False, "error": "prefix is required"})
            return
        limit = int(body.get("limit", 100))
        threads = self.server.state.sessions.list_sessions(prefix, limit=limit)
        self.respond_json(200, {"ok": True, "threads": threads})

    def handle_session_reset(self, body: dict[str, Any]) -> None:
        logical_key = safe_session_key(
            user=str(body.get("user") or DEFAULT_SESSION_MAIN_KEY),
            requested=str(body["session_key"]) if body.get("session_key") else None,
        )
        sessions = self.server.state.sessions
        # Archive whatever's active, then force the router to a fresh session so
        # the next turn starts clean (no carry-forward — an explicit reset means
        # "forget this").
        active_key = sessions.peek_active_key(logical_key)
        archived_path = sessions.archive_session(active_key)
        sessions.resolve_active_session(logical_key, idle_seconds=None, force_new=True)
        if archived_path is None:
            self.respond_json(200, {"ok": True, "session_key": logical_key, "already_empty": True})
            return
        self.respond_json(200, {"ok": True, "session_key": logical_key, "archived_path": archived_path})

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
    parser.add_argument("--port", default=8788, type=int)
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
    if provider_backend not in {"openai_compatible", "codex_cli", "claude_cli"}:
        raise SystemExit("MODEL_BACKEND must be 'openai_compatible', 'codex_cli', or 'claude_cli'")

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
    # codex ignores ~/.codex/config.toml here (--ignore-user-config), so default
    # to the latest model explicitly instead of codex's built-in fallback.
    if provider_backend == "codex_cli" and model_name is None:
        model_name = os.environ.get("CODEX_MODEL", "gpt-5.5").strip() or None

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
        # mybot runs codex with --ignore-user-config, so ~/.codex/config.toml
        # (model, reasoning) is NOT applied — we must pass these explicitly.
        # xhigh for the user-facing answer; a cheap tier for the internal query
        # planner so retrieval doesn't crawl.
        codex_reasoning_effort=os.environ.get("CODEX_REASONING_EFFORT", "xhigh").strip(),
        codex_planner_reasoning_effort=os.environ.get("CODEX_PLANNER_REASONING_EFFORT", "low").strip(),
        claude_command=os.environ.get("CLAUDE_COMMAND", "claude").strip() or "claude",
        # Alias (opus/sonnet/fable) → always the latest of that family, no pinning.
        claude_model=os.environ.get("CLAUDE_MODEL", "opus").strip() or "opus",
        claude_thinking=os.environ.get("CLAUDE_THINKING", "xhigh").strip() or "xhigh",
        claude_planner_thinking=os.environ.get("CLAUDE_PLANNER_THINKING", "low").strip() or "low",
        model_config_path=os.environ.get("MODEL_CONFIG_PATH", str(Path(args.state_dir) / "model_config.json")),
        mybot_tool_python=os.environ.get("MYBOT_TOOL_PYTHON", sys.executable or "python3"),
        embedding_model_name=os.environ.get("EMBEDDING_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2"),
        imported_owner_actor_id=os.environ.get("MEMORY_IMPORTED_OWNER_ID", "local-owner"),
        owner_display_name=os.environ.get("OWNER_DISPLAY_NAME", "").strip(),
        team_name=os.environ.get("TEAM_NAME", "").strip(),
        bot_name_template=os.environ.get("BOT_NAME_TEMPLATE", "{handle}-mybot").strip() or "{handle}-mybot",
        bot_handle_override=os.environ.get("BOT_HANDLE", "").strip(),
        guest_owner_access=coerce_bool(os.environ.get("GUEST_OWNER_ACCESS"), True),
        claude_bash_sandbox=coerce_bool(os.environ.get("CLAUDE_BASH_SANDBOX"), True),
        session_tail_pairs=session_tail_pairs,
        compaction_trigger_message_count=int(os.environ.get("COMPACTION_TRIGGER_MESSAGES", "40")),
        compaction_trigger_char_count=int(os.environ.get("COMPACTION_TRIGGER_CHARS", "16000")),
        # A conversation idle past this rolls to a fresh session (continuity kept
        # via a one-line carry-forward). Follow-up queries tolerate the full idle
        # window; a topic-shift after the shorter grace period rolls immediately.
        session_idle_rollover_seconds=float(os.environ.get("SESSION_IDLE_ROLLOVER_SECONDS", "21600")),  # 6h
        session_topic_shift_seconds=float(os.environ.get("SESSION_TOPIC_SHIFT_SECONDS", "900")),  # 15m
        # Group channels: only the last window of chatter is loaded as context,
        # and the observe log rotates past this many messages (deep history is
        # not the durable memory — the person registry + !remember are).
        group_context_window_seconds=float(os.environ.get("GROUP_CONTEXT_WINDOW_SECONDS", "43200")),  # 12h
        group_observe_max_messages=int(os.environ.get("GROUP_OBSERVE_MAX_MESSAGES", "400")),
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
        # Skip unattended index refresh + embedding (CPU-heavy) while on battery,
        # so mybot doesn't drain the laptop in the background. User-initiated
        # searches still refresh on demand; set false to always index.
        trajectory_index_pause_on_battery=coerce_bool(
            os.environ.get("TRAJECTORY_INDEX_PAUSE_ON_BATTERY"), True
        ),
        trajectory_agentic_search=coerce_bool(os.environ.get("TRAJECTORY_AGENTIC_SEARCH"), True),
        trajectory_agentic_max_steps=int(os.environ.get("TRAJECTORY_AGENTIC_MAX_STEPS", "6")),
        trajectory_query_planner=coerce_bool(os.environ.get("TRAJECTORY_QUERY_PLANNER"), True),
        trajectory_query_planner_max_rounds=int(os.environ.get("TRAJECTORY_QUERY_PLANNER_MAX_ROUNDS", "2")),
        trajectory_query_planner_max_queries=int(os.environ.get("TRAJECTORY_QUERY_PLANNER_MAX_QUERIES", "4")),
        trajectory_search_time_budget_seconds=float(os.environ.get("TRAJECTORY_SEARCH_TIME_BUDGET_SECONDS", "25")),
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
    state.refresh_prompt_history()
    state.start_trajectory_index_background_refresh()
    # Decode the embedding matrix off the request path: the first semantic
    # search otherwise pays tens of seconds out of the agent's retrieval budget.
    state.trajectory_chunk_index.warm_vector_cache_async()
    state.maybe_start_identity_onboarding()
    server = StandaloneServer((config.host, config.port), ChatHandler, state)
    print(f"Listening on http://{config.host}:{config.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
