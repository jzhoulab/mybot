"""Classify how a trajectory session was created.

`interactive` — a human was driving (Codex VSCode/TUI, Claude Code CLI).
`automated`   — agent/headless/exec driven (codex exec, Claude SDK/-p), which the
                user typically wants to exclude from the memory index.
"""

from __future__ import annotations

from typing import Any

AUTOMATED_CLAUDE_ENTRYPOINTS = {"sdk-cli", "sdk-py", "sdk-ts", "sdk", "headless", "print"}


def classify_codex_origin(source: str, originator: str) -> str:
    src = (source or "").strip().lower()
    orig = (originator or "").strip().lower()
    if src == "exec" or "exec" in orig:
        return "automated"
    return "interactive"


def classify_claude_origin(entrypoints: Any) -> str:
    for entrypoint in entrypoints or []:
        if str(entrypoint).strip().lower() in AUTOMATED_CLAUDE_ENTRYPOINTS:
            return "automated"
    return "interactive"


def classify_from_metadata(source_name: str, metadata: dict[str, Any]) -> str:
    """Best-effort origin from indexed chunk metadata (no file read).

    Returns "" when it can't be determined without the source file (codex has no
    origin signal in older metadata).
    """
    origin = str(metadata.get("origin") or "").strip().lower()
    if origin in {"interactive", "automated"}:
        return origin
    if source_name == "claude":
        return classify_claude_origin(metadata.get("entrypoints"))
    return ""
