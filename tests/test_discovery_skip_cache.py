"""Transcripts that discovery rejects are remembered, not re-parsed every
cycle, and a warm pass returns exactly what a cold pass does."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sources.common as common
from sources.claude import ClaudeSourceAdapter


def line(kind: str, text: str, ts: str, **extra) -> str:
    message = {"role": kind, "content": text if kind == "user" else [{"type": "text", "text": text}]}
    return json.dumps({"type": kind, "message": message, "timestamp": ts, "cwd": "/Users/x/proj",
                       "entrypoint": "cli", **extra})


class ClaudeSkipCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "-Users-x-proj"
        self.project.mkdir()
        common._DISCOVERY_CACHE.clear()
        self.addCleanup(common._DISCOVERY_CACHE.clear)
        self.addCleanup(self.tmp.cleanup)

    def write(self, name: str, lines: list[str], mtime: int) -> Path:
        path = self.project / f"{name}.jsonl"
        path.write_text("\n".join(lines) + "\n")
        os.utime(path, (mtime, mtime))
        return path

    def discover(self, max_files: int = 0) -> list[str]:
        adapter = ClaudeSourceAdapter(base_dir=self.tmp.name)
        return [s.session_id for s in adapter.discover_sessions(max_files=max_files)]

    def test_rejected_transcript_is_parsed_once_until_it_changes(self) -> None:
        self.write("real", [line("user", "plan the model", "2026-11-19T10:00:00Z"),
                            line("assistant", "ok", "2026-11-19T10:00:05Z")], 1_000)
        empty = self.write("no-typed-turns", [line("assistant", "hello", "2026-11-19T11:00:00Z")], 2_000)
        cold = self.discover()
        self.assertEqual(cold, ["real"])
        with mock.patch("sources.claude.iter_bounded_jsonl_lines", wraps=common.iter_bounded_jsonl_lines) as reads:
            self.assertEqual(self.discover(), cold)
        self.assertEqual(reads.call_count, 0)
        # A user turn arrives: the file changed, so it is parsed again and kept.
        empty.write_text(empty.read_text() + line("user", "now a question", "2026-11-19T11:01:00Z") + "\n")
        os.utime(empty, (3_000, 3_000))
        self.assertEqual(sorted(self.discover()), ["no-typed-turns", "real"])

    def test_cap_matches_a_cold_pass(self) -> None:
        # Newest first: a rejected file uses up a slot on a cold pass, so a
        # warm pass must not reach one file further back.
        self.write("newest-rejected", [line("assistant", "no user", "2026-11-19T12:00:00Z")], 3_000)
        self.write("middle", [line("user", "middle question", "2026-11-19T11:00:00Z")], 2_000)
        self.write("oldest", [line("user", "old question", "2026-11-19T10:00:00Z")], 1_000)
        cold = self.discover(max_files=2)
        self.assertEqual(cold, ["middle"])
        self.assertEqual(self.discover(max_files=2), cold)


if __name__ == "__main__":
    unittest.main()
