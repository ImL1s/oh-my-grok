"""Fail-closed proposal capture and evidence validation primitives.

Model/host output is never authoritative by itself.  The supported path is:

``host JSON -> run/invocation-scoped proposal -> validation -> CLI stamp``.

The helpers in this module deliberately keep the Grok host envelope separate
from the JSON object returned in its ``text`` field.  A model-produced
``writer: omg-cli`` value is rejected rather than interpreted as authority.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


CLI_WRITER = "omg-cli"
PROPOSALS_REL = Path(".omg") / "artifacts" / "proposals"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STAMP_CAPABILITY = object()


class EvidenceError(ValueError):
    """Raised when a proposal or evidence binding is malformed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    body = (
        json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_write_bytes(path, body)


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def sha256_file(path: Path | str) -> str:
    return sha256_bytes(Path(path).read_bytes())


def validate_identifier(value: str, *, label: str) -> str:
    normalized = (value or "").strip()
    if not normalized or not _IDENTIFIER_RE.fullmatch(normalized):
        raise EvidenceError(
            f"invalid {label} {value!r}; expected a safe identifier "
            "([A-Za-z0-9][A-Za-z0-9._-]{0,127})"
        )
    return normalized


def proposals_root(root: Path | str) -> Path:
    return Path(root).resolve() / PROPOSALS_REL


def proposal_dir(root: Path | str, run_id: str, invocation_id: str) -> Path:
    """Return the only directory in which this invocation may place proposals.

    Identifier validation prevents traversal.  Existing symlink components are
    refused, even if they happen to resolve back inside the repository, so a
    proposal cannot redirect a later CLI write.
    """

    run_id = validate_identifier(run_id, label="run_id")
    invocation_id = validate_identifier(invocation_id, label="invocation_id")
    root_resolved = Path(root).resolve()
    base = root_resolved / PROPOSALS_REL
    candidate = base / run_id / invocation_id

    # The proposal base itself must remain within the project root.
    try:
        base.resolve(strict=False).relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceError(f"proposal root escapes project root: {base}") from exc

    current = root_resolved
    for component in PROPOSALS_REL.parts + (run_id, invocation_id):
        current = current / component
        if current.is_symlink():
            raise EvidenceError(f"proposal path contains symlink component: {current}")

    try:
        candidate.resolve(strict=False).relative_to(base.resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceError(f"proposal path escapes proposal root: {candidate}") from exc
    return candidate


def proposal_path(
    root: Path | str,
    run_id: str,
    invocation_id: str,
    filename: str = "host-envelope.json",
) -> Path:
    name = (filename or "").strip()
    if not name or name in {".", ".."} or Path(name).name != name:
        raise EvidenceError(f"invalid proposal filename: {filename!r}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
        raise EvidenceError(f"invalid proposal filename: {filename!r}")
    return proposal_dir(root, run_id, invocation_id) / name


def capture_host_output(
    root: Path | str,
    run_id: str,
    invocation_id: str,
    raw_output: bytes | str | Mapping[str, Any],
    *,
    filename: str = "host-envelope.json",
) -> dict[str, Any]:
    """Capture untrusted host output under its run/invocation proposal root."""

    path = proposal_path(root, run_id, invocation_id, filename)
    parent = proposal_dir(root, run_id, invocation_id)
    parent.mkdir(parents=True, exist_ok=True)
    # Recheck after mkdir to close an existing-component symlink redirect.
    if parent.is_symlink():
        raise EvidenceError(f"proposal directory may not be a symlink: {parent}")

    if isinstance(raw_output, Mapping):
        body = (
            json.dumps(dict(raw_output), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
    elif isinstance(raw_output, str):
        body = raw_output.encode("utf-8")
    elif isinstance(raw_output, bytes):
        body = raw_output
    else:
        raise TypeError("host output must be bytes, str, or mapping")

    _atomic_write_bytes(path, body)
    return {
        "path": str(path),
        "sha256": sha256_bytes(body),
        "size": len(body),
        "captured_at": _utc_now(),
        "run_id": validate_identifier(run_id, label="run_id"),
        "invocation_id": validate_identifier(invocation_id, label="invocation_id"),
    }


def _json_object(raw: Any, *, layer: str) -> dict[str, Any]:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvidenceError(f"{layer} JSON must be UTF-8") from exc
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EvidenceError(f"{layer} JSON parse failed: {exc}") from exc
    if not isinstance(raw, dict):
        raise EvidenceError(f"{layer} JSON must be an object")
    return dict(raw)


def _nonempty_string(data: Mapping[str, Any], key: str, *, layer: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{layer}.{key} must be a non-empty string")
    return value.strip()


def parse_host_envelope(
    raw: Any,
    *,
    expected_session_id: str | None = None,
    expected_stop_reason: str | None = None,
) -> dict[str, Any]:
    """Parse and validate the outer JSON emitted by ``grok --output-format json``."""

    data = _json_object(raw, layer="host envelope")
    if "writer" in data:
        raise EvidenceError(
            "host envelope may not self-declare writer/authoritative provenance"
        )
    if "text" in data and "output" in data and data["text"] != data["output"]:
        raise EvidenceError("host envelope text/output fields disagree")
    if "text" in data:
        text = _nonempty_string(data, "text", layer="host envelope")
    else:
        text = _nonempty_string(data, "output", layer="host envelope")
    stop_reason = _nonempty_string(data, "stopReason", layer="host envelope")
    session_id = _nonempty_string(data, "sessionId", layer="host envelope")
    request_id = _nonempty_string(data, "requestId", layer="host envelope")
    if expected_session_id is not None and session_id != expected_session_id:
        raise EvidenceError(
            f"host envelope session mismatch: {session_id!r} != {expected_session_id!r}"
        )
    if expected_stop_reason is not None and stop_reason != expected_stop_reason:
        raise EvidenceError(
            f"host envelope stop reason mismatch: "
            f"{stop_reason!r} != {expected_stop_reason!r}"
        )
    normalized = dict(data)
    normalized.update(
        {
            "text": text,
            "stopReason": stop_reason,
            "sessionId": session_id,
            "requestId": request_id,
        }
    )
    return normalized


def parse_structured_payload(
    raw_text: bytes | str,
    *,
    schema_version: int = 2,
) -> dict[str, Any]:
    """Parse the nested model JSON from a validated host envelope."""

    data = _json_object(raw_text, layer="payload")
    if "writer" in data:
        raise EvidenceError(
            "payload may not self-declare writer/authoritative provenance"
        )
    raw_version = data.get("schema_version")
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise EvidenceError("payload.schema_version must be an integer")
    if raw_version != schema_version:
        raise EvidenceError(
            f"unsupported payload schema_version={raw_version!r}; "
            f"expected {schema_version}"
        )
    return data


def _validate_sha(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise EvidenceError(f"{label} must be a SHA-256 string")
    digest = value.strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise EvidenceError(f"{label} must be 64 lowercase hex characters")
    return digest


def _parse_timestamp(value: datetime | str, *, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise EvidenceError(f"{label} must be an ISO-8601 timestamp") from exc
    else:
        raise EvidenceError(f"{label} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class _ValidatedEvidence:
    record: dict[str, Any]
    capability: object


def validate_proposal(
    host: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    root: Path | str,
    run_id: str,
    invocation_id: str,
    session_id: str,
    stage: str,
    role: str,
    source_path: Path | str,
    input_sha256: str,
    artifact_sha256: str,
    rc: int,
    started_at: datetime | str,
    finished_at: datetime | str,
    captured_at: datetime | str,
    timed_out: bool = False,
    round_n: int | None = None,
    cycle: int | None = None,
    attempt: int | None = None,
    allowed_verdicts: set[str] | frozenset[str] | None = None,
    max_age_seconds: float = 300.0,
    now: datetime | str | None = None,
) -> _ValidatedEvidence:
    """Bind a captured proposal to the exact CLI invocation identity.

    This validates the common strict-v2 identity tuple and recomputes the
    source hash.  The returned in-memory capability is required by
    :func:`write_authoritative_stamp`; serializing ``record`` does not recreate
    that capability.
    """

    if rc != 0:
        raise EvidenceError(f"proposal process rc must be 0, got {rc}")
    if timed_out:
        raise EvidenceError("proposal process timed out")

    run_id = validate_identifier(run_id, label="run_id")
    invocation_id = validate_identifier(invocation_id, label="invocation_id")
    stage = validate_identifier(stage, label="stage")
    role = validate_identifier(role, label="role")
    session_id = validate_identifier(session_id, label="session_id")
    input_digest = _validate_sha(input_sha256, label="input_sha256")
    artifact_digest = _validate_sha(artifact_sha256, label="artifact_sha256")

    started = _parse_timestamp(started_at, label="started_at")
    finished = _parse_timestamp(finished_at, label="finished_at")
    captured = _parse_timestamp(captured_at, label="captured_at")
    current = _parse_timestamp(now, label="now") if now is not None else datetime.now(timezone.utc)
    if finished < started:
        raise EvidenceError("finished_at precedes started_at")
    if captured < finished:
        raise EvidenceError("captured output predates invocation completion")
    if captured > current + timedelta(seconds=5):
        raise EvidenceError("captured output timestamp is in the future")
    if max_age_seconds < 0:
        raise EvidenceError("max_age_seconds must be non-negative")
    if (current - captured).total_seconds() > max_age_seconds:
        raise EvidenceError("captured output is stale")

    host_data = parse_host_envelope(
        host,
        expected_session_id=session_id,
        expected_stop_reason="EndTurn",
    )
    payload_data = parse_structured_payload(json.dumps(dict(payload)))

    expected: dict[str, Any] = {
        "run_id": run_id,
        "invocation_id": invocation_id,
        "session_id": session_id,
        "stage": stage,
        "role": role,
    }
    optional_expected = {
        "round": round_n,
        "cycle": cycle,
        "attempt": attempt,
    }
    for key, value in optional_expected.items():
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise EvidenceError(f"{key} must be a non-negative integer")
    expected.update({key: value for key, value in optional_expected.items() if value is not None})
    for key, expected_value in expected.items():
        if payload_data.get(key) != expected_value:
            raise EvidenceError(
                f"payload identity mismatch for {key}: "
                f"{payload_data.get(key)!r} != {expected_value!r}"
            )

    verdict = payload_data.get("verdict", payload_data.get("status"))
    if not isinstance(verdict, str) or not verdict.strip():
        raise EvidenceError("payload verdict/status must be a non-empty string")
    verdict = verdict.strip()
    if not allowed_verdicts:
        raise EvidenceError("allowed_verdicts policy must be supplied")
    if verdict not in allowed_verdicts:
        raise EvidenceError(f"payload verdict/status is not allowed: {verdict!r}")
    if payload_data.get("stub") is True or payload_data.get("is_stub") is True:
        raise EvidenceError("stub proposal cannot satisfy an authoritative gate")

    source = Path(source_path)
    if not source.is_file():
        raise EvidenceError(f"proposal source artifact missing: {source}")
    expected_source_root = proposal_dir(root, run_id, invocation_id).resolve(
        strict=True
    )
    if source.is_symlink():
        raise EvidenceError("proposal source artifact may not be a symlink")
    try:
        source = source.resolve(strict=True)
        source.relative_to(expected_source_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceError(
            "proposal source artifact is outside its run/invocation proposal root"
        ) from exc
    source_digest = sha256_file(source)
    if source_digest != artifact_digest:
        raise EvidenceError("proposal artifact_sha256 mismatch")

    proposal_input = payload_data.get("input_sha256")
    if _validate_sha(
        proposal_input, label="payload.input_sha256"
    ) != input_digest:
        raise EvidenceError("payload input_sha256 mismatch")

    record = {
        "schema_version": 2,
        "run_id": run_id,
        "invocation_id": invocation_id,
        "session_id": session_id,
        "host_request_id": host_data["requestId"],
        "host_stop_reason": host_data["stopReason"],
        "stage": stage,
        "role": role,
        "round": round_n,
        "cycle": cycle,
        "attempt": attempt,
        "source_artifact": str(source),
        "artifact_sha256": source_digest,
        "input_sha256": input_digest,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "captured_at": captured.isoformat(),
        "rc": 0,
        "timed_out": False,
        "verdict": verdict,
        "payload": payload_data,
        "validated_at": _utc_now(),
    }
    return _ValidatedEvidence(record=record, capability=_STAMP_CAPABILITY)


def write_authoritative_stamp(
    root: Path | str,
    run_id: str,
    relative_path: Path | str,
    validated: _ValidatedEvidence,
) -> dict[str, Any]:
    """Write a CLI-owned strict-v2 stage stamp from an in-memory validation.

    Only ``runs/<run>/stages/**`` is accepted here.  Status, acceptance,
    integration and goal-ledger files have separate authority paths.
    """

    if not isinstance(validated, _ValidatedEvidence) or (
        validated.capability is not _STAMP_CAPABILITY
    ):
        raise PermissionError("authoritative stamp requires live CLI validation")
    run_id = validate_identifier(run_id, label="run_id")
    if validated.record.get("run_id") != run_id:
        raise EvidenceError("validated evidence run_id does not match stamp run")

    rel = Path(relative_path)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "stages":
        raise EvidenceError(
            "authoritative evidence stamp must be a relative stages/** path"
        )
    if rel.suffix != ".json":
        raise EvidenceError("authoritative evidence stamp must be a JSON file")

    run_root = Path(root).resolve() / ".omg" / "state" / "runs" / run_id
    target = run_root / rel
    try:
        target.resolve(strict=False).relative_to(run_root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceError(f"authoritative stamp path escapes run: {target}") from exc

    stamped = dict(validated.record)
    stamped["writer"] = CLI_WRITER
    stamped["stamped_at"] = _utc_now()
    _atomic_write_json(target, stamped)
    return stamped


def assert_safe_supervised_parent(env: Mapping[str, str] | None = None) -> None:
    """Reject the external-CLI bypass before any supervised run mutation."""

    source = env if env is not None else os.environ
    raw = str(source.get("OMG_ALLOW_EXTERNAL_CLI", "")).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        raise RuntimeError(
            "refusing supervised lifecycle while parent "
            "OMG_ALLOW_EXTERNAL_CLI=1; use `omg ask` for an isolated advisor child"
        )


def safe_supervised_child_env(
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy an environment while removing lifecycle escape variables."""

    env = dict(base if base is not None else os.environ)
    for key in list(env):
        if key.startswith("OMG_ALLOW_") or key in {"OMG_ALLOW_UNSAFE_SPAWN"}:
            env.pop(key, None)
    return env


__all__ = [
    "CLI_WRITER",
    "EvidenceError",
    "assert_safe_supervised_parent",
    "capture_host_output",
    "parse_host_envelope",
    "parse_structured_payload",
    "proposal_dir",
    "proposal_path",
    "proposals_root",
    "safe_supervised_child_env",
    "sha256_bytes",
    "sha256_file",
    "validate_identifier",
    "validate_proposal",
    "write_authoritative_stamp",
]
