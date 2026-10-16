"""Team sharing: which of the owner's projects teammates may ask about.

Two layers, kept apart on purpose:

- SharingPolicy (state/sharing.json) is the owner's decisions and is what the
  server enforces. In "reviewed" mode (the default for team use) a teammate
  sees a project only once the owner marked it `team`; everything else —
  new, unreviewed, `owner` — stays owner-only. Single sessions can be made
  owner-only inside a shared project (a tax question asked from a work dir).
- SharingReviewer (state/sharing_review.json) is an agent that reads each
  project's session titles and the owner's own typed prompts and SUGGESTS a
  level with a reason, and flags personal sessions inside work projects. It
  never decides; the owner confirms in the menu app.

Levels: team (teammates can ask), owner (indexed, owner-only), exclude (not
indexed at all).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger
from app.redact import redact_text

log = get_logger("mybot.sharing", "server.log")

LEVELS = ("team", "owner", "exclude")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_CATCH_ALL_NAMES = {"code", "documents", "desktop", "downloads", "projects", "src", "work", "tmp"}


def is_catch_all_dir(cwd: str) -> bool:
    """Folders sessions get started from for anything at all — the home dir,
    ~/Code itself, Downloads, temp dirs, an agent's own config dir. A project
    there is not one project, so it is never suggested for the team."""
    path = Path(os.path.expanduser((cwd or "").strip() or "/")).resolve(strict=False)
    home = Path.home().resolve()
    if path in (Path("/"), home) or str(path).startswith(("/private/tmp", "/private/var/", "/tmp")):
        return True
    if path.parent == home and path.name.lower() in _CATCH_ALL_NAMES:
        return True
    try:
        rel = path.relative_to(home)
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0].startswith((".claude", ".codex"))


def _session_id(ref: str) -> str:
    """claude:<uuid>#3, <uuid>, codex:<uuid> all name the same session."""
    return ref.split("#", 1)[0].rsplit(":", 1)[-1].strip().lower()


def project_key(source_name: str, cwd: str) -> str:
    return f"{(source_name or '').strip().lower()}:{(cwd or '').strip()}"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


class SharingPolicy:
    def __init__(self, path: Path, *, mode: str = "reviewed") -> None:
        self.path = path
        self.mode = mode if mode in ("reviewed", "open") else "reviewed"
        self._lock = threading.Lock()
        self._mtime = -1.0
        self._data: dict[str, Any] = {}

    def _load(self) -> dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime != self._mtime:
            try:
                data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                data = {}
            data.setdefault("projects", {})
            data.setdefault("private_sessions", {})
            self._data, self._mtime = data, mtime
        return self._data

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._load()))

    def level(self, source_name: str, cwd: str) -> str | None:
        with self._lock:
            entry = self._load()["projects"].get(project_key(source_name, cwd))
        return (entry or {}).get("level")

    def hidden_from_team(self, source_name: str, cwd: str, session_ref: str = "") -> bool:
        """True when a teammate must not see this session."""
        with self._lock:
            data = self._load()
            if session_ref and _session_id(session_ref) in {_session_id(r) for r in data["private_sessions"]}:
                return True
            if self.mode == "open":
                return False
            if is_catch_all_dir(cwd):
                # Never shared, whatever was decided: anything can start there.
                return True
            entry = data["projects"].get(project_key(source_name, cwd))
        return (entry or {}).get("level") != "team"

    def decide(self, key: str, level: str, *, by: str = "owner") -> None:
        if level not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS}")
        with self._lock:
            data = self._load()
            data["projects"][key] = {"level": level, "decided_at": _now(), "by": by}
            _write_json(self.path, data)
            self._mtime = -1.0

    def set_session_private(self, ref: str, private: bool, *, reason: str = "") -> None:
        with self._lock:
            data = self._load()
            if private:
                data["private_sessions"][ref] = {"at": _now(), "reason": reason}
            else:
                data["private_sessions"].pop(ref, None)
            _write_json(self.path, data)
            self._mtime = -1.0


REVIEW_SYSTEM_PROMPT = """You help a researcher decide which of their AI coding-agent projects their \
teammates may ask questions about. Teammates will query a bot that answers from these sessions, so \
anything shared can be quoted to colleagues.

For each project you get its folder, how many sessions it has, recent session titles, and some of \
the owner's own typed requests. Suggest one level:
- "team": research, engineering, data analysis, papers in progress, lab tools and infrastructure — \
work the owner would normally discuss with collaborators.
- "owner": keep it to the owner. Personal life, family, health, money, taxes, immigration, housing; \
job searches, tenure/promotion files, salaries, HR; grading, student evaluations, recommendation \
letters, admissions; confidential peer review of others' papers or grants; personal email and \
correspondence; anything about specific people's private matters. Also mixed or unclear projects.
- "exclude": noise not worth indexing — throwaway scratch dirs, automated test runs, generated \
agent workspaces with no human work.
When in doubt between team and owner, choose owner.

Also flag individual sessions inside a project you'd mark "team" that look personal or confidential \
by the same rules (e.g. a tax question asked from a work folder). Use the session refs shown.

Reply with ONLY a JSON array, one object per project, in the order given:
[{"key": "<project key>", "level": "team|owner|exclude", "confidence": "high|medium|low", \
"reason": "<one short plain sentence>", "private_sessions": [{"ref": "<ref>", "reason": "<short>"}]}]"""


class SharingReviewer:
    """Runs the suggesting agent in the background, a few projects per call."""

    def __init__(self, state: Any, *, path: Path, model: str, batch_size: int = 8) -> None:
        self.state = state
        self.path = path
        self.model = model
        self.batch_size = max(1, batch_size)
        self._lock = threading.Lock()
        self.status: dict[str, Any] = {"running": False}

    # ---------------------------------------------------------------- data
    def suggestions(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        data.setdefault("projects", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        _write_json(self.path, data)

    def _connect(self) -> sqlite3.Connection:
        db = str(self.state.config.trajectory_index_db_path)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def projects(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT source_name, cwd, COUNT(DISTINCT source_ref) AS sessions, MAX(updated_at) AS last
                   FROM trajectory_chunks WHERE cwd != '' GROUP BY source_name, cwd ORDER BY last DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def digest(self, conn: sqlite3.Connection, project: dict[str, Any]) -> str:
        source, cwd = project["source_name"], project["cwd"]
        sessions = conn.execute(
            """SELECT source_ref, MAX(title) AS title, MAX(updated_at) AS u FROM trajectory_chunks
               WHERE source_name = ? AND cwd = ? GROUP BY source_ref ORDER BY u DESC LIMIT 30""",
            (source, cwd),
        ).fetchall()
        prompts = conn.execute(
            """SELECT session_id, substr(text, 1, 180) AS t FROM prompt_history
               WHERE source_name = ? AND cwd = ? ORDER BY ts DESC LIMIT 18""",
            (source, cwd),
        ).fetchall()
        lines = [f"### {project_key(source, cwd)}", f"sessions: {project['sessions']}", "titles:"]
        lines += [f"- {row['source_ref']}: {(row['title'] or '')[:110]}" for row in sessions]
        if prompts:
            lines.append("owner's requests:")
            lines += [f"- {source}:{row['session_id']}: {' '.join(str(row['t']).split())}" for row in prompts]
        return redact_text("\n".join(lines))

    # ------------------------------------------------------------- running
    def pending(self, data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Projects with no suggestion yet, or that grew a lot since."""
        data = data or self.suggestions()
        decided = self.state.sharing.snapshot()["projects"]
        out = []
        for project in self.projects():
            key = project_key(project["source_name"], project["cwd"])
            seen = data["projects"].get(key)
            if key in decided and seen:
                continue
            stale_catch_all = bool(seen) and seen.get("level") != "owner" and is_catch_all_dir(project["cwd"])
            if seen and not stale_catch_all and int(project["sessions"]) < int(seen.get("sessions") or 0) + 5:
                continue
            out.append(project)
        return out

    def run_async(self) -> bool:
        if self.status.get("running"):
            return False
        threading.Thread(target=self.run, name="sharing-review", daemon=True).start()
        return True

    def run(self, limit: int | None = None) -> dict[str, Any]:
        with self._lock:
            if self.status.get("running"):
                return self.status
            self.status = {"running": True, "started_at": _now(), "done": 0}
        try:
            data = self.suggestions()
            todo = self.pending(data)[: limit or None]
            self.status["total"] = len(todo)
            access = self.state.access_config
            agent_batch: list[dict[str, Any]] = []
            for project in todo:
                key = project_key(project["source_name"], project["cwd"])
                # No agent needed: generated agent workspaces and the owner's
                # private chat homes are owner-only by construction.
                if access.is_agent_workspace(project["cwd"]) or access.is_private_workdir(
                    project["source_name"], project["cwd"]
                ):
                    data["projects"][key] = {
                        "level": "owner", "confidence": "high", "sessions": project["sessions"],
                        "reason": "Private chat home or agent workspace — kept to you.",
                        "private_sessions": [], "at": _now(),
                    }
                    self.status["done"] += 1
                    continue
                if is_catch_all_dir(project["cwd"]):
                    data["projects"][key] = {
                        "level": "owner", "confidence": "high", "sessions": project["sessions"],
                        "reason": "A catch-all folder (home, ~/Code, Downloads, temp) where any kind of session starts — kept to you.",
                        "private_sessions": [], "at": _now(),
                    }
                    self.status["done"] += 1
                    continue
                agent_batch.append(project)
            self._save(data)
            for start in range(0, len(agent_batch), self.batch_size):
                batch = agent_batch[start:start + self.batch_size]
                try:
                    results = self._ask(batch)
                except Exception as exc:  # keep going; the next run retries
                    log.warning("sharing review batch failed: %s", exc)
                    continue
                for project in batch:
                    key = project_key(project["source_name"], project["cwd"])
                    result = results.get(key)
                    if not result:
                        continue
                    level = result.get("level") if result.get("level") in LEVELS else "owner"
                    data["projects"][key] = {
                        "level": level,
                        "confidence": str(result.get("confidence") or "low"),
                        "reason": str(result.get("reason") or "")[:300],
                        "sessions": project["sessions"],
                        "private_sessions": [
                            {"ref": str(s.get("ref")), "reason": str(s.get("reason") or "")[:200]}
                            for s in result.get("private_sessions") or []
                            if isinstance(s, dict) and str(s.get("ref") or "").startswith(("claude:", "codex:"))
                        ][:20],
                        "at": _now(),
                    }
                    self.status["done"] += 1
                self._save(data)
            self.status.update({"running": False, "finished_at": _now()})
        except Exception as exc:
            log.exception("sharing review failed")
            self.status.update({"running": False, "error": str(exc)})
        return self.status

    def _ask(self, batch: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            message = "\n\n".join(self.digest(conn, project) for project in batch)
        started = time.time()
        result = self.state.provider.run_claude_task(
            system_prompt=REVIEW_SYSTEM_PROMPT,
            message=message,
            model=self.model,
            effort="low",
            with_tools=False,
            timeout=600,
        )
        text = str(result.get("text") or "")
        start, end = text.find("["), text.rfind("]")
        parsed = json.loads(text[start:end + 1]) if start >= 0 and end > start else []
        log.info("sharing review: %d projects in %.0fs", len(batch), time.time() - started)
        return {str(item.get("key")): item for item in parsed if isinstance(item, dict)}
