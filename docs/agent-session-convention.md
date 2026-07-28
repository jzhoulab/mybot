# Agent-session convention

## Problem

When a tool launches an AI coding agent (Claude Code, Codex) with **no human at
the keyboard** — an orchestrator spawning worker agents
in git worktrees — the resulting session is, by every field the CLIs record,
**indistinguishable from a human session**:

| field          | human session | launched agent |
|----------------|---------------|--------------------|
| `entrypoint`   | `cli`         | `cli`              |
| `promptSource` | `typed`       | `typed`            |
| `origin`       | `{kind: human}` | `{kind: human}`  |
| `permissionMode` | `bypassPermissions` | `bypassPermissions` |
| `userType`     | `external`    | `external`         |

The CLI genuinely cannot tell that the "typed" prompt was injected by a program.
So mybot mis-indexed these agent runs as the owner's own interactive work,
polluting retrieval — worker runs showed up as if the user did them.

## Convention

A launcher that starts an agent session non-interactively **prepends one marker
line to the agent's initial prompt**:

```
<!-- agent-session: launcher=<tool> origin=<cluster> -->
```

- It is an HTML comment: invisible in rendered markdown, harmless in raw text.
- It is recorded durably in the trajectory `.jsonl` (it is message content), so it
  survives worktree cleanup — unlike a marker file.
- It is tool-agnostic: any orchestrator adopts the same line.

Attributes:

- `launcher` — free-form tool name, e.g. `agentctl`. For provenance/debugging.
- `origin` — the automated cluster this session belongs to. Use `orchestrated`
  for dispatcher-launched worker agents (default if omitted). Other recognized
  clusters: `subagent`, `sdk`, `exec`, `no-user-turns`.

### Example

When a dispatcher launches a worker agent, the first prompt it sends becomes:

```
<!-- agent-session: launcher=agentctl origin=orchestrated -->
Fix the failing test in ./src/parser.ts (you are in a git worktree…)
```

## How mybot uses it

`sources/origin.py`:

- `parse_agent_marker(text)` → `{launcher, origin}` or `None`.
- `classify_claude_origin_detailed(...)` / `classify_codex_origin(...)` take an
  `agent_marker` argument and, when present, classify the session
  `automated` with `origin_detail = origin` **before** any other heuristic — it is
  the strongest, most explicit signal.

The adapters (`sources/claude.py`, `sources/codex.py`) and the admin `classify`
backfill scan early user turns for the marker. `<!-- agent-session:` is also in
`AUTO_PREFIXES` so a marker-led turn is never counted as a genuine human turn.

Sessions that predate the convention still fall back to the legacy heuristics
(`.agent-worktrees/`-style worktree paths, `codex exec`, sdk entrypoints, sidechain
transcripts, zero-human-turn sessions).

## Adopting it in another launcher

Prepend the marker line to whatever prompt you send as the agent's first message.
That's the whole integration — no mybot-side change needed.
