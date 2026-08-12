"""Fail-closed advisor taxonomy and AdvisorHarnessSpecV1 parser.

Slice A admits documents only.  It never probes PATH, binaries, or the network,
and it never claims support or qualification.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_integer,
    require_nonempty_string,
    require_object,
    require_safe_id,
    require_string_list,
)
from .writer_chain import canonical_json_bytes, sha256_hex


RUNTIME_KINDS = ("native_host", "external_cli")
PURPOSES = ("advisory", "task_execution")
LIFECYCLES = ("foreground", "background_job", "team_member")
ADVISOR_LIFECYCLES = ("foreground", "background_job")
ADVISOR_READ_ONLY_STATES = (
    "qualified",
    "interactive_only",
    "unproven",
    "unsupported",
)
ADVISOR_FLAG_KEYS = ("worker_eligible", "authoritative", "auto_apply")

CANONICAL_HARNESS_IDS: tuple[str, ...] = (
    "claude-cli",
    "codex-cli",
    "grok-cli",
    "cursor-cli",
    "antigravity-cli",
    "gemini-cli",
)
HARNESS_ALIASES: dict[str, tuple[str, ...]] = {
    "claude-cli": ("claude", "fable"),
    "codex-cli": ("codex",),
    "grok-cli": ("grok",),
    "cursor-cli": ("cursor", "cursor-agent"),
    "antigravity-cli": ("agy", "antigravity"),
    "gemini-cli": ("gemini",),
}

ADVISOR_HARNESS_SPEC_V1_KEYS = (
    "schema_version",
    "harness_id",
    "aliases",
    "binary_names",
    "identity_probe",
    "version_probe",
    "tested_versions",
    "platforms",
    "supports_advisor",
    "supports_executor",
    "supports_background",
    "supports_structured_output",
    "supports_resume",
    "prompt_transports",
    "preferred_prompt_transport",
    "needs_pty",
    "cancellation_strategy",
    "default_timeout_s",
    "max_output_bytes",
    "advisor_read_only",
    "limitations",
)

# Family-tagged before exact-key checks so callers can assert team vs native.
_TEAM_FORBIDDEN_KEYS = frozenset(
    {
        "member",
        "member_id",
        "task",
        "task_id",
        "worktree",
        "worktree_id",
        "token",
        "claim_token",
        "cancellation_token",
        "team_id",
        "worker_id",
        "mailbox",
        "pane_id",
    }
)
_NATIVE_FORBIDDEN_KEYS = frozenset(
    {
        "provider",
        "catalog",
        "catalog_id",
        "receipt",
        "access",
        "access_lane",
        "model_route",
        "medley",
        "native_provider",
    }
)
_CREDENTIAL_FORBIDDEN_KEYS = frozenset(
    {
        "argv",
        "api_key",
        "endpoint",
        "account",
        "query",
        "password",
        "secret",
        "credential",
        "prompt",
        "response",
        "body",
        "stdout",
        "stderr",
    }
)
_SUPPORT_KEYS = (
    "supports_advisor",
    "supports_executor",
    "supports_background",
    "supports_structured_output",
    "supports_resume",
)

_ALIAS_OWNERS: dict[str, str] = {}
for _harness_id, _aliases in HARNESS_ALIASES.items():
    for _alias in _aliases:
        if _alias in _ALIAS_OWNERS or _alias in CANONICAL_HARNESS_IDS:
            raise RuntimeError(f"static advisor alias table is not unique: {_alias!r}")
        _ALIAS_OWNERS[_alias] = _harness_id


def reject_advisor_forbidden_keys(payload: Mapping[str, Any]) -> None:
    """Reject Team, native, and credential/prompt keys before exact-key checks."""

    team_hits = sorted(key for key in payload if key in _TEAM_FORBIDDEN_KEYS)
    if team_hits:
        raise ContractValidationError(
            f"advisor document contains forbidden team key(s): {team_hits}"
        )
    native_hits = sorted(key for key in payload if key in _NATIVE_FORBIDDEN_KEYS)
    if native_hits:
        raise ContractValidationError(
            f"advisor document contains forbidden native key(s): {native_hits}"
        )
    credential_hits = sorted(key for key in payload if key in _CREDENTIAL_FORBIDDEN_KEYS)
    if credential_hits:
        raise ContractValidationError(
            f"advisor document contains forbidden credential/prompt key(s): {credential_hits}"
        )


_reject_forbidden_keys = reject_advisor_forbidden_keys


def _require_bool(value: Any, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{label} must be a boolean")
    return value


def _require_string_allow_empty(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ContractValidationError(f"{label} must be a string")
    if value == "":
        return value
    return require_nonempty_string(value, label=label)


def _require_positive_number(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractValidationError(f"{label} must be a number")
    if not math.isfinite(value) or value <= 0:
        raise ContractValidationError(f"{label} must be a finite number > 0")
    # Canonical JSON v1 is integer-only; whole-number timeouts normalize to int.
    if float(value) != int(value):
        raise ContractValidationError(
            f"{label} must be a whole number of seconds for canonical encoding"
        )
    return int(value)


def _require_basename(value: Any, *, label: str) -> str:
    text = require_nonempty_string(value, label=label)
    if text != text.strip():
        raise ContractValidationError(f"{label} must not have surrounding whitespace")
    if any(separator in text for separator in ("/", "\\", "~", ":")):
        raise ContractValidationError(
            f"{label} must be a basename only; absolute, home, and private paths are rejected"
        )
    if text in {".", ".."}:
        raise ContractValidationError(f"{label} is not a usable basename")
    return text


def _normalize_alias_token(value: str, *, label: str) -> str:
    require_nonempty_string(value, label=label)
    token = value.strip().casefold()
    if not token:
        raise ContractValidationError(f"{label} must be a non-empty token")
    return token


def _normalize_aliases(value: Any, *, harness_id: str) -> list[str]:
    raw = require_string_list(value, label="aliases", unique=True)
    aliases: list[str] = []
    seen: set[str] = set()
    for item in raw:
        token = _normalize_alias_token(item, label="aliases[]")
        if token in CANONICAL_HARNESS_IDS:
            raise ContractValidationError("aliases must not contain a canonical harness id")
        owner = _ALIAS_OWNERS.get(token)
        if owner is not None and owner != harness_id:
            raise ContractValidationError(
                f"alias {token!r} belongs to {owner}, not {harness_id}"
            )
        if token in seen:
            raise ContractValidationError("aliases must not contain duplicates")
        seen.add(token)
        aliases.append(token)
    return aliases


def validate_advisor_taxonomy(
    payload_or_kwargs: Mapping[str, Any] | None = None,
    /,
    **kwargs: Any,
) -> dict[str, Any]:
    """Accept only external_cli + advisory + foreground|background_job."""

    if payload_or_kwargs is None:
        payload = dict(kwargs)
    else:
        payload = require_object(payload_or_kwargs, label="advisor taxonomy")
        if kwargs:
            payload = {**payload, **kwargs}
    _reject_forbidden_keys(payload)

    runtime_kind = payload.get("runtime_kind")
    purpose = payload.get("purpose")
    lifecycle = payload.get("lifecycle")
    if runtime_kind not in RUNTIME_KINDS:
        raise ContractValidationError(f"unknown runtime_kind {runtime_kind!r}")
    if purpose not in PURPOSES:
        raise ContractValidationError(f"unknown purpose {purpose!r}")
    if lifecycle not in LIFECYCLES:
        raise ContractValidationError(f"unknown lifecycle {lifecycle!r}")
    if runtime_kind != "external_cli":
        raise ContractValidationError(
            "advisor taxonomy rejects runtime_kind=native_host (owned by native host routing)"
        )
    if purpose != "advisory":
        raise ContractValidationError(
            "advisor taxonomy rejects purpose=task_execution (owned by team/workers)"
        )
    if lifecycle not in ADVISOR_LIFECYCLES:
        raise ContractValidationError(
            "advisor taxonomy rejects lifecycle=team_member (owned by team/workers)"
        )

    for flag in ADVISOR_FLAG_KEYS:
        if flag not in payload:
            continue
        value = _require_bool(payload[flag], label=flag)
        if value:
            raise ContractValidationError(
                f"{flag} cannot be true for an advisor; advisors are never workers"
            )

    return {
        "runtime_kind": runtime_kind,
        "purpose": purpose,
        "lifecycle": lifecycle,
        "worker_eligible": False,
        "authoritative": False,
        "auto_apply": False,
    }


def parse_advisor_harness_spec_v1(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize an AdvisorHarnessSpecV1 document.  Specs do not carry flags."""

    payload = require_object(raw, label="advisor harness spec")
    _reject_forbidden_keys(payload)
    require_exact_keys(
        payload,
        required=set(ADVISOR_HARNESS_SPEC_V1_KEYS),
        label="advisor harness spec",
    )

    schema_version = require_integer(
        payload["schema_version"], label="schema_version", minimum=1
    )
    if schema_version != 1:
        raise ContractValidationError(
            f"unsupported advisor harness schema_version={schema_version}; expected 1"
        )

    harness_id = require_safe_id(payload["harness_id"], label="harness_id")
    if harness_id not in CANONICAL_HARNESS_IDS:
        raise ContractValidationError(f"unknown harness_id {harness_id!r}")

    aliases = _normalize_aliases(payload["aliases"], harness_id=harness_id)
    binary_names = require_string_list(
        payload["binary_names"], label="binary_names", unique=True
    )
    binary_names = [
        _require_basename(item, label="binary_names[]") for item in binary_names
    ]
    if len(binary_names) != len(set(binary_names)):
        raise ContractValidationError("binary_names must not contain duplicates")

    identity_probe = require_nonempty_string(
        payload["identity_probe"], label="identity_probe"
    )
    if identity_probe != "none":
        raise ContractValidationError('identity_probe must be "none"')
    version_probe = require_nonempty_string(
        payload["version_probe"], label="version_probe"
    )
    if version_probe != "none":
        raise ContractValidationError('version_probe must be "none"')
    if payload["tested_versions"] is not None:
        raise ContractValidationError("tested_versions must be null")

    platforms = require_string_list(payload["platforms"], label="platforms", unique=True)
    if platforms:
        raise ContractValidationError("platforms must be empty")

    supports = {
        key: _require_bool(payload[key], label=key) for key in _SUPPORT_KEYS
    }
    if any(supports.values()):
        raise ContractValidationError("Slice A advisor supports_* flags must be false")

    prompt_transports = require_string_list(
        payload["prompt_transports"], label="prompt_transports", unique=True
    )
    if prompt_transports:
        raise ContractValidationError("prompt_transports must be empty")
    preferred = _require_string_allow_empty(
        payload["preferred_prompt_transport"],
        label="preferred_prompt_transport",
    )
    if preferred != "":
        raise ContractValidationError(
            "preferred_prompt_transport must be empty when prompt_transports is empty"
        )

    needs_pty = _require_bool(payload["needs_pty"], label="needs_pty")
    if needs_pty:
        raise ContractValidationError("needs_pty must be false")
    cancellation_strategy = require_nonempty_string(
        payload["cancellation_strategy"], label="cancellation_strategy"
    )
    if cancellation_strategy != "none":
        raise ContractValidationError('cancellation_strategy must be "none"')

    default_timeout_s = _require_positive_number(
        payload["default_timeout_s"], label="default_timeout_s"
    )
    max_output_bytes = require_integer(
        payload["max_output_bytes"], label="max_output_bytes", minimum=1
    )

    advisor_read_only = require_nonempty_string(
        payload["advisor_read_only"], label="advisor_read_only"
    )
    if advisor_read_only not in ADVISOR_READ_ONLY_STATES:
        raise ContractValidationError(
            f"unknown advisor_read_only {advisor_read_only!r}"
        )

    limitations = require_string_list(payload["limitations"], label="limitations")
    if not limitations:
        raise ContractValidationError("limitations must be non-empty")

    return {
        "schema_version": 1,
        "harness_id": harness_id,
        "aliases": aliases,
        "binary_names": binary_names,
        "identity_probe": "none",
        "version_probe": "none",
        "tested_versions": None,
        "platforms": [],
        "supports_advisor": False,
        "supports_executor": False,
        "supports_background": False,
        "supports_structured_output": False,
        "supports_resume": False,
        "prompt_transports": [],
        "preferred_prompt_transport": "",
        "needs_pty": False,
        "cancellation_strategy": "none",
        "default_timeout_s": default_timeout_s,
        "max_output_bytes": max_output_bytes,
        "advisor_read_only": advisor_read_only,
        "limitations": limitations,
    }


def advisor_harness_spec_digest(spec: Mapping[str, Any]) -> str:
    """SHA-256 of canonical JSON.  Digest is not a field on the spec."""

    payload = require_object(spec, label="advisor harness spec")
    return sha256_hex(canonical_json_bytes(payload))
