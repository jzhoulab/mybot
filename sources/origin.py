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

import re
from typing import Any

AUTOMATED_CLAUDE_ENTRYPOINTS = {"sdk-cli", "sdk-py", "sdk-ts", "sdk", "headless", "print"}

# Agent orchestrators (agentctl dispatcher, etc.) run agents in dedicated git worktrees
# named like `.agent-worktrees/`, `.worktrees/`, `.agent-worktrees/`. A dot-prefixed
# "*worktrees" path segment is a strong "launched by an orchestrator" signal — the
# turns inside are injected task prompts, not a human typing, even at entrypoint=cli.
AGENT_WORKTREE_RE = re.compile(r"/\.[^/]*worktrees?/", re.IGNORECASE)


def is_agent_worktree(cwd: str | None) -> bool:
    return bool(cwd) and bool(AGENT_WORKTREE_RE.search(str(cwd)))


def classify_codex_origin(source: str, originator: str, cwd: str | None = None) -> str:
    if is_agent_worktree(cwd):
        return "automated"
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
    cwd: str | None = None,
) -> tuple[str, str]:
    """Return (origin, detail). detail explains WHY a session is automated."""
    if sidechain:
        return "automated", "subagent"
    # Orchestrated agent runs (worktrees) look interactive by entrypoint + have
    # "user" turns, but those turns are machine-injected tasks — check before them.
    if is_agent_worktree(cwd):
        return "automated", "orchestrated"
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
    cwd: str | None = None,
) -> str:
    return classify_claude_origin_detailed(
        entrypoints, sidechain=sidechain, human_turns=human_turns, cwd=cwd
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
