from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import recent_files


DEFAULT_BASE_DIRS = {
    "codex": "~/.codex",
    "claude": "~/.claude/projects",
}


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_config_path() -> Path:
    configured = os.environ.get("TRAJECTORY_ACCESS_CONFIG_PATH", "").strip()
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured))).resolve()
    return project_root() / "config" / "access.json"


def expand_path(path: str) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(path)))


def is_path_like(value: str) -> bool:
    value = value.strip()
    return value.startswith(("/", "~", "$", "."))


def is_under_path(candidate: str, root: str) -> bool:
    try:
        candidate_real = os.path.realpath(expand_path(candidate))
        root_real = os.path.realpath(expand_path(root))
        return os.path.commonpath([candidate_real, root_real]) == root_real
    except (OSError, ValueError):
        return False


def path_components(path: str) -> list[str]:
    normalized = path.replace("\\", "/").strip("/")
    return [part for part in normalized.split("/") if part]


def normalize_visibility_mode(mode: str | None) -> str:
    value = str(mode or "").strip().lower()
    if value in {"whitelist", "allowlist", "only_inclusions", "include_only"}:
        return "whitelist"
    return "blacklist"


def classify_workdir(source_name: str, workdir: str | None) -> list[str]:
    classes: list[str] = []
    raw = str(workdir or "").strip()
    if not raw:
        classes.append("empty_workdir")
    expanded = expand_path(raw) if raw else ""
    home = expand_path("~")
    documents_codex = os.path.join(home, "Documents", "Codex")
    mybot_workspace = os.path.join(home, ".local", "share", "mybot-codex-workspace")
    mybot_profiles = str(project_root() / "profiles")

    if source_name == "codex":
        is_home_default = bool(expanded) and os.path.realpath(expanded) == os.path.realpath(home)
        is_documents_codex = bool(expanded) and is_under_path(expanded, documents_codex)
        is_mybot_workspace = bool(expanded) and (
            is_under_path(expanded, mybot_workspace)
            or is_under_path(expanded, mybot_profiles)
        )
        if is_home_default:
            classes.append("codex_home_default")
        if is_documents_codex:
            classes.append("codex_documents_chat")
        if is_mybot_workspace:
            classes.append("codex_mybot_workspace")
        if not raw or is_home_default or is_documents_codex or is_mybot_workspace:
            classes.append("codex_no_project")
        else:
            classes.append("codex_project")

    return classes


@dataclass
class TrajectoryAccessAccount:
    source_name: str
    name: str
    base_dir: str
    enabled: bool = True
    visibility_mode: str = "blacklist"
    included_workdirs: list[str] = field(default_factory=list)
    excluded_workdirs: list[str] = field(default_factory=list)
    included_workdir_classes: list[str] = field(default_factory=list)
    excluded_workdir_classes: list[str] = field(default_factory=list)
    excluded_entrypoints: list[str] = field(default_factory=list)
    excluded_session_ids: list[str] = field(default_factory=list)
    exclude_automated: bool = False
    # Per-cluster automated exclusion (origin_detail values: "subagent", "sdk",
    # "exec", "no-user-turns"). exclude_automated=True still means all of them.
    excluded_origin_details: list[str] = field(default_factory=list)
    # Owner-only visibility: sessions from these workdirs/classes ARE indexed
    # and searchable by the owner, but never served to other actors (guest
    # owner-access included). private_workdirs is EXACT cwd match only — a
    # prefix rule for "/Users/x" would swallow every project under home.
    private_workdirs: list[str] = field(default_factory=list)
    private_workdir_classes: list[str] = field(default_factory=list)

    @property
    def expanded_base_dir(self) -> str:
        return expand_path(self.base_dir)

    def scoped_session_id(self, raw_session_id: str) -> str:
        if self.name and self.name != "default":
            return f"{self.name}:{raw_session_id}"
        return raw_session_id

    def workdir_matches_rules(self, workdir: str | None, rules: list[str]) -> bool:
        if not workdir:
            return False
        workdir_text = workdir.strip()
        if not workdir_text:
            return False
        workdir_lower = workdir_text.lower()
        components = {part.lower() for part in path_components(workdir_text)}

        for raw_rule in rules:
            rule = str(raw_rule or "").strip()
            if not rule:
                continue
            if is_path_like(rule):
                if is_under_path(workdir_text, rule):
                    return True
                continue

            rule_lower = rule.lower()
            if rule_lower in components or rule_lower in workdir_lower:
                return True
        return False

    def workdir_classes(self, workdir: str | None) -> set[str]:
        return set(classify_workdir(self.source_name, workdir))

    def is_workdir_blocked(self, workdir: str | None) -> bool:
        classes = self.workdir_classes(workdir)
        excluded_classes = {value.lower() for value in self.excluded_workdir_classes}
        return bool(classes & excluded_classes) or self.workdir_matches_rules(workdir, self.excluded_workdirs)

    def is_workdir_allowed(self, workdir: str | None) -> bool:
        classes = self.workdir_classes(workdir)
        included_classes = {value.lower() for value in self.included_workdir_classes}
        return bool(classes & included_classes) or self.workdir_matches_rules(workdir, self.included_workdirs)

    def include_workdir(self, workdir: str | None) -> bool:
        mode = normalize_visibility_mode(self.visibility_mode)
        if mode == "whitelist":
            return self.is_workdir_allowed(workdir) and not self.is_workdir_blocked(workdir)
        return not self.is_workdir_blocked(workdir)

    def is_workdir_private(self, workdir: str | None) -> bool:
        if not workdir:
            return False
        classes = self.workdir_classes(workdir)
        private_classes = {value.lower() for value in self.private_workdir_classes}
        if classes & private_classes:
            return True
        cwd = workdir.strip().rstrip("/")
        return any(
            cwd == expand_path(str(rule)).rstrip("/")
            for rule in self.private_workdirs
            if str(rule).strip()
        )

    def is_entrypoint_blocked(self, entrypoints: list[str]) -> bool:
        excluded = {entrypoint.lower() for entrypoint in self.excluded_entrypoints}
        if not excluded:
            return False
        return any(entrypoint.lower() in excluded for entrypoint in entrypoints)

    def include_entrypoints(self, entrypoints: list[str]) -> bool:
        return not self.is_entrypoint_blocked(entrypoints)

    def is_session_blocked(self, *candidate_ids: str) -> bool:
        if not self.excluded_session_ids:
            return False
        blocked = {value.strip().lower() for value in self.excluded_session_ids if value.strip()}
        if not blocked:
            return False
        for candidate in candidate_ids:
            value = str(candidate or "").strip().lower()
            if not value:
                continue
            if value in blocked:
                return True
            # Tolerate source-prefixed refs like "claude:<id>" on either side.
            if ":" in value and value.split(":", 1)[1] in blocked:
                return True
        return False

    def include_session(self, session_id: str, raw_session_id: str = "") -> bool:
        return not self.is_session_blocked(session_id, raw_session_id)

    def is_origin_excluded(self, origin: str, origin_detail: str = "") -> bool:
        if origin != "automated":
            return False
        if self.exclude_automated:
            return True
        detail = (origin_detail or "").strip().lower()
        return bool(detail) and detail in {
            str(value).strip().lower() for value in self.excluded_origin_details
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_dir": self.base_dir,
            "enabled": self.enabled,
            "visibility_mode": normalize_visibility_mode(self.visibility_mode),
            "included_workdirs": self.included_workdirs,
            "excluded_workdirs": self.excluded_workdirs,
            "included_workdir_classes": self.included_workdir_classes,
            "excluded_workdir_classes": self.excluded_workdir_classes,
            "excluded_entrypoints": self.excluded_entrypoints,
            "excluded_session_ids": self.excluded_session_ids,
            "exclude_automated": self.exclude_automated,
            "excluded_origin_details": self.excluded_origin_details,
            "private_workdirs": self.private_workdirs,
            "private_workdir_classes": self.private_workdir_classes,
        }


@dataclass
class TrajectoryAccessConfig:
    version: int
    sources: dict[str, list[TrajectoryAccessAccount]]
    # Dirs (or parents of dirs) that tools use as dedicated agent-chat
    # workspaces (e.g. ~/.mytool/worktrees). Sessions there stay indexed and
    # searchable, but never flag as new projects needing owner review.
    agent_workspace_roots: list[str] = field(default_factory=list)

    def accounts_for_source(self, source_name: str) -> list[TrajectoryAccessAccount]:
        accounts = self.sources.get(source_name, [])
        return [account for account in accounts if account.enabled]

    def is_agent_workspace(self, cwd: str) -> bool:
        cwd = (cwd or "").rstrip("/")
        for raw in self.agent_workspace_roots:
            root = os.path.expanduser(str(raw)).rstrip("/")
            if root and (cwd == root or cwd.startswith(root + "/")):
                return True
        return False

    def is_private_workdir(self, source_name: str, cwd: str) -> bool:
        return any(
            account.is_workdir_private(cwd)
            for account in self.accounts_for_source(source_name)
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "policy": {
                "mode": "per_root_visibility",
                "description": (
                    "Enabled roots use blacklist mode by default. "
                    "Use visibility_mode=whitelist to allow only included workdirs/classes."
                ),
            },
            "agent_workspace_roots": list(self.agent_workspace_roots),
            "sources": {
                source_name: {"roots": [account.to_json() for account in accounts]}
                for source_name, accounts in sorted(self.sources.items())
            },
        }


def default_access_config() -> TrajectoryAccessConfig:
    return TrajectoryAccessConfig(
        version=1,
        sources={
            source_name: [
                TrajectoryAccessAccount(
                    source_name=source_name,
                    name="default",
                    base_dir=base_dir,
                    enabled=True,
                    excluded_workdirs=[],
                    excluded_entrypoints=[],
                )
            ]
            for source_name, base_dir in DEFAULT_BASE_DIRS.items()
        },
    )


def _coerce_accounts(source_name: str, raw: Any) -> list[TrajectoryAccessAccount]:
    if not isinstance(raw, dict):
        return default_access_config().sources[source_name]

    accounts_raw = raw.get("roots")
    if accounts_raw is None:
        accounts_raw = raw.get("accounts")
    if not isinstance(accounts_raw, list):
        accounts_raw = []

    accounts: list[TrajectoryAccessAccount] = []
    for index, item in enumerate(accounts_raw):
        if not isinstance(item, dict):
            continue
        base_dir = str(item.get("base_dir") or "").strip()
        if not base_dir:
            continue
        name = str(item.get("name") or f"{source_name}-{index + 1}").strip()
        visibility_mode = normalize_visibility_mode(str(item.get("visibility_mode") or "blacklist"))

        included_raw = item.get("included_workdirs")
        if included_raw is None:
            included_raw = item.get("whitelist_workdirs", [])
        included_workdirs = []
        if isinstance(included_raw, list):
            included_workdirs = [str(value).strip() for value in included_raw if str(value).strip()]

        excluded_raw = item.get("excluded_workdirs")
        if excluded_raw is None:
            excluded_raw = item.get("blacklist_workdirs", [])
        excluded_workdirs = []
        if isinstance(excluded_raw, list):
            excluded_workdirs = [str(value).strip() for value in excluded_raw if str(value).strip()]

        included_classes_raw = item.get("included_workdir_classes")
        if included_classes_raw is None:
            included_classes_raw = item.get("whitelist_workdir_classes", [])
        included_workdir_classes = []
        if isinstance(included_classes_raw, list):
            included_workdir_classes = [
                str(value).strip().lower()
                for value in included_classes_raw
                if str(value).strip()
            ]

        excluded_classes_raw = item.get("excluded_workdir_classes")
        if excluded_classes_raw is None:
            excluded_classes_raw = item.get("blacklist_workdir_classes", [])
        excluded_workdir_classes = []
        if isinstance(excluded_classes_raw, list):
            excluded_workdir_classes = [
                str(value).strip().lower()
                for value in excluded_classes_raw
                if str(value).strip()
            ]

        excluded_entrypoints_raw = item.get("excluded_entrypoints", [])
        excluded_entrypoints = []
        if isinstance(excluded_entrypoints_raw, list):
            excluded_entrypoints = [
                str(value).strip().lower()
                for value in excluded_entrypoints_raw
                if str(value).strip()
            ]

        excluded_sessions_raw = item.get("excluded_session_ids", [])
        excluded_session_ids = []
        if isinstance(excluded_sessions_raw, list):
            excluded_session_ids = [
                str(value).strip()
                for value in excluded_sessions_raw
                if str(value).strip()
            ]
        accounts.append(
            TrajectoryAccessAccount(
                source_name=source_name,
                name=name or f"{source_name}-{index + 1}",
                base_dir=base_dir,
                enabled=bool(item.get("enabled", True)),
                visibility_mode=visibility_mode,
                included_workdirs=included_workdirs,
                excluded_workdirs=excluded_workdirs,
                included_workdir_classes=included_workdir_classes,
                excluded_workdir_classes=excluded_workdir_classes,
                excluded_entrypoints=excluded_entrypoints,
                excluded_session_ids=excluded_session_ids,
                exclude_automated=bool(item.get("exclude_automated", False)),
                excluded_origin_details=[
                    str(value).strip().lower()
                    for value in item.get("excluded_origin_details", []) or []
                    if str(value).strip()
                ],
                private_workdirs=[
                    str(value).strip()
                    for value in item.get("private_workdirs", []) or []
                    if str(value).strip()
                ],
                private_workdir_classes=[
                    str(value).strip().lower()
                    for value in item.get("private_workdir_classes", []) or []
                    if str(value).strip()
                ],
            )
        )

    return accounts or default_access_config().sources[source_name]


def load_access_config(path: str | os.PathLike[str] | None = None) -> TrajectoryAccessConfig:
    if path is None:
        config_path = default_config_path()
    else:
        config_path = Path(os.path.expandvars(os.path.expanduser(str(path))))
    if not config_path.exists():
        return default_access_config()

    with config_path.open() as handle:
        data = json.load(handle)
    sources_raw = data.get("sources", {}) if isinstance(data, dict) else {}

    defaults = default_access_config()
    sources: dict[str, list[TrajectoryAccessAccount]] = {}
    for source_name in DEFAULT_BASE_DIRS:
        raw_source = sources_raw.get(source_name) if isinstance(sources_raw, dict) else None
        sources[source_name] = _coerce_accounts(source_name, raw_source)

    for source_name, accounts in defaults.sources.items():
        sources.setdefault(source_name, accounts)

    agent_roots = data.get("agent_workspace_roots", []) if isinstance(data, dict) else []
    return TrajectoryAccessConfig(
        version=int(data.get("version", 1)) if isinstance(data, dict) else 1,
        sources=sources,
        agent_workspace_roots=[str(root) for root in agent_roots if str(root).strip()]
        if isinstance(agent_roots, list)
        else [],
    )


def save_access_config(config: TrajectoryAccessConfig, path: str | os.PathLike[str] | None = None) -> Path:
    if path is None:
        config_path = default_config_path()
    else:
        config_path = Path(os.path.expandvars(os.path.expanduser(str(path))))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w") as handle:
        json.dump(config.to_json(), handle, indent=2)
        handle.write("\n")
    return config_path


def decode_claude_project_slug(path: str) -> str | None:
    slug = os.path.basename(os.path.dirname(path))
    if not slug.startswith("-"):
        return None
    parts = [part for part in slug.split("-") if part]
    if not parts:
        return None
    return "/" + "/".join(parts)


def discover_codex_workdir(path: str) -> tuple[str | None, str | None]:
    session_id = None
    workdir = None
    with open(path) as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = entry.get("payload", {})
            if not isinstance(payload, dict):
                continue
            if entry.get("type") == "session_meta":
                session_id = payload.get("id") or session_id
            if payload.get("cwd"):
                workdir = str(payload.get("cwd"))
            if session_id and workdir:
                break
    return session_id, workdir


def peek_codex_session_meta(path: str) -> tuple[str | None, str | None, str]:
    """Cheap pre-filter read: session id, workdir, and the `source` marker,
    stopping as soon as they are known (session_meta is the first line).

    Lets the scanner reject subagent/exec transcripts without parsing whole
    files, so the recency window can be filled with sessions that will
    actually be indexed. Returns source kind "subagent" for spawned agent
    transcripts, else the raw source string (e.g. "exec"), else "".
    """
    session_id = None
    workdir = None
    source_kind = ""
    seen_meta = False
    with open(path) as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = entry.get("payload", {})
            if not isinstance(payload, dict):
                continue
            if entry.get("type") == "session_meta":
                seen_meta = True
                session_id = payload.get("id") or session_id
                source = payload.get("source")
                if isinstance(source, dict):
                    if "subagent" in source:
                        source_kind = "subagent"
                elif isinstance(source, str):
                    source_kind = source
            if payload.get("cwd"):
                workdir = str(payload.get("cwd"))
            if seen_meta and session_id and workdir:
                break
    return session_id, workdir, source_kind


def discover_claude_metadata(path: str) -> tuple[str | None, str | None, list[str]]:
    session_id = os.path.basename(path).replace(".jsonl", "")
    fallback_workdir = decode_claude_project_slug(path)
    workdir = None
    entrypoints: list[str] = []
    seen_entrypoints: set[str] = set()

    with open(path) as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("sessionId"):
                session_id = str(entry.get("sessionId"))
            if entry.get("entrypoint"):
                entrypoint = str(entry.get("entrypoint")).strip()
                if entrypoint and entrypoint not in seen_entrypoints:
                    seen_entrypoints.add(entrypoint)
                    entrypoints.append(entrypoint)
            if entry.get("cwd"):
                workdir = str(entry.get("cwd"))
            if session_id and workdir and entrypoints:
                break
    return session_id, workdir or fallback_workdir, entrypoints


def discover_claude_workdir(path: str) -> tuple[str | None, str | None]:
    session_id, workdir, _ = discover_claude_metadata(path)
    return session_id, workdir


def account_patterns(account: TrajectoryAccessAccount) -> list[str]:
    if account.source_name == "codex":
        return [
            os.path.join(account.expanded_base_dir, "sessions", "**", "*.jsonl"),
            os.path.join(account.expanded_base_dir, "archived_sessions", "*.jsonl"),
        ]
    if account.source_name == "claude":
        return [os.path.join(account.expanded_base_dir, "*", "*.jsonl")]
    return []


def discover_workdir_counts(
    account: TrajectoryAccessAccount,
    *,
    max_files: int = 500,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in recent_files(account_patterns(account), max_files):
        if account.source_name == "codex":
            _, workdir = discover_codex_workdir(path)
        elif account.source_name == "claude":
            _, workdir, entrypoints = discover_claude_metadata(path)
            if not account.include_entrypoints(entrypoints):
                continue
        else:
            workdir = None
        if not workdir:
            workdir = "(unknown)"
        counts[workdir] = counts.get(workdir, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def discover_entrypoint_counts(
    account: TrajectoryAccessAccount,
    *,
    max_files: int = 500,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in recent_files(account_patterns(account), max_files):
        if account.source_name != "claude":
            continue
        _, _, entrypoints = discover_claude_metadata(path)
        if not entrypoints:
            entrypoints = ["(missing)"]
        for entrypoint in entrypoints:
            counts[entrypoint] = counts.get(entrypoint, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def discover_default_roots() -> dict[str, list[str]]:
    roots: dict[str, list[str]] = {}
    for source_name, default_base in DEFAULT_BASE_DIRS.items():
        expanded = expand_path(default_base)
        roots[source_name] = [default_base] if os.path.isdir(expanded) else []
    return roots
