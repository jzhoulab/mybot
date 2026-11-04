import types
import unittest
from unittest import mock

from app import briefing

DRAFT = "Intro.\n\n## You may have overlooked\n\n- a\n- b\n\n## Article one\n\n" + "text " * 300 + "\n\n## Article two\n\nmore\n\n```json\n{}\n```"


class VerifyGuardTest(unittest.TestCase):
    def run_verify(self, outputs):
        runner = briefing.BriefingRunner.__new__(briefing.BriefingRunner)
        runner._AUDIENCE, runner.verify_effort = "", "low"
        runner._tool_section = lambda: ""
        calls = iter(outputs)
        runner._agent = lambda **kwargs: next(calls)
        with mock.patch.object(briefing.log, "warning"):
            return runner.verify({}, DRAFT)

    def test_refusal_is_retried_then_falls_back_to_the_draft(self):
        refusal = "I can't reproduce or revise that draft.\n\nSay the word and I'll begin."
        self.assertEqual(self.run_verify([refusal, refusal]), DRAFT)

    def test_good_retry_is_used(self):
        fixed = DRAFT.replace("Article one", "Article one, fixed")
        self.assertEqual(self.run_verify(["", fixed]), fixed)

    def test_checked_pulse_passes(self):
        self.assertTrue(briefing.looks_like_pulse(DRAFT.replace("- b\n", ""), DRAFT))
        self.assertFalse(briefing.looks_like_pulse("## One\n\nshort", DRAFT))


if __name__ == "__main__":
    unittest.main()
