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


# -- Search reports: an agent on this machine says trajectory search came up
# short. Kept like answer feedback, and passed on (batched) to the hop session
# that maintains mybot, so search gets fixed instead of silently worked around.

SEARCH_REPORT_DIR = Path.home() / ".mybot" / "search-feedback"


class SearchReports:
    def __init__(self, state_dir: str | Path) -> None:
        self.path = Path(state_dir) / "search_feedback.json"
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

    def record(self, report: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        entry = {"id": now.strftime("%Y%m%dT%H%M%S%fZ"), "created_at": _iso(now), "notified": "", **report}
        with self._lock:
            entries = self._load()
            entries.append(entry)
            self._save(entries)
        return entry

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._load() if not e.get("notified")]

    def mark(self, ids: set[str], how: str) -> None:
        with self._lock:
            entries = self._load()
            for entry in entries:
                if entry.get("id") in ids:
                    entry["notified"] = how
            self._save(entries)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._load()))[: max(0, limit)]


def search_report_file(reports: list[dict[str, Any]], directory: Path = SEARCH_REPORT_DIR) -> Path:
    """The details, one section per report, for the maintainer to pick up."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{reports[-1]['id']}.md"
    lines = [
        f"(Search feedback for mybot: {len(reports)} report(s) from agents on this machine that "
        "trajectory search came up short.)",
        "",
        "For each: reproduce the queries, find why the wanted result ranked low or was missing "
        "(indexing, chunking, ranking, filters, the CLI's defaults or the skill's guidance), and "
        "improve it where that helps search in general, not just this one query.",
    ]
    for n, report in enumerate(reports, 1):
        who = ", ".join(x for x in (
            report.get("agent"), f"hop session {report['hop_session']}" if report.get("hop_session") else "",
            report.get("conversation"), report.get("cwd"),
        ) if x)
        lines += ["", f"## {n}. {report.get('wanted') or 'Search came up short'}", "",
                  f"- When: {report.get('created_at')}", f"- From: {who or 'unknown'}"]
        for query in report.get("queries") or []:
            lines.append(f"- Query: `{query}`")
        if report.get("got"):
            lines.append(f"- What came back: {report['got']}")
        if report.get("found"):
            lines.append(f"- Where it really was: {report['found']}")
        if report.get("conversation"):
            lines.append(f"- Reporter's conversation: `mybot trajectory-read --source-ref {report['conversation']}`")
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def search_report_message(reports: list[dict[str, Any]], path: Path) -> str:
    # One line: hop submits input with Enter, so a newline would split it.
    first = " ".join(str(reports[0].get("wanted") or "").split())[:120]
    more = f" and {len(reports) - 1} more" if len(reports) > 1 else ""
    return (
        f"From mybot search feedback: an agent reported that trajectory search came up short "
        f"(\"{first}\"{more}). Details in {path} - please look into improving search; check with me "
        "before anything irreversible or outward-facing."
    )


class SearchFeedbackNotifier:
    """Batches search reports into one hop message to the maintainer session.
    A report starts a short wait so a burst goes out as one message. Reports
    stay pending while that session isn't open (or doesn't let agents type
    into it), and go with the next batch. The maintainer's own reports are
    kept but not sent back to it."""

    def __init__(
        self,
        reports: SearchReports,
        *,
        session_name: str,
        find_session: Any,  # name -> internal name of the live, agent-permitted session, or ""
        send: Any,  # (internal, message) -> {"ok": bool, ...}
        delay_seconds: float = 120.0,
        directory: Path = SEARCH_REPORT_DIR,
    ) -> None:
        self.reports = reports
        self.session_name = session_name
        self.find_session = find_session
        self.send = send
        self.delay = delay_seconds
        self.directory = directory
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def schedule(self) -> None:
        if not self.session_name:
            return
        with self._lock:
            if self._timer is not None:
                return
            self._timer = threading.Timer(self.delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        try:
            self.flush()
        except Exception:  # a notifier must never take the server down
            import logging

            logging.getLogger("mybot.feedback").warning("search feedback notify failed", exc_info=True)

    def flush(self) -> dict[str, Any]:
        pending = self.reports.pending()
        own = {e["id"] for e in pending if e.get("hop_session") == self.session_name}
        if own:
            self.reports.mark(own, "own session")
        pending = [e for e in pending if e["id"] not in own]
        if not pending:
            return {"ok": True, "sent": 0}
        internal = self.find_session(self.session_name)
        if not internal:
            return {"ok": False, "sent": 0, "waiting": len(pending), "error": f"{self.session_name} not open"}
        path = search_report_file(pending, self.directory)
        result = self.send(internal, search_report_message(pending, path))
        if not result.get("ok"):
            return {"ok": False, "sent": 0, "waiting": len(pending), "error": str(result)[:300]}
        self.reports.mark({e["id"] for e in pending}, f"hop:{self.session_name} {path.name}")
        return {"ok": True, "sent": len(pending), "file": str(path)}
