import json
import os
import tempfile
import types
import unittest
from pathlib import Path

from app.prompt_history import PromptHistoryStore
from app.server import AppState
from app.sharing_review import SharingPolicy
from sources.access import TrajectoryAccessAccount
from sources.antigravity import AntigravitySourceAdapter, infer_cwd, user_request
from sources.trajectory_lookup import read_payload_from_file

CLI_ID = "9b2c4c1e-1111-4a2b-9c3d-aaaaaaaaaaaa"
EDITOR_ID = "5d6e7f80-2222-4b3c-8d4e-bbbbbbbbbbbb"


def step(i, source, kind, content="", calls=None, ts="2026-11-05T15:0{}:00Z"):
    return {"step_index": i, "source": source, "type": kind, "status": "DONE",
            "created_at": ts.format(i), "content": content, "tool_calls": calls or [], "thinking": "hmm"}


class AntigravityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        logs = self.home / "antigravity-cli" / "brain" / CLI_ID / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        rows = [
            step(1, "USER_EXPLICIT", "USER_INPUT",
                 "<USER_REQUEST>where are the alphagenome tracks?</USER_REQUEST>\n<ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"),
            step(2, "MODEL", "RUN_COMMAND", "", [{"name": "run_command",
                 "args": {"CommandLine": "ls", "Cwd": "/Users/me/Code/accessibility"}}]),
            step(3, "MODEL", "VIEW_FILE", "", [{"name": "view_file",
                 "args": {"AbsolutePath": "/Users/me/Code/accessibility/README.md"}}]),
            step(4, "MODEL", "PLANNER_RESPONSE", "They are under /gpfs/data/zhou-lab/jzhou/datasets."),
            step(5, "SYSTEM", "USER_INPUT", "<USER_REQUEST>automatic follow-up</USER_REQUEST>"),
        ]
        full = logs / "transcript_full.jsonl"
        full.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        (logs / "transcript.jsonl").write_text(json.dumps(rows[0]) + "\n")
        # The same conversation copied to the editor's store on an upgrade.
        (self.home / "antigravity" / "brain" / CLI_ID).mkdir(parents=True)
        editor = self.home / "antigravity" / "brain" / EDITOR_ID
        editor.mkdir(parents=True)
        (editor / "implementation_plan.md").write_text(
            "# Plan\nEdit /Users/me/Code/puffin/train.py and /Users/me/Code/puffin/model.py")
        (editor / "walkthrough.md").write_text("Done: added the gate.")
        self.full = str(full)
        account = TrajectoryAccessAccount(source_name="antigravity", name="default", base_dir=str(self.home),
                                          private_workdirs=["/Users/me"])
        self.sessions = {s.session_id: s for s in AntigravitySourceAdapter(account=account).discover_sessions()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_conversation(self):
        s = self.sessions[CLI_ID]
        self.assertEqual([t.role for t in s.turns], ["user", "assistant", "user"])
        self.assertEqual(s.turns[0].text, "where are the alphagenome tracks?")
        self.assertEqual(s.metadata["cwd"], "/Users/me/Code/accessibility")  # where its commands ran
        self.assertEqual(s.metadata["human_turns"], 1)  # the SYSTEM input isn't the owner
        self.assertEqual(s.file_path, self.full)  # the full copy, not the clipped one
        self.assertEqual(s.metadata["app"], "cli")
        self.assertEqual(s.updated_at, "2026-11-05T15:05:00+00:00")

    def test_editor_only_conversation_reads_its_artifacts(self):
        s = self.sessions[EDITOR_ID]
        self.assertEqual(s.metadata["app"], "editor")
        self.assertIn("[implementation_plan.md]", s.turns[0].text + s.turns[-1].text)
        self.assertEqual(s.metadata["cwd"], "/Users/me/Code/puffin")

    def test_each_conversation_once(self):
        self.assertEqual(sorted(self.sessions), sorted([CLI_ID, EDITOR_ID]))

    def test_read_in_full(self):
        payload = read_payload_from_file(
            source_name="antigravity", source_ref=f"antigravity:{CLI_ID}", session_id=CLI_ID, title="t",
            updated_at="", cwd="", file_path=self.full, metadata={})
        self.assertIn("/gpfs/data/zhou-lab/jzhou/datasets", json.dumps(payload))

    def test_typed_prompts_reach_the_prompt_history(self):
        store = PromptHistoryStore(str(self.home / "ph.sqlite3"))
        store.refresh(history_paths={"antigravity": [self.full]})
        with store._connect() as conn:
            rows = conn.execute("SELECT session_id, ts, text FROM prompt_history").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(CLI_ID, "2026-11-05T15:01:00+00:00", "where are the alphagenome tracks?")])

    def test_hidden_from_teammates_until_shared(self):
        policy = SharingPolicy(self.home / "sharing.json")
        state = types.SimpleNamespace(sharing=policy,
                                      access_config=types.SimpleNamespace(is_private_workdir=lambda s, c: False))
        payload = {"source_ref": f"antigravity:{CLI_ID}#0", "source_name": "antigravity",
                   "cwd": "/Users/me/Code/accessibility", "metadata": {}}
        self.assertTrue(AppState.trajectory_payload_is_private(state, payload))
        policy.decide("antigravity:/Users/me/Code/accessibility", "team")
        self.assertFalse(AppState.trajectory_payload_is_private(state, payload))

    def test_helpers(self):
        self.assertEqual(user_request("<USER_REQUEST> hi </USER_REQUEST><USER_SETTINGS_CHANGE>m</USER_SETTINGS_CHANGE>"), "hi")
        self.assertEqual(infer_cwd([], []), "")


if __name__ == "__main__":
    unittest.main()
