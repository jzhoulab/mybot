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


class AutoSendTest(unittest.TestCase):
    """Items whose work is open in a live hop session go straight there."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        state = types.SimpleNamespace(config=types.SimpleNamespace(
            state_dir=str(root), trajectory_index_db_path=str(root / "none.sqlite3"), imported_owner_actor_id="1"))
        self.runner = briefing.BriefingRunner(state, "claude")
        self.calls = []
        live = {
            "c1": {"internal_name": "s_a", "name": "alpha", "agent": "claude", "agent_permitted": True},
            "c2": {"internal_name": "s_b", "name": "beta", "agent": "codex", "agent_permitted": False},
        }
        self.patches = [
            mock.patch.object(briefing, "PULSE_TASK_DIR", root / "tasks"),
            mock.patch.object(briefing, "hop_agent_sessions", lambda: live),
            mock.patch.object(briefing.BriefingRunner, "_hopa", staticmethod(self._hopa)),
        ]
        for p in self.patches:
            p.start()

    def _hopa(self, args, *, timeout):
        self.calls.append(json.loads(args[2]))
        return {"ok": True}

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_live_items_sent_once_per_session(self):
        actions = [
            {"id": "o1", "label": "One", "prompt": "do one", "target": "live:s_a"},
            {"id": "a1", "label": "Two", "prompt": "do two", "target": "live:s_a"},
            {"id": "o2", "label": "Locked", "prompt": "x", "target": "live:s_b"},
            {"id": "o3", "label": "Closed", "prompt": "x", "target": "resume:claude:abc"},
            {"id": "o4", "label": "New", "prompt": "x", "target": "new"},
        ]
        sent = self.runner.send_to_live_sessions("2026-11-15", actions)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["terminal_id"], "s_a")
        self.assertIn("2 items", self.calls[0]["data"])
        self.assertNotIn("\n", self.calls[0]["data"])
        self.assertEqual(sent, {"alpha": {"ok": True, "items": ["o1", "a1"]}})
        self.assertTrue(actions[0]["spawned"]["auto"])
        self.assertIsNone(actions[2].get("spawned"))   # hop refuses agents there: left to the owner
        self.assertIsNone(actions[3].get("spawned"))   # opening a closed one starts an agent
        text = briefing.follow_ups_text(actions)
        self.assertIn("sent to hop session “alpha”", text)
        self.assertIn("the rest send from the Pulse tab", text)
        # Sent once: a second pass sends nothing.
        self.runner.send_to_live_sessions("2026-11-15", actions)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()


class OneRouteTest(FollowUpEditTest):
    """Where a follow-up goes is part of the pulse: the app's plan, the
    phone's feed and the Discord/email text all show the same route."""

    def _set(self, **fields):
        data = json.loads(self.path.read_text())
        data["actions"][0].update(fields)
        self.path.write_text(json.dumps(data))

    def _hop_entry(self):
        briefing.PULSE_TASK_DIR.mkdir(parents=True, exist_ok=True)
        latest = {"date": "2026-10-29", **json.loads(self.path.read_text())}
        return briefing._hop_pulse("claude", latest, briefing.hop_agent_sessions(), "db")["actions"][0]

    def test_stored_route_is_what_every_surface_uses(self):
        self._set(target="resume:claude:aaaaaaaa-1111", destination="claude conversation aaaaaaaa, reopened in hop")
        plan = self.runner.follow_up_plan("2026-10-29", "o1")
        self.assertEqual(plan["default"], "resume:claude:aaaaaaaa-1111")
        entry = self._hop_entry()
        self.assertEqual(entry["target"], plan["default"])
        self.assertEqual(entry["resumes"], "claude:aaaaaaaa-1111")
        self.assertNotIn("target_internal_name", entry)  # not sent to the live guess
        text = briefing.follow_ups_text(json.loads(self.path.read_text())["actions"])
        self.assertIn("→ claude conversation aaaaaaaa, reopened in hop", text)
        self.assertIn("> Original task", text)

    def test_owner_choice_and_edit_carry_to_the_phone(self):
        self.runner.follow_up("2026-10-29", "o1", prompt="Edited task", target="new")
        action = json.loads(self.path.read_text())["actions"][0]
        self.assertEqual(action["target"], "new")
        self.assertIn("a new claude session", action["destination"])
        self.assertEqual(self.runner.follow_up_plan("2026-10-29", "o1")["default"], "new")
        entry = self._hop_entry()
        self.assertEqual((entry["target"], entry["prompt"]), ("new", "Edited task"))
        self.assertIn("Edited task", (briefing.PULSE_TASK_DIR / "2026-10-29-claude-o1.md").read_text())

    def test_closed_live_session_falls_back(self):
        self._set(target="live:s_gone")
        route = briefing.resolve_route(json.loads(self.path.read_text())["actions"][0],
                                       briefing.hop_agent_sessions(), None, "claude")
        self.assertEqual(route["target"], "live:s_live")  # the agent's guess is still open
        self.assertIn("“guess” (open now)", route["destination"])
