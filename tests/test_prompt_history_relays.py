"""Prompt history keeps what other agents sent through hop apart from what the
owner typed: both are in the CLI's prompt log, but they are not the same."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.prompt_history import PromptHistoryStore


class AgentRelayTest(unittest.TestCase):
    def test_owner_words_rank_first_on_a_tie_and_relays_are_marked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / "history.jsonl"
            rows = [
                {"display": '[hop message from session "angler" (a Claude agent, not your user)] randi or dsi?',
                 "timestamp": 1795737448000, "project": "/w", "sessionId": "s-1"},
                {"display": "roght now only randi option is open we will use dsi when it is isable",
                 "timestamp": 1795737829000, "project": "/w", "sessionId": "s-1"},
            ]
            history.write_text("".join(json.dumps(row) + "\n" for row in rows))
            store = PromptHistoryStore(str(Path(tmp) / "ph.sqlite3"))
            store.refresh(history_paths={"claude": [str(history)]})
            hits = store.search("randi dsi")
            self.assertEqual([(hit["text"][:5], hit["from_agent"]) for hit in hits],
                             [("roght", False), ("[hop ", True)])


if __name__ == "__main__":
    unittest.main()
