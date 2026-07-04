from __future__ import annotations

from .claude import ClaudeSourceAdapter
from .codex import CodexSourceAdapter
from .access import load_access_config
from .models import TrajectorySourceAdapter


def _registry() -> dict[str, type[TrajectorySourceAdapter]]:
    return {
        "codex": CodexSourceAdapter,
        "claude": ClaudeSourceAdapter,
    }


def list_source_names() -> list[str]:
    return sorted(_registry())


def get_source_adapters(names: list[str] | None = None) -> list[TrajectorySourceAdapter]:
    registry = _registry()
    selected = list_source_names() if names is None else [name.strip().lower() for name in names if name.strip()]
    access_config = load_access_config()
    adapters: list[TrajectorySourceAdapter] = []
    for name in selected:
        factory = registry.get(name)
        if factory is None:
            raise ValueError(f"Unsupported source '{name}'. Available sources: {', '.join(list_source_names())}")
        for account in access_config.accounts_for_source(name):
            adapters.append(factory(account=account))
    return adapters
