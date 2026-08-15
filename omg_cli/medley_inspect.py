"""Consume Medley inspect JSON (#131/#134 glue).

The inspect document is an explicit, secret-free host projection. Support is
never inferred from executable name, branding, PATH, or state directories.
Absence is original Grok Build baseline (Medley caps unsupported), not an
install failure. Discovery performs no paid inference.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from omg_cli.host_capabilities import (
    ADVERTISED_MISSING,
    HOST_TIER_MEDLEY,
    HostCapabilitySnapshot,
    negotiate,
    stock_grok_snapshot,
)

INSPECT_SCHEMA = "medley.native-subagent-route.inspect/v1"
RECEIPT_SCHEMA = "medley.native-route-receipt.v1"
INSPECT_ENV = "OMG_MEDLEY_INSPECT"
EXACT_HOST_CAP = "host.native-exact-model.v1"
EXACT_MEDLEY_CAP = "medley.native-exact-model.v1"
_ADVERTISED_INCOMPATIBLE = "incompatible"
_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# Credential-shaped values. Diagnostic words such as "authorization" in a
# capability reason are not secrets; secret-named keys with values still fail.
_VALUE_SECRET_NEEDLES: tuple[str, ...] = (
    "sk-",
    "bearer ",
    "acct_",
    "-----begin ",
    "x-api-key",
)
_SECRET_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "x-api-key",
        "x_api_key",
        "bearer",
        "oauth",
        "secret",
        "password",
        "token",
    }
)


class MedleyInspectError(ValueError):
    """Fail-closed inspect parse error."""

    def __init__(self, message: str, *, code: str = "E_MEDLEY_INSPECT") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class MedleyInspectDocument:
    schema: str
    schema_version: int
    host: str
    capabilities: tuple[dict[str, Any], ...]
    receipts: tuple[dict[str, Any], ...]
    source_path: str

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "host": self.host,
            "capabilities": list(self.capabilities),
            "receipts": list(self.receipts),
            "source_path": self.source_path,
        }


def inspect_path_from_env(
    env: Mapping[str, str] | None = None,
    *,
    cli_path: str | None = None,
) -> Path | None:
    """Resolve an explicit inspect path. Empty/unset means stock Grok Build."""
    if cli_path and str(cli_path).strip():
        return Path(str(cli_path).strip())
    source = env if env is not None else os.environ
    raw = str(source.get(INSPECT_ENV, "") or "").strip()
    if not raw:
        return None
    return Path(raw)


def load_inspect_document(
    path: Path,
) -> MedleyInspectDocument:
    if path.is_symlink():
        raise MedleyInspectError(
            "inspect path must be a regular file (symlink refused)",
            code="E_MEDLEY_INSPECT_PATH",
        )
    if not path.is_file():
        raise MedleyInspectError(
            f"inspect file not found: {path}",
            code="E_MEDLEY_INSPECT_PATH",
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MedleyInspectError(
            f"inspect file unreadable: {exc}",
            code="E_MEDLEY_INSPECT_PATH",
        ) from exc
    _reject_secrets(raw, label="inspect")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MedleyInspectError(
            f"inspect JSON is malformed: {exc}",
            code="E_MEDLEY_INSPECT_SCHEMA",
        ) from exc
    if not isinstance(payload, dict):
        raise MedleyInspectError(
            "inspect document must be a JSON object",
            code="E_MEDLEY_INSPECT_SCHEMA",
        )
    schema = str(payload.get("schema") or "").strip()
    if schema != INSPECT_SCHEMA:
        raise MedleyInspectError(
            f"unsupported inspect schema {schema!r}",
            code="E_MEDLEY_INSPECT_SCHEMA",
        )
    version = payload.get("schemaVersion", payload.get("schema_version", 1))
    if not isinstance(version, int) or version != 1:
        raise MedleyInspectError(
            f"incompatible inspect schema_version {version!r}",
            code="E_MEDLEY_INSPECT_SCHEMA",
        )
    host = str(payload.get("host") or "").strip()
    if host != "medley":
        raise MedleyInspectError(
            "inspect host must be 'medley'",
            code="E_MEDLEY_INSPECT_SCHEMA",
        )
    caps = payload.get("capabilities") or []
    receipts = payload.get("receipts") or []
    if not isinstance(caps, list) or not isinstance(receipts, list):
        raise MedleyInspectError(
            "capabilities and receipts must be arrays",
            code="E_MEDLEY_INSPECT_SCHEMA",
        )
    cap_rows: list[dict[str, Any]] = []
    for item in caps:
        if not isinstance(item, dict):
            raise MedleyInspectError(
                "capability row must be an object",
                code="E_MEDLEY_INSPECT_SCHEMA",
            )
        cap_id = str(item.get("capability_id") or item.get("capabilityId") or "").strip()
        state = str(item.get("state") or "").strip()
        if not cap_id or not state:
            raise MedleyInspectError(
                "capability row needs capability_id and state",
                code="E_MEDLEY_INSPECT_SCHEMA",
            )
        cap_rows.append(
            {
                "capability_id": cap_id,
                "state": state,
                "version": item.get("version"),
                "reason": item.get("reason"),
            }
        )
    rec_rows: list[dict[str, Any]] = []
    for item in receipts:
        if not isinstance(item, dict):
            raise MedleyInspectError(
                "receipt must be an object",
                code="E_MEDLEY_INSPECT_SCHEMA",
            )
        rec_rows.append(_validated_receipt(item))
    _reject_secrets_tree(payload)
    return MedleyInspectDocument(
        schema=schema,
        schema_version=version,
        host=host,
        capabilities=tuple(cap_rows),
        receipts=tuple(rec_rows),
        source_path=str(path),
    )


def advertised_from_inspect(doc: MedleyInspectDocument) -> dict[str, str]:
    """Map inspect rows onto OMG negotiate() advertised values."""
    advertised: dict[str, str] = {}
    for row in doc.capabilities:
        cap_id = str(row["capability_id"])
        state = str(row["state"])
        version = row.get("version")
        if state == "supported":
            advertised[cap_id] = str(version).strip() if version else "v1"
        elif state == "unavailable":
            advertised[cap_id] = ADVERTISED_MISSING
        elif state == "incompatible":
            # Never forward a recognized version: negotiate() would mark
            # incompatible+v1 as supported.
            advertised[cap_id] = _ADVERTISED_INCOMPATIBLE
        # unsupported / unknown: omit so negotiate reports unsupported
    if any(
        str(row["capability_id"]) == EXACT_MEDLEY_CAP and str(row["state"]) == "supported"
        for row in doc.capabilities
    ):
        advertised.setdefault(EXACT_HOST_CAP, "v1")
    return advertised


def snapshot_from_inspect(doc: MedleyInspectDocument) -> HostCapabilitySnapshot:
    return negotiate(
        host_tier=HOST_TIER_MEDLEY,
        advertised=advertised_from_inspect(doc),
    )


def resolve_host_snapshot(
    *,
    inspect_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    cli_path: str | None = None,
) -> tuple[HostCapabilitySnapshot, MedleyInspectDocument | None]:
    """Stock Grok Build unless an explicit inspect document is supplied."""
    path = inspect_path or inspect_path_from_env(env, cli_path=cli_path)
    if path is None:
        return stock_grok_snapshot(), None
    doc = load_inspect_document(path)
    return snapshot_from_inspect(doc), doc


def receipt_for_policy(
    doc: MedleyInspectDocument | None,
    *,
    policy_id: str,
    agent_id: str | None = None,
) -> dict[str, Any] | None:
    if doc is None:
        return None
    for row in doc.receipts:
        consumer = str(
            row.get("consumerPolicyId")
            or row.get("consumer_policy_id")
            or ""
        ).strip()
        if consumer == policy_id:
            return dict(row)
        if agent_id and str(row.get("agent_id") or row.get("agentId") or "") == agent_id:
            return dict(row)
    return None


def apply_receipt_to_view_fields(receipt: Mapping[str, Any]) -> dict[str, Any]:
    selected = (
        receipt.get("selectedCatalogId")
        or receipt.get("selected_catalog_id")
        or receipt.get("selected_model_ref")
    )
    digest = (
        receipt.get("routeDigest")
        or receipt.get("route_digest")
        or receipt.get("route_receipt_digest")
    )
    attempt = receipt.get("attempt")
    return {
        "selected_model_ref": str(selected).strip() if selected else None,
        "route_receipt_digest": str(digest).strip() if digest else None,
        "attempt": int(attempt) if isinstance(attempt, int) else None,
    }


def _validated_receipt(item: Mapping[str, Any]) -> dict[str, Any]:
    schema = str(item.get("schema") or "").strip()
    if schema != RECEIPT_SCHEMA:
        raise MedleyInspectError(
            f"unsupported receipt schema {schema!r}",
            code="E_MEDLEY_INSPECT_SCHEMA",
        )
    selected = (
        item.get("selectedCatalogId")
        or item.get("selected_catalog_id")
        or item.get("selected_model_ref")
    )
    if not str(selected or "").strip():
        raise MedleyInspectError(
            "receipt needs a selected catalog id",
            code="E_MEDLEY_INSPECT_SCHEMA",
        )
    digest = (
        item.get("routeDigest")
        or item.get("route_digest")
        or item.get("route_receipt_digest")
    )
    digest_text = str(digest or "").strip()
    if not _DIGEST_RE.fullmatch(digest_text):
        raise MedleyInspectError(
            "receipt digest must be a 64-char hex SHA-256",
            code="E_MEDLEY_INSPECT_SCHEMA",
        )
    attempt = item.get("attempt")
    if type(attempt) is not int or attempt < 1:
        raise MedleyInspectError(
            "receipt attempt must be a positive integer",
            code="E_MEDLEY_INSPECT_SCHEMA",
        )
    return dict(item)


def _normalized_secret_key(key: str) -> str:
    return key.strip().lower().replace("-", "_")


def _reject_secrets(text: str, *, label: str) -> None:
    lower = text.lower()
    for needle in _VALUE_SECRET_NEEDLES:
        if needle in lower:
            raise MedleyInspectError(
                f"{label} contains forbidden material",
                code="E_MEDLEY_INSPECT_SECRET",
            )


def _reject_secrets_tree(value: Any, *, key: str | None = None) -> None:
    if key is not None and _normalized_secret_key(key) in _SECRET_KEYS:
        if isinstance(value, str) and value.strip():
            raise MedleyInspectError(
                "inspect contains forbidden material",
                code="E_MEDLEY_INSPECT_SECRET",
            )
        if value not in (None, "", [], {}):
            raise MedleyInspectError(
                "inspect contains forbidden material",
                code="E_MEDLEY_INSPECT_SECRET",
            )
    if isinstance(value, str):
        _reject_secrets(value, label="inspect")
    elif isinstance(value, Mapping):
        for nested_key, nested in value.items():
            _reject_secrets_tree(
                nested,
                key=str(nested_key) if nested_key is not None else None,
            )
    elif isinstance(value, list):
        for nested in value:
            _reject_secrets_tree(nested)


__all__ = [
    "INSPECT_ENV",
    "INSPECT_SCHEMA",
    "MedleyInspectDocument",
    "MedleyInspectError",
    "advertised_from_inspect",
    "apply_receipt_to_view_fields",
    "inspect_path_from_env",
    "load_inspect_document",
    "receipt_for_policy",
    "resolve_host_snapshot",
    "snapshot_from_inspect",
]
