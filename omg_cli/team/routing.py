"""Per-role provider routing for multi-CLI team panes (D3).

Resolves a role→{provider, model?} config **once** at team start into an
immutable snapshot. Security floors (fail-closed):

1. **FLOOR 1** — reviewer/verifier roles route only to structured-verdict
   providers ``{grok, codex, claude, gemini}``. ``cursor`` is **excluded**
   (no structured-verdict mode). Rejection is hard — never silent downgrade.
2. **FLOOR 2** — :class:`~omg_cli.team.roles.UnknownRoleError` **propagates**
   (never swallowed). An unrecognized role name rejects the whole config.
3. **FLOOR 3** — posture is derived from role via
   :func:`~omg_cli.team.roles.role_posture` only; routing never assigns write
   posture to a reviewer role.

Loud fallback: if a resolved provider's binary is absent, fall back to
``grok`` for that role, emit a **visible** warning (stderr + snapshot), and
keep the same posture. Silent fallback is a failure.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Callable, Collection, Mapping, Sequence

from omg_cli.team.providers import (
    EXECUTOR_PROVIDERS,
    EXECUTOR_SPECS,
    TeamProviderError,
    TeamProviderMissing,
    normalize_executor_provider,
    resolve_executor_binary,
)
from omg_cli.team.roles import (
    UnknownRoleError,
    is_reviewer_or_verifier,
    normalize_role,
    role_posture,
)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

DEFAULT_PROVIDER = "grok"

# Structured-verdict capable providers (FLOOR 1). cursor is intentionally
# excluded — mirrors OMC (no structured-verdict mode).
STRUCTURED_VERDICT_PROVIDERS: frozenset[str] = frozenset(
    {"grok", "codex", "claude", "gemini"}
)

WarnFn = Callable[[str], None]


class RoutingError(ValueError):
    """Fail-closed routing / FLOOR rejection (maps to team start refusal)."""


@dataclass(frozen=True, slots=True)
class RoleRoute:
    """Immutable per-role resolution after floors + binary check."""

    role: str
    provider: str
    model: str | None
    posture: str  # from role_posture only
    needs_pty: bool
    fallback_from: str | None = None
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "posture": self.posture,
            "needs_pty": self.needs_pty,
            "fallback_from": self.fallback_from,
            "warning": self.warning,
        }


@dataclass(frozen=True, slots=True)
class ResolvedRouting:
    """Immutable routing snapshot written into team.json."""

    default_provider: str
    by_role: Mapping[str, RoleRoute]
    warnings: tuple[str, ...]

    def for_role(self, role: str) -> RoleRoute:
        """Return the route for *role* (fail-closed on unknown role).

        Unknown roles raise :class:`UnknownRoleError` (FLOOR 2) — never
        invent a default for an unregistered role name.
        """
        key = normalize_role(role)
        # Validate membership first so unknowns fail closed.
        posture = role_posture(key)  # noqa: F841 — may raise UnknownRoleError
        if key in self.by_role:
            return self.by_role[key]
        # Role is known but not in snapshot — synthesize default (same floors).
        # Callers that used resolve_routing with roles_needed should not hit this.
        return _route_for_role(
            key,
            provider=self.default_provider,
            model=None,
            available_providers=None,
            check_binary=False,
            warn=None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_provider": self.default_provider,
            "by_role": {k: v.to_dict() for k, v in sorted(self.by_role.items())},
            "warnings": list(self.warnings),
        }


def _default_warn(msg: str) -> None:
    print(f"omg team routing WARNING: {msg}", file=sys.stderr)


def _provider_available(
    provider: str,
    available_providers: Collection[str] | None,
) -> bool:
    if available_providers is not None:
        return provider in available_providers
    try:
        resolve_executor_binary(provider)
        return True
    except TeamProviderMissing:
        return False
    except TeamProviderError:
        return False


def _route_for_role(
    role: str,
    *,
    provider: str,
    model: str | None,
    available_providers: Collection[str] | None,
    check_binary: bool,
    warn: WarnFn | None,
) -> RoleRoute:
    """Build one RoleRoute after FLOOR 1/2/3 + optional loud fallback.

    FLOOR 2: role_posture / is_reviewer_or_verifier raise UnknownRoleError.
    """
    key = normalize_role(role)
    # FLOOR 2 + FLOOR 3: posture from role registry only (UnknownRoleError propagates).
    posture = role_posture(key)
    is_rv = is_reviewer_or_verifier(key)

    try:
        canon = normalize_executor_provider(provider)
    except TeamProviderError as exc:
        raise RoutingError(str(exc)) from exc

    # FLOOR 1 — structured-verdict only for reviewer/verifier.
    if is_rv and canon not in STRUCTURED_VERDICT_PROVIDERS:
        raise RoutingError(
            f"FLOOR 1: reviewer/verifier role {key!r} cannot route to "
            f"provider {canon!r}; structured-verdict providers are "
            f"{sorted(STRUCTURED_VERDICT_PROVIDERS)} "
            f"(cursor is excluded — no structured-verdict mode)"
        )

    # claude is structured-verdict capable but not an executor pane provider.
    if canon not in EXECUTOR_PROVIDERS:
        raise RoutingError(
            f"provider {canon!r} is not an executor pane provider; "
            f"expected one of: {', '.join(sorted(EXECUTOR_PROVIDERS))}"
        )

    final = canon
    fallback_from: str | None = None
    warning: str | None = None

    if check_binary and not _provider_available(canon, available_providers):
        if canon == DEFAULT_PROVIDER:
            raise RoutingError(
                f"default executor binary not found on PATH: {DEFAULT_PROVIDER!r}"
            )
        # Loud fallback — never silent.
        warning = (
            f"provider {canon!r} binary absent for role {key!r}; "
            f"falling back to {DEFAULT_PROVIDER!r} (posture={posture} unchanged)"
        )
        if warn is not None:
            warn(warning)
        else:
            _default_warn(warning)
        final = DEFAULT_PROVIDER
        fallback_from = canon
        if check_binary and not _provider_available(final, available_providers):
            raise RoutingError(
                f"fallback provider {final!r} binary also absent on PATH"
            )

    spec = EXECUTOR_SPECS[final]
    return RoleRoute(
        role=key,
        provider=final,
        model=model,
        posture=posture,
        needs_pty=bool(spec.needs_pty),
        fallback_from=fallback_from,
        warning=warning,
    )


def _parse_entry(raw: Any, *, key: str) -> tuple[str, str | None]:
    if not isinstance(raw, Mapping):
        raise RoutingError(
            f"routing[{key!r}] must be an object with 'provider' "
            f"(got {type(raw).__name__})"
        )
    prov = raw.get("provider")
    if prov is None or (isinstance(prov, str) and not prov.strip()):
        raise RoutingError(f"routing[{key!r}].provider is required")
    if not isinstance(prov, str):
        raise RoutingError(f"routing[{key!r}].provider must be a string")
    model = raw.get("model")
    if model is not None and not isinstance(model, str):
        raise RoutingError(f"routing[{key!r}].model must be a string or null")
    model_s = model.strip() if isinstance(model, str) and model.strip() else None
    return prov.strip(), model_s


def resolve_routing(
    config: Mapping[str, Any] | None,
    *,
    roles_needed: Sequence[str] | None = None,
    available_providers: Collection[str] | None = None,
    default_provider: str = DEFAULT_PROVIDER,
    check_binary: bool = True,
    warn: WarnFn | None = None,
) -> ResolvedRouting:
    """Resolve role→{provider, model?} once into an immutable snapshot.

    Parameters
    ----------
    config:
        Optional mapping of **role name** → ``{"provider": str, "model"?: str}``.
        Every key is validated as a canonical role (FLOOR 2).
    roles_needed:
        Roles that will appear on tasks (defaults applied when absent from
        *config*). Each is validated (UnknownRoleError propagates).
    available_providers:
        Optional set of providers treated as present (hermetic tests). When
        ``None``, PATH is probed via :func:`resolve_executor_binary`.
    default_provider:
        Provider used when a needed role has no config entry (default ``grok``).
    check_binary:
        When True, missing binaries trigger loud fallback to grok.
    warn:
        Warning sink (default: stderr). Called for each loud fallback.
    """
    try:
        default_canon = normalize_executor_provider(default_provider)
    except TeamProviderError as exc:
        raise RoutingError(str(exc)) from exc

    warn_fn = warn if warn is not None else _default_warn
    by_role: dict[str, RoleRoute] = {}
    warnings: list[str] = []

    cfg = dict(config) if config else {}

    # 1) Explicit config entries — every key is a role (FLOOR 2 propagates).
    for raw_key, raw_val in cfg.items():
        if not isinstance(raw_key, str):
            raise RoutingError(
                f"routing keys must be role name strings (got {type(raw_key).__name__})"
            )
        # FLOOR 2: do NOT catch UnknownRoleError.
        key = normalize_role(raw_key)
        role_posture(key)  # raises UnknownRoleError if unknown
        prov, model = _parse_entry(raw_val, key=raw_key)
        route = _route_for_role(
            key,
            provider=prov,
            model=model,
            available_providers=available_providers,
            check_binary=check_binary,
            warn=warn_fn,
        )
        if route.warning:
            warnings.append(route.warning)
        by_role[key] = route

    # 2) Ensure every needed role has an entry (default provider).
    needed: list[str] = []
    for r in roles_needed or ():
        # FLOOR 2 propagates for unknown needed roles too.
        needed.append(normalize_role(r))
        role_posture(needed[-1])

    for key in needed:
        if key in by_role:
            continue
        route = _route_for_role(
            key,
            provider=default_canon,
            model=None,
            available_providers=available_providers,
            check_binary=check_binary,
            warn=warn_fn,
        )
        if route.warning:
            warnings.append(route.warning)
        by_role[key] = route

    # Freeze mapping
    frozen_roles: Mapping[str, RoleRoute] = {
        k: by_role[k] for k in sorted(by_role)
    }
    return ResolvedRouting(
        default_provider=default_canon,
        by_role=frozen_roles,
        warnings=tuple(warnings),
    )


def parse_routing_json(raw: str | Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Parse CLI / API routing input into a plain dict (or None)."""
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    import json

    text = str(raw).strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RoutingError(f"--routing is not valid JSON: {exc}") from exc
    if not isinstance(data, Mapping):
        raise RoutingError("--routing must be a JSON object (role → {provider, model?})")
    return dict(data)


__all__ = [
    "DEFAULT_PROVIDER",
    "STRUCTURED_VERDICT_PROVIDERS",
    "ResolvedRouting",
    "RoleRoute",
    "RoutingError",
    "parse_routing_json",
    "resolve_routing",
]
