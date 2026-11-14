import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from app import briefing

SOURCE = "aaaaaaaa-0000-4000-8000-000000000001"
FORK = "bbbbbbbb-0000-4000-8000-000000000002"
THREAD = "cccccccc-0000-7000-8000-000000000003"


class ForkedSessionRoutingTest(unittest.TestCase):
    """A session hop forked is a new session: the conversation it forked from
    stays with the session that runs it, and its pulse items go there."""

    def test_a_forked_claude_does_not_take_over_its_source_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / ".hop2" / "claude-sessions"
            records.mkdir(parents=True)
            (records / "src.json").write_text(json.dumps({"sessionId": SOURCE}))
            (records / "fork.json").write_text(json.dumps({"sessionId": FORK}))
            listing = json.dumps([
                {"internalName": "src", "name": "train", "live": True, "agent": "claude"},
                {"internalName": "fork", "name": "train-fork", "live": True, "agent": "claude"},
                {"internalName": "cx", "name": "eval", "live": True, "agent": "codex"},
            ])
            # `ps eww` shows each process's environment, where "codex" turns
            # up anywhere (a path, a session name).
            ps = "\n".join([
                f"102 claude --resume {SOURCE} CODEX_HOME=/Users/x/.codex HOP_SESSION=src",
                f"101 claude --resume {SOURCE} --fork-session PATH=/Users/x/Code/model-codex/bin HOP_SESSION=fork",
                f"103 node /opt/codex/bin/codex.js resume '{THREAD}' HOP_SESSION=cx",
            ])
            done = lambda out: types.SimpleNamespace(stdout=out)
            with mock.patch.object(briefing.Path, "home", return_value=Path(tmp)), \
                    mock.patch("subprocess.run", side_effect=lambda cmd, **kw: done(listing if cmd[0] == "hopa" else ps)):
                found = briefing.hop_agent_sessions()
        self.assertEqual(found[SOURCE]["internal_name"], "src")
        self.assertEqual(found[FORK]["internal_name"], "fork")
        self.assertEqual(found[THREAD]["internal_name"], "cx")

        # So an item from the source's conversation routes to the source.
        action = {"source_ref": f"claude:{SOURCE[:8]}", "target": ""}
        route = briefing.resolve_route(action, found, None, "claude")
        self.assertEqual(route["target"], "live:src")


if __name__ == "__main__":
    unittest.main()
