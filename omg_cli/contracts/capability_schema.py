"""Honest capability and parity classification schema."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_iso8601,
    require_nonempty_string,
    require_object,
    require_sha256,
    require_string_list,
)


CAPABILITY_TIERS = (
    "configured",
    "installed",
    "enabled",
    "loadable",
    "observed",
    "healthy",
    "verified",
)
PARITY_CLASSIFICATIONS = (
    "faithful",
    "native_substitute",
    "host_owned",
    "host_impossible",
    "optional_unclaimed",
)

_REDACTION_MARKER_RE = re.compile(
    r"(?:\[redacted\]|<redacted>|<omitted>|\*{3,}|sha256:[0-9a-f]{64})",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bauthorization\s*[:=]\s*(?:bearer|basic)\s+([^\s,;]+)", re.I),
    re.compile(r"\b(?:cookie|set-cookie)\s*[:=]\s*([^\r\n]+)", re.I),
    re.compile(
        r"\b(?:access[_-]?token|refresh[_-]?token|token|password|passwd|secret|"
        r"client[_-]?secret|api[_-]?key)\s*[:=]\s*([^\s,;&]+)",
        re.I,
    ),
    re.compile(
        r"[?&](?:access[_-]?token|refresh[_-]?token|token|password|secret|api[_-]?key)="
        r"([^&#\s]+)",
        re.I,
    ),
)


def _assert_no_raw_secret_text(text: str, *, label: str) -> None:
    for pattern in _SECRET_VALUE_PATTERNS:
        for match in pattern.finditer(text):
            if not _REDACTION_MARKER_RE.search(match.group(1)):
                raise ContractValidationError(f"{label} contains an unredacted credential")


def _assert_redacted_value(value: Any, *, label: str) -> None:
    if isinstance(value, str):
        _assert_no_raw_secret_text(value, label=label)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _assert_redacted_value(item, label=f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_redacted_value(item, label=f"{label}[{index}]")


def validate_capability_record(value: Mapping[str, Any]) -> dict[str, Any]:
    record = require_object(value, label="capability record")
    required = {
        "store_kind",
        "schema_version",
        "canonical_name",
        "aliases",
        "origin",
        "resolution_priority",
        "version",
        "digest",
        "probe_timestamp",
        "bounded_result",
        "redacted_diagnostic",
        *CAPABILITY_TIERS,
    }
    require_exact_keys(record, required=required, label="capability record")
    if record["store_kind"] != "capability_evidence":
        raise ContractValidationError("invalid capability store_kind")
    if record["schema_version"] != 1 or isinstance(record["schema_version"], bool):
        raise ContractValidationError("capability schema_version must be integer 1")
    require_nonempty_string(record["canonical_name"], label="canonical_name")
    require_string_list(record["aliases"], label="aliases", unique=True)
    require_nonempty_string(record["origin"], label="origin")
    if isinstance(record["resolution_priority"], bool) or not isinstance(
        record["resolution_priority"], int
    ) or record["resolution_priority"] < 0:
        raise ContractValidationError("resolution_priority must be a non-negative integer")
    if record["canonical_name"] in record["aliases"]:
        raise ContractValidationError("aliases may not repeat canonical_name")
    require_nonempty_string(record["version"], label="version")
    require_sha256(record["digest"], label="digest")
    require_iso8601(record["probe_timestamp"], label="probe_timestamp")
    bounded_result = require_object(record["bounded_result"], label="bounded_result")
    _assert_redacted_value(bounded_result, label="bounded_result")
    if not isinstance(record["redacted_diagnostic"], str):
        raise ContractValidationError("redacted_diagnostic must be a string")
    _assert_no_raw_secret_text(record["redacted_diagnostic"], label="redacted_diagnostic")
    for tier in CAPABILITY_TIERS:
        if not isinstance(record[tier], bool):
            raise ContractValidationError(f"{tier} must be an independent boolean")
    return record


def validate_parity_classification(value: str) -> str:
    if value not in PARITY_CLASSIFICATIONS:
        raise ContractValidationError(f"unsupported parity classification: {value!r}")
    return value


def claimed_tiers(record: Mapping[str, Any]) -> list[str]:
    validated = validate_capability_record(record)
    return [tier for tier in CAPABILITY_TIERS if validated[tier]]
