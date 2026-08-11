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


def normalize_team_argv(argv: Sequence[str]) -> list[str]:
    """Rewrite OMX-like shorthand into ``team launch …``; else return a copy.

    Only rewrites when the first token is ``team`` and the second token is
    either a worker spec (``3`` / ``3:executor``) or a bare goal string
    (not a reserved action / flag).
    """
    raw = list(argv)
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
