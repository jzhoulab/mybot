import types
import unittest
from unittest import mock

from app import slack_bridge as sb


class FakeClient:
    def __init__(self, replies=None, history=None, fail=False):
        self.replies, self.history, self.fail = replies or [], history or [], fail

    def conversations_replies(self, **kwargs):
        if self.fail:
            raise RuntimeError("no access")
        self.asked = kwargs
        return {"messages": self.replies}

    def conversations_history(self, **kwargs):
        self.asked = kwargs
        return {"messages": self.history}


BRIDGE = types.SimpleNamespace(
    bot_user_id="UBOT",
    display_name=lambda client, user: {"UJ": "Jian", "UK": "Junru"}.get(user, user),
    resolve_mentions=lambda client, text: text,
)


class ContextTest(unittest.TestCase):
    def test_a_thread_reply_sees_the_question_it_hangs_off(self):
        client = FakeClient(replies=[
            {"ts": "1", "user": "UJ", "text": "where are alphagenome datasets in SSDs?"},
            {"ts": "2", "user": "UBOT", "bot_id": "B", "text": "I couldn't find it"},
            {"ts": "3", "user": "UK", "text": "Where it is in the cri cluster?"},
        ])
        text = sb.conversation_context(client, BRIDGE, {"channel": "C", "thread_ts": "1", "ts": "3"})
        self.assertIn("Jian: where are alphagenome datasets in SSDs?", text)
        self.assertIn("(you): I couldn't find it", text)
        self.assertNotIn("Where it is", text)  # the message being answered

    def test_top_level_message_sees_the_channel_in_order(self):
        client = FakeClient(history=[{"ts": "5", "user": "UK", "text": "second"}, {"ts": "4", "user": "UJ", "text": "first"}])
        text = sb.conversation_context(client, BRIDGE, {"channel": "C", "ts": "6"})
        self.assertEqual(text, "Jian: first\nJunru: second")

    def test_failure_costs_only_the_context(self):
        with mock.patch.object(sb.log, "warning"):
            self.assertEqual(sb.conversation_context(FakeClient(fail=True), BRIDGE, {"channel": "C", "thread_ts": "1", "ts": "3"}), "")

    def test_long_context_keeps_the_latest(self):
        client = FakeClient(replies=[{"ts": "1", "user": "UJ", "text": "old " * 4000}, {"ts": "2", "user": "UK", "text": "latest"}])
        text = sb.conversation_context(client, BRIDGE, {"channel": "C", "thread_ts": "1", "ts": "9"})
        self.assertLessEqual(len(text), sb.CONTEXT_CHARS)
        self.assertTrue(text.endswith("Junru: latest"))


class MrkdwnTest(unittest.TestCase):
    def test_bold_headings_links(self):
        self.assertEqual(sb.to_slack_mrkdwn("**Cluster:** x"), "*Cluster:* x")
        self.assertEqual(sb.to_slack_mrkdwn("## 1. Where"), "*1. Where*")
        self.assertEqual(sb.to_slack_mrkdwn("[a](https://x.io/b)"), "<https://x.io/b|a>")

    def test_code_is_untouched(self):
        self.assertEqual(sb.to_slack_mrkdwn("`**a**` and **b**"), "`**a**` and *b*")
        self.assertEqual(sb.to_slack_mrkdwn("```\n**a**\n```"), "```\n**a**\n```")

    def test_table_becomes_a_code_block(self):
        self.assertEqual(sb.to_slack_mrkdwn("| a | b |\n|---|---|\n| 1 | 2 |\nafter"),
                         "```\n| a | b |\n| 1 | 2 |\n```\nafter")


if __name__ == "__main__":
    unittest.main()
