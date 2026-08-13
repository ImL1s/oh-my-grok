"""Canonical unproven advisor harness registry.

Rows are static documents.  Construction never calls shutil.which, subprocess,
socket, urllib, or reads PATH.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omg_cli.contracts.advisor_contract import (
    CANONICAL_HARNESS_IDS,
    HARNESS_ALIASES,
    parse_advisor_harness_spec_v1,
)
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_nonempty_string,
)


UNPROVEN_LIMITATION = "unproven: no pinned identity/version/behavior fixture"
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_MAX_OUTPUT_BYTES = 524288
_MAX_ALIAS_CHARS = 128

HARNESS_BINARY_NAMES: dict[str, tuple[str, ...]] = {
    "claude-cli": ("claude",),
    "codex-cli": ("codex",),
    "grok-cli": ("grok",),
    "cursor-cli": ("cursor", "cursor-agent"),
    "antigravity-cli": ("agy",),
    "gemini-cli": ("gemini",),
}


def _copy_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    copied = dict(spec)
    for key in ("aliases", "binary_names", "platforms", "prompt_transports", "limitations"):
        copied[key] = list(spec[key])
    return copied


def _build_alias_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for harness_id in CANONICAL_HARNESS_IDS:
        mapping[harness_id] = harness_id
        for alias in HARNESS_ALIASES[harness_id]:
            if alias in mapping:
                raise ContractValidationError(f"duplicate harness alias {alias!r}")
            mapping[alias] = harness_id
    return mapping


def _build_specs() -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for harness_id in CANONICAL_HARNESS_IDS:
        rows.append(
            parse_advisor_harness_spec_v1(
                {
                    "schema_version": 1,
                    "harness_id": harness_id,
                    "aliases": list(HARNESS_ALIASES[harness_id]),
                    "binary_names": list(HARNESS_BINARY_NAMES[harness_id]),
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
                    "default_timeout_s": DEFAULT_TIMEOUT_S,
                    "max_output_bytes": DEFAULT_MAX_OUTPUT_BYTES,
                    "advisor_read_only": "unproven",
                    "limitations": [UNPROVEN_LIMITATION],
                }
            )
        )
    return tuple(rows)


ALIAS_TO_HARNESS: dict[str, str] = _build_alias_map()
_SPECS: tuple[dict[str, Any], ...] = _build_specs()
_SPECS_BY_ID: dict[str, dict[str, Any]] = {spec["harness_id"]: spec for spec in _SPECS}


def list_harness_specs() -> tuple[dict[str, Any], ...]:
    return tuple(_copy_spec(spec) for spec in _SPECS)


def get_harness_spec(harness_id: str) -> dict[str, Any]:
    if not isinstance(harness_id, str):
        raise ContractValidationError("harness_id must be a string")
    spec = _SPECS_BY_ID.get(harness_id)
    if spec is None:
        raise ContractValidationError(f"unknown harness {harness_id!r}")
    return _copy_spec(spec)


def resolve_harness_id(name: str) -> str:
    if not isinstance(name, str):
        raise ContractValidationError("harness name must be a string")
    require_nonempty_string(name, label="harness name")
    stripped = name.strip()
    if not stripped:
        raise ContractValidationError("harness name must be a non-empty string")
    if len(stripped) > _MAX_ALIAS_CHARS:
        raise ContractValidationError(
            "harness alias is overlong (exceeds 128 characters)"
        )
    token = stripped.casefold()
    harness_id = ALIAS_TO_HARNESS.get(token)
    if harness_id is None:
        raise ContractValidationError(f"unknown harness {name!r}")
    return harness_id


__all__ = [
    "ALIAS_TO_HARNESS",
    "CANONICAL_HARNESS_IDS",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_S",
    "HARNESS_BINARY_NAMES",
    "UNPROVEN_LIMITATION",
    "get_harness_spec",
    "list_harness_specs",
    "resolve_harness_id",
]
