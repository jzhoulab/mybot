from __future__ import annotations

import json
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .models import NormalizedTrajectory, TrajectorySourceAdapter, normalize_text
from .registry import get_source_adapters


TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]{3,}")
DEFAULT_STOP_WORDS = {
    "about",
    "after",
    "again",
    "and",
    "answer",
    "before",
    "could",
    "detail",
    "details",
    "from",
    "have",
    "happened",
    "into",
    "just",
    "most",
    "need",
    "please",
    "recent",
    "session",
    "sessions",
    "that",
    "the",
    "them",
    "then",
    "there",
    "this",
    "through",
    "trajectory",
    "trajectories",
    "two",
    "what",
    "when",
    "where",
    "which",
    "with",
}
TRAJECTORY_INTENT_RE = re.compile(
    r"\b("
    r"trajectory|trajectories|session|sessions|conversation|conversations|"
    r"codex|claude|previous|prior|earlier|recent|history|chat|chats|"
    r"what happened|what was|what were|what did|how did|where did|when did|"
    r"when i asked|asked to|asked for|which one|find|search"
    r")\b",
    re.IGNORECASE,
)
MYBOT_CODEX_WORKSPACE_FRAGMENT = "/.local/share/mybot-codex-workspace"
# tool_result carries command output — the live data (SU balances, disk usage,
# job status) that prose only paraphrases — so it is searchable evidence, not
# noise to be skipped when scoring sessions or building read transcripts.
SEARCHABLE_EVENT_ROLES = {"user", "assistant", "tool_result"}
GENERATED_SESSION_TITLE_PREFIXES = (
    "system and memory context for this chat service",
)


@dataclass
class ParsedEvent:
    role: str
    text: str
    timestamp: str = ""


@dataclass
class TrajectoryDocument:
    source_name: str
    source_ref: str
    session_id: str
    title: str
    updated_at: str
    cwd: str
    file_path: str
    metadata: dict[str, Any]
    events: list[ParsedEvent]
    transcript: str


@dataclass
class TrajectorySearchResult:
    source_name: str
    source_ref: str
    session_id: str
    title: str
    updated_at: str
    cwd: str
    score: float
    match_kind: str
    snippets: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_type": "trajectory",
            "source_name": self.source_name,
            "source_ref": self.source_ref,
            "parent_source_ref": None,
            "title": self.title,
            "updated_at": self.updated_at,
            "cwd": self.cwd,
            "score": round(self.score, 3),
            "match_kind": self.match_kind,
            "snippets": self.snippets,
            "metadata": self.metadata,
        }


def query_tokens(query: str) -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []
    for token in TOKEN_RE.findall(query or ""):
        lowered = token.lower()
        if lowered in DEFAULT_STOP_WORDS or lowered in seen:
            continue
        variants = [lowered]
        if len(lowered) > 5 and lowered.endswith("ing"):
            stem = lowered[:-3]
            if stem.endswith("in"):
                variants.append(f"{stem}e")
            else:
                variants.append(stem)
        if len(lowered) > 4 and lowered.endswith("s"):
            variants.append(lowered[:-1])
        for variant in variants:
            if variant and variant not in DEFAULT_STOP_WORDS and variant not in seen:
                seen.add(variant)
                tokens.append(variant)
    return tokens


def anchor_tokens(tokens: list[str]) -> set[str]:
    return {
        token
        for token in tokens
        if len(token) >= 8 or any(char.isdigit() or char in "/._:-" for char in token)
    }


def minimum_distinct_matches(tokens: list[str]) -> int:
    count = len(set(tokens))
    if count <= 1:
        return count
    if count <= 3:
        return 2
    return min(3, count)


def token_match_is_strong_enough(tokens: list[str], matched: set[str], *, phrase_matched: bool = False) -> bool:
    if phrase_matched:
        return True
    if not tokens:
        return True
    if len(matched) < minimum_distinct_matches(tokens):
        return False
    anchors = anchor_tokens(tokens)
    return not anchors or bool(anchors & matched)


def likely_trajectory_question(query: str) -> bool:
    return bool(TRAJECTORY_INTENT_RE.search(query or ""))


def focus_query(query: str) -> str:
    cleaned = normalize_text(query or "", 500).strip()
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    for marker in (" about ", " involving ", " regarding ", " where ", " that ", " for "):
        index = lowered.find(marker)
        if index < 0:
            continue
        prefix = lowered[:index]
        if TRAJECTORY_INTENT_RE.search(prefix):
            focused = cleaned[index + len(marker):].strip(" ?.'\"")
            if focused:
                return focused
    return cleaned


def source_ref_for(session: NormalizedTrajectory) -> str:
    return f"{session.source_name}:{session.session_id}"


def metadata_cwd(session: NormalizedTrajectory) -> str:
    return normalize_text(str(session.metadata.get("cwd") or ""), 300)


def _compact_json(value: Any, limit: int = 1200) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(value)
    return normalize_text(text, limit)


def _append_event(events: list[ParsedEvent], role: str, text: str, timestamp: str = "") -> None:
    cleaned = normalize_text(text, None)
    if not cleaned:
        return
    if events and events[-1].role == role and events[-1].text == cleaned:
        return
    events.append(ParsedEvent(role=role, text=cleaned, timestamp=timestamp[:19]))


def _summarize_claude_tool(block: dict[str, Any]) -> str:
    name = str(block.get("name") or "tool")
    inp = block.get("input", {})
    if name == "Bash" and isinstance(inp, dict):
        return f"[Bash] {normalize_text(str(inp.get('command') or ''), 1000)}"
    if name in {"Read", "Edit", "MultiEdit", "Write"} and isinstance(inp, dict):
        path = inp.get("file_path") or inp.get("path") or ""
        return f"[{name}] {path}"
    if name in {"Grep", "Glob"} and isinstance(inp, dict):
        return f"[{name}] {_compact_json(inp, 600)}"
    return f"[{name}] {_compact_json(inp, 1000)}"


def _summarize_codex_payload(payload: dict[str, Any]) -> str:
    ptype = str(payload.get("type") or "")
    name = str(payload.get("name") or payload.get("tool_name") or ptype or "tool")
    arguments = payload.get("arguments")
    if arguments is None:
        arguments = payload.get("input")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            pass
    if isinstance(arguments, dict):
        command = arguments.get("cmd") or arguments.get("command")
        if command:
            return f"[{name}] {normalize_text(str(command), 1000)}"
    return f"[{name}] {_compact_json(arguments or payload, 1000)}"


def _read_claude_events(path: str) -> list[ParsedEvent]:
    events: list[ParsedEvent] = []
    pending_assistant: list[str] = []
    pending_tools: list[str] = []
    pending_ts = ""

    def flush_assistant() -> None:
        nonlocal pending_assistant, pending_tools, pending_ts
        if pending_assistant:
            _append_event(events, "assistant", "\n".join(pending_assistant), pending_ts)
        if pending_tools:
            _append_event(events, "actions", "\n".join(pending_tools), pending_ts)
        pending_assistant = []
        pending_tools = []
        pending_ts = ""

    with open(path) as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            etype = entry.get("type")
            ts = str(entry.get("timestamp") or "")
            if etype == "assistant":
                flush_assistant()
                message = entry.get("message", {})
                if not isinstance(message, dict):
                    continue
                pending_ts = ts
                for block in message.get("content", []) or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        pending_assistant.append(str(block.get("text") or ""))
                    elif block.get("type") == "tool_use":
                        pending_tools.append(_summarize_claude_tool(block))
                continue

            if etype == "user":
                flush_assistant()
                message = entry.get("message", {})
                content = message.get("content", "") if isinstance(message, dict) else ""
                text = ""
                tool_results: list[str] = []
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = str(block.get("text") or "")
                            break
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            result_text = block.get("content", "")
                            tool_results.append(normalize_text(str(result_text), 1200))
                        elif isinstance(block, str):
                            text = block
                            break
                if text:
                    _append_event(events, "user", text, ts)
                if tool_results:
                    _append_event(events, "tool_result", "\n".join(tool_results), ts)

    flush_assistant()
    return events


def _read_codex_events(path: str) -> list[ParsedEvent]:
    events: list[ParsedEvent] = []
    with open(path) as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            etype = entry.get("type")
            ts = str(entry.get("timestamp") or "")
            payload = entry.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}

            if etype == "event_msg" and payload.get("type") == "user_message":
                _append_event(events, "user", str(payload.get("message") or ""), ts)
                continue

            if etype != "response_item":
                continue

            ptype = payload.get("type")
            role = payload.get("role")
            if ptype == "message" and role == "assistant":
                texts: list[str] = []
                for block in payload.get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "output_text":
                        texts.append(str(block.get("text") or ""))
                _append_event(events, "assistant", "\n".join(texts), ts)
                continue

            if ptype == "message" and role == "user":
                texts = []
                for block in payload.get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "input_text":
                        texts.append(str(block.get("text") or ""))
                _append_event(events, "user", "\n".join(texts), ts)
                continue

            if ptype in {"function_call", "custom_tool_call", "mcp_tool_call", "local_shell_call"}:
                _append_event(events, "actions", _summarize_codex_payload(payload), ts)
                continue

            if ptype in {"function_call_output", "custom_tool_call_output", "mcp_tool_call_output"}:
                output = payload.get("output")
                if output is None:
                    output = payload.get("content")
                _append_event(events, "tool_result", normalize_text(str(output or ""), 1600), ts)

    return events


def read_events_for_session(session: NormalizedTrajectory) -> list[ParsedEvent]:
    if session.source_name == "claude":
        return _read_claude_events(session.file_path)
    if session.source_name == "codex":
        return _read_codex_events(session.file_path)
    return [ParsedEvent(role=turn.role, text=turn.text, timestamp=turn.timestamp or "") for turn in session.turns]


def render_events(events: list[ParsedEvent], *, max_chars: int | None = None) -> str:
    lines: list[str] = []
    for event in events:
        role = event.role.upper()
        label = f"{role} [{event.timestamp}]" if event.timestamp else role
        lines.append(label)
        lines.append(event.text)
        lines.append("")
    text = "\n".join(lines).strip()
    if max_chars is not None and len(text) > max_chars:
        return text[: max_chars - 30].rstrip() + "\n\n[truncated]"
    return text


def primary_event_transcript(events: list[ParsedEvent], *, max_chars: int | None = None) -> str:
    return render_events(
        [event for event in events if event.role.lower() in SEARCHABLE_EVENT_ROLES],
        max_chars=max_chars,
    )


def read_payload_from_file(
    *,
    source_name: str,
    source_ref: str,
    session_id: str,
    title: str,
    updated_at: str,
    cwd: str,
    file_path: str,
    metadata: dict[str, Any],
    query: str = "",
    max_chars: int = 12000,
) -> dict[str, Any] | None:
    if not file_path or not os.path.exists(file_path):
        return None
    if source_name == "claude":
        events = _read_claude_events(file_path)
    elif source_name == "codex":
        events = _read_codex_events(file_path)
    else:
        return None
    transcript = render_events(events)
    snippets = event_snippets_for_query(events, query=query, title=title, cwd=cwd, limit=4) if query else []
    evidence = transcript
    if max_chars and len(evidence) > max_chars:
        if snippets:
            evidence = "\n\n---\n\n".join(snippets)
            if len(evidence) > max_chars:
                evidence = evidence[: max_chars - 30].rstrip() + "\n\n[truncated]"
        else:
            evidence = render_events(events[-12:], max_chars=max_chars)
    return {
        "source_type": "trajectory",
        "source_name": source_name,
        "source_ref": source_ref,
        "session_id": session_id,
        "title": title,
        "updated_at": updated_at,
        "cwd": cwd,
        "metadata": metadata,
        "snippets": snippets,
        "transcript": evidence,
        "transcript_chars": len(transcript),
    }


def snippet_around(text: str, needle: str, *, width: int = 900) -> str | None:
    if not needle:
        return None
    haystack = text.lower()
    index = haystack.find(needle.lower())
    if index < 0:
        return None
    start = max(0, index - width // 2)
    end = min(len(text), index + len(needle) + width // 2)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet += "..."
    return snippet


def snippet_candidates(text: str, needle: str, *, width: int = 900, max_candidates: int = 8) -> list[str]:
    if not needle:
        return []
    haystack = text.lower()
    needle_lower = needle.lower()
    snippets: list[str] = []
    start_at = 0
    while len(snippets) < max_candidates:
        index = haystack.find(needle_lower, start_at)
        if index < 0:
            break
        start = max(0, index - width // 2)
        end = min(len(text), index + len(needle) + width // 2)
        snippet = text[start:end].strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet += "..."
        snippets.append(snippet)
        start_at = index + max(1, len(needle))
    return snippets


def snippet_quality(snippet: str) -> int:
    lowered = snippet.lower()
    score = 0
    if "\nuser [" in lowered or lowered.startswith("user ["):
        score += 30
    if "\nassistant [" in lowered or lowered.startswith("assistant ["):
        score += 8
    if "[exec_command]" in lowered or "curl -fss" in lowered or "trajectory_investigation" in lowered:
        score -= 40
    if "tool_result" in lowered or "[truncated]" in lowered:
        score -= 6
    return score


def snippets_for_query(text: str, query: str, *, limit: int = 3, width: int = 900) -> list[str]:
    candidates: list[str] = []
    phrase = normalize_text(focus_query(query), 160)
    seen: set[str] = set()
    if phrase and len(phrase) >= 6:
        for snippet in snippet_candidates(text, phrase, width=width):
            if snippet not in seen:
                candidates.append(snippet)
                seen.add(snippet)
    for token in query_tokens(query):
        if len(candidates) >= limit * 8:
            break
        for snippet in snippet_candidates(text, token, width=width, max_candidates=3):
            if snippet not in seen:
                candidates.append(snippet)
                seen.add(snippet)
    candidates.sort(key=snippet_quality, reverse=True)
    return candidates[:limit]


def score_text(*, text: str, title: str, cwd: str, query: str, tokens: list[str], candidate_boost: float) -> tuple[float, str]:
    lowered = text.lower()
    token_list = TOKEN_RE.findall(lowered)
    haystack_tokens = Counter(token_list)
    title_lower = title.lower()
    cwd_lower = cwd.lower()
    phrase = normalize_text(focus_query(query), 200).lower()
    score = candidate_boost
    match_kind = "candidate" if score else "exact"
    phrase_matched = bool(phrase and len(phrase) >= 6 and phrase in lowered)
    if phrase_matched:
        score += 80.0
        match_kind = "phrase"
    matched_tokens = set(tokens) & set(token_list)
    if not token_match_is_strong_enough(tokens, matched_tokens, phrase_matched=phrase_matched):
        return (candidate_boost, "candidate") if candidate_boost else (0.0, "none")
    distinct_matches = 0
    for token in tokens:
        count = haystack_tokens.get(token, 0)
        if not count:
            continue
        distinct_matches += 1
        score += 1.0 + (min(count, 2) * 0.25)
        if token in title_lower:
            score += 8.0
        if token in cwd_lower:
            score += 4.0
    score += distinct_matches * distinct_matches * 1.5
    score += proximity_score(token_list, tokens)
    if not tokens and not phrase and score <= 0:
        score = 0.1
    return score, match_kind


def event_role_weight(role: str) -> float:
    normalized = role.lower()
    if normalized == "user":
        return 1.0
    if normalized == "assistant":
        return 0.7
    if normalized == "actions":
        return 0.18
    if normalized == "tool_result":
        return 0.12
    return 0.35


def generated_session_penalty(*, title: str, cwd: str) -> float:
    title_lower = title.lower().strip()
    cwd_lower = cwd.lower()
    if MYBOT_CODEX_WORKSPACE_FRAGMENT in cwd_lower:
        return 0.2
    if any(title_lower.startswith(prefix) for prefix in GENERATED_SESSION_TITLE_PREFIXES):
        return 0.2
    return 1.0


def score_document(
    *,
    document: TrajectoryDocument,
    query: str,
    tokens: list[str],
    candidate_boost: float,
) -> tuple[float, str]:
    header_score, header_kind = score_text(
        text="\n".join([document.title, document.cwd, json.dumps(document.metadata, ensure_ascii=False)]),
        title=document.title,
        cwd=document.cwd,
        query=query,
        tokens=tokens,
        candidate_boost=0.0,
    )

    best_event_score = 0.0
    best_event_kind = ""
    first_user_seen = False
    for event in document.events:
        if event.role.lower() not in SEARCHABLE_EVENT_ROLES:
            continue
        is_first_user_turn = event.role == "user" and not first_user_seen
        if event.role == "user":
            first_user_seen = True
        event_score, event_kind = score_text(
            text=event.text,
            title=document.title,
            cwd=document.cwd,
            query=query,
            tokens=tokens,
            candidate_boost=0.0,
        )
        if event_score <= 0:
            continue
        weighted_score = event_score * event_role_weight(event.role)
        if is_first_user_turn:
            weighted_score *= 1.25
            weighted_score += 40.0
        if weighted_score > best_event_score:
            best_event_score = weighted_score
            best_event_kind = event_kind

    score = candidate_boost + (header_score * 1.35) + best_event_score
    score *= generated_session_penalty(title=document.title, cwd=document.cwd)
    if score <= 0:
        return 0.0, "none"
    if best_event_score >= header_score:
        return score, best_event_kind or "event"
    return score, header_kind or "header"


def event_snippets_for_query(
    events: list[ParsedEvent],
    *,
    query: str,
    title: str,
    cwd: str,
    limit: int = 4,
) -> list[str]:
    tokens = query_tokens(query)
    primary_scored: list[tuple[float, int]] = []
    secondary_scored: list[tuple[float, int]] = []
    first_user_seen = False
    for index, event in enumerate(events):
        is_first_user_turn = event.role == "user" and not first_user_seen
        if event.role == "user":
            first_user_seen = True
        base, _ = score_text(
            text=event.text,
            title=title,
            cwd=cwd,
            query=query,
            tokens=tokens,
            candidate_boost=0.0,
        )
        if base <= 0:
            continue
        if event.role in {"user", "assistant"}:
            weighted = base * event_role_weight(event.role)
            if is_first_user_turn:
                weighted = weighted * 1.25 + 40.0
            primary_scored.append((weighted, index))
        else:
            # Rank command output by raw relevance, not the role-discounted
            # score. The discount exists for cross-session ranking; within a
            # session a direct data hit (a balance line, a disk-usage table) is
            # precisely the evidence a live-status question needs.
            secondary_scored.append((base, index))

    primary_scored.sort(reverse=True)
    secondary_scored.sort(reverse=True)
    windows: list[tuple[int, int]] = []
    seen_indexes: set[int] = set()

    def add_window(center: int, *, before: int | None = None, after: int | None = None) -> None:
        if center in seen_indexes:
            return
        seen_indexes.add(center)
        event = events[center]
        if before is None:
            before = 0 if event.role in {"user", "assistant"} else 0
        if after is None:
            after = 1 if event.role == "user" else 0
        start = max(0, center - before)
        end = min(len(events), center + after + 1)
        windows.append((start, end))

    final_assistant_index = None
    if likely_trajectory_question(query):
        for index in range(len(events) - 1, -1, -1):
            if events[index].role == "assistant":
                final_assistant_index = index
                break

    # Reserve a slot for the strongest command-output (tool_result/actions)
    # match: live data such as SU balances and disk usage lives there and must
    # not be crowded out by prose turns that merely discuss it.
    strong_secondary = bool(secondary_scored) and secondary_scored[0][0] >= 60.0
    primary_budget = limit
    if final_assistant_index is not None:
        primary_budget -= 1
    if strong_secondary:
        primary_budget -= 1
    primary_budget = max(1, primary_budget)

    for _, index in primary_scored[:primary_budget]:
        add_window(index)
        if len(windows) >= primary_budget:
            break

    if final_assistant_index is not None and len(windows) < limit:
        add_window(final_assistant_index, before=0, after=0)

    for _, index in secondary_scored:
        if len(windows) >= limit:
            break
        add_window(index, before=0, after=0)

    snippets: list[str] = []
    rendered_seen: set[str] = set()
    for start, end in windows:
        snippet = render_events(events[start:end], max_chars=1800)
        if snippet and snippet not in rendered_seen:
            rendered_seen.add(snippet)
            snippets.append(snippet)
        if len(snippets) >= limit:
            break

    if snippets:
        return snippets
    return snippets_for_query(render_events(events), query, limit=limit)


def proximity_score(token_list: list[str], tokens: list[str], *, window: int = 40) -> float:
    token_set = set(tokens)
    positions = [(index, token) for index, token in enumerate(token_list) if token in token_set]
    if len(positions) < 3:
        return 0.0
    best = 0
    left = 0
    counts: dict[str, int] = {}
    for right, (position, token) in enumerate(positions):
        counts[token] = counts.get(token, 0) + 1
        while position - positions[left][0] > window:
            old_token = positions[left][1]
            counts[old_token] -= 1
            if counts[old_token] <= 0:
                counts.pop(old_token, None)
            left += 1
        best = max(best, len(counts))
    if best < 3:
        return 0.0
    return float(best * best * 5)


class TrajectoryLookup:
    def __init__(self, *, source_names: list[str], max_files_per_tool: int) -> None:
        self.source_names = source_names
        self.max_files_per_tool = max_files_per_tool
        self._lock = threading.Lock()
        self._sessions: list[NormalizedTrajectory] | None = None
        self._documents: dict[str, TrajectoryDocument] = {}

    def clear(self) -> None:
        with self._lock:
            self._sessions = None
            self._documents = {}

    def sessions(self) -> list[NormalizedTrajectory]:
        with self._lock:
            if self._sessions is not None:
                return list(self._sessions)

        sessions: list[NormalizedTrajectory] = []
        for adapter in get_source_adapters(self.source_names):
            sessions.extend(adapter.discover_sessions(max_files=self.max_files_per_tool))
        sessions.sort(key=lambda session: session.updated_at, reverse=True)

        with self._lock:
            self._sessions = sessions
        return list(sessions)

    def find_session(self, source_ref: str) -> NormalizedTrajectory | None:
        wanted = source_ref.strip()
        if not wanted:
            return None
        for session in self.sessions():
            ref = source_ref_for(session)
            if ref == wanted or session.session_id == wanted:
                return session
            raw_session_id = str(session.metadata.get("raw_session_id") or "")
            if raw_session_id and f"{session.source_name}:{raw_session_id}" == wanted:
                return session
        return None

    def document_for(self, session: NormalizedTrajectory) -> TrajectoryDocument:
        source_ref = source_ref_for(session)
        with self._lock:
            cached = self._documents.get(source_ref)
            if cached is not None:
                return cached

        events = read_events_for_session(session)
        transcript = render_events(events)
        document = TrajectoryDocument(
            source_name=session.source_name,
            source_ref=source_ref,
            session_id=session.session_id,
            title=session.title,
            updated_at=session.updated_at,
            cwd=metadata_cwd(session),
            file_path=session.file_path,
            metadata=dict(session.metadata),
            events=events,
            transcript=transcript,
        )
        with self._lock:
            self._documents[source_ref] = document
        return document

    def read(self, source_ref: str, *, query: str = "", max_chars: int = 12000) -> dict[str, Any] | None:
        session = self.find_session(source_ref)
        if session is None:
            return None
        doc = self.document_for(session)
        return read_payload_from_file(
            source_name=doc.source_name,
            source_ref=doc.source_ref,
            session_id=doc.session_id,
            title=doc.title,
            updated_at=doc.updated_at,
            cwd=doc.cwd,
            file_path=doc.file_path,
            metadata=doc.metadata,
            query=query,
            max_chars=max_chars,
        )

    def search(
        self,
        *,
        query: str,
        limit: int = 5,
        candidate_refs: dict[str, float] | None = None,
        full_scan: bool = False,
    ) -> list[TrajectorySearchResult]:
        tokens = query_tokens(query)
        candidate_refs = candidate_refs or {}
        preliminary: list[tuple[float, str, NormalizedTrajectory]] = []

        for session in self.sessions():
            source_ref = source_ref_for(session)
            cwd = metadata_cwd(session)
            searchable = "\n".join(
                [
                    session.title,
                    cwd,
                    session.updated_at,
                    session.search_text,
                    json.dumps(session.metadata, ensure_ascii=False),
                ]
            )
            score, match_kind = score_text(
                text=searchable,
                title=session.title,
                cwd=cwd,
                query=query,
                tokens=tokens,
                candidate_boost=candidate_refs.get(source_ref, 0.0),
            )
            if score <= 0:
                continue
            preliminary.append((score, match_kind, session))

        preliminary.sort(key=lambda item: (item[0], item[2].updated_at), reverse=True)
        scan_sessions = preliminary
        if not full_scan:
            scan_sessions = preliminary[: max(limit * 4, 12)]

        ranked: list[TrajectorySearchResult] = []
        for preliminary_score, preliminary_match_kind, session in scan_sessions:
            doc = self.document_for(session)
            score, match_kind = score_document(
                document=doc,
                query=query,
                tokens=tokens,
                candidate_boost=candidate_refs.get(doc.source_ref, 0.0),
            )
            if score <= 0:
                continue

            searchable_transcript = primary_event_transcript(doc.events)
            snippets = snippets_for_query(searchable_transcript, query, limit=3)
            if not snippets:
                snippets = [normalize_text(searchable_transcript or doc.transcript, 900)]
            ranked.append(
                TrajectorySearchResult(
                    source_name=doc.source_name,
                    source_ref=doc.source_ref,
                    session_id=doc.session_id,
                    title=doc.title,
                    updated_at=doc.updated_at,
                    cwd=doc.cwd,
                    score=score,
                    match_kind=match_kind,
                    snippets=snippets,
                    metadata=doc.metadata,
                )
            )

        ranked.sort(key=lambda item: (item.score, item.updated_at), reverse=True)
        return ranked[:limit]


def candidate_refs_from_memory(memory_sources: list[dict[str, Any]]) -> dict[str, float]:
    refs: dict[str, float] = {}
    for index, source in enumerate(memory_sources):
        if source.get("source_type") != "trajectory":
            continue
        ref = str(source.get("parent_source_ref") or source.get("source_ref") or "")
        if ":chunk:" in ref:
            ref = ref.split(":chunk:", 1)[0]
        if not ref:
            continue
        refs[ref] = max(refs.get(ref, 0.0), 60.0 - index * 4.0)
    return refs


def render_evidence(results: list[dict[str, Any]], *, max_chars: int = 18000) -> str:
    if not results:
        return ""
    sections: list[str] = [
        (
            "The following trajectory evidence was retrieved by local source adapters. "
            "Use it as stronger grounding than summaries. It may be excerpted when the full parsed trajectory is large."
        )
    ]
    total = len(sections[0])
    for index, result in enumerate(results, start=1):
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        header = [
            f"### Candidate {index}",
            f"Source ref: {result.get('source_ref')}",
            f"Tool: {result.get('source_name')}",
            f"Updated: {result.get('updated_at')}",
            f"Title: {result.get('title')}",
        ]
        cwd = result.get("cwd") or metadata.get("cwd")
        if cwd:
            header.append(f"Working directory: {cwd}")
        transcript = str(result.get("transcript") or "")
        block = "\n".join(header) + "\n\nParsed trajectory evidence:\n" + transcript
        remaining = max_chars - total - 2
        if remaining <= 0:
            break
        if len(block) > remaining:
            block = block[: remaining - 30].rstrip() + "\n\n[truncated]"
        sections.append(block)
        total += len(block) + 2
    return "\n\n".join(sections)
