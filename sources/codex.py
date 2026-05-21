from __future__ import annotations

import json
import os

from .common import append_turn, choose_title, make_detailed_summary, make_short_summary, parse_timestamp, recent_files
from .models import NormalizedTrajectory, TrajectorySourceAdapter


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
    def __init__(self, base_dir: str | None = None) -> None:
        super().__init__()
        self.base_dir = os.path.expanduser(base_dir or "~/.codex")

    def source_name(self) -> str:
        return "codex"

    def discover_sessions(self, max_files: int = 200) -> list[NormalizedTrajectory]:
        index_map = load_codex_index(self.base_dir)
        patterns = [
            os.path.join(self.base_dir, "sessions", "**", "*.jsonl"),
            os.path.join(self.base_dir, "archived_sessions", "*.jsonl"),
        ]
        files = recent_files(patterns, max_files)
        by_session: dict[str, NormalizedTrajectory] = {}

        for path in files:
            first_ts = None
            last_ts = None
            session_id = ""
            turns = []

            with open(path) as handle:
                for line in handle:
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
                        source = payload.get("source")
                        if isinstance(source, dict) and "subagent" in source:
                            session_id = ""
                            break
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
                    append_turn(turns, "user", text, ts_text)

            if not session_id:
                continue
            user_turns = [turn.text for turn in turns if turn.role == "user"]
            assistant_turns = [turn.text for turn in turns if turn.role == "assistant"]
            if not user_turns:
                continue
            title = choose_title(index_map.get(session_id, ""), user_turns, "codex session")
            updated_at = (last_ts or first_ts)
            if updated_at is None:
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
                metadata={"tool": self.source_name()},
            )
            existing = by_session.get(session.session_id)
            if existing is None or session.updated_at > existing.updated_at:
                by_session[session.session_id] = session

        self._sessions = sorted(by_session.values(), key=lambda session: session.updated_at, reverse=True)
        return self._sessions
