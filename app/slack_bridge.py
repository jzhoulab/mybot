#!/usr/bin/env python3
"""Slack bridge for the standalone chatbot backbone.

Transport-neutral twin of the Discord bridge: it maps Slack identity + channels
onto the same mybot server API (/chat, /sessions/observe, /owner/identity,
/memory/promote), so everything the server already does — owner-vs-teammate
framing, group sessions, observation, idle rollover, sandboxed retrieval — works
over Slack with no server changes.

Runs in Socket Mode (no public URL needed): needs a bot token (xoxb-...) and an
app-level token (xapp-...) with connections:write.
"""

from __future__ import annotations

import os
import re
import time
import json
import urllib.error
import urllib.request
from typing import Any

try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
except ImportError as exc:  # pragma: no cover - import guard for runtime only
    raise SystemExit(
        "slack_bolt is required. Install it with: python3 -m pip install -U slack_bolt"
    ) from exc

from app.logging_setup import get_logger

log = get_logger("mybot.slack", "slack.log")

TRUTHY = {"1", "true", "yes", "on"}


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in TRUTHY


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def split_for_slack(text: str, limit: int = 3500) -> list[str]:
    """Slack hard-caps a message near 4000 chars; split on paragraph/line/space
    boundaries like the Discord bridge does."""
    clean = text.strip() or "(empty response)"
    if len(clean) <= limit:
        return [clean]
    chunks: list[str] = []
    remaining = clean
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        for sep in ("\n\n", "\n", " "):
            cut = remaining.rfind(sep, 0, limit)
            if cut > 0:
                break
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks


def command_argument(text: str, prefix: str) -> str | None:
    lowered = text.lower()
    if lowered == prefix:
        return ""
    if lowered.startswith(prefix + " "):
        return text[len(prefix):].strip()
    return None


class ChatbotApi:
    """Minimal HTTP client to the local mybot server. Deliberately duplicated
    from the Discord bridge so the Slack process has no discord.py dependency;
    the server API it speaks is identical."""

    def __init__(self, base_url: str, *, timeout_seconds: int, use_trajectory_memory: bool) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.use_trajectory_memory = use_trajectory_memory

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url=f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Chat API HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Chat API connection failed: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("Chat API returned a non-object payload")
        if not parsed.get("ok", False):
            raise RuntimeError(str(parsed.get("error") or "Chat API returned ok=false"))
        return parsed

    def chat(
        self,
        *,
        actor_id: str,
        user_key: str,
        session_key: str,
        message: str,
        memory_scope: str = "private",
        target_user_id: str | None = None,
        display_name: str = "",
        channel_kind: str = "",
        channel_label: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "actor_id": actor_id,
            "user": user_key,
            "session_key": session_key,
            "message": message,
            "memory_scope": memory_scope,
            "use_trajectory_memory": self.use_trajectory_memory,
            "return_sources": False,
        }
        if display_name:
            payload["actor_display_name"] = display_name
        if channel_kind:
            payload["channel_kind"] = channel_kind
        if channel_label:
            payload["channel_label"] = channel_label
        if target_user_id:
            payload["target_user_id"] = target_user_id
        return self._post("/chat", payload)

    def observe(
        self, *, actor_id: str, user_key: str, session_key: str, message: str, display_name: str = ""
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "actor_id": actor_id,
            "user": user_key,
            "session_key": session_key,
            "message": message,
        }
        if display_name:
            payload["actor_display_name"] = display_name
            payload["author_label"] = display_name
        return self._post("/sessions/observe", payload)

    def set_owner_identity(self, *, actor_id: str, display_name: str) -> dict[str, Any]:
        return self._post(
            "/owner/identity", {"action": "set", "actor_id": actor_id, "display_name": display_name}
        )

    def reset_session(self, *, user_key: str, session_key: str) -> dict[str, Any]:
        try:
            return self._post("/sessions/reset", {"user": user_key, "session_key": session_key})
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return {"ok": True, "already_empty": True}
            raise

    def promote_memory(self, *, actor_id: str, scope: str, text: str) -> dict[str, Any]:
        return self._post("/memory/promote", {"actor_id": actor_id, "scope": scope, "text": text})


class SlackBridge:
    def __init__(self, *, api: ChatbotApi, observe_channels: bool, group_sessions: bool) -> None:
        self.api = api
        self.observe_channels = observe_channels
        self.group_sessions = group_sessions
        self.bot_user_id = ""
        self.team_id = ""
        self._name_cache: dict[str, str] = {}
        self._channel_cache: dict[str, str] = {}

    # -- identity/label lookups (cached; Slack events carry ids, not names) --

    def display_name(self, client, user_id: str) -> str:
        if not user_id:
            return ""
        if user_id in self._name_cache:
            return self._name_cache[user_id]
        name = user_id
        try:
            info = client.users_info(user=user_id)
            profile = (info.get("user") or {}).get("profile") or {}
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or (info.get("user") or {}).get("name")
                or user_id
            )
        except Exception:
            log.warning("users_info failed for %s", user_id, exc_info=True)
        self._name_cache[user_id] = name
        return name

    def channel_label(self, client, channel_id: str) -> str:
        if not channel_id:
            return ""
        if channel_id in self._channel_cache:
            return self._channel_cache[channel_id]
        label = channel_id
        try:
            info = client.conversations_info(channel=channel_id)
            ch = info.get("channel") or {}
            name = ch.get("name")
            label = f"#{name}" if name else channel_id
        except Exception:
            pass
        self._channel_cache[channel_id] = label
        return label

    def resolve_mentions(self, client, text: str) -> str:
        """<@U123> / <@U123|name> -> @DisplayName, and strip the bot's own
        mention so the model sees clean prose."""
        def repl(match: re.Match) -> str:
            uid = match.group(1)
            if uid == self.bot_user_id:
                return " "
            return f"@{self.display_name(client, uid)}"

        return re.sub(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", repl, text or "")

    # -- session scoping (mirrors the Discord bridge; threads get own session) --

    def session_key(self, *, channel_id: str, channel_type: str, user_id: str, thread_ts: str) -> str:
        if channel_type == "im":
            return f"slack-dm-user-{user_id}"
        team = self.team_id or "team"
        if thread_ts:
            return f"slack-{team}-thread-{thread_ts}"
        if self.group_sessions:
            return f"slack-{team}-channel-{channel_id}"
        return f"slack-{team}-channel-{channel_id}-user-{user_id}"

    def user_key(self, user_id: str) -> str:
        return f"slack-user-{user_id}"


def build_bridge() -> tuple[App, SlackBridge, str]:
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    app_token = os.environ.get("SLACK_APP_TOKEN", "").strip()
    if not bot_token or not app_token:
        raise SystemExit("SLACK_BOT_TOKEN (xoxb-...) and SLACK_APP_TOKEN (xapp-...) are required")

    base_url = os.environ.get("CHATBOT_BASE_URL", "http://127.0.0.1:8787")
    api = ChatbotApi(
        base_url,
        timeout_seconds=int(os.environ.get("CHATBOT_REQUEST_TIMEOUT_SECONDS", "150")),
        use_trajectory_memory=env_bool("SLACK_USE_TRAJECTORY_MEMORY", True),
    )
    bridge = SlackBridge(
        api=api,
        observe_channels=env_bool("SLACK_OBSERVE_CHANNELS", True),
        group_sessions=env_bool("SLACK_GROUP_SESSIONS", True),
    )
    app = App(token=bot_token)

    def reply(say, text: str, *, thread_ts: str | None) -> None:
        for chunk in split_for_slack(text):
            say(text=chunk, thread_ts=thread_ts)

    def handle_command(text: str, actor_id: str, user_key: str, session_key: str) -> str | None:
        """Returns a reply string if the text was a bang-command, else None."""
        iam = command_argument(text, "!iam")
        if iam is not None:
            if not iam:
                return "Usage: `!iam <your name>`"
            try:
                result = api.set_owner_identity(actor_id=actor_id, display_name=iam)
                name = (result.get("identity") or {}).get("display_name") or iam
                return f"Got it — I'll call you *{name}*."
            except RuntimeError as exc:
                return f"Couldn't set that: {exc}"
        for prefix, scope in (("!remember-shared", "shared"), ("!remember", "private")):
            note = command_argument(text, prefix)
            if note is not None:
                if not note:
                    return f"Usage: `{prefix} <note>`"
                try:
                    rec = api.promote_memory(actor_id=actor_id, scope=scope, text=note)
                    title = (rec.get("memory") or {}).get("title") or "note"
                    return f"Stored {scope} memory: {title}"
                except RuntimeError as exc:
                    return f"Memory promote failed: {exc}"
        if text.lower() in ("!new", "!reset"):
            api.reset_session(user_key=user_key, session_key=session_key)
            return "Started a fresh conversation."
        return None

    @app.event("message")
    def on_message(event, say, client, logger):  # noqa: ANN001
        # Ignore our own messages, bot messages, and edits/deletes/joins.
        if event.get("bot_id") or event.get("subtype"):
            return
        user_id = event.get("user") or ""
        if not user_id or user_id == bridge.bot_user_id:
            return
        channel_id = event.get("channel") or ""
        channel_type = event.get("channel_type") or ""
        thread_ts = event.get("thread_ts") or ""
        raw_text = bridge.resolve_mentions(client, event.get("text") or "")
        text = normalize_text(raw_text)
        if not text:
            return

        is_dm = channel_type == "im"
        addressed = is_dm or (bridge.bot_user_id and bridge.bot_user_id in (event.get("text") or ""))
        actor_id = user_id  # transport-neutral: the raw Slack user id
        user_key = bridge.user_key(user_id)
        session_key = bridge.session_key(
            channel_id=channel_id, channel_type=channel_type, user_id=user_id, thread_ts=thread_ts
        )
        # In channels, reply inside a thread to keep things tidy (start one on the
        # triggering message if not already threaded).
        reply_thread = None if is_dm else (thread_ts or event.get("ts"))
        display = bridge.display_name(client, user_id)

        if not addressed:
            if bridge.observe_channels and not is_dm:
                try:
                    api.observe(
                        actor_id=actor_id, user_key=user_key, session_key=session_key,
                        message=text, display_name=display,
                    )
                except Exception:
                    log.warning("observe failed for %s", user_id, exc_info=True)
            return

        command_reply = handle_command(text, actor_id, user_key, session_key)
        if command_reply is not None:
            reply(say, command_reply, thread_ts=reply_thread)
            return

        # Acknowledge receipt (long answers take a while) then answer.
        try:
            client.reactions_add(channel=channel_id, timestamp=event.get("ts"), name="eyes")
        except Exception:
            pass
        try:
            result = api.chat(
                actor_id=actor_id, user_key=user_key, session_key=session_key, message=text,
                display_name=display,
                channel_kind="dm" if is_dm else "group",
                channel_label="" if is_dm else bridge.channel_label(client, channel_id),
            )
            reply(say, result.get("text") or "(empty response)", thread_ts=reply_thread)
        except Exception as exc:
            log.exception("chat failed for user=%s", user_id)
            reply(say, f"Sorry — I couldn't answer that ({exc}). Please try again.", thread_ts=reply_thread)

    return app, bridge, app_token


def main() -> None:
    app, bridge, app_token = build_bridge()
    auth = app.client.auth_test()
    bridge.bot_user_id = auth.get("user_id") or ""
    bridge.team_id = auth.get("team_id") or ""
    log.info("logged in as %s (bot user %s, team %s)", auth.get("user"), bridge.bot_user_id, bridge.team_id)
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
