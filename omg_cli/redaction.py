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

    Quoted values consume through a matching closer only (commas/semicolons
    inside quotes are content). Unclosed quotes eat to EOL/EOF (query also
    stops at ``&``/``#``). Authorization/cookie keys use an EOL consumer so
    multi-token schemes cannot leak. Delimiter-leading unquoted values still
    redact through the following token rather than fail-open.
    """

    if start >= len(text):
        return None
    if _is_authorization_key(key) or _is_cookie_key(key):
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
            if query and ch in "&#":
                return i
            i += 1
        return len(text)

    # Delimiter-leading: password=;hunter2 / command=;rm -rf / — fail-closed
    # through EOL so a parser delimiter cannot hide the remainder.
    if not query and text[start] in ",;":
        return _line_end(text, start)

    if query:
        match = re.match(r"[^&#\s]+", text[start:])
    else:
        match = re.match(r"[^\s&]+", text[start:])
    if match is None:
        # Sensitive key with no parseable value: still redact through EOL.
        return _line_end(text, start)
    return start + match.end()

def _key_start_before(text: str, key_end: int, floor: int) -> int:
    """Walk backward from ``key_end`` to the start of a key candidate."""

    start = key_end
    limit = max(floor, key_end - _MAX_KEY_LEN)
    while start > limit:
        ch = text[start - 1]
        if ch in "\r\n&?;,=":
            break
        if ch in " \t":
            # Allow interior spaces ("api key") but not whitespace before the key.
            if start - 1 <= limit:
                break
            prev = text[start - 2]
            if prev in "\r\n&?;,=":
                break
            start -= 1
            continue
        start -= 1
    return start


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
        if eq < 0 or eq - key_start > _MAX_KEY_LEN:
            i = key_start
            continue
        # Query keys stop at structural chars; take literal slice to ``=``.
        key = text[key_start:eq]
        if not key or any(ch in key for ch in "\r\n&?#"):
            i = key_start
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
    """Scan ``key[:=]value`` with keys validated only by ``is_sensitive_key``.

    Avoids a narrower key regex that can diverge from normalization (e.g.
    ``api/key``, spaced brackets). Non-sensitive keys are not consumed, so
    ``detail=token=secret`` still exposes the inner sensitive assignment.
    """

    parts: list[str] = []
    pos = 0
    i = 0
    length = len(text)
    while i < length:
        if text[i] not in ":=":
            i += 1
            continue
        sep_idx = i
        # Skip ``://`` URL schemes.
        if text[i] == ":" and i + 2 < length and text[i + 1 : i + 3] == "//":
            i += 1
            continue
        key_end = sep_idx
        while key_end > pos and text[key_end - 1] in " \t":
            key_end -= 1
        key_start = _key_start_before(text, key_end, pos)
        key = text[key_start:key_end]
        if not key or not is_sensitive_key(key):
            i = sep_idx + 1
            continue
        # Separator may include surrounding whitespace: ``key = value``.
        sep_end = sep_idx + 1
        while sep_end < length and text[sep_end] in " \t":
            sep_end += 1
        sep = text[key_end:sep_end]
        end = _consume_value(text, sep_end, query=False, key=key)
        if end is None:
            i = sep_idx + 1
            continue
        # Prefer a word-ish boundary before the key when present.
        prefix = ""
        emit_from = key_start
        if key_start > pos and text[key_start - 1].isalnum():
            # Mid-token match (e.g. glued text); still redact — confidentiality
            # wins — but keep the glued prefix outside the replacement.
            pass
        parts.append(text[pos:emit_from])
        parts.append(f"{prefix}{key}{sep}{REDACTED}")
        pos = end
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
