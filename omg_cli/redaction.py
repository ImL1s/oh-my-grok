"""Deterministic recursive redaction for all persisted OMG diagnostics.

The redactor is deliberately conservative.  Raw prompts, command bodies,
credentials, account/model/quota identifiers, and secret-like environment
values are never useful state authority, so the persisted representation keeps
only a stable marker.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


REDACTED = "[REDACTED]"

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "token",
    "password",
    "passwd",
    "secret",
    "apikey",
    "account",
    "model",
    "quota",
    "prompt",
    "command",
)
_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*"
    r"(?:bearer|basic)?\s*([^\s,;]+)"
)
_COOKIE_RE = re.compile(r"(?i)\b(cookie|set-cookie)\s*[:=]\s*([^\r\n]+)")
_QUERY_RE = re.compile(
    r"(?i)([?&](?:access[_-]?token|refresh[_-]?token|token|password|passwd|"
    r"secret|client[_-]?secret|api[_-]?key)=)([^&#\s]+)"
)
_ASSIGN_RE = re.compile(
    r"(?i)\b((?:access[_-]?token|refresh[_-]?token|token|password|passwd|"
    r"secret|client[_-]?secret|api[_-]?key)\s*[:=]\s*)([^\s,;&]+)"
)


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def is_sensitive_key(value: object) -> bool:
    normalized = _normalized_key(value)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact_text(value: str) -> str:
    """Redact credential-shaped substrings while retaining safe context."""

    if not isinstance(value, str):
        raise TypeError("redact_text requires a string")
    result = _HEADER_RE.sub(lambda match: f"{match.group(1)}: {REDACTED}", value)
    result = _COOKIE_RE.sub(lambda match: f"{match.group(1)}: {REDACTED}", result)
    result = _QUERY_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", result)
    result = _ASSIGN_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", result)
    return result


def redact_value(value: Any, *, _key: object | None = None) -> Any:
    """Return a JSON-compatible recursively redacted value.

    ``None`` and booleans are preserved. Other values under sensitive keys are
    fully redacted, including integers; e.g. ``supports.models: true`` remains
    a boolean while a numeric account or quota identifier does not persist.
    """

    if value is None:
        return value
    if _key is not None and is_sensitive_key(_key):
        if isinstance(value, bool):
            return value
        return REDACTED
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(key): redact_value(item, _key=key)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return redact_text(str(value))


__all__ = ["REDACTED", "is_sensitive_key", "redact_text", "redact_value"]
