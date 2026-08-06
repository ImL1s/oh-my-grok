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

# Key tokenizer aligned with ``is_sensitive_key`` / ``unquote_plus``: ``+`` (form
# space), brackets, quoted bracket segments, %-escapes, and spaced names.
_KEY = (
    r"(?:[A-Za-z_]|%[0-9A-Fa-f]{2}|[0-9]+[A-Za-z_])"
    r"(?:"
    r"[A-Za-z0-9_.\-\[\]+]|"
    r"%[0-9A-Fa-f]{2}|"
    r" (?=[A-Za-z_%\[+])|"
    r"\"[^\"]{1,64}\"|"
    r"'[^']{1,64}'"
    r"){0,127}"
)

_HEADER_RE = re.compile(
    r"(?im)\b(authorization|proxy-authorization)\s*[:=][^\r\n]*"
)
_COOKIE_RE = re.compile(r"(?i)\b(cookie|set-cookie)\s*[:=]\s*([^\r\n]+)")
_ASSIGN_KEY_RE = re.compile(
    rf"(?i)(?P<prefix>\b|(?<=[?&]))(?P<key>{_KEY})(?P<sep>\s*[:=]\s*)"
)
_QUERY_KEY_RE = re.compile(rf"(?i)(?P<prefix>[?&])(?P<key>{_KEY})(?P<sep>=)")


def _normalized_key(value: object) -> str:
    text = unquote_plus(str(value))
    return re.sub(r"[^a-z0-9]", "", text.lower())


def is_sensitive_key(value: object) -> bool:
    normalized = _normalized_key(value)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _consume_value(text: str, start: int, *, query: bool) -> int | None:
    """Return end index of a value starting at ``start``, or None if empty.

    Quoted values consume through a matching closer. Unclosed quotes are eaten
    conservatively to the structural end of the field (or EOF). Unquoted values
    may contain ``=``; non-sensitive outer keys are skipped by the caller, so
    inner ``token=secret`` remains visible to a later match.
    """

    if start >= len(text):
        return None
    quote = text[start]
    if quote in "'\"":
        i = start + 1
        while i < len(text):
            ch = text[i]
            if ch == "\\" and i + 1 < len(text):
                i += 2
                continue
            if ch == quote:
                return i + 1
            if query and ch in "&#":
                return i
            if not query and ch in ",;\r\n":
                return i
            i += 1
        return len(text)
    if query:
        match = re.match(r"[^&#\s]+", text[start:])
    else:
        match = re.match(r"[^\s,;&]+", text[start:])
    if match is None:
        return None
    return start + match.end()


def _redact_keyed_assignments(
    text: str,
    *,
    key_re: re.Pattern[str],
    query: bool,
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
        end = _consume_value(text, key_match.end(), query=query)
        if end is None:
            continue
        parts.append(text[pos : key_match.start()])
        parts.append(
            f"{key_match.group('prefix')}{key_match.group('key')}"
            f"{key_match.group('sep')}{REDACTED}"
        )
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def redact_text(value: str) -> str:
    """Redact credential-shaped substrings while retaining safe context.

    Free-text assign/query forms use the same ``is_sensitive_key`` predicate as
    structured mapping keys (compound / encoded names included). Quoted values
    are redacted as a whole, including truncated/unclosed quotes.
    """

    if not isinstance(value, str):
        raise TypeError("redact_text requires a string")
    result = _HEADER_RE.sub(lambda match: f"{match.group(1)}: {REDACTED}", value)
    result = _COOKIE_RE.sub(lambda match: f"{match.group(1)}: {REDACTED}", result)
    result = _redact_keyed_assignments(result, key_re=_QUERY_KEY_RE, query=True)
    result = _redact_keyed_assignments(result, key_re=_ASSIGN_KEY_RE, query=False)
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
