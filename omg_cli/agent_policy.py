"""Dual-host agent/model policy resolution (#131).

Consumes the #71 read-only catalog (``agents/catalog.json``). This module is a
policy overlay, not a second plugin-agent registry.

Stock original Grok Build uses explicit ``inherit`` (or exact /
``requires_capability`` when declared). Medley extensions are never flattened
to their first catalog id when unsupported. Exact never silently becomes the
parent model.

No credentials, endpoints, account ids, or mutable readiness facts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from omg_cli.agents_catalog import (
    ALLOWED_CAPABILITY_MODES,
    FORBIDDEN_CAPABILITY_MODES,
    AgentsCatalogError,
    load_agents_catalog,
    plugin_root,
)
from omg_cli.host_capabilities import (
    CAPABILITY_IDS,
    HOST_TIER_GROK,
    HostCapabilitySnapshot,
    medley_capability_outcome,
    route_specific_facts_state,
    stock_grok_snapshot,
)

POLICY_SCHEMA = "omg-agent-model-policy/v1"
OVERRIDE_SCHEMA = "omg-agent-model-policy-override/v1"
VIEW_SCHEMA = "omg.agent_policy_view/v1"
POLICY_RELATIVE = "agents/model_policies.json"
PROJECT_OVERRIDE_RELATIVE = ".omg/agent-policies.json"
USER_OVERRIDE_RELATIVE = ".omg/agent-policies.json"

PROMPT_PROFILES: frozenset[str] = frozenset(
    {"claude-family", "gpt-family", "gemini-family", "generic"}
)
BASELINE_MODES: frozenset[str] = frozenset(
    {"exact", "inherit", "requires_capability"}
)
ROUTE_KIND_NATIVE = "native"
ROUTE_KIND_EXTERNAL = "external_executor"
POLICY_ROUTE_KINDS: frozenset[str] = frozenset(
    {ROUTE_KIND_NATIVE, ROUTE_KIND_EXTERNAL}
)
EXTERNAL_EXECUTORS: frozenset[str] = frozenset(
    {"grok", "codex", "agy", "cursor", "gemini"}
)

ORDERED_CANDIDATES_CAP = "medley.native-ordered-candidates.v1"
EXACT_MODEL_CAP = "host.native-exact-model.v1"
INHERIT_MODEL_CAP = "host.native-inherit-model.v1"

_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "private_key",
        "access_token",
        "endpoint",
        "endpoints",
        "credentials",
        "oauth",
        "account_id",
    }
)
_SECRET_VALUE_NEEDLES: tuple[str, ...] = (
    "sk-",
    "bearer ",
    "acct_",
    "-----begin ",
)

_NATIVE_ONLY_KEYS: frozenset[str] = frozenset(
    {
        "catalog",
        "catalog_id",
        "receipt",
        "receipt_digest",
        "access_profile",
        "readiness",
        "candidates",
    }
)


class AgentPolicyError(ValueError):
    """Fail-closed policy load / resolution error."""

    def __init__(self, message: str, *, code: str = "E_AGENT_POLICY") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PolicyReason:
    code: str
    message: str
    next_action: str

    def to_json(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "next_action": self.next_action,
        }


@dataclass(frozen=True, slots=True)
class AgentPolicyViewV1:
    """Secret-free typed projection shared by CLI/JSON/doctor (#131/#134)."""

    agent_id: str
    aliases: tuple[str, ...]
    category: str | None
    tier: str | None
    capability_floor: str
    tool_floor: tuple[str, ...]
    policy_id: str
    policy_digest: str
    policy_source: str
    baseline_mode: str
    baseline_model: str | None
    requested_extension: str | None
    candidate_ids: tuple[str, ...]
    prompt_profile: str
    reasoning_preference: str | None
    host_capabilities: tuple[dict[str, Any], ...]
    selected_model_ref: str | None
    route_kind: str
    route_receipt_digest: str | None
    attempt: int | None
    status: str
    reasons: tuple[PolicyReason, ...]
    host_facts: dict[str, str]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": VIEW_SCHEMA,
            "agent_id": self.agent_id,
            "aliases": list(self.aliases),
            "category": self.category,
            "tier": self.tier,
            "capability_floor": self.capability_floor,
            "tool_floor": list(self.tool_floor),
            "policy_id": self.policy_id,
            "policy_digest": self.policy_digest,
            "policy_source": self.policy_source,
            "baseline_mode": self.baseline_mode,
            "baseline_model": self.baseline_model,
            "requested_extension": self.requested_extension,
            "candidate_ids": list(self.candidate_ids),
            "prompt_profile": self.prompt_profile,
            "reasoning_preference": self.reasoning_preference,
            "host_capabilities": list(self.host_capabilities),
            "selected_model_ref": self.selected_model_ref,
            "route_kind": self.route_kind,
            "route_receipt_digest": self.route_receipt_digest,
            "attempt": self.attempt,
            "status": self.status,
            "reasons": [item.to_json() for item in self.reasons],
            "host_facts": dict(self.host_facts),
            "requested_policy": {"binding": self.baseline_mode},
        }


@dataclass(frozen=True, slots=True)
class NativeAgentRoute:
    kind: str
    policy_id: str
    baseline_mode: str
    model_ref: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": ROUTE_KIND_NATIVE,
            "policy_id": self.policy_id,
            "baseline_mode": self.baseline_mode,
            "model_ref": self.model_ref,
        }


@dataclass(frozen=True, slots=True)
class ExternalExecutorRoute:
    kind: str
    executor: str
    model_flag: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": ROUTE_KIND_EXTERNAL,
            "executor": self.executor,
            "model_flag": self.model_flag,
        }


def policy_overlay_path(root: Path | None = None) -> Path:
    return (root if root is not None else plugin_root()) / POLICY_RELATIVE


def _require_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentPolicyError(f"{label} must be a JSON object")
    return value


def _require_str(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentPolicyError(f"{label} must be a non-empty string")
    return value.strip()


def _scan_secrets(value: Any, *, label: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_l = str(key).lower()
            if key_l in {"note", "kind"}:
                continue
            normalized = key_l.replace("-", "_")
            if normalized in _SECRET_KEYS or key_l in _SECRET_KEYS:
                raise AgentPolicyError(
                    f"{label} contains forbidden key {key!r}",
                    code="E_AGENT_POLICY_SECRET",
                )
            _scan_secrets(item, label=f"{label}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_secrets(item, label=f"{label}[{index}]")
        return
    if isinstance(value, str):
        lowered = value.lower()
        for needle in _SECRET_VALUE_NEEDLES:
            if needle in lowered:
                raise AgentPolicyError(
                    f"{label} contains forbidden sentinel {needle!r}",
                    code="E_AGENT_POLICY_SECRET",
                )


def _load_json(path: Path, *, label: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentPolicyError(f"cannot read {label}: {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentPolicyError(f"{label} is not valid JSON: {path}: {exc}") from exc


def _parse_baseline(raw: Any, *, label: str) -> dict[str, Any]:
    obj = _require_object(raw, label=label)
    mode = _require_str(obj.get("mode"), label=f"{label}.mode")
    if mode not in BASELINE_MODES:
        raise AgentPolicyError(
            f"{label}.mode must be one of {sorted(BASELINE_MODES)}, got {mode!r}"
        )
    model = obj.get("model")
    models = obj.get("models")
    if model is not None and models is not None:
        raise AgentPolicyError(
            f"{label}: model and models cannot both be set",
            code="E_AGENT_POLICY_CONFLICT",
        )
    if models is not None:
        raise AgentPolicyError(
            f"{label}.models is not valid on a baseline (use inherit/exact model)",
            code="E_AGENT_POLICY_CONFLICT",
        )
    model_s: str | None = None
    if model is not None:
        model_s = _require_str(model, label=f"{label}.model")
    if mode == "exact" and not model_s:
        raise AgentPolicyError(f"{label}: exact baseline requires model")
    extra = sorted(set(obj) - {"mode", "model", "models"})
    if extra:
        raise AgentPolicyError(f"{label} has unknown keys: {', '.join(extra)}")
    return {"mode": mode, "model": model_s}


def _parse_candidates(raw: Any, *, label: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list) or not raw:
        raise AgentPolicyError(f"{label} must be a non-empty array")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        obj = _require_object(item, label=f"{label}[{index}]")
        catalog = _require_str(obj.get("catalog"), label=f"{label}[{index}].catalog")
        if catalog in seen:
            raise AgentPolicyError(f"{label}: duplicate catalog {catalog!r}")
        seen.add(catalog)
        profile = obj.get("prompt_profile")
        profile_s = (
            _require_str(profile, label=f"{label}[{index}].prompt_profile")
            if profile is not None
            else "generic"
        )
        if profile_s not in PROMPT_PROFILES:
            raise AgentPolicyError(
                f"{label}[{index}].prompt_profile {profile_s!r} is not a known family"
            )
        reasoning = obj.get("reasoning")
        reasoning_s = (
            _require_str(reasoning, label=f"{label}[{index}].reasoning")
            if reasoning is not None
            else None
        )
        extra = sorted(set(obj) - {"catalog", "prompt_profile", "reasoning"})
        if extra:
            raise AgentPolicyError(
                f"{label}[{index}] has unknown keys: {', '.join(extra)}"
            )
        out.append(
            {
                "catalog": catalog,
                "prompt_profile": profile_s,
                "reasoning": reasoning_s,
            }
        )
    return tuple(out)


def _parse_extensions(raw: Any, *, label: str) -> dict[str, Any]:
    if raw is None:
        return {}
    obj = _require_object(raw, label=label)
    extra = sorted(set(obj) - set(CAPABILITY_IDS))
    if extra:
        raise AgentPolicyError(f"{label} has unknown capability ids: {', '.join(extra)}")
    parsed: dict[str, Any] = {}
    for cap_id, body in obj.items():
        cap_label = f"{label}.{cap_id}"
        cap_obj = _require_object(body, label=cap_label)
        if cap_id == ORDERED_CANDIDATES_CAP:
            candidates = _parse_candidates(
                cap_obj.get("candidates"), label=f"{cap_label}.candidates"
            )
            requirements = cap_obj.get("requirements")
            if requirements is not None:
                _require_object(requirements, label=f"{cap_label}.requirements")
            extra_keys = sorted(set(cap_obj) - {"candidates", "requirements"})
            if extra_keys:
                raise AgentPolicyError(
                    f"{cap_label} has unknown keys: {', '.join(extra_keys)}"
                )
            parsed[cap_id] = {
                "candidates": [dict(item) for item in candidates],
                "requirements": (
                    dict(requirements) if isinstance(requirements, dict) else {}
                ),
            }
        else:
            parsed[cap_id] = dict(cap_obj)
    return parsed


def _parse_model_policy(raw: Any, *, label: str) -> dict[str, Any]:
    obj = _require_object(raw, label=label)
    policy_id = _require_str(obj.get("policy_id"), label=f"{label}.policy_id")
    baseline = _parse_baseline(obj.get("baseline"), label=f"{label}.baseline")
    profile = _require_str(
        obj.get("prompt_profile", "generic"), label=f"{label}.prompt_profile"
    )
    if profile not in PROMPT_PROFILES:
        raise AgentPolicyError(
            f"{label}.prompt_profile {profile!r} is not a known family"
        )
    reasoning = obj.get("reasoning")
    reasoning_s = (
        _require_str(reasoning, label=f"{label}.reasoning")
        if reasoning is not None
        else None
    )
    extensions = _parse_extensions(obj.get("extensions"), label=f"{label}.extensions")
    extra = sorted(
        set(obj) - {"policy_id", "baseline", "prompt_profile", "reasoning", "extensions"}
    )
    if extra:
        raise AgentPolicyError(f"{label} has unknown keys: {', '.join(extra)}")
    return {
        "policy_id": policy_id,
        "baseline": baseline,
        "prompt_profile": profile,
        "reasoning": reasoning_s,
        "extensions": extensions,
    }


@dataclass(frozen=True, slots=True)
class PolicyIdentity:
    agent_id: str
    aliases: tuple[str, ...]
    category: str | None
    tier: str | None
    capability_mode: str
    spawn_policy: str
    is_catalog_agent: bool
    policy: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PolicyBundle:
    schema: str
    prompt_profile_version: str
    identities: tuple[PolicyIdentity, ...]

    def by_id(self) -> dict[str, PolicyIdentity]:
        return {item.agent_id: item for item in self.identities}

    def resolve_name(self, name: str) -> PolicyIdentity:
        token = name.strip()
        if not token:
            raise AgentPolicyError(
                "agent/profile id must be non-empty", code="E_AGENT_NOT_FOUND"
            )
        lowered = token.lower()
        by_id = self.by_id()
        if lowered in by_id:
            return by_id[lowered]
        for identity in self.identities:
            if lowered in {alias.lower() for alias in identity.aliases}:
                return identity
        raise AgentPolicyError(
            f"unknown agent or profile {name!r}",
            code="E_AGENT_NOT_FOUND",
        )


def load_policy_bundle(root: Path | None = None) -> PolicyBundle:
    """Load #71 catalog plus the policy overlay. Fail-closed."""
    base = Path(root) if root is not None else plugin_root()
    try:
        catalog = load_agents_catalog(base, require_projections=False)
    except AgentsCatalogError as exc:
        raise AgentPolicyError(f"agent catalog: {exc}") from exc
    path = policy_overlay_path(base)
    if not path.is_file():
        raise AgentPolicyError(f"missing policy overlay: {POLICY_RELATIVE}")
    raw = _require_object(_load_json(path, label="policy overlay"), label="overlay")
    _scan_secrets(raw, label="overlay")
    schema = _require_str(raw.get("schema"), label="overlay.schema")
    if schema != POLICY_SCHEMA:
        raise AgentPolicyError(f"unsupported policy schema {schema!r}")
    profile_version = _require_str(
        raw.get("prompt_profile_version"), label="overlay.prompt_profile_version"
    )
    agents_raw = _require_object(raw.get("agents"), label="overlay.agents")
    catalog_ids = {record.id for record in catalog.agents}
    overlay_ids = set(agents_raw)
    missing = sorted(catalog_ids - overlay_ids)
    extra = sorted(overlay_ids - catalog_ids)
    if missing:
        raise AgentPolicyError(
            "policy overlay missing catalog agent(s): " + ", ".join(missing)
        )
    if extra:
        raise AgentPolicyError(
            "policy overlay has uncatalogued agent(s): " + ", ".join(extra)
        )
    identities: list[PolicyIdentity] = []
    seen_aliases: dict[str, str] = {}
    for record in catalog.agents:
        policy = _parse_model_policy(
            agents_raw[record.id], label=f"agents.{record.id}"
        )
        short = record.id[4:] if record.id.startswith("omg-") else record.id
        aliases = (record.id, short)
        for alias in aliases:
            key = alias.lower()
            if key in seen_aliases and seen_aliases[key] != record.id:
                raise AgentPolicyError(
                    f"duplicate alias {alias!r} for {record.id} and {seen_aliases[key]}"
                )
            seen_aliases[key] = record.id
        identities.append(
            PolicyIdentity(
                agent_id=record.id,
                aliases=aliases,
                category=record.tier,
                tier=record.tier,
                capability_mode=record.capability_mode,
                spawn_policy=record.spawn_policy,
                is_catalog_agent=True,
                policy=policy,
            )
        )
    profiles_raw = raw.get("profiles") or {}
    profiles_obj = _require_object(profiles_raw, label="overlay.profiles")
    for profile_id, body in profiles_obj.items():
        pid = _require_str(profile_id, label="profiles id")
        obj = _require_object(body, label=f"profiles.{pid}")
        cap = _require_str(
            obj.get("capability_mode"), label=f"profiles.{pid}.capability_mode"
        )
        if cap in FORBIDDEN_CAPABILITY_MODES:
            raise AgentPolicyError(
                f"profiles.{pid}.capability_mode {cap!r} is forbidden"
            )
        if cap not in ALLOWED_CAPABILITY_MODES:
            raise AgentPolicyError(
                f"profiles.{pid}.capability_mode must be read-only or read-write"
            )
        aliases_raw = obj.get("aliases") or [pid]
        if not isinstance(aliases_raw, list) or not aliases_raw:
            raise AgentPolicyError(
                f"profiles.{pid}.aliases must be a non-empty array"
            )
        aliases = tuple(
            _require_str(item, label=f"profiles.{pid}.aliases") for item in aliases_raw
        )
        for alias in aliases:
            key = alias.lower()
            if key in seen_aliases:
                raise AgentPolicyError(
                    f"duplicate alias {alias!r} for {pid} and {seen_aliases[key]}"
                )
            seen_aliases[key] = pid
        if pid.lower() in seen_aliases and seen_aliases[pid.lower()] != pid:
            raise AgentPolicyError(
                f"profile id {pid!r} collides with an agent alias"
            )
        seen_aliases[pid.lower()] = pid
        policy = _parse_model_policy(
            obj.get("model_policy"), label=f"profiles.{pid}.model_policy"
        )
        identities.append(
            PolicyIdentity(
                agent_id=pid,
                aliases=aliases,
                category=_optional_str(obj.get("category")),
                tier=_optional_str(obj.get("tier")),
                capability_mode=cap,
                spawn_policy=_require_str(
                    obj.get("spawn_policy", "leaf"),
                    label=f"profiles.{pid}.spawn_policy",
                ),
                is_catalog_agent=False,
                policy=policy,
            )
        )
    identities.sort(key=lambda item: item.agent_id)
    return PolicyBundle(
        schema=schema,
        prompt_profile_version=profile_version,
        identities=tuple(identities),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _load_override_file(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = _require_object(_load_json(path, label=label), label=label)
    _scan_secrets(raw, label=label)
    schema = raw.get("schema")
    if schema is not None and schema not in {OVERRIDE_SCHEMA, POLICY_SCHEMA}:
        raise AgentPolicyError(f"{label} unsupported schema {schema!r}")
    for banned in ("credentials", "endpoints", "endpoint"):
        if banned in raw:
            raise AgentPolicyError(
                f"{label} cannot define credentials or endpoints",
                code="E_AGENT_POLICY_SECRET",
            )
    agents = raw.get("agents") or {}
    return _require_object(agents, label=f"{label}.agents")


def _merge_policy(
    base: dict[str, Any], overlay: Mapping[str, Any], *, label: str
) -> dict[str, Any]:
    merged = {
        "policy_id": base["policy_id"],
        "baseline": dict(base["baseline"]),
        "prompt_profile": base["prompt_profile"],
        "reasoning": base["reasoning"],
        "extensions": dict(base["extensions"]),
    }
    if "capability_mode" in overlay or "capabilityMode" in overlay:
        raise AgentPolicyError(
            f"{label} cannot widen or replace capability floors",
            code="E_AGENT_POLICY_WIDEN",
        )
    if "policy_id" in overlay:
        merged["policy_id"] = _require_str(
            overlay["policy_id"], label=f"{label}.policy_id"
        )
    if "baseline" in overlay:
        merged["baseline"] = _parse_baseline(
            overlay["baseline"], label=f"{label}.baseline"
        )
    if "prompt_profile" in overlay:
        profile = _require_str(
            overlay["prompt_profile"], label=f"{label}.prompt_profile"
        )
        if profile not in PROMPT_PROFILES:
            raise AgentPolicyError(f"{label}.prompt_profile {profile!r} is unknown")
        merged["prompt_profile"] = profile
    if "reasoning" in overlay:
        merged["reasoning"] = (
            _require_str(overlay["reasoning"], label=f"{label}.reasoning")
            if overlay["reasoning"] is not None
            else None
        )
    if "extensions" in overlay:
        merged["extensions"] = _parse_extensions(
            overlay["extensions"], label=f"{label}.extensions"
        )
    if "model" in overlay and "models" in overlay:
        raise AgentPolicyError(
            f"{label}: model and models cannot both be set",
            code="E_AGENT_POLICY_CONFLICT",
        )
    extra = sorted(
        set(overlay)
        - {
            "policy_id",
            "baseline",
            "prompt_profile",
            "reasoning",
            "extensions",
            "model",
            "models",
        }
    )
    if extra:
        raise AgentPolicyError(f"{label} has unknown keys: {', '.join(extra)}")
    if "model" in overlay:
        merged["baseline"] = {
            "mode": "exact",
            "model": _require_str(overlay["model"], label=f"{label}.model"),
        }
    return merged


def policy_digest(
    *,
    policy: Mapping[str, Any],
    prompt_profile_version: str,
    capability_floor: str,
) -> str:
    payload = {
        "schema": POLICY_SCHEMA,
        "policy_id": policy["policy_id"],
        "baseline": policy["baseline"],
        "prompt_profile": policy["prompt_profile"],
        "prompt_profile_version": prompt_profile_version,
        "reasoning": policy.get("reasoning"),
        "extensions": policy.get("extensions") or {},
        "capability_floor": capability_floor,
    }
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _candidate_ids(policy: Mapping[str, Any]) -> tuple[str, ...]:
    ext = (policy.get("extensions") or {}).get(ORDERED_CANDIDATES_CAP) or {}
    rows = ext.get("candidates") or []
    return tuple(str(item["catalog"]) for item in rows if isinstance(item, dict))


def _first_extension_id(policy: Mapping[str, Any]) -> str | None:
    extensions = policy.get("extensions") or {}
    if not extensions:
        return None
    for cap_id in CAPABILITY_IDS:
        if cap_id in extensions:
            return cap_id
    return None


def resume_pin(view: AgentPolicyViewV1) -> dict[str, Any]:
    """Secret-free resume identity. Never invents a Medley receipt."""
    return {
        "agent_id": view.agent_id,
        "policy_id": view.policy_id,
        "policy_digest": view.policy_digest,
        "baseline_mode": view.baseline_mode,
        "selected_model_ref": view.selected_model_ref,
        "route_kind": view.route_kind,
        "route_receipt_digest": view.route_receipt_digest,
        "attempt": view.attempt if view.attempt is not None else 1,
        "prompt_profile": view.prompt_profile,
    }


def render_stock_host_projection(view: AgentPolicyViewV1) -> dict[str, Any]:
    """Fields a stock Grok Build projection may carry. Medley ids stripped."""
    return {
        "agent_id": view.agent_id,
        "capability_floor": view.capability_floor,
        "baseline_mode": view.baseline_mode,
        "baseline_model": (
            view.baseline_model if view.baseline_mode == "exact" else None
        ),
        "prompt_profile": view.prompt_profile,
        "route_kind": ROUTE_KIND_NATIVE,
        "medley_capability_outcome": view.host_facts.get(
            "medley_capability_outcome"
        ),
        "route_specific_facts": view.host_facts.get("route_specific_facts"),
    }


def parse_policy_route(
    raw: Mapping[str, Any],
) -> NativeAgentRoute | ExternalExecutorRoute:
    """Fail-closed native vs external_executor discriminator."""
    obj = _require_object(dict(raw), label="route")
    kind = _require_str(obj.get("kind"), label="route.kind")
    if kind == ROUTE_KIND_NATIVE:
        extra = sorted(
            set(obj) - {"kind", "policy_id", "baseline_mode", "model_ref"}
        )
        if extra:
            raise AgentPolicyError(
                f"native route has unknown keys: {', '.join(extra)}"
            )
        if any(key in obj for key in ("executor", "provider", "model_flag")):
            raise AgentPolicyError("native route cannot carry executor fields")
        return NativeAgentRoute(
            kind=ROUTE_KIND_NATIVE,
            policy_id=_require_str(obj.get("policy_id"), label="route.policy_id"),
            baseline_mode=_require_str(
                obj.get("baseline_mode"), label="route.baseline_mode"
            ),
            model_ref=_optional_str(obj.get("model_ref")),
        )
    if kind == ROUTE_KIND_EXTERNAL:
        leaked = sorted(set(obj) & _NATIVE_ONLY_KEYS)
        if leaked:
            raise AgentPolicyError(
                "external_executor route cannot carry native catalog/receipt keys: "
                + ", ".join(leaked)
            )
        executor = obj.get("executor")
        provider = obj.get("provider")
        if executor is None and provider is not None:
            executor = provider
        exec_s = _require_str(executor, label="route.executor")
        if exec_s not in EXTERNAL_EXECUTORS:
            raise AgentPolicyError(
                f"unknown executor {exec_s!r}; expected one of "
                f"{sorted(EXTERNAL_EXECUTORS)}"
            )
        return ExternalExecutorRoute(
            kind=ROUTE_KIND_EXTERNAL,
            executor=exec_s,
            model_flag=_optional_str(obj.get("model_flag")),
        )
    raise AgentPolicyError(
        f"route.kind must be {ROUTE_KIND_NATIVE!r} or {ROUTE_KIND_EXTERNAL!r}, "
        f"got {kind!r}"
    )


def _required_cap_status(state: str) -> str:
    """Map a missing required capability onto the view status taxonomy."""
    if state in {"unknown", "unavailable", "incompatible"}:
        return state
    return "blocked"


def resolve_agent_policy(
    name: str,
    *,
    root: Path | None = None,
    project_root: Path | None = None,
    user_home: Path | None = None,
    per_run: Mapping[str, Any] | None = None,
    host: HostCapabilitySnapshot | None = None,
    bundle: PolicyBundle | None = None,
) -> AgentPolicyViewV1:
    """Resolve portable model intent before host execution."""
    loaded = bundle or load_policy_bundle(root)
    identity = loaded.resolve_name(name)
    snapshot = host or stock_grok_snapshot()
    policy = dict(identity.policy)
    source = "canonical"

    if user_home is not None:
        user_agents = _load_override_file(
            Path(user_home) / USER_OVERRIDE_RELATIVE, label="user override"
        )
        if identity.agent_id in user_agents:
            policy = _merge_policy(
                policy, user_agents[identity.agent_id], label="user override"
            )
            source = "user"

    if project_root is not None:
        project_agents = _load_override_file(
            Path(project_root) / PROJECT_OVERRIDE_RELATIVE,
            label="project override",
        )
        if identity.agent_id in project_agents:
            policy = _merge_policy(
                policy,
                project_agents[identity.agent_id],
                label="project override",
            )
            source = "project"

    if per_run:
        _scan_secrets(per_run, label="per-run override")
        policy = _merge_policy(policy, per_run, label="per-run override")
        source = "per_run"

    if identity.capability_mode in FORBIDDEN_CAPABILITY_MODES:
        raise AgentPolicyError(
            f"{identity.agent_id}: capability floor "
            f"{identity.capability_mode!r} is forbidden"
        )

    digest = policy_digest(
        policy=policy,
        prompt_profile_version=loaded.prompt_profile_version,
        capability_floor=identity.capability_mode,
    )
    baseline = policy["baseline"]
    baseline_mode = str(baseline["mode"])
    baseline_model = baseline.get("model")
    requested_extension = _first_extension_id(policy)
    candidates = _candidate_ids(policy)
    reasons: list[PolicyReason] = []
    status = "ready"
    selected: str | None = None
    receipt: str | None = None

    if not snapshot.is_supported("host.native-agent.v1"):
        native_state = snapshot.state_of("host.native-agent.v1")
        status = _required_cap_status(native_state)
        reasons.append(
            PolicyReason(
                code="E_NATIVE_AGENT_UNSUPPORTED",
                message="host.native-agent.v1 is not supported on this host tier",
                next_action=(
                    "use original Grok Build, or negotiate a supported host capability"
                ),
            )
        )

    if baseline_mode == "inherit":
        if not snapshot.is_supported(INHERIT_MODEL_CAP):
            status = _required_cap_status(snapshot.state_of(INHERIT_MODEL_CAP))
            reasons.append(
                PolicyReason(
                    code="E_INHERIT_UNSUPPORTED",
                    message="inherit requires host.native-inherit-model.v1",
                    next_action="use a host that supports parent-model inheritance",
                )
            )
        selected = None
    elif baseline_mode == "exact":
        if not snapshot.is_supported(EXACT_MODEL_CAP):
            status = _required_cap_status(snapshot.state_of(EXACT_MODEL_CAP))
            selected = None
            reasons.append(
                PolicyReason(
                    code="E_EXACT_UNSUPPORTED",
                    message=(
                        "exact model binding requires host.native-exact-model.v1; "
                        "refusing to inherit the parent model"
                    ),
                    next_action=(
                        "declare baseline.mode inherit, or use a host that exposes "
                        "a safe exact-model contract"
                    ),
                )
            )
        else:
            selected = baseline_model
    elif baseline_mode == "requires_capability":
        needed = requested_extension or EXACT_MODEL_CAP
        if not snapshot.is_supported(needed):
            outcome = snapshot.state_of(needed)
            status = "blocked" if outcome == "unsupported" else outcome
            selected = None
            reasons.append(
                PolicyReason(
                    code="E_REQUIRES_CAPABILITY",
                    message=f"policy requires {needed} (state={snapshot.state_of(needed)})",
                    next_action=f"enable {needed} or choose an inherit baseline",
                )
            )

    if requested_extension and not snapshot.is_supported(requested_extension):
        ext_state = snapshot.state_of(requested_extension)
        reasons.append(
            PolicyReason(
                code="E_EXTENSION_NOT_AUTHORIZED",
                message=(
                    f"optional extension {requested_extension} is {ext_state}; "
                    "using declared baseline only (not the first catalog candidate)"
                ),
                next_action=(
                    "no action on original Grok Build; Medley exact/candidates "
                    "remain host-owned when that contract ships"
                ),
            )
        )

    if snapshot.host_tier == HOST_TIER_GROK:
        receipt = None

    host_facts = {
        "medley_capability_outcome": medley_capability_outcome(snapshot),
        "route_specific_facts": route_specific_facts_state(snapshot),
        "host_tier": snapshot.host_tier,
    }

    return AgentPolicyViewV1(
        agent_id=identity.agent_id,
        aliases=identity.aliases,
        category=identity.category,
        tier=identity.tier,
        capability_floor=identity.capability_mode,
        tool_floor=(),
        policy_id=str(policy["policy_id"]),
        policy_digest=digest,
        policy_source=source,
        baseline_mode=baseline_mode,
        baseline_model=baseline_model,
        requested_extension=requested_extension,
        candidate_ids=candidates,
        prompt_profile=str(policy["prompt_profile"]),
        reasoning_preference=policy.get("reasoning"),
        host_capabilities=tuple(item.to_json() for item in snapshot.capabilities),
        selected_model_ref=selected,
        route_kind=ROUTE_KIND_NATIVE,
        route_receipt_digest=receipt,
        attempt=1,
        status=status,
        reasons=tuple(reasons),
        host_facts=host_facts,
    )


def list_agent_policies(
    *,
    root: Path | None = None,
    project_root: Path | None = None,
    user_home: Path | None = None,
    host: HostCapabilitySnapshot | None = None,
    catalog_only: bool = False,
) -> tuple[AgentPolicyViewV1, ...]:
    loaded = load_policy_bundle(root)
    snapshot = host or stock_grok_snapshot()
    rows: list[AgentPolicyViewV1] = []
    for identity in loaded.identities:
        if catalog_only and not identity.is_catalog_agent:
            continue
        rows.append(
            resolve_agent_policy(
                identity.agent_id,
                root=root,
                project_root=project_root,
                user_home=user_home,
                host=snapshot,
                bundle=loaded,
            )
        )
    rows.sort(key=lambda item: item.agent_id)
    return tuple(rows)


def filter_policy_views(
    rows: tuple[AgentPolicyViewV1, ...],
    *,
    agent: str | None = None,
    alias: str | None = None,
    category: str | None = None,
    capability_floor: str | None = None,
    policy_source: str | None = None,
    host_capability: str | None = None,
    status: str | None = None,
) -> tuple[AgentPolicyViewV1, ...]:
    out: list[AgentPolicyViewV1] = []
    for row in rows:
        if agent and agent.lower() not in {
            row.agent_id.lower(),
            *(a.lower() for a in row.aliases),
        }:
            continue
        if alias and alias.lower() not in {a.lower() for a in row.aliases}:
            continue
        if category and (row.category or "").lower() != category.lower():
            continue
        if capability_floor and row.capability_floor != capability_floor:
            continue
        if policy_source and row.policy_source != policy_source:
            continue
        if status and row.status != status:
            continue
        if host_capability:
            caps = {
                str(item.get("capability_id")): str(item.get("state"))
                for item in row.host_capabilities
            }
            needle = host_capability
            if "=" in needle:
                cap_id, want = needle.split("=", 1)
                if caps.get(cap_id.strip()) != want.strip():
                    continue
            elif needle not in caps:
                continue
        out.append(row)
    return tuple(out)


def spawn_admitted(view: AgentPolicyViewV1) -> bool:
    """True when native spawn may proceed. Exact-missing never inherits."""
    if view.status == "blocked":
        return False
    if view.baseline_mode == "exact" and view.selected_model_ref is None:
        return False
    if view.capability_floor in FORBIDDEN_CAPABILITY_MODES:
        return False
    if view.capability_floor not in ALLOWED_CAPABILITY_MODES:
        return False
    return view.status in {"ready", "unsupported"}


__all__ = [
    "AgentPolicyError",
    "AgentPolicyViewV1",
    "BASELINE_MODES",
    "EXTERNAL_EXECUTORS",
    "ExternalExecutorRoute",
    "NativeAgentRoute",
    "POLICY_RELATIVE",
    "POLICY_ROUTE_KINDS",
    "POLICY_SCHEMA",
    "PROMPT_PROFILES",
    "PolicyBundle",
    "PolicyReason",
    "ROUTE_KIND_EXTERNAL",
    "ROUTE_KIND_NATIVE",
    "VIEW_SCHEMA",
    "filter_policy_views",
    "list_agent_policies",
    "load_policy_bundle",
    "parse_policy_route",
    "policy_digest",
    "policy_overlay_path",
    "render_stock_host_projection",
    "resolve_agent_policy",
    "resume_pin",
    "spawn_admitted",
]
