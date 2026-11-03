"""Team sharing: which of the owner's projects teammates may ask about.

Two layers, kept apart on purpose:

- SharingPolicy (state/sharing.json) is the owner's decisions and is what the
  server enforces. In "reviewed" mode (the default for team use) a teammate
  sees a project only once the owner marked it `team`; everything else —
  new, unreviewed, `owner` — stays owner-only. Single sessions can be made
  owner-only inside a shared project (a tax question asked from a work dir),
  or shared on their own: much of the valuable work starts from a catch-all
  folder (a hop session in the home dir), where the folder says nothing.
- SharingReviewer (state/sharing_review.json) is an agent that reads each
  project's session titles and the owner's own typed prompts and SUGGESTS a
  level with a reason, and flags personal sessions inside work projects. It
  also goes through individual sessions no teammate can see yet and recommends
  the ones worth sharing. It never decides; the owner confirms in the menu app.
  Where the agent is unsure it asks instead of guessing ("Need your decision"),
  and the owner's answers, those above all, guide its later reviews.

Chats started in the Codex GUI outside any project are where personal and
logistics questions go: they are never recommended and can't be shared. Where
a chat's origin can't be established it counts as one of those.

Levels: team (teammates can ask), owner (indexed, owner-only), exclude (not
indexed at all).
"""
from __future__ import annotations

import json
import glob
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger
from app.redact import redact_text

log = get_logger("mybot.sharing", "server.log")

LEVELS = ("team", "owner", "exclude")

# Cheap prefilter for the review digest, not a decision: the agent judges.
_SENSITIVE_WORDS = (
    "grant", "r01", "r21", "r35", "nih", "nsf", "specific aims", "research strategy", "biosketch",
    "budget", "proposal", "resubmission", "tax", "receipt", "reimburs", "salary", "offer letter",
    "tenure", "promotion", "recommendation", "reference letter", "visa", "green card", "immigration",
    "mortgage", "insurance", "doctor", "medical", "kid", "baby", "daughter",
    "son's", "wife", "husband", "family", "student evaluation", "grading", "peer review", "referee",
    "reviewer comments", "confidential", "personal",
)


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


def _no_project_dir(cwd: str) -> bool:
    """A chat with no real folder: a catch-all, or the Codex app's own
    per-chat folders under ~/Documents/Codex."""
    if is_catch_all_dir(cwd):
        return True
    documents = str(Path.home() / "Documents" / "Codex")
    return (cwd or "").startswith(documents)


_origin_cache: dict[str, Any] = {"at": 0.0, "threads": {}}
_origin_lock = threading.Lock()


def codex_thread_origins() -> dict[str, dict[str, str]]:
    """{thread id: {originator, source}} from the Codex app's own database."""
    with _origin_lock:
        if time.monotonic() - _origin_cache["at"] < 600 and _origin_cache["threads"]:
            return _origin_cache["threads"]
        threads: dict[str, dict[str, str]] = {}
        home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        for path in glob.glob(str(Path(home) / "state_*.sqlite")):
            try:
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
                try:
                    for ident, originator, source in conn.execute("SELECT id, originator, source FROM threads"):
                        threads[str(ident)] = {"originator": str(originator or ""), "source": str(source or "")}
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                log.warning("could not read Codex threads from %s: %s", path, exc)
        _origin_cache.update(at=time.monotonic(), threads=threads)
        return threads


def is_codex_gui_chat(source_name: str, session_id: str, cwd: str,
                      origins: dict[str, dict[str, str]] | None = None) -> bool:
    """A Codex chat with no project folder that isn't provably from a
    terminal. Fails closed: unknown origin counts as a GUI chat."""
    if (source_name or "").strip().lower() != "codex" or not _no_project_dir(cwd):
        return False
    if (cwd or "").startswith(str(Path.home() / "Documents" / "Codex")):
        return True
    origin = (codex_thread_origins() if origins is None else origins).get(_session_id(session_id))
    if not origin:
        return True
    if "desktop" in origin["originator"].lower():
        return True
    return not (origin["originator"] == "codex-tui" or origin["source"] == "cli")


_hop_cache: dict[str, Any] = {"at": 0.0, "names": {}}


def hop_session_names() -> dict[str, str]:
    """{conversation id: hop session name} for conversations hop hosts or has
    hosted. Empty for anyone not using hop."""
    with _origin_lock:
        if time.monotonic() - _hop_cache["at"] < 300:
            return _hop_cache["names"]
    from app.briefing import hop_agent_sessions  # the live ones, Codex included

    readable: dict[str, str] = {}
    try:
        proc = subprocess.run(["hopa", "sessions", "--json"], capture_output=True, text=True, timeout=30)
        listing = json.loads(proc.stdout or "[]")
        for item in listing if isinstance(listing, list) else listing.get("sessions") or []:
            readable[str(item.get("internalName"))] = str(item.get("name") or "")
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    names: dict[str, str] = {}
    for path in (Path.home() / ".hop2" / "claude-sessions").glob("*.json"):
        try:
            ident = json.loads(path.read_text()).get("sessionId")
        except (OSError, ValueError):
            continue
        # A closed session keeps only its record; its file is named after it
        # unless that's an opaque internal id.
        name = readable.get(path.stem) or ("" if path.stem.startswith("s_") else path.stem)
        if ident:
            names[str(ident).lower()] = name
    for conversation, target in hop_agent_sessions().items():
        names[conversation.lower()] = str(target.get("name") or "")
    with _origin_lock:
        _hop_cache.update(at=time.monotonic(), names=names)
    return names


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
            data.setdefault("sessions", {})
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
            ident = _session_id(session_ref) if session_ref else ""
            if ident and ident in {_session_id(r) for r in data["private_sessions"]}:
                return True
            if self.mode == "open":
                return False
            decided = data["sessions"].get(ident) if ident else None
            if decided:
                return decided.get("level") != "team"
            if is_catch_all_dir(cwd):
                # Never shared, whatever was decided: anything can start there.
                return True
            entry = data["projects"].get(project_key(source_name, cwd))
        return (entry or {}).get("level") != "team"

    def session_shared(self, session_ref: str) -> bool:
        """The owner shared this one session (and it isn't marked private)."""
        ident = _session_id(session_ref or "")
        if not ident:
            return False
        with self._lock:
            data = self._load()
            if ident in {_session_id(r) for r in data["private_sessions"]}:
                return False
            return (data["sessions"].get(ident) or {}).get("level") == "team"

    def decide_session(self, ref: str, level: str, *, by: str = "owner", chunks: int = 0) -> None:
        if level not in ("team", "owner"):
            raise ValueError("a session is shared (team) or kept (owner)")
        with self._lock:
            data = self._load()
            data["sessions"][_session_id(ref)] = {
                "ref": ref, "level": level, "decided_at": _now(), "by": by, "chunks": chunks,
            }
            _write_json(self.path, data)
            self._mtime = -1.0

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
letters, admissions; confidential peer review of others' papers or grants; the owner's own grant \
and funding applications (NIH/NSF/foundation proposals, specific aims, research strategy, budgets, \
biosketches, progress reports, resubmission responses); personal email and correspondence; anything \
about specific people's private matters. Also mixed or unclear projects.
- "exclude": noise not worth indexing — throwaway scratch dirs, automated test runs, generated \
agent workspaces with no human work.
When in doubt between team and owner, choose owner with confidence "low", which puts it to the owner \
as a question. Past decisions, when given below, are how this owner actually decides: follow their pattern.

Also flag individual sessions inside a project you'd mark "team" that look personal or confidential \
by the same rules (e.g. a tax question asked from a work folder, or grant writing inside a code \
repo). Use the session refs shown.

Reply with ONLY a JSON array, one object per project, in the order given:
[{"key": "<project key>", "level": "team|owner|exclude", "confidence": "high|medium|low", \
"reason": "<one short plain sentence>", "private_sessions": [{"ref": "<ref>", "reason": "<short>"}]}]"""


SESSION_REVIEW_PROMPT = """You help a researcher decide which of their individual AI coding-agent \
sessions their teammates may ask a bot about. Each session below is currently invisible to teammates, \
usually because it was started from a general folder. You get its title, folder, size, and some of the \
owner's own requests. Most valuable work happens in long working sessions, so actively look for those.

Recommend "team" when the session is research, engineering, data, infrastructure or lab-tool work whose \
facts would help colleagues: where data lives, how something was built or run, results, decisions, \
methods. Recommend "owner" for: personal life, family, health, money, taxes, immigration, housing, \
travel and logistics; job searches, tenure/promotion, salaries, HR; grading, evaluations, recommendation \
letters, admissions; confidential peer review; the owner's own grant and funding applications; personal \
email and correspondence; private matters of specific people; commercial or side projects outside the \
lab's research; and sessions too thin to be useful. A mostly-work session containing some of the above \
is "owner".

Don't guess. When you can't tell which way the owner would go (it could be either, a mostly-work session \
with a sensitive part, a project you can't place), answer "ask" and write the "question" that would settle \
it. These are the most useful answers you can give: the owner's reply teaches you their preference.
Past decisions, when given below, are how this owner actually decides: follow their pattern over these \
general rules.

Reply with ONLY a JSON array, one object per session:
[{"ref": "<ref>", "name": "<what the session is about, 3-7 words, not its first request>", \
"level": "team|owner|ask", "confidence": "high|medium|low", \
"reason": "<one short plain sentence: for team, what a colleague could learn from it>", \
"question": "<for ask: the one question for the owner, else empty>"}]"""


def needs_decision(session: dict[str, Any]) -> bool:
    """The agent asked, or would only be guessing."""
    return session.get("level") == "ask" or session.get("confidence") == "low"


class SharingReviewer:
    """Runs the suggesting agent in the background, a few projects per call."""

    def __init__(self, state: Any, *, path: Path, model: str, batch_size: int = 8) -> None:
        self.state = state
        self.path = path
        self.model = model
        self.batch_size = max(1, batch_size)
        self.session_batch_size = 12
        self.sessions_per_run = int(os.environ.get("SHARING_SESSIONS_PER_RUN", "120") or 120)
        self._lock = threading.Lock()
        self.status: dict[str, Any] = {"running": False}

    # ---------------------------------------------------------------- data
    def suggestions(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        data.setdefault("projects", {})
        data.setdefault("sessions", {})
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
        # Older sessions fall outside the recent window above; surface any
        # whose title or the owner's prompts hit a sensitive word so the
        # agent can still flag them.
        recent = {row["source_ref"] for row in sessions}
        clause = " OR ".join(["lower(title) LIKE ?"] * len(_SENSITIVE_WORDS))
        flagged = conn.execute(
            f"""SELECT source_ref, MAX(title) AS title FROM trajectory_chunks
                WHERE source_name = ? AND cwd = ? AND ({clause}) GROUP BY source_ref LIMIT 25""",
            (source, cwd, *[f"%{w}%" for w in _SENSITIVE_WORDS]),
        ).fetchall()
        prompt_clause = " OR ".join(["lower(text) LIKE ?"] * len(_SENSITIVE_WORDS))
        flagged_prompts = conn.execute(
            f"""SELECT session_id, substr(text, 1, 180) AS t FROM prompt_history
                WHERE source_name = ? AND cwd = ? AND ({prompt_clause}) ORDER BY ts DESC LIMIT 15""",
            (source, cwd, *[f"%{w}%" for w in _SENSITIVE_WORDS]),
        ).fetchall()
        extra = [f"- {row['source_ref']}: {(row['title'] or '')[:110]}" for row in flagged if row["source_ref"] not in recent]
        extra += [f"- {source}:{row['session_id']}: (request) {' '.join(str(row['t']).split())}" for row in flagged_prompts]
        if extra:
            lines.append("older sessions with possibly sensitive words (check these):")
            lines += extra
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

    def run_async(self, *, full: bool = False) -> bool:
        if self.status.get("running"):
            return False
        threading.Thread(target=self.run, kwargs={"full": full}, name="sharing-review", daemon=True).start()
        return True

    def run(self, limit: int | None = None, *, full: bool = False) -> dict[str, Any]:
        """full: re-suggest every project the owner hasn't decided (after the
        review rules change), not only new or grown ones."""
        with self._lock:
            if self.status.get("running"):
                return self.status
            self.status = {"running": True, "started_at": _now(), "done": 0}
        try:
            data = self.suggestions()
            if full:
                decided = self.state.sharing.snapshot()["projects"]
                todo = [p for p in self.projects() if project_key(p["source_name"], p["cwd"]) not in decided]
            else:
                todo = self.pending(data)
            todo = todo[: limit or None]
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
                        ][:50],
                        "at": _now(),
                    }
                    self.status["done"] += 1
                self._save(data)
            self.status.update({"running": False, "finished_at": _now()})
        except Exception as exc:
            log.exception("sharing review failed")
            self.status.update({"running": False, "error": str(exc)})
        return self.status

    # ------------------------------------------------- individual sessions
    def session_candidates(self, data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Owner-typed sessions a teammate can't see, not decided by the
        owner and not reviewed yet (or grown a lot since), most useful first:
        hop sessions and terminal Codex, then the largest and newest."""
        data = data or self.suggestions()
        policy = self.state.sharing.snapshot()
        access = self.state.access_config
        origins = codex_thread_origins()
        hop = hop_session_names()
        with self._connect() as conn:
            typed = {(row[0], row[1]) for row in conn.execute(
                "SELECT DISTINCT source_name, session_id FROM prompt_history")}
            rows = conn.execute(
                """SELECT source_ref, source_name, session_id, cwd, COUNT(*) AS chunks, MAX(updated_at) AS last
                   FROM trajectory_chunks WHERE source_name IN ('claude', 'codex') AND cwd != ''
                   GROUP BY source_ref"""
            ).fetchall()
        private = {_session_id(ref) for ref in policy["private_sessions"]}
        last_answer = max((str(s.get("decided_at") or "") for s in policy.get("sessions", {}).values()
                           if s.get("by") == "owner"), default="")
        out = []
        for row in rows:
            source, ident, cwd = row["source_name"], row["session_id"], row["cwd"]
            ident_key = _session_id(ident)
            if (source, ident) not in typed or ident_key in private or ident_key in policy.get("sessions", {}):
                continue
            if access.is_agent_workspace(cwd) or is_codex_gui_chat(source, ident, cwd, origins):
                continue
            level = (policy["projects"].get(project_key(source, cwd)) or {}).get("level")
            if level == "exclude" or (level == "team" and not is_catch_all_dir(cwd)):
                continue
            seen = data["sessions"].get(row["source_ref"])
            if seen and needs_decision(seen) and str(seen.get("at") or "") < last_answer:
                # The owner has answered something since: it may settle this.
                out.append({**dict(row), "hop": ident_key in hop, "hop_name": hop.get(ident_key, ""),
                            "project_level": None, "_rank": True})
                continue
            grown = not seen or int(row["chunks"]) >= 2 * max(int(seen.get("chunks") or 0), 25)
            unnamed = bool(seen) and seen.get("level") == "team" and not seen.get("name")
            if not grown and not unnamed:
                continue
            origin = origins.get(ident_key) or {}
            hosted = ident_key in hop or origin.get("originator") == "codex-tui" or origin.get("source") == "cli"
            out.append({**dict(row), "hop": ident_key in hop, "hop_name": hop.get(ident_key, ""), "project_level": level, "_rank": hosted})
        out.sort(key=lambda s: (not s["_rank"], -int(s["chunks"]), s["last"] or ""))
        return out

    def session_digest(self, conn: sqlite3.Connection, session: dict[str, Any]) -> str:
        source, ident = session["source_name"], session["session_id"]
        title = conn.execute(
            "SELECT title FROM trajectory_chunks WHERE source_ref = ? ORDER BY updated_at DESC LIMIT 1",
            (session["source_ref"],),
        ).fetchone()
        session["title"] = str(title["title"] or "")[:120] if title else ""
        prompts = [str(row["text"]) for row in conn.execute(
            "SELECT text FROM prompt_history WHERE source_name = ? AND session_id = ? ORDER BY ts", (source, ident))]
        sensitive = [p for p in prompts if any(w in p.lower() for w in _SENSITIVE_WORDS)]
        picked = list(dict.fromkeys(prompts[:6] + prompts[-6:] + sensitive[:8]))
        lines = [
            f"### {session['source_ref']}", f"title: {session['title']}", f"folder: {session['cwd']}",
            f"size: {session['chunks']} chunks, {len(prompts)} owner requests, last active {str(session['last'])[:10]}",
            "owner's requests:",
        ]
        lines += [f"- {' '.join(p.split())[:220]}" for p in picked]
        return redact_text("\n".join(lines))

    def run_sessions(self, limit: int | None = None) -> dict[str, Any]:
        """Review the next sessions and record recommendations. Sharing still
        takes the owner's yes (the menu app shows these), but a shared session that has since turned
        personal is taken back straight away."""
        data = self.suggestions()
        todo = self.session_candidates(data)[: limit or self.sessions_per_run]
        todo += self._shared_grown(data)
        new_team = 0
        for start in range(0, len(todo), self.session_batch_size):
            batch = todo[start:start + self.session_batch_size]
            try:
                with self._connect() as conn:
                    message = "\n\n".join(self.session_digest(conn, s) for s in batch)
                results = self._ask_json(SESSION_REVIEW_PROMPT, message + self.preference_examples(data))
            except Exception as exc:  # keep going; the next run retries
                log.warning("sharing session review failed: %s", exc)
                continue
            by_ref = {str(item.get("ref")): item for item in results if isinstance(item, dict)}
            for session in batch:
                result = by_ref.get(session["source_ref"])
                if not result:
                    continue
                level = result.get("level") if result.get("level") in ("team", "owner") else "ask"
                data["sessions"][session["source_ref"]] = {
                    "question": str(result.get("question") or "")[:300],
                    "level": level, "confidence": str(result.get("confidence") or "low"),
                    "name": str(result.get("name") or "")[:80],
                    "reason": str(result.get("reason") or "")[:300], "title": session.get("title", ""),
                    "source": session["source_name"], "cwd": session["cwd"], "chunks": int(session["chunks"]),
                    "last": session["last"], "hop": bool(session.get("hop")), "hop_name": session.get("hop_name", ""), "at": _now(),
                }
                if session.get("shared") and level != "team":
                    self.state.sharing.decide_session(session["source_ref"], "owner", by="reviewer",
                                                      chunks=int(session["chunks"]))
                    log.info("sharing: took back %s, it no longer looks like shareable work", session["source_ref"])
                elif session.get("shared"):
                    self.state.sharing.decide_session(session["source_ref"], "team", by="owner",
                                                      chunks=int(session["chunks"]))
                elif level == "team" and not needs_decision(data["sessions"][session["source_ref"]]):
                    new_team += 1
            self._save(data)
        return {"reviewed": len(todo), "recommended": new_team}

    def _shared_grown(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Shared sessions that kept growing: their new content needs a look."""
        shared = {ident: s for ident, s in self.state.sharing.snapshot().get("sessions", {}).items()
                  if s.get("level") == "team"}
        if not shared:
            return []
        marks = ",".join("?" for _ in shared)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT source_ref, source_name, session_id, cwd, COUNT(*) AS chunks, MAX(updated_at) AS last
                    FROM trajectory_chunks WHERE session_id IN ({marks}) GROUP BY source_ref""", list(shared),
            ).fetchall()
        return [
            {**dict(row), "shared": True} for row in rows
            if int(row["chunks"]) >= int(shared[_session_id(row["session_id"])].get("chunks") or 0) + 25
        ]

    def decisions(self, data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Sessions the agent can't call. A shared one it took back because it
        was no longer sure comes here too: the owner has the last word."""
        data = data or self.suggestions()
        policy = self.state.sharing.snapshot()
        private = {_session_id(ref) for ref in policy["private_sessions"]}
        answered = {ident for ident, s in policy.get("sessions", {}).items() if s.get("by") != "reviewer"}
        waiting = [
            {"ref": ref, **s} for ref, s in data["sessions"].items()
            if needs_decision(s) and _session_id(ref) not in private and _session_id(ref) not in answered
        ]
        self.with_hop_names(waiting)
        waiting.sort(key=lambda s: (s.get("level") != "ask", not s.get("hop"), -int(s.get("chunks") or 0)))
        return waiting

    def preference_examples(self, data: dict[str, Any] | None = None, limit: int = 30) -> str:
        """What the owner actually chose, for the agent to learn from: the
        questions it asked and the calls it got wrong first."""
        data = data or self.suggestions()
        by_id = {_session_id(ref): s for ref, s in data["sessions"].items()}
        rows = []
        for ident, decision in self.state.sharing.snapshot().get("sessions", {}).items():
            if decision.get("by") != "owner":
                continue
            seen = by_id.get(ident) or {}
            shared = decision.get("level") == "team"
            overruled = seen.get("level") in ("team", "owner") and seen.get("level") != decision.get("level")
            rows.append(((not needs_decision(seen), not overruled, str(decision.get("decided_at") or "")),
                         f"- {seen.get('name') or seen.get('title') or ident} ({seen.get('cwd', '')}): "
                         f"{seen.get('question') or seen.get('reason') or ''} → owner chose "
                         f"{'to share' if shared else 'to keep private'}"))
        if not rows:
            return ""
        # Questions first, then overruled calls; newest first inside each group.
        ordered: list[str] = []
        for flags in ((False, False), (False, True), (True, False), (True, True)):
            group = sorted((r for r in rows if r[0][:2] == flags), key=lambda r: r[0][2], reverse=True)
            ordered += [r[1] for r in group]
        return redact_text("\n\nPast decisions by this owner:\n" + "\n".join(ordered[:limit]))

    def recommended(self, data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Sessions the agent would share and the owner hasn't answered."""
        data = data or self.suggestions()
        policy = self.state.sharing.snapshot()
        private = {_session_id(ref) for ref in policy["private_sessions"]}
        waiting = [
            {"ref": ref, **s} for ref, s in data["sessions"].items()
            if s.get("level") == "team" and not needs_decision(s) and _session_id(ref) not in private
            and _session_id(ref) not in policy.get("sessions", {})
        ]
        rank = {"high": 0, "medium": 1, "low": 2}
        self.with_hop_names(waiting)
        waiting.sort(key=lambda s: (rank.get(s.get("confidence"), 3), not s.get("hop"), -int(s.get("chunks") or 0)))
        return waiting

    @staticmethod
    def with_hop_names(sessions: list[dict[str, Any]]) -> None:
        """The hop session each is in now, if any; a name seen at review time
        outlasts the hop session closing."""
        names = hop_session_names()
        for session in sessions:
            ident = _session_id(str(session.get("ref") or ""))
            if names.get(ident):
                session["hop_name"] = names[ident]
            if ident in names:
                session["hop"] = True

    def _ask_json(self, system: str, message: str) -> list[Any]:
        result = self.state.provider.run_claude_task(
            system_prompt=system, message=message, model=self.model, effort="low", with_tools=False, timeout=600,
        )
        text = str(result.get("text") or "")
        start, end = text.find("["), text.rfind("]")
        return json.loads(text[start:end + 1]) if start >= 0 and end > start else []

    def _ask(self, batch: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            message = "\n\n".join(self.digest(conn, project) for project in batch) + self.preference_examples()
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
