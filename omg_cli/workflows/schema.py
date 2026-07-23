"""Compiler and bounded JSON validation for ``repository-workflow/v1``."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.contracts.workflow_contract import validate_workflow_definition
from omg_cli.contracts.writer_chain import canonical_json_bytes, parse_canonical_json_bytes, sha256_hex


class WorkflowSchemaError(ValueError):
    """Definition, input, output or persisted workflow bytes are invalid."""


_JSON_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


def _validate_schema_shape(schema: Any, *, label: str) -> None:
    if not isinstance(schema, Mapping):
        raise WorkflowSchemaError(f"{label} schema must be an object")
    expected = schema.get("type")
    if expected is not None and (
        not isinstance(expected, str) or expected not in _JSON_SCHEMA_TYPES
    ):
        raise WorkflowSchemaError(f"{label} schema type is unsupported")
    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not all(
            isinstance(item, str) for item in required
        ):
            raise WorkflowSchemaError(f"{label} schema required must be a string array")
    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, Mapping):
            raise WorkflowSchemaError(f"{label} schema properties must be an object")
        for key, subschema in properties.items():
            if not isinstance(key, str):
                raise WorkflowSchemaError(f"{label} schema property names must be strings")
            _validate_schema_shape(subschema, label=f"{label}.{key}")
    if "items" in schema:
        _validate_schema_shape(schema["items"], label=f"{label} items")


def _load_source(source: Mapping[str, Any] | Path | str | bytes) -> dict[str, Any]:
    if isinstance(source, Mapping):
        # Canonical round-trip rejects non-JSON values and removes caller aliases.
        raw = canonical_json_bytes(dict(source))
    elif isinstance(source, Path):
        raw = source.read_bytes()
    elif isinstance(source, bytes):
        raw = source
    elif isinstance(source, str):
        candidate = Path(source)
        if "\n" not in source and len(source) < 4096 and candidate.is_file():
            raw = candidate.read_bytes()
        else:
            raw = source.encode("utf-8")
    else:  # pragma: no cover - public type guard
        raise WorkflowSchemaError("workflow source must be mapping, path, text or bytes")
    try:
        parsed = parse_canonical_json_bytes(raw)
    except Exception:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise WorkflowSchemaError(f"workflow source is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise WorkflowSchemaError("workflow definition must be an object")
    return parsed


def compile_workflow(source: Mapping[str, Any] | Path | str | bytes) -> dict[str, Any]:
    """Compile to validated canonical JSON data without executing host syntax."""
    parsed = _load_source(source)
    try:
        validated = validate_workflow_definition(parsed)
    except ContractValidationError as exc:
        raise WorkflowSchemaError(str(exc)) from exc
    compiled = parse_canonical_json_bytes(canonical_json_bytes(validated))
    _validate_schema_shape(compiled["input_schema"], label="workflow input")
    for stage in compiled["stages"]:
        _validate_schema_shape(
            stage["output_schema"], label=f"stage {stage['id']} output"
        )
        _validate_schema_shape(
            stage["artifact_contract"]["schema"],
            label=f"stage {stage['id']} artifact",
        )
    return compiled


def _validate_json_schema(value: Any, schema: Mapping[str, Any], *, label: str) -> None:
    _validate_schema_shape(schema, label=label)
    expected = schema.get("type")
    type_map: dict[str, type[Any] | tuple[type[Any], ...]] = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    if expected in type_map:
        if not isinstance(value, type_map[expected]) or (
            expected in {"integer", "number"} and isinstance(value, bool)
        ):
            raise WorkflowSchemaError(f"{label} must be {expected}")
    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise WorkflowSchemaError(f"{label} schema required must be a string array")
        missing = sorted(set(required) - set(value))
        if missing:
            raise WorkflowSchemaError(f"{label} missing required fields: {missing}")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                subschema = properties[key]
                if not isinstance(subschema, Mapping):
                    raise WorkflowSchemaError(f"{label}.{key} schema must be an object")
                _validate_json_schema(item, subschema, label=f"{label}.{key}")
            elif schema.get("additionalProperties") is False:
                raise WorkflowSchemaError(f"{label} has undeclared field: {key}")
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(value):
            _validate_json_schema(item, schema["items"], label=f"{label}[{index}]")


def validate_workflow_input(
    definition: Mapping[str, Any], workflow_input: Mapping[str, Any]
) -> dict[str, Any]:
    compiled = compile_workflow(definition)
    if not isinstance(workflow_input, Mapping):
        raise WorkflowSchemaError("workflow input must be an object")
    canonical = parse_canonical_json_bytes(canonical_json_bytes(dict(workflow_input)))
    _validate_json_schema(canonical, compiled["input_schema"], label="workflow input")
    return canonical


def validate_stage_output(stage: Mapping[str, Any], output: Any) -> Any:
    schema = stage.get("output_schema")
    if not isinstance(schema, Mapping):
        raise WorkflowSchemaError("stage output_schema must be an object")
    canonical = parse_canonical_json_bytes(canonical_json_bytes(output))
    _validate_json_schema(canonical, schema, label=f"stage {stage.get('id')} output")
    return canonical


def validate_json_value(
    value: Any, schema: Mapping[str, Any], *, label: str
) -> Any:
    """Return canonical JSON after validating it against a workflow schema."""
    canonical = parse_canonical_json_bytes(canonical_json_bytes(value))
    _validate_json_schema(canonical, schema, label=label)
    return canonical


def input_digest(workflow_input: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes(dict(workflow_input)))


__all__ = [
    "WorkflowSchemaError",
    "compile_workflow",
    "input_digest",
    "validate_json_value",
    "validate_stage_output",
    "validate_workflow_input",
]
