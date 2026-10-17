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

import html
import os
import re
import threading
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

from app import relay_protocol
from app.api_auth import authorize
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
        authorize(request)
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

    def channel_call(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Team channel endpoints (GET when payload is None); never raises."""
        try:
            if payload is not None:
                return self._post(path, payload)
            request = urllib.request.Request(url=f"{self.base_url}{path}", method="GET")
            authorize(request)
            with urllib.request.urlopen(request, timeout=15) as response:
                parsed = json.loads(response.read().decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except (RuntimeError, urllib.error.URLError, OSError, ValueError):
            return {}

    def bot_name(self) -> str:
        try:
            request = urllib.request.Request(url=f"{self.base_url}/health", method="GET")
            authorize(request)
            with urllib.request.urlopen(request, timeout=10) as response:
                return str(json.loads(response.read().decode("utf-8")).get("bot_name") or "")
        except (urllib.error.URLError, OSError, ValueError):
            return ""


class SlackBridge:
    def __init__(self, *, api: ChatbotApi, observe_channels: bool, group_sessions: bool) -> None:
        self.api = api
        self.observe_channels = observe_channels
        self.group_sessions = group_sessions
        self.bot_user_id = ""
        self.team_id = ""
        self._name_cache: dict[str, str] = {}
        self._channel_cache: dict[str, str] = {}
        self._account_cache: dict[str, bool] = {}
        # Channels the bot answers and listens in. Empty = DMs only: being
        # invited to a channel is not consent to answer everyone in it.
        self.allowed_channels = {
            value.strip()
            for value in os.environ.get("SLACK_ALLOWED_CHANNEL_IDS", "").split(",")
            if value.strip()
        }
        # Slack guest accounts (single/multi-channel guests) are outsiders.
        self.allow_guest_accounts = env_bool("SLACK_ALLOW_GUEST_ACCOUNTS", False)
        # The team channel the owner connected in the menu app (from the
        # server) — connecting it is consent to answer and listen there.
        self.team_channel_ids: set[str] = set()
        self.blocked_bots: set[str] = set()
        self.sibling_bots = {
            v.strip() for v in os.environ.get("MYBOT_SIBLING_BOT_IDS", "").split(",") if v.strip()
        }
        self.relay_limiter = relay_protocol.RateLimiter()

    def channel_allowed(self, channel_id: str, is_dm: bool) -> bool:
        return is_dm or channel_id in self.allowed_channels or channel_id in self.team_channel_ids

    def channel_view(self, data: dict[str, Any]) -> set[str]:
        """Apply the server's view; returns channels newly connected."""
        if not data or "channels" not in data:
            return set()
        previous = self.team_channel_ids
        self.team_channel_ids = {str(c.get("channel_id")) for c in data.get("channels") or [] if c.get("channel_id")}
        self.blocked_bots = {str(b) for b in data.get("blocked_bots") or []}
        return self.team_channel_ids - previous

    def relay_trusted(self, event: dict) -> bool:
        """Mybot instances of this workspace that may relay asks: listed
        siblings anywhere, any home-team bot in the team channel unless
        blocked. Slack Connect bots from other organizations never."""
        sender = event.get("user") or ""
        team = (event.get("bot_profile") or {}).get("team_id") or event.get("team") or ""
        if not sender or sender in self.blocked_bots:
            return False
        if self.team_id and team and team != self.team_id:
            return False
        return sender in self.sibling_bots or (event.get("channel") or "") in self.team_channel_ids

    def feed_row(self, client, event: dict) -> dict[str, Any]:
        author = event.get("user") or event.get("bot_id") or ""
        is_bot = bool(event.get("bot_id"))
        name = (event.get("bot_profile") or {}).get("name") if is_bot else ""
        return {
            "id": event.get("ts") or "",
            "ts": float(event.get("ts") or 0),
            "author_id": author,
            "author_name": name or self.display_name(client, author),
            "is_bot": is_bot,
            "text": html.unescape(self.resolve_mentions(client, event.get("text") or "", keep_self=True)),
            "reply_to": event.get("thread_ts") if event.get("thread_ts") != event.get("ts") else "",
            "mention_ids": re.findall(r"<@([A-Z0-9]+)", event.get("text") or ""),
        }

    def member_allowed(self, client, event: dict) -> bool:
        """Only full members of this workspace: no Slack Connect users from
        other organizations, and no guest accounts unless opted in."""
        team = event.get("user_team") or event.get("team") or ""
        if self.team_id and team and team != self.team_id:
            return False
        user_id = event.get("user") or ""
        if user_id not in self._account_cache:
            allowed = False
            try:
                user = (client.users_info(user=user_id).get("user") or {})
                restricted = bool(user.get("is_restricted") or user.get("is_ultra_restricted"))
                foreign = bool(self.team_id and user.get("team_id") and user.get("team_id") != self.team_id)
                allowed = not user.get("is_bot") and not foreign and (self.allow_guest_accounts or not restricted)
            except Exception:
                log.warning("users_info failed for %s; refusing", user_id, exc_info=True)
            self._account_cache[user_id] = allowed
        return self._account_cache[user_id]

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

    def resolve_mentions(self, client, text: str, *, keep_self: bool = False) -> str:
        """<@U123> / <@U123|name> -> @DisplayName, and strip the bot's own
        mention so the model sees clean prose."""
        def repl(match: re.Match) -> str:
            uid = match.group(1)
            if uid == self.bot_user_id and not keep_self:
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

    base_url = os.environ.get("CHATBOT_BASE_URL", "http://127.0.0.1:8788")
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
            if not session_key.startswith("slack-dm-"):
                return "`!new` only works in a DM — a channel's conversation is shared."
            api.reset_session(user_key=user_key, session_key=session_key)
            return "Started a fresh conversation."
        return None

    def answer_relay(event: dict, client, relay: dict) -> None:
        """A trusted mybot asked us on a human's behalf: answer in public, in
        the ask's thread, tagged as an ANSWER (app/relay_protocol.py)."""
        sender = event.get("user") or ""
        if f"<@{bridge.bot_user_id}" not in (event.get("text") or ""):
            return
        actor_id = relay_protocol.relay_actor(sender, relay["actor_id"])
        question = normalize_text(bridge.resolve_mentions(client, relay["body"]))[:4000]
        if not actor_id or not question or not bridge.relay_limiter.allow(sender, time.monotonic()):
            return
        channel_id = event.get("channel") or ""
        try:
            result = api.chat(
                actor_id=actor_id, user_key=f"slack-user-{actor_id}",
                session_key=f"relay-{bridge.team_id or 'team'}-{actor_id}", message=question,
                display_name=relay.get("name") or "", channel_kind="group",
                channel_label=bridge.channel_label(client, channel_id),
            )
            answer = result.get("text") or "(empty response)"
        except Exception as exc:
            log.exception("relay answer failed id=%s", relay["id"])
            answer = f"(couldn't answer: {exc})"
        chunks = split_for_slack(relay_protocol.neutralize(answer), limit=3300)
        chunks[-1] += relay_protocol.trailer("slack", "ans", relay["id"])
        for chunk in chunks:
            client.chat_postMessage(channel=channel_id, text=chunk, thread_ts=event.get("thread_ts") or event.get("ts"))

    @app.event("message")
    def on_message(event, say, client, logger):  # noqa: ANN001
        if event.get("subtype") not in (None, "bot_message", "thread_broadcast"):
            return  # edits, deletes, joins
        if event.get("channel") and event.get("channel") in bridge.team_channel_ids:
            api.channel_call("/channel/feed", {
                "platform": "slack", "channel_id": event["channel"],
                "messages": [bridge.feed_row(client, event)],
            })
        if event.get("bot_id"):
            if event.get("user") == bridge.bot_user_id:
                return
            relay = relay_protocol.parse(event.get("text") or "")
            if relay and relay["kind"] == "ask" and bridge.relay_trusted(event):
                answer_relay(event, client, relay)
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
        if not bridge.channel_allowed(channel_id, is_dm):
            return  # not an allowed channel: don't answer, don't listen
        if not bridge.member_allowed(client, event):
            return
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
            reply(say, relay_protocol.neutralize(result.get("text") or "(empty response)"), thread_ts=reply_thread)
        except Exception as exc:
            log.exception("chat failed for user=%s", user_id)
            reply(say, f"Sorry — I couldn't answer that ({exc}). Please try again.", thread_ts=reply_thread)

    return app, bridge, app_token


def sync_team_channel(app: App, bridge: SlackBridge) -> None:
    """Tell the server who this bot is and which channels it's in."""
    channels = []
    cursor = None
    for _ in range(10):
        page = app.client.users_conversations(types="public_channel,private_channel", limit=200, cursor=cursor)
        for ch in page.get("channels") or []:
            channels.append({
                "id": ch["id"], "name": ch.get("name") or ch["id"], "guild": "",
                "session_key": f"slack-{bridge.team_id or 'team'}-channel-{ch['id']}",
                "label": f"#{ch.get('name') or ch['id']}",
            })
        cursor = (page.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    for channel_id in bridge.channel_view(bridge.api.channel_call("/channel/sync", {
        "platform": "slack", "self_id": bridge.bot_user_id,
        "self_name": bridge.api.bot_name() or "mybot", "channels": channels,
    })):
        backfill_team_channel(app, bridge, channel_id)


def backfill_team_channel(app: App, bridge: SlackBridge, channel_id: str) -> None:
    try:
        history = app.client.conversations_history(channel=channel_id, limit=60)
    except Exception as exc:
        log.info("team channel backfill unavailable: %s", exc)
        return
    events = [{**m, "channel": channel_id} for m in reversed(history.get("messages") or [])]
    bridge.api.channel_call("/channel/feed", {
        "platform": "slack", "channel_id": channel_id,
        "messages": [bridge.feed_row(app.client, e) for e in events if not e.get("subtype") or e.get("bot_id")],
    })


def post_team_item(app: App, bridge: SlackBridge, item: dict[str, Any]) -> None:
    """Post what the owner wrote in the menu app's channel view."""
    api = bridge.api
    item_id = str(item.get("id") or "")
    channel_id = str(item.get("channel_id") or "")
    reply_to = str(item.get("reply_to") or "") or None
    try:
        sent = app.client.chat_postMessage(channel=channel_id, text=str(item.get("text") or "")[:3900], thread_ts=reply_to)
    except Exception as exc:
        api.channel_call("/channel/outbox/ack", {"id": item_id, "error": str(exc)[:300]})
        return
    ts = sent.get("ts") or ""
    api.channel_call("/channel/outbox/ack", {"id": item_id, "message_id": ts})
    ask = item.get("answer_self")
    if not (isinstance(ask, dict) and ask.get("question")):
        return
    owner = str(ask.get("actor_id") or "")
    try:
        result = api.chat(
            actor_id=owner, user_key=bridge.user_key(owner),
            session_key=f"slack-{bridge.team_id or 'team'}-thread-{ts}", message=str(ask["question"])[:4000],
            display_name=str(ask.get("name") or ""), channel_kind="group",
            channel_label=bridge.channel_label(app.client, channel_id),
        )
        answer = result.get("text") or "(empty response)"
    except Exception as exc:
        answer = f"(couldn't answer: {exc})"
    for chunk in split_for_slack(relay_protocol.neutralize(answer)):
        app.client.chat_postMessage(channel=channel_id, text=chunk, thread_ts=ts)
    api.channel_call("/channel/outbox/ack", {"id": item_id, "answered": True})


def team_channel_loop(app: App, bridge: SlackBridge) -> None:
    last_sync = 0.0
    while True:
        try:
            if time.time() - last_sync > 300:
                sync_team_channel(app, bridge)
                last_sync = time.time()
            data = bridge.api.channel_call("/channel/outbox?platform=slack")
            for channel_id in bridge.channel_view(data):
                backfill_team_channel(app, bridge, channel_id)
            for item in data.get("items") or []:
                post_team_item(app, bridge, item)
        except Exception as exc:
            log.warning("team channel loop: %s", exc)
        time.sleep(2)


def main() -> None:
    app, bridge, app_token = build_bridge()
    auth = app.client.auth_test()
    bridge.bot_user_id = auth.get("user_id") or ""
    bridge.team_id = auth.get("team_id") or ""
    log.info("logged in as %s (bot user %s, team %s)", auth.get("user"), bridge.bot_user_id, bridge.team_id)
    threading.Thread(target=team_channel_loop, args=(app, bridge), daemon=True, name="team-channel").start()
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
