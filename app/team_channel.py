"""The team channel: one shared Discord/Slack channel the owner connects, where
teammates and their mybot instances talk in public.

The bridge mirrors that channel here (POST /channel/feed) and reports which
channels it can see (POST /channel/sync). The menu app reads the feed and
posts through it (GET/POST /channel): the bridge picks posts up from
/channel/outbox and sends them as this bot, on the owner's behalf. An @mention
of a teammate's bot becomes a relayed ask (app/relay_protocol.py); an
@mention of this bot is answered by it, in the channel, at teammate level —
the channel is public.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from app import relay_protocol

MAX_MESSAGES = 400          # kept per channel on disk and in memory
ASK_TIMEOUT_SECONDS = 300   # how long the menu shows "<bot> is answering…"


def _now() -> float:
    return time.time()


def _clean(value: Any, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


class TeamChannel:
    def __init__(self, state_dir: str | Path) -> None:
        self.dir = Path(state_dir) / "team_channel"
        self.config_path = Path(state_dir) / "team_channel.json"
        self._lock = threading.RLock()
        self._config = self._load_config()
        self._messages: dict[str, list[dict[str, Any]]] = {}
        self._outbox: list[dict[str, Any]] = []
        self._waiting: dict[str, dict[str, Any]] = {}   # request id -> {bots, created}
        self._seq = 0

    # -- persistence --

    def _load_config(self) -> dict[str, Any]:
        try:
            data = json.loads(self.config_path.read_text())
        except (OSError, ValueError):
            data = {}
        data.setdefault("connected", None)
        data.setdefault("blocked_bots", [])
        data.setdefault("platforms", {})   # platform -> {self_id, self_name, channels, updated}
        return data

    def _save_config(self) -> None:
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._config, indent=1))
        tmp.replace(self.config_path)

    def _feed_path(self, platform: str, channel_id: str) -> Path:
        return self.dir / f"{platform}-{re.sub(r'[^A-Za-z0-9_.-]', '', channel_id)}.jsonl"

    def _feed(self, platform: str, channel_id: str) -> list[dict[str, Any]]:
        key = f"{platform}:{channel_id}"
        if key not in self._messages:
            rows: list[dict[str, Any]] = []
            try:
                for line in self._feed_path(platform, channel_id).read_text().splitlines():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
            except OSError:
                pass
            self._messages[key] = rows[-MAX_MESSAGES:]
            for row in self._messages[key]:
                self._seq = max(self._seq, int(row.get("seq") or 0))
        return self._messages[key]

    def _append(self, platform: str, channel_id: str, row: dict[str, Any]) -> None:
        feed = self._feed(platform, channel_id)
        self._seq += 1
        row["seq"] = self._seq
        feed.append(row)
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._feed_path(platform, channel_id)
        if len(feed) > MAX_MESSAGES * 1.5:
            del feed[: len(feed) - MAX_MESSAGES]
            tmp = path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(r) + "\n" for r in feed))
            tmp.replace(path)
        else:
            with path.open("a") as handle:
                handle.write(json.dumps(row) + "\n")

    # -- bridge side --

    def sync(self, body: dict[str, Any]) -> dict[str, Any]:
        """The bridge reports who it is and which channels it can post in."""
        platform = _clean(body.get("platform"), 20) or "discord"
        channels = []
        for ch in body.get("channels") or []:
            if isinstance(ch, dict) and ch.get("id"):
                channels.append({
                    "id": _clean(ch.get("id"), 40), "name": _clean(ch.get("name"), 100),
                    "guild": _clean(ch.get("guild"), 100), "session_key": _clean(ch.get("session_key"), 200),
                    "label": _clean(ch.get("label"), 200),
                })
        with self._lock:
            self._config["platforms"][platform] = {
                "self_id": _clean(body.get("self_id"), 40), "self_name": _clean(body.get("self_name"), 100),
                "channels": channels[:200], "updated": _now(),
            }
            connected = self._config.get("connected")
            if connected and connected.get("platform") == platform:
                for ch in channels:   # keep the session key/label current
                    if ch["id"] == connected.get("channel_id"):
                        connected.update({k: ch[k] for k in ("name", "guild", "session_key", "label") if ch[k]})
            self._save_config()
            return self.bridge_view(platform)

    def bridge_view(self, platform: str) -> dict[str, Any]:
        connected = self._config.get("connected")
        mine = connected if connected and connected.get("platform") == platform else None
        return {
            "ok": True,
            "connected": mine,
            "blocked_bots": list(self._config.get("blocked_bots") or []),
        }

    def feed(self, body: dict[str, Any], *, owner_name: str) -> dict[str, Any]:
        """Messages the bridge saw in the connected channel (any author)."""
        platform = _clean(body.get("platform"), 20) or "discord"
        channel_id = _clean(body.get("channel_id"), 40)
        added = 0
        with self._lock:
            connected = self._config.get("connected") or {}
            if connected.get("platform") != platform or connected.get("channel_id") != channel_id:
                return {"ok": True, "added": 0, "ignored": "not the connected channel"}
            self_id = (self._config["platforms"].get(platform) or {}).get("self_id", "")
            feed = self._feed(platform, channel_id)
            known = {r.get("id") for r in feed}
            for raw in body.get("messages") or []:
                if not isinstance(raw, dict) or not raw.get("id") or str(raw["id"]) in known:
                    continue
                row = self._row(raw, self_id=self_id, owner_name=owner_name)
                known.add(row["id"])
                self._append(platform, channel_id, row)
                added += 1
                if row.get("relay_kind") == "ans":
                    waiting = self._waiting.get(row.get("relay_id") or "")
                    if waiting:
                        waiting["bots"].pop(row["author_id"], None)
        return {"ok": True, "added": added}

    @staticmethod
    def _row(raw: dict[str, Any], *, self_id: str, owner_name: str) -> dict[str, Any]:
        text = str(raw.get("text") or "")[:8000]
        author_id = _clean(raw.get("author_id"), 40)
        relay = relay_protocol.parse(text)
        display = relay["body"] if relay else relay_protocol.strip(text)
        if relay:
            on_behalf = relay.get("asker") or ""
        else:
            # A plain post a bot made for its owner: "**Name** (via bot): …"
            on_behalf, display = relay_protocol.split_on_behalf(display)
        return {
            "id": _clean(raw.get("id"), 60),
            "ts": float(raw.get("ts") or _now()),
            "author_id": author_id,
            "author_name": _clean(raw.get("author_name"), 100) or author_id,
            "is_bot": bool(raw.get("is_bot")),
            "is_self": bool(self_id and author_id == self_id),
            "on_behalf": on_behalf,
            "text": display,
            "reply_to": _clean(raw.get("reply_to"), 60),
            "relay_kind": relay["kind"] if relay else "",
            "relay_id": relay["id"] if relay else "",
            "mention_ids": [_clean(m, 40) for m in (raw.get("mention_ids") or [])][:20],
        }

    def outbox(self, platform: str) -> dict[str, Any]:
        with self._lock:
            view = self.bridge_view(platform)
            view["items"] = [i for i in self._outbox if i["platform"] == platform and not i.get("sent")]
            return view

    def outbox_ack(self, body: dict[str, Any]) -> dict[str, Any]:
        item_id = _clean(body.get("id"), 40)
        with self._lock:
            for item in self._outbox:
                if item["id"] == item_id and body.get("answered"):
                    self._waiting.pop(f"self-{item_id}", None)
                    return {"ok": True}
                if item["id"] == item_id:
                    item["sent"] = True
                    item["message_id"] = _clean(body.get("message_id"), 60)
                    item["error"] = _clean(body.get("error"), 300)
                    if item["error"]:
                        for request_id in [item.get("relay_id")]:
                            self._waiting.pop(request_id or "", None)
                        self._note(item["platform"], item["channel_id"], f"Couldn't post: {item['error']}")
            # Sent items are only needed briefly (for the echo to land).
            cutoff = _now() - 600
            self._outbox = [i for i in self._outbox if not i.get("sent") or i["created"] > cutoff]
        return {"ok": True}

    def _note(self, platform: str, channel_id: str, text: str) -> None:
        """A local-only line in the feed (never posted), e.g. a failed send."""
        self._append(platform, channel_id, {
            "id": f"note-{self._seq + 1}", "ts": _now(), "author_id": "", "author_name": "mybot",
            "is_bot": True, "is_self": True, "on_behalf": "", "text": text, "reply_to": "",
            "relay_kind": "", "relay_id": "", "mention_ids": [], "local": True,
        })

    # -- menu app side --

    def view(self, *, after: int = 0) -> dict[str, Any]:
        with self._lock:
            connected = self._config.get("connected")
            platforms = self._config.get("platforms") or {}
            available = [
                {**ch, "platform": p}
                for p, info in platforms.items() for ch in info.get("channels") or []
            ]
            out: dict[str, Any] = {
                "ok": True, "connected": connected, "available": available,
                "bridges": {p: {"self_id": i.get("self_id"), "self_name": i.get("self_name"),
                                "updated": i.get("updated")} for p, i in platforms.items()},
                "messages": [], "members": [], "waiting": [], "seq": self._seq,
            }
            if not connected:
                return out
            platform, channel_id = connected["platform"], connected["channel_id"]
            feed = self._feed(platform, channel_id)
            out["messages"] = [r for r in feed if int(r.get("seq") or 0) > after][-200:]
            out["members"] = self._members(platform, feed)
            now = _now()
            for request_id, w in list(self._waiting.items()):
                if now - w["created"] > ASK_TIMEOUT_SECONDS or not w["bots"]:
                    self._waiting.pop(request_id, None)
                    continue
                out["waiting"] += [{"request_id": request_id, "bot_id": b, "name": n} for b, n in w["bots"].items()]
            out["seq"] = self._seq
            return out

    def _members(self, platform: str, feed: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Who can be @mentioned: this bot, then everyone seen in the channel."""
        info = self._config["platforms"].get(platform) or {}
        blocked = set(self._config.get("blocked_bots") or [])
        members: dict[str, dict[str, Any]] = {}
        if info.get("self_id"):
            members[info["self_id"]] = {"id": info["self_id"], "name": info.get("self_name") or "mybot",
                                        "is_bot": True, "is_self": True, "blocked": False, "last": _now()}
        for row in feed:
            author = row.get("author_id")
            if not author or row.get("local") or author in members:
                if author in members:
                    members[author]["last"] = row.get("ts") or 0
                continue
            members[author] = {"id": author, "name": row.get("author_name") or author,
                               "is_bot": bool(row.get("is_bot")), "is_self": False,
                               "blocked": author in blocked, "last": row.get("ts") or 0}
        ordered = sorted(members.values(), key=lambda m: (not m["is_self"], not m["is_bot"], -m["last"]))
        for m in ordered:
            m.pop("last", None)
        return ordered

    def connect(self, platform: str, channel_id: str) -> dict[str, Any]:
        with self._lock:
            info = self._config["platforms"].get(platform) or {}
            match = next((c for c in info.get("channels") or [] if c["id"] == channel_id), None)
            if match is None:
                return {"ok": False, "error": "the bridge hasn't reported that channel"}
            self._config["connected"] = {"platform": platform, "channel_id": channel_id, **{
                k: match[k] for k in ("name", "guild", "session_key", "label")}, "since": _now()}
            self._save_config()
        return self.view()

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            self._config["connected"] = None
            self._save_config()
        return self.view()

    def set_blocked(self, bot_id: str, blocked: bool) -> dict[str, Any]:
        with self._lock:
            current = set(self._config.get("blocked_bots") or [])
            (current.add if blocked else current.discard)(_clean(bot_id, 40))
            self._config["blocked_bots"] = sorted(current)
            self._save_config()
        return self.view()

    def post(self, text: str, mention_ids: list[str], *, owner_name: str, owner_actor_id: str,
             reply_to: str = "") -> dict[str, Any]:
        """Queue the owner's message for the channel. `text` names people as
        @Name; `mention_ids` are the members picked in the composer."""
        text = (text or "").strip()[:3500]
        if not text:
            return {"ok": False, "error": "empty message"}
        with self._lock:
            connected = self._config.get("connected")
            if not connected:
                return {"ok": False, "error": "no team channel connected"}
            platform, channel_id = connected["platform"], connected["channel_id"]
            info = self._config["platforms"].get(platform) or {}
            self_id, self_name = info.get("self_id", ""), info.get("self_name") or "mybot"
            members = {m["id"]: m for m in self._members(platform, self._feed(platform, channel_id))}
            picked = [members[m] for m in dict.fromkeys(mention_ids) if m in members]
            # Only mentions still written in the text count.
            picked = [m for m in picked if f"@{m['name']}" in text]
            wire = relay_protocol.neutralize(text)
            for m in sorted(picked, key=lambda m: -len(m["name"])):
                wire = wire.replace(f"@{m['name']}", f"<@{m['id']}>")
            bots = [m for m in picked if m["is_bot"] and not m["is_self"]]
            asks_self = any(m["is_self"] for m in picked)
            request_id = relay_protocol.new_request_id() if bots else ""
            body = relay_protocol.on_behalf_prefix(platform, owner_name, self_name) + wire
            if bots:
                body += relay_protocol.trailer(platform, "ask", request_id, actor_id=owner_actor_id, name=owner_name)
                self._waiting[request_id] = {"bots": {b["id"]: b["name"] for b in bots}, "created": _now()}
            item = {
                "id": f"p{int(_now() * 1000)}-{len(self._outbox)}", "platform": platform,
                "channel_id": channel_id, "text": body, "mention_ids": [m["id"] for m in picked],
                "reply_to": _clean(reply_to, 60), "relay_id": request_id, "created": _now(),
                # This bot answers its own @mention after posting, as the
                # owner asking in public (teammate-level data).
                "answer_self": {
                    "question": re.sub(rf"@{re.escape(self_name)}\b", "", text).strip(),
                    "actor_id": owner_actor_id, "name": owner_name,
                } if asks_self else None,
            }
            self._outbox.append(item)
            if asks_self:
                self._waiting[f"self-{item['id']}"] = {"bots": {self_id: self_name}, "created": _now()}
        return {"ok": True, "queued": item["id"], "relay_id": request_id}
