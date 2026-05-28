from __future__ import annotations

import re
from typing import Any


SECRET_KEYS = {
    "access_token",
    "authorization",
    "client_secret",
    "cookie",
    "cookies",
    "password",
    "refresh_token",
    "secret",
    "token",
}

SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)(sp_dc=)[^;\s]+"),
    re.compile(r"(?i)(sp_key=)[^;\s]+"),
    re.compile(r"(?i)((?:access|refresh)_token[\"'=:\s]+)[^\"'\s,}]+"),
]


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if str(key).lower() in SECRET_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    result = text
    for pattern in SECRET_PATTERNS:
        result = pattern.sub(r"\1[REDACTED]", result)
    return result
