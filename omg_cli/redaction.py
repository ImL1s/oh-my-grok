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


class _UrlQueryTracker:
    """O(1)-per-char forward tracker: real URL/query ``?`` vs prose/shell.

    Accepts ``http(s)://…?``, relative paths whose token contains ``/``, or
    clear query context (``?`` at BOL / after only leading whitespace).
    Rejects ``maybe?`` / ``safe ?`` / ``q?token`` without URL shape.
    """

    __slots__ = ("line_has_content", "token_has_slash", "seen_scheme")

    def __init__(self) -> None:
        self.line_has_content = False
        self.token_has_slash = False
        self.seen_scheme = False

    def on_newline(self) -> None:
        self.line_has_content = False
        self.token_has_slash = False
        self.seen_scheme = False

    def on_whitespace(self) -> None:
        self.token_has_slash = False
        self.seen_scheme = False

    def on_other(self, text: str, i: int, ch: str) -> None:
        self.line_has_content = True
        if ch == "/":
            self.token_has_slash = True
            if i >= 2 and text[i - 2] == ":" and text[i - 1] == "/":
                self.seen_scheme = True

    def is_url_query(self) -> bool:
        return (not self.line_has_content) or self.seen_scheme or self.token_has_slash

    def after_query_marker(self) -> None:
        self.line_has_content = True
        self.token_has_slash = False
        self.seen_scheme = False


def _consume_value(text: str, start: int, *, query: bool, key: str) -> int | None:
    """Return end index of a value starting at ``start``, or None if empty.

    Plain (non-query) sensitive assignments always eat through EOL so matching
    quotes, ampersands, and multi-token tails cannot leak. Query values
    fail-closed to the next ``&``/``#``/EOL (quotes and whitespace do not end
    the value early). Authorization/cookie keys use an EOL consumer in both
    modes.
    """

    if start >= len(text):
        return None
    if not query or _is_authorization_key(key) or _is_cookie_key(key):
        return _line_end(text, start)

    for idx in range(start, len(text)):
        if text[idx] in "&#\r\n":
            return idx
    return len(text)


def _redact_query_assignments(text: str) -> str:
    """Redact ``?key=`` / ``&key=`` assignments in a single O(n) forward pass.

    ``?`` opens query context only in real URL/query shapes; ``&`` continues
    it. Bare shell ``&`` / prose ``?`` outside that context are ignored here
    (plain assignment redaction owns those). Whitespace between a marker and
    the key rejects the param (``? token=`` is not a query assignment). Each
    character is visited a constant number of times — no per-marker suffix
    ``find("=")``.
    """

    parts: list[str] = []
    pos = 0
    i = 0
    in_query = False
    url = _UrlQueryTracker()
    length = len(text)
    while i < length:
        ch = text[i]
        if ch in "\r\n":
            in_query = False
            url.on_newline()
            i += 1
            continue
        start_param = False
        if ch == "?":
            if url.is_url_query():
                in_query = True
                start_param = True
            url.after_query_marker()
        elif ch == "&" and in_query:
            start_param = True
        if not start_param:
            if ch in " \t":
                url.on_whitespace()
            elif ch != "?":
                url.on_other(text, i, ch)
            i += 1
            continue

        prefix = ch
        key_start = i + 1
        # Prose ``? token=`` / ``& key=``: whitespace before key is not a param.
        if key_start < length and text[key_start] in " \t":
            i = key_start
            continue
        j = key_start
        while j < length and text[j] not in "=\r\n&?#":
            j += 1
        if j >= length or text[j] != "=":
            # No assignment at this marker; advance one char (already scanned).
            i = key_start
            continue
        eq = j
        key = text[key_start:eq]
        if not key:
            i = eq + 1
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
    each separator (avoids O(n·cap) separator-flood cost). Quote awareness
    applies **only** inside bracket-key segments ``[...]`` so
    ``headers["api&key"]`` keeps delimiters literal, while free-text
    ``"token=secret"`` still redacts. Unclosed bracket quotes fail-closed
    (EOL resets quote state; scanning continues). ``?`` opens query context
    only for URL/query shapes; bare shell ``&`` does not. Keys may include
    characters stripped by ``_normalized_key`` (``;``, ``/``, brackets, …).
    """

    parts: list[str] = []
    pos = 0
    boundary = -1
    query_active = False
    bracket_depth = 0
    url = _UrlQueryTracker()
    # Quote delimiter only while bracket_depth > 0: '"', "'", '\\"', "\\'".
    quote: str | None = None
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if quote is not None:
            if quote in ("\\'", '\\"'):
                # Close only on matching backslash-quote; other chars are literal.
                if (
                    ch == "\\"
                    and i + 1 < length
                    and text[i + 1] == quote[1]
                ):
                    quote = None
                    i += 2
                    continue
            else:
                if ch == "\\" and i + 1 < length:
                    i += 2
                    continue
                if ch == quote:
                    quote = None
                    i += 1
                    continue
            if ch in "\r\n":
                # Fail-closed: unclosed bracket quote does not freeze the line.
                quote = None
                bracket_depth = 0
                boundary = i
                query_active = False
                url.on_newline()
                i += 1
                continue
            # Inside bracket quotes: &/?/:/= are literal key material.
            url.on_other(text, i, ch)
            i += 1
            continue
        if ch == "[":
            bracket_depth += 1
            url.on_other(text, i, ch)
            i += 1
            continue
        if ch == "]" and bracket_depth > 0:
            bracket_depth -= 1
            url.on_other(text, i, ch)
            i += 1
            continue
        # Quote awareness only inside ``[...]`` bracket-key segments.
        if bracket_depth > 0:
            if ch == "\\" and i + 1 < length and text[i + 1] in "'\"":
                quote = "\\" + text[i + 1]
                url.on_other(text, i, ch)
                i += 2
                continue
            if ch in "'\"":
                quote = ch
                url.on_other(text, i, ch)
                i += 1
                continue
        if ch in "\r\n":
            boundary = i
            query_active = False
            bracket_depth = 0
            url.on_newline()
            i += 1
            continue
        if ch == "?":
            boundary = i
            if url.is_url_query():
                query_active = True
            url.after_query_marker()
            i += 1
            continue
        if ch == "&":
            # Always a key boundary; only continues query mode if URL ``?`` opened it.
            boundary = i
            url.on_other(text, i, ch)
            i += 1
            continue
        if ch in " \t":
            url.on_whitespace()
            i += 1
            continue
        if ch not in ":=":
            url.on_other(text, i, ch)
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
            url.on_other(text, i, ch)
            i += 1
            continue
        if not key or not is_sensitive_key(key):
            # Non-matching separators become boundaries so the next key cannot
            # glue across them (``detail=token=secret`` → inner ``token``).
            boundary = sep_idx
            url.on_other(text, i, ch)
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
        # Query-string values (URL ``?``, then ``&`` continuations) stop at the
        # next ``&``. Bare shell ``&`` / prose ``?`` never open query mode, so
        # plain ``token="…" secret`` still eats through EOL.
        in_query = query_active and boundary >= 0 and text[boundary] in "?&"
        end = _consume_value(text, sep_end, query=in_query, key=key)
        if end is None:
            boundary = sep_idx
            url.on_other(text, i, ch)
            i = sep_idx + 1
            continue
        parts.append(text[pos:key_start])
        parts.append(f"{key}{sep}{REDACTED}")
        pos = end
        boundary = end - 1 if end > 0 else -1
        url.line_has_content = True
        url.token_has_slash = False
        url.seen_scheme = False
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
