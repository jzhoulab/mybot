import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from app import briefing
from app.prompt_history import PromptHistoryStore
from app.server import AppState
from app.sharing_review import SharingPolicy
from sources.access import TrajectoryAccessAccount
from sources.antigravity import (AntigravitySourceAdapter, _MODEL_RE, conversation_index, infer_cwd, read_events,
                                  step_output, tool_summary, transcript_workdir, user_request)
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


PRINT_ID = "7c8d9e0f-3333-4c4d-9e5f-cccccccccccc"


class CliRecordsTest(unittest.TestCase):
    """The files agy 1.5 keeps beside its transcripts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.store = self.home / "antigravity-cli"
        self.paths = {}
        for ident, text in ((CLI_ID, "fix the session store"), (PRINT_ID, "scripted run")):
            logs = self.store / "brain" / ident / ".system_generated" / "logs"
            logs.mkdir(parents=True)
            rows = [
                step(1, "USER_EXPLICIT", "USER_INPUT", f"<USER_REQUEST>{text}</USER_REQUEST>\n<USER_SETTINGS_CHANGE>"
                     "The user changed setting `Model Selection` from None to Gemini 3.8 Flash (Low). No need to comment"),
                step(2, "MODEL", "PLANNER_RESPONSE", "", [{"name": "run_command", "args": {
                    "CommandLine": "git status", "WaitMsBeforeAsync": 5000, "toolSummary": "x"}},
                    {"name": "read_file", "args": {"Path": "/tmp/elsewhere/a.py"}}]),
                step(3, "MODEL", "GENERIC", "Created At: 2026\nCompleted At: 2026\nCommand output:\nclean"),
                step(4, "MODEL", "PLANNER_RESPONSE", "All clean."),
            ]
            path = logs / "transcript_full.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            self.paths[ident] = str(path)
        self.history = self.store / "history.jsonl"
        self.history.write_text("\n".join(json.dumps(r) for r in [
            {"display": "/model", "timestamp": 1794361121975, "workspace": "/Users/me/Code/nebula", "type": "slash_command"},
            {"display": "fix the session store", "timestamp": 1794361220876, "workspace": "/Users/me/Code/nebula",
             "conversationId": CLI_ID},
        ]) + "\n")
        (self.store / "cache").mkdir()
        (self.store / "cache" / "conversation_metadata.json").write_text(json.dumps({"conversations": {
            CLI_ID: {"summary": {"ID": CLI_ID, "Title": "Session store hardening", "Internal": False}, "is_internal": False},
        }}))
        account = TrajectoryAccessAccount(source_name="antigravity", name="default", base_dir=str(self.home))
        self.sessions = {s.session_id: s for s in AntigravitySourceAdapter(account=account).discover_sessions()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_folder_title_and_model(self):
        s = self.sessions[CLI_ID]
        self.assertEqual(s.metadata["cwd"], "/Users/me/Code/nebula")
        self.assertFalse(s.metadata["cwd_inferred"])
        self.assertEqual(s.title, "Session store hardening")
        self.assertEqual(s.metadata["model"], "Gemini 3.8 Flash (Low)")

    def test_print_mode_is_automated(self):
        self.assertEqual(self.sessions[CLI_ID].metadata["origin"], "interactive")
        self.assertEqual((self.sessions[PRINT_ID].metadata["origin"], self.sessions[PRINT_ID].metadata["origin_detail"]),
                         ("automated", "exec"))

    def test_print_mode_stays_out_with_the_default_policy(self):
        account = TrajectoryAccessAccount(source_name="antigravity", name="default", base_dir=str(self.home),
                                          excluded_origin_details=["exec"])
        found = {s.session_id for s in AntigravitySourceAdapter(account=account).discover_sessions()}
        self.assertEqual(found, {CLI_ID})

    def test_events_carry_actions_and_tool_output(self):
        events = read_events(self.paths[CLI_ID])
        self.assertEqual([e[0] for e in events], ["user", "actions", "tool_result", "assistant"])
        self.assertEqual(events[1][1], "[run_command] git status\n[read_file] /tmp/elsewhere/a.py")
        self.assertEqual(events[2][1], "Command output:\nclean")

    def test_workdir_without_reading_turns(self):
        self.assertEqual(transcript_workdir(self.paths[CLI_ID], str(self.home)), "/Users/me/Code/nebula")
        self.assertEqual(transcript_workdir(self.paths[PRINT_ID], str(self.home)), "/tmp/elsewhere")

    def test_index_sees_a_new_prompt(self):
        self.assertNotIn(PRINT_ID, conversation_index(str(self.home))["typed"])
        with self.history.open("a") as handle:
            handle.write(json.dumps({"display": "later", "timestamp": 1794361221000, "workspace": "/w",
                                     "conversationId": PRINT_ID}) + "\n")
        self.assertIn(PRINT_ID, conversation_index(str(self.home))["typed"])

    def test_prompt_history_from_the_cli_log(self):
        store = PromptHistoryStore(str(self.home / "ph.sqlite3"))
        store.refresh(history_paths={"antigravity": [str(self.history)]})
        with store._connect() as conn:
            rows = conn.execute("SELECT session_id, cwd, text FROM prompt_history").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(CLI_ID, "/Users/me/Code/nebula", "fix the session store")])

    def test_session_viewer_parses_steps(self):
        from scripts.mybot_admin import _parse_trajectory
        events, omitted = _parse_trajectory(self.paths[CLI_ID], "antigravity", 100)
        self.assertEqual(omitted, 0)
        kinds = [e["kind"] for e in events]
        self.assertIn("tool_use", kinds)
        self.assertIn("tool_result", kinds)
        self.assertEqual(events[0]["text"], "fix the session store")

    def test_helpers(self):
        self.assertEqual(step_output("Created At: a\nCompleted At: b\nok"), "ok")
        self.assertEqual(tool_summary({"name": "search_web", "args": {"query": "x", "toolSummary": "y"}}),
                         '[search_web] {"query": "x"}')
        self.assertIsNone(_MODEL_RE.search("no setting here"))


class FollowUpRoutingTest(unittest.TestCase):
    def test_resume_command(self):
        session = {"agent": "antigravity", "session_id": CLI_ID, "cwd": "/w", "env": {}}
        self.assertEqual(briefing._resume_command(session),
                         f"command agy --conversation {CLI_ID} --dangerously-skip-permissions")

    def test_only_the_running_agents_record_is_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / ".hop2" / "claude-sessions"
            records.mkdir(parents=True)
            (records / "room1.json").write_text(json.dumps({"sessionId": "stale-claude"}))
            (records / "room1.agy").write_text(json.dumps({"sessionId": CLI_ID}))
            (records / "room2.json").write_text(json.dumps({"sessionId": "live-claude"}))
            (records / "room2.agy").write_text(json.dumps({"sessionId": "stale-agy"}))
            listing = json.dumps([
                {"internalName": "room1", "name": "nebula", "live": True, "agent": "antigravity", "agentPermitted": True},
                {"internalName": "room2", "name": "other", "live": True, "agent": "claude"},
            ])
            done = lambda out: types.SimpleNamespace(stdout=out)
            with mock.patch.object(briefing.Path, "home", return_value=Path(tmp)), \
                    mock.patch("subprocess.run", side_effect=lambda cmd, **kw: done(listing if cmd[0] == "hopa" else "")):
                found = briefing.hop_agent_sessions()
        self.assertEqual(sorted(found), sorted([CLI_ID, "live-claude"]))
        self.assertEqual(found[CLI_ID]["agent"], "antigravity")
