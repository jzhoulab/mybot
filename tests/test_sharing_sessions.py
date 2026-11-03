import json
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from app import sharing_review as sr
from app.server import AppState
from sources.access import TrajectoryAccessAccount


HOME = str(Path.home())
ORIGINS = {
    "tui": {"originator": "codex-tui", "source": "cli"},
    "old-cli": {"originator": "", "source": "cli"},
    "gui": {"originator": "Codex Desktop", "source": "vscode"},
    "old-gui": {"originator": "", "source": "vscode"},
    "exec": {"originator": "codex_exec", "source": "exec"},
}


class SecondHomeTest(unittest.TestCase):
    def test_only_a_snapshot_prefixes_session_ids(self):
        snapshot = TrajectoryAccessAccount(source_name="claude", name="snapshot", base_dir="/x")
        home = TrajectoryAccessAccount(source_name="claude", name="fable", base_dir="/y", scope_session_ids=False)
        self.assertEqual(snapshot.scoped_session_id("abc"), "snapshot:abc")
        self.assertEqual(home.scoped_session_id("abc"), "abc")
        self.assertFalse(home.to_json()["scope_session_ids"])


class GuiChatTest(unittest.TestCase):
    def check(self, ident, cwd, expected, source="codex"):
        self.assertEqual(sr.is_codex_gui_chat(source, f"codex:{ident}", cwd, ORIGINS), expected, (ident, cwd))

    def test_only_gui_chats_without_a_project(self):
        self.check("gui", HOME, True)
        self.check("old-gui", HOME, True)
        self.check("gui", f"{HOME}/Documents/Codex/2026-10-17/x", True)
        self.check("gui", f"{HOME}/Code/project", False)  # the GUI in a real folder is reviewed normally
        self.check("tui", HOME, False)
        self.check("old-cli", f"{HOME}/Code", False)
        self.check("tui", HOME, False, source="claude")

    def test_unknown_origin_fails_closed(self):
        self.check("not-in-db", HOME, True)
        self.check("exec", HOME, True)
        self.check("tui", f"{HOME}/Documents/Codex/x", True)  # the app's own folders


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.policy = sr.SharingPolicy(Path(self.tmp.name) / "sharing.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_shared_session_in_a_catch_all_is_visible(self):
        self.assertTrue(self.policy.hidden_from_team("codex", HOME, "codex:abc"))
        self.policy.decide_session("codex:abc", "team")
        self.assertFalse(self.policy.hidden_from_team("codex", HOME, "codex:abc#3"))
        self.assertTrue(self.policy.hidden_from_team("codex", HOME, "codex:other"))

    def test_private_wins_and_kept_session_hides_inside_a_team_project(self):
        self.policy.decide_session("codex:abc", "team")
        self.policy.set_session_private("codex:abc", True)
        self.assertFalse(self.policy.session_shared("codex:abc"))
        self.assertTrue(self.policy.hidden_from_team("codex", HOME, "codex:abc"))
        self.policy.decide("codex:/w", "team")
        self.policy.decide_session("codex:kept", "owner")
        self.assertTrue(self.policy.hidden_from_team("codex", "/w", "codex:kept"))
        self.assertFalse(self.policy.hidden_from_team("codex", "/w", "codex:new"))

    def test_shared_session_outranks_a_private_workdir_stamp(self):
        state = types.SimpleNamespace(
            sharing=self.policy,
            access_config=types.SimpleNamespace(is_private_workdir=lambda s, c: True),
        )
        payload = {"source_ref": "codex:abc#2", "source_name": "codex", "cwd": HOME,
                   "metadata": {"visibility": "private"}}
        self.assertTrue(AppState.trajectory_payload_is_private(state, payload))
        self.policy.decide_session("codex:abc", "team")
        self.assertFalse(AppState.trajectory_payload_is_private(state, payload))


class ReviewerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        db = root / "index.sqlite3"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE trajectory_chunks (source_ref, source_name, session_id, cwd, title, updated_at)")
        conn.execute("CREATE TABLE prompt_history (source_name, session_id, ts, cwd, text)")
        for ident, source, cwd, chunks, typed in [
            ("tui", "codex", HOME, 30, True), ("gui", "codex", HOME, 90, True),
            ("hopc", "claude", HOME, 5, True), ("auto", "claude", HOME, 50, False),
        ]:
            for i in range(chunks):
                conn.execute("INSERT INTO trajectory_chunks VALUES (?,?,?,?,?,?)",
                             (f"{source}:{ident}", source, ident, cwd, f"Title {ident}", f"2026-10-{i % 28 + 1:02d}"))
            if typed:
                conn.execute("INSERT INTO prompt_history VALUES (?,?,?,?,?)", (source, ident, "1", cwd, "where is the data"))
        conn.commit()
        conn.close()
        self.state = types.SimpleNamespace(
            sharing=sr.SharingPolicy(root / "sharing.json"),
            config=types.SimpleNamespace(trajectory_index_db_path=str(db), state_dir=str(root),
                                         imported_owner_actor_id="123"),
            access_config=types.SimpleNamespace(is_agent_workspace=lambda c: False),
            provider=types.SimpleNamespace(run_claude_task=self._agent),
        )
        self.reviewer = sr.SharingReviewer(self.state, path=root / "review.json", model="m")
        self.answers = []
        self.hop = {"hopc": "alpha-claude"}
        self.patches = [
            mock.patch.object(sr, "codex_thread_origins", lambda: ORIGINS),
            mock.patch.object(sr, "hop_session_names", lambda: self.hop),
        ]
        for p in self.patches:
            p.start()

    def _agent(self, **kwargs):
        self.seen = kwargs["message"]
        return {"text": json.dumps(self.answers)}

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_candidates_skip_gui_and_automated_sessions(self):
        self.assertEqual({c["source_ref"] for c in self.reviewer.session_candidates()}, {"codex:tui", "claude:hopc"})

    def test_recommends_without_sharing(self):
        self.answers = [{"ref": "codex:tui", "name": "SSD data staging", "level": "team", "confidence": "high",
                         "reason": "data paths"},
                        {"ref": "claude:hopc", "level": "owner", "confidence": "high", "reason": "logistics"}]
        self.assertEqual(self.reviewer.run_sessions()["recommended"], 1)
        self.assertNotIn("codex:gui", self.seen)
        self.assertEqual([s["ref"] for s in self.reviewer.recommended()], ["codex:tui"])
        self.assertTrue(self.state.sharing.hidden_from_team("codex", HOME, "codex:tui"))  # still needs a yes
        self.assertEqual(self.reviewer.recommended()[0]["name"], "SSD data staging")
        self.assertFalse((Path(self.state.config.state_dir) / "outbox.jsonl").exists())  # the menu app shows these
        self.assertEqual(self.reviewer.session_candidates(), [])  # reviewed: not asked again

    def test_recommendation_without_a_name_is_reviewed_again(self):
        self.answers = [{"ref": "codex:tui", "level": "team", "confidence": "high", "reason": "paths"}]
        self.reviewer.run_sessions()
        self.answers = []
        self.assertIn("codex:tui", [c["source_ref"] for c in self.reviewer.session_candidates()])

    def test_hop_name_shown_and_kept_after_it_closes(self):
        self.answers = [{"ref": "claude:hopc", "name": "n", "level": "team", "confidence": "high", "reason": "r"},
                        {"ref": "codex:tui", "name": "n", "level": "team", "confidence": "low", "reason": "r"}]
        self.reviewer.run_sessions()
        by_ref = {s["ref"]: s for s in self.reviewer.recommended()}
        self.assertEqual(by_ref["claude:hopc"]["hop_name"], "alpha-claude")
        self.assertEqual(by_ref["codex:tui"]["hop_name"], "")  # not in hop: nothing hop-related
        self.assertFalse(by_ref["codex:tui"]["hop"])
        self.hop = {}  # closed, or hop isn't used at all
        self.assertEqual({s["ref"]: s["hop_name"] for s in self.reviewer.recommended()}["claude:hopc"], "alpha-claude")

    def test_shared_session_that_turned_personal_is_taken_back(self):
        self.state.sharing.decide_session("codex:tui", "team", chunks=1)
        self.answers = [{"ref": "codex:tui", "level": "owner", "confidence": "high", "reason": "now personal"}]
        self.reviewer.run_sessions()
        self.assertFalse(self.state.sharing.session_shared("codex:tui"))


if __name__ == "__main__":
    unittest.main()
