import json
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from app import briefing, server


class AntigravityPulseEngineTest(unittest.TestCase):
    """The Antigravity pulse engine runs `agy -p` and cleans up after it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        store = self.home / ".gemini" / "antigravity-cli"
        self.cid = "77841cc1-544d-4ed4-bb64-74ba106afaa8"
        for sub in ("conversations", "presence", "brain"):
            (store / sub).mkdir(parents=True)
        (store / "conversations" / f"{self.cid}.db").write_text("x")
        (store / "presence" / f"{self.cid}.lock").write_text("x")
        (store / "brain" / self.cid).mkdir()
        (store / "conversations" / "keep.db").write_text("x")
        self.store = store
        self.args = self.root / "args.json"
        self.fake = self.root / "agy"
        self.provider = types.SimpleNamespace(config=types.SimpleNamespace(codex_cwd=str(self.root / "work")))

    def tearDown(self):
        self.tmp.cleanup()

    def _fake(self, status="SUCCESS", code=0, response=" the pulse "):
        self.fake.write_text(
            "#!/usr/bin/env python3\nimport json, sys, os\n"
            f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'tok': os.environ.get('MYBOT_TOOL_TOKEN')}}, open({str(self.args)!r}, 'w'))\n"
            f"print(json.dumps({{'conversation_id': {self.cid!r}, 'status': {status!r}, 'response': {response!r}}}))\n"
            f"sys.exit({code})\n")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)

    def _call(self, **kw):
        cls = next(c for c in vars(server).values() if isinstance(c, type) and hasattr(c, "run_antigravity_task"))
        return cls.run_antigravity_task(self.provider, prompt="sys\n\n---\n\nask", **kw)

    def test_flags_text_and_cleanup(self):
        self._fake()
        with mock.patch.dict(os.environ, {"ANTIGRAVITY_COMMAND": str(self.fake)}), \
                mock.patch.object(server.Path, "home", lambda: self.home):
            out = self._call(tool_env={"MYBOT_TOOL_TOKEN": "agt_x"}, model="gemini-4-hydrogen",
                             effort="max", timeout=60)
        self.assertEqual(out["text"], "the pulse")
        seen = json.loads(self.args.read_text())
        argv = seen["argv"]
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--sandbox", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-4-hydrogen")
        self.assertEqual(argv[-1], "-p=sys\n\n---\n\nask")
        self.assertEqual(seen["tok"], "agt_x")
        self.assertEqual(os.path.realpath(seen["cwd"]), os.path.realpath(self.root / "work"))
        # Its own conversation is gone; others are untouched.
        self.assertFalse((self.store / "conversations" / f"{self.cid}.db").exists())
        self.assertFalse((self.store / "presence" / f"{self.cid}.lock").exists())
        self.assertFalse((self.store / "brain" / self.cid).exists())
        self.assertTrue((self.store / "conversations" / "keep.db").exists())

    def test_error_status_with_an_answer_keeps_the_answer(self):
        self._fake(status="ERROR")
        with mock.patch.dict(os.environ, {"ANTIGRAVITY_COMMAND": str(self.fake)}), \
                mock.patch.object(server.Path, "home", lambda: self.home):
            self.assertEqual(self._call()["text"], "the pulse")

    def test_failure_raises_and_still_cleans_up(self):
        self._fake(status="ERROR", code=1, response="")
        with mock.patch.dict(os.environ, {"ANTIGRAVITY_COMMAND": str(self.fake)}), \
                mock.patch.object(server.Path, "home", lambda: self.home):
            with self.assertRaises(RuntimeError):
                self._call()
        self.assertFalse((self.store / "conversations" / f"{self.cid}.db").exists())

    def test_forget_ignores_bad_ids(self):
        server.forget_antigravity_conversation("../conversations", home=self.home)
        server.forget_antigravity_conversation("", home=self.home)
        self.assertTrue((self.store / "conversations" / "keep.db").exists())

    def test_engine_wiring(self):
        state = types.SimpleNamespace(config=types.SimpleNamespace(
            state_dir=str(self.root), trajectory_index_db_path="x", imported_owner_actor_id="1"))
        runner = briefing.BriefingRunner(state, "antigravity")
        self.assertEqual(runner.suffix, ".antigravity")
        self.assertEqual(runner.model, "gemini-4-hydrogen")
        self.assertIn("(Antigravity)", runner._title("2026-11-15"))
        self.assertEqual(briefing.engine_agent("antigravity"), "antigravity")
        self.assertIn("command agy", briefing._launch_command("antigravity", Path("/t.md")))
        route = briefing.resolve_route({"target": "new", "cwd": "/w"}, {}, None, "antigravity")
        self.assertIn("new antigravity session", route["destination"])


if __name__ == "__main__":
    unittest.main()
