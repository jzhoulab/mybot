"""Filters written inside a search query become real filters."""

from __future__ import annotations

import unittest

from app.server import split_search_operators


class SearchOperatorsTest(unittest.TestCase):
    def test_inline_filters_are_lifted_out_of_the_query(self) -> None:
        self.assertEqual(
            split_search_operators("18358699 source:claude after:2026-11-25"),
            ("18358699", {"source": "claude", "after": "2026-11-25"}),
        )
        self.assertEqual(
            split_search_operators("deploy since:2026-11-01 until:2026-11-20T12:00:00Z Source:Codex"),
            ("deploy", {"after": "2026-11-01", "before": "2026-11-20T12:00:00Z", "source": "codex"}),
        )

    def test_ordinary_text_is_left_alone(self) -> None:
        # The last one: nothing would be left to search for, so it stays as typed.
        for query in ("ratio 3:1 after the run", "source:somewhere-else", "http://x/after:2026-11-25", "after:2026-11-25"):
            self.assertEqual(split_search_operators(query), (query, {}))


class MergeKeepsTheAnsweringChunkTest(unittest.TestCase):
    def test_a_title_refinement_does_not_replace_the_chunk_with_the_query_terms(self) -> None:
        from app.server import merge_trajectory_source

        by_ref: dict = {}
        answer = {"source_type": "trajectory", "source_ref": "claude:s", "score": 57.0, "_chunk_index": True,
                  "metadata": {"chunk_id": 2, "event_start": 19904},
                  "snippets": ["Job 18358699 is verified complete"]}
        opening = {"source_type": "trajectory", "source_ref": "claude:s", "score": 917.0, "_chunk_index": True,
                   "metadata": {"chunk_id": 1, "event_start": 0},
                   "snippets": ["can you use the nebula mcp to read the notebook"]}
        merge_trajectory_source(by_ref, answer, query="18358699", channel="initial")
        merge_trajectory_source(by_ref, opening, query="18358699", channel="refine:5")
        kept = by_ref["claude:s"]
        self.assertEqual((kept["metadata"]["chunk_id"], kept["metadata"]["event_start"], kept["score"]), (2, 19904, 917.0))
        self.assertEqual(kept["snippets"], ["Job 18358699 is verified complete"])

        # A better-scoring hit that also has the query's terms still wins.
        better = dict(answer, score=1000.0, metadata={"chunk_id": 3, "event_start": 19950},
                      snippets=["18358699 follow-through done"])
        merge_trajectory_source(by_ref, better, query="18358699", channel="refine:6")
        self.assertEqual(by_ref["claude:s"]["metadata"]["chunk_id"], 3)


if __name__ == "__main__":
    unittest.main()
