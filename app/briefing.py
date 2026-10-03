"""Daily chief-of-staff briefing.

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


class BriefingRunner:
    def __init__(self, state: "AppState") -> None:
        self.state = state
        self.enabled = _env_bool("BRIEFING_ENABLED", True)
        self.at = _env("BRIEFING_TIME", "08:00")
        self.analyst_effort = _env("BRIEFING_ANALYST_EFFORT", "xhigh")
        self.chief_effort = _env("BRIEFING_CHIEF_EFFORT", "max")
        self.verify_effort = _env("BRIEFING_VERIFY_EFFORT", "high")
        self.planner_effort = _env("BRIEFING_PLANNER_EFFORT", "medium")
        self.model = _env("BRIEFING_MODEL", "")
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
        if (self.dir / f"{now.date().isoformat()}.md").exists():
            return False
        failed = self.dir / f"{now.date().isoformat()}.failed"
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
            record["evidence"] = evidence
            if not evidence["sessions"] and not evidence["prompts"] and not force:
                text = self._quiet_day_note(evidence)
                record["stages"] = {"skipped": "no activity in window"}
            else:
                self.status["stage"] = "plan"
                workstreams = self.plan(evidence)
                record["workstreams"] = workstreams
                self.status["stage"] = f"analysts (0/{len(workstreams)})"
                reports = self.analyze(evidence, workstreams)
                record["analyst_reports"] = reports
                self.status["stage"] = "chief of staff"
                draft = self.chief_of_staff(evidence, workstreams, reports)
                record["draft"] = draft
                self.status["stage"] = "verify"
                text = self.verify(evidence, reports, draft)
            text, open_loops = self._split_open_loops(text)
            record["open_loops"] = open_loops
            record["briefing"] = text
            record["seconds"] = round(time.time() - started, 1)
            self._write(day, text, record)
            self.status["stage"] = "deliver"
            record["delivered"] = self.deliver(day, text)
            (self.dir / f"{day}.json").write_text(json.dumps(record, indent=1, default=str))
            (self.dir / f"{day}.failed").unlink(missing_ok=True)
            self.status["last"] = {"date": day, "ok": True, "seconds": record["seconds"]}
            log.info("daily briefing %s written in %.0fs", day, record["seconds"])
            return {"ok": True, "date": day, "seconds": record["seconds"], "path": str(self.dir / f"{day}.md")}
        except Exception as exc:
            (self.dir / f"{day}.failed").write_text(traceback.format_exc())
            self.status["last"] = {"date": day, "ok": False, "error": str(exc)}
            log.warning("daily briefing failed: %s", exc)
            return {"ok": False, "error": str(exc)}
        finally:
            self.status.update(running=False, stage="")
            self._lock.release()

    # ------------------------------------------------------------------ gather

    def _previous(self, limit: int = 3) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.dir.glob("*.json"), reverse=True)[:limit]:
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            out.append(
                {
                    "date": data.get("date"),
                    "started_at": data.get("started_at"),
                    "briefing": str(data.get("briefing") or "")[:6000],
                    "open_loops": data.get("open_loops") or [],
                }
            )
        return out

    def gather(self) -> dict[str, Any]:
        previous = self._previous()
        now = datetime.now(timezone.utc)
        if previous and previous[0].get("started_at"):
            since = str(previous[0]["started_at"])
        else:
            since = (now - timedelta(hours=36)).isoformat()
        # Never reach further back than 4 days, even after a long gap.
        floor = (now - timedelta(days=4)).isoformat()
        since = max(since, floor)
        week = (now - timedelta(days=7)).isoformat()
        db = self.state.config.trajectory_index_db_path
        prompts: list[dict[str, Any]] = []
        week_projects: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute(
                "SELECT source_name, session_id, ts, cwd, text FROM prompt_history "
                "WHERE ts >= ? ORDER BY ts",
                (since,),
            ):
                text = str(row["text"] or "").strip()
                if len(text) < 3:
                    continue
                prompts.append(
                    {
                        "source_ref": f"{row['source_name']}:{row['session_id']}",
                        "ts": row["ts"],
                        "cwd": row["cwd"],
                        "text": text[:600],
                    }
                )
            for row in conn.execute(
                "SELECT cwd, COUNT(*) AS prompts, COUNT(DISTINCT session_id) AS sessions, MAX(ts) AS last "
                "FROM prompt_history WHERE ts >= ? GROUP BY cwd ORDER BY prompts DESC LIMIT 25",
                (week,),
            ):
                week_projects.append(dict(row))
            for row in conn.execute(
                "SELECT source_ref, MAX(source_name) AS source_name, MAX(updated_at) AS updated_at, COUNT(*) AS chunks "
                "FROM trajectory_chunks GROUP BY source_ref HAVING MAX(updated_at) >= ? "
                "ORDER BY updated_at DESC LIMIT 60",
                (since,),
            ):
                detail = conn.execute(
                    "SELECT title, cwd FROM trajectory_chunks WHERE source_ref = ? LIMIT 1",
                    (row["source_ref"],),
                ).fetchone()
                new_chunks = conn.execute(
                    "SELECT COUNT(*) FROM trajectory_chunks WHERE source_ref = ? AND updated_at >= ?",
                    (row["source_ref"], since),
                ).fetchone()[0]
                sessions.append(
                    {
                        "source_ref": row["source_ref"],
                        "title": (detail["title"] if detail else "")[:120],
                        "cwd": detail["cwd"] if detail else "",
                        "updated_at": row["updated_at"],
                        "chunks": row["chunks"],
                        "new_chunks": new_chunks,
                    }
                )
        prompted_refs = {p["source_ref"] for p in prompts}
        # Human-driven work first: sessions the owner typed into, then the
        # most-changed others (agent runs they launched).
        sessions.sort(key=lambda s: (s["source_ref"] not in prompted_refs, -int(s["new_chunks"])))
        return {
            "window_start": since,
            "window_end": now.isoformat(),
            "prompts": prompts[-400:],
            "sessions": sessions[:40],
            "week_projects": week_projects,
            "previous": previous,
            "owner": self._owner_line(),
        }

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

    def _evidence_digest(self, evidence: dict[str, Any], *, max_prompts: int = 250) -> str:
        lines = [
            f"Window: {evidence['window_start']} → {evidence['window_end']} (UTC).",
            "",
            "## Sessions active in the window (human-typed first)",
        ]
        for s in evidence["sessions"]:
            lines.append(
                f"- {s['source_ref']} | {s['title']} | cwd={s['cwd']} | "
                f"last={_fmt_local(s['updated_at'])} | +{s['new_chunks']} chunks"
            )
        lines += ["", "## What the owner typed (chronological; local time)"]
        for p in evidence["prompts"][-max_prompts:]:
            lines.append(f"- [{_fmt_local(p['ts'])}] {p['source_ref']} ({p['cwd']}): {p['text'][:300]}")
        lines += ["", "## Last 7 days, by project (prompts / sessions / last)"]
        for w in evidence["week_projects"]:
            lines.append(f"- {w['cwd']}: {w['prompts']} prompts, {w['sessions']} sessions, last {_fmt_local(w['last'])}")
        if evidence["previous"]:
            lines += ["", "## Open loops handed forward by previous briefings"]
            for prev in evidence["previous"]:
                for loop in prev.get("open_loops") or []:
                    lines.append(f"- (from {prev['date']}) {json.dumps(loop, ensure_ascii=False)[:300]}")
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
            "You also have WebSearch and WebFetch for outside research (papers, docs, "
            "deadlines, tool releases, anything that sharpens a recommendation). Cite URLs."
            if self.web_research
            else "You have no web access."
        )
        return (
            "## Tools\n"
            f"`{tool} trajectory-search -q \"...\" [--after YYYY-MM-DD]`, "
            f"`{tool} trajectory-read --source-ref REF [--around-event N | --chunk-id C]`, "
            f"`{tool} sql -q \"SELECT ...\"` (tables trajectory_chunks, prompt_history), "
            f"`{tool} budget [--extend N --reason ...]`. "
            "These read the owner's own Claude Code / Codex sessions. Read the actual "
            "transcripts — titles and prompts are only pointers. " + web
        )

    def plan(self, evidence: dict[str, Any]) -> list[dict[str, Any]]:
        system = (
            "You organize a researcher's recent work into workstreams for a morning briefing. "
            "Output ONLY a JSON array, no prose."
        )
        message = (
            self._evidence_digest(evidence, max_prompts=150)
            + "\n\nGroup this activity into at most "
            f"{self.max_workstreams} workstreams that deserve a careful look (merge small related "
            "items; drop pure noise like tool tinkering that ended). For each: "
            '{"name": short name, "why": one line, "source_refs": [refs from the list], '
            '"questions": [2-4 specific things an analyst should establish — e.g. did the run '
            'finish, what did the owner promise to do next, what is blocked, any deadline]}. '
            "Include open loops from previous briefings in the most relevant workstream."
        )
        text = self._agent(label="planner", system=system, message=message,
                           effort=self.planner_effort, tools=False, web=False)
        match = re.search(r"\[.*\]", text, re.S)
        try:
            items = json.loads(match.group(0)) if match else []
        except json.JSONDecodeError:
            items = []
        items = [item for item in items if isinstance(item, dict) and item.get("name")]
        if not items:
            refs = [s["source_ref"] for s in evidence["sessions"][:8]]
            items = [{"name": "Recent work", "why": "fallback", "source_refs": refs, "questions": []}]
        return items[: self.max_workstreams]

    def analyze(self, evidence: dict[str, Any], workstreams: list[dict[str, Any]]) -> list[dict[str, Any]]:
        digest = self._evidence_digest(evidence, max_prompts=120)
        system = (
            "You are an analyst on a chief of staff's team, preparing one section of a "
            "researcher's morning briefing. Be rigorous: every claim must come from what you "
            "read in their sessions (cite the source_ref and, when useful, the event/chunk), or "
            "from a cited web source. Never guess at outcomes — if a job's result is not in the "
            "record, say it is unknown and what to check.\n\n" + self._tool_section()
        )
        done = [0]

        def one(ws: dict[str, Any]) -> dict[str, Any]:
            message = (
                f"# Workstream: {ws.get('name')}\nWhy it matters: {ws.get('why','')}\n"
                f"Sessions: {', '.join(ws.get('source_refs') or [])}\n"
                f"Questions to establish: {json.dumps(ws.get('questions') or [], ensure_ascii=False)}\n\n"
                "Context (all recent activity, for orientation):\n" + digest + "\n\n"
                "Dig into this workstream's sessions — read the last stretch of each, plus "
                "anything older the questions need. Then report, in markdown:\n"
                "1. **State** — where it stands now, concretely (numbers, file/job names).\n"
                "2. **Since last briefing** — what got done or decided.\n"
                "3. **Open loops** — things the owner said they would do, asked an agent to do, "
                "or left running/unverified; who/what is waiting on them; any dates.\n"
                "4. **Risks / blockers** — anything likely to bite today or this week.\n"
                "5. **Suggestions** — specific next steps worth taking, with reasoning; use web "
                "research where it adds something real (a relevant paper, a known bug/fix, a "
                "deadline). Skip generic advice.\n"
                "End with a line `EVIDENCE:` listing the source_refs (and URLs) you relied on."
            )
            text = self._agent(label=f"analyst:{ws.get('name')}", system=system, message=message,
                               effort=self.analyst_effort, tools=True, web=True)
            done[0] += 1
            self.status["stage"] = f"analysts ({done[0]}/{len(workstreams)})"
            return {"workstream": ws.get("name"), "report": text}

        with ThreadPoolExecutor(max_workers=max(1, self.parallel)) as pool:
            return list(pool.map(one, workstreams))

    def chief_of_staff(self, evidence: dict[str, Any], workstreams: list[dict[str, Any]],
                       reports: list[dict[str, Any]]) -> str:
        previous = "\n\n".join(
            f"### Briefing {p['date']}\n{p['briefing'][:2500]}" for p in evidence["previous"][:2]
        ) or "(none — this is the first briefing)"
        system = (
            "You are the owner's chief of staff: senior, discerning, proactive. You think hard "
            "about what actually matters for them today, not about summarizing activity. You "
            "verify before you assert, and you do your own extra research when a point deserves "
            "it.\n\nThe owner: " + (evidence.get("owner") or "a principal investigator running "
            "a computational genomics lab, who works through many Claude Code / Codex sessions.")
            + "\n\n" + self._tool_section()
        )
        message = (
            "Analyst reports for today's briefing:\n\n"
            + "\n\n".join(f"## {r['workstream']}\n{r['report']}" for r in reports)
            + "\n\n# Previous briefings (do not repeat what is unchanged; follow up on what was "
            "flagged)\n" + previous
            + "\n\n# Activity digest\n" + self._evidence_digest(evidence, max_prompts=80)
            + "\n\nWrite this morning's briefing. They will read it at 9–10am, likely on a phone. "
            "Structure:\n"
            "**Today, in one line** — the single most important thing.\n"
            "**Top priorities** (≤3) — what to do first and why now.\n"
            "**Don't forget** — commitments, promised follow-ups, things left running or "
            "unverified, people waiting on them; with dates where known.\n"
            "**Heads-up** — risks, blockers, anomalies worth a look.\n"
            "**Worth considering** — 1–3 sharper ideas: a better approach, a relevant new "
            "paper/tool, a connection across workstreams. Research these properly.\n"
            "Rules: every item cites its evidence compactly (session ref like `codex:019f…` "
            "or a URL). Be concrete (names, numbers, paths). Cut anything they obviously "
            "already know or that has no action. Aim for what a great human chief of staff "
            "would write in ~400–700 words.\n\n"
            "After the briefing, output a fenced ```json block: a list of open loops to carry "
            "forward to tomorrow, each {\"item\": ..., \"source_ref\": ..., \"due\": optional}."
        )
        return self._agent(label="chief-of-staff", system=system, message=message,
                           effort=self.chief_effort, tools=True, web=True)

    def verify(self, evidence: dict[str, Any], reports: list[dict[str, Any]], draft: str) -> str:
        system = (
            "You are the fact-checker for a chief of staff's morning briefing. Your job is to "
            "make sure nothing in it is wrong or unsupported.\n\n" + self._tool_section()
        )
        message = (
            "Draft briefing:\n\n" + draft
            + "\n\nAnalyst reports it was built from:\n\n"
            + "\n\n".join(f"## {r['workstream']}\n{r['report']}" for r in reports)
            + "\n\nCheck every factual claim in the draft that the reports do not clearly "
            "support — and spot-check the most important ones even if they do — by reading the "
            "cited sessions (or URLs). Fix wrong facts, soften unsupported ones (\"unclear "
            "whether…\"), remove items you cannot support at all. Keep the structure, voice and "
            "length. Output ONLY the corrected briefing followed by the (corrected) ```json "
            "open-loops block — no commentary about your checking."
        )
        text = self._agent(label="verifier", system=system, message=message,
                           effort=self.verify_effort, tools=True, web=True)
        return text or draft

    # ----------------------------------------------------------------- outputs

    @staticmethod
    def _split_open_loops(text: str) -> tuple[str, list[Any]]:
        match = None
        for match in re.finditer(r"```json\s*(.*?)```", text, re.S):
            pass
        if not match:
            return text.strip(), []
        try:
            loops = json.loads(match.group(1))
        except json.JSONDecodeError:
            loops = []
        body = (text[: match.start()] + text[match.end():]).strip()
        return body, loops if isinstance(loops, list) else []

    def _quiet_day_note(self, evidence: dict[str, Any]) -> str:
        carried = [loop for prev in evidence["previous"][:1] for loop in prev.get("open_loops") or []]
        lines = ["**Quiet stretch** — no session activity since the last briefing."]
        if carried:
            lines.append("\n**Still open from before:**")
            lines += [f"- {loop.get('item') if isinstance(loop, dict) else loop}" for loop in carried]
        return "\n".join(lines) + "\n\n```json\n" + json.dumps(carried) + "\n```"

    def _write(self, day: str, text: str, record: dict[str, Any]) -> None:
        header = f"# Morning briefing — {datetime.fromisoformat(day).strftime('%A %b %-d')}\n\n"
        (self.dir / f"{day}.md").write_text(header + text + "\n")

    def deliver(self, day: str, text: str) -> dict[str, Any]:
        owner = self.state.config.imported_owner_actor_id
        title = f"Morning briefing — {datetime.fromisoformat(day).strftime('%a %b %-d')}"
        sessions = self.state.sessions
        delivered: dict[str, Any] = {}
        # A menu-app thread: shows up in Ask history; follow-ups continue in place.
        thread = f"{THREAD_PREFIX}{day.replace('-', '')}"
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
                    "id": f"briefing-{day}",
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
