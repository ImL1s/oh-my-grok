"""Deterministic recursive redaction for all persisted OMG diagnostics.

The redactor is deliberately conservative.  Raw prompts, command bodies,
credentials, account/model/quota identifiers, and secret-like environment
values are never useful state authority, so the persisted representation keeps
only a stable marker.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
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


def _line_end(text: str, start: int, limit: int | None = None) -> int:
    end = len(text) if limit is None else min(limit, len(text))
    if end < start:
        return start
    for idx in range(start, end):
        if text[idx] in "\r\n":
            return idx
    return end


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

    def copy(self) -> _UrlQueryTracker:
        other = _UrlQueryTracker()
        other.line_has_content = self.line_has_content
        other.token_has_slash = self.token_has_slash
        other.seen_scheme = self.seen_scheme
        return other


def _consume_value(
    text: str,
    start: int,
    *,
    query: bool,
    key: str,
    limit: int | None = None,
) -> int | None:
    """Return end index of a value starting at ``start``, or None if empty.

    Plain (non-query) sensitive assignments always eat through EOL so matching
    quotes, ampersands, and multi-token tails cannot leak. Query values
    fail-closed to the next ``&``/``#``/EOL (quotes and whitespace do not end
    the value early). Authorization/cookie keys use an EOL consumer in both
    modes.

    ``limit`` caps the scan (key-bracket close) so nested ``]=x`` depth stays
    O(n) instead of each EOL probe walking the full remaining string.

    Callers must run :func:`_query_value_after_consume` on the returned span:
    when the consumer jumps over a structural clearer (``<>`` / quotes / JSON /
    grouping), query mode must be cleared — and when the clearer is not a
    clean quoted-value delimiter, the redact end fail-closed to EOL so a later
    bare ``&second-secret`` tail cannot leak.
    """

    if start >= len(text):
        return None
    scan_end = len(text) if limit is None else min(limit, len(text))
    if start >= scan_end:
        return None
    if not query or _is_authorization_key(key) or _is_cookie_key(key):
        return _line_end(text, start, limit)

    for idx in range(start, scan_end):
        if text[idx] in "&#\r\n":
            return idx
    return scan_end


# Key-bracket table codes (O(n) single forward pass; never re-scan suffixes).
_KB_NOT_KEY = -1  # matched ``]`` but not an assignment key-bracket
_KB_EXHAUSTED = -2  # hit EOL/newline while unclosed

# Lexical ends for URL/query tokens (closing quotes, JSON, shell, groups,
# redirection). Shared by query + plain scanners so ``>`` / ``<`` / ``(``
# cannot leave query mode into a later ``&token=`` tail. ``(`` covers shell
# grouping / command-substitution openers (``$(…)``) symmetrically with ``)``.
_QUERY_TOKEN_END = frozenset("\"',()]}`<>")


def _clears_query_token(ch: str) -> bool:
    """True when ``ch`` ends a URL/query token (must clear ``in_query``).

    Includes whitespace, shell/JSON clearers, grouping openers/closers
    (``(`` ``]``), and URL fragment ``#`` — query mode must not survive any of
    these into a later ``&token=`` / ``&second-secret`` tail.
    """
    return ch in " \t;|,#" or ch in _QUERY_TOKEN_END


# Bash special-parameter / digraph second chars after ``$`` that clear query
# mode (parity with ``$(``). Includes ``{`` / ``[`` expansions and GNU Bash
# special parameters ``$@ $* $? $- $$ $! $0``…``$9``. A bare ``$`` followed by
# a normal identifier letter (``$bar``) must NOT clear.
_DOLLAR_QUERY_CLEAR_SECONDS = frozenset("{[@*?-$!0123456789")


def _clears_query_at(text: str, idx: int) -> bool:
    """True when ``text[idx]`` starts a query-token clearer (incl. digraphs).

    Single-char clearers match :func:`_clears_query_token`. Shell expansions
    ``${…}`` / ``$[…]`` and Bash special parameters (``$@`` ``$$`` ``$*``
    ``$?`` ``$-`` ``$!`` ``$0``…) clear as digraphs (parity with ``$(…`` via
    ``(``) — bare ``$`` alone or ``$`` + normal identifier does **not**, so
    legitimate keys/values with ``$`` are preserved. Opening ``{`` / ``[`` are
    intentionally *not* global clearers here (URLs / key-brackets); only the
    ``$``-prefixed forms qualify.
    """
    if idx < 0 or idx >= len(text):
        return False
    ch = text[idx]
    if _clears_query_token(ch):
        return True
    if ch == "$" and idx + 1 < len(text) and text[idx + 1] in _DOLLAR_QUERY_CLEAR_SECONDS:
        return True
    return False


def _query_value_after_consume(text: str, start: int, end: int) -> str:
    """Classify a consumed query-value span for query-mode cleanup.

    Returns:
      ``""`` — no structural clearer jumped; keep query mode.
      ``"clear"`` — clearer present; clear query mode, keep end at ``&``/``#``.
        Used for clean quoted values (``?prompt="…"&ok=1``) and terminal
        clearers (``foo>``) so a following ``&`` remains a real separator.
      ``"clear_eol"`` — clearer mid-span (``foo>out``, ``foo bar``, ``foo]bar``,
        JSON quote/comma junk, ``${…}`` / ``$[…]`` / ``$@`` / ``$$``…); clear
        query mode and fail-closed to EOL so ``&second-secret`` cannot leak as
        a truncated tail.

    Whitespace / ``]`` are real clearers (``_consume_value`` may jump over them
    before the main loop can see them). Fragment ``#`` is handled by the
    caller via :func:`_clears_query_at` when consume stops at ``#``.
    """

    if start >= end:
        return ""
    # Well-delimited quoted value then real ``&``: ``?prompt="hello world"&ok=1``.
    if (
        end - start >= 2
        and text[start] in "'\""
        and text[end - 1] == text[start]
    ):
        return "clear"

    saw_mid = False
    last_clear = False
    for idx in range(start, end):
        if _clears_query_at(text, idx):
            if idx == end - 1:
                last_clear = True
            else:
                saw_mid = True
    if saw_mid:
        return "clear_eol"
    if last_clear:
        return "clear"
    return ""


def _flatten_parts(parts: list) -> str:
    """Join a possibly nested parts tree in O(total leaf chars).

    Nested key-bracket frames store interior segments by reference so frame-pop
    stays O(1) per level instead of re-materializing O(depth) strings.
    """

    out: list[str] = []
    stack: list = list(reversed(parts))
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item))
        else:
            out.append(item)
    return "".join(out)

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


def _compute_quoted_keys(text: str) -> list[int]:
    """O(n) table: quoted object-key open → close index; else ``-1``.

    Single forward pass — unclosed quotes jump to EOL so escaped-quote floods
    (``\\"`` * n) stay linear instead of re-scanning the suffix per quote.
    """

    length = len(text)
    table = [-1] * length
    i = 0
    while i < length:
        ch = text[i]
        if ch in "\r\n":
            i += 1
            continue
        if ch == "\\" and i + 1 < length:
            i += 2
            continue
        if ch not in "'\"":
            i += 1
            continue
        quote = ch
        open_idx = i
        j = i + 1
        closed = False
        while j < length:
            c = text[j]
            if c in "\r\n":
                break
            if c == "\\" and j + 1 < length:
                j += 2
                continue
            if c == quote:
                k = j + 1
                while k < length and text[k] in " \t":
                    k += 1
                if k < length and text[k] in ":=":
                    table[open_idx] = j
                i = j + 1
                closed = True
                break
            j += 1
        if not closed:
            # Unclosed through EOL: no further quoted keys on this line.
            i = j if j < length and text[j] in "\r\n" else length
    return table


def _redact_query_assignments(text: str) -> str:
    """Redact ``?key=`` / ``&key=`` assignments in a single O(n) forward pass.

    ``?`` opens query context only in real URL/query shapes; ``&`` continues
    it. Bare shell ``&`` / prose ``?`` outside that context are ignored here
    (plain assignment redaction owns those). Query mode is confined to a single
    continuous URL/query token — whitespace / ``;`` / ``|`` / ``#`` / closing
    quotes / JSON ``,`` / grouping openers/closers (incl. ``(`` ``]``; covers
    ``$(…)``) / shell expansions ``${…}`` / ``$[…]`` / Bash special parameters
    (``$@`` ``$$`` ``$*`` ``$?`` ``$-`` ``$!`` ``$0``…) / shell redirection
    (``<`` ``>``) / shell ``&&`` clear ``in_query`` so a later assignment
    cannot inherit query continuation.
    Whitespace between a marker and the key rejects the param (``? token=`` is
    not a query assignment). Each character is visited a constant number of
    times — no per-marker suffix ``find("=")``.
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
            elif _clears_query_at(text, i) and ch not in " \t":
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
        value_start = eq + 1
        end = _consume_value(text, value_start, query=True, key=key)
        if end is None:
            i = eq + 1
            continue
        # Consumer may jump over ``<>`` / quotes / JSON / grouping — clear query
        # mode; mid-span clearers fail-closed to EOL so ``&second-secret`` tails
        # cannot leak (clean ``?prompt="…"&ok=1`` only clears, keeps ``&ok``).
        action = _query_value_after_consume(text, value_start, end)
        if action:
            in_query = False
        if action == "clear_eol":
            end = _line_end(text, value_start)
        parts.append(text[pos:i])
        parts.append(f"{prefix}{key}={REDACTED}")
        pos = end
        i = end
    parts.append(text[pos:])
    return "".join(parts)


@dataclass
class _BracketFrame:
    """Saved outer scanner state while iteratively scanning a key-bracket interior."""

    open_idx: int
    close_idx: int
    token_start: int
    parts: list
    pos: int
    boundary: int
    query_active: bool
    url: _UrlQueryTracker
    pending_key: str | list | None
    pending_key_start: int
    pending_key_end: int
    pending_known_sensitive: bool = False
    had_nested_key_bracket: bool = False


def _redact_quote_interior(text: str) -> str:
    """Redact assignments inside a quoted object-key interior (non-opaque).

    Runs the plain scanner on the interior slice with shell/query/JSON
    punctuation treated as key material (parity with ``is_sensitive_key``
    normalize, which strips ``?`` ``&`` ``#`` quotes ``,`` etc.). Only ``:`` /
    ``=`` open assignments; newlines and key-brackets remain structural.
    Key-brackets inside are handled by the iterative stack in that call; this
    is not the nested key-bracket hot path (``[`` * n), so a slice here is
    acceptable.
    """

    if not text:
        return text
    return _redact_plain_assignments(text, quote_interior=True)


def _redact_plain_assignments(text: str, *, quote_interior: bool = False) -> str:
    """Scan ``key[:=]value`` in a single O(n) forward pass.

    Tracks the most recent structural boundary instead of walking backward from
    each separator (avoids O(n·cap) separator-flood cost). Quote awareness
    applies for real bracket-**key** segments (``headers["api&key"]=``) via an
    O(n) bracket table, and for quoted object keys (``"api?key":``) so
    ``?``/``&``/``://`` inside the quotes stay key material while interiors are
    still scanned for nested assignments. When ``quote_interior`` is set (quoted
    object-key interior re-scan), shell/query/JSON punctuation (``?`` ``&``
    ``#`` quotes ``,`` ``<>`` ``{}`` etc.) stays key material so
    ``to?ken=…`` / ``to\\"ken=…`` still reach ``is_sensitive_key``; only ``:`` /
    ``=`` open assignments. Nested key-brackets use an explicit stack over
    original index ranges (no recursive sliced re-entry); frame-pop keeps
    interior segments by reference so rewrite nests stay O(n). Array values /
    malformed bracket quotes fall back to ordinary scanning. Non-sensitive
    ``:``/``=`` inside a contiguous (no-whitespace) token do **not** advance the
    boundary. ``?`` opens query context only for URL/query shapes; whitespace /
    ``;`` / ``|`` / ``#`` / ``<`` / ``>`` / closing quotes / JSON ``,`` /
    grouping (incl. ``(``) / shell ``&&`` clear it so later assignments cannot
    inherit query mode. Fragment ``#`` clears query without hard-bounding keys
    (``api#key=`` must still reach the predicate).
    """

    parts: list = []
    pos = 0
    boundary = -1
    query_active = False
    url = _UrlQueryTracker()
    brackets = _compute_key_brackets(text)
    quoted_keys = _compute_quoted_keys(text)
    pending_key: str | list | None = None
    pending_key_start = -1
    pending_key_end = -1
    pending_known_sensitive = False
    bracket_stack: list[_BracketFrame] = []
    i = 0
    length = len(text)

    def _clear_pending() -> None:
        nonlocal pending_key, pending_key_start, pending_key_end
        nonlocal pending_known_sensitive
        pending_key = None
        pending_key_start = -1
        pending_key_end = -1
        pending_known_sensitive = False

    def _finish_bracket_frame() -> bool:
        """If ``i`` is the close of the active key-bracket frame, pop it.

        Returns True when a frame was closed (caller should ``continue``).
        """
        nonlocal parts, pos, boundary, query_active, url
        nonlocal pending_key, pending_key_start, pending_key_end
        nonlocal pending_known_sensitive, i
        if not bracket_stack:
            return False
        frame = bracket_stack[-1]
        if i != frame.close_idx:
            return False
        bracket_stack.pop()
        # Interior changed iff an assignment redaction advanced ``pos`` / parts.
        # Avoid materializing O(depth) interior strings for the comparison.
        red_changed = bool(parts) or pos != frame.open_idx + 1
        interior_parts = parts
        interior_pos = pos
        # Restore outer scanner state.
        parts = frame.parts
        pos = frame.pos
        boundary = frame.boundary
        query_active = frame.query_active
        url = frame.url
        pending_key = frame.pending_key
        pending_key_start = frame.pending_key_start
        pending_key_end = frame.pending_key_end
        pending_known_sensitive = frame.pending_known_sensitive
        # Sensitivity of this ``prefix[interior]`` as an outer assignment key.
        # Avoid re-normalizing O(depth)-sized nested ``]=x`` aggregates (O(n²)):
        # when nested key-brackets were scanned and left the interior unchanged,
        # no sensitive nested assignment remains.
        if red_changed:
            key_sensitive = True
        elif frame.had_nested_key_bracket:
            # Nested ``]=x`` aggregates are non-sensitive when unchanged, but a
            # sensitive prefix (``token[[safe]=x]=…``) must still win.
            key_sensitive = (
                frame.token_start < frame.open_idx
                and is_sensitive_key(text[frame.token_start : frame.open_idx])
            )
        else:
            key_sensitive = is_sensitive_key(
                text[frame.token_start : frame.close_idx + 1]
            )
        if key_sensitive:
            if red_changed:
                # Segment tree by reference — O(1) wrap per level, flatten once.
                trailing = text[interior_pos : frame.close_idx]
                seg: list = [
                    text[frame.token_start : frame.open_idx + 1],
                    interior_parts,
                ]
                if trailing:
                    seg.append(trailing)
                seg.append("]")
                pending_key = seg
            else:
                pending_key = text[frame.token_start : frame.close_idx + 1]
            pending_key_start = frame.token_start
            pending_key_end = frame.close_idx + 1
            pending_known_sensitive = True
        else:
            _clear_pending()
            # Do not let nested ``]=x`` layers re-accumulate into one key span.
            boundary = frame.close_idx
        url.line_has_content = True
        query_active = False
        i = frame.close_idx + 1
        return True

    while i < length:
        # Close innermost key-bracket when its ``]`` is reached.
        if _finish_bracket_frame():
            continue

        ch = text[i]
        if ch == "[":
            close = brackets[i]
            if close >= 0:
                # Enter interior iteratively — push outer state, scan in place.
                token_start = boundary + 1
                if token_start < pos:
                    token_start = pos
                while token_start < i and text[token_start] in " \t":
                    token_start += 1
                if bracket_stack:
                    bracket_stack[-1].had_nested_key_bracket = True
                bracket_stack.append(
                    _BracketFrame(
                        open_idx=i,
                        close_idx=close,
                        token_start=token_start,
                        parts=parts,
                        pos=pos,
                        boundary=boundary,
                        query_active=query_active,
                        url=url.copy(),
                        pending_key=pending_key,
                        pending_key_start=pending_key_start,
                        pending_key_end=pending_key_end,
                        pending_known_sensitive=pending_known_sensitive,
                    )
                )
                # Fresh interior scan state over original indices (open+1 .. close).
                parts = []
                pos = i + 1
                boundary = i
                query_active = False
                url = _UrlQueryTracker()
                url.line_has_content = True
                _clear_pending()
                i = i + 1
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
        # Quoted object-key interiors: shell/query/JSON punctuation is literal
        # key material (``is_sensitive_key`` strips it). Only ``:`` / ``=`` open
        # assignments; brackets/newlines already handled above.
        if quote_interior and ch not in ":=":
            if pending_key is not None and i >= pending_key_end:
                _clear_pending()
            url.on_other(text, i, ch)
            i += 1
            continue
        if ch in "'\"":
            qclose = quoted_keys[i]
            if qclose >= 0:
                # Quoted object key: scan interior (non-opaque), keep ?/&/://.
                interior = text[i + 1 : qclose]
                red_int = _redact_quote_interior(interior)
                token_start = boundary + 1
                if token_start < pos:
                    token_start = pos
                while token_start < i and text[token_start] in " \t":
                    token_start += 1
                if red_int != interior:
                    pending_key = (
                        text[token_start:i] + ch + red_int + text[qclose]
                    )
                    pending_key_start = token_start
                    pending_key_end = qclose + 1
                    pending_known_sensitive = False
                else:
                    _clear_pending()
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
            # Keep pending bracket/quoted-key across ``… = value`` whitespace.
            if pending_key is None:
                query_active = False
                url.on_whitespace()
                i += 1
                continue
            url.on_whitespace()
            i += 1
            continue
        if ch in ";|#":
            # Clear query inheritance but do NOT hard-bound keys — ``api;key=``,
            # ``api#key=``, and ``to;ken://`` must still reach ``is_sensitive_key``.
            # Fragment ``#`` still ends URL/query mode (``?api_key=foo#frag&…``).
            query_active = False
            url.on_other(text, i, ch)
            i += 1
            continue
        if ch in "<>":
            # Shell redirection ends URL/query inheritance (no surrounding ws).
            _clear_pending()
            boundary = i
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
        if ch in "()]}`":
            # Grouping openers/closers end query inheritance but must NOT
            # hard-bound keys (malformed ``headers["api_key]=secret`` still
            # redacts; ``$(…)`` must not inherit query into ``&second-secret``).
            # Note: key-bracket ``]`` is handled via ``_finish_bracket_frame``.
            query_active = False
            url.on_other(text, i, ch)
            i += 1
            continue
        if (
            ch == "$"
            and i + 1 < length
            and text[i + 1] in _DOLLAR_QUERY_CLEAR_SECONDS
        ):
            # Shell ``${…}`` / ``$[…]`` / Bash special-parameter digraphs
            # clear query (parity with ``$(``); bare ``$`` / ``$bar`` do not.
            query_active = False
            url.on_other(text, i, ch)
            i += 1
            continue
        if ch not in ":=":
            if pending_key is not None and i >= pending_key_end:
                # Non-assignment after a rewritten key: flush span.
                _clear_pending()
            url.on_other(text, i, ch)
            i += 1
            continue

        sep_idx = i
        known_sensitive = False
        if (
            pending_key is not None
            and pending_key_end >= 0
            and sep_idx >= pending_key_end
        ):
            key: str | list = pending_key
            key_start = pending_key_start
            key_end = pending_key_end
            known_sensitive = pending_known_sensitive
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

        key_sensitive = known_sensitive or (
            isinstance(key, str) and bool(key) and is_sensitive_key(key)
        )
        # Skip non-sensitive ``scheme://`` URL forms only after the key check.
        if (
            ch == ":"
            and i + 2 < length
            and text[i + 1 : i + 3] == "//"
            and not key_sensitive
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
        if not known_sensitive and isinstance(key, str):
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
                # Exception: a completed key-bracket span (ends with ``]``) must not
                # re-accumulate across nested ``]=x`` layers — that is O(n²) on
                # ``"["*n + "safe" + "]=x"*n``.
                if key.endswith("]"):
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
        # Segment-tree keys are known-sensitive bracket rewrites; pass a
        # sensitive stand-in so auth/cookie EOL rules are not required.
        consume_key = key if isinstance(key, str) else "token"
        value_start = sep_end
        bracket_limit = (
            bracket_stack[-1].close_idx if bracket_stack else None
        )
        end = _consume_value(
            text,
            value_start,
            query=in_query,
            key=consume_key,
            limit=bracket_limit,
        )
        if end is None:
            url.on_other(text, i, ch)
            i = sep_idx + 1
            continue
        if in_query:
            action = _query_value_after_consume(text, value_start, end)
            if action:
                query_active = False
            if action == "clear_eol":
                end = _line_end(text, value_start, bracket_limit)
        # Clamp to active key-bracket close so in-place interiors match sliced
        # EOL semantics (never consume past the closing ``]``).
        if bracket_stack:
            end = min(end, bracket_stack[-1].close_idx)
        parts.append(text[pos:key_start])
        if isinstance(key, list):
            parts.append(key)
            parts.append(f"{sep}{REDACTED}")
        else:
            parts.append(f"{key}{sep}{REDACTED}")
        pos = end
        boundary = end - 1 if end > 0 else -1
        url.line_has_content = True
        url.token_has_slash = False
        url.seen_scheme = False
        i = end
    parts.append(text[pos:])
    return _flatten_parts(parts)


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
