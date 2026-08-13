"""User-facing ``omg team`` argv grammar (OMX-like shorthand).

Normalizes::

    team 3:executor "fix flaky tests"
      → team launch --workers 3 --role executor --goal "fix flaky tests"

    team "fix flaky tests"
      → team launch --workers 3 --role executor --goal "fix flaky tests"

Legacy verbose actions (``start|run|…|api``) pass through unchanged.
"""

from __future__ import annotations

import re
from typing import Sequence

from omg_cli.team.roles import UnknownRoleError, normalize_role, role_meta

RESERVED_ACTIONS: frozenset[str] = frozenset(
    {
        "start",
        "run",
        "scale",
        "resume",
        "status",
        "collect",
        "stop",
        "shutdown",
        "api",
        "launch",
        "worker-ready",
        "supervisor",
        "panes",
        "capture",
        "focus",
        "key",
        "input",
        "watch",
        "view",
        "hyperplan",
        "security-research",
        "help",
        "-h",
        "--help",
    }
)

_SPEC_RE = re.compile(r"^(\d+)(?::([A-Za-z0-9_-]+))?$")
DEFAULT_WORKERS = 3
DEFAULT_ROLE = "executor"


class TeamCliError(ValueError):
    """User-facing team argv / grammar failure (exit 2)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.exit_code = 2


def parse_worker_spec(token: str) -> tuple[int, str]:
    """Parse ``N`` or ``N:role`` into ``(workers, role)``."""
    match = _SPEC_RE.fullmatch((token or "").strip())
    if match is None:
        raise TeamCliError(
            f"invalid worker spec {token!r}; expected N or N:role "
            f"(example: 3:executor)"
        )
    count = int(match.group(1))
    if count < 1:
        raise TeamCliError(f"worker count must be >= 1 (got {count})")
    role_raw = match.group(2) or DEFAULT_ROLE
    role = normalize_role(role_raw)
    try:
        role_meta(role)
    except UnknownRoleError as exc:
        raise TeamCliError(str(exc)) from exc
    return count, role


def _is_flag(token: str) -> bool:
    return token.startswith("-")


# Root parser globals that may precede ``team`` (see omg_cli.main.build_parser).
# Flags are arity 0; --project-root takes one PATH (or --project-root=PATH).
# Do not add unknown options here — unknown tokens stay in the remainder so
# argparse / host-launch keep their existing behavior.
_LEADING_FLAG_OPTS: frozenset[str] = frozenset({"--json", "--safe", "--yolo"})
_LEADING_VALUE_OPTS: frozenset[str] = frozenset({"--project-root"})
_LEADING_VALUE_EQ_PREFIXES: tuple[str, ...] = ("--project-root=",)


def split_supported_leading_globals(
    argv: Sequence[str],
) -> tuple[list[str], list[str]]:
    """Peel supported leading globals; stop at the first non-global token.

    Respects option arity. Incomplete ``--project-root`` (no value) is left
    in the remainder for argparse. Does not scan arbitrary payloads.
    """
    raw = list(argv)
    i = 0
    n = len(raw)
    while i < n:
        tok = raw[i]
        if tok in _LEADING_FLAG_OPTS:
            i += 1
            continue
        if tok in _LEADING_VALUE_OPTS:
            if i + 1 >= n:
                break
            i += 2
            continue
        if any(tok.startswith(p) for p in _LEADING_VALUE_EQ_PREFIXES):
            i += 1
            continue
        break
    return raw[:i], raw[i:]


def _normalize_team_tail(raw: list[str]) -> list[str]:
    """Rewrite a argv whose first token is already ``team``."""
    if not raw or raw[0] != "team":
        return raw
    if len(raw) == 1:
        return raw

    second = raw[1]
    if second in RESERVED_ACTIONS or _is_flag(second):
        # ``shutdown`` is an OMX alias — rewrite to ``stop`` for argparse.
        if second == "shutdown":
            return ["team", "stop", *raw[2:]]
        return raw

    # Form A: team N[:role] <goal> [flags…]
    if _SPEC_RE.fullmatch(second):
        workers, role = parse_worker_spec(second)
        rest = list(raw[2:])
        if not rest or _is_flag(rest[0]):
            raise TeamCliError(
                'omg team shorthand requires a goal string, e.g. '
                'omg team 3:executor "fix flaky tests"'
            )
        goal = rest[0]
        if goal in RESERVED_ACTIONS:
            raise TeamCliError(
                f"goal must not be a reserved team action ({goal!r})"
            )
        flags = rest[1:]
        return [
            "team",
            "launch",
            "--workers",
            str(workers),
            "--role",
            role,
            "--goal",
            goal,
            *flags,
        ]

    # Form B: team "<goal>" [flags…]  → default 3:executor
    goal = second
    flags = list(raw[2:])
    return [
        "team",
        "launch",
        "--workers",
        str(DEFAULT_WORKERS),
        "--role",
        DEFAULT_ROLE,
        "--goal",
        goal,
        *flags,
    ]


def normalize_team_argv(argv: Sequence[str]) -> list[str]:
    """Rewrite OMX-like shorthand into ``team launch …``; else return a copy.

    Finds ``team`` after supported leading globals (``--project-root PATH``,
    ``--json``, ``--safe``, ``--yolo``) and rewrites Form A/B before argparse.
    Only rewrites when the team token's next token is a worker spec
    (``3`` / ``3:executor``) or a bare goal string (not a reserved action /
    flag). Unknown leading tokens are not skipped.
    """
    prefix, rest = split_supported_leading_globals(argv)
    if not rest or rest[0] != "team":
        return list(argv)
    return prefix + _normalize_team_tail(rest)
