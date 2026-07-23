"""Bounded, immutable recovery of Grok session JSONL sources.

The recovery pipeline deliberately consumes the frozen W0 helpers and caps.
It never follows source symlinks, never exposes unknown record payloads to the
resume context, and persists enough mechanical evidence to audit every cut.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import (
    IMMUTABLE_SOURCE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
)
from omg_cli.contracts.resume_contract import (
    RECOVERY_CAPS,
    RECOVERY_COUNTERS,
    fit_context_turns,
    omit_oversized_physical_lines,
    ordered_warnings,
    retain_newest_complete_turns,
    retain_newest_parsed_records,
    retain_newest_physical_lines,
    retain_source_suffix,
    validate_recovery_manifest,
)
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
from omg_cli.redaction import redact_value


RECOGNIZED_TYPES = frozenset(
    {"checkpoint", "turn_start", "user_message", "assistant_message", "turn_end"}
)


class SessionRecoveryError(RuntimeError):
    """A source identity or source-file safety invariant was violated."""


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _read_bounded_source(source: Path) -> tuple[bytes, dict[str, Any], os.stat_result, os.stat_result]:
    before_lstat = source.lstat()
    if stat.S_ISLNK(before_lstat.st_mode) or not stat.S_ISREG(before_lstat.st_mode):
        raise SessionRecoveryError("E_RESUME_SOURCE_NOT_REGULAR")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except (OSError, ValueError) as exc:
        raise SessionRecoveryError("E_RESUME_SOURCE_NOT_REGULAR") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _stat_identity(before) != _stat_identity(before_lstat):
            raise SessionRecoveryError("E_RESUME_SOURCE_CHANGED_DURING_COPY")
        cap = RECOVERY_CAPS["source_bytes"]
        if before.st_size <= cap:
            os.lseek(descriptor, 0, os.SEEK_SET)
            raw = b""
            remaining = before.st_size + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                raw += chunk
                remaining -= len(chunk)
            suffix = retain_source_suffix(raw)
        else:
            # One preceding byte lets the frozen helper determine whether the
            # bounded suffix begins inside a physical line.
            start = before.st_size - cap - 1
            os.lseek(descriptor, start, os.SEEK_SET)
            raw = b""
            remaining = cap + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                raw += chunk
                remaining -= len(chunk)
            suffix = retain_source_suffix(raw)
            suffix["source_bytes_total"] = before.st_size
            suffix["source_prefix_bytes_omitted"] = before.st_size - cap
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    try:
        path_after = source.lstat()
    except OSError as exc:
        raise SessionRecoveryError("E_RESUME_SOURCE_CHANGED_DURING_COPY") from exc
    if (
        _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != _stat_identity(path_after)
        or stat.S_ISLNK(path_after.st_mode)
        or not stat.S_ISREG(path_after.st_mode)
    ):
        raise SessionRecoveryError("E_RESUME_SOURCE_CHANGED_DURING_COPY")
    return suffix["retained"], suffix, before, after


def _split_physical_lines(source: bytes) -> list[bytes]:
    if not source:
        return []
    return source.splitlines(keepends=True)


def _decode_records(lines: Sequence[bytes]) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    malformed: list[str] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            malformed.append(sha256_hex(line))
            continue
        if not isinstance(value, dict):
            malformed.append(sha256_hex(line))
            continue
        event_type = value.get("type")
        event_id = value.get("event_id")
        previous = value.get("prev_event_id")
        payload = value.get("payload")
        if (
            not isinstance(event_type, str)
            or not event_type
            or not isinstance(event_id, str)
            or not event_id
            or (previous is not None and not isinstance(previous, str))
            or not isinstance(payload, dict)
        ):
            malformed.append(sha256_hex(line))
            continue
        records.append(
            {
                "record_class": "recognized" if event_type in RECOGNIZED_TYPES else "unknown",
                "record": value,
                "line_sha256": sha256_hex(line),
            }
        )
    return records, malformed


def _chain_is_broken(records: Sequence[Mapping[str, Any]]) -> bool:
    if not records:
        return False
    first = records[0]["record"]
    if first.get("prev_event_id") is not None:
        return True
    prior = first.get("event_id")
    for envelope in records[1:]:
        record = envelope["record"]
        if record.get("prev_event_id") != prior:
            return True
        prior = record.get("event_id")
    return False


def _complete_turns(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Project only turns that occur in legal physical event order.

    Grouping by ``turn_id`` can accidentally splice an early start to a late
    end across interleaved, duplicated, or reordered events.  Recovery is an
    evidence projection, not a repair engine, so an illegal sequence is
    omitted and marks the recovery partial.
    """

    complete: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    illegal_order = False
    for envelope in records:
        if envelope.get("record_class") != "recognized":
            continue
        record = envelope["record"]
        event_type = record.get("type")
        if event_type == "checkpoint":
            continue
        payload = record.get("payload")
        turn_id = payload.get("turn_id") if isinstance(payload, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            illegal_order = True
            active = None
            continue

        if event_type == "turn_start":
            if active is not None:
                illegal_order = True
            active = {
                "turn_id": turn_id,
                "phase": "users",
                "user_messages": [],
                "assistant_messages": [],
                "valid": True,
            }
            continue
        if active is None or active["turn_id"] != turn_id:
            illegal_order = True
            active = None
            continue
        if event_type == "user_message":
            if active["phase"] != "users":
                illegal_order = True
                active = None
                continue
            active["user_messages"].append(redact_value(payload.get("text", "")))
            continue
        if event_type == "assistant_message":
            if not active["user_messages"]:
                illegal_order = True
                active = None
                continue
            active["phase"] = "assistants"
            active["assistant_messages"].append(
                redact_value(payload.get("text", ""))
            )
            continue
        if event_type == "turn_end":
            if not active["user_messages"] or not active["assistant_messages"]:
                illegal_order = True
                active = None
                continue
            complete.append(
                {
                    "turn_id": active["turn_id"],
                    "user_messages": active["user_messages"],
                    "assistant_messages": active["assistant_messages"],
                }
            )
            active = None
            continue
        illegal_order = True
        active = None
    if active is not None:
        illegal_order = True
    return complete, illegal_order


def _serialize_turns(turns: Sequence[Mapping[str, Any]]) -> list[bytes]:
    return [canonical_json_bytes(dict(turn)) + b"\n" for turn in turns]


def _unknown_types(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for envelope in records:
        if envelope.get("record_class") != "unknown":
            continue
        name = str(envelope["record"].get("type"))
        grouped[name].append(str(envelope["line_sha256"]))
    return [
        {
            "name": name,
            "count": len(grouped[name]),
            "sha256": sha256_hex("\n".join(grouped[name]).encode("ascii")),
        }
        for name in sorted(grouped, key=lambda item: item.encode("utf-8"))
    ]


def _write_exact(path: Path, body: bytes, *, mode: int) -> Path:
    try:
        return atomic_write_bytes(path, body, mode=mode, replace=False)
    except FileExistsError as exc:
        raise SessionRecoveryError(
            f"recovery artifact publication collision: {path}"
        ) from exc


def _limit_event(limit: str, observed: int, omitted: int) -> dict[str, Any] | None:
    cap = RECOVERY_CAPS[limit]
    if observed <= cap or omitted < 1:
        return None
    return {"limit": limit, "cap": cap, "observed": observed, "omitted": omitted}


def recover_session(
    source: Path | str,
    destination_root: Path | str,
    *,
    repository_id: str = "OMG",
    host: str = "grok",
) -> dict[str, Any]:
    """Recover a bounded context and immutable evidence copy from *source*."""

    source_path = Path(source)
    retained_source, suffix, before, after = _read_bounded_source(source_path)

    physical_all = _split_physical_lines(retained_source)
    oversized = omit_oversized_physical_lines(physical_all)
    physical = retain_newest_physical_lines(oversized["retained"])
    oldest_count = physical["physical_lines_omitted_oldest"]
    oldest_lines = list(oversized["retained"][:oldest_count])
    retained_lines = list(physical["retained"])
    nonoversized_indexes = [
        index
        for index, line in enumerate(physical_all)
        if len(line) <= RECOVERY_CAPS["physical_line_bytes"]
    ]
    omitted_indexes = {
        index
        for index, line in enumerate(physical_all)
        if len(line) > RECOVERY_CAPS["physical_line_bytes"]
    } | set(nonoversized_indexes[:oldest_count])
    ordered_omitted_hashes = [
        sha256_hex(line)
        for index, line in enumerate(physical_all)
        if index in omitted_indexes
    ]
    immutable_body = b"".join(retained_lines)

    decoded, malformed = _decode_records(retained_lines)
    parsed = retain_newest_parsed_records(decoded)
    retained_records = list(parsed["retained"])
    turns_all, illegal_event_order = _complete_turns(retained_records)

    context_error: str | None = None
    retained_turns: list[dict[str, Any]] = []
    context_result: dict[str, Any] = {
        "context": b"",
        "context_bytes_before": 0,
        "context_bytes_after": 0,
        "context_turns_omitted_oldest": 0,
        "warnings": [],
    }
    turn_result: dict[str, Any] = {
        "complete_turns_seen": 0,
        "complete_turns_retained": 0,
        "complete_turns_omitted_oldest": 0,
        "warnings": [],
    }
    source_turns = turns_all
    if source_turns:
        turn_result = retain_newest_complete_turns(source_turns)
        retained_turns = list(turn_result["retained"])
        serialized = _serialize_turns(retained_turns)
        try:
            context_result = fit_context_turns(serialized)
        except ContractValidationError as exc:
            if str(exc) != "E_RESUME_CONTEXT_OVER_CAP":
                raise
            context_error = "E_RESUME_CONTEXT_OVER_CAP"
            context_result = {
                "context": b"",
                "context_bytes_before": sum(len(row) for row in serialized),
                "context_bytes_after": 0,
                "context_turns_omitted_oldest": len(serialized),
                "warnings": ["W_CONTEXT_TRUNCATED"],
            }
    else:
        context_error = "E_RESUME_NO_COMPLETE_TURNS"

    broken_chain = _chain_is_broken(retained_records)
    unknown = _unknown_types(retained_records)
    complete_turn_ids = {turn["turn_id"] for turn in turns_all}
    fragment_turn_ids = {
        envelope["record"]["payload"].get("turn_id")
        for envelope in retained_records
        if envelope.get("record_class") == "recognized"
        and envelope["record"].get("type")
        in {"turn_start", "user_message", "assistant_message", "turn_end"}
        and isinstance(envelope["record"].get("payload"), dict)
        and isinstance(envelope["record"]["payload"].get("turn_id"), str)
    }
    incomplete_turn_records = bool(fragment_turn_ids - complete_turn_ids)

    warnings: list[str] = []
    if broken_chain:
        warnings.append("W_BROKEN_CHAIN")
    if (
        broken_chain
        or unknown
        or malformed
        or incomplete_turn_records
        or illegal_event_order
        or suffix["warnings"]
        or oversized["warnings"]
        or physical["warnings"]
        or parsed["warnings"]
        or turn_result["warnings"]
        or context_result["warnings"]
    ):
        warnings.append("W_PARTIAL_RECOVERY")
    warnings.extend(suffix["warnings"])
    warnings.extend(oversized["warnings"])
    warnings.extend(physical["warnings"])
    warnings.extend(parsed["warnings"])
    warnings.extend(turn_result["warnings"])
    warnings.extend(context_result["warnings"])
    if unknown:
        warnings.append("W_UNKNOWN_RECORD_TYPE")
    warnings = ordered_warnings(warnings)

    counters: dict[str, Any] = dict.fromkeys(RECOVERY_COUNTERS, 0)
    counters.update(
        {
            "source_bytes_total": before.st_size,
            "source_bytes_considered": suffix["source_bytes_considered"],
            "source_prefix_bytes_omitted": suffix["source_prefix_bytes_omitted"],
            "leading_fragment_bytes_omitted": suffix["leading_fragment_bytes_omitted"],
            "physical_lines_seen": len(physical_all),
            "physical_lines_retained": len(retained_lines),
            "physical_lines_omitted_oldest": oldest_count,
            "oversized_lines_omitted": oversized["oversized_lines_omitted"],
            "parsed_records_seen": parsed["parsed_records_seen"],
            "parsed_records_retained": parsed["parsed_records_retained"],
            "parsed_records_omitted_oldest": parsed["parsed_records_omitted_oldest"],
            "recognized_records_seen": parsed["recognized_records_seen"],
            "recognized_records_retained": parsed["recognized_records_retained"],
            "unknown_records_seen": parsed["unknown_records_seen"],
            "unknown_records_retained": parsed["unknown_records_retained"],
            "malformed_lines_seen": len(malformed),
            "complete_turns_seen": turn_result["complete_turns_seen"],
            "complete_turns_retained": turn_result["complete_turns_retained"],
            "complete_turns_omitted_oldest": turn_result["complete_turns_omitted_oldest"],
            "context_bytes_before": context_result["context_bytes_before"],
            "context_bytes_after": context_result["context_bytes_after"],
            "context_turns_omitted_oldest": context_result["context_turns_omitted_oldest"],
        }
    )

    destination = Path(destination_root).resolve()
    ensure_managed_dir(destination)
    immutable_sha = sha256_hex(immutable_body)
    artifact_id = uuid.uuid4().hex
    immutable_path = destination / f"source-{immutable_sha}-{artifact_id}.jsonl"
    _write_exact(immutable_path, immutable_body, mode=IMMUTABLE_SOURCE_MODE)

    limit_events = [
        row
        for row in (
            _limit_event(
                "source_bytes",
                before.st_size,
                suffix["source_prefix_bytes_omitted"] + suffix["leading_fragment_bytes_omitted"],
            ),
            _limit_event(
                "physical_line_bytes",
                max((len(row) for row in physical_all), default=0),
                oversized["oversized_lines_omitted"],
            ),
            _limit_event("physical_lines", len(oversized["retained"]), oldest_count),
            _limit_event(
                "parsed_records",
                parsed["parsed_records_seen"],
                parsed["parsed_records_omitted_oldest"],
            ),
            _limit_event(
                "complete_turns",
                turn_result["complete_turns_seen"],
                turn_result["complete_turns_omitted_oldest"],
            ),
            _limit_event(
                "context_bytes",
                context_result["context_bytes_before"],
                max(1, context_result["context_bytes_before"] - context_result["context_bytes_after"])
                if context_result["context_bytes_before"] > RECOVERY_CAPS["context_bytes"]
                else 0,
            ),
        )
        if row is not None
    ]
    first_event = retained_records[0]["record"]["event_id"] if retained_records else "none"
    last_event = retained_records[-1]["record"]["event_id"] if retained_records else "none"
    copied_start = min(
        before.st_size,
        suffix["source_prefix_bytes_omitted"]
        + suffix["leading_fragment_bytes_omitted"]
        + sum(len(line) for line in oldest_lines),
    )
    manifest = {
        "store_kind": "bounded_recovery_manifest",
        "schema_version": 1,
        "repository_id": repository_id,
        "host": host,
        "source_path_hash": sha256_hex(str(source_path.resolve()).encode("utf-8")),
        "immutable_copy_path": str(immutable_path),
        "immutable_copy_sha256": immutable_sha,
        "copy_mode": "0400",
        "device_before": before.st_dev,
        "inode_before": before.st_ino,
        "size_before": before.st_size,
        "mtime_ns_before": before.st_mtime_ns,
        "device_after": after.st_dev,
        "inode_after": after.st_ino,
        "size_after": after.st_size,
        "mtime_ns_after": after.st_mtime_ns,
        "copied_byte_start": copied_start,
        "copied_byte_end": before.st_size,
        "first_event_id": first_event,
        "last_event_id": last_event,
        "counters": counters,
        "unknown_record_types": unknown,
        "malformed_line_hashes": malformed,
        "omitted_hashes": ordered_omitted_hashes,
        "limit_events": limit_events,
        "warnings": warnings,
        "partial": bool(warnings),
    }
    validate_recovery_manifest(manifest)
    manifest_body = canonical_json_bytes(manifest)
    manifest_path = destination / f"recovery-manifest-{artifact_id}.json"
    _write_exact(manifest_path, manifest_body, mode=IMMUTABLE_SOURCE_MODE)

    receipt = {
        "store_kind": "bounded_recovery_receipt",
        "schema_version": 1,
        "artifact_id": artifact_id,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_hex(manifest_body),
        "manifest_mode": "0400",
        "immutable_copy_path": str(immutable_path),
        "immutable_copy_sha256": immutable_sha,
        "immutable_copy_mode": "0400",
    }
    receipt_body = canonical_json_bytes(receipt)
    receipt_path = destination / f"recovery-receipt-{artifact_id}.json"
    _write_exact(receipt_path, receipt_body, mode=IMMUTABLE_SOURCE_MODE)

    context_path: Path | None = None
    if context_error is None and context_result["context"]:
        context_sha = sha256_hex(context_result["context"])
        context_path = destination / f"context-{context_sha}-{artifact_id}.jsonl"
        _write_exact(context_path, context_result["context"], mode=IMMUTABLE_SOURCE_MODE)

    return {
        "manifest": manifest,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_hex(manifest_body),
        "immutable_copy_path": str(immutable_path),
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha256_hex(receipt_body),
        "context_path": str(context_path) if context_path else None,
        "error": context_error,
    }


__all__ = ["SessionRecoveryError", "recover_session"]
