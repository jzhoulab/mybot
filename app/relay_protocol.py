"""The protocol teammates' mybot instances use to talk to each other in a
shared channel (Discord or Slack), in public, on a human's behalf.

A relayed message is ordinary readable text plus one machine-readable trailer
line, so people in the channel see a normal conversation:

    **Alex** (via alex-mybot): @sam-mybot did the nightly run finish?
    -# ⟦mybot ask id=r3f2a actor=1234 name=Alex⟧

    (reply) The nightly run finished at 04:10 …
    -# ⟦mybot ans id=r3f2a⟧

Rules every instance follows:
- Only an ASK is ever answered; an ANSWER is never treated as a new ask, so two
  bots can't loop. A bot never asks on its own initiative, only for a human.
- An ask is answered only by a bot it @mentions, and only if the sender is a
  bot the receiver trusts (a sibling in its connected team channel, or one
  listed in MYBOT_SIBLING_BOT_IDS).
- The claimed asker is namespaced under the sending bot
  (relay-<bot>-<actor>), so a relay can never pass itself off as the owner;
  it always gets the teammate view, and in a channel the answer is public.
- The trailer is valid only as the last line of a message the bridge itself
  composed: model output has relay markers neutralized before it is posted.
"""
from __future__ import annotations

import re
import secrets
from typing import Any
from urllib.parse import quote, unquote

OPEN, CLOSE = "⟦", "⟧"
_FIELDS = r"mybot (ask|ans)([^" + CLOSE + r"]*)"
# Current form: the envelope is the message's last line (Discord renders
# "-# " as small grey subtext; Slack wraps it in _italics_).
_TRAILER_RE = re.compile(r"\n[ \t]*(?:-#[ \t]*|_)?" + OPEN + _FIELDS + CLOSE + r"_?[ \t]*$")
# Original form, still accepted: "<@id> ⟦mybot ask …⟧ question".
_LEADING_RE = re.compile(
    r"^(?:<@!?[A-Za-z0-9]+(?:\|[^>]*)?>\s*)*" + OPEN + _FIELDS + CLOSE + r"\s?(.*)$", re.DOTALL
)
# "**Alex** (via alex-mybot): " / "*Alex* (via alex-mybot): " — who is asking.
_ON_BEHALF_RE = re.compile(r"^\*{1,2}([^*\n]{1,80})\*{1,2} \(via [^)\n]{1,80}\):\s*")


def new_request_id() -> str:
    return "r" + secrets.token_hex(4)


def _fields(raw: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in raw.split():
        if "=" in token:
            key, value = token.split("=", 1)
            fields[key] = value
    return fields


def envelope(kind: str, request_id: str, *, actor_id: str = "", name: str = "") -> str:
    parts = [f"mybot {kind}", f"id={request_id}"]
    if kind == "ask":
        parts += [f"actor={quote(actor_id, safe='')}", f"name={quote(name or '', safe='')}"]
    return OPEN + " ".join(parts) + CLOSE


def trailer(platform: str, kind: str, request_id: str, *, actor_id: str = "", name: str = "") -> str:
    env = envelope(kind, request_id, actor_id=actor_id, name=name)
    return f"\n-# {env}" if platform == "discord" else f"\n_{env}_"


def on_behalf_prefix(platform: str, person: str, bot_name: str) -> str:
    stars = "**" if platform == "discord" else "*"
    clean = re.sub(r"[*\n()]", "", person).strip()[:80] or "someone"
    return f"{stars}{clean}{stars} (via {bot_name}): "


def neutralize(text: str) -> str:
    """Model output must never carry a relay envelope (a model coaxed into
    printing one would otherwise forge an ask or an answer)."""
    return (text or "").replace(OPEN, "[").replace(CLOSE, "]")


def parse(text: str) -> dict[str, Any] | None:
    """{kind: 'ask'|'ans', id, actor_id, name, body, asker} or None.
    `body` is the human-readable text without the envelope or the
    "**Name** (via bot):" prefix; `asker` is that prefix's name, if any."""
    raw = (text or "").rstrip()
    match = _TRAILER_RE.search(raw)
    if match:
        kind, fields_raw = match.group(1), match.group(2)
        body = raw[: match.start()].strip()
    else:
        match = _LEADING_RE.match(raw.strip())
        if not match:
            return None
        kind, fields_raw, body = match.group(1), match.group(2), (match.group(3) or "").strip()
    asker, body = split_on_behalf(body)
    fields = _fields(fields_raw)
    return {
        "kind": kind,
        "id": fields.get("id", ""),
        "actor_id": unquote(fields.get("actor", "")),
        "name": unquote(fields.get("name", "")) or asker,
        "body": body,
        "asker": asker,
    }


def split_on_behalf(text: str) -> tuple[str, str]:
    """("Alex", "did the run finish?") from "**Alex** (via alex-mybot): did …"."""
    prefix = _ON_BEHALF_RE.match(text or "")
    if not prefix:
        return "", (text or "").strip()
    return prefix.group(1).strip(), text[prefix.end():].strip()


def strip(text: str) -> str:
    """Text for display: the envelope removed, the prefix kept."""
    raw = (text or "").rstrip()
    match = _TRAILER_RE.search(raw)
    if match:
        return raw[: match.start()].rstrip()
    match = _LEADING_RE.match(raw.strip())
    return (match.group(3) or "").strip() if match else raw


def relay_actor(sender_bot_id: str, claimed_actor: str) -> str:
    """The asker as the receiver records it: always under the sending bot."""
    claimed = re.sub(r"[^\w.-]", "", claimed_actor or "")[:64]
    return f"relay-{sender_bot_id}-{claimed}" if claimed else ""


class RateLimiter:
    """At most `limit` relay asks per sending bot per `window` seconds — a
    misbehaving sibling can't run up its neighbours' agent bills."""

    def __init__(self, limit: int = 6, window: float = 60.0) -> None:
        self.limit, self.window = limit, window
        self._seen: dict[str, list[float]] = {}

    def allow(self, key: str, now: float) -> bool:
        recent = [t for t in self._seen.get(key, []) if now - t < self.window]
        if len(recent) >= self.limit:
            self._seen[key] = recent
            return False
        self._seen[key] = recent + [now]
        return True
