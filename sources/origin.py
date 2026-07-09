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

# --- Agent-session convention (the robust, explicit signal) --------------------
# A tool that launches an AI coding agent with NO human at the keyboard is
# otherwise indistinguishable from a human session (the CLIs log the injected
# prompt as `promptSource: typed`, `origin: {kind: human}`, `entrypoint: cli`).
# Convention: such a launcher prepends ONE marker line to the agent's initial
# prompt. It survives durably in the trajectory jsonl and is tool-agnostic:
#
#     <!-- agent-session: launcher=<tool> origin=orchestrated -->
#
# `origin` maps to an automated cluster (default "orchestrated"); `launcher` is
# free-form (e.g. hop). See docs/agent-session-convention.md. mybot detects this
# marker and both classifies the session automated AND strips it from indexed text.
AGENT_SESSION_MARKER_RE = re.compile(r"<!--\s*agent-session:\s*(.*?)\s*-->", re.IGNORECASE)


def parse_agent_marker(text: str | None) -> dict[str, str] | None:
    """Return {launcher, origin} if an agent-session marker is present, else None."""
    if not text:
        return None
    m = AGENT_SESSION_MARKER_RE.search(text)
    if not m:
        return None
    attrs = dict(re.findall(r"(\w+)\s*=\s*([^\s]+)", m.group(1)))
    return {"launcher": attrs.get("launcher", ""), "origin": attrs.get("origin", "orchestrated")}


# Legacy/fallback heuristic for sessions that predate the convention: agent
# orchestrators run agents in dedicated git worktrees (`.agent-worktrees/`, etc.).
AGENT_WORKTREE_RE = re.compile(r"/\.[^/]*worktrees?/", re.IGNORECASE)


def is_agent_worktree(cwd: str | None) -> bool:
    return bool(cwd) and bool(AGENT_WORKTREE_RE.search(str(cwd)))


def classify_codex_origin(
    source: str, originator: str, cwd: str | None = None,
    agent_marker: dict[str, str] | None = None,
) -> str:
    if agent_marker or is_agent_worktree(cwd):
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
    agent_marker: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return (origin, detail). detail explains WHY a session is automated."""
    # Explicit convention marker wins — the durable, tool-agnostic signal.
    if agent_marker:
        return "automated", (agent_marker.get("origin") or "orchestrated")
    if sidechain:
        return "automated", "subagent"
    # Fallback for pre-convention sessions: worktree path looks interactive by
    # entrypoint + has "user" turns, but those turns are machine-injected tasks.
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
