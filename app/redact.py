"""Mask credentials before anything leaves the server.

Transcripts routinely contain secrets — `cat .env`, an API call with its
Authorization header, a token pasted into a prompt. The index keeps the raw
text (the owner's own machine); every response the server sends — search
hits, read windows, SQL rows, chat answers — passes through `redact_text`,
so no asker, teammate or agent ever sees a credential, whatever it asks.

Patterns are matched on serialized JSON too, so values never span a quote or
backslash.
"""
from __future__ import annotations

import re

_VALUE = r"[^\s\"'\\`<>\[\],;(){}]"

# (pattern, replacement). Specific token formats first; generic last.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL),
     "[redacted private key]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"), "[redacted anthropic key]"),
    (re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}"), "[redacted openai key]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"), "[redacted github token]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "[redacted github token]"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), "[redacted gitlab token]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "[redacted slack token]"),
    (re.compile(r"\bxapp-[A-Za-z0-9-]{10,}"), "[redacted slack token]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[redacted aws key]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[redacted google key]"),
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}"), "[redacted huggingface token]"),
    (re.compile(r"\b[MNO][A-Za-z0-9]{23,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"), "[redacted discord token]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[redacted jwt]"),
    (re.compile(r"\bagt_[A-Za-z0-9_-]{20,}"), "[redacted mybot grant]"),
    # Authorization: Bearer <x>, Basic <x>, token <x>
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic|token)\s+)" + _VALUE + r"{12,}"),
     r"\1[redacted]"),
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{20,}"), r"\1[redacted]"),
    # user:password@host in URLs
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@\"'\\]+:)[^\s@/\"'\\]{3,}(@)"), r"\1[redacted]\2"),
    # NAME=value / "name": "value" where the name ENDS in a secret word
    # (SLACK_BOT_TOKEN, db_password, "api_key") — not max_tokens, token_count.
    (re.compile(
        r"(?i)(\b[A-Z0-9_.-]*(?:token|secret|passw(?:or)?d|passwd|pwd|api[_-]?key|apikey|private[_-]?key|"
        r"access[_-]?key|client[_-]?secret|auth[_-]?key|cookie)"
        r"[\"']?\s*[:=]\s*[\"']?)(" + _VALUE + r"{6,})"),
     r"\1[redacted]"),
]

# Values the generic NAME=value rule would otherwise mask but that are not
# secrets (keeps answers readable: "max_tokens=4096", "token_count: 120").
_HARMLESS_VALUE = re.compile(r"^(?:\d{1,9}|true|false|none|null|\[redacted[^\]]*\]|\$\{?[A-Z_]+\}?|<[^>]*>|\.{3}|x+"
                            # code, not a credential: token = self.embed / process.env.KEY
                            r"|[a-z_]+(?:\.[a-z_]+)+)$", re.I)


def redact_text(text: str) -> str:
    if not text:
        return text
    for pattern, replacement in _PATTERNS[:-1]:
        text = pattern.sub(replacement, text)

    def _assignment(match: re.Match[str]) -> str:
        if _HARMLESS_VALUE.match(match.group(2)):
            return match.group(0)
        return f"{match.group(1)}[redacted]"

    return _PATTERNS[-1][0].sub(_assignment, text)


def extra_literal_secrets(text: str, literals: list[str]) -> str:
    """Mask exact known secret values (e.g. the server's own API key)."""
    for literal in literals:
        if literal and len(literal) >= 12:
            text = text.replace(literal, "[redacted]")
    return text
