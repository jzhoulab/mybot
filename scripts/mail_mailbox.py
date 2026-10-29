#!/usr/bin/env python3
"""Give a teammate's mybot its own mailbox on the team's mail Worker.

    python scripts/mail_mailbox.py add mybot-alex@zhoulab.io --owner alex@gmail.com
    python scripts/mail_mailbox.py add mybot-me@zhoulab.io --owner me@gmail.com --write-env .env
    python scripts/mail_mailbox.py list

`add` stores the mailbox (owners + the hash of a fresh token) in the Worker's
KV, routes the address to the Worker, and registers the owner as a
destination address (Cloudflare emails them a one-time verification link if
they aren't verified yet; until they click it the bot can't reply). It prints
the four MYBOT_MAIL_* lines for that person's .env — the token is shown once
and only its hash is stored. Re-running `add` rotates the token.

Needs wrangler logged in to the account that owns the domain, and
mail-worker/wrangler.jsonc (see wrangler.jsonc.example).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parent.parent / "mail-worker"
WORKER_NAME = "mybot-mail"
ADDRESS_RE = re.compile(r"^[a-z0-9._+-]+@[a-z0-9.-]+\.[a-z]{2,}$")


def wrangler(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["npx", "--no-install", "wrangler", *args], cwd=WORKER_DIR,
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        sys.exit(f"wrangler {' '.join(args[:3])} failed:\n{proc.stderr[-2000:] or proc.stdout[-2000:]}")
    return proc.stdout


def worker_url(given: str | None) -> str:
    """The Worker's URL: --url, else mail-worker/.url (written at deploy)."""
    if given:
        return given.rstrip("/")
    saved = WORKER_DIR / ".url"
    if saved.exists():
        return saved.read_text().strip().rstrip("/")
    sys.exit("pass --url https://mybot-mail.<subdomain>.workers.dev (or put it in mail-worker/.url)")


def add(address: str, owners: list[str], write_env: str | None, url: str | None) -> None:
    url = worker_url(url)
    address = address.lower()
    owners = [o.lower() for o in owners]
    for value in [address, *owners]:
        if not ADDRESS_RE.match(value):
            sys.exit(f"not an email address: {value}")
    domain = address.split("@", 1)[1]
    token = secrets.token_urlsafe(32)
    entry = {"owners": owners, "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(entry, handle)
        path = handle.name
    try:
        wrangler("kv", "key", "put", f"mailbox/{address}", "--binding", "MAIL_QUEUE", "--remote", "--path", path)
    finally:
        os.unlink(path)
    print(f"mailbox {address} → owners {', '.join(owners)}")

    rules = wrangler("email", "routing", "rules", "list", domain)
    if f"to:{address}" in rules:
        print(f"routing rule for {address} already exists")
    else:
        wrangler("email", "routing", "rules", "create", domain, "--name", f"mybot mailbox {address}",
                 "--match-type", "literal", "--match-field", "to", "--match-value", address,
                 "--action-type", "worker", "--action-value", WORKER_NAME)
        print(f"routed {address} to the {WORKER_NAME} Worker")

    verified = wrangler("email", "routing", "addresses", "list")
    for owner in owners:
        if owner in verified:
            print(f"{owner} is already a destination address")
        else:
            wrangler("email", "routing", "addresses", "create", owner)
            print(f"{owner}: Cloudflare sent a verification email — click it before the bot can reply")

    lines = [
        f"MYBOT_MAIL_URL={url}",
        f"MYBOT_MAIL_TOKEN={token}",
        f"MYBOT_MAIL_ADDRESS={address}",
        f"MYBOT_MAIL_OWNER_ADDRESSES={','.join(owners)}",
    ]
    if write_env:
        env_path = Path(write_env)
        kept = [line for line in (env_path.read_text().splitlines() if env_path.exists() else [])
                if not line.startswith(("MYBOT_MAIL_URL=", "MYBOT_MAIL_TOKEN=", "MYBOT_MAIL_ADDRESS=",
                                        "MYBOT_MAIL_OWNER_ADDRESSES="))]
        tmp = env_path.with_name(env_path.name + ".tmp")
        tmp.write_text("\n".join(kept + lines) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(env_path)
        print(f"wrote the MYBOT_MAIL_* settings to {env_path}")
    else:
        print("\nAdd these to that person's mybot .env (the token is shown only now):\n")
        print("\n".join(lines))


def list_boxes() -> None:
    out = wrangler("kv", "key", "list", "--binding", "MAIL_QUEUE", "--remote", "--prefix", "mailbox/")
    for key in json.loads(out or "[]"):
        print(key["name"].split("/", 1)[1])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    add_p = sub.add_parser("add", help="create or re-key a mailbox")
    add_p.add_argument("address")
    add_p.add_argument("--owner", action="append", required=True, help="the person's own address (repeatable)")
    add_p.add_argument("--write-env", help="write the settings into this .env instead of printing them")
    add_p.add_argument("--url", help="the Worker's URL (default: mail-worker/.url)")
    sub.add_parser("list", help="list mailboxes")
    args = parser.parse_args()
    if not (WORKER_DIR / "wrangler.jsonc").exists():
        sys.exit("mail-worker/wrangler.jsonc is missing — see mail-worker/wrangler.jsonc.example")
    if args.command == "add":
        add(args.address, args.owner, args.write_env, args.url)
    else:
        list_boxes()


if __name__ == "__main__":
    main()
