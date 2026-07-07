#!/usr/bin/env python3
"""Server-independent admin CLI for the mybot trajectory index.

This is the write/query backend for the native menu-bar app. It talks straight
to the SQLite index and config/access.json — no running chat server required —
so the app never depends on (or waits on) the HTTP server.

All commands print a single JSON object to stdout.

    mybot_admin.py state
    mybot_admin.py exclude --source codex --kind workdir --value /path/to/proj
    mybot_admin.py include --source codex --kind workdir --value /path/to/proj [--reindex]
    mybot_admin.py maintenance --action refresh|embed|rebuild|compact|compact_embeddings
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_dotenv() -> None:
    """Populate os.environ from .env (without overriding real env) so the CLI
    sees the same config the server does."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_dotenv()

from app.trajectory_index import (  # noqa: E402
    EMBEDDED_WHERE,
    TrajectoryChunkIndex,
)
from sources.access import (  # noqa: E402
    account_patterns,
    load_access_config,
    normalize_visibility_mode,
    save_access_config,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _config() -> dict[str, Any]:
    def _int(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, str(default)))
        except ValueError:
            return default

    db_path = os.environ.get("TRAJECTORY_INDEX_DB_PATH")
    if not db_path:
        db_path = str(REPO_ROOT / "state" / "trajectory_index.sqlite3")
    memory_db = os.environ.get("MEMORY_DB_PATH") or str(
        REPO_ROOT / "state" / "semantic_memory.sqlite3"
    )
    sources = [
        name.strip().lower()
        for name in os.environ.get("TRAJECTORY_SOURCES", "codex,claude").split(",")
        if name.strip()
    ]
    return {
        "db_path": db_path,
        "memory_db_path": memory_db,
        "model_name": os.environ.get(
            "EMBEDDING_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2"
        ),
        "source_names": sources,
        "max_files_per_tool": _int("TRAJECTORY_MAX_FILES_PER_TOOL", 200),
        "chunk_chars": _int("TRAJECTORY_INDEX_CHUNK_CHARS", 4800),
        "overlap_chars": _int("TRAJECTORY_INDEX_OVERLAP_CHARS", 800),
        "refresh_max_sessions": _int("TRAJECTORY_INDEX_REFRESH_MAX_SESSIONS", 0),
        "known_projects_path": str(
            Path(db_path).resolve().parent / "known_projects.json"
        ),
    }


def _build_index(cfg: dict[str, Any]) -> TrajectoryChunkIndex:
    # Cheap: LocalEmbedder loads the model lazily, so this is model-free until we
    # actually embed.
    return TrajectoryChunkIndex(
        db_path=cfg["db_path"],
        model_name=cfg["model_name"],
        source_names=cfg["source_names"],
        max_files_per_tool=cfg["max_files_per_tool"],
        chunk_chars=cfg["chunk_chars"],
        overlap_chars=cfg["overlap_chars"],
    )


def _policy_predicate(config: Any):
    """Mirror server._session_allowed_by_policy without the server."""

    def is_allowed(source_name: str, session_id: str, cwd: str) -> bool:
        accounts = config.accounts_for_source(source_name)
        if not accounts:
            return False
        ref_id = session_id.split(":", 1)[1] if ":" in session_id else session_id
        return any(
            account.include_workdir(cwd)
            and not account.is_session_blocked(session_id, ref_id)
            for account in accounts
        )

    return is_allowed


# --------------------------------------------------------------------------- #
# Read-only state
# --------------------------------------------------------------------------- #
def _file_group_bytes(path: str) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(path + suffix)
        except OSError:
            continue
    return total


def _footprint(cfg: dict[str, Any]) -> dict[str, Any]:
    db = cfg["db_path"]
    total = embedded = 0
    text_estimate = embed_estimate = 0
    try:
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute("SELECT COUNT(*) FROM trajectory_chunks").fetchone()[0]
            embedded = conn.execute(
                f"SELECT COUNT(*) FROM trajectory_chunks WHERE {EMBEDDED_WHERE}"
            ).fetchone()[0]
            sample = conn.execute(
                "SELECT length(text) t, length(embedding_json) ej, length(embedding_blob) eb "
                "FROM trajectory_chunks ORDER BY id DESC LIMIT 300"
            ).fetchall()
        if sample and total:
            n = len(sample)
            avg_text = sum((r["t"] or 0) for r in sample) / n
            avg_embed = sum(((r["ej"] or 0) + (r["eb"] or 0)) for r in sample) / n
            text_estimate = int(avg_text * total)
            embed_estimate = int(avg_embed * total)
    except sqlite3.Error:
        pass
    return {
        "index_bytes": _file_group_bytes(db),
        "memory_bytes": _file_group_bytes(cfg["memory_db_path"]),
        "total_chunks": total,
        "embedded_chunks": embedded,
        "text_bytes_estimate": text_estimate,
        "embedding_bytes_estimate": embed_estimate,
        "max_files_per_tool": cfg["max_files_per_tool"],
    }


def _latest_source_mtime(config: Any) -> float:
    latest = 0.0
    import glob

    for source_accounts in config.sources.values():
        for account in source_accounts:
            for pattern in account_patterns(account):
                for path in glob.glob(os.path.expanduser(pattern), recursive=True):
                    try:
                        latest = max(latest, os.path.getmtime(path))
                    except OSError:
                        continue
    return latest


def _detect_new_projects(cfg: dict[str, Any], breakdown: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same baseline-then-flag logic as the server, sharing the same file."""
    path = Path(cfg["known_projects_path"])
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {"baselined": False, "projects": {}}
    except (OSError, json.JSONDecodeError):
        data = {"baselined": False, "projects": {}}
    projects = data.setdefault("projects", {})
    first_run = not data.get("baselined")
    changed = False
    for entry in breakdown:
        cwd = str(entry.get("cwd") or "").strip()
        if not cwd:
            continue
        source_name = str(entry.get("source_name") or "")
        key = f"{source_name}:{cwd}"
        sessions = int(entry.get("sessions") or 0)
        existing = projects.get(key)
        if existing is None:
            projects[key] = {
                "source_name": source_name,
                "cwd": cwd,
                "first_seen": _utc_now(),
                "sessions": sessions,
                "reviewed": bool(first_run),
            }
            changed = True
        elif existing.get("sessions") != sessions:
            existing["sessions"] = sessions
            changed = True
    if first_run:
        data["baselined"] = True
        changed = True
    if changed:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)
        except OSError:
            pass
    pending = [entry for entry in projects.values() if not entry.get("reviewed")]
    pending.sort(key=lambda item: str(item.get("first_seen") or ""), reverse=True)
    return pending


def cmd_state(_args: argparse.Namespace) -> dict[str, Any]:
    cfg = _config()
    config = load_access_config()
    index = _build_index(cfg)

    breakdown = index.project_breakdown(limit=1000)
    is_allowed = _policy_predicate(config)
    # Mark inclusion + which account owns each project.
    for entry in breakdown:
        cwd = str(entry.get("cwd") or "")
        source_name = str(entry.get("source_name") or "")
        entry["included"] = is_allowed(source_name, "", cwd)

    accounts: list[dict[str, Any]] = []
    for source_name, source_accounts in sorted(config.sources.items()):
        for account in source_accounts:
            payload = account.to_json()
            payload["source_name"] = source_name
            accounts.append(payload)

    freshness = index.freshness() if hasattr(index, "freshness") else {}
    latest_mtime = _latest_source_mtime(config)
    stale = False
    indexed_at = str(freshness.get("indexed_at") or "") if isinstance(freshness, dict) else ""

    return {
        "ok": True,
        "generated_at": _utc_now(),
        "config": {
            "db_path": cfg["db_path"],
            "config_path": str(getattr(config, "path", "")) or None,
            "max_files_per_tool": cfg["max_files_per_tool"],
        },
        "footprint": _footprint(cfg),
        "freshness": freshness if isinstance(freshness, dict) else {},
        "latest_source_file_mtime": latest_mtime,
        "accounts": accounts,
        "projects": breakdown,
        "new_projects": _detect_new_projects(cfg, breakdown),
    }


# --------------------------------------------------------------------------- #
# Scope mutations (mirror server.update_scope, server-free)
# --------------------------------------------------------------------------- #
def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _mutate_scope(action: str, source_name: str, kind: str, values: Any, account_name: str) -> dict[str, Any]:
    config = load_access_config()
    source_accounts = config.sources.get(source_name) or []
    if not source_accounts:
        return {"ok": False, "error": f"unknown source '{source_name}'"}
    account = next((a for a in source_accounts if a.name == account_name), source_accounts[0])

    if action == "set_visibility":
        mode = (_as_list(values)[:1] or [""])[0]
        account.visibility_mode = normalize_visibility_mode(mode)
    elif action in {"exclude", "include"}:
        field_by_kind = {
            "workdir": account.excluded_workdirs,
            "session": account.excluded_session_ids,
            "workdir_class": account.excluded_workdir_classes,
            "entrypoint": account.excluded_entrypoints,
        }
        target = field_by_kind.get(kind)
        if target is None:
            return {"ok": False, "error": f"unknown kind '{kind}'"}
        cleaned = [str(v).strip() for v in _as_list(values) if str(v).strip()]
        if not cleaned:
            return {"ok": False, "error": "value is required"}
        for value in cleaned:
            normalized = value if kind in {"workdir", "session"} else value.lower()
            if kind == "session" and ":" in normalized:
                normalized = normalized.split(":", 1)[1]
            if action == "exclude":
                if normalized not in target:
                    target.append(normalized)
            else:
                lowered = normalized.lower()
                target[:] = [item for item in target if item.lower() != lowered]
    else:
        return {"ok": False, "error": f"unknown action '{action}'"}

    save_access_config(config)
    return {"ok": True, "config": config}


def cmd_exclude(args: argparse.Namespace) -> dict[str, Any]:
    result = _mutate_scope("exclude", args.source.strip().lower(), args.kind, args.value, args.account)
    if not result.get("ok"):
        return result
    cfg = _config()
    index = _build_index(cfg)
    purge = index.purge_disallowed(_policy_predicate(result["config"]))
    return {"ok": True, "action": "exclude", "count": len(_as_list(args.value)), "purge": purge}


def cmd_include(args: argparse.Namespace) -> dict[str, Any]:
    result = _mutate_scope("include", args.source.strip().lower(), args.kind, args.value, args.account)
    if not result.get("ok"):
        return result
    out: dict[str, Any] = {"ok": True, "action": "include", "count": len(_as_list(args.value)), "reindexed": False}
    if args.reindex:
        cfg = _config()
        index = _build_index(cfg)
        out["refresh"] = index.refresh_changed(
            include_vectors=True, max_sessions=cfg["refresh_max_sessions"]
        )
        out["reindexed"] = True
    return out


def cmd_set_visibility(args: argparse.Namespace) -> dict[str, Any]:
    result = _mutate_scope("set_visibility", args.source.strip().lower(), "", args.value, args.account)
    if not result.get("ok"):
        return result
    cfg = _config()
    index = _build_index(cfg)
    purge = index.purge_disallowed(_policy_predicate(result["config"]))
    return {"ok": True, "action": "set_visibility", "mode": args.value, "purge": purge}


def cmd_maintenance(args: argparse.Namespace) -> dict[str, Any]:
    cfg = _config()
    index = _build_index(cfg)
    action = args.action
    if action == "refresh":
        return {"ok": True, "action": action, "result": index.refresh_changed(
            include_vectors=True, max_sessions=cfg["refresh_max_sessions"])}
    if action == "rebuild":
        return {"ok": True, "action": action, "result": index.rebuild(include_vectors=True)}
    if action == "embed":
        total = 0
        for _ in range(400):
            updated = int(index.backfill_vectors(limit=512).get("updated_chunks") or 0)
            total += updated
            if updated == 0:
                break
        return {"ok": True, "action": action, "result": {"updated_chunks_total": total}}
    if action == "compact_embeddings":
        return {"ok": True, "action": action, "result": index.compact_embeddings()}
    if action == "compact":
        return {"ok": True, "action": action, "result": index.vacuum()}
    return {"ok": False, "error": f"unknown action '{action}'"}


def cmd_review(args: argparse.Namespace) -> dict[str, Any]:
    """Mark a detected project reviewed; optionally exclude it in the same call."""
    cfg = _config()
    key = f"{args.source.strip().lower()}:{args.value.strip()}"
    path = Path(cfg["known_projects_path"])
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {"baselined": True, "projects": {}}
    entry = data.setdefault("projects", {}).get(key)
    if entry is not None:
        entry["reviewed"] = True
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)
        except OSError:
            pass
    out: dict[str, Any] = {"ok": True, "decision": args.decision, "key": key}
    if args.decision == "exclude":
        out["scope_update"] = cmd_exclude(
            argparse.Namespace(source=args.source, kind="workdir", value=args.value, account=args.account)
        )
    return out


# --------------------------------------------------------------------------- #
# Trajectory viewer
# --------------------------------------------------------------------------- #
def _stringify(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return str(obj)


def _trunc(text: str, cap: int = 8000) -> str:
    text = text or ""
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n… (+{len(text) - cap:,} more chars)"


def _push(events: list[dict[str, Any]], kind: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    events.append({"kind": kind, "tool": "", "text": _trunc(text)})


TRAJECTORY_MAX_LINE_BYTES = 512 * 1024      # never buffer a single line bigger than this
TRAJECTORY_MAX_TOTAL_BYTES = 48 * 1024 * 1024  # stop after reading this much of the file


def _iter_trajectory_lines(path: str):
    """Yield decoded json-line strings with bounded memory.

    A single event can be hundreds of MB (embedded files/base64). Reading such a
    line with `for line in f` would load it whole and hang the viewer. Here we
    read in bounded chunks: any line over TRAJECTORY_MAX_LINE_BYTES is skipped
    (yielding None so the caller can note it), and we stop after
    TRAJECTORY_MAX_TOTAL_BYTES total.
    """
    total = 0
    with open(path, "rb") as handle:
        while True:
            raw = handle.readline(TRAJECTORY_MAX_LINE_BYTES)
            if not raw:
                break
            total += len(raw)
            if total > TRAJECTORY_MAX_TOTAL_BYTES:
                break
            if not raw.endswith(b"\n") and len(raw) >= TRAJECTORY_MAX_LINE_BYTES:
                # oversized line — drain to the next newline without buffering it
                while True:
                    extra = handle.readline(TRAJECTORY_MAX_LINE_BYTES)
                    total += len(extra)
                    if not extra or extra.endswith(b"\n") or total > TRAJECTORY_MAX_TOTAL_BYTES:
                        break
                yield None
                continue
            yield raw.decode("utf-8", "replace")


def _parse_codex_event(entry: dict[str, Any], events: list[dict[str, Any]]) -> None:
    etype = entry.get("type")
    payload = entry.get("payload", {})
    if etype in ("session_meta", "turn_context"):
        return  # hidden: metadata
    if etype == "event_msg" and payload.get("type") == "user_message":
        _push(events, "user", payload.get("message", ""))
    elif etype == "response_item":
        rtype = payload.get("type")
        role = payload.get("role")
        if rtype == "message" and role == "assistant":
            text = "\n".join(
                b.get("text", "") for b in payload.get("content", [])
                if isinstance(b, dict) and b.get("type") == "output_text"
            )
            _push(events, "assistant", text)
        elif rtype == "message" and role == "user":
            text = "".join(
                b.get("text", "") for b in payload.get("content", [])
                if isinstance(b, dict) and b.get("type") == "input_text"
            )
            _push(events, "user", text)
        elif rtype == "reasoning":
            blocks = payload.get("summary") or payload.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
            _push(events, "thinking", text)
        elif rtype in ("function_call", "custom_tool_call", "local_shell_call"):
            name = payload.get("name") or rtype.replace("_", " ")
            args = payload.get("arguments")
            if args is None:
                args = payload.get("action") or payload.get("input")
            events.append({"kind": "tool_use", "tool": str(name), "text": _trunc(_stringify(args))})
        elif rtype in ("function_call_output", "custom_tool_call_output"):
            out = payload.get("output")
            if isinstance(out, dict) and "output" in out:
                out = out["output"]
            events.append({"kind": "tool_result", "tool": "output", "text": _trunc(_stringify(out))})


def _parse_claude_event(entry: dict[str, Any], events: list[dict[str, Any]]) -> None:
    etype = entry.get("type")
    message = entry.get("message", {})
    if etype == "assistant" and isinstance(message, dict):
        for block in message.get("content", []):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                _push(events, "assistant", block.get("text", ""))
            elif btype == "thinking":
                _push(events, "thinking", block.get("thinking", "") or block.get("text", ""))
            elif btype == "tool_use":
                events.append({"kind": "tool_use", "tool": str(block.get("name", "tool")),
                               "text": _trunc(_stringify(block.get("input", {})))})
    elif etype == "user" and isinstance(message, dict):
        content = message.get("content", "")
        if isinstance(content, str):
            text = content.strip()
            if text.startswith("<system-reminder>") and text.endswith("</system-reminder>"):
                return  # hidden: injected reminder
            _push(events, "user", text)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    _push(events, "user", block.get("text", ""))
                elif btype == "tool_result":
                    events.append({"kind": "tool_result", "tool": "result",
                                   "text": _trunc(_stringify(block.get("content", "")))})


def _parse_trajectory(path: str, source: str, cap: int) -> tuple[list[dict[str, Any]], int]:
    """Bounded parse: skips oversized lines, stops at `cap` events. Returns
    (events, omitted_large_line_count)."""
    parse_event = _parse_claude_event if source == "claude" else _parse_codex_event
    events: list[dict[str, Any]] = []
    omitted = 0
    for line in _iter_trajectory_lines(path):
        if len(events) >= cap:
            break
        if line is None:
            omitted += 1
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        try:
            parse_event(entry, events)
        except Exception:
            continue
    return events, omitted


def cmd_trajectory(args: argparse.Namespace) -> dict[str, Any]:
    cfg = _config()
    ref = args.ref
    with sqlite3.connect(cfg["db_path"]) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT MAX(file_path) AS file_path, MAX(title) AS title, MAX(cwd) AS cwd "
            "FROM trajectory_chunks WHERE source_ref = ?",
            (ref,),
        ).fetchone()
    if not row or not row["file_path"]:
        return {"ok": False, "error": "session not found in index"}
    path = str(row["file_path"])
    if not os.path.exists(path):
        return {"ok": False, "error": f"trajectory file missing: {path}"}
    source = (args.source or ref.split(":", 1)[0]).strip().lower()
    events, omitted = _parse_trajectory(path, source, args.limit)
    return {
        "ok": True,
        "title": row["title"] or "",
        "cwd": row["cwd"] or "",
        "path": path,
        "source": source,
        "omitted_large": omitted,
        "events": events,
        "truncated": len(events) >= args.limit,
    }


def cmd_classify(_args: argparse.Namespace) -> dict[str, Any]:
    """Backfill session 'origin' into existing chunk metadata, embeddings intact.

    Claude is classified from indexed entrypoints (no file read); codex reads only
    the session_meta first line. Writes metadata_json.origin via json_set.
    """
    from sources.origin import classify_codex_origin, classify_from_metadata

    cfg = _config()
    counts = {"interactive": 0, "automated": 0, "unknown": 0}
    with sqlite3.connect(cfg["db_path"]) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT source_ref, MAX(source_name) AS sn, MAX(file_path) AS fp, MAX(metadata_json) AS mj "
            "FROM trajectory_chunks GROUP BY source_ref"
        ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["mj"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            origin = classify_from_metadata(str(row["sn"] or ""), metadata)
            if not origin and str(row["sn"] or "") == "codex" and row["fp"] and os.path.exists(row["fp"]):
                try:
                    with open(row["fp"]) as handle:
                        payload = json.loads(handle.readline()).get("payload", {})
                    src = payload.get("source")
                    origin = classify_codex_origin(
                        src if isinstance(src, str) else "", str(payload.get("originator") or "")
                    )
                except Exception:
                    origin = ""
            if not origin:
                counts["unknown"] += 1
                continue
            counts[origin] += 1
            conn.execute(
                "UPDATE trajectory_chunks SET metadata_json = json_set(metadata_json, '$.origin', ?) "
                "WHERE source_ref = ?",
                (origin, row["source_ref"]),
            )
        conn.commit()
    return {"ok": True, "sessions": len(rows), "counts": counts}


def cmd_automated(args: argparse.Namespace) -> dict[str, Any]:
    """Toggle exclusion of automated (agent/exec/SDK) sessions across all sources."""
    exclude = args.action == "exclude"
    config = load_access_config()
    for accounts in config.sources.values():
        for account in accounts:
            account.exclude_automated = exclude
    save_access_config(config)
    cfg = _config()
    index = _build_index(cfg)
    if exclude:
        cmd_classify(argparse.Namespace())  # ensure origin is populated first
        purge = index.purge_by_origin("automated")
        return {"ok": True, "action": "exclude", "purge": purge}
    result = index.refresh_changed(include_vectors=True, max_sessions=cfg["refresh_max_sessions"])
    return {"ok": True, "action": "include", "refresh": result,
            "note": "changed sessions re-index now; a full rebuild restores all automated history"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("state", help="dump index health, projects, and scope as JSON")

    for name in ("exclude", "include"):
        p = sub.add_parser(name)
        p.add_argument("--source", required=True)
        p.add_argument("--kind", default="workdir",
                       choices=["workdir", "session", "workdir_class", "entrypoint"])
        p.add_argument("--value", required=True, nargs="+", help="one or more values (batch)")
        p.add_argument("--account", default="default")
        if name == "include":
            p.add_argument("--reindex", action="store_true")

    pv = sub.add_parser("set_visibility")
    pv.add_argument("--source", required=True)
    pv.add_argument("--value", required=True, choices=["blacklist", "whitelist"])
    pv.add_argument("--account", default="default")

    pm = sub.add_parser("maintenance")
    pm.add_argument("--action", required=True,
                    choices=["refresh", "rebuild", "embed", "compact", "compact_embeddings"])

    pr = sub.add_parser("review")
    pr.add_argument("--source", required=True)
    pr.add_argument("--value", required=True)
    pr.add_argument("--decision", required=True, choices=["keep", "exclude"])
    pr.add_argument("--account", default="default")

    ptj = sub.add_parser("trajectory")
    ptj.add_argument("--ref", required=True, help="source_ref, e.g. codex:<session_id>")
    ptj.add_argument("--source", default="")
    ptj.add_argument("--limit", type=int, default=500)

    sub.add_parser("classify", help="backfill session origin into existing metadata")

    pa = sub.add_parser("automated", help="exclude/include automated (agent-driven) sessions")
    pa.add_argument("--action", required=True, choices=["exclude", "include"])

    args = parser.parse_args()
    handlers = {
        "state": cmd_state,
        "exclude": cmd_exclude,
        "include": cmd_include,
        "set_visibility": cmd_set_visibility,
        "maintenance": cmd_maintenance,
        "review": cmd_review,
        "trajectory": cmd_trajectory,
        "classify": cmd_classify,
        "automated": cmd_automated,
    }
    try:
        result = handlers[args.command](args)
    except Exception as exc:  # surface as JSON so the app can show it
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(1)
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
