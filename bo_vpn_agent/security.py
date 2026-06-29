from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


SENSITIVE_KEYS = {"password", "vpn_password", "secret", "token", "private_key"}


def check_bearer(auth_header: str | None, expected_token: str) -> bool:
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    supplied = auth_header.removeprefix("Bearer ").strip()
    return hmac.compare_digest(supplied, expected_token)


def mask_username(username: str) -> str:
    if not username:
        return ""
    if len(username) <= 2:
        return "*" * len(username)
    return f"{username[:1]}***{username[-1:]}"


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in SENSITIVE_KEYS:
                redacted[key] = "***"
            elif key.lower() == "username":
                redacted[key] = mask_username(str(item))
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def fingerprint_request(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
