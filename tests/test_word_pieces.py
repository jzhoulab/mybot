"""A word inside a path or joined name is found, as the FTS index finds it;
a session without a transcript says so instead of failing a read silently."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.prompt_history import PromptHistoryStore
from app.trajectory_index import TrajectoryChunkIndex
from sources.trajectory_lookup import text_tokens


def add_chunk(index: TrajectoryChunkIndex, ref: str, chunk_index: int, title: str, text: str) -> None:
    with index._connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO trajectory_chunks (source_ref, source_name, session_id, chunk_index, title, cwd,
                                           updated_at, text, content_hash)
            VALUES (?, ?, ?, ?, ?, '/w', '2026-11-26T21:40:50+00:00', ?, ?)
            """,
            (ref, ref.split(":")[0], ref.split(":")[1], chunk_index, title, text, f"{ref}-{chunk_index}"),
        )
        conn.execute(
            "INSERT INTO trajectory_chunks_fts(rowid, title, cwd, text) VALUES (?, ?, '/w', ?)",
            (cursor.lastrowid, title, text),
        )


class WordPiecesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "index.sqlite3")
        self.index = TrajectoryChunkIndex(db_path=self.db, model_name="unused", source_names=["codex"],
                                          max_files_per_tool=5)
        self.tmp = Path(tmp.name)

    def test_pieces_of_paths_and_joined_names_are_words_too(self) -> None:
        tokens = text_tokens("cd /Users/j/Code/orca-superres-1d2d/experiments/handoff.json; harish's run_name.")
        for word in ("orca-superres-1d2d", "1d2d", "orca", "superres", "experiments", "handoff.json",
                     "handoff", "json", "harish", "run_name", "run", "name"):
            self.assertIn(word, tokens)

    def test_a_word_inside_a_path_is_found(self) -> None:
        add_chunk(self.index, "antigravity:s-1", 3, "Handoff",
                  "USER: read /Users/j/Code/orca-superres-1d2d/experiments/compact-width-r2/HANDOFF.json")
        add_chunk(self.index, "codex:s-2", 0, "Other", "USER: protein folding with a 2d module")
        hits = self.index.search(query="1d2d", mode="exact")
        self.assertEqual([hit["source_ref"] for hit in hits], ["antigravity:s-1"])

    def test_a_follow_up_built_from_a_title_keeps_the_echo_penalty(self) -> None:
        # A session about mybot quotes the query; a refinement made from its title
        # ("why discord is needed?") must not lift the penalty on quoted mybot output.
        add_chunk(self.index, "claude:echo", 2, "why discord is needed?",
                  "Title: why discord is needed? Updated: 2026-11-27T14:20:00Z "
                  "USER: From mybot search feedback: Query: `harish protein` came up short")
        follow_up = "harish protein why discord is needed"
        exempt = self.index.search(query=follow_up, mode="exact")[0]["score"]
        kept = self.index.search(query=follow_up, mode="exact", intent_query="harish protein")[0]["score"]
        self.assertLess(kept, exempt * 0.5)


class TranscriptlessSessionTest(unittest.TestCase):
    def test_prompts_say_whether_a_transcript_exists_and_a_read_returns_them(self) -> None:
        from app.server import AppState

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "index.sqlite3")
            index = TrajectoryChunkIndex(db_path=db, model_name="unused", source_names=["codex"], max_files_per_tool=5)
            add_chunk(index, "codex:kept", 0, "t", "USER: 1d2d orca speed")
            history = Path(tmp) / "history.jsonl"
            history.write_text("".join(json.dumps(row) + "\n" for row in [
                {"session_id": "kept", "ts": 1795729000, "text": "has 1d2d orca overtaken the native now?"},
                {"session_id": "gone", "ts": 1795730000, "text": "have you launched 1d2d without diag conv?"},
                {"session_id": "gone", "ts": 1795731000, "text": "what was the answer to my zstd question"},
            ]))
            store = PromptHistoryStore(db)
            store.refresh(history_paths={"codex": [str(history)]})
            flags = {hit["source_ref"]: hit["transcript"] for hit in store.search("1d2d")}
            self.assertEqual(flags, {"codex:kept": True, "codex:gone": False})

            state = SimpleNamespace(prompt_history=store, visible_prompt_hits=lambda hits, viewer_id: hits)
            payload = AppState.missing_trajectory_payload(state, "codex:gone", viewer_id="owner")
            self.assertFalse(payload["ok"])
            self.assertIn("no transcript", payload["error"])
            self.assertEqual([hit["text"][:12] for hit in payload["prompt_history"]],
                             ["have you lau", "what was the"])
            bare = AppState.missing_trajectory_payload(state, "codex:never", viewer_id="owner")
            self.assertEqual(bare, {"ok": False, "error": "trajectory not found in allowed sources"})


if __name__ == "__main__":
    unittest.main()
