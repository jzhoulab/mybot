"""Daily "pulse": ideas and overlooked things, written for the owner.

The owner already gets per-session status elsewhere, so this deliberately does
NOT recap sessions. It reads mainly what the OWNER typed (their own voice,
filtered from tool-injected and automated prompts), then: a scout picks article
topics and candidate overlooked items; writers research and write short plain-
language articles; a checker keeps only overlooked items that are really still
open; an editor assembles; a verifier fact- and jargon-checks.

(Original design notes below describe the plumbing, which is unchanged.)

Daily chief-of-staff briefing.

Every morning (BRIEFING_TIME, local) mybot reads what happened since the last
briefing and writes the owner a short, prioritized briefing: what matters
today, commitments and loose ends, risks, and ideas worth acting on — each item
grounded in their own sessions, with outside research where it helps.

One model turn cannot do this well, so it is a pipeline of agents:

  1. gather      (no LLM)  typed prompts + changed sessions in the window, a
                           7-day backdrop, and the open loops the previous
                           briefing handed forward
  2. plan        (fast)    group the activity into a few workstreams
  3. analysts    (deep)    one agent per workstream, in parallel: reads the
                           real transcripts through the mybot tools, may search
                           the web, reports status / open loops / risks
  4. chief of staff (deepest) synthesizes, prioritizes, researches further
  5. verifier    (deep)    re-checks every claim against the evidence and
                           drops or softens what it cannot support

Output goes to state/briefings/<date>.md (+ .json with every stage), a menu
app chat thread (so the owner can ask follow-ups in place), the owner's
Discord DM session, and an outbox the Discord bridge delivers from.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.server import AppState

log = get_logger("mybot.briefing", "briefing.log")

THREAD_PREFIX = "menuapp-briefing-"


def _env(name: str, default: str) -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _fmt_local(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%a %H:%M")
    except ValueError:
        return iso[:16]


ENGINE_LABELS = {"claude": "Claude", "gpt": "GPT"}


class BriefingRunner:
    def __init__(self, state: "AppState", engine: str = "claude") -> None:
        self.state = state
        self.engine = engine
        # Claude keeps the original file names; other engines add a suffix.
        self.suffix = "" if engine == "claude" else f".{engine}"
        self.enabled = _env_bool("BRIEFING_ENABLED", True)
        self.at = _env("BRIEFING_TIME", "08:00")
        self.analyst_effort = _env("BRIEFING_ANALYST_EFFORT", "xhigh")
        self.chief_effort = _env("BRIEFING_CHIEF_EFFORT", "max")
        self.verify_effort = _env("BRIEFING_VERIFY_EFFORT", "high")
        self.planner_effort = _env("BRIEFING_PLANNER_EFFORT", "medium")
        # Each engine uses its strongest model by default.
        self.model = (
            _env("BRIEFING_GPT_MODEL", "gpt-6-astra") if engine == "gpt" else _env("BRIEFING_MODEL", "fable")
        )
        self.web_research = _env_bool("BRIEFING_WEB_RESEARCH", True)
        self.max_workstreams = int(_env("BRIEFING_MAX_WORKSTREAMS", "6"))
        self.parallel = int(_env("BRIEFING_PARALLEL_ANALYSTS", "3"))
        self.retrieval_budget = _env("BRIEFING_RETRIEVAL_BUDGET_SECONDS", "900")
        self.agent_timeout = int(_env("BRIEFING_AGENT_TIMEOUT_SECONDS", "2700"))
        self.discord_dm = _env_bool("BRIEFING_DISCORD_DM", True)
        self.dir = Path(state.config.state_dir) / "briefings"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.outbox_path = Path(state.config.state_dir) / "outbox.jsonl"
        self._lock = threading.Lock()
        self.status: dict[str, Any] = {"running": False, "stage": "", "started_at": "", "last": None}

    # ---------------------------------------------------------------- schedule

    def start_scheduler(self) -> None:
        if not self.enabled:
            log.info("daily briefing disabled (BRIEFING_ENABLED=false)")
            return

        def loop() -> None:
            while True:
                try:
                    if self._due():
                        self.run()
                except Exception:  # pragma: no cover - keep the scheduler alive
                    log.warning("briefing scheduler error:\n%s", traceback.format_exc())
                time.sleep(120)

        threading.Thread(target=loop, name="daily-briefing", daemon=True).start()
        log.info("daily briefing scheduled for %s local", self.at)

    def _due(self) -> bool:
        now = _local_now()
        try:
            hour, minute = (int(part) for part in self.at.split(":", 1))
        except ValueError:
            hour, minute = 8, 0
        if (now.hour, now.minute) < (hour, minute):
            return False
        # Once per local day; a laptop asleep at the scheduled time catches up
        # on wake. A failed run is retried on the next tick only after an hour.
        if (self.dir / f"{now.date().isoformat()}{self.suffix}.md").exists():
            return False
        failed = self.dir / f"{now.date().isoformat()}{self.suffix}.failed"
        if failed.exists() and time.time() - failed.stat().st_mtime < 3600:
            return False
        return not self.status["running"]

    # --------------------------------------------------------------------- run

    def run(self, *, force: bool = False) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "a briefing is already running", "status": self.status}
        day = _local_now().date().isoformat()
        started = time.time()
        record: dict[str, Any] = {"date": day, "started_at": datetime.now(timezone.utc).isoformat()}
        try:
            self.status.update(running=True, started_at=record["started_at"], stage="gather")
            evidence = self.gather()
            record["evidence"] = {k: v for k, v in evidence.items() if k != "previous"}
            if not evidence["recent_prompts"] and not force:
                text = self._quiet_day_note(evidence)
            else:
                self.status["stage"] = "scout"
                scout = self.scout(evidence)
                record["scout"] = scout
                self.status["stage"] = "articles + overlooked checks"
                with ThreadPoolExecutor(max_workers=max(1, self.parallel)) as pool:
                    articles_future = pool.submit(self.write_articles, evidence, scout)
                    overlooked_future = pool.submit(self.check_overlooked, evidence, scout)
                    articles = articles_future.result()
                    overlooked = overlooked_future.result()
                record["articles"] = articles
                record["overlooked"] = overlooked
                self.status["stage"] = "editor"
                draft = self.edit(evidence, articles, overlooked)
                record["draft"] = draft
                self.status["stage"] = "verify"
                text = self.verify(evidence, draft)
            text, carry = self._split_carry(text)
            record["carry"] = carry
            record["open_loops"] = carry.get("overlooked", []) if isinstance(carry, dict) else []
            record["topics"] = carry.get("topics", []) if isinstance(carry, dict) else []
            record["briefing"] = text
            self.status["stage"] = "follow-up actions"
            record["sections"] = self.structure(text)
            record["actions"] = self.make_actions(evidence, record["sections"])
            record["seconds"] = round(time.time() - started, 1)
            self._write(day, text, record)
            self.status["stage"] = "deliver"
            record["delivered"] = self.deliver(day, text)
            (self.dir / f"{day}{self.suffix}.json").write_text(json.dumps(record, indent=1, default=str))
            try:
                publish_for_hop(getattr(self.state, "briefings", {}) or {self.engine: self})
            except Exception as exc:  # pragma: no cover - phone copy is best-effort
                log.warning("could not publish pulse for hop: %s", exc)
            (self.dir / f"{day}{self.suffix}.failed").unlink(missing_ok=True)
            self.status["last"] = {"date": day, "ok": True, "seconds": record["seconds"]}
            log.info("daily briefing %s written in %.0fs", day, record["seconds"])
            return {"ok": True, "date": day, "seconds": record["seconds"], "path": str(self.dir / f"{day}{self.suffix}.md")}
        except Exception as exc:
            (self.dir / f"{day}{self.suffix}.failed").write_text(traceback.format_exc())
            self.status["last"] = {"date": day, "ok": False, "error": str(exc)}
            log.warning("daily briefing failed: %s", exc)
            return {"ok": False, "error": str(exc)}
        finally:
            self.status.update(running=False, stage="")
            self._lock.release()

    # ------------------------------------------------------------------ gather

    def _previous(self, limit: int = 5) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.dir.glob(f"????-??-??{self.suffix}.json"), reverse=True)[:limit]:
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            out.append(
                {
                    "date": data.get("date"),
                    "started_at": data.get("started_at"),
                    "briefing": str(data.get("briefing") or "")[:5000],
                    "open_loops": data.get("open_loops") or [],
                    "topics": data.get("topics") or [],
                }
            )
        return out

    # Prompts that tools inject on the owner's behalf (handoffs, reconnects,
    # plan executions, pasted dumps) are not how the owner talks; they would
    # drown the owner's own voice, which is the briefing's primary signal.
    _INJECTED = re.compile(
        r"^(Read the ENTIRE file|You are driving a Nebula notebook|We were disconnected|"
        r"Implement the following plan|<!--\s*agent-session|In notebook /|Continue from where|"
        r"\[Pasted text|Caveat: The messages below|<command-name>|<local-command|"
        r"This session is being continued|Summarize prior conversation|\[Agent-driver|"
        r"Reply with exactly|Review expected|You are (an?|the) [a-z -]*agent\b|"
        r".*\bcontinue authorized\b)",
        re.I,
    )

    _ACKS = {"sure", "ok", "okay", "yes", "y", "proceed", "continue", "go ahead", "go", "thanks",
             "thank you", "do it", "yes please", "sounds good", "please proceed", "great", "lgtm"}

    def _owner_voice(self, text: str) -> bool:
        text = text.strip()
        if len(text) < 4 or len(text) > 1500:
            return False
        if text.lower().strip(" .!") in self._ACKS:
            return False
        if text.startswith("/") and " " not in text:
            return False
        return not self._INJECTED.match(text)

    def gather(self) -> dict[str, Any]:
        previous = self._previous()
        now = datetime.now(timezone.utc)
        # One shared window across engines (anchored on the latest run of ANY
        # engine) so side-by-side pulses are comparable.
        anchors = []
        for path in self.dir.glob("????-??-??*.json"):
            try:
                anchors.append(str(json.loads(path.read_text()).get("started_at") or ""))
            except (OSError, json.JSONDecodeError):
                continue
        anchors = [a for a in anchors if a]
        since = max(anchors) if anchors else (now - timedelta(hours=36)).isoformat()
        since = max(since, (now - timedelta(days=4)).isoformat())
        backdrop_since = (now - timedelta(days=14)).isoformat()
        db = self.state.config.trajectory_index_db_path
        recent: list[dict[str, Any]] = []
        backdrop: list[dict[str, Any]] = []
        titles: dict[str, str] = {}
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT source_name, session_id, ts, cwd, text FROM prompt_history WHERE ts >= ? ORDER BY ts",
                (backdrop_since,),
            ).fetchall()
            # Text repeated verbatim many times is a scheduler or driver, not the owner.
            repeats: dict[str, int] = {}
            for row in rows:
                key = str(row["text"] or "").strip()[:200]
                repeats[key] = repeats.get(key, 0) + 1
            session_info: dict[str, tuple[str, str, str]] = {}

            def info(ref: str) -> tuple[str, str, str]:
                if ref not in session_info:
                    one = conn.execute(
                        "SELECT title, cwd, COALESCE(json_extract(metadata_json, '$.origin'), '') "
                        "FROM trajectory_chunks WHERE source_ref = ? LIMIT 1",
                        (ref,),
                    ).fetchone()
                    session_info[ref] = (str(one[0] or ""), str(one[1] or ""), str(one[2] or "")) if one else ("", "", "")
                return session_info[ref]

            for row in rows:
                text = str(row["text"] or "").strip()
                if not self._owner_voice(text) or repeats.get(text[:200], 0) >= 3:
                    continue
                ref = f"{row['source_name']}:{row['session_id']}"
                title, session_cwd, origin = info(ref)
                if origin == "automated":
                    continue
                item = {
                    "source_ref": ref,
                    "ts": row["ts"],
                    "project": self._project_name(str(row["cwd"] or "") or session_cwd),
                    "text": text[:700],
                }
                if row["ts"] >= since:
                    recent.append(item)
                    titles[ref] = title[:100]
                else:
                    backdrop.append(item)
        return {
            "window_start": since,
            "window_end": now.isoformat(),
            "recent_prompts": recent[-350:],
            "backdrop_prompts": backdrop[-500:],
            "session_titles": titles,
            "previous": previous,
            "owner": self._owner_line(),
        }

    @staticmethod
    def _project_name(cwd: str) -> str:
        base = cwd.rstrip("/").rsplit("/", 1)[-1] if cwd else ""
        # ~/.nebula/agent/p-<hex>-<project> mirrors → the project's name.
        return re.sub(r"^p-[0-9a-f]{6}-", "", base) or cwd

    def _owner_line(self) -> str:
        parts = []
        try:
            name = str(self.state.get_owner_identity().get("display_name") or "")
            if name:
                parts.append(f"Name: {name}.")
        except Exception:
            pass
        try:
            profile = self.state.get_owner_profile()
            if profile:
                parts.append(profile)
        except Exception:
            pass
        return "\n".join(parts)[:2500]

    def _voice_digest(self, evidence: dict[str, Any], *, recent: int = 350, backdrop: int = 250) -> str:
        lines = [
            "# What the owner typed — their own words, the primary signal",
            f"## Since the last briefing ({_fmt_local(evidence['window_start'])} → now)",
        ]
        for p in evidence["recent_prompts"][-recent:]:
            title = evidence["session_titles"].get(p["source_ref"], "")
            lines.append(f"- [{_fmt_local(p['ts'])}] ({p['project']}; {p['source_ref']}; “{title[:50]}”) {p['text'][:400]}")
        lines += ["", "## Earlier, last two weeks (background)"]
        for p in evidence["backdrop_prompts"][-backdrop:]:
            lines.append(f"- [{p['ts'][:10]}] ({p['project']}) {p['text'][:240]}")
        if evidence["previous"]:
            lines += ["", "## Already covered by recent briefings (don't repeat)"]
            for prev in evidence["previous"][:5]:
                topics = ", ".join(str(t) for t in prev.get("topics") or [])
                if topics:
                    lines.append(f"- {prev['date']}: articles on {topics}")
                for loop in prev.get("open_loops") or []:
                    item = loop.get("item") if isinstance(loop, dict) else loop
                    lines.append(f"- {prev['date']}: flagged “{str(item)[:160]}”")
        return "\n".join(lines)

    # ------------------------------------------------------------------- agents

    def _agent(self, *, label: str, system: str, message: str, effort: str, tools: bool, web: bool) -> str:
        provider = self.state.provider
        active = provider.active_model()
        model = self.model or active["model"]
        budget_log = self.state.new_retrieval_budget_log_path() if tools else None
        tool_env = (
            self.state.tool_env_for_request(
                actor_id=self.state.config.imported_owner_actor_id,
                memory_scope="private",
                target_user_id=None,
                retrieval_budget_log_path=str(budget_log) if budget_log else None,
                budget_seconds=float(self.retrieval_budget),
            )
            if tools
            else None
        )
        started = time.time()
        if self.engine == "gpt":
            # Codex has no separate system prompt in exec mode; it always has
            # its (sandboxed) shell, and --search turns on live web search.
            result = provider._run_codex(
                prompt=f"{system}\n\n---\n\n{message}",
                backend_session_id=None,
                tool_env=tool_env,
                model=self.model,
                reasoning_effort=effort,
                search=bool(web and self.web_research),
                timeout=self.agent_timeout,
            )
        else:
            result = provider.run_claude_task(
                system_prompt=system,
                message=message,
                model=model,
                effort=effort,
                with_tools=tools,
                extra_tools=["WebSearch", "WebFetch"] if (web and self.web_research) else [],
                tool_env=tool_env,
                timeout=self.agent_timeout,
            )
        log.info("briefing %s finished in %.0fs", label, time.time() - started)
        return str(result.get("text") or "").strip()

    def _tool_section(self) -> str:
        tool = f"{self.state.config.mybot_tool_python} {self.state.config.mybot_tool_path}"
        web = (
            "You have WebSearch and WebFetch for real research. Cite URLs."
            if self.web_research
            else "You have no web access."
        )
        return (
            "## Tools\n"
            f"`{tool} sql -q \"SELECT ts, cwd, text FROM prompt_history WHERE ...\"` — everything the "
            "owner ever typed (the primary source); "
            f"`{tool} trajectory-search -q \"...\" [--after YYYY-MM-DD]` and "
            f"`{tool} trajectory-read --source-ref REF [--around-event N]` — the full sessions, "
            "agent replies included. Agent output is long and full of terms the agents coined: "
            "open it only to check a specific fact, never as the source of what the owner cares "
            "about. " + web
        )

    _AUDIENCE = (
        "The owner already has a separate tool that watches each session and reports job "
        "status and progress. This briefing must NOT recap sessions, runs, metrics or progress. "
        "It exists for what that misses: ideas worth thinking about, and things that slipped "
        "through the cracks across days and projects.\n\n"
        "Write for the owner, not for an agent: plain, well-written English. Use the owner's "
        "own words for their projects (see how they talk in their prompts). Do not use terms "
        "the agents invented (internal run names, stage labels, metric nicknames) unless the "
        "owner uses them too; if a technical term is needed, explain it in a phrase."
    )

    def scout(self, evidence: dict[str, Any]) -> dict[str, Any]:
        system = (
            "You read a researcher's own messages to their AI agents and figure out what is on "
            "their mind. You are the scout for a morning 'pulse' written for them.\n\n"
            + self._AUDIENCE + "\n\nOwner: " + (evidence.get("owner") or "") + "\n\n" + self._tool_section()
        )
        message = (
            self._voice_digest(evidence)
            + "\n\nFrom the owner's OWN words above (use the tools only to clarify), produce JSON "
            "with two lists:\n"
            '"article_topics": 3–5 candidates for a short inspiring article. Each: {"topic": '
            '"...", "why_them": the specific things they said that make this relevant (quote '
            'briefly), "angle": the idea or connection worth exploring — e.g. a method from '
            "another field that fits a problem they keep hitting, a recent paper that changes "
            "how to think about something they asked, a question they raised but never "
            'answered}. Favor ideas, not tasks. Avoid topics already covered recently.\n'
            '"overlooked_candidates": up to 12 things they may have let slip. Each: {"item": '
            'plain one-line description in their words, "evidence": what they said and when '
            '(source_ref), "kind": one of asked-but-never-followed-up | promised-someone | '
            "idea-floated-and-dropped | started-then-abandoned | cross-project-connection | "
            'deadline}. Only things they would plausibly NOT notice by glancing at their '
            "active sessions.\n"
            'Also "vocabulary": 10–25 terms/phrases the owner actually uses for their work, and '
            '"agent_jargon_to_avoid": terms that appear only in agent output.\n'
            "Output ONLY the JSON object."
        )
        text = self._agent(label="scout", system=system, message=message,
                           effort=self.analyst_effort, tools=True, web=False)
        match = re.search(r"\{.*\}", text, re.S)
        try:
            data = json.loads(match.group(0)) if match else {}
        except json.JSONDecodeError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _style_note(self, scout: dict[str, Any]) -> str:
        vocab = ", ".join(str(v) for v in (scout.get("vocabulary") or [])[:25])
        avoid = ", ".join(str(v) for v in (scout.get("agent_jargon_to_avoid") or [])[:25])
        return (
            (f"The owner's own vocabulary: {vocab}.\n" if vocab else "")
            + (f"Agent-coined terms to avoid (or explain): {avoid}.\n" if avoid else "")
        )

    def write_articles(self, evidence: dict[str, Any], scout: dict[str, Any]) -> list[dict[str, Any]]:
        topics = [t for t in scout.get("article_topics") or [] if isinstance(t, dict)][:3]
        if not topics:
            return []
        system = (
            "You write a short, genuinely interesting article for one specific researcher — "
            "like a great science writer who knows exactly what they are working on. You do "
            "real research first (read sources, not just abstracts), then write.\n\n"
            + self._AUDIENCE + "\n" + self._style_note(scout)
            + "\nOwner: " + (evidence.get("owner") or "") + "\n\n" + self._tool_section()
        )

        def one(topic: dict[str, Any]) -> dict[str, Any]:
            message = (
                f"Candidate topic: {json.dumps(topic, ensure_ascii=False)}\n\n"
                "Research it properly (web: papers, docs, talks; their own prompts for what "
                "they actually said). If, after researching, the idea turns out weak, obvious "
                "to them, or not actually connected to their work, reply only with `SKIP: "
                "<reason>`.\n\nOtherwise write the article (350–600 words): a title; open with "
                "the connection to something they said (paraphrase, say roughly when); explain "
                "the idea clearly from first principles; give one or two concrete examples; end "
                "with what they could try or think about, in a sentence or two. Cite sources "
                "inline as links. No bullet-point dumps — prose."
            )
            text = self._agent(label=f"article:{str(topic.get('topic'))[:40]}", system=system,
                               message=message, effort=self.chief_effort, tools=True, web=True)
            return {"topic": topic.get("topic"), "text": text}

        with ThreadPoolExecutor(max_workers=max(1, self.parallel)) as pool:
            results = list(pool.map(one, topics))
        return [r for r in results if r["text"] and not r["text"].lstrip().upper().startswith("SKIP")]

    def check_overlooked(self, evidence: dict[str, Any], scout: dict[str, Any]) -> str:
        candidates = [c for c in scout.get("overlooked_candidates") or [] if isinstance(c, dict)]
        if not candidates:
            return ""
        system = (
            "You check whether things a researcher mentioned really slipped through the cracks. "
            "Be skeptical: most candidates were handled somewhere later.\n\n"
            + self._AUDIENCE + "\n" + self._style_note(scout) + "\n\n" + self._tool_section()
        )
        message = (
            "Candidates:\n" + json.dumps(candidates, ensure_ascii=False, indent=1)
            + "\n\nFor each, look for evidence it was later done, answered, or deliberately "
            "dropped (search their later prompts first, then sessions). Keep only the ones that "
            "still look open and that they would plausibly not notice on their own. For each "
            "keeper write ONE plain line in their words (what, why it may matter), plus a short "
            "pointer they can follow up with (a session ref or the day they mentioned it). No "
            "details beyond that — they will follow up themselves. Output the keepers as a "
            "markdown list, at most 7, most important first."
        )
        return self._agent(label="overlooked", system=system, message=message,
                           effort=self.analyst_effort, tools=True, web=False)

    def edit(self, evidence: dict[str, Any], articles: list[dict[str, Any]], overlooked: str) -> str:
        system = (
            "You are the editor of a one-reader morning pulse. Taste matters: cut anything "
            "generic, anything that recaps their sessions, and any jargon they would not use.\n\n"
            + self._AUDIENCE + "\nOwner: " + (evidence.get("owner") or "")
        )
        previous = "\n\n".join(f"### {p['date']}\n{p['briefing'][:1500]}" for p in evidence["previous"][:2]) or "(none)"
        message = (
            "Articles (pick the best one or two; if none is genuinely good, use none):\n\n"
            + "\n\n---\n\n".join(f"## {a['topic']}\n{a['text']}" for a in articles)
            + "\n\nOverlooked items (already checked):\n" + (overlooked or "(none)")
            + "\n\nRecent briefings, to avoid repeating:\n" + previous
            + "\n\nAssemble today's pulse in EXACTLY this markdown shape (the app parses it):\n"
            "1. One short opening line (no greeting fluff), no heading.\n"
            "2. A line `## You may have overlooked`, then the items as `- ` bullets (at most 7 "
            "one-liners, each with its pointer).\n"
            "3. Each article under its own `## <article title>` heading, lightly edited for "
            "clarity and the owner's vocabulary.\n"
            "Then a fenced ```json block: {\"topics\": [article topics used], \"overlooked\": "
            "[{\"item\": ..., \"pointer\": ...}]} so tomorrow can avoid repeats and follow up."
        )
        return self._agent(label="editor", system=system, message=message,
                           effort=self.verify_effort, tools=False, web=False)

    def verify(self, evidence: dict[str, Any], draft: str) -> str:
        system = (
            "You are the last check before a morning pulse reaches its reader.\n\n"
            + self._AUDIENCE + "\n\n" + self._tool_section()
        )
        message = (
            "Draft:\n\n" + draft
            + "\n\nCheck: (1) every factual claim about the owner's own work is supported — "
            "verify the overlooked items' pointers quickly with the tools; fix or drop what is "
            "wrong; (2) every external claim matches its cited source; (3) no session-status "
            "recap slipped in; (4) no unexplained agent jargon — rewrite in plain words. Keep "
            "the voice, length and markdown shape (`## You may have overlooked` with `- ` bullets, "
            "then one `## <title>` per article). Output ONLY the final pulse followed by the "
            "```json block."
        )
        text = self._agent(label="verifier", system=system, message=message,
                           effort=self.verify_effort, tools=True, web=True)
        return text or draft

    # ------------------------------------------------------------------ replay

    def replay(self, source_date: str, model: str, tag: str = "") -> dict[str, Any]:
        """Re-run the pulse pipeline on a stored evidence snapshot with a given
        model, for side-by-side comparison. Nothing is delivered; output goes
        to briefings/compare/. (Fact checks still read the current index.)"""
        data = json.loads((self.dir / f"{source_date}{self.suffix}.json").read_text())
        evidence = dict(data["evidence"])
        evidence["previous"] = []
        evidence.setdefault("owner", self._owner_line())
        runner = BriefingRunner(self.state, self.engine)
        runner.model = model
        started = time.time()
        scout = runner.scout(evidence)
        with ThreadPoolExecutor(max_workers=2) as pool:
            articles_f = pool.submit(runner.write_articles, evidence, scout)
            overlooked_f = pool.submit(runner.check_overlooked, evidence, scout)
            articles, overlooked = articles_f.result(), overlooked_f.result()
        draft = runner.edit(evidence, articles, overlooked)
        text, carry = self._split_carry(runner.verify(evidence, draft))
        out_dir = self.dir / "compare"
        out_dir.mkdir(exist_ok=True)
        stem = f"{source_date}{self.suffix}.{tag or model}"
        (out_dir / f"{stem}.md").write_text(
            f"# Replay of {source_date} with {model}\n\n" + text + "\n"
        )
        (out_dir / f"{stem}.json").write_text(json.dumps({
            "source_date": source_date, "model": model, "scout": scout, "articles": articles,
            "overlooked": overlooked, "draft": draft, "briefing": text, "carry": carry,
            "seconds": round(time.time() - started, 1),
        }, indent=1, default=str))
        log.info("replay of %s with %s done in %.0fs", source_date, model, time.time() - started)
        return {"ok": True, "path": str(out_dir / f"{stem}.md")}

    # ------------------------------------------------------- app + follow-ups

    @staticmethod
    def structure(text: str) -> list[dict[str, Any]]:
        """Split the verified pulse into app sections WITHOUT rewording it:
        an intro, the overlooked list (one entry per item), and articles.
        Any heading level starts a new section (models vary between # and
        ##); the overlooked list also ends at a horizontal rule, so bullets
        inside articles never become overlooked items."""
        sections: list[dict[str, Any]] = []
        blocks: list[tuple[str, list[str]]] = [("", [])]
        for line in text.splitlines():
            heading = re.match(r"^\s{0,3}#{1,4}\s+(.*\S)\s*$", line)
            if heading:
                blocks.append((heading.group(1).strip("* ").strip(), []))
            elif re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", line):
                blocks.append(("", []))
            else:
                blocks[-1][1].append(line)
        for title, lines in blocks:
            body = "\n".join(lines).strip()
            if not title and not body:
                continue
            if "overlook" in title.lower():
                items: list[str] = []
                notes: list[str] = []
                for line in lines:
                    if re.match(r"^\s*[-*]\s+", line):
                        items.append(re.sub(r"^\s*[-*]\s+", "", line).strip())
                    elif items and line.startswith(("  ", "\t")) and line.strip():
                        items[-1] += " " + line.strip()
                    elif line.strip():
                        notes.append(line.strip())
                for index, item in enumerate(items, start=1):
                    sections.append({"kind": "overlooked", "id": f"o{index}", "title": "", "body": item})
                if notes:
                    sections.append({"kind": "note", "title": "", "body": "\n".join(notes)})
            elif title:
                index = sum(1 for sec in sections if sec["kind"] == "article") + 1
                sections.append({"kind": "article", "id": f"a{index}", "title": title, "body": body})
            elif not sections:
                sections.append({"kind": "intro", "title": "", "body": body.strip("*").strip()})
            else:
                sections.append({"kind": "note", "title": "", "body": body})
        return sections

    def make_actions(self, evidence: dict[str, Any], sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        targets = [sec for sec in sections if sec["kind"] in {"overlooked", "article"}]
        if not targets:
            return []
        system = (
            "You turn items from a researcher's morning pulse into tasks for a capable coding "
            "agent (Claude Code / Codex with shell access and a `mybot` command that searches "
            "the researcher's past sessions). Output ONLY a JSON array."
        )
        listing = "\n\n".join(
            f"[{sec['id']}] ({sec['kind']}) {sec.get('title') or ''}\n{sec['body'][:3000]}" for sec in targets
        )
        message = (
            listing
            + "\n\nFor each item write a follow-up task the owner can launch with one tap. "
            'Each element: {"id": item id, "label": 2–5 word button label, "source_ref": the '
            'main session ref the item points to (e.g. "claude:2099f0a8-…" — copy the full id '
            'if shown, else ""), "prompt": the task}. The prompt must be self-contained (the '
            "agent has not seen the pulse): restate the item in plain words, say how to get "
            "context (`mybot trajectory-read --source-ref REF`, `mybot trajectory-search -q ...`), "
            "and say what to deliver. Overlooked items: find out whether it is really still "
            "open, and if so prepare the next step (a draft message, a command, a short plan). "
            "Articles: assess how the idea applies to the owner's actual code/models and propose "
            "a concrete small experiment. Always: report findings briefly in plain language; do "
            "NOT take irreversible or outward-facing actions (deleting data, messaging people, "
            "launching large jobs, pushing code) without asking first."
        )
        text = self._agent(label="actions", system=system, message=message,
                           effort=self.verify_effort, tools=False, web=False)
        match = re.search(r"\[.*\]", text, re.S)
        try:
            raw = json.loads(match.group(0)) if match else []
        except json.JSONDecodeError:
            raw = []
        by_id = {str(item.get("id")): item for item in raw if isinstance(item, dict)}
        actions = []
        for sec in targets:
            item = by_id.get(sec["id"])
            if not item or not str(item.get("prompt") or "").strip():
                continue
            ref = str(item.get("source_ref") or "")
            actions.append({
                "id": sec["id"],
                "kind": sec["kind"],
                "label": str(item.get("label") or ("Follow up" if sec["kind"] == "overlooked" else "Explore"))[:40],
                "prompt": str(item["prompt"]),
                "source_ref": ref,
                "cwd": self._cwd_for(ref),
                "spawned": None,
            })
        return actions

    def _cwd_for(self, source_ref: str) -> str:
        fallback = str(Path.home() / "Code")
        if not source_ref or ":" not in source_ref:
            return fallback
        db = self.state.config.trajectory_index_db_path
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10) as conn:
                like = source_ref.rstrip("…").rstrip(".") + "%"
                row = conn.execute(
                    "SELECT cwd FROM trajectory_chunks WHERE source_ref LIKE ? LIMIT 1", (like,)
                ).fetchone()
        except sqlite3.Error:
            row = None
        cwd = str(row[0]) if row and row[0] else ""
        # Agent mirror/scratch dirs are not where follow-up work belongs.
        if not cwd or "/.nebula" in cwd or "/.claude/" in cwd or not os.path.isdir(cwd):
            return fallback
        return cwd

    def latest(self) -> dict[str, Any] | None:
        paths = sorted(self.dir.glob(f"????-??-??{self.suffix}.json"))
        if not paths:
            return None
        data = json.loads(paths[-1].read_text())
        sections = data.get("sections") or self.structure(str(data.get("briefing") or ""))
        return {
            "engine": self.engine,
            "label": ENGINE_LABELS.get(self.engine, self.engine),
            "date": data.get("date"),
            "title": self._title(str(data.get("date"))),
            "sections": sections,
            "actions": data.get("actions") or [],
        }

    def follow_up(self, date: str, action_id: str) -> dict[str, Any]:
        """Launch the item's follow-up as an agent in hop (the owner watches
        and steers it there). Returns immediately; the agent works on."""
        path = self.dir / f"{date}{self.suffix}.json"
        data = json.loads(path.read_text())
        action = next((a for a in data.get("actions") or [] if a.get("id") == action_id), None)
        if action is None:
            raise ValueError(f"no action {action_id} for {date}")
        if action.get("spawned"):
            return {"ok": True, "already": True, **action["spawned"]}
        agent = "codex" if self.engine == "gpt" else "claude"
        name = f"pulse-{date[5:].replace('-', '')}-{self.engine}-{action_id}"
        spawn = self._hopa(["tool", "hopx_spawn_agent", json.dumps({
            "name": name, "agent": agent, "cwd": action.get("cwd") or str(Path.home() / "Code"),
        })], timeout=180)
        if not spawn.get("ok"):
            raise RuntimeError(f"hop spawn failed: {spawn}")
        terminal = str(spawn.get("terminal_id") or name)
        prompt = (
            f"(Follow-up launched from mybot's morning pulse, {date}.)\n\n" + str(action["prompt"])
        )
        sent = self._hopa(["tool", "hopx_send_and_wait", json.dumps({
            "terminal_id": terminal, "data": prompt, "press_enter": True, "wait": False,
        })], timeout=60)
        if not sent.get("ok"):
            raise RuntimeError(f"hop send failed: {sent}")
        action["spawned"] = {
            "terminal": name, "terminal_id": terminal, "session": spawn.get("sessionName"),
            "at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(data, indent=1, default=str))
        log.info("pulse follow-up %s/%s spawned in hop as %s", date, action_id, name)
        return {"ok": True, **action["spawned"]}

    @staticmethod
    def _hopa(args: list[str], *, timeout: int) -> dict[str, Any]:
        import subprocess

        hopa = os.environ.get("HOPA_COMMAND") or "hopa"
        proc = subprocess.run(
            [hopa, *args, "--cli-timeout", str(timeout * 1000)],
            capture_output=True, text=True, timeout=timeout + 15,
        )
        try:
            return json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "error": (proc.stdout or proc.stderr)[:500]}

    # ----------------------------------------------------------------- outputs

    @staticmethod
    def _split_carry(text: str) -> tuple[str, Any]:
        match = None
        for match in re.finditer(r"```json\s*(.*?)```", text, re.S):
            pass
        if not match:
            return text.strip(), {}
        try:
            carry = json.loads(match.group(1))
        except json.JSONDecodeError:
            carry = {}
        body = (text[: match.start()] + text[match.end():]).strip()
        return body, carry if isinstance(carry, (dict, list)) else {}

    def _quiet_day_note(self, evidence: dict[str, Any]) -> str:
        return "**Quiet stretch** — nothing new from you since the last pulse.\n\n```json\n{}\n```"

    def _write(self, day: str, text: str, record: dict[str, Any]) -> None:
        header = f"# {self._title(day, long=True)}\n\n"
        (self.dir / f"{day}{self.suffix}.md").write_text(header + text + "\n")

    def _title(self, day: str, *, long: bool = False) -> str:
        when = datetime.fromisoformat(day).strftime("%A %b %-d" if long else "%a %b %-d")
        label = "" if self.engine == "claude" else f" ({ENGINE_LABELS.get(self.engine, self.engine)})"
        return f"Morning pulse{label} — {when}"

    def deliver(self, day: str, text: str) -> dict[str, Any]:
        owner = self.state.config.imported_owner_actor_id
        title = self._title(day)
        sessions = self.state.sessions
        delivered: dict[str, Any] = {}
        # A menu-app thread: shows up in Ask history; follow-ups continue in place.
        thread = f"{THREAD_PREFIX}{day.replace('-', '')}{self.suffix.replace('.', '-')}"
        sessions.append_message(thread, owner, "user", title, {"kind": "briefing"})
        sessions.append_message(thread, owner, "assistant", text, {"kind": "briefing"})
        delivered["thread"] = thread
        if self.discord_dm and owner.isdigit():
            # Also seed the owner's DM conversation so a reply there has the
            # briefing as context; the bridge picks the text up from the outbox.
            try:
                active, _ = sessions.resolve_active_session(f"discord-dm-user-{owner}", idle_seconds=None)
                sessions.append_message(active, owner, "assistant", f"**{title}**\n\n{text}", {"kind": "briefing"})
            except Exception as exc:  # pragma: no cover
                log.warning("could not seed DM session: %s", exc)
            with self.outbox_path.open("a") as handle:
                handle.write(json.dumps({
                    "id": f"briefing-{day}{self.suffix}",
                    "platform": "discord",
                    "target_user_id": owner,
                    "text": f"**{title}**\n\n{text}",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }) + "\n")
            delivered["discord"] = "queued"
        return delivered

    # ------------------------------------------------------------------ outbox

    def outbox_pending(self, platform: str) -> list[dict[str, Any]]:
        acked = self._acked_ids()
        out = []
        if not self.outbox_path.exists():
            return out
        for line in self.outbox_path.read_text().splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("platform") == platform and item.get("id") not in acked:
                out.append(item)
        return out

    def outbox_ack(self, item_id: str) -> None:
        with (self.outbox_path.with_suffix(".acked")).open("a") as handle:
            handle.write(item_id + "\n")

    def _acked_ids(self) -> set[str]:
        path = self.outbox_path.with_suffix(".acked")
        if not path.exists():
            return set()
        return {line.strip() for line in path.read_text().splitlines() if line.strip()}


# ------------------------------------------------------------- hop (phone)

# The hop daemon serves files under /assets/ to its logged-in clients; the
# hop iOS app already reads digest.json from there with its session cookie.
# Write pulse.json beside it (to every candidate root, as digest.mjs does:
# the daemon rebuilds the dev dist and wipes it) so the phone shows the pulse
# natively with no new endpoint and no mybot port exposed.
HOP_ASSET_ROOTS = [
    Path.home() / "Code/hop2/hay/apps/web/dist/assets",
    Path.home() / "Code/hop2/hay-web/assets",
]
PULSE_TASK_DIR = Path.home() / ".mybot" / "pulse-tasks"


def _launch_command(engine: str, task_path: Path) -> str:
    # The phone creates a hop session in the item's folder and types this
    # one line. The task itself lives in a file, so nothing long or quoted
    # crosses the terminal. `command` bypasses shell wrappers/functions.
    if engine == "gpt":
        return f"command codex --dangerously-bypass-approvals-and-sandbox \"$(cat '{task_path}')\""
    return f"command claude --permission-mode bypassPermissions \"$(cat '{task_path}')\""


def publish_for_hop(runners: dict[str, "BriefingRunner"]) -> dict[str, Any]:
    roots = [root for root in HOP_ASSET_ROOTS if root.is_dir()]
    if not roots:
        return {"ok": False, "error": "no hop asset directory found"}
    PULSE_TASK_DIR.mkdir(parents=True, exist_ok=True)
    pulses = []
    for engine, runner in runners.items():
        try:
            latest = runner.latest()
        except (OSError, ValueError):
            latest = None
        if not latest:
            continue
        actions = []
        for action in latest.get("actions") or []:
            stem = f"{latest['date']}-{engine}-{action['id']}"
            task_path = PULSE_TASK_DIR / f"{stem}.md"
            task_path.write_text(
                f"(Follow-up launched from mybot's morning pulse, {latest['date']}.)\n\n{action['prompt']}\n"
            )
            actions.append({
                "id": action["id"],
                "label": action.get("label") or "Follow up",
                "cwd": action.get("cwd") or str(Path.home() / "Code"),
                "session_name": f"pulse-{latest['date'][5:].replace('-', '')}-{engine}-{action['id']}",
                "command": _launch_command(engine, task_path),
            })
        pulses.append({
            "engine": engine,
            "label": latest.get("label") or engine,
            "date": latest.get("date"),
            "title": latest.get("title"),
            "sections": latest.get("sections") or [],
            "actions": actions,
        })
    payload = json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "pulses": pulses},
                         ensure_ascii=False)
    written = []
    for root in roots:
        target = root / "pulse.json"
        tmp = root / ".pulse.json.tmp"
        tmp.write_text(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        written.append(str(target))
    return {"ok": True, "pulses": len(pulses), "written": written}
