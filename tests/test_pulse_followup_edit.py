import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from app import briefing


class FollowUpEditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        state = types.SimpleNamespace(config=types.SimpleNamespace(
            state_dir=str(root), trajectory_index_db_path=str(root / "none.sqlite3"), imported_owner_actor_id="1"))
        self.runner = briefing.BriefingRunner(state, "claude")
        self.path = self.runner.dir / "2026-10-29.json"
        self.path.write_text(json.dumps({"actions": [{
            "id": "o1", "label": "Check it", "prompt": "Original task", "source_ref": "claude:aaaaaaaa",
            "hop_session": "guess", "cwd": str(root), "spawned": None,
        }]}))
        self.calls = []
        live = {"conv-1": {"internal_name": "s_live", "name": "guess", "agent": "claude", "agent_permitted": True}}
        source = {"agent": "claude", "session_id": "aaaaaaaa-1111", "cwd": str(root), "env": {}}
        self.patches = [
            mock.patch.object(briefing, "PULSE_TASK_DIR", root / "tasks"),
            mock.patch.object(briefing, "hop_agent_sessions", lambda: live),
            mock.patch.object(briefing, "live_hop_sessions", lambda db, also=(): [
                {"conversation": "conv-1", **live["conv-1"], "title": "Some work", "cwd": str(root)}]),
            mock.patch.object(briefing, "source_session", lambda db, ref: source if ref.startswith("claude:aaaa") else None),
            mock.patch.object(briefing.BriefingRunner, "_hopa", staticmethod(self._hopa)),
        ]
        for p in self.patches:
            p.start()

    def _hopa(self, args, *, timeout):
        self.calls.append((args[1], json.loads(args[2])))
        return {"ok": True, "terminal_id": "t-new"}

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_plan_shows_default_and_alternatives(self):
        plan = self.runner.follow_up_plan("2026-10-29", "o1")
        self.assertEqual(plan["default"], "live:s_live")  # the agent's hop-session guess is live
        ids = [o["id"] for o in plan["options"]]
        self.assertEqual(ids, ["live:s_live", "resume:claude:aaaaaaaa-1111", "new"])
        self.assertEqual(plan["prompt"], "Original task")
        self.assertIn("2026-10-29-claude-o1.md", plan["live_message"])

    def test_edited_text_to_another_target(self):
        result = self.runner.follow_up("2026-10-29", "o1", prompt="Edited task", target="resume:claude:aaaaaaaa-1111")
        self.assertEqual(result["routed"], "resumed")
        spawn, send = self.calls
        self.assertEqual(spawn[0], "hopx_spawn_agent")
        self.assertIn("--resume aaaaaaaa-1111", spawn[1]["command"])
        self.assertIn("Edited task", send[1]["data"])
        saved = json.loads(self.path.read_text())["actions"][0]
        self.assertEqual(saved["prompt_edited"], "Edited task")
        self.assertIn("Edited task", (briefing.PULSE_TASK_DIR / "2026-10-29-claude-o1.md").read_text())
        # The plan now shows the edited text and where it went.
        plan = self.runner.follow_up_plan("2026-10-29", "o1")
        self.assertEqual(plan["prompt"], "Edited task")
        self.assertTrue(plan["spawned"])

    def test_live_target_gets_pointer_and_resend_is_allowed(self):
        self.runner.follow_up("2026-10-29", "o1", target="live:s_live")
        self.assertEqual(self.calls[0][1]["terminal_id"], "s_live")
        self.assertIn("2026-10-29-claude-o1.md", self.calls[0][1]["data"])
        again = self.runner.follow_up("2026-10-29", "o1")
        self.assertTrue(again.get("already"))
        self.runner.follow_up("2026-10-29", "o1", target="new")
        self.assertEqual(self.calls[-2][0], "hopx_spawn_agent")

    def test_bad_targets(self):
        with self.assertRaises(ValueError):
            self.runner.follow_up("2026-10-29", "o1", target="live:gone")
        with self.assertRaises(ValueError):
            self.runner.follow_up("2026-10-29", "o1", target="rm -rf")
        with self.assertRaises(ValueError):
            self.runner.follow_up("../x", "o1")


if __name__ == "__main__":
    unittest.main()
