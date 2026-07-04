#!/usr/bin/env python3
"""Retrieval regression harness: check that known questions still rank the
right session first.

Test cases are personal by nature — they name your own projects and pin real
session ids — so they live in an untracked file rather than in this script.
Copy `scripts/demo_queries.example.json` to `scripts/demo_queries.local.json`
and fill it in with questions whose answers you know.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.server import AppState, load_config, parse_args as parse_server_args, trajectory_rank_score  # noqa: E402


DEFAULT_CASES_PATH = ROOT / "scripts" / "demo_queries.local.json"
EXAMPLE_CASES_PATH = ROOT / "scripts" / "demo_queries.example.json"


def load_cases(path: Path) -> list[dict]:
    """Read test cases, explaining how to create the file if it is missing."""
    if not path.exists():
        raise SystemExit(
            f"No retrieval test cases at {path}.\n"
            f"Copy the example and edit it:\n"
            f"  cp {EXAMPLE_CASES_PATH.relative_to(ROOT)} {path.relative_to(ROOT)}\n"
            f"Each case needs a short `name`, the `query` to ask, and the "
            f"`expected` source_ref (e.g. \"codex:<session-id>\") that should "
            f"rank first. Find source_refs with POST /trajectory/search."
        )
    loaded = json.loads(path.read_text(encoding="utf-8"))
    cases = loaded.get("cases") if isinstance(loaded, dict) else loaded
    if not isinstance(cases, list) or not cases:
        raise SystemExit(f"{path} contains no cases.")
    for case in cases:
        missing = {"name", "query", "expected"} - set(case)
        if missing:
            raise SystemExit(f"{path}: case {case!r} is missing {sorted(missing)}.")
    return cases


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help=f"JSON file of retrieval test cases (default: {DEFAULT_CASES_PATH.name})",
    )
    args = parser.parse_args()

    demo_queries = load_cases(args.cases)

    load_dotenv(ROOT / ".env")
    original_argv = sys.argv[:]
    try:
        sys.argv = [sys.argv[0]]
        server_args = parse_server_args()
    finally:
        sys.argv = original_argv
    state = AppState(load_config(server_args))
    actor_id = state.config.imported_owner_actor_id
    failures = 0

    for item in demo_queries:
        trace: list[dict] = []
        matches = state.trajectory_agentic_search(
            query=item["query"],
            actor_id=actor_id,
            memory_scope="private",
            target_user_id=None,
            limit=args.limit,
            seed_sources=None,
            trace=trace if args.trace else None,
        )
        top_ref = matches[0]["source_ref"] if matches else ""
        ok = top_ref == item["expected"]
        failures += 0 if ok else 1
        print(f"\n[{item['name']}] {'OK' if ok else 'MISS'}")
        print(f"query: {item['query']}")
        print(f"expected: {item['expected']}")
        for index, match in enumerate(matches, start=1):
            rank = trajectory_rank_score(match, query=item["query"])
            raw = float(match.get("match_score") or match.get("score") or 0.0)
            print(
                f"{index}. rank={rank:.4f} raw={raw:.4f} "
                f"{match.get('source_ref')} | {match.get('title')} | "
                f"{','.join(match.get('_retrieval_channels') or [])}"
            )
        if args.trace:
            for step in trace:
                top = ", ".join(
                    f"{entry.get('source_ref')}:{entry.get('rank_score')}"
                    for entry in (step.get("top") or [])[:2]
                )
                print(f"  trace {step.get('stage')}: {step.get('counts')} top=[{top}]")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
