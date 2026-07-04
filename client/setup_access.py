#!/usr/bin/env python3
"""Step-by-step local access setup for Codex and Claude trajectory sources."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sources.access import (
    DEFAULT_BASE_DIRS,
    TrajectoryAccessAccount,
    TrajectoryAccessConfig,
    default_access_config,
    default_config_path,
    discover_default_roots,
    discover_entrypoint_counts,
    discover_workdir_counts,
    expand_path,
    load_access_config,
    normalize_visibility_mode,
    save_access_config,
)


def home_relative(path: str) -> str:
    home = os.path.expanduser("~")
    expanded = expand_path(path)
    if expanded == home:
        return "~"
    if expanded.startswith(home + os.sep):
        return "~" + expanded[len(home) :]
    return expanded


def prompt(message: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{message}{suffix}: ").strip()
    return value or default


def prompt_bool(message: str, default: bool = True) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        value = input(f"{message} [{default_text}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer y or n.")


def split_csv(raw: str) -> list[str]:
    return [chunk.strip() for chunk in raw.split(",") if chunk.strip()]


def append_unique(existing: list[str], additions: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in existing + additions:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def unique_root_name(source_name: str, existing: set[str], base_dir: str) -> str:
    base = "default" if home_relative(base_dir) == DEFAULT_BASE_DIRS[source_name] else Path(expand_path(base_dir)).name
    base = base or source_name
    name = base
    index = 2
    while name in existing:
        name = f"{base}-{index}"
        index += 1
    existing.add(name)
    return name


def merge_roots(source_name: str, existing: list[TrajectoryAccessAccount]) -> list[TrajectoryAccessAccount]:
    by_base = {home_relative(root.base_dir): root for root in existing}
    names = {root.name for root in existing}

    for root in discover_default_roots().get(source_name, []):
        key = home_relative(root)
        if key in by_base:
            continue
        by_base[key] = TrajectoryAccessAccount(
            source_name=source_name,
            name=unique_root_name(source_name, names, key),
            base_dir=key,
            enabled=True,
            excluded_workdirs=[],
        )
    return list(by_base.values())


def show_workdirs(root: TrajectoryAccessAccount, max_files: int) -> None:
    counts = discover_workdir_counts(root, max_files=max_files)
    if not counts:
        print("  No trajectory files found for this root.")
        return
    print("  Recent working directories:")
    for index, (workdir, count) in enumerate(counts.items()):
        if index >= 12:
            remaining = len(counts) - index
            if remaining > 0:
                print(f"  ... {remaining} more")
            break
        print(f"  {count:4d}  {workdir}")


def run_wizard(config_path: Path, max_files: int) -> Path:
    existing = load_access_config(config_path)
    next_sources: dict[str, list[TrajectoryAccessAccount]] = {}

    print("mybot trajectory access setup")
    print("Policy: each enabled root can use blacklist mode or whitelist mode.")
    print("The bot reads Codex and Claude trajectory JSONL files through their source adapters, not arbitrary files.\n")

    for source_name in ("codex", "claude"):
        print(f"Step: {source_name} roots")
        roots = merge_roots(source_name, existing.sources.get(source_name, []))
        default_root = DEFAULT_BASE_DIRS[source_name]

        while prompt_bool(
            f"Do you have another {source_name} root in addition to the default ({default_root})?",
            default=False,
        ):
            base_dir = prompt(f"Path to {source_name} trajectory root")
            if not base_dir:
                continue
            used_names = {root.name for root in roots}
            name = prompt("Short root name", unique_root_name(source_name, used_names, base_dir))
            roots.append(
                TrajectoryAccessAccount(
                    source_name=source_name,
                    name=name,
                    base_dir=home_relative(base_dir),
                    enabled=True,
                    excluded_workdirs=[],
                    excluded_entrypoints=[],
                )
            )

        if len(roots) > 1:
            print("Multiple roots are configured. Choose which ones mybot may scan.")
        selected: list[TrajectoryAccessAccount] = []
        for root in roots:
            exists = os.path.isdir(expand_path(root.base_dir))
            label = f"{root.name} ({home_relative(root.base_dir)})"
            if not exists:
                label += " [path not found]"
            root.enabled = prompt_bool(f"Enable {label}?", default=root.enabled and exists)

            if not root.enabled:
                selected.append(root)
                continue

            show_workdirs(root, max_files=max_files)
            mode = normalize_visibility_mode(
                prompt("Visibility mode: blacklist or whitelist", root.visibility_mode)
            )
            root.visibility_mode = mode

            if mode == "whitelist":
                if root.included_workdirs:
                    print("  Current included workdirs:")
                    for value in root.included_workdirs:
                        print(f"  - {value}")
                else:
                    print("  Current included workdirs: none")
                raw_included = prompt("Add included workdirs or path fragments, comma-separated. Blank keeps current")
                if raw_included:
                    root.included_workdirs = append_unique(root.included_workdirs, split_csv(raw_included))

                if root.included_workdir_classes:
                    print("  Current included classes:")
                    for value in root.included_workdir_classes:
                        print(f"  - {value}")
                else:
                    print("  Current included classes: none")
                raw_included_classes = prompt("Add included classes, comma-separated. Blank keeps current")
                if raw_included_classes:
                    root.included_workdir_classes = append_unique(
                        root.included_workdir_classes,
                        [value.lower() for value in split_csv(raw_included_classes)],
                    )
            else:
                if root.excluded_workdirs:
                    print("  Current exclusions:")
                    for value in root.excluded_workdirs:
                        print(f"  - {value}")
                else:
                    print("  Current exclusions: none")

                raw = prompt("Add exclusions, comma-separated. Blank keeps current")
                if raw:
                    root.excluded_workdirs = append_unique(root.excluded_workdirs, split_csv(raw))

                if root.excluded_workdir_classes:
                    print("  Current excluded classes:")
                    for value in root.excluded_workdir_classes:
                        print(f"  - {value}")
                else:
                    print("  Current excluded classes: none")
                if source_name == "codex":
                    print("  Useful Codex classes: codex_no_project, codex_project, codex_home_default, codex_documents_chat")
                raw_classes = prompt("Add excluded classes, comma-separated. Blank keeps current")
                if raw_classes:
                    root.excluded_workdir_classes = append_unique(
                        root.excluded_workdir_classes,
                        [value.lower() for value in split_csv(raw_classes)],
                    )

            if mode == "whitelist" and root.excluded_workdirs:
                print("  Current exclusions inside whitelist:")
                for value in root.excluded_workdirs:
                    print(f"  - {value}")
            if mode == "whitelist":
                raw = prompt("Add exclusions inside whitelist, comma-separated. Blank keeps current")
                if raw:
                    root.excluded_workdirs = append_unique(root.excluded_workdirs, split_csv(raw))

            if source_name == "claude":
                entrypoint_counts = discover_entrypoint_counts(root, max_files=max_files)
                if entrypoint_counts:
                    print("  Detected Claude entrypoints:")
                    for entrypoint, count in entrypoint_counts.items():
                        print(f"  {count:4d}  {entrypoint}")
                if root.excluded_entrypoints:
                    print("  Current entrypoint exclusions:")
                    for value in root.excluded_entrypoints:
                        print(f"  - {value}")
                else:
                    print("  Current entrypoint exclusions: none")

                raw_entrypoints = prompt("Add entrypoints to exclude, comma-separated. Blank keeps current")
                if raw_entrypoints:
                    root.excluded_entrypoints = append_unique(
                        root.excluded_entrypoints,
                        [value.lower() for value in split_csv(raw_entrypoints)],
                    )
            selected.append(root)
            print()

        next_sources[source_name] = selected

    config = TrajectoryAccessConfig(version=1, sources=next_sources)
    saved_path = save_access_config(config, config_path)
    print(f"Saved access config to {saved_path}")
    print("Restart the mybot server/Discord bridge for changes to take effect.")
    return saved_path


def print_effective_config(config_path: Path) -> None:
    config = load_access_config(config_path)
    print(json.dumps(config.to_json(), indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(default_config_path()), help="Path to access config JSON")
    parser.add_argument("--max-files", type=int, default=300, help="Recent files to inspect for workdir previews")
    parser.add_argument("--show", action="store_true", help="Print the effective access config and exit")
    parser.add_argument("--init-default", action="store_true", help="Write a permissive default config and exit")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing config with --init-default")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = Path(args.config)

    if args.show:
        print_effective_config(config_path)
        return

    if args.init_default:
        if config_path.exists() and not args.force:
            raise SystemExit(f"{config_path} already exists. Pass --force to overwrite it.")
        saved_path = save_access_config(default_access_config(), config_path)
        print(f"Saved permissive default access config to {saved_path}")
        return

    run_wizard(config_path, max_files=args.max_files)


if __name__ == "__main__":
    main()
