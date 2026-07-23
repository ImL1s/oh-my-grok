"""Deterministic, redacted project fact memory with lock-safe import/export."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
)
from omg_cli.contracts.state_schemas import require_iso8601
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
    sha256_hex,
)
from omg_cli.redaction import redact_value


def memory_path(root: Path | str) -> Path:
    return Path(root).resolve() / ".omg" / "memory" / "facts.json"


def _empty_store() -> dict[str, Any]:
    return {"store_kind": "project_fact_memory", "schema_version": 1, "facts": []}


def _validate_store(value: Mapping[str, Any]) -> dict[str, Any]:
    store = dict(value)
    if set(store) != {"store_kind", "schema_version", "facts"}:
        raise ValueError("project memory keys mismatch")
    if store["store_kind"] != "project_fact_memory" or store["schema_version"] != 1:
        raise ValueError("project memory header mismatch")
    if not isinstance(store["facts"], list):
        raise ValueError("project memory facts must be an array")
    seen: set[str] = set()
    for fact in store["facts"]:
        if not isinstance(fact, dict) or set(fact) != {
            "key",
            "value",
            "source",
            "updated_at",
        }:
            raise ValueError("project fact keys mismatch")
        if not isinstance(fact["key"], str) or not fact["key"]:
            raise ValueError("project fact key must be non-empty")
        if fact["key"] in seen:
            raise ValueError("project fact keys must be unique")
        seen.add(fact["key"])
        if not isinstance(fact["value"], str):
            raise ValueError("project fact value must be text")
        if fact["source"] not in {"user", "scanner", "import"}:
            raise ValueError("project fact source is invalid")
        require_iso8601(fact["updated_at"], label="updated_at")
    ordered = sorted(store["facts"], key=lambda row: row["key"].encode("utf-8"))
    if store["facts"] != ordered:
        raise ValueError("project facts are not in canonical order")
    return store


def _quarantine_corrupt(path: Path) -> None:
    body = path.read_bytes()
    quarantine = path.with_name(f"facts.corrupt-{sha256_hex(body)}.json")
    if quarantine.exists():
        if quarantine.read_bytes() != body:
            raise ValueError("project memory quarantine hash collision")
        path.unlink()
        return
    os.replace(path, quarantine)
    os.chmod(quarantine, DATA_FILE_MODE)


def _load_unlocked(path: Path, *, quarantine: bool = True) -> dict[str, Any]:
    if not path.exists():
        return _empty_store()
    try:
        parsed = parse_canonical_json_bytes(path.read_bytes())
        if not isinstance(parsed, dict):
            raise ValueError("project memory must be an object")
        return _validate_store(parsed)
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        if not quarantine:
            raise
        _quarantine_corrupt(path)
        return _empty_store()


def _write_unlocked(path: Path, store: Mapping[str, Any]) -> dict[str, Any]:
    validated = _validate_store(store)
    atomic_write_bytes(
        path,
        canonical_json_bytes(validated),
        mode=DATA_FILE_MODE,
        replace=True,
    )
    return validated


def upsert_fact(
    root: Path | str,
    *,
    key: str,
    value: str,
    source: str,
    updated_at: str,
) -> dict[str, Any]:
    if not isinstance(key, str) or not key.strip():
        raise ValueError("project fact key required")
    if not isinstance(value, str):
        raise ValueError("project fact value must be text")
    if source not in {"user", "scanner", "import"}:
        raise ValueError("project fact source is invalid")
    require_iso8601(updated_at, label="updated_at")
    redacted = redact_value(value)
    if not isinstance(redacted, str):  # pragma: no cover - string input
        raise ValueError("redacted project fact must remain text")
    path = memory_path(root)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        store = _load_unlocked(path)
        rows = {row["key"]: dict(row) for row in store["facts"]}
        current = rows.get(key.strip())
        if current is not None and current["source"] == "user" and source != "user":
            return current
        fact = {
            "key": key.strip(),
            "value": redacted,
            "source": source,
            "updated_at": updated_at,
        }
        rows[fact["key"]] = fact
        _write_unlocked(
            path,
            {
                **_empty_store(),
                "facts": sorted(rows.values(), key=lambda row: row["key"].encode("utf-8")),
            },
        )
        return fact


def export_memory(root: Path | str) -> dict[str, Any]:
    path = memory_path(root)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        store = _load_unlocked(path)
        if path.exists():
            os.chmod(path, DATA_FILE_MODE)
        return store


def import_memory(root: Path | str, bundle: Mapping[str, Any]) -> dict[str, Any]:
    incoming = _validate_store(bundle)
    path = memory_path(root)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        current = _load_unlocked(path)
        rows = {row["key"]: dict(row) for row in current["facts"]}
        for raw in incoming["facts"]:
            fact = dict(raw)
            fact["value"] = str(redact_value(fact["value"]))
            existing = rows.get(fact["key"])
            if existing is not None and existing["source"] == "user" and fact["source"] != "user":
                continue
            if existing is None or (
                fact["updated_at"], fact["source"], canonical_json_bytes(fact)
            ) >= (
                existing["updated_at"],
                existing["source"],
                canonical_json_bytes(existing),
            ):
                rows[fact["key"]] = fact
        merged = {
            **_empty_store(),
            "facts": sorted(rows.values(), key=lambda row: row["key"].encode("utf-8")),
        }
        return _write_unlocked(path, merged)


def rescan_memory(
    root: Path | str,
    facts: Iterable[Mapping[str, Any]],
    *,
    observed_at: str,
) -> dict[str, Any]:
    require_iso8601(observed_at, label="observed_at")
    rows = sorted(
        (dict(fact) for fact in facts),
        key=lambda row: str(row.get("key", "")).encode("utf-8"),
    )
    for row in rows:
        upsert_fact(
            root,
            key=str(row.get("key", "")),
            value=str(row.get("value", "")),
            source="scanner",
            updated_at=observed_at,
        )
    return export_memory(root)


def search_memory(root: Path | str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("project memory query required")
    bounded_limit = max(1, min(int(limit), 100))
    needle = query.casefold()
    return [
        row
        for row in export_memory(root)["facts"]
        if needle in row["key"].casefold() or needle in row["value"].casefold()
    ][:bounded_limit]


__all__ = [
    "export_memory",
    "import_memory",
    "memory_path",
    "rescan_memory",
    "search_memory",
    "upsert_fact",
]
