"""OmO-specific static discovery extractors for parity completeness (#78-H)."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_nonempty_string,
    require_safe_id,
)

_SAFE_ENUM_VALUE = re.compile(r"[A-Za-z0-9][\w.-]*")


def _helpers():
    from omg_cli import parity_discovery as pd

    return pd._category_for_kind, pd._strip_ts_comments, pd._require_relative_posix


def _parse_zod_string_enum_values(source: str, *, export_name: str, label: str) -> list[str]:
    """Extract string members from ``export const ExportName = z.enum([...])``."""
    cleaned = _helpers()[1](source)
    marker = re.search(
        rf"export\s+const\s+{re.escape(export_name)}\s*=\s*z\.enum\s*\(\s*\[",
        cleaned,
    )
    if not marker:
        raise ContractValidationError(
            f"{label}: export const {export_name} = z.enum([...]) not found"
        )
    start = marker.end() - 1  # '['
    if cleaned[start] != "[":
        raise ContractValidationError(f"{label}: z.enum payload is not an array")
    depth = 0
    i = start
    while i < len(cleaned):
        ch = cleaned[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < len(cleaned):
                c = cleaned[i]
                if c == "\\" and i + 1 < len(cleaned):
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                body = cleaned[start + 1 : i]
                if "..." in body or re.search(r"\[\s*[^\]]+\s*\]", body):
                    raise ContractValidationError(
                        f"{label}: computed/spread enum members rejected"
                    )
                values: list[str] = []
                seen: set[str] = set()
                for m in re.finditer(r"(['\"])([^'\"]+)\1", body):
                    value = m.group(2)
                    if not _SAFE_ENUM_VALUE.fullmatch(value):
                        raise ContractValidationError(
                            f"{label}: invalid enum member {value!r}"
                        )
                    norm = value.lower()
                    if norm in seen:
                        raise ContractValidationError(
                            f"{label}: duplicate normalized enum member {value!r}"
                        )
                    seen.add(norm)
                    values.append(value)
                if not values:
                    raise ContractValidationError(f"{label}: empty z.enum")
                return values
        i += 1
    raise ContractValidationError(f"{label}: unterminated z.enum array")


def _enum_surfaces_from_values(
    *,
    registry_path: str,
    values: list[str],
    kind: str,
    surface_prefix: str,
    category_assignment: Mapping[str, str],
    reg_digest: str,
    emit_catalog: bool,
    catalog_kind: str,
) -> list[dict[str, Any]]:
    _category_for_kind, _, _ = _helpers()
    category = _category_for_kind(kind, category_assignment, label=registry_path)
    surfaces: list[dict[str, Any]] = []
    for value in values:
        surfaces.append(
            {
                "surface_id": f"{surface_prefix}.{value}",
                "kind": kind,
                "category": category,
                "source_path": registry_path,
                "anchor": f"{surface_prefix}:{value}",
                "content_digest": reg_digest,
            }
        )
    if emit_catalog:
        catalog_category = _category_for_kind(
            catalog_kind if catalog_kind in category_assignment else "catalog",
            category_assignment,
            label=registry_path,
        )
        surfaces.append(
            {
                "surface_id": f"catalog.{surface_prefix}",
                "kind": "catalog",
                "category": catalog_category,
                "source_path": registry_path,
                "anchor": f"catalog:{surface_prefix}",
                "content_digest": reg_digest,
            }
        )
    return surfaces


def extract_omo_zod_string_enum_v1(
    *,
    registry_path: str,
    registry_bytes: bytes,
    category_assignment: Mapping[str, str],
    file_digest: Callable[[bytes], str],
    options: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Parse an OmO zod string enum registry into surfaces (+ optional catalog)."""
    try:
        text = registry_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractValidationError(
            f"{registry_path} is not valid UTF-8: {exc}"
        ) from exc

    export_name = require_nonempty_string(
        options.get("export_name"), label=f"{registry_path}.options.export_name"
    )
    kind = require_nonempty_string(options.get("kind"), label=f"{registry_path}.options.kind")
    surface_prefix = require_safe_id(
        options.get("surface_prefix"), label=f"{registry_path}.options.surface_prefix"
    )
    emit_catalog = bool(options.get("emit_catalog", False))
    catalog_kind = str(options.get("catalog_kind", "catalog"))
    values = _parse_zod_string_enum_values(
        text, export_name=export_name, label=registry_path
    )
    reg_digest = file_digest(registry_bytes)
    surfaces = _enum_surfaces_from_values(
        registry_path=registry_path,
        values=values,
        kind=kind,
        surface_prefix=surface_prefix,
        category_assignment=category_assignment,
        reg_digest=reg_digest,
        emit_catalog=emit_catalog,
        catalog_kind=catalog_kind,
    )
    input_parts = [{"path": registry_path, "content_digest": reg_digest}]
    return surfaces, input_parts


def extract_omo_agent_names_schema_v1(
    *,
    registry_path: str,
    registry_bytes: bytes,
    category_assignment: Mapping[str, str],
    file_digest: Callable[[bytes], str],
    options: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Parse BuiltinAgentNameSchema + BuiltinSkillNameSchema from one file."""
    del options
    try:
        text = registry_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractValidationError(
            f"{registry_path} is not valid UTF-8: {exc}"
        ) from exc
    reg_digest = file_digest(registry_bytes)
    agent_values = _parse_zod_string_enum_values(
        text, export_name="BuiltinAgentNameSchema", label=registry_path
    )
    skill_values = _parse_zod_string_enum_values(
        text, export_name="BuiltinSkillNameSchema", label=registry_path
    )
    surfaces = []
    surfaces.extend(
        _enum_surfaces_from_values(
            registry_path=registry_path,
            values=agent_values,
            kind="agent",
            surface_prefix="agent",
            category_assignment=category_assignment,
            reg_digest=reg_digest,
            emit_catalog=True,
            catalog_kind="agent_catalog",
        )
    )
    surfaces.extend(
        _enum_surfaces_from_values(
            registry_path=registry_path,
            values=skill_values,
            kind="skill",
            surface_prefix="skill",
            category_assignment=category_assignment,
            reg_digest=reg_digest,
            emit_catalog=True,
            catalog_kind="catalog",
        )
    )
    input_parts = [{"path": registry_path, "content_digest": reg_digest}]
    return surfaces, input_parts


def extract_omo_command_tree_v1(
    *,
    registry_path: str,
    category_assignment: Mapping[str, str],
    pin_paths: set[str],
    file_digest: Callable[[bytes], str],
    read_blob: Callable[[str], bytes],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Enumerate ``{registry_path}/*.md`` (registry_path may contain slashes)."""
    _category_for_kind, _strip_ts_comments, _require_relative_posix = _helpers()
    del _strip_ts_comments
    root_dir = _require_relative_posix(registry_path, label="registry_path").rstrip("/")
    category = _category_for_kind("command", category_assignment, label=root_dir)
    prefix = root_dir + "/"
    surfaces: list[dict[str, Any]] = []
    input_parts: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in sorted(pin_paths):
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix) :]
        if "/" in rest or not rest.endswith(".md"):
            continue
        stem = rest[:-3]
        if not stem or not re.fullmatch(r"[A-Za-z0-9][\w-]*", stem):
            raise ContractValidationError(
                f"omo_command_tree_v1: invalid command stem in {path}"
            )
        norm = stem.lower()
        if norm in seen:
            raise ContractValidationError(
                f"omo_command_tree_v1: case-colliding command stem {stem}"
            )
        seen.add(norm)
        raw = read_blob(path)
        digest = file_digest(raw)
        surfaces.append(
            {
                "surface_id": f"command.{stem}",
                "kind": "command",
                "category": category,
                "source_path": path,
                "anchor": f"command:{stem}",
                "content_digest": digest,
            }
        )
        input_parts.append({"path": path, "content_digest": digest})
    if not surfaces:
        raise ContractValidationError(
            f"omo_command_tree_v1: no commands under {root_dir}/"
        )
    return surfaces, input_parts
