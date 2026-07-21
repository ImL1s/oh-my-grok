"""Team plane package: roles, executor providers, routing floors, tmux plane."""

from omg_cli.team.roles import (
    CANONICAL_ROLES,
    RoleMeta,
    UnknownRoleError,
    is_reviewer_or_verifier,
    normalize_role,
    role_class,
    role_meta,
    role_posture,
)
from omg_cli.team.routing import (
    DEFAULT_PROVIDER,
    STRUCTURED_VERDICT_PROVIDERS,
    ResolvedRouting,
    RoleRoute,
    RoutingError,
    resolve_routing,
)

__all__ = [
    "CANONICAL_ROLES",
    "DEFAULT_PROVIDER",
    "ResolvedRouting",
    "RoleMeta",
    "RoleRoute",
    "RoutingError",
    "STRUCTURED_VERDICT_PROVIDERS",
    "UnknownRoleError",
    "is_reviewer_or_verifier",
    "normalize_role",
    "resolve_routing",
    "role_class",
    "role_meta",
    "role_posture",
]
