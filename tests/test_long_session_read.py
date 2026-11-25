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


if __name__ == "__main__":
    unittest.main()
