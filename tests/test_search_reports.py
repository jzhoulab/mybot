"""`mybot report`: an agent says search came up short; the report is kept and
passed on, batched, to the hop session that maintains mybot."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app import feedback as fb
from app.server import ChatHandler


def report(**overrides):
    return {"queries": ["streaming parser tradeoff"], "wanted": "why we chose the streaming parser",
            "got": "only the batch parser benchmark", "found": "claude:abc chunk 12",
            "agent": "claude", "hop_session": "Alpha", "conversation": "claude:xyz", "cwd": "/w",
            **overrides}


class NotifierTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.reports = fb.SearchReports(self.dir)
        self.sent: list[tuple[str, str]] = []
        self.live = {"Mybot": "Mybot-internal"}
        self.notifier = fb.SearchFeedbackNotifier(
            self.reports, session_name="Mybot", find_session=lambda name: self.live.get(name, ""),
            send=lambda internal, message: self.sent.append((internal, message)) or {"ok": True},
            directory=self.dir / "out",
        )

    def test_a_burst_goes_out_as_one_message_pointing_at_the_details(self) -> None:
        self.reports.record(report())
        self.reports.record(report(wanted="the nightly eval path", queries=["nightly eval", "eval output dir"]))
        result = self.notifier.flush()
        self.assertEqual(result["sent"], 2)
        internal, message = self.sent[0]
        self.assertEqual(internal, "Mybot-internal")
        self.assertNotIn("\n", message)  # hop submits on Enter
        self.assertIn("and 1 more", message)
        details = Path(result["file"]).read_text()
        self.assertIn(str(result["file"]), message)
        for text in ("`streaming parser tradeoff`", "`eval output dir`", "Where it really was: claude:abc chunk 12",
                     "mybot trajectory-read --source-ref claude:xyz"):
            self.assertIn(text, details)
        self.assertEqual(self.reports.pending(), [])
        self.assertEqual(self.notifier.flush()["sent"], 0)  # nothing twice

    def test_reports_wait_while_the_session_is_closed_and_skip_its_own(self) -> None:
        self.live = {}
        self.reports.record(report())
        self.assertFalse(self.notifier.flush()["ok"])
        self.assertEqual(len(self.reports.pending()), 1)
        self.reports.record(report(hop_session="Mybot"))  # the maintainer's own search
        self.live = {"Mybot": "Mybot-internal"}
        self.assertEqual(self.notifier.flush()["sent"], 1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual({e["notified"].split()[0] for e in self.reports.recent()}, {"hop:Mybot", "own"})

    def test_a_failed_send_keeps_them_pending(self) -> None:
        self.notifier.send = lambda internal, message: {"ok": False, "error": "Terminal not found"}
        self.reports.record(report())
        self.assertFalse(self.notifier.flush()["ok"])
        self.assertEqual(len(self.reports.pending()), 1)


class EndpointTest(unittest.TestCase):
    def test_report_is_validated_kept_and_scheduled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scheduled: list[bool] = []
            state = SimpleNamespace(
                search_reports=fb.SearchReports(tmp),
                search_notifier=SimpleNamespace(session_name="Mybot", schedule=lambda: scheduled.append(True)),
            )
            replies: list[tuple[int, dict]] = []
            handler = SimpleNamespace(server=SimpleNamespace(state=state),
                                      respond_json=lambda status, payload: replies.append((status, payload)))
            ChatHandler.handle_search_report(handler, {"queries": ["x"], "wanted": ""})
            self.assertEqual(replies[-1][0], 400)
            ChatHandler.handle_search_report(handler, {**report(), "queries": ["  ", "nightly eval"]})
            self.assertEqual(replies[-1][0], 200)
            self.assertEqual(replies[-1][1]["passed_to"], "Mybot")
            self.assertEqual(scheduled, [True])
            kept = state.search_reports.recent()[0]
            self.assertEqual((kept["queries"], kept["hop_session"]), (["nightly eval"], "Alpha"))


class CliIdentityTest(unittest.TestCase):
    def test_reporter_conversation_comes_from_the_agent_env(self) -> None:
        import importlib.util
        import os
        from unittest.mock import patch

        spec = importlib.util.spec_from_file_location("mybot_tool", Path(__file__).parents[1] / "scripts" / "mybot_tool.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "t-1", "HOP_SESSION": "Beta"}, clear=True):
            identity = tool.reporter_identity()
        self.assertEqual((identity["agent"], identity["conversation"], identity["hop_session"]),
                         ("codex", "codex:t-1", "Beta"))


if __name__ == "__main__":
    unittest.main()
