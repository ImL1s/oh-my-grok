"""Exact selector precedence and bounded partial-recovery schema."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from .state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_integer,
    require_nonempty_string,
    require_object,
    require_sha256,
)


RECOVERY_CAPS = {
    "source_bytes": 16_777_216,
    "physical_line_bytes": 1_048_576,
    "physical_lines": 900,
    "parsed_records": 900,
    "complete_turns": 256,
    "context_bytes": 2_097_152,
}
WARNING_ORDER = (
    "W_BROKEN_CHAIN",
    "W_PARTIAL_RECOVERY",
    "W_TRUNCATED_SOURCE",
    "W_PARSED_RECORDS_TRUNCATED",
    "W_TURNS_TRUNCATED",
    "W_CONTEXT_TRUNCATED",
    "W_UNKNOWN_RECORD_TYPE",
)
RESUME_SELECTORS = (
    "recovery_manifest",
    "run_id",
    "native_session_id",
    "current_process_run",
    "signed_handoff",
    "best_effort_cwd",
)
RESUME_ERRORS = (
    "E_RESUME_SELECTOR_CONFLICT",
    "E_RESUME_AMBIGUOUS",
    "E_RESUME_NOT_FOUND",
    "E_RESUME_NO_COMPLETE_TURNS",
    "E_RESUME_CONTEXT_OVER_CAP",
    "E_RESUME_SOURCE_CHANGED_DURING_COPY",
    "E_RESUME_SOURCE_NOT_REGULAR",
)
RECOVERY_COUNTERS = (
    "source_bytes_total",
    "source_bytes_considered",
    "source_prefix_bytes_omitted",
    "leading_fragment_bytes_omitted",
    "physical_lines_seen",
    "physical_lines_retained",
    "physical_lines_omitted_oldest",
    "oversized_lines_omitted",
    "parsed_records_seen",
    "parsed_records_retained",
    "parsed_records_omitted_oldest",
    "recognized_records_seen",
    "recognized_records_retained",
    "unknown_records_seen",
    "unknown_records_retained",
    "malformed_lines_seen",
    "complete_turns_seen",
    "complete_turns_retained",
    "complete_turns_omitted_oldest",
    "context_bytes_before",
    "context_bytes_after",
    "context_turns_omitted_oldest",
)


def retain_source_suffix(source: bytes, *, cap: int = RECOVERY_CAPS["source_bytes"]) -> dict[str, Any]:
    """Retain the newest bounded source suffix without inventing a partial first line."""

    if not isinstance(source, bytes) or isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ContractValidationError("source recovery requires bytes and a positive cap")
    total = len(source)
    if total <= cap:
        return {
            "retained": source,
            "source_bytes_total": total,
            "source_bytes_considered": total,
            "source_prefix_bytes_omitted": 0,
            "leading_fragment_bytes_omitted": 0,
            "warnings": [],
        }
    start = total - cap
    suffix = source[start:]
    leading_omitted = 0
    if start > 0 and source[start - 1 : start] != b"\n":
        newline = suffix.find(b"\n")
        leading_omitted = len(suffix) if newline < 0 else newline + 1
        suffix = suffix[leading_omitted:]
    return {
        "retained": suffix,
        "source_bytes_total": total,
        "source_bytes_considered": len(suffix),
        "source_prefix_bytes_omitted": start,
        "leading_fragment_bytes_omitted": leading_omitted,
        "warnings": ["W_TRUNCATED_SOURCE"],
    }


def omit_oversized_physical_lines(
    lines: Sequence[bytes], *, cap: int = RECOVERY_CAPS["physical_line_bytes"]
) -> dict[str, Any]:
    """Omit an over-cap physical line whole and preserve ordered omission hashes."""

    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ContractValidationError("physical-line cap must be a positive integer")
    retained: list[bytes] = []
    omitted_hashes: list[str] = []
    for line in lines:
        if not isinstance(line, bytes):
            raise ContractValidationError("physical lines must be bytes")
        if len(line) > cap:
            omitted_hashes.append(hashlib.sha256(line).hexdigest())
        else:
            retained.append(line)
    return {
        "retained": retained,
        "physical_lines_seen": len(lines),
        "oversized_lines_omitted": len(omitted_hashes),
        "omitted_hashes": omitted_hashes,
        "warnings": ["W_TRUNCATED_SOURCE"] if omitted_hashes else [],
    }


def retain_newest_physical_lines(
    lines: Sequence[bytes], *, cap: int = RECOVERY_CAPS["physical_lines"]
) -> dict[str, Any]:
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ContractValidationError("physical-lines cap must be a positive integer")
    rows = list(lines)
    omitted = max(0, len(rows) - cap)
    retained = rows[omitted:]
    return {
        "retained": retained,
        "physical_lines_seen": len(rows),
        "physical_lines_retained": len(retained),
        "physical_lines_omitted_oldest": omitted,
        "warnings": ["W_TRUNCATED_SOURCE"] if omitted else [],
    }


def retain_newest_parsed_records(
    records: Sequence[Mapping[str, Any]],
    *,
    cap: int = RECOVERY_CAPS["parsed_records"],
) -> dict[str, Any]:
    """Retain recognized/unknown decoded envelopes; malformed lines are not inputs."""

    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ContractValidationError("parsed-record cap must be a positive integer")
    rows: list[dict[str, Any]] = []
    for record in records:
        row = dict(require_object(record, label="parsed record"))
        if row.get("record_class") not in {"recognized", "unknown"}:
            raise ContractValidationError(
                "parsed record_class must be recognized or unknown; malformed lines are separate"
            )
        rows.append(row)
    omitted = max(0, len(rows) - cap)
    retained = rows[omitted:]
    return {
        "retained": retained,
        "parsed_records_seen": len(rows),
        "parsed_records_retained": len(retained),
        "parsed_records_omitted_oldest": omitted,
        "recognized_records_seen": sum(row["record_class"] == "recognized" for row in rows),
        "recognized_records_retained": sum(
            row["record_class"] == "recognized" for row in retained
        ),
        "unknown_records_seen": sum(row["record_class"] == "unknown" for row in rows),
        "unknown_records_retained": sum(row["record_class"] == "unknown" for row in retained),
        "warnings": ["W_PARSED_RECORDS_TRUNCATED"] if omitted else [],
    }


def retain_newest_complete_turns(
    turns: Sequence[Any], *, cap: int = RECOVERY_CAPS["complete_turns"]
) -> dict[str, Any]:
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ContractValidationError("complete-turn cap must be a positive integer")
    rows = list(turns)
    if not rows:
        raise ContractValidationError("E_RESUME_NO_COMPLETE_TURNS")
    omitted = max(0, len(rows) - cap)
    retained = rows[omitted:]
    return {
        "retained": retained,
        "complete_turns_seen": len(rows),
        "complete_turns_retained": len(retained),
        "complete_turns_omitted_oldest": omitted,
        "warnings": ["W_TURNS_TRUNCATED"] if omitted else [],
    }


def fit_context_turns(
    turns: Sequence[bytes], *, cap: int = RECOVERY_CAPS["context_bytes"]
) -> dict[str, Any]:
    """Drop oldest whole serialized turns until the canonical context fits."""

    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ContractValidationError("context cap must be a positive integer")
    rows = list(turns)
    if any(not isinstance(turn, bytes) for turn in rows):
        raise ContractValidationError("serialized context turns must be bytes")
    before = sum(len(turn) for turn in rows)
    omitted = 0
    while rows and sum(len(turn) for turn in rows) > cap:
        if len(rows) == 1:
            raise ContractValidationError("E_RESUME_CONTEXT_OVER_CAP")
        rows.pop(0)
        omitted += 1
    body = b"".join(rows)
    return {
        "retained": rows,
        "context": body,
        "context_bytes_before": before,
        "context_bytes_after": len(body),
        "context_turns_omitted_oldest": omitted,
        "warnings": ["W_CONTEXT_TRUNCATED"] if omitted else [],
    }


def ordered_warnings(found: Sequence[str]) -> list[str]:
    unknown = set(found) - set(WARNING_ORDER)
    if unknown:
        raise ContractValidationError(f"unknown recovery warnings: {sorted(unknown)!r}")
    return [warning for warning in WARNING_ORDER if warning in set(found)]


def select_resume_selector(selectors: Mapping[str, Any], *, best_effort: bool = False) -> str:
    """Return the highest explicit selector without fallback after a mismatch."""

    present: list[str] = []
    for name in RESUME_SELECTORS:
        value = selectors.get(name)
        if value not in (None, False, "", []):
            present.append(name)
    if not present:
        raise ContractValidationError("E_RESUME_NOT_FOUND")
    winner = present[0]
    if winner == "best_effort_cwd" and not best_effort:
        raise ContractValidationError("best-effort cwd search requires --best-effort")
    if len(present) > 1 and present != ["run_id", "native_session_id"]:
        # Rank two is the sole compound selector: an explicit run ID may be
        # narrowed by its optional native session/conversation ID.  Every
        # other multi-rank combination is conflicting explicit authority.
        raise ContractValidationError("E_RESUME_SELECTOR_CONFLICT")
    return winner


def validate_recovery_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    manifest = require_object(value, label="recovery manifest")
    required = {
        "store_kind",
        "schema_version",
        "repository_id",
        "host",
        "source_path_hash",
        "immutable_copy_path",
        "immutable_copy_sha256",
        "copy_mode",
        "device_before",
        "inode_before",
        "size_before",
        "mtime_ns_before",
        "device_after",
        "inode_after",
        "size_after",
        "mtime_ns_after",
        "copied_byte_start",
        "copied_byte_end",
        "first_event_id",
        "last_event_id",
        "counters",
        "unknown_record_types",
        "malformed_line_hashes",
        "omitted_hashes",
        "limit_events",
        "warnings",
        "partial",
    }
    require_exact_keys(manifest, required=required, label="recovery manifest")
    if manifest["store_kind"] != "bounded_recovery_manifest" or manifest["schema_version"] != 1:
        raise ContractValidationError("recovery manifest header mismatch")
    require_nonempty_string(manifest["repository_id"], label="repository_id")
    require_nonempty_string(manifest["host"], label="host")
    require_sha256(manifest["source_path_hash"], label="source_path_hash")
    require_nonempty_string(manifest["immutable_copy_path"], label="immutable_copy_path")
    require_sha256(manifest["immutable_copy_sha256"], label="immutable_copy_sha256")
    if manifest["copy_mode"] != "0400":
        raise ContractValidationError("immutable recovery copy mode must be 0400")
    for field in (
        "device_before",
        "inode_before",
        "size_before",
        "mtime_ns_before",
        "device_after",
        "inode_after",
        "size_after",
        "mtime_ns_after",
        "copied_byte_start",
        "copied_byte_end",
    ):
        require_integer(manifest[field], label=field, minimum=0)
    for stem in ("device", "inode", "size", "mtime_ns"):
        if manifest[f"{stem}_before"] != manifest[f"{stem}_after"]:
            raise ContractValidationError("E_RESUME_SOURCE_CHANGED_DURING_COPY")
    if not 0 <= manifest["copied_byte_start"] <= manifest["copied_byte_end"] <= manifest["size_after"]:
        raise ContractValidationError("recovery copied byte range is invalid")
    require_nonempty_string(manifest["first_event_id"], label="first_event_id")
    require_nonempty_string(manifest["last_event_id"], label="last_event_id")
    counters = require_object(manifest["counters"], label="counters")
    if set(counters) != set(RECOVERY_COUNTERS):
        raise ContractValidationError("recovery counters do not match frozen set")
    for name in RECOVERY_COUNTERS:
        require_integer(counters[name], label=name, minimum=0)
    if counters["source_bytes_considered"] > RECOVERY_CAPS["source_bytes"]:
        raise ContractValidationError("source_bytes_considered exceeds frozen cap")
    if counters["source_bytes_considered"] > counters["source_bytes_total"]:
        raise ContractValidationError("source_bytes_considered exceeds source total")
    if (
        counters["source_prefix_bytes_omitted"]
        + counters["leading_fragment_bytes_omitted"]
        + counters["source_bytes_considered"]
        > counters["source_bytes_total"]
    ):
        raise ContractValidationError("source byte counters are contradictory")
    if counters["physical_lines_retained"] > RECOVERY_CAPS["physical_lines"]:
        raise ContractValidationError("physical_lines_retained exceeds frozen cap")
    if (
        counters["physical_lines_retained"]
        + counters["physical_lines_omitted_oldest"]
        + counters["oversized_lines_omitted"]
        != counters["physical_lines_seen"]
    ):
        raise ContractValidationError("physical line counters are contradictory")
    if counters["parsed_records_retained"] > RECOVERY_CAPS["parsed_records"]:
        raise ContractValidationError("parsed_records_retained exceeds frozen cap")
    if (
        counters["parsed_records_retained"] + counters["parsed_records_omitted_oldest"]
        != counters["parsed_records_seen"]
    ):
        raise ContractValidationError("parsed record counters are contradictory")
    if (
        counters["recognized_records_seen"] + counters["unknown_records_seen"]
        != counters["parsed_records_seen"]
        or counters["recognized_records_retained"] + counters["unknown_records_retained"]
        != counters["parsed_records_retained"]
    ):
        raise ContractValidationError("recognized/unknown record counters are contradictory")
    if counters["recognized_records_retained"] > counters["recognized_records_seen"]:
        raise ContractValidationError("recognized retained records exceed seen records")
    if counters["unknown_records_retained"] > counters["unknown_records_seen"]:
        raise ContractValidationError("unknown retained records exceed seen records")
    if counters["complete_turns_retained"] > RECOVERY_CAPS["complete_turns"]:
        raise ContractValidationError("complete_turns_retained exceeds frozen cap")
    if (
        counters["complete_turns_retained"] + counters["complete_turns_omitted_oldest"]
        != counters["complete_turns_seen"]
    ):
        raise ContractValidationError("complete turn counters are contradictory")
    if counters["context_bytes_after"] > RECOVERY_CAPS["context_bytes"]:
        raise ContractValidationError("context_bytes_after exceeds frozen cap")
    if counters["context_bytes_after"] > counters["context_bytes_before"]:
        raise ContractValidationError("context bytes grow during bounded packing")
    if counters["context_turns_omitted_oldest"] > counters["complete_turns_retained"]:
        raise ContractValidationError("context omitted turns exceed retained complete turns")
    if not isinstance(manifest["unknown_record_types"], list):
        raise ContractValidationError("unknown_record_types must be an array")
    unknown_names: list[str] = []
    unknown_count = 0
    for row in manifest["unknown_record_types"]:
        item = require_object(row, label="unknown record type")
        require_exact_keys(item, required={"name", "count", "sha256"}, label="unknown record type")
        require_nonempty_string(item["name"], label="unknown type name")
        require_integer(item["count"], label="unknown type count", minimum=1)
        require_sha256(item["sha256"], label="unknown type sha256")
        unknown_names.append(item["name"])
        unknown_count += item["count"]
    if len(unknown_names) != len(set(unknown_names)):
        raise ContractValidationError("unknown record type names must be unique")
    if unknown_count != counters["unknown_records_retained"]:
        raise ContractValidationError("unknown record type counts differ from retained counter")
    for field in ("malformed_line_hashes", "omitted_hashes"):
        if not isinstance(manifest[field], list):
            raise ContractValidationError(f"{field} must be an array")
        for digest in manifest[field]:
            require_sha256(digest, label=field)
    if len(manifest["malformed_line_hashes"]) != counters["malformed_lines_seen"]:
        raise ContractValidationError("malformed line hashes differ from malformed counter")
    if len(manifest["omitted_hashes"]) != (
        counters["physical_lines_omitted_oldest"] + counters["oversized_lines_omitted"]
    ):
        raise ContractValidationError("omitted line hashes differ from omission counters")
    if not isinstance(manifest["limit_events"], list):
        raise ContractValidationError("limit_events must be an array")
    limit_names: list[str] = []
    for row in manifest["limit_events"]:
        item = require_object(row, label="limit event")
        require_exact_keys(
            item,
            required={"limit", "cap", "observed", "omitted"},
            label="limit event",
        )
        limit_name = require_nonempty_string(item["limit"], label="limit")
        if limit_name not in RECOVERY_CAPS:
            raise ContractValidationError("limit event names an unknown recovery cap")
        if item["cap"] != RECOVERY_CAPS[limit_name]:
            raise ContractValidationError("limit event cap differs from frozen cap")
        require_integer(item["cap"], label="cap", minimum=1)
        require_integer(item["observed"], label="observed", minimum=0)
        require_integer(item["omitted"], label="omitted", minimum=0)
        if item["observed"] <= item["cap"] or item["omitted"] < 1:
            raise ContractValidationError("limit event must describe an actual over-cap cut")
        limit_names.append(limit_name)
    if len(limit_names) != len(set(limit_names)):
        raise ContractValidationError("recovery limit events must be unique")
    required_limit_warnings = {
        "source_bytes": "W_TRUNCATED_SOURCE",
        "physical_line_bytes": "W_TRUNCATED_SOURCE",
        "physical_lines": "W_TRUNCATED_SOURCE",
        "parsed_records": "W_PARSED_RECORDS_TRUNCATED",
        "complete_turns": "W_TURNS_TRUNCATED",
        "context_bytes": "W_CONTEXT_TRUNCATED",
    }
    for limit_name in limit_names:
        if required_limit_warnings[limit_name] not in manifest["warnings"]:
            raise ContractValidationError("recovery limit warning is missing")
    if manifest["warnings"] != ordered_warnings(manifest["warnings"]):
        raise ContractValidationError("recovery warnings are not in frozen order")
    if not isinstance(manifest["partial"], bool):
        raise ContractValidationError("partial must be boolean")
    if (manifest["limit_events"] or manifest["warnings"]) and not manifest["partial"]:
        raise ContractValidationError("warning-bearing recovery must remain partial")
    return manifest


def validate_golden_recovery_counts(counters: Mapping[str, Any], warnings: Sequence[str]) -> None:
    if counters.get("physical_lines_seen") != 913:
        raise ContractValidationError("golden source must contain 913 physical lines")
    if counters.get("physical_lines_retained") != 900:
        raise ContractValidationError("golden immutable copy must retain 900 lines")
    if counters.get("physical_lines_omitted_oldest") != 13:
        raise ContractValidationError("golden must omit exactly 13 oldest lines")
    if counters.get("recognized_records_retained") != 897:
        raise ContractValidationError("golden must retain exactly 897 recognized records")
    if counters.get("unknown_records_retained") != 3:
        raise ContractValidationError("golden must retain exactly 3 unknown records")
    if counters.get("complete_turns_retained") != 124:
        raise ContractValidationError("golden must reconstruct exactly 124 complete turns")
    expected = [
        "W_BROKEN_CHAIN",
        "W_PARTIAL_RECOVERY",
        "W_TRUNCATED_SOURCE",
        "W_UNKNOWN_RECORD_TYPE",
    ]
    if list(warnings) != expected:
        raise ContractValidationError("golden warning oracle mismatch")
