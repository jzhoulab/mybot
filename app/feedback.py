"""Answers people said missed, kept for the owner to review.

A 👎 on one of mybot's answers (Discord or Slack) records a rating here and
asks the person what was off. Their reply goes back into the same
conversation as a feedback turn, so mybot takes another pass, and the note
is attached to the rating. The owner reads the list with GET /feedback.

The file holds the newest entries only. It is the owner's state: nothing
here is shown to anyone else.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MAX_ENTRIES = 1000
PREVIEW_CHARS = 400
# A note that arrives within this window joins the rating it answers.
COMMENT_JOIN_SECONDS = 2 * 60 * 60

FEEDBACK_TURN_PREFIX = "(Feedback on your previous answer: it missed for them.)"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _preview(text: Any) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"


def last_exchange(messages: list[dict[str, Any]]) -> dict[str, str]:
    """The latest assistant answer in a session and the question before it."""
    answer: dict[str, Any] | None = None
    question = ""
    for entry in reversed(messages):
        role = entry.get("role")
        if answer is None:
            if role == "assistant":
                answer = entry
            continue
        if role == "user":
            question = str(entry.get("content") or "")
            break
    if answer is None:
        return {}
    meta = answer.get("meta") if isinstance(answer.get("meta"), dict) else {}
    return {
        "question": _preview(question),
        "answer": _preview(answer.get("content")),
        "answer_hash": str(meta.get("response_hash") or ""),
        "answered_at": str(answer.get("timestamp") or ""),
    }


class FeedbackStore:
    def __init__(self, state_dir: str | Path) -> None:
        self.path = Path(state_dir) / "feedback.json"
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []

    def _save(self, entries: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entries[-MAX_ENTRIES:], indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def record(
        self,
        *,
        actor_id: str,
        session_key: str,
        rating: str = "down",
        comment: str = "",
        exchange: dict[str, str] | None = None,
        platform: str = "",
        display_name: str = "",
        channel_label: str = "",
    ) -> dict[str, Any]:
        """A rating, or a note on one. A note from the same person in the same
        session shortly after their rating fills in that rating instead of
        adding a second entry."""
        now = _now()
        comment = comment.strip()[:4000]
        with self._lock:
            entries = self._load()
            if comment:
                for entry in reversed(entries):
                    if entry.get("actor_id") != actor_id or entry.get("session_key") != session_key:
                        continue
                    created = _parse(str(entry.get("created_at") or ""))
                    if entry.get("comment") or created is None:
                        break
                    if now - created <= timedelta(seconds=COMMENT_JOIN_SECONDS):
                        entry["comment"] = comment
                        entry["commented_at"] = _iso(now)
                        self._save(entries)
                        return entry
                    break
            entry = {
                "created_at": _iso(now),
                "rating": rating,
                "comment": comment,
                "actor_id": actor_id,
                "display_name": display_name,
                "platform": platform,
                "channel_label": channel_label,
                "session_key": session_key,
                **(exchange or {}),
            }
            entries.append(entry)
            self._save(entries)
            return entry

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            entries = self._load()
        return list(reversed(entries))[: max(0, limit)]


# -- Bridge side: which bot messages were answers, and who owes a note. --

FEEDBACK_ASK = (
    "Sorry that missed. What were you looking for, or what's off? "
    "Reply here and I'll take another pass."
)
# How long after a 👎 the person's next message counts as their note.
PENDING_SECONDS = 30 * 60


def is_thumbs_down(name: str) -> bool:
    """Discord sends the emoji (with any skin tone); Slack sends a name."""
    name = (name or "").strip(":")
    return name.startswith("👎") or name.split("::", 1)[0] in {"-1", "thumbsdown"}


class AnswerLedger:
    """Bounded map from a bot message id to the chat request it answered,
    so a 👎 lands in that conversation (not, say, on a pulse post)."""

    def __init__(self, capacity: int = 4000) -> None:
        from collections import OrderedDict

        self.capacity = capacity
        self._items: "OrderedDict[str, Any]" = OrderedDict()

    def add(self, message_id: Any, ref: Any) -> None:
        key = str(message_id)
        self._items[key] = ref
        self._items.move_to_end(key)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def get(self, message_id: Any) -> Any:
        return self._items.get(str(message_id))


class PendingFeedback:
    """Who was asked what missed, keyed by (place, person). Expires."""

    def __init__(self, ttl_seconds: float = PENDING_SECONDS) -> None:
        self.ttl = ttl_seconds
        self._items: dict[tuple[str, str], tuple[float, Any]] = {}

    def expect(self, place: Any, person: Any, ref: Any, now: float) -> None:
        self._items = {k: v for k, v in self._items.items() if v[0] > now}
        self._items[(str(place), str(person))] = (now + self.ttl, ref)

    def peek(self, place: Any, person: Any, now: float) -> Any:
        item = self._items.get((str(place), str(person)))
        return item[1] if item and item[0] > now else None

    def take(self, place: Any, person: Any, now: float) -> Any:
        item = self._items.pop((str(place), str(person)), None)
        return item[1] if item and item[0] > now else None
