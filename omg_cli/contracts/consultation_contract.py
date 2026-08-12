"""Fail-closed Consultation/Council V1 documents.

Slice A admits documents only.  Parsers never probe PATH, binaries, or the
network, and they never claim support or qualification.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from .advisor_contract import (
    ADVISOR_FLAG_KEYS,
    ADVISOR_READ_ONLY_STATES,
    CANONICAL_HARNESS_IDS,
    reject_advisor_forbidden_keys,
    validate_advisor_taxonomy,
)
from .state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_integer,
    require_iso8601,
    require_nonempty_string,
    require_object,
    require_safe_id,
    require_sha256,
)
from .writer_chain import canonical_json_bytes, sha256_hex


ARTIFACT_KINDS = ("prompt", "response", "stderr", "attachment", "descriptor")
REQUESTED_OUTPUTS = ("text", "structured_verdict_v1")
PROMPT_TRANSPORTS = ("stdin", "prompt_file", "argv_value", "none")
EXIT_CLASSES = ("ok", "usage", "missing", "timeout", "cancelled", "error")
NONTERMINAL_STATUSES = frozenset({"queued", "running"})
CONSULTATION_STATUSES = frozenset(
    {
        "queued",
        "running",
        "succeeded",
        "failed",
        "blocked",
        "unsupported",
        "timed_out",
        "cancelled",
    }
)
COUNCIL_STATUSES = CONSULTATION_STATUSES | {"mixed"}
CONFLICT_POLICIES = ("preserve_dissent",)

ARTIFACT_DESCRIPTOR_KEYS = ("kind", "relative_path", "sha256", "byte_length")
WORKSPACE_DESCRIPTOR_KEYS = ("kind", "relative_path")
TYPED_REASON_KEYS = ("code", "message")

CONSULTATION_REQUEST_V1_KEYS = (
    "schema_version",
    "consultation_id",
    "runtime_kind",
    "purpose",
    "lifecycle",
    "harness_id",
    "original_task_digest",
    "prompt_artifact",
    "role_id",
    "role_prompt_digest",
    "requested_model",
    "requested_output",
    "cwd_descriptor",
    "attachment_descriptors",
    "run_id",
    "timeout_s",
    "max_output_bytes",
    "attempt_budget",
    "policy_digest",
)
CONSULTATION_ATTEMPT_V1_KEYS = (
    "schema_version",
    "attempt",
    "harness_id",
    "harness_version",
    "platform",
    "read_only_qualification",
    "prompt_transport",
    "job_id",
    "started_at",
    "terminal_at",
    "status",
    "exit_class",
    "output_present",
    "output_truncated",
    "response_digest",
    "receipt_digest",
)
CONSULTATION_RECEIPT_V1_KEYS = (
    "schema_version",
    "consultation_id",
    "request_digest",
    "runtime_kind",
    "purpose",
    "lifecycle",
    "harness_id",
    "attempt",
    "status",
    "read_only_qualification",
    "role_id",
    "requested_model",
    "selected_model",
    "job_id",
    "started_at",
    "terminal_at",
    "exit_class",
    "artifact_descriptors",
    "private_transcript_available",
    "response_digest",
    "authoritative",
    "auto_apply",
    "worker_eligible",
)
CONSULTATION_VIEW_V1_KEYS = (
    "schema_version",
    "consultation_id",
    "runtime_kind",
    "purpose",
    "lifecycle",
    "harness_id",
    "role_id",
    "status",
    "attempt",
    "read_only_qualification",
    "model",
    "job_id",
    "started_at",
    "terminal_at",
    "output_present",
    "output_truncated",
    "receipt_digest",
    "council_id",
    "reasons",
    "authoritative",
    "auto_apply",
    "worker_eligible",
)
COUNCIL_REQUEST_V1_KEYS = (
    "schema_version",
    "council_id",
    "advisor_requests",
    "concurrency_limit",
    "timeout_s",
    "minimum_successes",
    "synthesis_mode",
    "conflict_policy",
    "policy_digest",
)
COUNCIL_RECEIPT_V1_KEYS = (
    "schema_version",
    "council_id",
    "request_digest",
    "lane_receipt_digests",
    "success_count",
    "minimum_successes",
    "status",
    "synthesis_mode",
    "synthesis_receipt_digest",
    "authoritative",
    "auto_apply",
    "worker_eligible",
)
COUNCIL_VIEW_V1_KEYS = (
    "schema_version",
    "council_id",
    "status",
    "success_count",
    "minimum_successes",
    "lane_count",
    "receipt_digest",
    "reasons",
    "authoritative",
    "auto_apply",
    "worker_eligible",
)

_MAX_ARTIFACTS = 32
_MAX_REASONS = 16
_MAX_COUNCIL_LANES = 8
_MAX_REASON_CHARS = 240
_STATE_ROOT = ".omg/state"
_SECRET_EXACT_MARKERS = (
    "eyJ",
    "-----BEGIN",
    "Bearer ",
    "/home/",
    "/Users/",
    "/private/",
    "C:\\",
)
_SECRET_CI_MARKERS = ("api_key=", "password=")


def _require_bool(value: Any, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{label} must be a boolean")
    return value


def _require_positive_whole_number(value: Any, *, label: str) -> int:
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


def _require_schema_v1(value: Any) -> int:
    version = require_integer(value, label="schema_version", minimum=1)
    if version != 1:
        raise ContractValidationError(
            f"unsupported schema_version={version}; expected 1"
        )
    return version


def _optional_safe_id(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    return require_safe_id(value, label=label)


def _optional_sha256(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    return require_sha256(value, label=label)


def _optional_iso8601(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    return require_iso8601(value, label=label)


def _require_canonical_harness_id(value: Any, *, label: str) -> str:
    harness_id = require_safe_id(value, label=label)
    if harness_id not in CANONICAL_HARNESS_IDS:
        raise ContractValidationError(f"unknown harness_id {harness_id!r}")
    return harness_id


def _require_model_token(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    text = require_nonempty_string(value, label=label)
    if text != text.strip():
        raise ContractValidationError(f"{label} must not have surrounding whitespace")
    if text.startswith("-"):
        raise ContractValidationError(f"{label} must not start with '-'")
    if any(char.isspace() for char in text):
        raise ContractValidationError(f"{label} must not contain whitespace")
    return text


def _require_repo_relative_posix(
    value: Any, *, label: str, allow_workspace_dot: bool = False
) -> str:
    text = require_nonempty_string(value, label=label)
    if text != text.strip():
        raise ContractValidationError(f"{label} must not have surrounding whitespace")
    if allow_workspace_dot and text == ".":
        return text
    if any(marker in text for marker in ("\\", "~", ":")):
        raise ContractValidationError(
            f"{label} must be a repo-relative POSIX path; "
            "absolute, home, and private paths are rejected"
        )
    pure = PurePosixPath(text)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or str(pure) != text
        or text in {"", "."}
    ):
        raise ContractValidationError(
            f"{label} must be a repo-relative POSIX path; "
            "absolute, home, and private paths are rejected"
        )
    if text == _STATE_ROOT or text.startswith(_STATE_ROOT + "/"):
        raise ContractValidationError(
            f"{label} must not point at {_STATE_ROOT}"
        )
    return text


def _require_enum(value: Any, *, label: str, allowed: Sequence[str]) -> str:
    text = require_nonempty_string(value, label=label)
    if text not in allowed:
        raise ContractValidationError(f"unknown {label} {text!r}")
    return text


def _require_synthesis_mode(value: Any, *, label: str) -> str:
    text = require_nonempty_string(value, label=label)
    if text == "none":
        return text
    if text.startswith("native:"):
        require_safe_id(text[len("native:") :], label=f"{label} native id")
        return text
    if text.startswith("advisor:"):
        suffix = text[len("advisor:") :]
        harness_id = require_safe_id(suffix, label=f"{label} advisor harness")
        if harness_id not in CANONICAL_HARNESS_IDS:
            raise ContractValidationError(
                f"{label} advisor harness must be a canonical harness id"
            )
        return text
    raise ContractValidationError(f"{label} is not a valid synthesis_mode")


def _require_exit_class(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    return _require_enum(value, label=label, allowed=EXIT_CLASSES)


def _enforce_terminal_rules(
    *, status: str, terminal_at: str | None, exit_class: str | None
) -> None:
    if status in NONTERMINAL_STATUSES:
        if terminal_at is not None:
            raise ContractValidationError(
                "terminal_at must be null while queued or running"
            )
        if exit_class is not None:
            raise ContractValidationError(
                "exit_class must be null while queued or running"
            )
        return
    if terminal_at is None:
        raise ContractValidationError("terminal_at is required for a terminal status")


def _require_false_flags(payload: Mapping[str, Any]) -> dict[str, bool]:
    flags: dict[str, bool] = {}
    for flag in ADVISOR_FLAG_KEYS:
        value = _require_bool(payload[flag], label=flag)
        if value:
            raise ContractValidationError(
                f"{flag} cannot be true for an advisor; advisors are never workers"
            )
        flags[flag] = False
    return flags


def _begin_parse(
    raw: Mapping[str, Any] | None,
    *,
    label: str,
    required: Sequence[str],
    schema: bool = False,
    secret_scan: bool = False,
) -> dict[str, Any]:
    payload = require_object(raw, label=label)
    reject_advisor_forbidden_keys(payload)
    require_exact_keys(payload, required=set(required), label=label)
    if schema:
        _require_schema_v1(payload["schema_version"])
    if secret_scan:
        _reject_secrets(payload, label=label)
    return payload


def _walk_secret_strings(value: Any, *, label: str) -> None:
    if isinstance(value, str):
        lowered = value.casefold()
        if any(marker in value for marker in _SECRET_EXACT_MARKERS) or any(
            marker in lowered for marker in _SECRET_CI_MARKERS
        ):
            raise ContractValidationError(
                f"{label} contains a forbidden secret or private-path marker"
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _walk_secret_strings(item, label=f"{label}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _walk_secret_strings(item, label=f"{label}[{index}]")


def _reject_secrets(payload: Mapping[str, Any], *, label: str) -> None:
    _walk_secret_strings(payload, label=label)


def _parse_taxonomy(payload: Mapping[str, Any]) -> dict[str, Any]:
    return validate_advisor_taxonomy(
        {
            "runtime_kind": payload["runtime_kind"],
            "purpose": payload["purpose"],
            "lifecycle": payload["lifecycle"],
        }
    )


def _parse_object_list(
    value: Any,
    *,
    label: str,
    parser: Any,
    maximum: int,
    minimum: int = 0,
) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError(f"{label} must be an array")
    if len(value) > maximum:
        raise ContractValidationError(f"{label} exceeds maximum of {maximum}")
    if len(value) < minimum:
        raise ContractValidationError(f"{label} must contain at least {minimum} item(s)")
    return [parser(item) for item in value]


def _parse_artifact_list(value: Any, *, label: str) -> list[dict[str, Any]]:
    parsed = _parse_object_list(
        value, label=label, parser=parse_artifact_descriptor, maximum=_MAX_ARTIFACTS
    )
    paths = [item["relative_path"] for item in parsed]
    if len(paths) != len(set(paths)):
        raise ContractValidationError(f"{label} relative_path values must be unique")
    return parsed


def _parse_reason_list(value: Any, *, label: str) -> list[dict[str, Any]]:
    return _parse_object_list(
        value, label=label, parser=parse_typed_reason, maximum=_MAX_REASONS
    )


def _parse_sha256_list(
    value: Any, *, label: str, minimum: int, maximum: int
) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError(f"{label} must be an array")
    if not (minimum <= len(value) <= maximum):
        raise ContractValidationError(
            f"{label} length must be {minimum}..{maximum}"
        )
    result = [require_sha256(item, label=f"{label}[]") for item in value]
    if len(result) != len(set(result)):
        raise ContractValidationError(f"{label} must not contain duplicates")
    return result


def parse_artifact_descriptor(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = _begin_parse(
        raw, label="artifact descriptor", required=ARTIFACT_DESCRIPTOR_KEYS
    )
    kind = _require_enum(payload["kind"], label="kind", allowed=ARTIFACT_KINDS)
    return {
        "kind": kind,
        "relative_path": _require_repo_relative_posix(
            payload["relative_path"], label="relative_path"
        ),
        "sha256": require_sha256(payload["sha256"], label="sha256"),
        "byte_length": require_integer(
            payload["byte_length"], label="byte_length", minimum=0
        ),
    }


def parse_workspace_descriptor(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = _begin_parse(
        raw, label="workspace descriptor", required=WORKSPACE_DESCRIPTOR_KEYS
    )
    kind = require_nonempty_string(payload["kind"], label="kind")
    if kind != "repository":
        raise ContractValidationError('workspace kind must be "repository"')
    return {
        "kind": "repository",
        "relative_path": _require_repo_relative_posix(
            payload["relative_path"],
            label="relative_path",
            allow_workspace_dot=True,
        ),
    }


def parse_typed_reason(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = _begin_parse(raw, label="typed reason", required=TYPED_REASON_KEYS)
    message = require_nonempty_string(payload["message"], label="message")
    if len(message) > _MAX_REASON_CHARS:
        raise ContractValidationError("message exceeds 240 characters")
    return {
        "code": require_safe_id(payload["code"], label="code"),
        "message": message,
    }


def parse_consultation_request_v1(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = _begin_parse(
        raw,
        label="consultation request",
        required=CONSULTATION_REQUEST_V1_KEYS,
        schema=True,
    )
    taxonomy = _parse_taxonomy(payload)
    prompt_artifact = parse_artifact_descriptor(payload["prompt_artifact"])
    if prompt_artifact["kind"] != "prompt":
        raise ContractValidationError('prompt_artifact kind must be "prompt"')
    return {
        "schema_version": 1,
        "consultation_id": require_safe_id(
            payload["consultation_id"], label="consultation_id"
        ),
        "runtime_kind": taxonomy["runtime_kind"],
        "purpose": taxonomy["purpose"],
        "lifecycle": taxonomy["lifecycle"],
        "harness_id": _require_canonical_harness_id(
            payload["harness_id"], label="harness_id"
        ),
        "original_task_digest": require_sha256(
            payload["original_task_digest"], label="original_task_digest"
        ),
        "prompt_artifact": prompt_artifact,
        "role_id": _optional_safe_id(payload["role_id"], label="role_id"),
        "role_prompt_digest": _optional_sha256(
            payload["role_prompt_digest"], label="role_prompt_digest"
        ),
        "requested_model": _require_model_token(
            payload["requested_model"], label="requested_model"
        ),
        "requested_output": _require_enum(
            payload["requested_output"],
            label="requested_output",
            allowed=REQUESTED_OUTPUTS,
        ),
        "cwd_descriptor": parse_workspace_descriptor(payload["cwd_descriptor"]),
        "attachment_descriptors": _parse_artifact_list(
            payload["attachment_descriptors"], label="attachment_descriptors"
        ),
        "run_id": _optional_safe_id(payload["run_id"], label="run_id"),
        "timeout_s": _require_positive_whole_number(
            payload["timeout_s"], label="timeout_s"
        ),
        "max_output_bytes": require_integer(
            payload["max_output_bytes"], label="max_output_bytes", minimum=1
        ),
        "attempt_budget": require_integer(
            payload["attempt_budget"], label="attempt_budget", minimum=1
        ),
        "policy_digest": require_sha256(
            payload["policy_digest"], label="policy_digest"
        ),
    }


def parse_consultation_attempt_v1(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = _begin_parse(
        raw,
        label="consultation attempt",
        required=CONSULTATION_ATTEMPT_V1_KEYS,
        schema=True,
    )
    if payload["harness_version"] is not None:
        raise ContractValidationError("harness_version must be null")
    platform = require_nonempty_string(payload["platform"], label="platform")
    if platform != "unspecified":
        raise ContractValidationError('platform must be "unspecified"')
    status = _require_enum(
        payload["status"], label="status", allowed=tuple(sorted(CONSULTATION_STATUSES))
    )
    terminal_at = _optional_iso8601(payload["terminal_at"], label="terminal_at")
    exit_class = _require_exit_class(payload["exit_class"], label="exit_class")
    _enforce_terminal_rules(
        status=status, terminal_at=terminal_at, exit_class=exit_class
    )
    return {
        "schema_version": 1,
        "attempt": require_integer(payload["attempt"], label="attempt", minimum=1),
        "harness_id": _require_canonical_harness_id(
            payload["harness_id"], label="harness_id"
        ),
        "harness_version": None,
        "platform": "unspecified",
        "read_only_qualification": _require_enum(
            payload["read_only_qualification"],
            label="read_only_qualification",
            allowed=ADVISOR_READ_ONLY_STATES,
        ),
        "prompt_transport": _require_enum(
            payload["prompt_transport"],
            label="prompt_transport",
            allowed=PROMPT_TRANSPORTS,
        ),
        "job_id": _optional_safe_id(payload["job_id"], label="job_id"),
        "started_at": require_iso8601(payload["started_at"], label="started_at"),
        "terminal_at": terminal_at,
        "status": status,
        "exit_class": exit_class,
        "output_present": _require_bool(
            payload["output_present"], label="output_present"
        ),
        "output_truncated": _require_bool(
            payload["output_truncated"], label="output_truncated"
        ),
        "response_digest": _optional_sha256(
            payload["response_digest"], label="response_digest"
        ),
        "receipt_digest": require_sha256(
            payload["receipt_digest"], label="receipt_digest"
        ),
    }


def parse_consultation_receipt_v1(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = _begin_parse(
        raw,
        label="consultation receipt",
        required=CONSULTATION_RECEIPT_V1_KEYS,
        schema=True,
        secret_scan=True,
    )
    taxonomy = _parse_taxonomy(payload)
    flags = _require_false_flags(payload)
    status = _require_enum(
        payload["status"], label="status", allowed=tuple(sorted(CONSULTATION_STATUSES))
    )
    terminal_at = _optional_iso8601(payload["terminal_at"], label="terminal_at")
    exit_class = _require_exit_class(payload["exit_class"], label="exit_class")
    _enforce_terminal_rules(
        status=status, terminal_at=terminal_at, exit_class=exit_class
    )
    parsed = {
        "schema_version": 1,
        "consultation_id": require_safe_id(
            payload["consultation_id"], label="consultation_id"
        ),
        "request_digest": require_sha256(
            payload["request_digest"], label="request_digest"
        ),
        "runtime_kind": taxonomy["runtime_kind"],
        "purpose": taxonomy["purpose"],
        "lifecycle": taxonomy["lifecycle"],
        "harness_id": _require_canonical_harness_id(
            payload["harness_id"], label="harness_id"
        ),
        "attempt": require_integer(payload["attempt"], label="attempt", minimum=1),
        "status": status,
        "read_only_qualification": _require_enum(
            payload["read_only_qualification"],
            label="read_only_qualification",
            allowed=ADVISOR_READ_ONLY_STATES,
        ),
        "role_id": _optional_safe_id(payload["role_id"], label="role_id"),
        "requested_model": _require_model_token(
            payload["requested_model"], label="requested_model"
        ),
        "selected_model": _require_model_token(
            payload["selected_model"], label="selected_model"
        ),
        "job_id": _optional_safe_id(payload["job_id"], label="job_id"),
        "started_at": require_iso8601(payload["started_at"], label="started_at"),
        "terminal_at": terminal_at,
        "exit_class": exit_class,
        "artifact_descriptors": _parse_artifact_list(
            payload["artifact_descriptors"], label="artifact_descriptors"
        ),
        "private_transcript_available": _require_bool(
            payload["private_transcript_available"],
            label="private_transcript_available",
        ),
        "response_digest": _optional_sha256(
            payload["response_digest"], label="response_digest"
        ),
        "authoritative": flags["authoritative"],
        "auto_apply": flags["auto_apply"],
        "worker_eligible": flags["worker_eligible"],
    }
    return parsed


def parse_consultation_view_v1(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = _begin_parse(
        raw,
        label="consultation view",
        required=CONSULTATION_VIEW_V1_KEYS,
        schema=True,
        secret_scan=True,
    )
    taxonomy = _parse_taxonomy(payload)
    flags = _require_false_flags(payload)
    status = _require_enum(
        payload["status"], label="status", allowed=tuple(sorted(CONSULTATION_STATUSES))
    )
    terminal_at = _optional_iso8601(payload["terminal_at"], label="terminal_at")
    _enforce_terminal_rules(status=status, terminal_at=terminal_at, exit_class=None)
    parsed = {
        "schema_version": 1,
        "consultation_id": require_safe_id(
            payload["consultation_id"], label="consultation_id"
        ),
        "runtime_kind": taxonomy["runtime_kind"],
        "purpose": taxonomy["purpose"],
        "lifecycle": taxonomy["lifecycle"],
        "harness_id": _require_canonical_harness_id(
            payload["harness_id"], label="harness_id"
        ),
        "role_id": _optional_safe_id(payload["role_id"], label="role_id"),
        "status": status,
        "attempt": require_integer(payload["attempt"], label="attempt", minimum=1),
        "read_only_qualification": _require_enum(
            payload["read_only_qualification"],
            label="read_only_qualification",
            allowed=ADVISOR_READ_ONLY_STATES,
        ),
        "model": _require_model_token(payload["model"], label="model"),
        "job_id": _optional_safe_id(payload["job_id"], label="job_id"),
        "started_at": require_iso8601(payload["started_at"], label="started_at"),
        "terminal_at": terminal_at,
        "output_present": _require_bool(
            payload["output_present"], label="output_present"
        ),
        "output_truncated": _require_bool(
            payload["output_truncated"], label="output_truncated"
        ),
        "receipt_digest": require_sha256(
            payload["receipt_digest"], label="receipt_digest"
        ),
        "council_id": _optional_safe_id(payload["council_id"], label="council_id"),
        "reasons": _parse_reason_list(payload["reasons"], label="reasons"),
        "authoritative": flags["authoritative"],
        "auto_apply": flags["auto_apply"],
        "worker_eligible": flags["worker_eligible"],
    }
    return parsed


def consultation_view_from_receipt(
    receipt: Mapping[str, Any],
    *,
    attempt: Any,
    council_id: str | None = None,
    reasons: Sequence[Any] = (),
) -> dict[str, Any]:
    """Project allowlisted receipt facts into ConsultationViewV1."""

    parsed_receipt = parse_consultation_receipt_v1(receipt)
    if isinstance(attempt, Mapping):
        parsed_attempt = parse_consultation_attempt_v1(attempt)
        attempt_number = parsed_attempt["attempt"]
        output_present = parsed_attempt["output_present"]
        output_truncated = parsed_attempt["output_truncated"]
    else:
        attempt_number = require_integer(attempt, label="attempt", minimum=1)
        output_present = parsed_receipt["response_digest"] is not None
        output_truncated = False
    model = parsed_receipt["selected_model"]
    if model is None:
        model = parsed_receipt["requested_model"]
    view = {
        "schema_version": 1,
        "consultation_id": parsed_receipt["consultation_id"],
        "runtime_kind": parsed_receipt["runtime_kind"],
        "purpose": parsed_receipt["purpose"],
        "lifecycle": parsed_receipt["lifecycle"],
        "harness_id": parsed_receipt["harness_id"],
        "role_id": parsed_receipt["role_id"],
        "status": parsed_receipt["status"],
        "attempt": attempt_number,
        "read_only_qualification": parsed_receipt["read_only_qualification"],
        "model": model,
        "job_id": parsed_receipt["job_id"],
        "started_at": parsed_receipt["started_at"],
        "terminal_at": parsed_receipt["terminal_at"],
        "output_present": output_present,
        "output_truncated": output_truncated,
        "receipt_digest": consultation_receipt_digest(parsed_receipt),
        "council_id": council_id,
        "reasons": list(reasons),
        "authoritative": False,
        "auto_apply": False,
        "worker_eligible": False,
    }
    return parse_consultation_view_v1(view)


def parse_council_request_v1(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = _begin_parse(
        raw,
        label="council request",
        required=COUNCIL_REQUEST_V1_KEYS,
        schema=True,
    )
    advisor_requests = _parse_object_list(
        payload["advisor_requests"],
        label="advisor_requests",
        parser=parse_consultation_request_v1,
        maximum=_MAX_COUNCIL_LANES,
        minimum=1,
    )
    consultation_ids = [item["consultation_id"] for item in advisor_requests]
    if len(consultation_ids) != len(set(consultation_ids)):
        raise ContractValidationError("advisor_requests consultation_id values must be unique")
    harness_ids = [item["harness_id"] for item in advisor_requests]
    if len(harness_ids) != len(set(harness_ids)):
        raise ContractValidationError("advisor_requests harness_id values must be unique")
    concurrency_limit = require_integer(
        payload["concurrency_limit"], label="concurrency_limit", minimum=1
    )
    if concurrency_limit > _MAX_COUNCIL_LANES:
        raise ContractValidationError("concurrency_limit must be <= 8")
    minimum_successes = require_integer(
        payload["minimum_successes"], label="minimum_successes", minimum=1
    )
    if minimum_successes > len(advisor_requests):
        raise ContractValidationError(
            "minimum_successes must be <= len(advisor_requests)"
        )
    conflict_policy = require_nonempty_string(
        payload["conflict_policy"], label="conflict_policy"
    )
    if conflict_policy not in CONFLICT_POLICIES:
        raise ContractValidationError(
            'conflict_policy must be "preserve_dissent"'
        )
    return {
        "schema_version": 1,
        "council_id": require_safe_id(payload["council_id"], label="council_id"),
        "advisor_requests": advisor_requests,
        "concurrency_limit": concurrency_limit,
        "timeout_s": _require_positive_whole_number(
            payload["timeout_s"], label="timeout_s"
        ),
        "minimum_successes": minimum_successes,
        "synthesis_mode": _require_synthesis_mode(
            payload["synthesis_mode"], label="synthesis_mode"
        ),
        "conflict_policy": "preserve_dissent",
        "policy_digest": require_sha256(
            payload["policy_digest"], label="policy_digest"
        ),
    }


def parse_council_receipt_v1(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = _begin_parse(
        raw,
        label="council receipt",
        required=COUNCIL_RECEIPT_V1_KEYS,
        schema=True,
        secret_scan=True,
    )
    flags = _require_false_flags(payload)
    synthesis_mode = _require_synthesis_mode(
        payload["synthesis_mode"], label="synthesis_mode"
    )
    synthesis_receipt_digest = _optional_sha256(
        payload["synthesis_receipt_digest"], label="synthesis_receipt_digest"
    )
    if synthesis_mode == "none" and synthesis_receipt_digest is not None:
        raise ContractValidationError(
            "synthesis_receipt_digest must be null when synthesis_mode is none"
        )
    parsed = {
        "schema_version": 1,
        "council_id": require_safe_id(payload["council_id"], label="council_id"),
        "request_digest": require_sha256(
            payload["request_digest"], label="request_digest"
        ),
        "lane_receipt_digests": _parse_sha256_list(
            payload["lane_receipt_digests"],
            label="lane_receipt_digests",
            minimum=1,
            maximum=_MAX_COUNCIL_LANES,
        ),
        "success_count": require_integer(
            payload["success_count"], label="success_count", minimum=0
        ),
        "minimum_successes": require_integer(
            payload["minimum_successes"], label="minimum_successes", minimum=1
        ),
        "status": _require_enum(
            payload["status"], label="status", allowed=tuple(sorted(COUNCIL_STATUSES))
        ),
        "synthesis_mode": synthesis_mode,
        "synthesis_receipt_digest": synthesis_receipt_digest,
        "authoritative": flags["authoritative"],
        "auto_apply": flags["auto_apply"],
        "worker_eligible": flags["worker_eligible"],
    }
    return parsed


def parse_council_view_v1(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = _begin_parse(
        raw,
        label="council view",
        required=COUNCIL_VIEW_V1_KEYS,
        schema=True,
        secret_scan=True,
    )
    flags = _require_false_flags(payload)
    lane_count = require_integer(payload["lane_count"], label="lane_count", minimum=1)
    if lane_count > _MAX_COUNCIL_LANES:
        raise ContractValidationError("lane_count must be <= 8")
    parsed = {
        "schema_version": 1,
        "council_id": require_safe_id(payload["council_id"], label="council_id"),
        "status": _require_enum(
            payload["status"], label="status", allowed=tuple(sorted(COUNCIL_STATUSES))
        ),
        "success_count": require_integer(
            payload["success_count"], label="success_count", minimum=0
        ),
        "minimum_successes": require_integer(
            payload["minimum_successes"], label="minimum_successes", minimum=1
        ),
        "lane_count": lane_count,
        "receipt_digest": require_sha256(
            payload["receipt_digest"], label="receipt_digest"
        ),
        "reasons": _parse_reason_list(payload["reasons"], label="reasons"),
        "authoritative": flags["authoritative"],
        "auto_apply": flags["auto_apply"],
        "worker_eligible": flags["worker_eligible"],
    }
    return parsed


def consultation_request_digest(value: Mapping[str, Any]) -> str:
    payload = require_object(value, label="consultation request")
    return sha256_hex(canonical_json_bytes(payload))


def consultation_receipt_digest(value: Mapping[str, Any]) -> str:
    payload = require_object(value, label="consultation receipt")
    return sha256_hex(canonical_json_bytes(payload))


def consultation_view_digest(value: Mapping[str, Any]) -> str:
    payload = require_object(value, label="consultation view")
    return sha256_hex(canonical_json_bytes(payload))


def council_request_digest(value: Mapping[str, Any]) -> str:
    payload = require_object(value, label="council request")
    return sha256_hex(canonical_json_bytes(payload))


def council_receipt_digest(value: Mapping[str, Any]) -> str:
    payload = require_object(value, label="council receipt")
    return sha256_hex(canonical_json_bytes(payload))


__all__ = [
    "ARTIFACT_DESCRIPTOR_KEYS",
    "ARTIFACT_KINDS",
    "CONSULTATION_ATTEMPT_V1_KEYS",
    "CONSULTATION_RECEIPT_V1_KEYS",
    "CONSULTATION_REQUEST_V1_KEYS",
    "CONSULTATION_STATUSES",
    "CONSULTATION_VIEW_V1_KEYS",
    "COUNCIL_RECEIPT_V1_KEYS",
    "COUNCIL_REQUEST_V1_KEYS",
    "COUNCIL_STATUSES",
    "COUNCIL_VIEW_V1_KEYS",
    "EXIT_CLASSES",
    "PROMPT_TRANSPORTS",
    "REQUESTED_OUTPUTS",
    "TYPED_REASON_KEYS",
    "WORKSPACE_DESCRIPTOR_KEYS",
    "consultation_receipt_digest",
    "consultation_request_digest",
    "consultation_view_digest",
    "consultation_view_from_receipt",
    "council_receipt_digest",
    "council_request_digest",
    "parse_artifact_descriptor",
    "parse_consultation_attempt_v1",
    "parse_consultation_receipt_v1",
    "parse_consultation_request_v1",
    "parse_consultation_view_v1",
    "parse_council_receipt_v1",
    "parse_council_request_v1",
    "parse_council_view_v1",
    "parse_typed_reason",
    "parse_workspace_descriptor",
]
