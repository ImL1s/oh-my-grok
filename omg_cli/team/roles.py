"""Canonical team role taxonomy for per-role routing floors (D3).

Maps every OMG agent role (existing + agent-role-parity set) to:

- ``posture``: ``"read-only"`` | ``"read-write"`` — CLI / capability_mode floor
- ``role_class``: ``"reviewer"`` | ``"verifier"`` | ``"executor"`` | ``"planner"``
  | ``"orchestrator"``

Reviewer / verifier roles must route only to structured-verdict providers;
read-only roles get a read-only CLI posture. Unknown roles fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping


Posture = str  # "read-only" | "read-write"
RoleClass = str  # "reviewer" | "verifier" | "executor" | "planner" | "orchestrator"


@dataclass(frozen=True, slots=True)
class RoleMeta:
    """Machine-readable metadata for one canonical team role."""

    posture: Posture
    role_class: RoleClass


# Canonical short names (without the ``omg-`` agent prefix).
_ROLES: Final[dict[str, RoleMeta]] = {
    # read-only + reviewer
    "code-reviewer": RoleMeta(posture="read-only", role_class="reviewer"),
    "critic": RoleMeta(posture="read-only", role_class="reviewer"),
    "security-reviewer": RoleMeta(posture="read-only", role_class="reviewer"),
    # read-only + verifier
    "verifier": RoleMeta(posture="read-only", role_class="verifier"),
    # read-only + planner / analyst / architect
    "analyst": RoleMeta(posture="read-only", role_class="planner"),
    "architect": RoleMeta(posture="read-only", role_class="planner"),
    "planner": RoleMeta(posture="read-only", role_class="planner"),
    # read-write + executor
    "executor": RoleMeta(posture="read-write", role_class="executor"),
    "debugger": RoleMeta(posture="read-write", role_class="executor"),
    "designer": RoleMeta(posture="read-write", role_class="executor"),
    "writer": RoleMeta(posture="read-write", role_class="executor"),
    "test-engineer": RoleMeta(posture="read-write", role_class="executor"),
    "qa-tester": RoleMeta(posture="read-write", role_class="executor"),
    # orchestrator (pinned)
    "orchestrator": RoleMeta(posture="read-write", role_class="orchestrator"),
}

CANONICAL_ROLES: Final[frozenset[str]] = frozenset(_ROLES)


class UnknownRoleError(KeyError):
    """Raised when a role name is not in the canonical taxonomy (fail-closed)."""

    def __init__(self, role: str) -> None:
        self.role = role
        super().__init__(
            f"unknown team role {role!r}; "
            f"expected one of: {', '.join(sorted(CANONICAL_ROLES))}"
        )


def normalize_role(role: str) -> str:
    """Normalize agent or short names to a canonical short role id.

    Accepts ``executor``, ``omg-executor``, ``OMG-Executor`` → ``executor``.
    Does not validate membership; use :func:`role_meta` for fail-closed lookup.
    """
    name = (role or "").strip().lower().replace("_", "-")
    if name.startswith("omg-"):
        name = name[4:]
    return name


def role_meta(role: str) -> RoleMeta:
    """Return metadata for *role*; raise :class:`UnknownRoleError` if unknown."""
    key = normalize_role(role)
    if not key or key not in _ROLES:
        raise UnknownRoleError(role if role is not None else "")
    return _ROLES[key]


def role_posture(role: str) -> str:
    """Return ``\"read-only\"`` or ``\"read-write\"`` for *role* (fail-closed)."""
    return role_meta(role).posture


def role_class(role: str) -> str:
    """Return role class string for *role* (fail-closed)."""
    return role_meta(role).role_class


def is_reviewer_or_verifier(role: str) -> bool:
    """True when *role* is a structured-verdict reviewer or verifier floor.

    Unknown roles raise :class:`UnknownRoleError` (fail-closed) rather than
    returning False — callers must not treat unknowns as non-reviewer.
    """
    cls = role_class(role)
    return cls in ("reviewer", "verifier")


def all_role_metadata() -> Mapping[str, RoleMeta]:
    """Read-only view of the full taxonomy (for tests / D3 tables)."""
    return _ROLES
