# omg_cli/deny.py
from __future__ import annotations

import json
import os
import re
import shlex
from functools import lru_cache
from typing import Any

# Executable names that default workers must not invoke as external agent CLIs
_DENY_BINS = r"(?:claude|codex|omx|agy|cursor-agent|kimi)"

# Command-position only: start of string, a NEWLINE, or after shell operators
# (not bare whitespace). A denied bin on its own line (multi-line scripts,
# heredocs, sequential setup+run) is command-position too — the newline class
# member closes the "no semicolon needed" bypass.
# Bare "echo claude is a word" must NOT match (claude is an argument, not a command head).
_CMD_POS = r"(?:^|[;&|(`\n\r]|\|\||&&)"
_ENV_ASSIGNS = r"(?:(?:[A-Za-z_][\w]*=\S*\s+)*)"
# Wrappers that still leave the denied bin in command position after them.
# Path-prefixed env/exec allowed: /usr/bin/env claude, /bin/exec codex.
_WRAPPER_BIN = r"(?:(?:\S*/)?(?:env|command|xargs|nice|nohup|sudo|time|exec))"
_WRAPPERS = rf"(?:{_WRAPPER_BIN}\s+(?:--\s+)*)*"
_PATH_PREFIX = r"(?:\S*/)?"
_SHELL_WORD = r"""(?:\\.|[^\s;&|()'"`\\]+|'[^']*'|"(?:\\.|[^"\\])*")+"""
_CONTROL_COMMAND_WORD = r"(?:if|elif|while|until|then|else|do|coproc|!)"
_FUNCTION_GROUP_LEAD = (
    rf"(?:(?:function\s+)?{_SHELL_WORD}\s*\(\s*\)"
    rf"|function\s+{_SHELL_WORD})\s*\{{\s+"
)
_COMMAND_LEAD = (
    rf"(?:(?:{_CONTROL_COMMAND_WORD})\s+"
    rf"|\{{\s+"
    rf"|{_FUNCTION_GROUP_LEAD})*"
)
_DENIED_BIN_NAMES = frozenset(
    {"claude", "codex", "omx", "agy", "cursor-agent", "kimi"}
)

_DENY_AT_CMD_POS = re.compile(
    rf"{_CMD_POS}\s*{_ENV_ASSIGNS}{_WRAPPERS}{_PATH_PREFIX}{_DENY_BINS}\b",
    re.IGNORECASE,
)
_DECODED_COMMAND_HEAD = re.compile(
    rf"{_CMD_POS}\s*"
    rf"{_COMMAND_LEAD}"
    rf"{_ENV_ASSIGNS}{_WRAPPERS}"
    rf"(?P<word>{_SHELL_WORD})",
    re.IGNORECASE,
)
_DYNAMIC_COMMAND_PREFIX = re.compile(
    rf"{_CMD_POS}\s*{_COMMAND_LEAD}{_ENV_ASSIGNS}{_WRAPPERS}",
    re.IGNORECASE,
)
_CASE_HEAD = re.compile(
    rf"(?:{_CMD_POS}|\))\s*{_COMMAND_LEAD}case\b",
    re.IGNORECASE,
)
_CASE_IN = re.compile(r"\bin\b", re.IGNORECASE)
_CASE_END = re.compile(rf"{_CMD_POS}\s*esac\b", re.IGNORECASE)
_CASE_PATTERN_COMMAND_HEAD = re.compile(
    rf"\)\s*{_ENV_ASSIGNS}{_WRAPPERS}(?P<word>{_SHELL_WORD})",
    re.IGNORECASE,
)
_CONDITIONAL_OPEN_AT_END = re.compile(
    rf"{_CMD_POS}\s*{_COMMAND_LEAD}\[\[\Z",
    re.IGNORECASE,
)
_ARRAY_ASSIGNMENT_AT_END = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\r\n]*\])?\+?=\Z"
)
_OMC_TEAM = re.compile(rf"{_CMD_POS}\s*omc\s+team\b", re.IGNORECASE)
_OMG_TEAM = re.compile(rf"{_CMD_POS}\s*omg\s+team\b", re.IGNORECASE)

# eval claude ... (command-position eval of a deny bin)
_EVAL = re.compile(
    rf"{_CMD_POS}\s*{_ENV_ASSIGNS}{_WRAPPERS}(?:\S*/)?eval\s+(?:['\"]?){_PATH_PREFIX}{_DENY_BINS}\b",
    re.IGNORECASE,
)
_EVAL_HEAD = re.compile(
    rf"{_CMD_POS}\s*{_ENV_ASSIGNS}{_WRAPPERS}(?:\S*/)?eval\b",
    re.IGNORECASE,
)

# sh/bash/zsh -c / -lc (login+command) head. The first argument is decoded and
# recursively inspected instead of using a greedy regex across later commands.
# Path-prefixed shells: /bin/bash -c 'claude'
# Requires short-flag cluster that includes `c` (so bare `bash -l` is not a hit).
_SHELL_C_HEAD = re.compile(
    rf"{_CMD_POS}\s*"
    rf"{_ENV_ASSIGNS}"
    rf"{_WRAPPERS}"
    rf"(?:\S*/)?(?:sh|bash|zsh)\s+-"
    rf"[A-Za-z]*c[A-Za-z]*"  # -c, -lc, -cl, any short-flag soup that includes c
    rf"\s+",
    re.IGNORECASE,
)


def _decode_ansi_c_string(value: str) -> str | None:
    """Decode Bash ANSI-C quoting, returning ``None`` for unusable NUL data."""

    simple_escapes = {
        "a": "\a",
        "b": "\b",
        "e": "\x1b",
        "E": "\x1b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "\\": "\\",
        "'": "'",
        '"': '"',
        "?": "?",
    }
    decoded: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "\\":
            decoded.append(value[index])
            index += 1
            continue
        if index + 1 >= len(value):
            decoded.append("\\")
            break

        escape = value[index + 1]
        if escape in simple_escapes:
            decoded.append(simple_escapes[escape])
            index += 2
            continue
        if escape in "\r\n":
            index += 2
            if escape == "\r" and index < len(value) and value[index] == "\n":
                index += 1
            continue
        if escape == "c" and index + 2 < len(value):
            control = ord(value[index + 2].upper()) ^ 0x40
            if control == 0:
                return None
            decoded.append(chr(control))
            index += 3
            continue
        if escape == "x":
            cursor = index + 2
            while (
                cursor < len(value)
                and cursor < index + 4
                and value[cursor] in "0123456789abcdefABCDEF"
            ):
                cursor += 1
            if cursor == index + 2:
                decoded.append("\\x")
                index += 2
                continue
            codepoint = int(value[index + 2 : cursor], 16)
            if codepoint == 0:
                return None
            decoded.append(chr(codepoint))
            index = cursor
            continue
        if escape in {"u", "U"}:
            limit = index + (6 if escape == "u" else 10)
            cursor = index + 2
            while (
                cursor < len(value)
                and cursor < limit
                and value[cursor] in "0123456789abcdefABCDEF"
            ):
                cursor += 1
            if cursor == index + 2:
                decoded.append("\\" + escape)
                index += 2
                continue
            codepoint = int(value[index + 2 : cursor], 16)
            if codepoint == 0 or codepoint > 0x10FFFF:
                return None
            decoded.append(chr(codepoint))
            index = cursor
            continue
        if escape in "01234567":
            cursor = index + 1
            digit_limit = index + (5 if escape == "0" else 4)
            while (
                cursor < len(value)
                and cursor < digit_limit
                and value[cursor] in "01234567"
            ):
                cursor += 1
            codepoint = int(value[index + 1 : cursor], 8) & 0xFF
            if codepoint == 0:
                return None
            decoded.append(chr(codepoint))
            index = cursor
            continue

        # Bash preserves the backslash for an unknown ANSI-C escape.
        decoded.extend(("\\", escape))
        index += 2

    return "".join(decoded)


def _parse_heredoc(
    command: str,
    position: int,
) -> tuple[str, bool, bool, int] | None:
    """Parse a ``<<`` redirection at ``position``.

    Return ``(delimiter, strip_tabs, quoted, end_position)``.  Here-strings
    (``<<<``) are intentionally excluded.
    """

    if (
        not command.startswith("<<", position)
        or command.startswith("<<<", position)
        or (position > 0 and command[position - 1] == "<")
    ):
        return None

    cursor = position + 2
    strip_tabs = cursor < len(command) and command[cursor] == "-"
    if strip_tabs:
        cursor += 1
    while cursor < len(command) and command[cursor] in " \t":
        cursor += 1
    if cursor >= len(command) or command[cursor] in "\r\n":
        return None

    delimiter: list[str] = []
    quoted = False
    while cursor < len(command):
        char = command[cursor]
        following = command[cursor + 1] if cursor + 1 < len(command) else ""
        ansi_c_quote = char == "$" and following == "'"
        locale_quote = char == "$" and following == '"'
        if ansi_c_quote or locale_quote:
            quoted = True
            if locale_quote:
                # The runtime locale may translate $"...". Without the exact
                # delimiter, do not mask any subsequent command as heredoc data.
                return None
            cursor += 1
            char = command[cursor]
            following = command[cursor + 1] if cursor + 1 < len(command) else ""
        if char.isspace() or char in ";&|()<>":
            break
        if char == "'":
            quoted = True
            if ansi_c_quote:
                cursor += 1
                raw_ansi_c: list[str] = []
                while cursor < len(command) and command[cursor] != "'":
                    following = (
                        command[cursor + 1] if cursor + 1 < len(command) else ""
                    )
                    if command[cursor] == "\\" and following:
                        raw_ansi_c.extend(("\\", following))
                        cursor += 2
                    else:
                        raw_ansi_c.append(command[cursor])
                        cursor += 1
                if cursor >= len(command):
                    return None
                decoded_ansi_c = _decode_ansi_c_string("".join(raw_ansi_c))
                if decoded_ansi_c is None:
                    return None
                delimiter.append(decoded_ansi_c)
                cursor += 1
                continue
            closing = command.find("'", cursor + 1)
            if closing < 0:
                return None
            delimiter.append(command[cursor + 1 : closing])
            cursor = closing + 1
            continue
        if char == '"':
            quoted = True
            cursor += 1
            while cursor < len(command) and command[cursor] != '"':
                following = command[cursor + 1] if cursor + 1 < len(command) else ""
                if command[cursor] == "\\" and following:
                    if following in {'$', "`", '"', "\\"}:
                        delimiter.append(following)
                        cursor += 2
                    elif following in {"\n", "\r"}:
                        cursor += 2
                        if (
                            following == "\r"
                            and cursor < len(command)
                            and command[cursor] == "\n"
                        ):
                            cursor += 1
                    else:
                        delimiter.append("\\")
                        cursor += 1
                else:
                    delimiter.append(command[cursor])
                    cursor += 1
            if cursor >= len(command):
                return None
            cursor += 1
            continue
        if char == "\\" and following:
            if following in {"\n", "\r"}:
                cursor += 2
                if (
                    following == "\r"
                    and cursor < len(command)
                    and command[cursor] == "\n"
                ):
                    cursor += 1
                continue
            quoted = True
            delimiter.append(following)
            cursor += 2
            continue
        delimiter.append(char)
        cursor += 1

    if not delimiter:
        return None
    return "".join(delimiter), strip_tabs, quoted, cursor


def _line_break(command: str, start: int) -> tuple[int, int]:
    """Return ``(line_end, next_line_start)`` from ``start``."""

    cursor = start
    while cursor < len(command) and command[cursor] not in "\r\n":
        cursor += 1
    if cursor >= len(command):
        return len(command), len(command)
    next_line = cursor + 1
    if command[cursor] == "\r" and next_line < len(command) and command[next_line] == "\n":
        next_line += 1
    return cursor, next_line


def _command_substitution_close(
    command: str,
    open_position: int,
    limit: int,
) -> int | None:
    """Find the ``)`` paired with a heredoc-body ``$(`` opener."""

    body = command[open_position + 1 : limit]
    body_contexts = _shell_context_map(body)
    depth = 1
    for offset, char in enumerate(body):
        if body_contexts[offset] != _EXECUTABLE_CONTEXT:
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            if _case_pattern_is_active(body, offset):
                continue
            depth -= 1
            if depth == 0:
                return open_position + 1 + offset
    return None


def _backtick_substitution_close(
    command: str,
    open_position: int,
    limit: int,
) -> int | None:
    """Find an unescaped closing backtick in an expanding heredoc body."""

    index = open_position + 1
    while index < limit:
        if command[index] == "\\" and index + 1 < limit:
            index += 2
            continue
        if command[index] == "`":
            return index
        index += 1
    return None


def _heredoc_body_bounds(command: str, start: int, end: int) -> tuple[int, int]:
    """Exclude the leading and terminator lines from a heredoc range."""

    _, body_start = _line_break(command, start)
    last_cr = command.rfind("\r", body_start, end)
    last_lf = command.rfind("\n", body_start, end)
    terminator_break = max(last_cr, last_lf)
    body_end = terminator_break if terminator_break >= body_start else body_start
    return body_start, body_end


def _heredoc_ranges(
    command: str,
    newline_position: int,
    pending: list[tuple[str, bool, bool]],
) -> tuple[list[tuple[int, int, bool]], int]:
    """Locate pending heredoc bodies and the final terminator newline.

    Ranges begin at the preceding newline because deny regexes include that
    newline in their command-position match.  The final terminator newline is
    excluded so a real command on the following line remains executable.
    """

    _, cursor = _line_break(command, newline_position)
    marker = newline_position
    ranges: list[tuple[int, int, bool]] = []
    resume = len(command)

    for delimiter, strip_tabs, quoted in pending:
        while cursor <= len(command):
            line_end, next_line = _line_break(command, cursor)
            line = command[cursor:line_end]
            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate == delimiter:
                resume = line_end
                ranges.append((marker, resume, quoted))
                cursor = next_line
                marker = resume
                break
            if line_end >= len(command):
                ranges.append((marker, len(command), quoted))
                return ranges, len(command)
            cursor = next_line

    return ranges, resume


_LITERAL_CONTEXT = 0
_EXECUTABLE_CONTEXT = 1
_LINE_CONTINUATION = 2
_EXECUTABLE_CONTEXTS = frozenset({"normal", "command_substitution", "backtick"})


def _mark_unquoted_heredoc_continuations(
    command: str,
    start: int,
    end: int,
    contexts: list[int],
) -> None:
    """Mark active backslash-newline pairs inside an expanding heredoc."""

    index = start
    while index < end:
        if command[index] not in "\r\n":
            index += 1
            continue
        backslashes = 0
        cursor = index - 1
        while cursor >= start and command[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2:
            contexts[index] = _LINE_CONTINUATION
            if (
                command[index] == "\r"
                and index + 1 < end
                and command[index + 1] == "\n"
            ):
                contexts[index + 1] = _LINE_CONTINUATION
                index += 1
        index += 1


def _mark_unquoted_heredoc_expansions(
    command: str,
    start: int,
    end: int,
    contexts: list[int],
) -> None:
    """Mark only command/backtick substitutions in unquoted heredoc data."""

    body_start, body_end = _heredoc_body_bounds(command, start, end)
    index = body_start
    while index < body_end:
        char = command[index]
        following = command[index + 1] if index + 1 < body_end else ""
        if char == "\\" and following in {"\\", "$", "`", "\r", "\n"}:
            index += 2
            if (
                following == "\r"
                and index < body_end
                and command[index] == "\n"
            ):
                index += 1
            continue
        if (
            char == "$"
            and following == "("
            and not command.startswith("$((", index)
        ):
            close = _command_substitution_close(command, index + 1, body_end)
            if close is None:
                index += 2
                continue
            inner = command[index + 2 : close]
            inner_contexts = _shell_context_map(inner)
            contexts[index + 1] = _EXECUTABLE_CONTEXT
            contexts[index + 2 : close] = inner_contexts[:-1]
            index = close + 1
            continue
        if char == "`":
            close = _backtick_substitution_close(command, index, body_end)
            if close is None:
                index += 1
                continue
            inner = command[index + 1 : close]
            inner_contexts = _shell_context_map(inner)
            contexts[index] = _EXECUTABLE_CONTEXT
            contexts[index + 1 : close] = inner_contexts[:-1]
            index = close + 1
            continue
        index += 1


def _conditional_opens_at(command: str, position: int) -> bool:
    """Recognize a command-position ``[[`` without rescanning the full input."""

    segment_start = position
    while (
        segment_start > 0
        and command[segment_start - 1] not in ";&|(`\n\r"
    ):
        segment_start -= 1
    if segment_start > 0:
        segment_start -= 1
    return (
        _CONDITIONAL_OPEN_AT_END.search(
            command,
            segment_start,
            position + 2,
        )
        is not None
    )


def _array_assignment_opens_at(command: str, position: int) -> bool:
    """Recognize the no-whitespace ``name=(`` array-assignment form."""

    word_start = position
    while (
        word_start > 0
        and command[word_start - 1] not in " \t\r\n;&|"
    ):
        word_start -= 1
    return (
        _ARRAY_ASSIGNMENT_AT_END.fullmatch(command, word_start, position)
        is not None
    )


@lru_cache(maxsize=128)
def _shell_context_map(command: str) -> tuple[int, ...]:
    """Build shell-context classifications for every character in one pass."""

    contexts = [_LITERAL_CONTEXT] * (len(command) + 1)
    # Entries are (context, parenthesis_depth, at_word_start). Quote contexts
    # return to the previous executable context when popped.
    stack: list[tuple[str, int, bool]] = [("normal", 0, True)]
    pending_heredocs: list[tuple[str, bool, bool]] = []
    index = 0

    while index < len(command):
        context, depth, at_word_start = stack[-1]
        executable = context in _EXECUTABLE_CONTEXTS
        contexts[index] = (
            _EXECUTABLE_CONTEXT if executable else _LITERAL_CONTEXT
        )
        char = command[index]
        following = command[index + 1] if index + 1 < len(command) else ""

        if context == "single":
            if char == "'":
                stack.pop()
            index += 1
            continue

        if context == "ansi_c_single":
            if char == "\\" and following:
                index += 2
                continue
            if char == "'":
                stack.pop()
            index += 1
            continue

        if context == "double":
            if char == "\\" and following in {'$', '`', '"', "\\", "\n", "\r"}:
                contexts[index + 1] = (
                    _LINE_CONTINUATION
                    if following in {"\n", "\r"}
                    else _LITERAL_CONTEXT
                )
                index += 2
                if (
                    following == "\r"
                    and index < len(command)
                    and command[index] == "\n"
                ):
                    contexts[index] = _LINE_CONTINUATION
                    index += 1
                continue
            if char == '"':
                stack.pop()
                index += 1
                continue
            if char == "$" and following == "(":
                if index + 2 < len(command) and command[index + 2] == "(":
                    contexts[index + 1] = _LITERAL_CONTEXT
                    contexts[index + 2] = _LITERAL_CONTEXT
                    stack.append(("arithmetic", 2, False))
                    index += 3
                else:
                    contexts[index + 1] = _EXECUTABLE_CONTEXT
                    stack.append(("command_substitution", 1, True))
                    index += 2
                continue
            if char == "`":
                contexts[index] = _EXECUTABLE_CONTEXT
                stack.append(("backtick", 0, True))
                index += 1
                continue
            index += 1
            continue

        if context == "arithmetic":
            if char == "\\" and following:
                index += 2
                continue
            if char == "$" and following == "(":
                if index + 2 < len(command) and command[index + 2] == "(":
                    contexts[index + 1] = _LITERAL_CONTEXT
                    contexts[index + 2] = _LITERAL_CONTEXT
                    stack.append(("arithmetic", 2, False))
                    index += 3
                else:
                    contexts[index + 1] = _EXECUTABLE_CONTEXT
                    stack.append(("command_substitution", 1, True))
                    index += 2
                continue
            if char == "`":
                contexts[index] = _EXECUTABLE_CONTEXT
                stack.append(("backtick", 0, True))
                index += 1
                continue
            if char == "(":
                stack[-1] = (context, depth + 1, False)
            elif char == ")":
                if depth == 1:
                    stack.pop()
                else:
                    stack[-1] = (context, depth - 1, False)
            index += 1
            continue

        if context in {"array", "conditional"}:
            if char == "\\" and following:
                index += 2
                continue
            if context == "conditional" and char == "]" and following == "]":
                contexts[index + 1] = _LITERAL_CONTEXT
                stack.pop()
                index += 2
                continue
            if char == "$" and following == "'":
                stack.append(("ansi_c_single", 0, False))
                index += 2
                continue
            if char == "'":
                stack.append(("single", 0, False))
                index += 1
                continue
            if char == '"':
                stack.append(("double", 0, False))
                index += 1
                continue
            if char == "`":
                contexts[index] = _EXECUTABLE_CONTEXT
                stack.append(("backtick", 0, True))
                index += 1
                continue
            if char == "$" and following == "(":
                if index + 2 < len(command) and command[index + 2] == "(":
                    contexts[index + 1] = _LITERAL_CONTEXT
                    contexts[index + 2] = _LITERAL_CONTEXT
                    stack.append(("arithmetic", 2, False))
                    index += 3
                else:
                    contexts[index + 1] = _EXECUTABLE_CONTEXT
                    stack.append(("command_substitution", 1, True))
                    index += 2
                continue
            if char in "<>" and following == "(":
                contexts[index + 1] = _EXECUTABLE_CONTEXT
                stack.append(("command_substitution", 1, True))
                index += 2
                continue
            if context == "array":
                if char == "(":
                    stack[-1] = (context, depth + 1, False)
                elif char == ")":
                    if depth == 1:
                        stack.pop()
                    else:
                        stack[-1] = (context, depth - 1, False)
            index += 1
            continue

        # normal, command substitution, and backtick bodies are executable
        # shell contexts. Quotes nested inside them are literal until closed.
        if char == "\\" and following:
            contexts[index + 1] = (
                _LINE_CONTINUATION
                if following in {"\n", "\r"}
                else _LITERAL_CONTEXT
            )
            if following not in {"\n", "\r"}:
                stack[-1] = (context, depth, False)
            index += 2
            if (
                following == "\r"
                and index < len(command)
                and command[index] == "\n"
            ):
                contexts[index] = _LINE_CONTINUATION
                index += 1
            continue
        if context == "backtick" and char == "`":
            stack.pop()
            index += 1
            continue
        if char == "#" and at_word_start:
            line_end, _ = _line_break(command, index)
            contexts[index:line_end] = [_LITERAL_CONTEXT] * (line_end - index)
            index = line_end
            continue
        if char in "\r\n" and pending_heredocs:
            ranges, resume = _heredoc_ranges(command, index, pending_heredocs)
            for start, end, quoted in ranges:
                contexts[start:end] = [_LITERAL_CONTEXT] * (end - start)
                if not quoted:
                    _mark_unquoted_heredoc_continuations(
                        command,
                        start,
                        end,
                        contexts,
                    )
                    _mark_unquoted_heredoc_expansions(
                        command,
                        start,
                        end,
                        contexts,
                    )
            pending_heredocs.clear()
            index = resume
            continue
        if char == "<" and following == "<":
            heredoc = _parse_heredoc(command, index)
            if heredoc is not None:
                delimiter, strip_tabs, quoted, end_position = heredoc
                pending_heredocs.append((delimiter, strip_tabs, quoted))
                stack[-1] = (context, depth, False)
                index = end_position
                continue
        if (
            char == "["
            and following == "["
            and _conditional_opens_at(command, index)
        ):
            contexts[index] = _LITERAL_CONTEXT
            contexts[index + 1] = _LITERAL_CONTEXT
            stack[-1] = (context, depth, False)
            stack.append(("conditional", 0, False))
            index += 2
            continue
        if char == "$" and following == "'":
            stack[-1] = (context, depth, False)
            stack.append(("ansi_c_single", 0, False))
            index += 2
            continue
        if char == "'":
            stack[-1] = (context, depth, False)
            stack.append(("single", 0, False))
            index += 1
            continue
        if char == '"':
            stack[-1] = (context, depth, False)
            stack.append(("double", 0, False))
            index += 1
            continue
        if char == "`":
            stack[-1] = (context, depth, False)
            stack.append(("backtick", 0, True))
            index += 1
            continue
        if char == "$" and following == "(":
            stack[-1] = (context, depth, False)
            if index + 2 < len(command) and command[index + 2] == "(":
                contexts[index + 1] = _LITERAL_CONTEXT
                contexts[index + 2] = _LITERAL_CONTEXT
                stack.append(("arithmetic", 2, False))
                index += 3
            else:
                contexts[index + 1] = _EXECUTABLE_CONTEXT
                stack.append(("command_substitution", 1, True))
                index += 2
            continue
        if char == "(" and _array_assignment_opens_at(command, index):
            contexts[index] = _LITERAL_CONTEXT
            stack[-1] = (context, depth, False)
            stack.append(("array", 1, False))
            index += 1
            continue
        if char == "(" and following == "(":
            contexts[index + 1] = _LITERAL_CONTEXT
            stack[-1] = (context, depth, False)
            stack.append(("arithmetic", 2, False))
            index += 2
            continue
        if context == "command_substitution":
            if char == "(":
                stack[-1] = (context, depth + 1, True)
            elif char == ")":
                if depth == 1:
                    stack.pop()
                else:
                    stack[-1] = (context, depth - 1, False)
            elif char.isspace() or char in ";&|<>":
                stack[-1] = (context, depth, True)
            else:
                stack[-1] = (context, depth, False)
        elif char.isspace() or char in ";&|()<>":
            stack[-1] = (context, depth, True)
        else:
            stack[-1] = (context, depth, False)
        index += 1

    context = stack[-1][0]
    contexts[len(command)] = (
        _EXECUTABLE_CONTEXT
        if context in _EXECUTABLE_CONTEXTS
        else _LITERAL_CONTEXT
    )
    return tuple(contexts)


def _shell_context_is_executable(command: str, position: int) -> bool | None:
    """Classify ``position`` as executable, literal, or line-continuation."""

    position = max(0, min(position, len(command)))
    context = _shell_context_map(command)[position]
    if context == _LINE_CONTINUATION:
        return None
    return context == _EXECUTABLE_CONTEXT


def _has_executable_match(pattern: re.Pattern[str], command: str) -> bool:
    search_position = 0
    while match := pattern.search(command, search_position):
        context = _shell_context_is_executable(command, match.start())
        if context is True:
            return True
        if context is None:
            marker = match.start()
            end = marker + 1
            if (
                marker < len(command)
                and command[marker] == "\r"
                and end < len(command)
                and command[end] == "\n"
            ):
                end += 1
            collapsed = command[: marker - 1] + command[end:]
            if _has_executable_match(pattern, collapsed):
                return True
        # A pattern such as ``sh -c '<body>'`` may greedily span from an inert
        # quoted occurrence across a later real invocation. Resume one
        # character after the rejected start so overlapping candidates remain
        # visible instead of advancing to the greedy match's end.
        search_position = match.start() + 1
    return False


def _collapse_line_continuations(command: str) -> str:
    """Remove active backslash-newline pairs before regex matching."""

    contexts = _shell_context_map(command)
    collapsed: list[str] = []
    index = 0
    while index < len(command):
        if (
            command[index] == "\\"
            and index + 1 < len(command)
            and contexts[index + 1] == _LINE_CONTINUATION
        ):
            index += 2
            if (
                index < len(command)
                and command[index - 1] == "\r"
                and command[index] == "\n"
            ):
                index += 1
            continue
        collapsed.append(command[index])
        index += 1
    return "".join(collapsed)


def _eval_argument_tail(command: str, start: int) -> str:
    """Return the outer-shell argument text following an ``eval`` command."""

    stack: list[tuple[str, int, bool]] = [("normal", 0, True)]
    index = start
    while index < len(command):
        context, depth, at_word_start = stack[-1]
        char = command[index]
        following = command[index + 1] if index + 1 < len(command) else ""

        if context == "single":
            if char == "'":
                stack.pop()
            index += 1
            continue
        if context == "ansi_c_single":
            if char == "\\" and following:
                index += 2
                continue
            if char == "'":
                stack.pop()
            index += 1
            continue
        if context == "double":
            if char == "\\" and following in {'$', '`', '"', "\\", "\n", "\r"}:
                index += 2
                continue
            if char == '"':
                stack.pop()
                index += 1
                continue
            if char == "$" and following == "(":
                stack.append(("command_substitution", 1, True))
                index += 2
                continue
            if char == "`":
                stack.append(("backtick", 0, True))
                index += 1
                continue
            index += 1
            continue

        if char == "\\" and following:
            stack[-1] = (context, depth, False)
            index += 2
            continue
        if context == "backtick" and char == "`":
            stack.pop()
            index += 1
            continue
        if len(stack) == 1:
            if char in ";&|\r\n)" or (char == "#" and at_word_start):
                break
        if char == "$" and following == "'":
            stack[-1] = (context, depth, False)
            stack.append(("ansi_c_single", 0, False))
            index += 2
            continue
        if char == "'":
            stack[-1] = (context, depth, False)
            stack.append(("single", 0, False))
            index += 1
            continue
        if char == '"':
            stack[-1] = (context, depth, False)
            stack.append(("double", 0, False))
            index += 1
            continue
        if char == "`":
            stack[-1] = (context, depth, False)
            stack.append(("backtick", 0, True))
            index += 1
            continue
        if char == "$" and following == "(":
            stack[-1] = (context, depth, False)
            stack.append(("command_substitution", 1, True))
            index += 2
            continue
        if context == "command_substitution":
            if char == "(":
                stack[-1] = (context, depth + 1, True)
            elif char == ")":
                if depth == 1:
                    stack.pop()
                else:
                    stack[-1] = (context, depth - 1, False)
            elif char.isspace() or char in ";&|<>":
                stack[-1] = (context, depth, True)
            else:
                stack[-1] = (context, depth, False)
        elif char.isspace() or char in "<>":
            stack[-1] = (context, depth, True)
        else:
            stack[-1] = (context, depth, False)
        index += 1

    return command[start:index]


def _decode_shell_words(raw_body: str) -> list[str]:
    try:
        words = shlex.split(raw_body, comments=False, posix=True)
    except ValueError:
        return [raw_body]
    if "$'" in raw_body or '$"' in raw_body:
        words = [word[1:] if word.startswith("$") else word for word in words]
    return words


def _decoded_shell_word_is_denied(raw_word: str) -> bool:
    words = _decode_shell_words(raw_word)
    if len(words) != 1:
        return False
    executable = words[0].rsplit("/", 1)[-1].lower()
    return executable in _DENIED_BIN_NAMES


def _starts_shell_expansion(char: str) -> bool:
    return bool(
        char
        and (
            char.isalnum()
            or char == "_"
            or char in "({*@#?$!-"
        )
    )


def _shell_word_has_dynamic_expansion(command: str, start: int = 0) -> bool:
    """Return whether one shell word contains an active runtime expansion."""

    index = start
    while index < len(command) and command[index].isspace():
        index += 1
    context = "normal"
    while index < len(command):
        char = command[index]
        following = command[index + 1] if index + 1 < len(command) else ""
        if context == "single":
            if char == "'":
                context = "normal"
            index += 1
            continue
        if context == "ansi_c_single":
            if char == "\\" and following:
                index += 2
                continue
            if char == "'":
                context = "normal"
            index += 1
            continue
        if context == "double":
            if char == "\\" and following in {'$', '`', '"', "\\", "\r", "\n"}:
                index += 2
                if (
                    following == "\r"
                    and index < len(command)
                    and command[index] == "\n"
                ):
                    index += 1
                continue
            if char == '"':
                context = "normal"
                index += 1
                continue
            if char == "`" or (
                char == "$" and _starts_shell_expansion(following)
            ):
                return True
            index += 1
            continue

        if char.isspace() or char in ";&|()<>":
            return False
        if char == "\\" and following:
            index += 2
            continue
        if char == "$" and following == "'":
            context = "ansi_c_single"
            index += 2
            continue
        if char == "$" and following == '"':
            # Locale translation can alter a command word at runtime.
            return True
        if char == "'":
            context = "single"
            index += 1
            continue
        if char == '"':
            context = "double"
            index += 1
            continue
        if char == "`" or (
            char == "$" and _starts_shell_expansion(following)
        ):
            return True
        index += 1
    return False


def _has_dynamic_command_head(command: str) -> bool:
    """Fail closed when an executable word is synthesized at runtime."""

    search_position = 0
    while match := _DYNAMIC_COMMAND_PREFIX.search(command, search_position):
        if (
            _shell_context_is_executable(command, match.start()) is True
            and _shell_word_has_dynamic_expansion(command, match.end())
        ):
            return True
        search_position = match.start() + 1
    return False


def _has_denied_decoded_command_head(command: str) -> bool:
    """Deny quoted or concatenated spellings of a blocked executable word."""

    search_position = 0
    while match := _DECODED_COMMAND_HEAD.search(command, search_position):
        if (
            _shell_context_is_executable(command, match.start()) is True
            and _decoded_shell_word_is_denied(match.group("word"))
        ):
            return True
        search_position = match.start() + 1
    return False


def _case_pattern_is_active(command: str, position: int) -> bool:
    """Return whether ``position`` closes a pattern in an open case block."""

    contexts = _shell_context_map(command)
    case_heads: list[re.Match[str]] = []
    search_position = 0
    while match := _CASE_HEAD.search(command, search_position, position):
        if contexts[match.start()] == _EXECUTABLE_CONTEXT:
            case_heads.append(match)
        search_position = match.start() + 1
    if not case_heads:
        return False

    case_ends: list[re.Match[str]] = []
    search_position = 0
    while match := _CASE_END.search(command, search_position, position):
        if contexts[match.start()] == _EXECUTABLE_CONTEXT:
            case_ends.append(match)
        search_position = match.start() + 1

    events = sorted(
        [(match.start(), 1, match) for match in case_heads]
        + [(match.start(), -1, match) for match in case_ends],
        key=lambda item: (item[0], -item[1]),
    )
    active_cases: list[re.Match[str]] = []
    for _, kind, match in events:
        if kind == 1:
            active_cases.append(match)
        elif active_cases:
            active_cases.pop()
    if not active_cases:
        return False
    latest_case = active_cases[-1]

    case_in: re.Match[str] | None = None
    search_position = latest_case.end()
    while match := _CASE_IN.search(command, search_position, position):
        if _shell_context_is_executable(command, match.start()) is True:
            case_in = match
            break
        search_position = match.start() + 1
    if case_in is None:
        return False

    nested_heads = {
        match.start(): match
        for match in case_heads
        if latest_case.start() < match.start() < position
    }
    nested_ends = {
        match.start(): match
        for match in case_ends
        if latest_case.start() < match.start() < position
    }
    expects_pattern_close = True
    nested_case_depth = 0
    index = case_in.end()
    while index <= position:
        if contexts[index] != _EXECUTABLE_CONTEXT:
            index += 1
            continue
        nested_head = nested_heads.get(index)
        if nested_head is not None:
            if command[index] == ")" and expects_pattern_close:
                expects_pattern_close = False
            nested_case_depth += 1
            index = nested_head.end()
            continue
        nested_end = nested_ends.get(index)
        if nested_end is not None and nested_case_depth:
            nested_case_depth -= 1
            index = nested_end.end()
            continue
        if nested_case_depth:
            index += 1
            continue
        if command.startswith(";;&", index):
            expects_pattern_close = True
            index += 3
            continue
        if command.startswith((";;", ";&"), index):
            expects_pattern_close = True
            index += 2
            continue
        if command[index] == ")" and expects_pattern_close:
            if index == position:
                return True
            expects_pattern_close = False
        index += 1
    return False


def _has_denied_case_command_head(command: str) -> bool:
    """Deny a blocked executable immediately after a case-pattern ``)``."""

    if not _has_executable_match(_CASE_HEAD, command):
        return False
    search_position = 0
    while match := _CASE_PATTERN_COMMAND_HEAD.search(command, search_position):
        if (
            _shell_context_is_executable(command, match.start()) is True
            and _decoded_shell_word_is_denied(match.group("word"))
            and _case_pattern_is_active(command, match.start())
        ):
            return True
        search_position = match.start() + 1
    return False


def _decode_eval_body(raw_body: str) -> str:
    return " ".join(_decode_shell_words(raw_body))


def _has_denied_eval_body(command: str, depth: int) -> bool:
    search_position = 0
    while match := _EVAL_HEAD.search(command, search_position):
        if _shell_context_is_executable(command, match.start()) is True:
            body = _decode_eval_body(_eval_argument_tail(command, match.end()))
            if body:
                if _has_dynamic_command_head(body):
                    return True
                if depth >= 8:
                    if re.search(rf"\b{_DENY_BINS}\b", body, re.IGNORECASE):
                        return True
                elif _should_deny_command(body, depth + 1):
                    return True
        search_position = match.start() + 1
    return False


def _has_denied_shell_c_body(command: str, depth: int) -> bool:
    search_position = 0
    while match := _SHELL_C_HEAD.search(command, search_position):
        if _shell_context_is_executable(command, match.start()) is True:
            raw_arguments = _eval_argument_tail(command, match.end())
            words = _decode_shell_words(raw_arguments)
            body = words[0] if words else ""
            if body:
                if _has_dynamic_command_head(body):
                    return True
                if depth >= 8:
                    if re.search(rf"\b{_DENY_BINS}\b", body, re.IGNORECASE):
                        return True
                elif _should_deny_command(body, depth + 1):
                    return True
        search_position = match.start() + 1
    return False


def _should_deny_command(command: str, eval_depth: int) -> bool:
    if not command or not isinstance(command, str):
        return False
    command = _collapse_line_continuations(command)
    # Deny when a blocked bin appears in command position (not as a free word/arg)
    if _has_executable_match(_DENY_AT_CMD_POS, command):
        return True
    if _has_denied_decoded_command_head(command):
        return True
    if _has_denied_case_command_head(command):
        return True
    if _has_executable_match(_OMC_TEAM, command) or _has_executable_match(
        _OMG_TEAM, command
    ):
        return True
    # sh/bash/zsh -c/-lc command strings are recursively inspected.
    if _has_denied_shell_c_body(command, eval_depth):
        return True
    if _has_executable_match(_EVAL, command):
        return True
    if _has_denied_eval_body(command, eval_depth):
        return True
    return False


def should_deny_command(command: str) -> bool:
    return _should_deny_command(command, 0)


# Role → required capability_mode for spawn_subagent fail-closed gate.
# Soft-gate: only effective when PreToolUse runs (still fail-open on hook crash).
_READ_ONLY_TYPES = frozenset(
    {
        "explore",
        "plan",
        "omg-critic",
        "omg-verifier",
        "oh-my-claudecode:explore",
        "oh-my-claudecode:code-reviewer",
        "oh-my-claudecode:security-reviewer",
        "oh-my-claudecode:architect",
        "oh-my-claudecode:critic",
        "oh-my-claudecode:planner",
    }
)
_READ_WRITE_TYPES = frozenset(
    {
        "omg-executor",
        "general-purpose",  # default implementer path in oh-my-grok skills
        "oh-my-claudecode:executor",
    }
)

_SAFE_RECEIPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_ISO8601 = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d{1,9}))?(Z|([+-])(\d{2}):(\d{2}))$"
)
_SPAWN_RECEIPT_KEYS = frozenset(
    {
        "store_kind",
        "schema_version",
        "receipt_id",
        "run_id",
        "team_id",
        "task_id",
        "parent_id",
        "parent_session_id",
        "requested_role",
        "capability_mode",
        "depth",
        "attempt",
        "receipt_generation",
        "lease_generation",
        "dispatch_nonce",
        "expires_at",
        "expected_state",
        "expected_sequence",
    }
)
_ROLE_RECEIPT_KEYS = frozenset(
    (_SPAWN_RECEIPT_KEYS - {"receipt_id", "store_kind"})
    | {"receipt_id", "store_kind", "spawn_receipt_hash"}
)
_RECEIPT_SAFE_FIELDS = (
    "receipt_id",
    "run_id",
    "team_id",
    "task_id",
    "parent_id",
    "parent_session_id",
    "requested_role",
    "dispatch_nonce",
    "expected_state",
)
_SHA256_K = (
    0x428A2F98,
    0x71374491,
    0xB5C0FBCF,
    0xE9B5DBA5,
    0x3956C25B,
    0x59F111F1,
    0x923F82A4,
    0xAB1C5ED5,
    0xD807AA98,
    0x12835B01,
    0x243185BE,
    0x550C7DC3,
    0x72BE5D74,
    0x80DEB1FE,
    0x9BDC06A7,
    0xC19BF174,
    0xE49B69C1,
    0xEFBE4786,
    0x0FC19DC6,
    0x240CA1CC,
    0x2DE92C6F,
    0x4A7484AA,
    0x5CB0A9DC,
    0x76F988DA,
    0x983E5152,
    0xA831C66D,
    0xB00327C8,
    0xBF597FC7,
    0xC6E00BF3,
    0xD5A79147,
    0x06CA6351,
    0x14292967,
    0x27B70A85,
    0x2E1B2138,
    0x4D2C6DFC,
    0x53380D13,
    0x650A7354,
    0x766A0ABB,
    0x81C2C92E,
    0x92722C85,
    0xA2BFE8A1,
    0xA81A664B,
    0xC24B8B70,
    0xC76C51A3,
    0xD192E819,
    0xD6990624,
    0xF40E3585,
    0x106AA070,
    0x19A4C116,
    0x1E376C08,
    0x2748774C,
    0x34B0BCB5,
    0x391C0CB3,
    0x4ED8AA4A,
    0x5B9CCA4F,
    0x682E6FF3,
    0x748F82EE,
    0x78A5636F,
    0x84C87814,
    0x8CC70208,
    0x90BEFFFA,
    0xA4506CEB,
    0xBEF9A3F7,
    0xC67178F2,
)


def _rotate_right(value: int, amount: int) -> int:
    return ((value >> amount) | (value << (32 - amount))) & 0xFFFFFFFF


def _standalone_sha256_hex(body: bytes) -> str:
    """Small standalone SHA-256 used by the generated deny soft-gate."""

    message = bytearray(body)
    bit_length = len(message) * 8
    message.append(0x80)
    while len(message) % 64 != 56:
        message.append(0)
    message.extend(bit_length.to_bytes(8, "big"))
    digest = [
        0x6A09E667,
        0xBB67AE85,
        0x3C6EF372,
        0xA54FF53A,
        0x510E527F,
        0x9B05688C,
        0x1F83D9AB,
        0x5BE0CD19,
    ]
    for offset in range(0, len(message), 64):
        words = [
            int.from_bytes(message[index : index + 4], "big")
            for index in range(offset, offset + 64, 4)
        ]
        for index in range(16, 64):
            left = words[index - 15]
            right = words[index - 2]
            sigma0 = _rotate_right(left, 7) ^ _rotate_right(left, 18) ^ (left >> 3)
            sigma1 = _rotate_right(right, 17) ^ _rotate_right(right, 19) ^ (right >> 10)
            words.append(
                (words[index - 16] + sigma0 + words[index - 7] + sigma1) & 0xFFFFFFFF
            )
        a, b, c, d, e, f, g, h = digest
        for index in range(64):
            big1 = _rotate_right(e, 6) ^ _rotate_right(e, 11) ^ _rotate_right(e, 25)
            choose = (e & f) ^ ((~e) & g)
            temp1 = (h + big1 + choose + _SHA256_K[index] + words[index]) & 0xFFFFFFFF
            big0 = _rotate_right(a, 2) ^ _rotate_right(a, 13) ^ _rotate_right(a, 22)
            majority = (a & b) ^ (a & c) ^ (b & c)
            temp2 = (big0 + majority) & 0xFFFFFFFF
            h, g, f, e, d, c, b, a = (
                g,
                f,
                e,
                (d + temp1) & 0xFFFFFFFF,
                c,
                b,
                a,
                (temp1 + temp2) & 0xFFFFFFFF,
            )
        digest = [
            (digest[0] + a) & 0xFFFFFFFF,
            (digest[1] + b) & 0xFFFFFFFF,
            (digest[2] + c) & 0xFFFFFFFF,
            (digest[3] + d) & 0xFFFFFFFF,
            (digest[4] + e) & 0xFFFFFFFF,
            (digest[5] + f) & 0xFFFFFFFF,
            (digest[6] + g) & 0xFFFFFFFF,
            (digest[7] + h) & 0xFFFFFFFF,
        ]
    return "".join(f"{word:08x}" for word in digest)


def _canonical_receipt_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _days_from_civil(year: int, month: int, day: int) -> int:
    adjusted_year = year - (1 if month <= 2 else 0)
    era = adjusted_year // 400
    year_of_era = adjusted_year - era * 400
    shifted_month = month + (-3 if month > 2 else 9)
    day_of_year = (153 * shifted_month + 2) // 5 + day - 1
    day_of_era = year_of_era * 365 + year_of_era // 4 - year_of_era // 100 + day_of_year
    return era * 146097 + day_of_era - 719468


def _iso8601_epoch(value: object) -> tuple[int, int]:
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    match = _ISO8601.fullmatch(value)
    if match is None:
        raise ValueError("timestamp must be ISO-8601")
    year, month, day, hour, minute, second = (
        int(match.group(index)) for index in range(1, 7)
    )
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    month_days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if (
        year < 1
        or month not in range(1, 13)
        or day not in range(1, month_days[month - 1] + 1)
        or hour not in range(24)
        or minute not in range(60)
        or second not in range(60)
    ):
        raise ValueError("timestamp components out of range")
    fraction = (match.group(7) or "").ljust(9, "0")
    nanoseconds = int(fraction or "0")
    offset_seconds = 0
    if match.group(8) != "Z":
        offset_hour = int(match.group(10))
        offset_minute = int(match.group(11))
        if offset_hour > 23 or offset_minute > 59:
            raise ValueError("timestamp offset out of range")
        offset_seconds = (offset_hour * 60 + offset_minute) * 60
        if match.group(9) == "-":
            offset_seconds = -offset_seconds
    epoch = (
        _days_from_civil(year, month, day) * 86400
        + hour * 3600
        + minute * 60
        + second
        - offset_seconds
    )
    return epoch, nanoseconds


def _current_epoch() -> tuple[int, int]:
    """Obtain wall-clock time using only generator-provided ``os``."""

    directory = os.environ.get("TMPDIR") or "/tmp"
    name = f".omg-receipt-clock-{os.getpid()}-{os.urandom(12).hex()}"
    path = os.path.join(directory, name)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        observed = os.fstat(descriptor)
        return observed.st_mtime_ns // 1_000_000_000, observed.st_mtime_ns % 1_000_000_000
    finally:
        os.close(descriptor)
        try:
            os.unlink(path)
        except OSError:
            pass


def _receipt_expectations(tin: dict[str, Any]) -> dict[str, Any]:
    nested = tin.get("receipt_expectation") or tin.get("receiptExpectation")
    return nested if isinstance(nested, dict) else tin


def _validate_receipt_pair(tin: dict[str, Any]) -> None:
    spawn = tin.get("spawn_receipt") or tin.get("spawnReceipt")
    role = tin.get("role_receipt") or tin.get("roleReceipt")
    if not isinstance(spawn, dict) or not isinstance(role, dict):
        raise ValueError("spawn and role receipts are both required")
    if set(spawn) != _SPAWN_RECEIPT_KEYS or set(role) != _ROLE_RECEIPT_KEYS:
        raise ValueError("receipt keys mismatch")
    if spawn["store_kind"] != "spawn_receipt" or spawn["schema_version"] != 1:
        raise ValueError("spawn receipt header mismatch")
    for field in _RECEIPT_SAFE_FIELDS:
        if not isinstance(spawn[field], str) or _SAFE_RECEIPT_ID.fullmatch(spawn[field]) is None:
            raise ValueError("spawn receipt identifier mismatch")
    if spawn["capability_mode"] not in {"read-only", "read-write"}:
        raise ValueError("spawn receipt capability mismatch")
    if spawn["depth"] != 1 or isinstance(spawn["depth"], bool):
        raise ValueError("spawn receipt depth mismatch")
    for field in (
        "attempt",
        "receipt_generation",
        "lease_generation",
        "expected_sequence",
    ):
        if isinstance(spawn[field], bool) or not isinstance(spawn[field], int) or spawn[field] < 0:
            raise ValueError("spawn receipt integer mismatch")

    expectation = _receipt_expectations(tin)
    now_value = expectation.get("observed_at") or expectation.get("now")
    current = _iso8601_epoch(now_value) if now_value is not None else _current_epoch()
    if _iso8601_epoch(spawn["expires_at"]) <= current:
        raise ValueError("spawn receipt expired")

    spawn_hash = _standalone_sha256_hex(_canonical_receipt_bytes(spawn))
    expected_role = {
        "store_kind": "role_receipt",
        "schema_version": 1,
        "receipt_id": f"role-{spawn['receipt_id']}",
        "spawn_receipt_hash": spawn_hash,
        **{
            field: spawn[field]
            for field in _SPAWN_RECEIPT_KEYS
            if field not in {"store_kind", "schema_version", "receipt_id"}
        },
    }
    if role != expected_role:
        raise ValueError("role receipt disagrees with spawn receipt")

    subagent_type, capability_mode = _spawn_fields(tin)
    if spawn["requested_role"].lower() != subagent_type:
        raise ValueError("spawn receipt requested role mismatch")
    if spawn["capability_mode"] != capability_mode.replace("_", "-"):
        raise ValueError("spawn receipt capability mismatch")
    expected_fields = (
        "run_id",
        "team_id",
        "task_id",
        "parent_id",
        "parent_session_id",
        "attempt",
        "receipt_generation",
        "lease_generation",
        "dispatch_nonce",
        "expected_state",
        "expected_sequence",
    )
    for field in expected_fields:
        if field not in expectation or expectation[field] != spawn[field]:
            raise ValueError(f"spawn receipt foreign or stale {field}")


def validate_spawn_authority(tin: dict[str, Any]) -> dict[str, str] | None:
    """Fail closed once either W0 native receipt is presented."""

    if not any(
        key in tin
        for key in ("spawn_receipt", "spawnReceipt", "role_receipt", "roleReceipt")
    ):
        return None
    try:
        _validate_receipt_pair(tin)
    except Exception:
        return {
            "decision": "deny",
            "reason": (
                "oh-my-grok: invalid, stale, foreign, replayed, or disagreeing "
                "spawn/role receipt; regenerate authority and retry"
            ),
        }
    return None


def _tool_input(event: dict[str, Any]) -> dict[str, Any]:
    tin = event.get("toolInput") or event.get("tool_input") or {}
    return tin if isinstance(tin, dict) else {}


def _spawn_fields(tin: dict[str, Any]) -> tuple[str, str]:
    """Return (subagent_type, capability_mode) lowercased, empty if missing."""
    st = (
        tin.get("subagent_type")
        or tin.get("subagentType")
        or tin.get("agent_type")
        or tin.get("agentType")
        or ""
    )
    cm = (
        tin.get("capability_mode")
        or tin.get("capabilityMode")
        or ""
    )
    return str(st).strip().lower(), str(cm).strip().lower()


def required_capability_mode(subagent_type: str) -> str | None:
    """Return required mode for *subagent_type*, or None if unknown (still require some mode)."""
    st = (subagent_type or "").strip().lower()
    if not st:
        return None
    if st in _READ_ONLY_TYPES or "critic" in st or "verifier" in st or "explore" in st:
        if st in _READ_WRITE_TYPES:
            return "read-write"  # explicit RW type wins
        return "read-only"
    if st in _READ_WRITE_TYPES or "executor" in st:
        return "read-write"
    # Unknown types: still require an explicit mode (caller enforces presence)
    return None


def suggested_capability_mode(subagent_type: str) -> str:
    """Best-effort mode for retry messaging when mode is missing or wrong."""
    req = required_capability_mode(subagent_type)
    if req:
        return req
    # Unknown type: default implementer-ish unless name looks read-only
    st = (subagent_type or "").strip().lower()
    if any(k in st for k in ("critic", "verif", "explore", "plan", "review", "architect")):
        return "read-only"
    return "read-write"


def spawn_deny_reason(*, st: str, cm: str, kind: str) -> str:
    """Human+model-facing deny text that **forces retry**, not abandon multi-agent.

    Models often stop spawning after a bare deny. The reason must say RETRY NOW
    with the exact field to set, and forbid solo-fallback as the default response.
    """
    suggested = suggested_capability_mode(st)
    type_label = st or "(missing subagent_type)"
    if kind == "missing":
        head = (
            f"oh-my-grok: spawn_subagent DENIED — missing capability_mode "
            f"(subagent_type={type_label!r})."
        )
    elif kind == "invalid":
        head = (
            f"oh-my-grok: spawn_subagent DENIED — invalid capability_mode={cm!r} "
            f"(subagent_type={type_label!r})."
        )
    elif kind == "execute_all":
        head = (
            f"oh-my-grok: spawn_subagent DENIED — capability_mode execute/all not allowed "
            f"(subagent_type={type_label!r})."
        )
    elif kind == "mismatch":
        head = (
            f"oh-my-grok: spawn_subagent DENIED — subagent_type={type_label!r} "
            f"requires capability_mode={suggested!r} (got {cm!r})."
        )
    else:
        head = "oh-my-grok: spawn_subagent DENIED."

    return (
        f"{head} "
        "RETRY IMMEDIATELY in this same turn: call spawn_subagent again with the SAME prompt "
        f"and capability_mode={suggested!r} "
        "(read-only for explore/plan/critic/verifier; read-write for general-purpose/"
        "omg-executor). "
        "Do NOT abandon multi-agent work. Do NOT switch to solo-only just because spawn was "
        "denied. Do NOT use capability_mode=execute or all. "
        f"Minimal fix: add parameter capability_mode={suggested!r}."
    )


def _depth_or_nested_spawn_denied(tin: dict[str, Any], st: str) -> str | None:
    """Executor/implementer children must not re-spawn (depth=1)."""
    depth = tin.get("depth") or tin.get("child_depth") or tin.get("spawn_depth")
    try:
        if depth is not None and int(depth) > 1:
            return (
                "oh-my-grok: spawn_subagent DENIED — child depth>1 forbidden "
                f"(subagent_type={st or '?'}). Workers must not re-spawn."
            )
    except (TypeError, ValueError):
        pass
    # Nested tool lists that include spawn are denied for executors
    tools = tin.get("tools") or tin.get("allowed_tools") or tin.get("allowedTools")
    if isinstance(tools, (list, tuple)):
        lowered = {str(t).strip().lower() for t in tools}
        if "spawn_subagent" in lowered or "task" in lowered:
            if st in _READ_WRITE_TYPES or "executor" in (st or ""):
                return (
                    "oh-my-grok: spawn_subagent DENIED — executor role may not "
                    "include spawn_subagent/Task in tools (depth=1)."
                )
    return None


def decide_spawn_subagent(tin: dict[str, Any]) -> dict[str, str]:
    """Fail-closed spawn policy when PreToolUse runs for spawn_subagent.

    - Missing capability_mode → deny (reason mandates immediate retry)
    - Mode incompatible with role table → deny (reason mandates retry with required mode)
    - execute/all denied; executor nested spawn denied
    - Unknown type with explicit mode → allow (host still applies the mode)
    """
    if os.environ.get("OMG_ALLOW_UNSAFE_SPAWN") == "1":
        return {"decision": "allow", "reason": "OMG_ALLOW_UNSAFE_SPAWN=1"}
    st, cm = _spawn_fields(tin)
    authority = validate_spawn_authority(tin)
    if authority is not None:
        return authority
    depth_deny = _depth_or_nested_spawn_denied(tin, st)
    if depth_deny:
        return {"decision": "deny", "reason": depth_deny}
    if not cm:
        return {
            "decision": "deny",
            "reason": spawn_deny_reason(st=st, cm=cm, kind="missing"),
        }
    if cm not in ("read-write", "read-only", "read_write", "read_only", "execute", "all"):
        return {
            "decision": "deny",
            "reason": spawn_deny_reason(st=st, cm=cm, kind="invalid"),
        }
    # Normalize underscores
    if cm == "read_write":
        cm = "read-write"
    if cm == "read_only":
        cm = "read-only"
    # execute/all are never allowed for default workers under oh-my-grok
    if cm in ("execute", "all"):
        return {
            "decision": "deny",
            "reason": spawn_deny_reason(st=st, cm=cm, kind="execute_all"),
        }
    required = required_capability_mode(st)
    if required is None:
        # Unknown type but mode present and is RW or RO → allow
        return {"decision": "allow"}
    if cm != required:
        return {
            "decision": "deny",
            "reason": spawn_deny_reason(st=st, cm=cm, kind="mismatch"),
        }
    return {"decision": "allow"}


def decide_pre_tool_use(event: dict[str, Any]) -> dict[str, str]:
    """Return Grok PreToolUse decision. Fail-safe: emit explicit allow/deny always."""
    try:
        tool = (event.get("toolName") or event.get("tool_name") or "").strip()
        # Claude alias
        if tool in ("Bash", "bash"):
            tool = "run_terminal_command"
        if tool in ("Task", "task"):
            tool = "spawn_subagent"
        if tool == "spawn_subagent":
            return decide_spawn_subagent(_tool_input(event))
        if tool not in ("run_terminal_command", "Shell"):
            return {"decision": "allow"}
        tin = _tool_input(event)
        cmd = tin.get("command") if isinstance(tin, dict) else None
        if not isinstance(cmd, str):
            return {"decision": "allow"}
        # ONLY process environment — never parse env from command string
        if os.environ.get("OMG_ALLOW_EXTERNAL_CLI") == "1":
            return {"decision": "allow"}
        if should_deny_command(cmd):
            return {
                "decision": "deny",
                "reason": (
                    "oh-my-grok: external agent CLI blocked "
                    "(use omg ask for advisors; set OMG_ALLOW_EXTERNAL_CLI only in omg ask child)"
                ),
            }
        return {"decision": "allow"}
    except Exception as e:
        # Explicit allow with reason logged — caller may still choose deny; fail-open is host policy
        return {"decision": "allow", "reason": f"omg-guard-error:{type(e).__name__}"}
