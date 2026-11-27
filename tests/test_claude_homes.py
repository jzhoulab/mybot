"""Claude CLI homes next to a configured one are indexed too: hop moves a live
conversation into another account's home when one hits its usage limit."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sources.access import load_access_config


class SiblingClaudeHomesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        for name in (".claude", ".claude_fable", ".claude_gpta", ".claude2"):
            (self.home / name / "projects").mkdir(parents=True)
        (self.home / ".claude-science").mkdir()  # no projects dir: not a CLI home with transcripts
        root = lambda name, base, **extra: {"name": name, "base_dir": str(self.home / base / "projects"), **extra}
        self.config = self.home / "access.json"
        self.config.write_text(json.dumps({"version": 1, "sources": {"claude": {"roots": [
            root("default", ".claude", excluded_workdirs=["/w/skip"], excluded_session_ids=["s-private"],
                 private_workdirs=["/w/home"], excluded_origin_details=["sdk"]),
            root("fable", ".claude_fable", scope_session_ids=False, excluded_workdirs=["/w/fable-only"]),
            root("off", ".claude2", enabled=False),
        ]}}}))

    def load(self):
        with patch.dict(os.environ, {"MYBOT_DISCOVER_CLAUDE_HOMES": "1"}):
            return load_access_config(self.config).sources["claude"]

    def test_an_unlisted_home_is_added_with_every_root_exclusion(self) -> None:
        roots = {Path(root.expanded_base_dir).parent.name: root for root in self.load()}
        self.assertEqual(sorted(roots), [".claude", ".claude2", ".claude_fable", ".claude_gpta"])
        gpta = roots[".claude_gpta"]
        self.assertEqual((gpta.name, gpta.enabled, gpta.scope_session_ids), ("gpta", True, False))
        self.assertEqual(gpta.excluded_workdirs, ["/w/skip", "/w/fable-only"])
        self.assertEqual((gpta.excluded_session_ids, gpta.private_workdirs, gpta.excluded_origin_details),
                         (["s-private"], ["/w/home"], ["sdk"]))
        self.assertFalse(roots[".claude2"].enabled)  # configured off stays off
        self.assertEqual(roots[".claude_fable"].excluded_workdirs, ["/w/fable-only"])  # configured stays as is

    def test_it_can_be_turned_off_and_ignores_homes_not_named_like_claude(self) -> None:
        with patch.dict(os.environ, {"MYBOT_DISCOVER_CLAUDE_HOMES": "0"}):
            self.assertEqual(len(load_access_config(self.config).sources["claude"]), 3)
        plain = self.home / "access-plain.json"
        plain.write_text(json.dumps({"version": 1, "sources": {"claude": {"roots": [
            {"name": "default", "base_dir": str(self.home / "fixtures" / "projects")}]}}}))
        with patch.dict(os.environ, {"MYBOT_DISCOVER_CLAUDE_HOMES": "1"}):
            self.assertEqual(len(load_access_config(plain).sources["claude"]), 1)


if __name__ == "__main__":
    unittest.main()
