#!/usr/bin/env python3
"""Build and search a lightweight trajectory-memory bundle from registered local sources."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sources.registry import get_source_adapters, list_source_names


TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]{3,}")


@dataclass
class TrajectorySession:
    tool: str
    session_id: str
    title: str
    file_path: str
    updated_at: str
    user_turns: list[str]
    assistant_turns: list[str]
    short_summary: str
    detailed_summary: str
    search_text: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(text: str, limit: int = 220) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text or "")]


def collect_sessions(
    max_files_per_tool: int = 200,
    source_names: list[str] | None = None,
) -> list[TrajectorySession]:
    sessions: list[TrajectorySession] = []
    for adapter in get_source_adapters(source_names):
        for session in adapter.discover_sessions(max_files=max_files_per_tool):
            sessions.append(
                TrajectorySession(
                    tool=session.source_name,
                    session_id=session.session_id,
                    title=session.title,
                    file_path=session.file_path,
                    updated_at=session.updated_at,
                    user_turns=session.user_turns[-8:],
                    assistant_turns=session.assistant_turns[-5:],
                    short_summary=session.short_summary,
                    detailed_summary=session.detailed_summary,
                    search_text=session.search_text,
                )
            )
    sessions.sort(key=lambda session: session.updated_at, reverse=True)
    return sessions


def build_index(
    *,
    max_files_per_tool: int = 200,
    recent_limit: int = 8,
    older_limit: int = 32,
    source_names: list[str] | None = None,
) -> dict[str, Any]:
    sessions = collect_sessions(max_files_per_tool=max_files_per_tool, source_names=source_names)
    counts = {name: 0 for name in list_source_names()}
    for session in sessions:
        counts[session.tool] = counts.get(session.tool, 0) + 1

    recent_sessions = sessions[:recent_limit]
    older_sessions = sessions[recent_limit : recent_limit + older_limit]

    return {
        "generated_at": utc_now(),
        "counts": {
            "total": len(sessions),
            "by_tool": counts,
        },
        "recent_sessions": [asdict(session) for session in recent_sessions],
        "older_sessions": [asdict(session) for session in older_sessions],
    }


def write_index(index: dict[str, Any], output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as handle:
        json.dump(index, handle, indent=2)


def load_index(path: str) -> dict[str, Any]:
    with open(path) as handle:
        return json.load(handle)


def score_session(session: dict[str, Any], query_tokens: list[str], is_recent: bool) -> float:
    haystack = (session.get("search_text") or "").lower()
    title = (session.get("title") or "").lower()
    if not query_tokens:
        return 1.0 if is_recent else 0.2

    score = 0.0
    for token in query_tokens:
        if token in title:
            score += 5.0
        score += haystack.count(token)
    if is_recent:
        score += 0.75
    return score


def search_index(index: dict[str, Any], query: str, limit: int = 5) -> list[dict[str, Any]]:
    query_tokens = tokenize(query)
    ranked: list[tuple[float, dict[str, Any]]] = []

    for session in index.get("recent_sessions", []):
        score = score_session(session, query_tokens, is_recent=True)
        if score > 0:
            ranked.append((score, session))
    for session in index.get("older_sessions", []):
        score = score_session(session, query_tokens, is_recent=False)
        if score > 0:
            ranked.append((score, session))

    ranked.sort(key=lambda item: (item[0], item[1].get("updated_at", "")), reverse=True)
    results = []
    for score, session in ranked[:limit]:
        result = dict(session)
        result["match_score"] = round(score, 2)
        results.append(result)
    return results


def render_prompt(index: dict[str, Any], query: str = "", match_limit: int = 4, older_catalog_limit: int = 8) -> str:
    counts = index.get("counts", {})
    by_tool = counts.get("by_tool", {})
    sections = [
        "Trajectory memory is available from prior local agent sessions.",
        (
            "Global overview: "
            f"{counts.get('total', 0)} total sessions "
            + ", ".join(f"{tool.title()} {count}" for tool, count in sorted(by_tool.items()))
            + "."
        ),
    ]

    if query:
        sections.append(f"Current user query for memory matching: {query}")
        matches = search_index(index, query, limit=match_limit)
        if matches:
            sections.append("Potentially relevant prior trajectories:")
            for session in matches:
                sections.append(
                    f"- [{session['tool']}][{session['updated_at'][:10]}] "
                    f"{session['title']}: {session['short_summary']}"
                )

    recent_sessions = index.get("recent_sessions", [])
    if recent_sessions:
        sections.append("Recent trajectories with more detail:")
        for session in recent_sessions:
            sections.append(
                f"- [{session['tool']}][{session['updated_at'][:10]}] "
                f"{session['detailed_summary']}"
            )

    older_sessions = index.get("older_sessions", [])[:older_catalog_limit]
    if older_sessions:
        sections.append("Older trajectory catalog:")
        for session in older_sessions:
            sections.append(
                f"- [{session['tool']}][{session['updated_at'][:10]}] "
                f"{session['title']}: {session['short_summary']}"
            )

    sections.append(
        "Treat these summaries as hints, not ground truth. If exact provenance matters, inspect the source trajectory file."
    )
    return "\n".join(sections)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Build a trajectory-memory index")
    build_parser.add_argument("--output", required=True, help="Path to write the JSON index")
    build_parser.add_argument("--max-files-per-tool", type=int, default=200)
    build_parser.add_argument("--recent-limit", type=int, default=8)
    build_parser.add_argument("--older-limit", type=int, default=32)
    build_parser.add_argument("--sources", default="codex,claude")

    search_parser = subparsers.add_parser("search", help="Search an existing trajectory-memory index")
    search_parser.add_argument("--index", required=True)
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--limit", type=int, default=5)

    prompt_parser = subparsers.add_parser("prompt", help="Render a prompt supplement from an index")
    prompt_parser.add_argument("--index", required=True)
    prompt_parser.add_argument("--query", default="")
    prompt_parser.add_argument("--match-limit", type=int, default=4)
    prompt_parser.add_argument("--older-catalog-limit", type=int, default=8)

    args = parser.parse_args()

    if args.command == "build":
        index = build_index(
            max_files_per_tool=args.max_files_per_tool,
            recent_limit=args.recent_limit,
            older_limit=args.older_limit,
            source_names=[name.strip() for name in args.sources.split(",") if name.strip()],
        )
        write_index(index, args.output)
        print(json.dumps(index["counts"], indent=2))
        return

    if args.command == "search":
        results = search_index(load_index(args.index), args.query, limit=args.limit)
        print(json.dumps(results, indent=2))
        return

    if args.command == "prompt":
        print(
            render_prompt(
                load_index(args.index),
                query=args.query,
                match_limit=args.match_limit,
                older_catalog_limit=args.older_catalog_limit,
            )
        )
        return

    raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
