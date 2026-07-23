"""Team plane package: roles, executor providers, routing floors, tmux plane."""

from omg_cli.team.roles import (
    CANONICAL_ROLES,
    RoleMeta,
    UnknownRoleError,
    is_reviewer_or_verifier,
    native_subagent_type,
    normalize_role,
    role_class,
    role_meta,
    role_posture,
    required_capability_mode,
)
from omg_cli.team.routing import (
    DEFAULT_PROVIDER,
    STRUCTURED_VERDICT_PROVIDERS,
    ResolvedRouting,
    NativeRoleRoute,
    RoleRoute,
    RoutingError,
    resolve_routing,
    resolve_native_routing,
)

__all__ = [
    "CANONICAL_ROLES",
    "DEFAULT_PROVIDER",
    "ResolvedRouting",
    "NativeRoleRoute",
    "RoleMeta",
    "RoleRoute",
    "RoutingError",
    "STRUCTURED_VERDICT_PROVIDERS",
    "UnknownRoleError",
    "is_reviewer_or_verifier",
    "native_subagent_type",
    "normalize_role",
    "resolve_routing",
    "resolve_native_routing",
    "role_class",
    "role_meta",
    "role_posture",
    "required_capability_mode",
]
