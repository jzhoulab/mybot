from __future__ import annotations

import json
import os

from .access import TrajectoryAccessAccount, peek_codex_session_meta
from .common import (
    append_turn,
    choose_title,
    iter_bounded_jsonl_lines,
    make_detailed_summary,
    make_short_summary,
    parse_timestamp,
    recent_files,
)
from .models import NormalizedTrajectory, TrajectorySourceAdapter
from .origin import classify_codex_origin, is_agent_worktree, parse_agent_marker


def load_codex_index(base_dir: str) -> dict[str, str]:
    index_map: dict[str, str] = {}
    index_path = os.path.join(base_dir, "session_index.jsonl")
    if not os.path.exists(index_path):
        return index_map
    with open(index_path) as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = entry.get("id")
            if session_id:
                index_map[session_id] = entry.get("thread_name", "")
    return index_map


class CodexSourceAdapter(TrajectorySourceAdapter):
    def __init__(
        self,
        base_dir: str | None = None,
        *,
        account: TrajectoryAccessAccount | None = None,
    ) -> None:
        account = account or TrajectoryAccessAccount(
            source_name="codex",
            name="default",
            base_dir=base_dir or "~/.codex",
        )
        if base_dir is not None:
            account.base_dir = base_dir
        super().__init__(account_name=account.name)
        self.account = account
        self.base_dir = account.expanded_base_dir

    def source_name(self) -> str:
        return "codex"

    def discover_sessions(self, max_files: int = 200) -> list[NormalizedTrajectory]:
        index_map = load_codex_index(self.base_dir)
        patterns = [
            os.path.join(self.base_dir, "sessions", "**", "*.jsonl"),
            os.path.join(self.base_dir, "archived_sessions", "*.jsonl"),
        ]
        # Fill the recency window with files that can actually be indexed.
        # Codex writes far more subagent/exec transcripts than human sessions,
        # so capping raw files starves genuine sessions out of the window (and
        # would gut the index on a rebuild). Reject those cheaply from
        # session_meta alone, then cap on what survives.
        excludes_exec = self.account.is_origin_excluded("automated", "exec")
        files: list[tuple[str, str | None]] = []
        for path in recent_files(patterns, 0):
            try:
                _, discovered_cwd, source_kind = peek_codex_session_meta(path)
            except OSError:
                continue
            if source_kind == "subagent":
                continue
            # `codex exec` is headless by definition; an agent-session marker
            # on such a run maps to another automated cluster, so skipping
            # before the full parse matches the policy outcome.
            if source_kind == "exec" and excludes_exec:
                continue
            if not self.account.include_workdir(discovered_cwd):
                continue
            files.append((path, discovered_cwd))
            if max_files > 0 and len(files) >= max_files:
                break

        by_session: dict[str, NormalizedTrajectory] = {}

        for path, discovered_cwd in files:

            first_ts = None
            last_ts = None
            session_id = ""
            cwd = discovered_cwd
            session_source = ""
            session_originator = ""
            agent_marker = None
            turns = []

            for line in iter_bounded_jsonl_lines(path):
                if line is not None:  # oversized lines are skipped as None
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = entry.get("payload", {})
                    etype = entry.get("type")
                    ts = parse_timestamp(entry.get("timestamp", ""))
                    ts_text = ts.isoformat() if ts else None
                    if ts:
                        first_ts = ts if first_ts is None else first_ts
                        last_ts = ts

                    if etype == "session_meta":
                        session_id = payload.get("id", session_id)
                        cwd = payload.get("cwd") or cwd
                        source = payload.get("source")
                        if isinstance(source, dict) and "subagent" in source:
                            session_id = ""
                            break
                        if isinstance(source, str):
                            session_source = source
                        session_originator = str(payload.get("originator") or session_originator)
                        continue

                    if etype == "turn_context":
                        cwd = payload.get("cwd") or cwd
                        continue

                    if etype == "response_item" and payload.get("role") == "assistant" and payload.get("type") == "message":
                        texts = []
                        for block in payload.get("content", []):
                            if isinstance(block, dict) and block.get("type") == "output_text":
                                texts.append(block.get("text", ""))
                        if texts:
                            append_turn(turns, "assistant", "\n".join(texts), ts_text)
                        continue

                    text = ""
                    if etype == "event_msg" and payload.get("type") == "user_message":
                        text = payload.get("message", "")
                    elif etype == "response_item" and payload.get("role") == "user" and payload.get("type") == "message":
                        for block in payload.get("content", []):
                            if isinstance(block, dict) and block.get("type") == "input_text":
                                text = block.get("text", "")
                                break
                    if text and agent_marker is None:
                        agent_marker = parse_agent_marker(text)
                    append_turn(turns, "user", text, ts_text)

            if not session_id:
                continue
            if not self.account.include_workdir(cwd):
                continue
            user_turns = [turn.text for turn in turns if turn.role == "user"]
            assistant_turns = [turn.text for turn in turns if turn.role == "assistant"]
            if not user_turns:
                continue
            title = choose_title(index_map.get(session_id, ""), user_turns, "codex session")
            updated_at = (last_ts or first_ts)
            if updated_at is None:
                continue
            raw_session_id = session_id
            session_id = self.account.scoped_session_id(raw_session_id)
            if not self.account.include_session(session_id, raw_session_id):
                continue
            origin = classify_codex_origin(session_source, session_originator, cwd=cwd,
                                           agent_marker=agent_marker)
            if origin == "interactive":
                origin_detail = ""
            elif agent_marker:
                origin_detail = agent_marker.get("origin") or "orchestrated"
            elif is_agent_worktree(cwd):
                origin_detail = "orchestrated"
            else:
                origin_detail = "exec"
            if self.account.is_origin_excluded(origin, origin_detail):
                continue
            session = NormalizedTrajectory(

                source_name=self.source_name(),
                session_id=session_id,
                title=title,
                file_path=path,
                updated_at=updated_at.isoformat(),
                turns=turns,
                short_summary=make_short_summary(title, user_turns, assistant_turns),
                detailed_summary=make_detailed_summary(title, user_turns, assistant_turns),
                metadata={
                    "tool": self.source_name(),
                    "account": self.account.name,
                    "raw_session_id": raw_session_id,
                    "cwd": cwd,
                    "origin": origin,
                    "origin_detail": origin_detail,
                    **({"visibility": "private"} if self.account.is_workdir_private(cwd) else {}),
                },
            )
            existing = by_session.get(session.session_id)
            if existing is None or session.updated_at > existing.updated_at:
                by_session[session.session_id] = session

        self._sessions = sorted(by_session.values(), key=lambda session: session.updated_at, reverse=True)
        return self._sessions
