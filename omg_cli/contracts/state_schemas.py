"""Small validation vocabulary shared by W0 contract schemas."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ContractValidationError(ValueError):
    """Structured contract input failed before any mutation."""


def require_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{label} must be an object")
    return dict(value)


def require_exact_keys(
    value: Mapping[str, Any],
    *,
    required: set[str] | frozenset[str],
    optional: set[str] | frozenset[str] = frozenset(),
    label: str,
) -> None:
    keys = set(value)
    missing = required - keys
    extra = keys - required - optional
    if missing or extra:
        raise ContractValidationError(
            f"{label} key mismatch: missing={sorted(missing)!r} extra={sorted(extra)!r}"
        )


def require_nonempty_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractValidationError(f"{label} must be a non-empty string")
    for char in value:
        codepoint = ord(char)
        if codepoint == 0 or codepoint < 0x20 or 0xD800 <= codepoint <= 0xDFFF:
            raise ContractValidationError(f"{label} contains a control or surrogate")
    return value


def require_safe_id(value: Any, *, label: str) -> str:
    text = require_nonempty_string(value, label=label)
    if not SAFE_ID_RE.fullmatch(text):
        raise ContractValidationError(f"{label} is not a safe identifier")
    return text


def require_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContractValidationError(f"{label} must be lowercase SHA-256 hex")
    return value


def require_git_oid(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not GIT_OID_RE.fullmatch(value):
        raise ContractValidationError(f"{label} must be a full lowercase Git object ID")
    return value


def require_integer(value: Any, *, label: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractValidationError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractValidationError(f"{label} must be >= {minimum}")
    return value


def require_string_list(
    value: Any,
    *,
    label: str,
    unique: bool = False,
) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError(f"{label} must be an array")
    result = [require_nonempty_string(item, label=f"{label}[]") for item in value]
    if unique and len(result) != len(set(result)):
        raise ContractValidationError(f"{label} must not contain duplicates")
    return result


def require_iso8601(value: Any, *, label: str) -> str:
    text = require_nonempty_string(value, label=label)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ContractValidationError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractValidationError(f"{label} must include a timezone")
    return text


def validate_store_header(
    value: Mapping[str, Any],
    *,
    store_kind: str,
    schema_version: int = 1,
) -> None:
    if value.get("store_kind") != store_kind:
        raise ContractValidationError(
            f"store_kind must be {store_kind!r}, got {value.get('store_kind')!r}"
        )
    version = require_integer(value.get("schema_version"), label="schema_version", minimum=1)
    if version != schema_version:
        raise ContractValidationError(
            f"unsupported {store_kind} schema_version={version}; expected {schema_version}"
        )
