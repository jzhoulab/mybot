"""Email as a way to reach mybot: the owner writes to the bot's address and
gets the answer back as a reply in the same thread.

Mail arrives through a small Cloudflare Worker (mail-worker/) that serves a
mailbox for each teammate's mybot (e.g. mybot-<name>@zhoulab.io): Email
Routing hands it each message, it keeps only mail whose From is that
mailbox's owner, and queues it. This thread polls that queue over HTTPS — nothing
on the Mac is exposed — and sends replies back through the Worker, whose
send_email binding Cloudflare restricts to the owner's verified addresses.

Trust: a From line is trivially forged, so a message counts as the owner's
only if it carries a valid DKIM signature from the From address's own domain
(checked here, against DNS, whatever the Worker decided). Anything else is
dropped unanswered. Mail is answered in the owner's own view, like the
menu app, so it never goes to anyone but the owner.

Attachments the owner sends are saved to ~/Downloads (or recognized when the
identical file is already there) and the answer refers to them by path.

Config (.env): MYBOT_MAIL_URL, MYBOT_MAIL_TOKEN, MYBOT_MAIL_ADDRESS (the bot's
address) and MYBOT_MAIL_OWNER_ADDRESSES (comma-separated).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Any, Callable

from app.api_auth import authorize
from app.logging_setup import get_logger

log = get_logger("mybot.email", "email.log")

DOWNLOADS = Path.home() / "Downloads"
MAX_ATTACHMENT = 50 * 1024 * 1024
_SIG_DOMAIN = re.compile(rb"(?:^|;)\s*d\s*=\s*([^;\s]+)", re.I)


class MailConfig:
    def __init__(self) -> None:
        env = os.environ.get
        self.url = (env("MYBOT_MAIL_URL") or "").strip().rstrip("/")
        self.token = (env("MYBOT_MAIL_TOKEN") or "").strip()
        self.address = (env("MYBOT_MAIL_ADDRESS") or "").strip()
        self.owners = {
            a.strip().lower() for a in re.split(r"[,\s]+", env("MYBOT_MAIL_OWNER_ADDRESSES") or "") if a.strip()
        }
        self.poll_seconds = max(10, int(env("MYBOT_MAIL_POLL_SECONDS") or "30"))

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token and self.address and self.owners)


# ------------------------------------------------------------------ checking

def dkim_ok(raw: bytes, from_address: str, verify: Callable[[bytes, int], bool] | None = None) -> bool:
    """True when some DKIM signature on the message verifies and was made by
    the From address's domain (or a parent of it) — i.e. DMARC-style alignment."""
    domain = from_address.rsplit("@", 1)[-1].lower()
    if not domain:
        return False
    import dkim  # dkimpy

    try:
        signatures = [v for k, v in dkim.DKIM(raw).headers if k.lower() == b"dkim-signature"]
    except Exception:
        signatures = []
    if verify is None:
        def verify(message: bytes, index: int) -> bool:
            return bool(dkim.DKIM(message).verify(idx=index))
    for index, value in enumerate(signatures):
        match = _SIG_DOMAIN.search(value.replace(b"\r\n", b"").replace(b"\n", b""))
        signer = match.group(1).decode("ascii", "ignore").lower() if match else ""
        if signer and (domain == signer or domain.endswith("." + signer)):
            try:
                if verify(raw, index):
                    return True
            except Exception as exc:
                log.info("dkim check raised: %s", exc)
    return False


def sender(msg: EmailMessage) -> str:
    return parseaddr(str(msg.get("From") or ""))[1].strip().lower()


def is_automatic(msg: EmailMessage) -> bool:
    auto = str(msg.get("Auto-Submitted") or "no").strip().lower()
    return auto != "no" or str(msg.get("Precedence") or "").lower() in {"bulk", "list", "junk"}


# ------------------------------------------------------------------- reading

def body_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        import html

        text = html.unescape(text)
    return strip_quoted(text)


def strip_quoted(text: str) -> str:
    """The new part of a reply: drop the quoted history below it."""
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if re.match(r"^\s*On .{4,200}wrote:\s*$", line) or line.strip() in {"-----Original Message-----"}:
            break
        if line.startswith(">"):
            continue
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def save_attachments(msg: EmailMessage, downloads: Path | None = None) -> list[dict[str, str]]:
    """Each attachment, as a file in Downloads: the identical file if it's
    already there, else saved there under a free name."""
    downloads = downloads or DOWNLOADS
    saved = []
    for part in msg.iter_attachments():
        name = Path(str(part.get_filename() or "attachment")).name.strip() or "attachment"
        try:
            data = part.get_content()
        except (LookupError, ValueError):
            data = part.get_payload(decode=True) or b""
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not data or len(data) > MAX_ATTACHMENT:
            continue
        digest = hashlib.sha256(data).hexdigest()
        existing = _same_file(downloads, name, digest)
        if existing:
            saved.append({"name": name, "path": str(existing), "status": "already in Downloads"})
            continue
        target = _free_name(downloads, name)
        downloads.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        saved.append({"name": name, "path": str(target), "status": "saved to Downloads"})
    return saved


def _same_file(folder: Path, name: str, digest: str) -> Path | None:
    stem, suffix = Path(name).stem, Path(name).suffix
    candidates = [folder / name, *sorted(folder.glob(f"{glob_escape(stem)} (*){glob_escape(suffix)}"))]
    for path in candidates:
        try:
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                return path
        except OSError:
            continue
    return None


def glob_escape(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)


def _free_name(folder: Path, name: str) -> Path:
    path = folder / name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 1
    while path.exists():
        path = folder / f"{stem} ({n}){suffix}"
        n += 1
    return path


def thread_root(msg: EmailMessage) -> str:
    refs = re.findall(r"<[^>]+>", str(msg.get("References") or ""))
    return refs[0] if refs else str(msg.get("In-Reply-To") or msg.get("Message-ID") or "").strip()


# ------------------------------------------------------------------- writing

def build_reply(*, original: EmailMessage, bot_address: str, to: str, text: str,
                images: dict[str, Path] | None = None) -> EmailMessage:
    """A threaded reply: plain text plus HTML (markdown rendered), with any
    `![caption](figure:name)` images embedded inline."""
    subject = str(original.get("Subject") or "").strip() or "(no subject)"
    reply = new_message(bot_address=bot_address, to=to,
                        subject=subject if subject.lower().startswith("re:") else f"Re: {subject}",
                        text=text, images=images)
    message_id = str(original.get("Message-ID") or "").strip()
    if message_id:
        refs = " ".join(re.findall(r"<[^>]+>", str(original.get("References") or "")) + [message_id])
        reply["In-Reply-To"] = message_id
        reply["References"] = refs
    return reply


def new_message(*, bot_address: str, to: str, subject: str, text: str,
                images: dict[str, Path] | None = None) -> EmailMessage:
    import markdown

    images = images or {}
    msg = EmailMessage()
    msg["From"] = f"mybot <{bot_address}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=bot_address.rsplit("@", 1)[-1])
    # Marks it as machine-written, so no auto-responder answers it back.
    msg["Auto-Submitted"] = "auto-replied"
    cids: dict[str, str] = {}

    def image_line(match: re.Match[str]) -> str:
        caption, name = match.group(1).strip(), match.group(2)
        if name not in images:
            return f"*Figure: {caption}*" if caption else ""
        cids[name] = make_msgid(domain="mybot.local")[1:-1]
        return f'<figure><img src="cid:{cids[name]}" alt="{_attr(caption)}" style="max-width:100%">' \
               f"<figcaption>{_attr(caption)}</figcaption></figure>"

    from app.pulse_figures import FIGURE_RE, as_captions

    plain = as_captions(text)
    html_body = markdown.markdown(FIGURE_RE.sub(image_line, text), extensions=["extra", "sane_lists"])
    msg.set_content(plain)
    msg.add_alternative(
        "<html><body style=\"font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:15px;"
        f"line-height:1.5;max-width:720px\">{html_body}</body></html>",
        subtype="html",
    )
    html_part = msg.get_payload()[-1]
    for name, cid in cids.items():
        html_part.add_related(Path(images[name]).read_bytes(), "image", "png", cid=f"<{cid}>",
                              filename=name, disposition="inline")
    return msg


def _attr(text: str) -> str:
    import html

    return html.escape(text, quote=True)


# -------------------------------------------------------------------- bridge

class EmailBridge:
    def __init__(self, *, config: MailConfig, server_url: str, owner_actor_id: str, state_dir: str | Path,
                 owner_name: Callable[[], str] = lambda: "") -> None:
        self.config = config
        self.server_url = server_url.rstrip("/")
        self.owner_actor_id = owner_actor_id
        self.owner_name = owner_name
        self.seen_path = Path(state_dir) / "email_seen.json"
        self._seen: list[str] = self._load_seen()
        self._send_lock = threading.Lock()

    # -- worker calls
    def _worker(self, method: str, path: str, payload: dict[str, Any] | None = None, timeout: int = 60) -> dict[str, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(f"{self.config.url}{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {self.config.token}", "X-Mybot-Mailbox": self.config.address,
            "Content-Type": "application/json",
            "User-Agent": "mybot-mail/1",
        })
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def send(self, to: str, msg: EmailMessage) -> None:
        if to.lower() not in self.config.owners:
            raise ValueError("mybot only emails its owner")
        raw = base64.b64encode(msg.as_bytes(policy=policy.SMTP)).decode("ascii")
        with self._send_lock:
            result = self._worker("POST", "/send", {"to": to, "raw": raw}, timeout=120)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "send failed"))

    def send_to_owner(self, subject: str, text: str, images: dict[str, Path] | None = None) -> bool:
        """Mail the owner something mybot started (e.g. the morning pulse)."""
        to = sorted(self.config.owners)[0]
        try:
            self.send(to, new_message(bot_address=self.config.address, to=to, subject=subject,
                                      text=text, images=images))
            return True
        except Exception as exc:
            log.warning("could not email the owner: %s", exc)
            return False

    # -- loop
    def start(self) -> None:
        threading.Thread(target=self._loop, name="email-bridge", daemon=True).start()
        log.info("email bridge polling %s for %s", self.config.url, self.config.address)

    def _loop(self) -> None:
        while True:
            time.sleep(self.config.poll_seconds)  # first, let the server come up
            try:
                self.poll_once()
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.warning("mail poll failed: %s", exc)
            except Exception:  # pragma: no cover - keep polling
                log.exception("mail poll crashed")

    def poll_once(self) -> int:
        inbox = self._worker("GET", "/inbox")
        handled = 0
        for item in inbox.get("messages") or []:
            try:
                self.handle(base64.b64decode(str(item.get("raw") or "")))
            except Exception:
                # A message that breaks us once would break us forever: log
                # it and move on rather than wedge the queue.
                log.exception("mail %s failed", item.get("id"))
            self._worker("POST", "/ack", {"ids": [item.get("id")]})
            handled += 1
        return handled

    def handle(self, raw: bytes) -> str:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        from_address = sender(msg)
        message_id = str(msg.get("Message-ID") or "").strip()
        if message_id and message_id in self._seen:
            return "duplicate"
        if from_address not in self.config.owners:
            log.info("ignored mail from a non-owner address")
            return "not owner"
        if from_address == self.config.address.lower() or is_automatic(msg):
            return "automatic"
        if not dkim_ok(raw, from_address):
            log.warning("ignored mail claiming to be from the owner without a valid DKIM signature")
            return "unverified"
        self._remember(message_id)
        attachments = save_attachments(msg)
        question = self._question(msg, attachments)
        if not question.strip():
            return "empty"
        answer = self._ask(question, thread_root(msg) or message_id)
        self.send(from_address, build_reply(original=msg, bot_address=self.config.address,
                                            to=from_address, text=answer))
        log.info("answered mail (%d chars in, %d out, %d attachments)", len(question), len(answer), len(attachments))
        return "answered"

    def _question(self, msg: EmailMessage, attachments: list[dict[str, str]]) -> str:
        subject = str(msg.get("Subject") or "").strip()
        body = body_text(msg)
        parts = [f"(Sent by email. Subject: {subject})" if subject else "(Sent by email.)", body]
        if attachments:
            parts.append("Attached files (on the owner's Mac; refer to them by path, don't re-send them):\n"
                         + "\n".join(f"- {a['name']}: {a['path']} ({a['status']})" for a in attachments))
        return "\n\n".join(p for p in parts if p)

    def _ask(self, question: str, thread: str) -> str:
        key = hashlib.sha1(thread.encode()).hexdigest()[:16]
        payload = {
            "actor_id": self.owner_actor_id, "user": f"email-user-{self.owner_actor_id}",
            "session_key": f"email-thread-{key}", "message": question[:20000],
            "memory_scope": "private", "use_trajectory_memory": True, "return_sources": False,
            "actor_display_name": self.owner_name() or "", "channel_kind": "dm", "channel_label": "email",
        }
        request = urllib.request.Request(f"{self.server_url}/chat", data=json.dumps(payload).encode(),
                                         method="POST", headers={"Content-Type": "application/json"})
        authorize(request)
        try:
            with urllib.request.urlopen(request, timeout=3600) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            return f"(mybot couldn't answer this one: {exc.code} {detail})"
        except (urllib.error.URLError, OSError) as exc:
            return f"(mybot couldn't answer this one: {exc})"
        return str(result.get("text") or result.get("answer") or "(empty answer)")

    # -- dedupe
    def _load_seen(self) -> list[str]:
        try:
            data = json.loads(self.seen_path.read_text())
            return [str(x) for x in data][-2000:] if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _remember(self, message_id: str) -> None:
        if not message_id:
            return
        self._seen = (self._seen + [message_id])[-2000:]
        tmp = self.seen_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._seen))
        os.chmod(tmp, 0o600)
        tmp.replace(self.seen_path)

