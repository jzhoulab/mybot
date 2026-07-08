"""Classify how a trajectory session was created.

`interactive` — a human was driving (Codex VSCode/TUI, Claude Code CLI).
`automated`   — AI-driven with no human in the loop: spawned subagents,
                codex exec, Claude SDK/-p, cron/headless runs. These are the
                sessions the user typically wants to exclude from memory.

Signals, strongest first (claude):
1. sidechain/agentId lines  → a spawned subagent transcript ("subagent").
   NOTE: subagent files claim entrypoint "cli", so entrypoint alone would
   misclassify them as interactive.
2. SDK/headless entrypoint  → programmatic ("sdk"). Its single user-shaped
   turn is the injected prompt, not typing (146/148 sampled sdk sessions
   have exactly one).
3. zero genuine human turns → nothing a person typed ("no-user-turns").
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


def classify_claude_origin_detailed(
    entrypoints: Any,
    *,
    sidechain: bool = False,
    human_turns: int | None = None,
) -> tuple[str, str]:
    """Return (origin, detail). detail explains WHY a session is automated."""
    if sidechain:
        return "automated", "subagent"
    for entrypoint in entrypoints or []:
        if str(entrypoint).strip().lower() in AUTOMATED_CLAUDE_ENTRYPOINTS:
            return "automated", "sdk"
    if human_turns is not None and human_turns == 0:
        return "automated", "no-user-turns"
    return "interactive", ""


def classify_claude_origin(
    entrypoints: Any,
    *,
    sidechain: bool = False,
    human_turns: int | None = None,
) -> str:
    return classify_claude_origin_detailed(
        entrypoints, sidechain=sidechain, human_turns=human_turns
    )[0]


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
