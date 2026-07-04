from __future__ import annotations

import json
import os

from .access import TrajectoryAccessAccount, discover_claude_metadata
from .common import append_turn, choose_title, make_detailed_summary, make_short_summary, parse_timestamp, recent_files
from .models import NormalizedTrajectory, TrajectorySourceAdapter


class ClaudeSourceAdapter(TrajectorySourceAdapter):
    def __init__(
        self,
        base_dir: str | None = None,
        *,
        account: TrajectoryAccessAccount | None = None,
    ) -> None:
        account = account or TrajectoryAccessAccount(
            source_name="claude",
            name="default",
            base_dir=base_dir or "~/.claude/projects",
        )
        if base_dir is not None:
            account.base_dir = base_dir
        super().__init__(account_name=account.name)
        self.account = account
        self.base_dir = account.expanded_base_dir

    def source_name(self) -> str:
        return "claude"

    def discover_sessions(self, max_files: int = 200) -> list[NormalizedTrajectory]:
        patterns = [os.path.join(self.base_dir, "*", "*.jsonl")]
        files = recent_files(patterns, max_files)
        sessions: list[NormalizedTrajectory] = []

        for path in files:
            _, discovered_cwd, discovered_entrypoints = discover_claude_metadata(path)
            if not self.account.include_workdir(discovered_cwd):
                continue
            if not self.account.include_entrypoints(discovered_entrypoints):
                continue

            first_ts = None
            last_ts = None
            slug = ""
            cwd = discovered_cwd
            entrypoints = list(discovered_entrypoints)
            turns = []

            with open(path) as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    etype = entry.get("type")
                    ts = parse_timestamp(entry.get("timestamp", ""))
                    ts_text = ts.isoformat() if ts else None
                    if ts:
                        first_ts = ts if first_ts is None else first_ts
                        last_ts = ts
                    slug = slug or entry.get("slug", "")
                    cwd = entry.get("cwd") or cwd
                    entrypoint = str(entry.get("entrypoint") or "").strip()
                    if entrypoint and entrypoint not in entrypoints:
                        entrypoints.append(entrypoint)

                    if etype == "assistant":
                        message = entry.get("message", {})
                        texts = []
                        for block in message.get("content", []) if isinstance(message, dict) else []:
                            if isinstance(block, dict) and block.get("type") == "text":
                                texts.append(block.get("text", ""))
                        if texts:
                            append_turn(turns, "assistant", "\n".join(texts), ts_text)
                        continue

                    if etype == "user":
                        message = entry.get("message", {})
                        content = message.get("content", "") if isinstance(message, dict) else ""
                        text = ""
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block.get("text", "")
                                    break
                                if isinstance(block, str):
                                    text = block
                                    break
                        append_turn(turns, "user", text, ts_text)

            user_turns = [turn.text for turn in turns if turn.role == "user"]
            assistant_turns = [turn.text for turn in turns if turn.role == "assistant"]
            if not user_turns:
                continue
            if not self.account.include_workdir(cwd):
                continue
            if not self.account.include_entrypoints(entrypoints):
                continue
            updated_at = last_ts or first_ts
            if updated_at is None:
                continue
            raw_session_id = os.path.basename(path).replace(".jsonl", "")
            session_id = self.account.scoped_session_id(raw_session_id)
            if not self.account.include_session(session_id, raw_session_id):
                continue
            title = choose_title(slug, user_turns, "claude session")
            sessions.append(
                NormalizedTrajectory(
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
                        "entrypoints": entrypoints,
                    },
                )
            )

        self._sessions = sorted(sessions, key=lambda session: session.updated_at, reverse=True)
        return self._sessions
