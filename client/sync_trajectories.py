#!/usr/bin/env python3
"""Manual sync client for pushing local trajectories to a shared chatbot server."""

from __future__ import annotations

import argparse
import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any

from sources.models import utc_now
from sources.registry import get_source_adapters, list_source_names


def normalize_text(text: str, limit: int | None = None) -> str:
    collapsed = " ".join((text or "").split()).strip()
    if limit is None or len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def split_batches(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if size <= 0:
        return [items]
    return [items[index : index + size] for index in range(0, len(items), size)]


class SyncClient:
    def __init__(self, server: str, token: str, timeout_seconds: int) -> None:
        self.server = server.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url=f"{self.server}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Connection failed: {exc}") from exc
        parsed = json.loads(raw)
        if not parsed.get("ok", False):
            raise RuntimeError(str(parsed.get("error") or "request failed"))
        return parsed

    def sync_status(self, *, actor_id: str, sources: list[str]) -> dict[str, Any]:
        return self.post_json(
            "/memory/sync-status",
            {
                "actor_id": actor_id,
                "sources": sources,
            },
        )

    def import_batch(
        self,
        *,
        actor_id: str,
        client_id: str,
        batch_id: str,
        source_name: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.post_json(
            "/memory/import-batch",
            {
                "actor_id": actor_id,
                "client_id": client_id,
                "batch_id": batch_id,
                "source_name": source_name,
                "items": items,
            },
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="Push local trajectories to the shared server")
    sync_parser.add_argument("--server", required=True, help="Base URL of the shared server")
    sync_parser.add_argument("--actor-id", required=True, help="Discord actor id bound to the sync token")
    sync_parser.add_argument("--sources", default="codex,claude", help="Comma-separated source adapters to sync")
    sync_parser.add_argument("--token", default=os.environ.get("SYNC_API_TOKEN", ""), help="Bearer token for sync auth")
    sync_parser.add_argument("--client-id", default=socket.gethostname(), help="Identifier for this client machine")
    sync_parser.add_argument("--max-files-per-source", type=int, default=200)
    sync_parser.add_argument("--recent-limit", type=int, default=20)
    sync_parser.add_argument("--chunk-limit", type=int, default=4)
    sync_parser.add_argument("--batch-size", type=int, default=50)
    sync_parser.add_argument("--timeout-seconds", type=int, default=180)

    status_parser = subparsers.add_parser("status", help="Check server-side sync status for this actor")
    status_parser.add_argument("--server", required=True, help="Base URL of the shared server")
    status_parser.add_argument("--actor-id", required=True, help="Discord actor id bound to the sync token")
    status_parser.add_argument("--sources", default="codex,claude", help="Comma-separated source adapters to inspect")
    status_parser.add_argument("--token", default=os.environ.get("SYNC_API_TOKEN", ""), help="Bearer token for sync auth")
    status_parser.add_argument("--timeout-seconds", type=int, default=60)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.token.strip():
        raise SystemExit("A sync token is required. Pass --token or set SYNC_API_TOKEN.")

    sources = [name.strip().lower() for name in args.sources.split(",") if name.strip()]
    for source in sources:
        if source not in list_source_names():
            raise SystemExit(
                f"Unsupported source '{source}'. Available: {', '.join(list_source_names())}"
            )

    client = SyncClient(args.server, args.token.strip(), args.timeout_seconds)

    if args.command == "status":
        status = client.sync_status(actor_id=args.actor_id, sources=sources)
        print(json.dumps(status, indent=2))
        return

    now = utc_now()
    totals = {
        "ok": True,
        "actor_id": args.actor_id,
        "client_id": args.client_id,
        "sources": {},
    }
    for adapter in get_source_adapters(sources):
        sessions = adapter.discover_sessions(max_files=args.max_files_per_source)
        records = adapter.to_export_records(
            actor_id=args.actor_id,
            now=now,
            recent_limit=args.recent_limit,
            chunk_limit=args.chunk_limit,
        )
        batches = split_batches([record.to_payload() for record in records], args.batch_size)
        source_totals = totals["sources"].setdefault(
            adapter.source_name(),
            {
                "sessions_discovered": 0,
                "records_prepared": 0,
                "accepted": 0,
                "inserted": 0,
                "updated": 0,
                "unchanged": 0,
                "rejected": 0,
                "batch_count": 0,
            },
        )
        source_totals["sessions_discovered"] += len(sessions)
        source_totals["records_prepared"] += len(records)
        source_totals["batch_count"] += len(batches)
        for index, batch in enumerate(batches):
            batch_id = f"{args.client_id}:{adapter.source_name()}:{index}:{now}"
            result = client.import_batch(
                actor_id=args.actor_id,
                client_id=args.client_id,
                batch_id=batch_id,
                source_name=adapter.source_name(),
                items=batch,
            )
            source_totals["accepted"] += int(result.get("accepted", 0))
            source_totals["inserted"] += int(result.get("inserted", 0))
            source_totals["updated"] += int(result.get("updated", 0))
            source_totals["unchanged"] += int(result.get("unchanged", 0))
            source_totals["rejected"] += int(result.get("rejected", 0))
    print(json.dumps(totals, indent=2))


if __name__ == "__main__":
    main()
