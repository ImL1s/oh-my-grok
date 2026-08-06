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
_MAX_KEY_LEN = 256

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
    r"(?im)\b(authorization|proxy-authorization)\s*[:=][^\r\n]*"
)
_COOKIE_RE = re.compile(r"(?i)\b(cookie|set-cookie)\s*[:=]\s*([^\r\n]+)")


def _normalized_key(value: object) -> str:
    text = unquote_plus(str(value))
    return re.sub(r"[^a-z0-9]", "", text.lower())


def is_sensitive_key(value: object) -> bool:
    normalized = _normalized_key(value)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _is_authorization_key(key: str) -> bool:
    return "authorization" in _normalized_key(key)


def _is_cookie_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return "cookie" in normalized or "setcookie" in normalized


def _line_end(text: str, start: int) -> int:
    for idx in range(start, len(text)):
        if text[idx] in "\r\n":
            return idx
    return len(text)


def _consume_value(text: str, start: int, *, query: bool, key: str) -> int | None:
    """Return end index of a value starting at ``start``, or None if empty.

    Plain (non-query) sensitive assignments always eat through EOL so matching
    quotes, ampersands, and multi-token tails cannot leak. Query values still
    stop at ``&``/``#``; unclosed quotes eat to that boundary or EOL/EOF.
    Authorization/cookie keys use an EOL consumer in both modes.
    """

    if start >= len(text):
        return None
    if not query or _is_authorization_key(key) or _is_cookie_key(key):
        return _line_end(text, start)

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
            if ch in "\r\n":
                return i
            if ch in "&#":
                return i
            i += 1
        return len(text)

    match = re.match(r"[^&#\s]+", text[start:])
    if match is None:
        return _line_end(text, start)
    return start + match.end()


def _redact_query_assignments(text: str) -> str:
    parts: list[str] = []
    pos = 0
    i = 0
    while i < len(text):
        if text[i] not in "?&":
            i += 1
            continue
        prefix = text[i]
        key_start = i + 1
        eq = text.find("=", key_start)
        if eq < 0:
            i = key_start
            continue
        # Query keys stop at structural chars; take literal slice to ``=``.
        key = text[key_start:eq]
        if not key or any(ch in key for ch in "\r\n&?#"):
            i = key_start
            continue
        # Overlong non-sensitive keys are skipped; overlong sensitive keys
        # still redact (fail-closed — never drop a sensitive assignment).
        if len(key) > _MAX_KEY_LEN and not is_sensitive_key(key):
            i = eq + 1
            continue
        if key_start < pos or not is_sensitive_key(key):
            i = eq + 1
            continue
        end = _consume_value(text, eq + 1, query=True, key=key)
        if end is None:
            i = eq + 1
            continue
        parts.append(text[pos:i])
        parts.append(f"{prefix}{key}={REDACTED}")
        pos = end
        i = end
    parts.append(text[pos:])
    return "".join(parts)


def _redact_plain_assignments(text: str) -> str:
    """Scan ``key[:=]value`` in a single O(n) forward pass.

    Tracks the most recent structural boundary instead of walking backward from
    each separator (avoids O(n·cap) separator-flood cost). Keys may include
    characters stripped by ``_normalized_key`` (``;``, ``/``, brackets, …) so
    the free-text scanner and ``is_sensitive_key`` stay aligned.
    """

    parts: list[str] = []
    pos = 0
    boundary = -1
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch in "\r\n&?":
            boundary = i
            i += 1
            continue
        if ch not in ":=":
            i += 1
            continue

        sep_idx = i
        region_start = boundary + 1
        if region_start < pos:
            region_start = pos
        key_end = sep_idx
        while key_end > region_start and text[key_end - 1] in " \t":
            key_end -= 1
        key_start = region_start
        while key_start < key_end and text[key_start] in " \t":
            key_start += 1
        key = text[key_start:key_end]

        # Skip non-sensitive ``scheme://`` URL forms only after the key check.
        if (
            ch == ":"
            and i + 2 < length
            and text[i + 1 : i + 3] == "//"
            and (not key or not is_sensitive_key(key))
        ):
            boundary = sep_idx
            i += 1
            continue
        if not key or not is_sensitive_key(key):
            # Non-matching separators become boundaries so the next key cannot
            # glue across them (``detail=token=secret`` → inner ``token``).
            boundary = sep_idx
            i = sep_idx + 1
            continue

        # Separator may include surrounding whitespace: ``key = value``.
        # Also accept ``key://value`` for sensitive keys.
        sep_end = sep_idx + 1
        if (
            text[sep_idx] == ":"
            and sep_idx + 2 < length
            and text[sep_idx + 1 : sep_idx + 3] == "//"
        ):
            sep_end = sep_idx + 3
        while sep_end < length and text[sep_end] in " \t":
            sep_end += 1
        sep = text[key_end:sep_end]
        # Assignments that sit in a query string (after ``?``/``&``) must stop
        # at the next ``&`` so ``?prompt=…&ok=1`` keeps the safe tail. Plain
        # shell-style ``command=… & curl secret`` still eats through EOL.
        in_query = boundary >= 0 and text[boundary] in "?&"
        end = _consume_value(text, sep_end, query=in_query, key=key)
        if end is None:
            boundary = sep_idx
            i = sep_idx + 1
            continue
        parts.append(text[pos:key_start])
        parts.append(f"{key}{sep}{REDACTED}")
        pos = end
        boundary = end - 1 if end > 0 else -1
        i = end
    parts.append(text[pos:])
    return "".join(parts)


def redact_text(value: str) -> str:
    """Redact credential-shaped substrings while retaining safe context.

    Free-text assign/query forms use the same ``is_sensitive_key`` predicate as
    structured mapping keys. Quoted values redact as a whole (including commas
    inside quotes and truncated closers).
    """

    if not isinstance(value, str):
        raise TypeError("redact_text requires a string")
    result = _HEADER_RE.sub(lambda match: f"{match.group(1)}: {REDACTED}", value)
    result = _COOKIE_RE.sub(lambda match: f"{match.group(1)}: {REDACTED}", result)
    result = _redact_query_assignments(result)
    result = _redact_plain_assignments(result)
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
