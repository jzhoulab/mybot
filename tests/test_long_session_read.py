"""A long session is read to its end: nothing past the first 64 MB of a
transcript may silently drop out of the index or out of reads."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sources import trajectory_lookup as tl
from sources.common import MAX_JSONL_LINE_BYTES


def line(entry: dict) -> str:
    return json.dumps(entry) + "\n"


class LongClaudeSessionTest(unittest.TestCase):
    def test_turns_past_64_mb_are_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "long.jsonl"
            filler = line({"type": "progress", "data": "x" * (MAX_JSONL_LINE_BYTES + 1024)})
            with open(path, "w") as handle:
                handle.write(line({"type": "user", "timestamp": "2026-08-17T05:45:08Z",
                                   "message": {"content": "early question"}}))
                for _ in range(64 * 1024 * 1024 // len(filler) + 2):  # oversized lines: skipped, but counted
                    handle.write(filler)
                handle.write(line({"type": "user", "timestamp": "2026-11-24T15:24:19Z",
                                   "message": {"content": "the newest question"}}))
                handle.write(line({"type": "assistant", "timestamp": "2026-11-24T15:24:30Z",
                                   "message": {"content": [{"type": "text", "text": "the newest answer"}]}}))
            self.assertGreater(path.stat().st_size, 64 * 1024 * 1024)

            events = tl._read_claude_events(str(path))
            self.assertEqual([event.text for event in events],
                             ["early question", "the newest question", "the newest answer"])


class MovedSessionTest(unittest.TestCase):
    def test_a_session_moved_to_another_account_is_listed_once_newest_copy(self) -> None:
        from unittest.mock import patch

        from sources.models import NormalizedTrajectory

        def session(path: str, updated_at: str) -> NormalizedTrajectory:
            return NormalizedTrajectory(source_name="claude", session_id="s-1", file_path=path,
                                        title="t", updated_at=updated_at, metadata={}, turns=[],
                                        short_summary="", detailed_summary="")

        old_root = SimpleAdapter([session("/a/.claude/projects/p/s-1.jsonl", "2026-11-24T15:24:19+00:00")])
        new_root = SimpleAdapter([session("/a/.claude_fable/projects/p/s-1.jsonl", "2026-11-26T00:49:03+00:00")])
        lookup = tl.TrajectoryLookup(source_names=["claude"], max_files_per_tool=10)
        with patch.object(tl, "get_source_adapters", return_value=[new_root, old_root]):
            listed = lookup.sessions()
        self.assertEqual([s.file_path for s in listed], ["/a/.claude_fable/projects/p/s-1.jsonl"])


class SimpleAdapter:
    def __init__(self, sessions: list) -> None:
        self._sessions = sessions

    def discover_sessions(self, max_files: int = 0) -> list:
        return list(self._sessions)


if __name__ == "__main__":
    unittest.main()
