"""Google Antigravity (the agy CLI and the editor) as a trajectory source.

Antigravity keeps one folder per conversation, named by its id, under a
`brain/` directory: ~/.gemini/antigravity-cli/brain/ for the CLI and
~/.gemini/antigravity/brain/ (or antigravity-ide/) for the editor.

- The CLI writes the whole conversation to
  `<id>/.system_generated/logs/transcript_full.jsonl` (transcript.jsonl is
  the same with long fields clipped). One JSON object per step:
  `step_index`, `source` (USER_EXPLICIT / MODEL / SYSTEM), `type`
  (USER_INPUT, PLANNER_RESPONSE, RUN_COMMAND, VIEW_FILE, ...), `status`,
  `created_at`, `content`, `tool_calls` [{name, args}], `thinking`. The
  prompt sits inside <USER_REQUEST>...</USER_REQUEST>.
- The editor's own chat log is an encrypted protobuf, so for an editor-only
  conversation what can be read is its markdown artifacts (task.md,
  implementation_plan.md, walkthrough.md): the plan and what was done.

Neither records a working directory; it's inferred from the paths the
agent's tools used. Upgraded installs can hold the same conversation under
more than one brain/ folder, so a conversation is read once, from its
fullest copy.
"""
from __future__ import annotations

import glob
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone

from .access import TrajectoryAccessAccount
from .common import (
    append_turn,
    choose_title,
    discovery_cache_get,
    discovery_cache_put,
    discovery_cache_signature,
    is_real_user_text,
    iter_bounded_jsonl_lines,
    make_detailed_summary,
    make_short_summary,
    parse_timestamp,
)
from .models import NormalizedTrajectory, TrajectorySourceAdapter

BRAIN_GLOBS = ("antigravity-cli/brain", "antigravity/brain", "antigravity-ide/brain", "antigravity-backup/brain")
ARTIFACTS = ("task.md", "implementation_plan.md", "walkthrough.md", "proposal.md")
_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.S)
_PATH_ARG_RE = re.compile(r"(?i)(cwd|workdir|directory|dir|path|file|target)")
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,}$")


def user_request(content: str) -> str:
    """The prompt the owner typed, without Antigravity's metadata blocks."""
    match = _REQUEST_RE.search(content or "")
    text = match.group(1) if match else (content or "")
    return re.split(r"<(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>", text, maxsplit=1)[0].strip()


def infer_cwd(paths: list[str], command_dirs: list[str]) -> str:
    """The folder the conversation worked in: where its commands ran, else
    the deepest folder holding most of the files it touched."""
    home = os.path.expanduser("~")
    dirs = [d.rstrip("/") for d in command_dirs if d.startswith("/")]
    if dirs:
        return Counter(dirs).most_common(1)[0][0]
    folders = [os.path.dirname(p) for p in paths if p.startswith("/") and "/.gemini/" not in p]
    if not folders:
        return ""
    common = os.path.commonpath(folders)
    if common in ("/", home) or len(set(folders)) == 1:
        # Spread over unrelated places: the folder used most is the best guess.
        common = Counter(folders).most_common(1)[0][0] if common in ("/", home) else common
    return common


def _walk_args(value, out_paths: list[str], out_dirs: list[str], key: str = "") -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _walk_args(v, out_paths, out_dirs, str(k))
    elif isinstance(value, list):
        for v in value:
            _walk_args(v, out_paths, out_dirs, key)
    elif isinstance(value, str) and value.startswith("/") and "\n" not in value and len(value) < 400:
        if key.lower() in ("cwd", "workdir", "working_directory", "workingdirectory"):
            out_dirs.append(value)
        elif _PATH_ARG_RE.search(key):
            out_paths.append(value)


def conversation_files(base_dir: str) -> dict[str, list[str]]:
    """{conversation id: [its brain/<id> folders]} across every brain/ store."""
    found: dict[str, list[str]] = {}
    for sub in BRAIN_GLOBS:
        for folder in glob.glob(os.path.join(base_dir, sub, "*")):
            ident = os.path.basename(folder)
            if os.path.isdir(folder) and _UUID_RE.match(ident):
                found.setdefault(ident, []).append(folder)
    return found


def best_transcript(folders: list[str]) -> str:
    candidates = []
    for folder in folders:
        logs = os.path.join(folder, ".system_generated", "logs")
        for name in ("transcript_full.jsonl", "transcript.jsonl"):
            path = os.path.join(logs, name)
            if os.path.isfile(path):
                candidates.append((name == "transcript_full.jsonl", os.path.getsize(path), path))
    return max(candidates)[2] if candidates else ""


class AntigravitySourceAdapter(TrajectorySourceAdapter):
    def __init__(self, base_dir: str | None = None, *, account: TrajectoryAccessAccount | None = None) -> None:
        account = account or TrajectoryAccessAccount(
            source_name="antigravity", name="default", base_dir=base_dir or "~/.gemini",
        )
        if base_dir is not None:
            account.base_dir = base_dir
        super().__init__(account_name=account.name)
        self.account = account
        self.base_dir = account.expanded_base_dir

    def source_name(self) -> str:
        return "antigravity"

    def discover_sessions(self, max_files: int = 200) -> list[NormalizedTrajectory]:
        policy_key = self.account.policy_cache_key()
        sessions: list[NormalizedTrajectory] = []
        by_recency = []
        for ident, folders in conversation_files(self.base_dir).items():
            transcript = best_transcript(folders)
            path = transcript or max(folders, key=os.path.getmtime)
            try:
                by_recency.append((os.path.getmtime(path), ident, folders, transcript))
            except OSError:
                continue
        by_recency.sort(reverse=True)
        for _, ident, folders, transcript in by_recency:
            if max_files > 0 and len(sessions) >= max_files:
                break
            key_path = transcript or folders[0]
            signature = discovery_cache_signature(key_path, policy_key) if transcript else None
            cached = discovery_cache_get(key_path, signature) if signature else None
            if cached is not None:
                sessions.append(cached)
                continue
            session = self._read(ident, folders, transcript)
            if session is None:
                continue
            if signature:
                discovery_cache_put(key_path, signature, session)
            sessions.append(session)
        self._sessions = sorted(sessions, key=lambda s: s.updated_at, reverse=True)
        return self._sessions

    def _read(self, ident: str, folders: list[str], transcript: str) -> NormalizedTrajectory | None:
        turns = []
        first_ts = last_ts = None
        paths: list[str] = []
        command_dirs: list[str] = []
        human_turns = 0
        model = ""
        if transcript:
            for line in iter_bounded_jsonl_lines(transcript):
                if line is None:
                    continue
                try:
                    step = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(step, dict):
                    continue
                ts = parse_timestamp(str(step.get("created_at") or ""))
                ts_text = ts.isoformat() if ts else None
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts
                for call in step.get("tool_calls") or []:
                    if isinstance(call, dict):
                        _walk_args(call.get("args"), paths, command_dirs)
                kind, origin = str(step.get("type") or ""), str(step.get("source") or "")
                content = str(step.get("content") or "")
                if kind == "USER_INPUT":
                    found = re.search(r"Model Selection[^\n]*?:\s*([^\n<]+)", content)
                    model = found.group(1).strip() if found else model
                    text = user_request(content)
                    if text:
                        if origin == "USER_EXPLICIT" and is_real_user_text(text):
                            human_turns += 1
                        append_turn(turns, "user", text, ts_text)
                elif kind == "PLANNER_RESPONSE" and content.strip():
                    append_turn(turns, "assistant", content.strip(), ts_text)
        else:
            # Editor-only conversation: its plan and walkthrough, oldest first.
            folder = max(folders, key=os.path.getmtime)
            for name in ARTIFACTS:
                path = os.path.join(folder, name)
                if not os.path.isfile(path):
                    continue
                try:
                    with open(path, encoding="utf-8", errors="replace") as handle:
                        text = handle.read().strip()
                    mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
                except OSError:
                    continue
                if text:
                    first_ts = min(first_ts, mtime) if first_ts else mtime
                    last_ts = max(last_ts, mtime) if last_ts else mtime
                    append_turn(turns, "assistant", f"[{name}]\n{text}", mtime.isoformat())
                    paths += re.findall(r"(?:file://)?(/(?:Users|home)/[^\s)`'\"]+)", text)
        if not turns or last_ts is None:
            return None
        cwd = infer_cwd(paths, command_dirs)
        if not self.account.include_workdir(cwd or None):
            return None
        session_id = self.account.scoped_session_id(ident)
        if not self.account.include_session(session_id, ident):
            return None
        origin = "interactive" if human_turns or not transcript else "automated"
        origin_detail = "" if origin == "interactive" else "no-user-turns"
        if self.account.is_origin_excluded(origin, origin_detail):
            return None
        user_turns = [t.text for t in turns if t.role == "user"]
        assistant_turns = [t.text for t in turns if t.role == "assistant"]
        title = choose_title("", user_turns, "antigravity conversation")
        return NormalizedTrajectory(
            source_name=self.source_name(),
            session_id=session_id,
            title=title,
            file_path=transcript or folders[0],
            updated_at=last_ts.isoformat(),
            turns=turns,
            short_summary=make_short_summary(title, user_turns, assistant_turns),
            detailed_summary=make_detailed_summary(title, user_turns, assistant_turns),
            metadata={
                "tool": self.source_name(),
                "account": self.account.name,
                "raw_session_id": ident,
                "cwd": cwd,
                "cwd_inferred": True,
                "app": "cli" if transcript and "/antigravity-cli/" in transcript else "editor",
                "transcript": bool(transcript),
                "model": model,
                "origin": origin,
                "origin_detail": origin_detail,
                **({"visibility": "private"} if self.account.is_workdir_private(cwd) else {}),
                "human_turns": human_turns,
            },
        )


def iter_typed_prompts(base_dir: str):
    """(conversation id, timestamp, cwd, prompt) for every prompt the owner
    typed, for the prompt history (Antigravity keeps no history.jsonl)."""
    for ident, folders in conversation_files(os.path.expanduser(base_dir)).items():
        transcript = best_transcript(folders)
        if not transcript:
            continue
        for line in iter_bounded_jsonl_lines(transcript):
            try:
                step = json.loads(line) if line else None
            except json.JSONDecodeError:
                continue
            if not isinstance(step, dict) or step.get("type") != "USER_INPUT" or step.get("source") != "USER_EXPLICIT":
                continue
            ts = parse_timestamp(str(step.get("created_at") or ""))
            text = user_request(str(step.get("content") or ""))
            if ts and text:
                yield ident, ts.isoformat(), "", text


def read_turns(file_path: str) -> list[tuple[str, str, str]]:
    """(role, text, timestamp) of a transcript or an editor conversation
    folder, for reading one session in full."""
    if os.path.isdir(file_path):
        ident = os.path.basename(file_path.rstrip("/"))
        session = AntigravitySourceAdapter(base_dir="/")._read(ident, [file_path], "")
    else:
        ident = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(file_path))))
        session = AntigravitySourceAdapter(base_dir="/")._read(ident, [os.path.dirname(file_path)], file_path)
    return [(t.role, t.text, t.timestamp or "") for t in session.turns] if session else []
