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
from urllib.parse import unquote_plus


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

# Key tokenizer: letter/underscore/%-escape, or digits glued to a letter (2fa_*);
# never a bare digit (avoids matching value fragments like ``0 quota=``). Brackets,
# %-escapes, and a single space between word chars ("api key") are allowed.
_KEY = (
    r"(?:[A-Za-z_]|%[0-9A-Fa-f]{2}|[0-9]+[A-Za-z_])"
    r"(?:[A-Za-z0-9_.\-\[\]]|%[0-9A-Fa-f]{2}| (?=[A-Za-z_%\[\]])){0,127}"
)
_QUOTED_VAL = (
    r"\"(?:\\.|[^\"\\])*\"|"
    r"'(?:\\.|[^'\\])*'"
)
# Unquoted values omit ``=`` so a non-sensitive outer assign cannot swallow an
# inner ``token=secret`` before the sensitive key is considered.
_UNQUOTED_ASSIGN_VAL = r"[^\s,;&=]+"
_UNQUOTED_QUERY_VAL = r"[^&#\s=]+"

_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*"
    r"(?:bearer|basic)?\s*"
    rf"({_QUOTED_VAL}|[^\s,;]+)"
)
_COOKIE_RE = re.compile(r"(?i)\b(cookie|set-cookie)\s*[:=]\s*([^\r\n]+)")
_ASSIGN_KEY_RE = re.compile(
    rf"(?i)(?P<prefix>\b|(?<=[?&]))(?P<key>{_KEY})(?P<sep>\s*[:=]\s*)"
)
_QUERY_KEY_RE = re.compile(rf"(?i)(?P<prefix>[?&])(?P<key>{_KEY})(?P<sep>=)")
_ASSIGN_VAL_RE = re.compile(rf"(?:{_QUOTED_VAL}|{_UNQUOTED_ASSIGN_VAL})")
_QUERY_VAL_RE = re.compile(rf"(?:{_QUOTED_VAL}|{_UNQUOTED_QUERY_VAL})")


def _normalized_key(value: object) -> str:
    text = unquote_plus(str(value))
    return re.sub(r"[^a-z0-9]", "", text.lower())


def is_sensitive_key(value: object) -> bool:
    normalized = _normalized_key(value)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_keyed_assignments(
    text: str,
    *,
    key_re: re.Pattern[str],
    val_re: re.Pattern[str],
) -> str:
    """Redact sensitive key=value forms without consuming non-sensitive keys.

    A plain ``re.sub`` over all candidates would match ``detail=token`` first
    (non-sensitive) and hide the trailing ``=secret`` from a later
    ``token=secret`` match.
    """

    parts: list[str] = []
    pos = 0
    for key_match in key_re.finditer(text):
        if key_match.start() < pos:
            continue
        if not is_sensitive_key(key_match.group("key")):
            continue
        val_match = val_re.match(text, key_match.end())
        if val_match is None:
            continue
        parts.append(text[pos : key_match.start()])
        parts.append(
            f"{key_match.group('prefix')}{key_match.group('key')}"
            f"{key_match.group('sep')}{REDACTED}"
        )
        pos = val_match.end()
    parts.append(text[pos:])
    return "".join(parts)


def redact_text(value: str) -> str:
    """Redact credential-shaped substrings while retaining safe context.

    Free-text assign/query forms use the same ``is_sensitive_key`` predicate as
    structured mapping keys (compound / encoded names included). Quoted values
    are redacted as a whole.
    """

    if not isinstance(value, str):
        raise TypeError("redact_text requires a string")
    result = _HEADER_RE.sub(lambda match: f"{match.group(1)}: {REDACTED}", value)
    result = _COOKIE_RE.sub(lambda match: f"{match.group(1)}: {REDACTED}", result)
    result = _redact_keyed_assignments(
        result, key_re=_QUERY_KEY_RE, val_re=_QUERY_VAL_RE
    )
    result = _redact_keyed_assignments(
        result, key_re=_ASSIGN_KEY_RE, val_re=_ASSIGN_VAL_RE
    )
    return result


def _redact_mapping_key(key: object) -> str:
    """Apply free-text redaction to mapping keys (keeps plain sensitive names)."""
    return redact_text(str(key))


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
            _redact_mapping_key(key): redact_value(item, _key=key)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return redact_text(str(value))


__all__ = ["REDACTED", "is_sensitive_key", "redact_text", "redact_value"]
