from __future__ import annotations

import json
import os

from .common import append_turn, choose_title, make_detailed_summary, make_short_summary, parse_timestamp, recent_files
from .models import NormalizedTrajectory, TrajectorySourceAdapter


class ClaudeSourceAdapter(TrajectorySourceAdapter):
    def __init__(self, base_dir: str | None = None) -> None:
        super().__init__()
        self.base_dir = os.path.expanduser(base_dir or "~/.claude/projects")

    def source_name(self) -> str:
        return "claude"

    def discover_sessions(self, max_files: int = 200) -> list[NormalizedTrajectory]:
        patterns = [os.path.join(self.base_dir, "*", "*.jsonl")]
        files = recent_files(patterns, max_files)
        sessions: list[NormalizedTrajectory] = []

        for path in files:
            first_ts = None
            last_ts = None
            slug = ""
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
            updated_at = last_ts or first_ts
            if updated_at is None:
                continue
            session_id = os.path.basename(path).replace(".jsonl", "")
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
                    metadata={"tool": self.source_name()},
                )
            )

        self._sessions = sorted(sessions, key=lambda session: session.updated_at, reverse=True)
        return self._sessions
