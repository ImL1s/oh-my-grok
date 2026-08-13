"""Explicit presence detector for catalog-forbidden ask execution options.

Catalog verbs share the `omg ask` parser. Admission is token presence, not
comparison with argparse defaults (`--timeout 600` is still forbidden).
Unique prefixes are treated as the same option because the shared parser
keeps ``allow_abbrev=True`` for real provider invocations.
"""

from __future__ import annotations

from collections.abc import Sequence

# Option strings owned by the shared provider `omg ask` parser.
ASK_EXECUTION_OPTION_STRINGS: frozenset[str] = frozenset(
    {
        "--prompt-file",
        "--file",
        "--cwd",
        "--timeout",
        "--max-bytes",
        "--out",
        "--run",
        "--dry-run",
        "--model",
        "--extra",
        "--background",
        "--attempt-budget",
        "--role",
    }
)

_ASK_FLAG_OPTIONS: frozenset[str] = frozenset({"--dry-run", "--background"})
_GLOBAL_FLAG_OPTIONS: frozenset[str] = frozenset(
    {"--json", "--safe", "--yolo", "--help", "-h"}
)
_GLOBAL_VALUE_OPTIONS: frozenset[str] = frozenset({"--project-root"})

CATALOG_USAGE_CODE = "E_USAGE"
_CATALOG_VERBS: frozenset[str] = frozenset({"list-advisors", "explain"})


def _unique_prefix_match(name: str, known: frozenset[str]) -> str | None:
    if name in known:
        return name
    if not name.startswith("-") or name in {"-", "--"}:
        return None
    hits = [item for item in known if item.startswith(name)]
    if len(hits) == 1:
        return hits[0]
    return None


def match_execution_option(name: str) -> str | None:
    """Return the canonical execution option *name* uniquely prefixes, if any."""

    return _unique_prefix_match(name, ASK_EXECUTION_OPTION_STRINGS)


def catalog_forbidden_supplied(argv: Sequence[str]) -> tuple[str, ...]:
    """Return execution option strings and `--` extras found in *argv*."""

    found: list[str] = []
    for token in argv:
        if token == "--":
            found.append("--")
            continue
        if not token.startswith("--"):
            continue
        name = token.split("=", 1)[0]
        matched = match_execution_option(name)
        if matched is not None:
            found.append(name)
    return tuple(found)


def catalog_usage_message(supplied: Sequence[str]) -> str:
    if not supplied:
        return "invalid catalog usage"
    if supplied[0] == "--":
        return "unexpected '--' extras"
    return f"execution option {supplied[0]} is not allowed"


def argv_wants_json(argv: Sequence[str]) -> bool:
    for token in argv:
        if token == "--":
            break
        name = token.split("=", 1)[0]
        if _unique_prefix_match(name, frozenset({"--json"})) is not None:
            return True
    return False


def catalog_verb_from_argv(argv: Sequence[str]) -> str | None:
    """Return list-advisors/explain when that token is the ask provider verb."""

    seen_ask = False
    skip_value = False
    ended_options = False
    for token in argv:
        if skip_value:
            skip_value = False
            continue
        if not seen_ask:
            if token == "ask":
                seen_ask = True
            continue
        if ended_options:
            return token if token in _CATALOG_VERBS else None
        if token == "--":
            ended_options = True
            continue
        if token.startswith("-"):
            name = token.split("=", 1)[0]
            if _unique_prefix_match(name, _GLOBAL_FLAG_OPTIONS | _ASK_FLAG_OPTIONS):
                continue
            if "=" in token:
                continue
            if _unique_prefix_match(
                name,
                ASK_EXECUTION_OPTION_STRINGS | _GLOBAL_VALUE_OPTIONS,
            ):
                skip_value = True
            continue
        if token in _CATALOG_VERBS:
            return token
        return None
    return None


def _option_consumes_value(token: str) -> bool:
    """Whether *token* is an option that takes a following argv word."""

    if "=" in token:
        return False
    name = token.split("=", 1)[0]
    if _unique_prefix_match(name, _GLOBAL_FLAG_OPTIONS | _ASK_FLAG_OPTIONS):
        return False
    if _unique_prefix_match(
        name, ASK_EXECUTION_OPTION_STRINGS | _GLOBAL_VALUE_OPTIONS
    ):
        return True
    # Unknown long options: consume a following non-option word so
    # ``ask provider --mod x prompt`` keeps ``prompt`` positional after hoist.
    return name.startswith("--")


def normalize_ask_argv(argv: Sequence[str]) -> list[str]:
    """Hoist options that sit between ``ask`` positionals.

    CPython <3.12 rejects ``omg ask explain --json fable`` (and the same
    shape for provider prompts) with ``unrecognized arguments``. Move any
    options that appear after the provider positional to the end so argparse
    on 3.11–3.13 still binds prompt words.
    """

    raw = list(argv)
    try:
        ask_i = next(i for i, tok in enumerate(raw) if tok == "ask")
    except StopIteration:
        return raw

    head = raw[: ask_i + 1]
    rest = raw[ask_i + 1 :]
    if not rest:
        return raw

    if "--" in rest:
        dash = rest.index("--")
        body, tail = rest[:dash], rest[dash:]
    else:
        body, tail = rest, []

    def _take_option(tokens: list[str], index: int) -> tuple[list[str], int]:
        tok = tokens[index]
        taken = [tok]
        nxt = index + 1
        if _option_consumes_value(tok) and nxt < len(tokens):
            nxt_tok = tokens[nxt]
            if not (nxt_tok.startswith("-") and nxt_tok not in {"-", "--"}):
                taken.append(nxt_tok)
                nxt += 1
        return taken, nxt

    leading: list[str] = []
    i = 0
    while i < len(body):
        tok = body[i]
        if tok.startswith("-") and tok not in {"-", "--"}:
            chunk, i = _take_option(body, i)
            leading.extend(chunk)
            continue
        break
    body = body[i:]
    if not body:
        return head + leading + tail

    provider = body[0]
    rem = body[1:]
    positionals: list[str] = []
    trailing: list[str] = []
    i = 0
    while i < len(rem):
        tok = rem[i]
        if tok.startswith("-") and tok not in {"-", "--"}:
            chunk, i = _take_option(rem, i)
            trailing.extend(chunk)
            continue
        positionals.append(tok)
        i += 1
    return head + leading + [provider] + positionals + trailing + tail


__all__ = [
    "ASK_EXECUTION_OPTION_STRINGS",
    "CATALOG_USAGE_CODE",
    "argv_wants_json",
    "catalog_forbidden_supplied",
    "catalog_usage_message",
    "catalog_verb_from_argv",
    "match_execution_option",
    "normalize_ask_argv",
]
