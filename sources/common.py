from __future__ import annotations

import glob
import os
import re
from datetime import datetime

from .models import NormalizedTurn, normalize_text


AUTO_PREFIXES = (
    "# AGENTS.md",
    "<INSTRUCTIONS>",
    "<environment_context>",
    "<turn_aborted>",
    "<system-reminder>",
    "<command-name>",
    "<command-message>",
    "<task-notification>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "Automation:",
    "Automation ID:",
    "<subagent_notification>",
    "<!-- agent-session:",  # orchestrator marker (see docs/agent-session-convention.md)
    "Independently run a",
    "Caveat: The messages below",
    # injected agent prompts (sdk/automated sessions) — never a human title
    "You are a ",
    "You are an ",
    "You are the ",
    # compaction boilerplate that opens every continued session — the real
    # request is in a later turn
    "This session is being continued",
)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-")
ROLLOUT_RE = re.compile(r"^rollout-\d{4}-\d{2}-\d{2}T")
SLUG_RE = re.compile(r"^[a-z]+-[a-z]+-[a-z]+$")

# A single trajectory event can be tens/hundreds of MB (embedded files/base64).
# `for line in f` would buffer such a line whole and hang/OOM whoever reads it.
MAX_JSONL_LINE_BYTES = 512 * 1024


def iter_bounded_jsonl_lines(path: str, *, max_total_bytes: int | None = None):
    """Yield decoded json-line strings with bounded memory.

    Lines over MAX_JSONL_LINE_BYTES are skipped (yielded as None so callers can
    count them). With max_total_bytes set, stops after reading that much.
    """
    total = 0
    with open(path, "rb") as handle:
        while True:
            raw = handle.readline(MAX_JSONL_LINE_BYTES)
            if not raw:
                break
            total += len(raw)
            if max_total_bytes is not None and total > max_total_bytes:
                break
            if not raw.endswith(b"\n") and len(raw) >= MAX_JSONL_LINE_BYTES:
                # oversized line — drain to the next newline without buffering it
                while True:
                    extra = handle.readline(MAX_JSONL_LINE_BYTES)
                    total += len(extra)
                    if not extra or extra.endswith(b"\n") or (
                        max_total_bytes is not None and total > max_total_bytes
                    ):
                        break
                yield None
                continue
            yield raw.decode("utf-8", "replace")


def parse_timestamp(raw: str) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def recent_files(patterns: list[str], limit: int) -> list[str]:
    """Newest-first source files. `limit <= 0` means no cap — callers that
    filter as they scan use that to rank by recency over the files they can
    actually use, rather than over raw files."""
    files: list[str] = []
    for pattern in patterns:
        files.extend(glob.glob(os.path.expanduser(pattern), recursive=True))
    files = [path for path in files if os.path.isfile(path)]
    files.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    return files if limit <= 0 else files[:limit]


BOT_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,19}$")


def derive_bot_handle(*, override: str = "", aliases: list[str] | None = None, display_name: str = "") -> str:
    """A short, legible handle for naming the bot instance (<handle>-mybot), so a
    team can tell whose bot is whose in a shared server/workspace. Prefer an
    explicit override, else the shortest username-like alias the identity
    discovered (e.g. 'alice'), else a slug of the display name."""
    override = (override or "").strip().lower()
    if override:
        slug = re.sub(r"[^a-z0-9._-]+", "-", override).strip("-._")
        if slug:
            return slug[:20]
    candidates = [
        alias.strip().lower()
        for alias in (aliases or [])
        if BOT_HANDLE_RE.match(alias.strip().lower())
    ]
    if candidates:
        return min(candidates, key=lambda handle: (len(handle), handle))
    slug = re.sub(r"[^a-z0-9]+", "-", (display_name or "").lower()).strip("-")
    return slug[:20] or "owner"


def format_bot_name(template: str, handle: str) -> str:
    template = template or "{handle}-mybot"
    try:
        name = template.format(handle=handle)
    except (KeyError, IndexError, ValueError):
        name = f"{handle}-mybot"
    return name.strip()[:32] or f"{handle}-mybot"


def is_human_title(title: str) -> bool:
    if not title:
        return False
    return not UUID_RE.match(title) and not ROLLOUT_RE.match(title) and not SLUG_RE.match(title)


def is_real_user_text(text: str) -> bool:
    cleaned = (text or "").strip()
    if len(cleaned) < 3:
        return False
    return not any(cleaned.startswith(prefix) for prefix in AUTO_PREFIXES)


def choose_title(explicit_title: str, user_turns: list[str], fallback: str) -> str:
    if is_human_title(explicit_title):
        return explicit_title
    for turn in user_turns:
        if is_real_user_text(turn):
            return normalize_text(turn, limit=90)
    return fallback


def make_short_summary(title: str, user_turns: list[str], assistant_turns: list[str]) -> str:
    lead = normalize_text(user_turns[0], 140) if user_turns else title
    trail = normalize_text(assistant_turns[-1], 120) if assistant_turns else ""
    if trail:
        return f"{lead} Last assistant note: {trail}"
    return lead


def make_detailed_summary(title: str, user_turns: list[str], assistant_turns: list[str]) -> str:
    parts = [f"Title: {title}."]
    if user_turns:
        recent_users = "; ".join(normalize_text(turn, 160) for turn in user_turns[-3:])
        parts.append(f"Recent user turns: {recent_users}.")
    if assistant_turns:
        recent_assistant = "; ".join(normalize_text(turn, 160) for turn in assistant_turns[-2:])
        parts.append(f"Recent assistant notes: {recent_assistant}.")
    return " ".join(parts)


def append_turn(turns: list[NormalizedTurn], role: str, text: str, timestamp: str | None = None) -> None:
    cleaned = normalize_text(text, 800)
    if not cleaned:
        return
    if role == "user" and not is_real_user_text(cleaned):
        return
    if turns and turns[-1].role == role and turns[-1].text == cleaned:
        return
    turns.append(NormalizedTurn(role=role, text=cleaned, timestamp=timestamp))
