"""Which agent models exist right now — read from the CLIs' own catalogs, so
the menu's model picker and the server's defaults follow new releases
without a code change.

Claude: the CLI's aliases (fable/opus/sonnet) always resolve to the newest
model of each family, so they are listed as-is.
Codex: the CLI keeps the account's model list in ~/.codex/models_cache.json
(refreshed by Codex itself); the default is the model in ~/.codex/config.toml.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# Codex's "ultra" delegates to sub-agents on its own — not for a bot turn.
_SKIP_EFFORTS = {"ultra"}
FALLBACK_CODEX_MODEL = "gpt-5.5"


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def codex_config_model() -> str:
    try:
        text = (_codex_home() / "config.toml").read_text()
    except OSError:
        return ""
    match = re.search(r'(?m)^\s*model\s*=\s*"([^"]+)"', text)
    return match.group(1).strip() if match else ""


def codex_models(limit: int = 4) -> list[dict[str, Any]]:
    """The newest listed Codex models (by Codex's own priority), plus the
    configured one. Each: {slug, label, efforts}."""
    try:
        data = json.loads((_codex_home() / "models_cache.json").read_text())
    except (OSError, ValueError):
        data = {}
    listed = [
        m for m in (data.get("models") or [])
        if isinstance(m, dict) and m.get("slug") and m.get("visibility", "list") == "list"
    ]
    listed.sort(key=lambda m: m.get("priority", 999))
    chosen = listed[:limit]
    configured = codex_config_model()
    if configured and all(m["slug"] != configured for m in chosen):
        chosen += [m for m in listed if m["slug"] == configured] or [{"slug": configured}]
    out = []
    for m in chosen:
        efforts = [
            str(level.get("effort")) for level in m.get("supported_reasoning_levels") or []
            if isinstance(level, dict) and level.get("effort") and level.get("effort") not in _SKIP_EFFORTS
        ] or ["low", "medium", "high", "xhigh"]
        out.append({"slug": m["slug"], "label": str(m.get("display_name") or m["slug"]), "efforts": efforts})
    return out


def codex_default_model() -> str:
    """CODEX_MODEL if set, else what Codex itself is configured to use, else
    its top listed model."""
    explicit = os.environ.get("CODEX_MODEL", "").strip()
    if explicit:
        return explicit
    configured = codex_config_model()
    if configured:
        return configured
    models = codex_models(limit=1)
    return models[0]["slug"] if models else FALLBACK_CODEX_MODEL
