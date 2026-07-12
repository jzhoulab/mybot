#!/usr/bin/env python3
"""Discord bridge for the standalone chatbot backbone."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

try:
    import discord
    from discord import app_commands
except ImportError as exc:  # pragma: no cover - import guard for runtime only
    raise SystemExit(
        "discord.py is required. Install it with: python3 -m pip install -U discord.py"
    ) from exc

from app.logging_setup import get_logger

log = get_logger("mybot.discord", "discord.log")

TRUTHY = {"1", "true", "yes", "on"}


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in TRUTHY


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def parse_id_set(raw: str, *, label: str) -> set[int]:
    values: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            values.add(int(chunk))
        except ValueError as exc:
            raise SystemExit(f"{label} must be a comma-separated list of integers") from exc
    return values


def split_for_discord(text: str, limit: int = 1900) -> list[str]:
    clean = text.strip() or "(empty response)"
    if len(clean) <= limit:
        return [clean]

    chunks: list[str] = []
    remaining = clean
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at < 0:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at < 0:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < 0:
            split_at = limit
        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            split_at = limit
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()
    return chunks


def command_argument(text: str, prefix: str) -> str | None:
    lowered = text.lower()
    prefix_lower = prefix.lower()
    if lowered == prefix_lower:
        return ""
    if lowered.startswith(prefix_lower + " "):
        return text[len(prefix) :].strip()
    return None


@dataclass
class BridgeConfig:
    discord_bot_token: str
    chatbot_base_url: str
    allowed_channel_ids: set[int]
    auto_reply_channel_ids: set[int]
    require_mention_in_guilds: bool
    enable_message_content: bool
    use_trajectory_memory: bool
    request_timeout_seconds: int
    max_output_tokens: int | None
    command_guild_id: int | None
    message_coalesce_seconds: float
    # One shared session per guild channel (multi-person context) instead of
    # one per (channel, user). DMs are always per-user.
    group_sessions: bool
    # Record non-addressed messages from explicitly listed channels into the
    # channel session so the bot has context when it IS addressed.
    observe_channels: bool
    # Set the per-guild nickname to '<handle>-mybot' so a team's instances are
    # distinguishable in a shared server.
    set_guild_nickname: bool


@dataclass
class PendingChatRequest:
    actor_id: str
    user_key: str
    session_key: str
    message_text: str
    memory_scope: str
    target_user_id: str | None = None
    discord_message: discord.Message | None = None
    interaction: discord.Interaction | None = None
    display_name: str = ""
    channel_kind: str = "dm"
    channel_label: str = ""


@dataclass
class ChatRequestBuffer:
    requests: list[PendingChatRequest]
    version: int = 0
    task: asyncio.Task[None] | None = None


class ChatbotApi:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

    async def fetch_bot_name(self) -> str:
        """The instance's display name (e.g. 'alice-mybot') from /health, so the
        bridge can name itself consistently in shared servers."""
        def _get() -> str:
            request = urllib.request.Request(
                url=f"{self.config.chatbot_base_url.rstrip('/')}/health", method="GET"
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    parsed = json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, json.JSONDecodeError, OSError):
                return ""
            return str(parsed.get("bot_name") or "") if isinstance(parsed, dict) else ""

        return await asyncio.to_thread(_get)

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url=f"{self.config.chatbot_base_url.rstrip('/')}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.request_timeout_seconds
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Chat API HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Chat API connection failed: {exc}") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Chat API returned invalid JSON: {raw[:500]}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("Chat API returned a non-object JSON payload")
        if not parsed.get("ok", False):
            raise RuntimeError(str(parsed.get("error") or "Chat API returned ok=false"))
        return parsed

    async def chat(
        self,
        *,
        actor_id: str,
        user_key: str,
        session_key: str,
        message: str,
        memory_scope: str = "private",
        target_user_id: str | None = None,
        return_sources: bool = True,
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
            "use_trajectory_memory": self.config.use_trajectory_memory,
            "return_sources": return_sources,
        }
        if display_name:
            payload["actor_display_name"] = display_name
        if channel_kind:
            payload["channel_kind"] = channel_kind
        if channel_label:
            payload["channel_label"] = channel_label
        if target_user_id:
            payload["target_user_id"] = target_user_id
        if self.config.max_output_tokens is not None:
            payload["max_output_tokens"] = self.config.max_output_tokens
        return await asyncio.to_thread(self._post_json, "/chat", payload)

    async def observe(
        self,
        *,
        actor_id: str,
        user_key: str,
        session_key: str,
        message: str,
        display_name: str = "",
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
        return await asyncio.to_thread(self._post_json, "/sessions/observe", payload)

    async def reset_session(self, *, user_key: str, session_key: str) -> dict[str, Any]:
        payload = {"user": user_key, "session_key": session_key}
        return await asyncio.to_thread(self._reset_session, payload)

    async def promote_memory(
        self,
        *,
        actor_id: str,
        scope: str,
        text: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "actor_id": actor_id,
            "scope": scope,
            "text": text,
        }
        if tags:
            payload["tags"] = tags
        return await asyncio.to_thread(self._post_json, "/memory/promote", payload)

    async def set_owner_identity(self, *, actor_id: str, display_name: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._post_json,
            "/owner/identity",
            {"action": "set", "actor_id": actor_id, "display_name": display_name},
        )

    async def session_history(
        self,
        *,
        user_key: str,
        session_key: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        payload = {
            "user": user_key,
            "session_key": session_key,
            "limit": limit,
        }
        return await asyncio.to_thread(self._post_json, "/sessions/history", payload)

    def _reset_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._post_json("/sessions/reset", payload)
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return {"ok": True, "already_empty": True}
            raise


class DiscordBridgeClient(discord.Client):
    def __init__(self, config: BridgeConfig, api: ChatbotApi) -> None:
        intents = discord.Intents(guilds=True, messages=True)
        intents.message_content = config.enable_message_content
        super().__init__(intents=intents)
        self.config = config
        self.api = api
        self.tree = app_commands.CommandTree(self)
        self.safe_mentions = discord.AllowedMentions.none()
        self.chat_buffers: dict[str, ChatRequestBuffer] = {}
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(
            name="chat",
            description="Send a private-scope message to the standalone chatbot.",
        )
        @app_commands.describe(message="Message to send to the chatbot")
        async def chat_command(interaction: discord.Interaction, message: str) -> None:
            print(
                f"/chat invoked by user={interaction.user.id} "
                f"channel={getattr(interaction.channel, 'id', 'unknown')} "
                f"age_s={self._interaction_age_seconds(interaction):.3f}"
            )
            if not self._interaction_allowed(interaction):
                await interaction.response.send_message(
                    "This channel is not allowed for the bot bridge.",
                    ephemeral=True,
                    allowed_mentions=self.safe_mentions,
                )
                return

            text = normalize_text(message)
            if not text:
                await interaction.response.send_message(
                    "Message cannot be empty.",
                    ephemeral=True,
                    allowed_mentions=self.safe_mentions,
                )
                return

            await interaction.response.defer(thinking=True)
            channel_kind, channel_label = self._channel_kind_label(interaction.channel)
            await self._enqueue_chat_request(
                PendingChatRequest(
                    actor_id=self._actor_id(interaction.user.id),
                    user_key=self._user_key(interaction.user.id),
                    session_key=self._session_key_for_channel(
                        user_id=interaction.user.id,
                        channel=interaction.channel,
                    ),
                    message_text=text,
                    memory_scope="private",
                    interaction=interaction,
                    display_name=interaction.user.display_name,
                    channel_kind=channel_kind,
                    channel_label=channel_label,
                )
            )

        @self.tree.command(
            name="new",
            description="Reset this Discord conversation's chatbot session.",
        )
        async def new_command(interaction: discord.Interaction) -> None:
            print(
                f"/new invoked by user={interaction.user.id} "
                f"channel={getattr(interaction.channel, 'id', 'unknown')} "
                f"age_s={self._interaction_age_seconds(interaction):.3f}"
            )
            if not self._interaction_allowed(interaction):
                await interaction.response.send_message(
                    "This channel is not allowed for the bot bridge.",
                    ephemeral=True,
                    allowed_mentions=self.safe_mentions,
                )
                return

            await interaction.response.defer(thinking=True, ephemeral=True)
            try:
                await self.api.reset_session(
                    user_key=self._user_key(interaction.user.id),
                    session_key=self._session_key_for_channel(
                        user_id=interaction.user.id,
                        channel=interaction.channel,
                    ),
                )
            except RuntimeError as exc:
                await interaction.followup.send(
                    f"Session reset failed: {exc}",
                    ephemeral=True,
                    allowed_mentions=self.safe_mentions,
                )
                return

            await interaction.followup.send(
                "Started a fresh session for this Discord context.",
                ephemeral=True,
                allowed_mentions=self.safe_mentions,
            )

    async def setup_hook(self) -> None:
        if self.config.command_guild_id:
            guild = discord.Object(id=self.config.command_guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
            except discord.Forbidden:
                print(
                    "Guild command sync failed with Missing Access. "
                    "Check that the bot is installed in the target server with "
                    "'bot' and 'applications.commands' scopes."
                )
            else:
                print(
                    f"Synced {len(synced)} Discord app commands to guild {self.config.command_guild_id}"
                )
                return

        synced = await self.tree.sync()
        print(f"Synced {len(synced)} global Discord app commands")

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("logged in as %s (%s)", self.user, self.user.id)
        await self._apply_bot_nickname()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._apply_bot_nickname(guilds=[guild])

    async def _apply_bot_nickname(self, guilds: list[discord.Guild] | None = None) -> None:
        """Name this instance '<handle>-mybot' in every server so a team running
        many instances can tell whose bot is whose. Best-effort — needs the
        Change Nickname permission; skipped silently if denied."""
        if not self.config.set_guild_nickname:
            return
        name = await self.api.fetch_bot_name()
        if not name:
            return
        for guild in (guilds if guilds is not None else list(self.guilds)):
            member = guild.me
            if member is None or member.nick == name:
                continue
            try:
                await member.edit(nick=name)
                log.info("set nickname %r in guild %s", name, guild.id)
            except discord.Forbidden:
                log.info("no permission to set nickname in guild %s (need Change Nickname)", guild.id)
            except discord.HTTPException:
                log.warning("failed to set nickname in guild %s", guild.id, exc_info=True)

    async def on_disconnect(self) -> None:
        log.warning("disconnected from Discord; waiting for reconnect (messages sent now are missed)")

    async def on_resumed(self) -> None:
        log.info("session resumed after reconnect")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not self.config.enable_message_content:
            return
        if not self._channel_allowed(message.channel):
            return
        if self.user is None:
            return

        raw_text = message.content or ""
        if message.guild is not None:
            if not self._should_reply_in_guild_message(message):
                await self._maybe_observe_message(message, raw_text)
                return
            raw_text = self._strip_bot_mentions(raw_text)
        raw_text = self._resolve_mentions(message, raw_text)

        text = normalize_text(raw_text)
        if not text:
            return

        log.info(
            "message received: author=%s channel=%s len=%d preview=%r",
            message.author.id, getattr(message.channel, "id", "?"), len(text), text[:80],
        )
        handled = await self._handle_command_message(message, text)
        if handled:
            return

        await self._handle_chat_message(
            message,
            text,
            memory_scope="private",
        )

    def _observation_eligible(self, channel: discord.abc.MessageableChannel) -> bool:
        """Observe only channels someone explicitly listed — never hoover a
        whole server just because the allowlist is empty (= all allowed)."""
        if not self.config.observe_channels or not self.config.group_sessions:
            return False
        channel_id = getattr(channel, "id", None)
        parent_id = getattr(channel, "parent_id", None)
        explicit = self.config.allowed_channel_ids | self.config.auto_reply_channel_ids
        return channel_id in explicit or parent_id in explicit

    async def _maybe_observe_message(self, message: discord.Message, raw_text: str) -> None:
        if not self._observation_eligible(message.channel):
            return
        text = normalize_text(self._resolve_mentions(message, raw_text))
        if not text:
            return
        try:
            await self.api.observe(
                actor_id=self._actor_id(message.author.id),
                user_key=self._user_key(message.author.id),
                session_key=self._session_key_for_channel(
                    user_id=message.author.id,
                    channel=message.channel,
                ),
                message=text,
                display_name=message.author.display_name,
            )
        except Exception:
            log.exception("failed to observe channel message author=%s", message.author.id)

    async def _handle_command_message(self, message: discord.Message, text: str) -> bool:
        if text.lower() in {"!new", "!reset"}:
            await self._handle_message_reset(message)
            return True

        if text.lower() == "!sources":
            await self._handle_sources(message)
            return True

        iam_name = command_argument(text, "!iam")
        if iam_name is not None:
            if not iam_name:
                await self._send_channel_text(
                    message.channel, "Usage: `!iam <your name>`", reference=message,
                )
                return True
            try:
                result = await self.api.set_owner_identity(
                    actor_id=self._actor_id(message.author.id),
                    display_name=iam_name,
                )
                name = (result.get("identity") or {}).get("display_name") or iam_name
                await self._send_channel_text(
                    message.channel, f"Got it — I'll call you **{name}**.", reference=message,
                )
            except RuntimeError as exc:
                await self._send_channel_text(
                    message.channel, f"Couldn't set that: {exc}", reference=message,
                )
            return True

        remember_shared = command_argument(text, "!remember-shared")
        if remember_shared is not None:
            if not remember_shared:
                await self._send_channel_text(
                    message.channel,
                    "Usage: `!remember-shared <note>`",
                    reference=message,
                )
                return True
            await self._handle_promote(message, scope="shared", note=remember_shared)
            return True

        remember_private = command_argument(text, "!remember")
        if remember_private is not None:
            if not remember_private:
                await self._send_channel_text(
                    message.channel,
                    "Usage: `!remember <note>`",
                    reference=message,
                )
                return True
            await self._handle_promote(message, scope="private", note=remember_private)
            return True

        shared_question = command_argument(text, "!team")
        if shared_question is not None:
            if not shared_question:
                await self._send_channel_text(
                    message.channel,
                    "Usage: `!team <question>`",
                    reference=message,
                )
                return True
            await self._handle_chat_message(
                message,
                shared_question,
                memory_scope="shared",
            )
            return True

        if text.lower().startswith("!ask"):
            await self._handle_target_user_chat(message, text)
            return True

        return False

    async def _handle_chat_message(
        self,
        message: discord.Message,
        text: str,
        *,
        memory_scope: str,
        target_user_id: str | None = None,
    ) -> None:
        session_key = self._session_key_for_channel(
            user_id=message.author.id,
            channel=message.channel,
        )
        channel_kind, channel_label = self._channel_kind_label(message.channel)
        await self._enqueue_chat_request(
            PendingChatRequest(
                actor_id=self._actor_id(message.author.id),
                user_key=self._user_key(message.author.id),
                session_key=session_key,
                message_text=text,
                memory_scope=memory_scope,
                target_user_id=target_user_id,
                discord_message=message,
                display_name=message.author.display_name,
                channel_kind=channel_kind,
                channel_label=channel_label,
            )
        )

    async def _handle_target_user_chat(self, message: discord.Message, text: str) -> None:
        if self.user is None:
            return
        targets = [member for member in message.mentions if member.id != self.user.id]
        if not targets:
            await self._send_channel_text(
                message.channel,
                "Usage: `!ask @user <question>`",
                reference=message,
            )
            return

        target = targets[0]
        cleaned = re.sub(rf"^!ask\s+<@!?{target.id}>\s*", "", text, count=1, flags=re.IGNORECASE).strip()
        if not cleaned:
            await self._send_channel_text(
                message.channel,
                "Usage: `!ask @user <question>`",
                reference=message,
            )
            return

        await self._handle_chat_message(
            message,
            cleaned,
            memory_scope="target_user",
            target_user_id=str(target.id),
        )

    def _chat_buffer_key(self, request: PendingChatRequest) -> str:
        # actor_id keeps coalescing per-author: in a shared channel session two
        # people's near-simultaneous messages must not fold into one request.
        return "|".join(
            [
                request.session_key,
                request.actor_id,
                request.memory_scope,
                request.target_user_id or "",
            ]
        )

    async def _enqueue_chat_request(self, request: PendingChatRequest) -> None:
        key = self._chat_buffer_key(request)
        buffer = self.chat_buffers.get(key)
        if buffer is None:
            buffer = ChatRequestBuffer(requests=[])
            self.chat_buffers[key] = buffer
        buffer.requests.append(request)
        buffer.version += 1
        if buffer.task is None or buffer.task.done():
            buffer.task = asyncio.create_task(self._chat_buffer_worker(key))

    async def _chat_buffer_worker(self, key: str) -> None:
        try:
            while True:
                buffer = self.chat_buffers.get(key)
                if buffer is None:
                    return

                delay = max(0.0, self.config.message_coalesce_seconds)
                if delay:
                    while True:
                        version = buffer.version
                        await asyncio.sleep(delay)
                        if buffer.version == version:
                            break

                requests = list(buffer.requests)
                buffer.requests.clear()
                if not requests:
                    return

                await self._send_batched_chat_request(requests)
                if not buffer.requests:
                    return
        finally:
            buffer = self.chat_buffers.get(key)
            if buffer is not None and buffer.task is asyncio.current_task():
                if buffer.requests:
                    buffer.task = asyncio.create_task(self._chat_buffer_worker(key))
                else:
                    self.chat_buffers.pop(key, None)

    async def _send_batched_chat_request(self, requests: list[PendingChatRequest]) -> None:
        if not requests:
            return
        target = requests[-1]
        combined_message = self._combined_chat_message(requests)
        log.info(
            "chat request: user=%s session=%s batched=%d chars=%d",
            target.user_key, target.session_key, len(requests), len(combined_message),
        )
        try:
            if target.discord_message is not None:
                async with target.discord_message.channel.typing():
                    response = await self._call_chat(target, combined_message)
            else:
                response = await self._call_chat(target, combined_message)
            text = response.get("text") or "(empty response)"
            await self._send_chat_response(target, text)
            log.info("chat reply sent: user=%s reply_chars=%d", target.user_key, len(text))
        except Exception as exc:  # never leave a message silently unanswered
            log.exception(
                "chat request failed: user=%s session=%s", target.user_key, target.session_key
            )
            try:
                await self._send_chat_error(
                    target, f"Sorry — I couldn't answer that ({exc}). Please try again."
                )
            except Exception:
                log.exception("failed to deliver error reply to Discord")
            await self._acknowledge_folded_interactions(
                requests[:-1],
                "This message was folded into a combined request, but the request failed.",
            )

    async def _call_chat(self, target: "PendingChatRequest", combined_message: str) -> dict[str, Any]:
        return await self.api.chat(
            actor_id=target.actor_id,
            user_key=target.user_key,
            session_key=target.session_key,
            message=combined_message,
            memory_scope=target.memory_scope,
            target_user_id=target.target_user_id,
            return_sources=False,
            display_name=target.display_name,
            channel_kind=target.channel_kind,
            channel_label=target.channel_label,
        )
        if len(requests) > 1:
            await self._acknowledge_folded_interactions(
                requests[:-1],
                "I folded this into the combined response.",
            )

    def _combined_chat_message(self, requests: list[PendingChatRequest]) -> str:
        if len(requests) == 1:
            return requests[0].message_text
        lines = ["The user sent these messages in sequence. Answer them together.", ""]
        for index, request in enumerate(requests, start=1):
            lines.append(f"{index}. {request.message_text}")
        return "\n".join(lines)

    async def _send_chat_response(self, request: PendingChatRequest, text: str) -> None:
        if request.interaction is not None:
            await self._send_interaction_text(request.interaction, text)
            return
        if request.discord_message is not None:
            await self._send_channel_text(
                request.discord_message.channel,
                text,
                reference=request.discord_message,
            )

    async def _send_chat_error(self, request: PendingChatRequest, text: str) -> None:
        if request.interaction is not None:
            await request.interaction.followup.send(
                text,
                allowed_mentions=self.safe_mentions,
            )
            return
        if request.discord_message is not None:
            await request.discord_message.channel.send(
                text,
                reference=request.discord_message.to_reference(fail_if_not_exists=False),
                allowed_mentions=self.safe_mentions,
            )

    async def _acknowledge_folded_interactions(
        self,
        requests: list[PendingChatRequest],
        text: str,
    ) -> None:
        for request in requests:
            if request.interaction is None:
                continue
            try:
                await request.interaction.followup.send(
                    text,
                    allowed_mentions=self.safe_mentions,
                )
            except discord.HTTPException:
                pass

    async def _handle_promote(self, message: discord.Message, *, scope: str, note: str) -> None:
        try:
            record = await self.api.promote_memory(
                actor_id=self._actor_id(message.author.id),
                scope=scope,
                text=note,
            )
        except RuntimeError as exc:
            await self._send_channel_text(
                message.channel,
                f"Memory promote failed: {exc}",
                reference=message,
            )
            return

        memory = record.get("memory", {})
        await self._send_channel_text(
            message.channel,
            (
                f"Stored {scope} memory: {memory.get('title') or 'note'}"
            ),
            reference=message,
        )

    async def _handle_sources(self, message: discord.Message) -> None:
        try:
            result = await self.api.session_history(
                user_key=self._user_key(message.author.id),
                session_key=self._session_key_for_channel(
                    user_id=message.author.id,
                    channel=message.channel,
                ),
                limit=30,
            )
        except RuntimeError as exc:
            await self._send_channel_text(
                message.channel,
                f"Could not fetch sources: {exc}",
                reference=message,
            )
            return

        history = result.get("history", [])
        last_sources: list[dict[str, Any]] = []
        for entry in reversed(history):
            if entry.get("role") == "assistant":
                meta = entry.get("meta", {})
                if isinstance(meta, dict):
                    last_sources = meta.get("sources", []) or []
                break

        if not last_sources:
            await self._send_channel_text(
                message.channel,
                "No stored grounding sources for the last assistant reply in this session.",
                reference=message,
            )
            return

        lines = ["Last grounding sources:"]
        for source in last_sources:
            label = (
                f"- [{source.get('scope', 'unknown')}][{source.get('source_type', 'unknown')}] "
                f"{source.get('title', 'source')}"
            )
            score = source.get("match_score")
            if score is not None:
                label += f" (score {score})"
            preview = source.get("text_preview")
            if preview:
                label += f": {preview}"
            lines.append(label)

        await self._send_channel_text(
            message.channel,
            "\n".join(lines),
            reference=message,
        )

    def _interaction_allowed(self, interaction: discord.Interaction) -> bool:
        channel = interaction.channel
        if channel is None:
            return True
        return self._channel_allowed(channel)

    def _channel_allowed(self, channel: discord.abc.MessageableChannel) -> bool:
        if getattr(channel, "guild", None) is None:
            return True
        if not self.config.allowed_channel_ids:
            return True

        channel_id = getattr(channel, "id", None)
        parent_id = getattr(channel, "parent_id", None)
        return (
            channel_id in self.config.allowed_channel_ids
            or parent_id in self.config.allowed_channel_ids
        )

    def _is_auto_reply_channel(self, channel: discord.abc.MessageableChannel) -> bool:
        channel_id = getattr(channel, "id", None)
        parent_id = getattr(channel, "parent_id", None)
        return (
            channel_id in self.config.auto_reply_channel_ids
            or parent_id in self.config.auto_reply_channel_ids
        )

    def _should_reply_in_guild_message(self, message: discord.Message) -> bool:
        if self._is_auto_reply_channel(message.channel):
            return True
        if not self.config.require_mention_in_guilds:
            return True
        assert self.user is not None
        return self.user in message.mentions

    def _strip_bot_mentions(self, text: str) -> str:
        assert self.user is not None
        pattern = rf"<@!?{self.user.id}>"
        return re.sub(pattern, " ", text)

    def _user_key(self, user_id: int) -> str:
        return f"discord-user-{user_id}"

    def _actor_id(self, user_id: int) -> str:
        return str(user_id)

    def _interaction_age_seconds(self, interaction: discord.Interaction) -> float:
        created_at = interaction.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - created_at).total_seconds())

    def _session_key_for_channel(self, *, user_id: int, channel: discord.abc.MessageableChannel | None) -> str:
        if channel is None:
            return f"discord-unknown-user-{user_id}"
        guild = getattr(channel, "guild", None)
        if guild is None:
            return f"discord-dm-user-{user_id}"
        channel_id = getattr(channel, "id", 0)
        kind = "thread" if isinstance(channel, discord.Thread) else "channel"
        if self.config.group_sessions:
            # One shared conversation per channel: everyone's messages and the
            # bot's replies form a single attributed transcript.
            return f"discord-guild-{guild.id}-{kind}-{channel_id}"
        return f"discord-guild-{guild.id}-{kind}-{channel_id}-user-{user_id}"

    def _channel_kind_label(self, channel: discord.abc.MessageableChannel | None) -> tuple[str, str]:
        guild = getattr(channel, "guild", None)
        if channel is None or guild is None:
            return "dm", ""
        name = getattr(channel, "name", "") or str(getattr(channel, "id", ""))
        return "group", f"#{name} ({guild.name})"

    def _resolve_mentions(self, message: discord.Message, text: str) -> str:
        """Replace raw <@id> mention tokens with @DisplayName so the model and
        the stored transcript read like human conversation."""
        for member in message.mentions:
            text = re.sub(rf"<@!?{member.id}>", f"@{member.display_name}", text)
        return text

    async def _handle_message_reset(self, message: discord.Message) -> None:
        try:
            await self.api.reset_session(
                user_key=self._user_key(message.author.id),
                session_key=self._session_key_for_channel(
                    user_id=message.author.id,
                    channel=message.channel,
                ),
            )
        except RuntimeError as exc:
            await message.channel.send(
                f"Session reset failed: {exc}",
                reference=message.to_reference(fail_if_not_exists=False),
                allowed_mentions=self.safe_mentions,
            )
            return

        await self._send_channel_text(
            message.channel,
            "Started a fresh session for this Discord context.",
            reference=message,
        )

    async def _send_channel_text(
        self,
        channel: discord.abc.Messageable,
        text: str,
        *,
        reference: discord.Message | None = None,
    ) -> None:
        for index, chunk in enumerate(split_for_discord(text)):
            kwargs: dict[str, Any] = {"allowed_mentions": self.safe_mentions}
            if reference is not None and index == 0:
                kwargs["reference"] = reference.to_reference(fail_if_not_exists=False)
            await channel.send(chunk, **kwargs)

    async def _send_interaction_text(
        self,
        interaction: discord.Interaction,
        text: str,
    ) -> None:
        for chunk in split_for_discord(text):
            await interaction.followup.send(
                chunk,
                allowed_mentions=self.safe_mentions,
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chatbot-base-url",
        default=os.environ.get("CHATBOT_BASE_URL", "http://127.0.0.1:8787"),
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=int,
        # 150s: hard questions legitimately take ~2min (codex-exec turns + budgeted
        # retrieval). The bridge always replies on failure now, so a long timeout
        # no longer means long silence.
        default=int(os.environ.get("CHATBOT_REQUEST_TIMEOUT_SECONDS", "150")),
    )
    return parser.parse_args()


def load_config(args: argparse.Namespace) -> BridgeConfig:
    discord_bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not discord_bot_token:
        raise SystemExit("DISCORD_BOT_TOKEN is required")

    allowed_channel_ids = parse_id_set(
        os.environ.get("DISCORD_ALLOWED_CHANNEL_IDS", ""),
        label="DISCORD_ALLOWED_CHANNEL_IDS",
    )
    auto_reply_channel_ids = parse_id_set(
        os.environ.get("DISCORD_AUTO_REPLY_CHANNEL_IDS", ""),
        label="DISCORD_AUTO_REPLY_CHANNEL_IDS",
    )
    raw_max_output_tokens = os.environ.get("DISCORD_MAX_OUTPUT_TOKENS", "").strip()
    raw_command_guild_id = os.environ.get("DISCORD_COMMAND_GUILD_ID", "").strip()
    raw_message_coalesce_seconds = os.environ.get("DISCORD_MESSAGE_COALESCE_SECONDS", "1.25").strip()

    try:
        max_output_tokens = int(raw_max_output_tokens) if raw_max_output_tokens else None
    except ValueError as exc:
        raise SystemExit("DISCORD_MAX_OUTPUT_TOKENS must be an integer") from exc

    try:
        command_guild_id = int(raw_command_guild_id) if raw_command_guild_id else None
    except ValueError as exc:
        raise SystemExit("DISCORD_COMMAND_GUILD_ID must be an integer") from exc

    try:
        message_coalesce_seconds = float(raw_message_coalesce_seconds)
    except ValueError as exc:
        raise SystemExit("DISCORD_MESSAGE_COALESCE_SECONDS must be a number") from exc

    return BridgeConfig(
        discord_bot_token=discord_bot_token,
        chatbot_base_url=args.chatbot_base_url,
        allowed_channel_ids=allowed_channel_ids,
        auto_reply_channel_ids=auto_reply_channel_ids,
        require_mention_in_guilds=env_bool("DISCORD_REQUIRE_MENTION_IN_GUILDS", True),
        enable_message_content=env_bool("DISCORD_ENABLE_MESSAGE_CONTENT", True),
        use_trajectory_memory=env_bool("DISCORD_USE_TRAJECTORY_MEMORY", True),
        request_timeout_seconds=args.request_timeout_seconds,
        max_output_tokens=max_output_tokens,
        command_guild_id=command_guild_id,
        message_coalesce_seconds=max(0.0, message_coalesce_seconds),
        group_sessions=env_bool("DISCORD_GROUP_SESSIONS", True),
        observe_channels=env_bool("DISCORD_OBSERVE_CHANNELS", True),
        set_guild_nickname=env_bool("DISCORD_SET_GUILD_NICKNAME", True),
    )


def main() -> None:
    args = parse_args()
    config = load_config(args)
    api = ChatbotApi(config)
    client = DiscordBridgeClient(config, api)
    client.run(config.discord_bot_token, reconnect=True)


if __name__ == "__main__":
    main()
