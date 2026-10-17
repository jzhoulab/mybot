from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.briefing import _resume_command, resumed_session_name, source_session


class PulseResumeTest(unittest.TestCase):
    """A pulse follow-up reopens the item's own conversation."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.work = root / "work"
        self.work.mkdir()
        claude = root / ".claude_alt" / "projects" / "-work" / "2099f0a8-e207-4aff-bfb8-f3d77701c825.jsonl"
        claude.parent.mkdir(parents=True)
        claude.write_text(json.dumps({"type": "user", "cwd": str(self.work)}) + "\n")
        codex = root / ".codex2" / "sessions" / "2026" / "10" / "16" / "rollout-x.jsonl"
        codex.parent.mkdir(parents=True)
        codex.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(self.work)}}) + "\n")
        self.db = root / "index.sqlite3"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE trajectory_chunks (source_name, session_id, file_path, cwd, updated_at)")
            conn.execute("INSERT INTO trajectory_chunks VALUES ('claude', '2099f0a8-e207-4aff-bfb8-f3d77701c825', ?, '/elsewhere', '1')", (str(claude),))
            conn.execute("INSERT INTO trajectory_chunks VALUES ('codex', '01a0751d-d5f7-7dc2-b17b-d1480f5eea11', ?, '', '1')", (str(codex),))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_claude_ref_resolves_with_its_home_and_cwd(self) -> None:
        s = source_session(self.db, "claude:2099f0a8…")
        self.assertEqual(s["session_id"], "2099f0a8-e207-4aff-bfb8-f3d77701c825")
        self.assertEqual(s["cwd"], str(self.work))   # from the transcript, not the chunk
        self.assertTrue(s["env"]["CLAUDE_CONFIG_DIR"].endswith(".claude_alt"))
        cmd = _resume_command(s, '"$(cat t.md)"')
        self.assertIn("command claude --resume 2099f0a8-e207-4aff-bfb8-f3d77701c825", cmd)
        self.assertTrue(cmd.startswith("CLAUDE_CONFIG_DIR="))
        self.assertEqual(resumed_session_name(s), "resume-claude-2099f0a8")

    def test_codex_ref(self) -> None:
        s = source_session(self.db, "codex:01a0751d")
        self.assertTrue(s["env"]["CODEX_HOME"].endswith(".codex2"))
        self.assertIn("resume 01a0751d-d5f7-7dc2-b17b-d1480f5eea11", _resume_command(s))

    def test_unresolvable_refs(self) -> None:
        for ref in ("", "claude:2099", "claude:ffffffff", "gemini:2099f0a8", "claude:2099f0a8'; rm"):
            self.assertIsNone(source_session(self.db, ref), ref)


if __name__ == "__main__":
    unittest.main()
