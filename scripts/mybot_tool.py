#!/usr/bin/env python3
"""Local mybot tool for agentic memory and trajectory lookup."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any


def post_json(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url=f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Connection failed: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON response: {raw[:500]}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("Expected a JSON object response")
    return parsed


def actor_payload(args: argparse.Namespace) -> dict[str, Any]:
    actor_id = (
        args.actor_id
        or os.environ.get("MYBOT_TOOL_ACTOR_ID")
        or os.environ.get("MEMORY_IMPORTED_OWNER_ID")
        or ""
    ).strip()
    if not actor_id:
        raise SystemExit("actor_id is required; pass --actor-id or set MYBOT_TOOL_ACTOR_ID")
    memory_scope = (args.memory_scope or os.environ.get("MYBOT_TOOL_MEMORY_SCOPE") or "private").strip()
    target_user_id = (args.target_user_id or os.environ.get("MYBOT_TOOL_TARGET_USER_ID") or "").strip()
    payload: dict[str, Any] = {
        "actor_id": actor_id,
        "memory_scope": memory_scope,
    }
    if target_user_id:
        payload["target_user_id"] = target_user_id
    return payload


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def estimate_tokens(value: Any) -> int:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value
    return max(1, round(len(text) / 4))


def result_count(result: dict[str, Any]) -> int:
    matches = result.get("matches")
    if isinstance(matches, list):
        return len(matches)
    if isinstance(result.get("trajectory"), dict):
        return 1
    return 0


def attach_budget(
    result: dict[str, Any],
    *,
    command: str,
    path: str,
    payload: dict[str, Any],
    started_at: float,
) -> dict[str, Any]:
    elapsed = time.monotonic() - started_at
    output_tokens = estimate_tokens(result)
    input_tokens = estimate_tokens(payload)
    budget = {
        "tool": command,
        "endpoint": path,
        "seconds": round(elapsed, 3),
        "tool_calls": 1,
        "tokens_estimate": input_tokens + output_tokens,
        "input_tokens_estimate": input_tokens,
        "output_tokens_estimate": output_tokens,
        "result_count": result_count(result),
    }
    trace = result.get("trace")
    if isinstance(trace, list):
        budget["retrieval_steps"] = len(trace)
    result["_budget"] = budget
    return result


def write_budget_log(budget: dict[str, Any], payload: dict[str, Any]) -> None:
    log_path = os.environ.get("MYBOT_TOOL_BUDGET_LOG", "").strip()
    if not log_path:
        return

    record = {
        **budget,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    query = payload.get("query")
    if isinstance(query, str) and query.strip():
        record["query"] = query[:300]
    source_ref = payload.get("source_ref")
    if isinstance(source_ref, str) and source_ref.strip():
        record["source_ref"] = source_ref[:300]

    try:
        directory = os.path.dirname(log_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    except OSError:
        return


def spent_seconds() -> float:
    """Total retrieval seconds already spent in THIS chat request (from the
    per-request budget log the server points us at)."""
    log_path = os.environ.get("MYBOT_TOOL_BUDGET_LOG", "").strip()
    if not log_path or not os.path.exists(log_path):
        return 0.0
    total = 0.0
    try:
        with open(log_path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    total += float(json.loads(line).get("seconds") or 0)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
    except OSError:
        return 0.0
    return total


def enforce_total_budget(command: str) -> None:
    """Stop runaway search loops: after the per-request budget is spent, tell
    the model to answer with the evidence it already has instead of searching
    again. Reads stay allowed — using found evidence is the goal."""
    if command not in ("memory-search", "trajectory-search"):
        return
    try:
        budget = float(os.environ.get("MYBOT_TOOL_TOTAL_BUDGET_SECONDS", "45"))
    except ValueError:
        budget = 45.0
    spent = spent_seconds()
    if spent <= budget:
        return
    print_json({
        "ok": False,
        "budget_exhausted": True,
        "error": (
            f"Retrieval budget exhausted ({spent:.0f}s spent of {budget:.0f}s). "
            "Do NOT search again. Answer the user now using the evidence already "
            "retrieved; if nothing relevant was found, say the memory has no "
            "grounded record of it."
        ),
    })
    raise SystemExit(0)


def call_and_print(args: argparse.Namespace, path: str, payload: dict[str, Any]) -> None:
    enforce_total_budget(args.command)
    started_at = time.monotonic()
    result = post_json(args.base_url, path, payload)
    result = attach_budget(
        result,
        command=args.command,
        path=path,
        payload=payload,
        started_at=started_at,
    )
    write_budget_log(result["_budget"], payload)
    print_json(result)


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=os.environ.get("MYBOT_TOOL_BASE_URL", "http://127.0.0.1:8788"))
    parser.add_argument("--actor-id", default="")
    parser.add_argument("--memory-scope", default="")
    parser.add_argument("--target-user-id", default="")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    memory_search = subparsers.add_parser("memory-search", help="Search semantic memory")
    memory_search.add_argument("-q", "--query", required=True)
    memory_search.add_argument("--limit", type=int, default=5)

    trajectory_search = subparsers.add_parser("trajectory-search", help="Search Codex and Claude trajectories")
    trajectory_search.add_argument("-q", "--query", required=True)
    trajectory_search.add_argument("--limit", type=int, default=5)
    trajectory_search.add_argument("--trace", action="store_true")

    trajectory_read = subparsers.add_parser("trajectory-read", help="Read focused evidence from a trajectory")
    trajectory_read.add_argument("--source-ref", required=True)
    trajectory_read.add_argument("-q", "--query", default="")
    trajectory_read.add_argument("--max-chars", type=int, default=12000)

    subparsers.add_parser("trajectory-stats", help="Inspect trajectory index stats")

    args = parser.parse_args()
    payload = actor_payload(args)

    if args.command == "memory-search":
        payload.update({"query": args.query, "limit": args.limit})
        call_and_print(args, "/memory/search", payload)
        return 0

    if args.command == "trajectory-search":
        payload.update({"query": args.query, "limit": args.limit, "return_trace": bool(args.trace)})
        call_and_print(args, "/trajectory/search", payload)
        return 0

    if args.command == "trajectory-read":
        payload.update({"source_ref": args.source_ref, "query": args.query, "max_chars": args.max_chars})
        call_and_print(args, "/trajectory/read", payload)
        return 0

    if args.command == "trajectory-stats":
        call_and_print(args, "/trajectory/index/stats", payload)
        return 0

    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
