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


if __name__ == "__main__":
    unittest.main()
