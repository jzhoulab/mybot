from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class SyncTokenRecord:
    token_hash: str
    actor_id: str
    display_name: str
    enabled: bool
    allowed_sources: list[str] | None


class SyncAuthStore:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path

    def configured(self) -> bool:
        return os.path.exists(self.config_path)

    def load(self) -> list[SyncTokenRecord]:
        if not self.configured():
            return []
        with open(self.config_path) as handle:
            data = json.load(handle)
        if isinstance(data, list):
            records_raw = data
        elif isinstance(data, dict):
            records_raw = data.get("tokens", [])
        else:
            records_raw = []
        records: list[SyncTokenRecord] = []
        for item in records_raw:
            if not isinstance(item, dict):
                continue
            token_hash = str(item.get("token_hash") or "").strip().lower()
            actor_id = str(item.get("actor_id") or "").strip()
            if not token_hash or not actor_id:
                continue
            allowed_sources = item.get("allowed_sources")
            if isinstance(allowed_sources, list):
                parsed_sources = [str(value).strip().lower() for value in allowed_sources if str(value).strip()]
            else:
                parsed_sources = None
            records.append(
                SyncTokenRecord(
                    token_hash=token_hash,
                    actor_id=actor_id,
                    display_name=str(item.get("display_name") or actor_id).strip(),
                    enabled=bool(item.get("enabled", True)),
                    allowed_sources=parsed_sources,
                )
            )
        return records

    def authenticate(self, authorization_header: str | None, actor_id: str) -> SyncTokenRecord:
        if not self.configured():
            raise PermissionError("sync token config is not present")
        if not authorization_header or not authorization_header.startswith("Bearer "):
            raise PermissionError("missing bearer token")
        token = authorization_header.split(" ", 1)[1].strip()
        if not token:
            raise PermissionError("missing bearer token")
        token_hash = hash_token(token)
        for record in self.load():
            if record.token_hash != token_hash:
                continue
            if not record.enabled:
                raise PermissionError("token is disabled")
            if record.actor_id != actor_id:
                raise PermissionError("token is not authorized for this actor_id")
            return record
        raise PermissionError("token not recognized")


def example_token_payload(actor_id: str, display_name: str, token: str, allowed_sources: list[str] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "token_hash": hash_token(token),
        "actor_id": actor_id,
        "display_name": display_name,
        "enabled": True,
    }
    if allowed_sources:
        payload["allowed_sources"] = allowed_sources
    return payload
