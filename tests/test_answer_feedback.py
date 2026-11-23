"""A 👎 on an answer asks what missed; the reply goes back into the same
conversation as feedback, and the owner keeps a record of both."""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import feedback as fb
from app.server import ChatHandler, SessionStore


class FeedbackStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = fb.FeedbackStore(self.tmp.name)

    def test_note_fills_in_the_rating_it_follows(self) -> None:
        self.store.record(actor_id="u1", session_key="s", exchange={"question": "q", "answer": "a"})
        self.store.record(actor_id="u2", session_key="s")  # someone else's rating stays separate
        self.store.record(actor_id="u1", session_key="s", comment="wrong project")
        entries = self.store.recent()
        self.assertEqual(len(entries), 2)
        mine = next(e for e in entries if e["actor_id"] == "u1")
        self.assertEqual((mine["comment"], mine["question"], mine["answer"]), ("wrong project", "q", "a"))
        # A second note is a new entry, not an overwrite.
        self.store.record(actor_id="u1", session_key="s", comment="also stale")
        self.assertEqual(len(self.store.recent()), 3)

    def test_last_exchange_pairs_the_answer_with_its_question(self) -> None:
        messages = [
            {"role": "user", "content": "old"}, {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "which run?"},
            {"role": "assistant", "content": "run 7", "meta": {"response_hash": "abc"}, "timestamp": "t"},
        ]
        self.assertEqual(fb.last_exchange(messages),
                         {"question": "which run?", "answer": "run 7", "answer_hash": "abc", "answered_at": "t"})
        self.assertEqual(fb.last_exchange([]), {})

    def test_pending_feedback_expires_and_is_taken_once(self) -> None:
        pending = fb.PendingFeedback(ttl_seconds=10)
        pending.expect("C:1", "u", {"session_key": "s"}, now=0)
        self.assertIsNone(pending.take("C:1", "other", now=1))
        self.assertEqual(pending.take("C:1", "u", now=1), {"session_key": "s"})
        self.assertIsNone(pending.take("C:1", "u", now=2))
        pending.expect("C:1", "u", {}, now=0)
        self.assertIsNone(pending.take("C:1", "u", now=11))

    def test_thumbs_down_names(self) -> None:
        for name in ("👎", "👎🏽", "-1", ":-1:", "thumbsdown", "-1::skin-tone-3"):
            self.assertTrue(fb.is_thumbs_down(name), name)
        for name in ("👍", "+1", "eyes", ""):
            self.assertFalse(fb.is_thumbs_down(name), name)


class ServerFeedbackTurnTest(unittest.TestCase):
    """/chat with feedback=down: marked for the model, kept in the same
    session even past the idle rollover, and recorded with the answer."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sessions = SessionStore(self.tmp.name)
        self.prompts: list[str] = []

        def build_system_prompt(**kwargs):
            self.prompts.append(kwargs["query"])
            return "system", [], False

        config = SimpleNamespace(
            session_idle_rollover_seconds=0, session_topic_shift_seconds=0,
            group_context_window_seconds=0, history_max_messages=20, history_max_chars=20000,
            memory_match_limit=5, agentic_tool_routing=False,
        )
        state = SimpleNamespace(
            sessions=self.sessions, config=config, feedback=fb.FeedbackStore(self.tmp.name),
            actor_is_owner=lambda actor: False, build_system_prompt=build_system_prompt,
            tool_env_for_request=lambda **kw: {},
        )
        self.handler = SimpleNamespace(server=SimpleNamespace(state=state))
        self.state = state

    def context(self, **body):
        ctx, error = ChatHandler._chat_context(self.handler, {"user": "u", "actor_id": "u",
                                                              "session_key": "dm-u", **body})
        self.assertIsNone(error)
        return ctx

    def test_feedback_turn_stays_with_the_answer_and_is_recorded(self) -> None:
        first = self.context(message="where did the nightly eval land?")
        self.sessions.append_message(first["session_key"], "u", "user", first["message"])
        self.sessions.append_message(first["session_key"], "u", "assistant", "In /old/path.",
                                     meta={"response_hash": "h1"})
        # Idle thresholds are 0 here, so any normal turn would roll over; the
        # feedback turn stays on the answer it is about.
        note = self.context(message="that is the old path, it moved last week", feedback="down",
                            platform="discord")
        self.assertEqual(note["session_key"], first["session_key"])
        self.assertTrue(note["message"].startswith(fb.FEEDBACK_TURN_PREFIX))
        self.assertTrue(self.prompts[-1].startswith(fb.FEEDBACK_TURN_PREFIX))
        self.assertNotEqual(self.context(message="totally different topic today please")["session_key"],
                            first["session_key"])
        entry = self.state.feedback.recent()[0]
        self.assertEqual((entry["comment"], entry["answer"], entry["answer_hash"], entry["platform"]),
                         ("that is the old path, it moved last week", "In /old/path.", "h1", "discord"))
        self.assertEqual(entry["question"], "where did the nightly eval land?")


class SlackReactionTest(unittest.TestCase):
    def setUp(self) -> None:
        from app import slack_bridge as sb

        env = patch.dict(os.environ, {"SLACK_BOT_TOKEN": "x", "SLACK_APP_TOKEN": "y",
                                      "SLACK_ALLOWED_CHANNEL_IDS": "C1"})
        env.start()
        self.addCleanup(env.stop)
        self.api = Mock(spec=sb.ChatbotApi)
        self.api.chat.return_value = {"text": "Second try."}
        self.api.channel_call.return_value = {}
        self.client = Mock()
        self.client.users_info.return_value = {"user": {"team_id": "T1"}}
        self.client.conversations_info.return_value = {"channel": {"id": "C1", "is_member": True}}
        self.client.conversations_replies.return_value = {"messages": [{"ts": "10.1", "thread_ts": "10.1"}]}
        self.client.conversations_history.return_value = {"messages": []}
        self.handlers: dict = {}
        app = Mock()
        app.client = self.client
        app.event.side_effect = lambda name: (lambda handler: self.handlers.setdefault(name, handler))
        with patch.object(sb, "App", return_value=app), patch.object(sb, "ChatbotApi", return_value=self.api):
            _, self.bridge, _ = sb.build_bridge()
        self.bridge.team_id, self.bridge.bot_user_id = "T1", "UBOT"
        self.say = Mock()

    def react(self, reaction: str = "-1", item_user: str = "UBOT") -> None:
        self.handlers["reaction_added"]({
            "user": "U1", "reaction": reaction, "item_user": item_user,
            "item": {"type": "message", "channel": "C1", "ts": "10.5"},
        }, self.client, Mock())

    def message(self, text: str) -> None:
        self.handlers["message"]({"channel": "C1", "channel_type": "channel", "team": "T1", "user": "U1",
                                  "text": text, "ts": "10.9", "thread_ts": "10.1"},
                                 self.say, self.client, Mock())

    def test_thumbs_down_asks_and_the_reply_is_feedback(self) -> None:
        self.react()
        self.api.record_feedback.assert_called_once()
        self.assertEqual(self.api.record_feedback.call_args.kwargs["session_key"], "slack-T1-thread-10.1")
        ask = self.client.chat_postMessage.call_args.kwargs
        self.assertEqual((ask["channel"], ask["thread_ts"]), ("C1", "10.1"))
        self.assertIn(fb.FEEDBACK_ASK, ask["text"])
        self.message("it was the March run, not this one")  # no @mention needed
        kwargs = self.api.chat.call_args.kwargs
        self.assertEqual((kwargs["feedback"], kwargs["session_key"]), ("down", "slack-T1-thread-10.1"))
        # Only the next message is a note.
        self.message("thanks")
        self.assertEqual(self.api.chat.call_count, 1)

    def test_other_reactions_and_messages_are_ignored(self) -> None:
        self.react(reaction="+1")
        self.react(item_user="USOMEONE")
        self.client.conversations_replies.return_value = {"messages": [{"ts": "10.5"}]}  # a top-level post
        self.react()
        self.api.record_feedback.assert_not_called()
        self.client.chat_postMessage.assert_not_called()


class DiscordReactionTest(unittest.IsolatedAsyncioTestCase):
    async def test_thumbs_down_asks_and_the_reply_is_feedback(self) -> None:
        from app.discord_bridge import DiscordBridgeClient

        me = SimpleNamespace(id=1, bot=True)
        sent: list[tuple[str, dict]] = []

        class Channel:
            id = 50
            guild = None

            async def send(self, text, **kwargs):
                sent.append((text, kwargs))
                return SimpleNamespace(id=900 + len(sent))

            def get_partial_message(self, message_id):
                return SimpleNamespace(to_reference=lambda **kw: ("ref", message_id))

        channel = Channel()
        recorded: list[dict] = []
        queued: list = []

        async def record_feedback(**kwargs):
            recorded.append(kwargs)
            return {"ok": True}

        bot = SimpleNamespace(
            user=me, api=SimpleNamespace(record_feedback=record_feedback),
            answers=fb.AnswerLedger(), pending_feedback=fb.PendingFeedback(),
            safe_mentions=None, get_channel=lambda cid: channel, _channel_allowed=lambda ch: True,
            _actor_id=lambda uid: str(uid), _user_key=lambda uid: f"discord-user-{uid}",
        )
        answer = {"session_key": "discord-dm-user-7", "memory_scope": "private", "target_user_id": None,
                  "channel_kind": "dm", "channel_label": ""}
        bot.answers.add(800, answer)

        def payload(emoji: str, message_id: int = 800):
            return SimpleNamespace(user_id=7, emoji=emoji, message_author_id=1, member=None,
                                   channel_id=50, message_id=message_id, guild_id=None)

        await DiscordBridgeClient.on_raw_reaction_add(bot, payload("👍"))
        self.assertEqual(sent, [])
        await DiscordBridgeClient.on_raw_reaction_add(bot, payload("👎"))
        self.assertEqual(recorded[0]["session_key"], "discord-dm-user-7")
        self.assertEqual(sent[0][0], fb.FEEDBACK_ASK)

        # The person's next DM is the note, sent into the answer's session.
        async def enqueue(request):
            queued.append(request)

        bot._enqueue_chat_request = enqueue
        message = SimpleNamespace(channel=channel, guild=None, reference=None, mentions=[],
                                  author=SimpleNamespace(id=7, display_name="Sam"))
        pending = DiscordBridgeClient._pending_feedback_for(bot, message)
        self.assertIsNotNone(pending)
        bot.pending_feedback.take(50, 7, 0)  # (on_message takes it before handling)
        await DiscordBridgeClient._handle_feedback_note(bot, message, "wrong cluster", pending)
        self.assertEqual((queued[0].feedback, queued[0].session_key, queued[0].message_text),
                         ("down", "discord-dm-user-7", "wrong cluster"))


if __name__ == "__main__":
    unittest.main()
