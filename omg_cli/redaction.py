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


# Key-bracket table codes (O(n) single forward pass; never re-scan suffixes).
_KB_NOT_KEY = -1  # matched ``]`` but not an assignment key-bracket
_KB_EXHAUSTED = -2  # hit EOL/newline while unclosed

# Lexical ends for URL/query tokens (closing quotes, JSON, shell, groups).
_QUERY_TOKEN_END = frozenset("\"',)]}`")


def _compute_key_brackets(text: str) -> list[int]:
    """O(n) table: for each ``[``, store close idx / ``_KB_NOT_KEY`` / ``_KB_EXHAUSTED``.

    A single quote-aware bracket stack resolves every open exactly once, so
    balanced nested non-key brackets stay linear (no repeated suffix probes).
    """

    length = len(text)
    table = [0] * length
    stack: list[int] = []
    quote: str | None = None
    i = 0
    while i < length:
        ch = text[i]
        if quote is not None:
            if quote in ("\\'", '\\"'):
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
                for open_idx in stack:
                    table[open_idx] = _KB_EXHAUSTED
                stack.clear()
                quote = None
                i += 1
                continue
            i += 1
            continue
        if ch in "\r\n":
            for open_idx in stack:
                table[open_idx] = _KB_EXHAUSTED
            stack.clear()
            i += 1
            continue
        if ch == "\\" and i + 1 < length and text[i + 1] in "'\"":
            quote = "\\" + text[i + 1]
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "[":
            stack.append(i)
            i += 1
            continue
        if ch == "]":
            if stack:
                open_idx = stack.pop()
                j = i + 1
                while j < length and text[j] in " \t":
                    j += 1
                if j < length and text[j] in ":=":
                    table[open_idx] = i
                else:
                    table[open_idx] = _KB_NOT_KEY
            i += 1
            continue
        i += 1
    for open_idx in stack:
        table[open_idx] = _KB_EXHAUSTED
    return table


def _quoted_key_close(text: str, open_idx: int) -> int:
    """Return closing quote index when ``text[open_idx]`` opens a quoted object key.

    A quoted key is ``"…"`` / ``'…'`` (backslash escapes OK) followed by
    optional whitespace then ``:`` / ``=``. Returns -1 otherwise. Stops at
    newline so free-text quotes do not span lines.
    """

    if open_idx >= len(text) or text[open_idx] not in "'\"":
        return -1
    quote = text[open_idx]
    length = len(text)
    i = open_idx + 1
    while i < length:
        ch = text[i]
        if ch in "\r\n":
            return -1
        if ch == "\\" and i + 1 < length:
            i += 2
            continue
        if ch == quote:
            j = i + 1
            while j < length and text[j] in " \t":
                j += 1
            if j < length and text[j] in ":=":
                return i
            return -1
        i += 1
    return -1


def _redact_query_assignments(text: str) -> str:
    """Redact ``?key=`` / ``&key=`` assignments in a single O(n) forward pass.

    ``?`` opens query context only in real URL/query shapes; ``&`` continues
    it. Bare shell ``&`` / prose ``?`` outside that context are ignored here
    (plain assignment redaction owns those). Query mode is confined to a single
    continuous URL/query token — whitespace / ``;`` / ``|`` / closing quotes /
    JSON ``,`` / grouping closers / shell ``&&`` clear ``in_query`` so a later
    assignment cannot inherit query continuation. Whitespace between a marker
    and the key rejects the param (``? token=`` is not a query assignment).
    Each character is visited a constant number of times — no per-marker
    suffix ``find("=")``.
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
            # Shell ``&&`` ends the URL/query token — not a query continuation.
            if i + 1 < length and text[i + 1] == "&":
                in_query = False
                url.on_other(text, i, ch)
                i += 1
                continue
            start_param = True
        if not start_param:
            if ch in " \t":
                # Token boundary: query state must not survive into later shell.
                in_query = False
                url.on_whitespace()
            elif ch in ";|" or ch in _QUERY_TOKEN_END or ch == ",":
                in_query = False
                url.on_other(text, i, ch)
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
    applies for real bracket-**key** segments (``headers["api&key"]=``) via an
    O(n) bracket table, and for quoted object keys (``"api?key":``) so
    ``?``/``&``/``://`` inside the quotes stay key material. Bracket interiors
    are still scanned for nested assignments (not opaque). Array values /
    malformed bracket quotes fall back to ordinary scanning. Non-sensitive
    ``:``/``=`` inside a contiguous (no-whitespace) token do **not** advance
    the boundary. ``?`` opens query context only for URL/query shapes;
    whitespace / ``;`` / ``|`` / closing quotes / JSON ``,`` / grouping /
    shell ``&&`` clear it so later assignments cannot inherit query mode.
    """

    parts: list[str] = []
    pos = 0
    boundary = -1
    query_active = False
    url = _UrlQueryTracker()
    brackets = _compute_key_brackets(text)
    # When a key-bracket interior was rewritten, outer ``headers[…]=`` must still
    # see one coherent (redacted) key span at the following separator.
    pending_key: str | None = None
    pending_key_start = -1
    pending_key_end = -1
    i = 0
    length = len(text)

    def _clear_pending() -> None:
        nonlocal pending_key, pending_key_start, pending_key_end
        pending_key = None
        pending_key_start = -1
        pending_key_end = -1

    while i < length:
        ch = text[i]
        if ch == "[":
            close = brackets[i]
            if close >= 0:
                # Key-bracket: redact interior, keep outer ``[…]=`` as one key.
                interior = text[i + 1 : close]
                red_int = (
                    _redact_plain_assignments(interior) if interior else interior
                )
                token_start = boundary + 1
                if token_start < pos:
                    token_start = pos
                while token_start < i and text[token_start] in " \t":
                    token_start += 1
                if red_int != interior:
                    pending_key = text[token_start : i + 1] + red_int + "]"
                    pending_key_start = token_start
                    pending_key_end = close + 1
                else:
                    _clear_pending()
                url.line_has_content = True
                query_active = False
                i = close + 1
                continue
            # Array / non-key / unclosed: ordinary character (table is O(1)).
            _clear_pending()
            query_active = False
            url.on_other(text, i, ch)
            i += 1
            continue
        if ch in "\r\n":
            _clear_pending()
            boundary = i
            query_active = False
            url.on_newline()
            i += 1
            continue
        if ch in "'\"":
            qclose = _quoted_key_close(text, i)
            if qclose >= 0:
                # Quoted object key: skip to closer; ?/&/:// inside are literal.
                url.line_has_content = True
                i = qclose + 1
                continue
            # Non-key quote still ends URL/query token inheritance.
            _clear_pending()
            boundary = i
            query_active = False
            url.on_other(text, i, ch)
            i += 1
            continue
        if ch == "?":
            _clear_pending()
            boundary = i
            if url.is_url_query():
                query_active = True
            url.after_query_marker()
            i += 1
            continue
        if ch == "&":
            _clear_pending()
            boundary = i
            # Shell ``&&`` ends URL/query inheritance.
            if i + 1 < length and text[i + 1] == "&":
                query_active = False
            url.on_other(text, i, ch)
            i += 1
            continue
        if ch in " \t":
            # Token boundary ends URL-query inheritance into later shell tokens.
            # Keep pending bracket-key across ``headers[…] = value`` whitespace.
            if pending_key is None:
                query_active = False
                url.on_whitespace()
                i += 1
                continue
            url.on_whitespace()
            i += 1
            continue
        if ch in ";|":
            # Clear query inheritance but do NOT hard-bound keys — ``api;key=``
            # and ``to;ken://`` must still reach ``is_sensitive_key``.
            query_active = False
            url.on_other(text, i, ch)
            i += 1
            continue
        if ch in ",{}":
            # JSON structural punctuation: end query + hard-bound keys.
            _clear_pending()
            boundary = i
            query_active = False
            url.on_other(text, i, ch)
            i += 1
            continue
        if ch in ")]}`":
            # Grouping closers end query inheritance but must NOT hard-bound keys
            # (malformed ``headers["api_key]=secret`` still redacts).
            query_active = False
            url.on_other(text, i, ch)
            i += 1
            continue
        if ch not in ":=":
            if pending_key is not None and i >= pending_key_end:
                # Non-assignment after a rewritten bracket-key: flush span.
                _clear_pending()
            url.on_other(text, i, ch)
            i += 1
            continue

        sep_idx = i
        if (
            pending_key is not None
            and pending_key_end >= 0
            and sep_idx >= pending_key_end
        ):
            key = pending_key
            key_start = pending_key_start
            key_end = pending_key_end
            _clear_pending()
        else:
            _clear_pending()
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
        if not key:
            # Adjacent separators (``:=``, ``==``) are hard boundaries so
            # marker floods cannot re-accumulate unbounded key spans.
            boundary = sep_idx
            url.on_other(text, i, ch)
            i = sep_idx + 1
            continue
        # Overlong spans: fail-closed redact when sensitive; otherwise advance
        # the boundary so ``:=`` floods stay O(n) (no unbounded re-normalize).
        if len(key) > _MAX_KEY_LEN:
            if not is_sensitive_key(key):
                boundary = sep_idx
                url.on_other(text, i, ch)
                i = sep_idx + 1
                continue
        elif not is_sensitive_key(key):
            # Keep boundary so contiguous fragments (``api:key``, ``api=key``)
            # accumulate into one candidate for the next separator. Whitespace
            # already ends the token via the space handler above.
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
