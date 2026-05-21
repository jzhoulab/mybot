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
    "<task-notification>",
    "<local-command-stdout>",
    "Automation:",
    "Automation ID:",
    "<subagent_notification>",
    "Independently run a",
)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-")
ROLLOUT_RE = re.compile(r"^rollout-\d{4}-\d{2}-\d{2}T")
SLUG_RE = re.compile(r"^[a-z]+-[a-z]+-[a-z]+$")


def parse_timestamp(raw: str) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def recent_files(patterns: list[str], limit: int) -> list[str]:
    files: list[str] = []
    for pattern in patterns:
        files.extend(glob.glob(os.path.expanduser(pattern), recursive=True))
    files = [path for path in files if os.path.isfile(path)]
    files.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    return files[:limit]


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
