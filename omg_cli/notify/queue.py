"""Owner-only durable notification queue and bounded retry processor."""
from __future__ import annotations

import os
import time
import fcntl
from contextlib import contextmanager
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
)
from omg_cli.contracts.writer_chain import canonical_json_bytes, parse_canonical_json_bytes, sha256_hex
from omg_cli.notify.events import notification_from_lifecycle, notification_payload, owner_matches


MAX_RECORD_BYTES = 16_384
MAX_SEGMENT_BYTES = 4 * 1024 * 1024
MAX_SEGMENT_RECORDS = 5_000
MAX_QUEUE_SEGMENTS = 8
MAX_PROCESS_RECORDS = 256
DEFAULT_MAX_ATTEMPTS = 3
MAX_BACKOFF_SECONDS = 300.0
MAX_DEDUPE_SCAN_BYTES = 1_048_576


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _paths(root: Path | str) -> tuple[Path, Path, Path]:
    directory = Path(root).resolve() / ".omg" / "state" / "notifications"
    return directory, directory / "cursor.json", directory / "queue.lock"


def _segment_path(directory: Path, segment: int, *, dead_letter: bool = False) -> Path:
    prefix = "dead-letter" if dead_letter else "queue"
    return directory / f"{prefix}-{segment:06d}.jsonl"


def _segments(directory: Path, *, dead_letter: bool = False) -> list[int]:
    prefix = "dead-letter" if dead_letter else "queue"
    result: list[int] = []
    for path in directory.glob(f"{prefix}-??????.jsonl"):
        try:
            result.append(int(path.stem.rsplit("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return sorted(set(result))


def _cursor_default() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "segment": 1,
        "record_seq": -1,
        "byte_offset": 0,
        "last_event_id": None,
        "last_dedupe_key": None,
    }


def _load_cursor(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _cursor_default()
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_uid != os.getuid()
        or (path.stat().st_mode & 0o777) != DATA_FILE_MODE
    ):
        raise ValueError("notification cursor is unsafe")
    raw = path.read_bytes()
    parsed = parse_canonical_json_bytes(raw)
    if not isinstance(parsed, dict) or set(parsed) != set(_cursor_default()):
        raise ValueError("notification cursor schema is invalid")
    if (
        parsed.get("schema_version") != 1
        or isinstance(parsed.get("segment"), bool)
        or not isinstance(parsed.get("segment"), int)
        or parsed["segment"] < 1
        or isinstance(parsed.get("record_seq"), bool)
        or not isinstance(parsed.get("record_seq"), int)
        or parsed["record_seq"] < -1
        or isinstance(parsed.get("byte_offset"), bool)
        or not isinstance(parsed.get("byte_offset"), int)
        or parsed["byte_offset"] < 0
        or not all(
            value is None or (isinstance(value, str) and len(value) == 64)
            for value in (parsed.get("last_event_id"), parsed.get("last_dedupe_key"))
        )
    ):
        raise ValueError("notification cursor schema is invalid")
    return parsed


def _trim_partial_tail(path: Path) -> None:
    if not path.exists():
        return
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_uid != os.getuid()
        or (path.stat().st_mode & 0o777) != DATA_FILE_MODE
    ):
        raise ValueError("notification queue segment is unsafe")
    body = path.read_bytes()
    if not body or body.endswith(b"\n"):
        return
    boundary = body.rfind(b"\n") + 1
    descriptor = os.open(path, os.O_WRONLY)
    try:
        os.ftruncate(descriptor, boundary)
        os.fsync(descriptor)
        os.fchmod(descriptor, DATA_FILE_MODE)
    finally:
        os.close(descriptor)


def _append_line(path: Path, body: bytes) -> tuple[int, int]:
    if not body or b"\n" in body or len(body) > MAX_RECORD_BYTES:
        raise ValueError("notification queue record exceeds bounds")
    ensure_managed_dir(path.parent)
    _trim_partial_tail(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, DATA_FILE_MODE)
    try:
        os.fchmod(descriptor, DATA_FILE_MODE)
        start = os.lseek(descriptor, 0, os.SEEK_END)
        payload = body + b"\n"
        if os.write(descriptor, payload) != len(payload):
            raise OSError("short notification queue append")
        os.fsync(descriptor)
        return start, start + len(payload)
    finally:
        os.close(descriptor)


def _complete_lines(path: Path, offset: int = 0) -> list[tuple[int, int, bytes]]:
    if not path.exists():
        return []
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_uid != os.getuid()
        or (path.stat().st_mode & 0o777) != DATA_FILE_MODE
    ):
        raise ValueError("notification queue segment is unsafe")
    body = path.read_bytes()
    if offset > len(body):
        raise ValueError("notification cursor exceeds queue bytes")
    rows: list[tuple[int, int, bytes]] = []
    position = offset
    for line in body[offset:].splitlines(keepends=True):
        end = position + len(line)
        if not line.endswith(b"\n"):
            break
        rows.append((position, end, line[:-1]))
        position = end
    return rows


def _record_count(path: Path) -> int:
    return len(_complete_lines(path))


def _select_segment(directory: Path, cursor: Mapping[str, Any], record_bytes: int) -> int | None:
    segments = _segments(directory)
    segment = segments[-1] if segments else 1
    path = _segment_path(directory, segment)
    _trim_partial_tail(path)
    if (
        path.exists()
        and (path.stat().st_size + record_bytes + 1 > MAX_SEGMENT_BYTES or _record_count(path) >= MAX_SEGMENT_RECORDS)
    ):
        segment += 1
    existing = _segments(directory)
    if segment in existing:
        return segment
    while len(existing) >= MAX_QUEUE_SEGMENTS:
        oldest = existing[0]
        if int(cursor["segment"]) <= oldest:
            return None
        _segment_path(directory, oldest).unlink(missing_ok=True)
        existing.pop(0)
    return segment


def _validate_record(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    expected = {
        "store_kind",
        "schema_version",
        "record_id",
        "segment",
        "record_seq",
        "enqueued_at",
        "not_before",
        "attempt",
        "max_attempts",
        "retry_of",
        "event",
    }
    if set(value) != expected or value.get("store_kind") != "omg_notification_record" or value.get("schema_version") != 1:
        return None
    safe_event = notification_payload(value.get("event", {}))
    integers = (value.get("segment"), value.get("record_seq"), value.get("attempt"), value.get("max_attempts"))
    if (
        safe_event is None
        or not all(isinstance(item, int) and not isinstance(item, bool) for item in integers)
        or value["segment"] < 1
        or value["record_seq"] < 0
        or value["attempt"] < 0
        or not 1 <= value["max_attempts"] <= 10
        or value["attempt"] >= value["max_attempts"]
        or _parse_time(value.get("enqueued_at")) is None
        or _parse_time(value.get("not_before")) is None
        or not isinstance(value.get("record_id"), str)
        or len(value["record_id"]) != 64
        or not (
            value.get("retry_of") is None
            or (isinstance(value.get("retry_of"), str) and len(value["retry_of"]) == 64)
        )
    ):
        return None
    unsigned = {key: value[key] for key in expected if key != "record_id"}
    if value["record_id"] != sha256_hex(canonical_json_bytes(unsigned)):
        return None
    return {**unsigned, "record_id": value["record_id"], "event": safe_event}


def _known_dedupe(directory: Path, cursor: Mapping[str, Any], dedupe_key: str) -> bool:
    if cursor.get("last_dedupe_key") == dedupe_key:
        return True
    remaining = MAX_DEDUPE_SCAN_BYTES
    for segment in reversed(_segments(directory)):
        path = _segment_path(directory, segment)
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_uid != os.getuid()
            or (path.stat().st_mode & 0o777) != DATA_FILE_MODE
        ):
            raise ValueError("notification queue segment is unsafe")
        body = path.read_bytes()
        selected = body[-remaining:]
        if len(selected) < len(body):
            boundary = selected.find(b"\n")
            selected = selected[boundary + 1 :] if boundary >= 0 else b""
        remaining -= min(len(body), remaining)
        for raw in selected.splitlines():
            try:
                record = _validate_record(parse_canonical_json_bytes(raw))
            except (UnicodeError, ValueError, TypeError):
                continue
            if record is not None and record["event"]["dedupe_key"] == dedupe_key:
                return True
        if remaining <= 0:
            break
    return False


def _build_record(
    event: Mapping[str, Any],
    *,
    segment: int,
    record_seq: int,
    enqueued_at: str,
    not_before: str,
    attempt: int,
    max_attempts: int,
    retry_of: str | None,
) -> dict[str, Any]:
    unsigned = {
        "store_kind": "omg_notification_record",
        "schema_version": 1,
        "segment": segment,
        "record_seq": record_seq,
        "enqueued_at": enqueued_at,
        "not_before": not_before,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "retry_of": retry_of,
        "event": dict(event),
    }
    return {**unsigned, "record_id": sha256_hex(canonical_json_bytes(unsigned))}


def enqueue_notification(
    root: Path | str,
    event: Mapping[str, Any],
    *,
    owner: Mapping[str, Any] | None,
    enqueued_at: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Append one event promptly; delivery never runs on the hook caller."""

    safe_event = notification_payload(event)
    if safe_event is None or not owner_matches(safe_event, owner):
        return {"queued": False, "duplicate": False, "code": "QUEUE_EVENT_REJECTED", "authoritative": False}
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts is outside bounds")
    timestamp = enqueued_at or _utc_now()
    if _parse_time(timestamp) is None:
        raise ValueError("enqueued_at must include a timezone")
    directory, cursor_path, lock_path = _paths(root)
    ensure_managed_dir(directory)
    with exclusive_lock(lock_path):
        cursor = _load_cursor(cursor_path)
        if _known_dedupe(directory, cursor, safe_event["dedupe_key"]):
            return {"queued": False, "duplicate": True, "code": "QUEUE_DUPLICATE", "authoritative": False}
        provisional = _build_record(
            safe_event,
            segment=1,
            record_seq=0,
            enqueued_at=timestamp,
            not_before=timestamp,
            attempt=0,
            max_attempts=max_attempts,
            retry_of=None,
        )
        segment = _select_segment(directory, cursor, len(canonical_json_bytes(provisional)))
        if segment is None:
            return {"queued": False, "duplicate": False, "code": "QUEUE_FULL", "authoritative": False}
        path = _segment_path(directory, segment)
        sequence = _record_count(path)
        record = _build_record(
            safe_event,
            segment=segment,
            record_seq=sequence,
            enqueued_at=timestamp,
            not_before=timestamp,
            attempt=0,
            max_attempts=max_attempts,
            retry_of=None,
        )
        body = canonical_json_bytes(record)
        _append_line(path, body)
    return {
        "queued": True,
        "duplicate": False,
        "code": "QUEUED",
        "record_id": record["record_id"],
        "event_id": safe_event["event_id"],
        "authoritative": False,
    }


def enqueue_lifecycle_notification(
    root: Path | str,
    lifecycle: Mapping[str, Any],
    *,
    owner: Mapping[str, Any],
) -> dict[str, Any]:
    """Passive-hook entry point: static mapping plus one bounded durable append."""

    generation = owner.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int):
        return {"queued": False, "duplicate": False, "code": "QUEUE_OWNER_REJECTED", "authoritative": False}
    event = notification_from_lifecycle(
        lifecycle,
        owner_id=str(owner.get("owner_id") or ""),
        generation=generation,
        owner_nonce=str(owner.get("owner_nonce") or ""),
    )
    if event is None:
        return {"queued": False, "duplicate": False, "code": "EVENT_NOT_MAPPED", "authoritative": False}
    return enqueue_notification(root, event, owner=owner)


def _next_record(directory: Path, cursor: Mapping[str, Any]) -> tuple[dict[str, Any] | None, int, int, str | None]:
    segments = _segments(directory)
    if not segments:
        return None, int(cursor["segment"]), int(cursor["byte_offset"]), None
    segment = int(cursor["segment"])
    candidates = [number for number in segments if number >= segment]
    if not candidates:
        return None, segment, int(cursor["byte_offset"]), None
    selected = candidates[0]
    offset = int(cursor["byte_offset"]) if selected == segment else 0
    rows = _complete_lines(_segment_path(directory, selected), offset)
    if not rows:
        later = [number for number in candidates if number > selected]
        if not later:
            return None, selected, offset, None
        selected = later[0]
        rows = _complete_lines(_segment_path(directory, selected), 0)
        offset = 0
    if not rows:
        return None, selected, offset, None
    _start, end, raw = rows[0]
    try:
        record = _validate_record(parse_canonical_json_bytes(raw))
    except (UnicodeError, ValueError, TypeError):
        record = None
    return record, selected, end, None if record is not None else sha256_hex(raw)


def _append_dead_letter(directory: Path, record: Mapping[str, Any] | None, *, code: str, raw_hash: str | None = None) -> None:
    event = record.get("event") if isinstance(record, Mapping) else None
    dead = {
        "store_kind": "omg_notification_dead_letter",
        "schema_version": 1,
        "record_id": record.get("record_id") if isinstance(record, Mapping) else None,
        "event_id": event.get("event_id") if isinstance(event, Mapping) else None,
        "dedupe_key": event.get("dedupe_key") if isinstance(event, Mapping) else None,
        "failed_at": _utc_now(),
        "code": code,
        "raw_sha256": raw_hash,
        "authoritative": False,
    }
    body = canonical_json_bytes(dead)
    segments = _segments(directory, dead_letter=True)
    segment = segments[-1] if segments else 1
    path = _segment_path(directory, segment, dead_letter=True)
    _trim_partial_tail(path)
    if (
        path.exists()
        and (path.stat().st_size + len(body) + 1 > MAX_SEGMENT_BYTES or _record_count(path) >= MAX_SEGMENT_RECORDS)
    ):
        segment += 1
        path = _segment_path(directory, segment, dead_letter=True)
    retained = sorted({*segments, segment})
    while len(retained) > MAX_QUEUE_SEGMENTS:
        expired = retained.pop(0)
        _segment_path(directory, expired, dead_letter=True).unlink(missing_ok=True)
    _append_line(path, body)


def _append_retry(
    directory: Path, cursor: Mapping[str, Any], record: Mapping[str, Any], *, now: datetime
) -> bool:
    attempt = int(record["attempt"]) + 1
    delay = min(MAX_BACKOFF_SECONDS, float(2 ** max(0, attempt - 1)))
    not_before = (now + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z")
    provisional = _build_record(
        record["event"],
        segment=1,
        record_seq=0,
        enqueued_at=record["enqueued_at"],
        not_before=not_before,
        attempt=attempt,
        max_attempts=record["max_attempts"],
        retry_of=record["record_id"],
    )
    segment = _select_segment(directory, cursor, len(canonical_json_bytes(provisional)))
    if segment is None:
        _append_dead_letter(directory, record, code="QUEUE_FULL_DURING_RETRY")
        return False
    path = _segment_path(directory, segment)
    retry = _build_record(
        record["event"],
        segment=segment,
        record_seq=_record_count(path),
        enqueued_at=record["enqueued_at"],
        not_before=not_before,
        attempt=attempt,
        max_attempts=record["max_attempts"],
        retry_of=record["record_id"],
    )
    _append_line(path, canonical_json_bytes(retry))
    return True


@contextmanager
def _worker_lease(directory: Path):
    """Serialize queue processors without blocking the queue append lock."""

    descriptor = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def process_notification_queue(
    root: Path | str,
    config: Mapping[str, Any],
    *,
    owner: Mapping[str, Any],
    max_records: int = 32,
    rate_limit_per_second: float = 10.0,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], Any] = time.sleep,
    dispatcher: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Process bounded records, advancing only after durable outcome handling."""

    if isinstance(max_records, bool) or not isinstance(max_records, int) or not 1 <= max_records <= MAX_PROCESS_RECORDS:
        raise ValueError("max_records is outside bounds")
    if isinstance(rate_limit_per_second, bool) or not isinstance(rate_limit_per_second, (int, float)) or not 0 < rate_limit_per_second <= 100:
        raise ValueError("rate limit is outside bounds")
    dispatch: Callable[..., list[dict[str, Any]]]
    if dispatcher is None:
        from omg_cli.notify.dispatcher import dispatch_notifications

        dispatch = dispatch_notifications
    else:
        dispatch = dispatcher
    directory, cursor_path, lock_path = _paths(root)
    ensure_managed_dir(directory)
    processed = delivered = failed = dead_lettered = duplicates = 0
    previous_dispatch: float | None = None
    with _worker_lease(directory):
        for _ in range(max_records):
            count_delivery = False
            with exclusive_lock(lock_path):
                cursor = _load_cursor(cursor_path)
                record, segment, end_offset, raw_hash = _next_record(directory, cursor)
            if record is None and raw_hash is None:
                break
            current_time = now().astimezone(timezone.utc)
            if record is not None:
                due = _parse_time(record["not_before"])
                if due is None or due > current_time:
                    break
                is_duplicate = record["attempt"] == 0 and cursor.get("last_dedupe_key") == record["event"]["dedupe_key"]
                outcomes: list[dict[str, Any]] = []
                if is_duplicate:
                    duplicates += 1
                    success = True
                else:
                    if previous_dispatch is not None:
                        wait = (1.0 / float(rate_limit_per_second)) - (time.monotonic() - previous_dispatch)
                        if wait > 0:
                            sleep(wait)
                    try:
                        outcomes = dispatch(record["event"], config, owner=dict(owner))
                    except Exception:  # noqa: BLE001 - queue delivery never owns workflow outcome
                        outcomes = [
                            {
                                "status": "failed",
                                "code": "NOTIFICATION_DISPATCH_FAILED",
                                "authoritative": False,
                            }
                        ]
                    previous_dispatch = time.monotonic()
                    # A globally disabled/absent destination is a documented soft
                    # condition.  It consumes the queue record without retries.
                    success = all(row.get("status") in {"delivered", "skipped"} for row in outcomes)
                    count_delivery = any(row.get("status") == "delivered" for row in outcomes)
            else:
                success = False
                outcomes = []
            with exclusive_lock(lock_path):
                latest = _load_cursor(cursor_path)
                if latest != cursor:
                    continue
                if record is None:
                    _append_dead_letter(directory, None, code="QUEUE_RECORD_INVALID", raw_hash=raw_hash)
                    dead_lettered += 1
                    event_id = latest.get("last_event_id")
                    dedupe_key = latest.get("last_dedupe_key")
                    record_seq = int(latest["record_seq"]) + 1
                else:
                    event_id = record["event"]["event_id"]
                    dedupe_key = record["event"]["dedupe_key"]
                    record_seq = record["record_seq"]
                    if success:
                        if count_delivery:
                            delivered += 1
                    else:
                        failed += 1
                        if record["attempt"] + 1 < record["max_attempts"]:
                            if not _append_retry(directory, latest, record, now=current_time):
                                dead_lettered += 1
                        else:
                            _append_dead_letter(directory, record, code="DELIVERY_ATTEMPTS_EXHAUSTED")
                            dead_lettered += 1
                updated = {
                    "schema_version": 1,
                    "segment": segment,
                    "record_seq": record_seq,
                    "byte_offset": end_offset,
                    "last_event_id": event_id,
                    "last_dedupe_key": dedupe_key,
                }
                atomic_write_bytes(cursor_path, canonical_json_bytes(updated), mode=DATA_FILE_MODE, replace=True)
            processed += 1
    return {
        "processed": processed,
        "delivered": delivered,
        "failed": failed,
        "dead_lettered": dead_lettered,
        "duplicates": duplicates,
        "authoritative": False,
    }


__all__ = [
    "enqueue_lifecycle_notification",
    "enqueue_notification",
    "process_notification_queue",
]
