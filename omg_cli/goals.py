# omg_cli/goals.py
"""Repo-native durable goal ledger with hash-chained checkpoints and repair.

Only the omg CLI may mutate authoritative goal snapshots and ledger events.
Agent/model files are proposals and become durable only through a distinct
CLI-stamped import event.
"""
from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from omg_cli.evidence import (
    CLI_WRITER,
    assert_safe_supervised_parent,
    sha256_bytes,
    sha256_file,
    validate_identifier,
)
from omg_cli.state import (
    LifecycleLockError,
    load_run,
    _require_posix_flock,
    _flock_bounded,
)


GENESIS_HASH = "0" * 64
EVENT_TYPES = frozenset(
    {
        "goal_created",
        "run_linked",
        "story_started",
        "checkpoint",
        "story_blocked",
        "story_resumed",
        "story_completed",
        "proposal_imported",
        "goal_verified",
        "ledger_repaired",
        "forensic_blocker",
    }
)


class GoalError(ValueError):
    """Invalid goal operation or corrupt ledger."""


class GoalRepairRefused(GoalError):
    """Automatic repair is not eligible; forensic restore required."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def ultragoal_root(root: Path | str) -> Path:
    return Path(root).resolve() / ".omg" / "ultragoal"


def goal_dir(root: Path | str, goal_id: str) -> Path:
    goal_id = validate_identifier(goal_id, label="goal_id")
    return ultragoal_root(root) / "goals" / goal_id


def ledger_path(root: Path | str, goal_id: str) -> Path:
    return goal_dir(root, goal_id) / "ledger.jsonl"


def snapshot_path(root: Path | str, goal_id: str) -> Path:
    return goal_dir(root, goal_id) / "snapshot.json"


def goal_lock_path(root: Path | str, goal_id: str) -> Path:
    return goal_dir(root, goal_id) / "goal.lock"


def backups_dir(root: Path | str) -> Path:
    return ultragoal_root(root) / "backups"


def _atomic_write_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        if os.name == "posix":
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
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


def compute_event_hash(prev_hash: str, event_without_hash: Mapping[str, Any]) -> str:
    payload = dict(event_without_hash)
    payload.pop("event_hash", None)
    body = prev_hash.encode("ascii") + b"\n" + _canonical(payload)
    return sha256_bytes(body)


def _validate_story_graph(stories: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not stories:
        raise GoalError("at least one story is required")
    by_id: dict[str, dict[str, Any]] = {}
    for raw in stories:
        if not isinstance(raw, Mapping):
            raise GoalError("each story must be an object")
        story_id = validate_identifier(str(raw.get("id", "")), label="story_id")
        if story_id in by_id:
            raise GoalError(f"duplicate story id: {story_id}")
        acceptance = str(raw.get("acceptance") or "").strip()
        if not acceptance:
            raise GoalError(f"story {story_id} missing acceptance")
        depends_on = raw.get("depends_on") or []
        if not isinstance(depends_on, list) or any(
            not isinstance(d, str) for d in depends_on
        ):
            raise GoalError(f"story {story_id} depends_on must be a string list")
        dep_ids = [validate_identifier(d, label="depends_on") for d in depends_on]
        title = str(raw.get("title") or story_id).strip()
        by_id[story_id] = {
            "id": story_id,
            "title": title,
            "depends_on": dep_ids,
            "acceptance": acceptance,
            "status": "pending",
            "block_reason": None,
            "next_action": None,
            "evidence": [],
            "checkpoints": 0,
        }
    # unknown deps + cycles (Kahn)
    for sid, story in by_id.items():
        for dep in story["depends_on"]:
            if dep not in by_id:
                raise GoalError(f"story {sid} depends on unknown story {dep}")
    remaining = {sid: set(s["depends_on"]) for sid, s in by_id.items()}
    ready = [sid for sid, deps in remaining.items() if not deps]
    ordered: list[str] = []
    while ready:
        sid = ready.pop()
        ordered.append(sid)
        for other, deps in remaining.items():
            if sid in deps:
                deps.remove(sid)
                if not deps and other not in ordered:
                    ready.append(other)
    if len(ordered) != len(by_id):
        raise GoalError("story dependency graph contains a cycle")
    # initial readiness
    for sid, story in by_id.items():
        if not story["depends_on"]:
            story["status"] = "ready"
        else:
            story["status"] = "pending"
    return by_id


def _refresh_ready(stories: dict[str, dict[str, Any]]) -> None:
    for story in stories.values():
        if story["status"] not in {"pending", "ready"}:
            continue
        deps_ok = all(
            stories[d]["status"] == "complete" for d in story["depends_on"]
        )
        if deps_ok:
            if story["status"] == "pending":
                story["status"] = "ready"
        else:
            story["status"] = "pending"


@contextmanager
def goal_lock(
    root: Path | str, goal_id: str, *, timeout_s: float = 10.0
) -> Iterator[None]:
    """Serialize append/repair for one goal."""
    try:
        _require_posix_flock()
    except LifecycleLockError as exc:
        raise GoalError(str(exc)) from exc
    path = goal_lock_path(root, goal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lockf = path.open("a+", encoding="utf-8")
    try:
        try:
            _flock_bounded(
                lockf,
                timeout_s=timeout_s,
                label=f"goal.lock for {goal_id}",
            )
        except LifecycleLockError as exc:
            raise GoalError(str(exc)) from exc
        yield
    finally:
        try:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lockf.close()


def _read_snapshot(root: Path, goal_id: str) -> dict[str, Any]:
    path = snapshot_path(root, goal_id)
    if not path.is_file():
        raise GoalError(f"goal snapshot missing: {goal_id}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoalError(f"goal snapshot unreadable: {goal_id}") from exc
    if not isinstance(data, dict):
        raise GoalError(f"goal snapshot must be object: {goal_id}")
    if data.get("writer") != CLI_WRITER:
        raise GoalError("goal snapshot lacks CLI writer authority")
    return data


def _scan_ledger(
    path: Path,
) -> tuple[list[dict[str, Any]], int | None, str | None, dict[str, Any] | None]:
    """Return (valid_prefix_events, boundary_seq_or_None, failure_reason, diagnosis).

    On a fully valid ledger: failure_reason is None and boundary is None.
    On eligible final-tail damage: valid_prefix is contiguous good events,
    diagnosis.kind in {truncated_tail, invalid_final_json}.
    On mid-chain/hash corruption: valid_prefix empty for refusal path,
    diagnosis.kind is forensic and automatic repair refused.
    """
    if not path.is_file():
        return [], None, "ledger missing", {"kind": "missing", "eligible": False}

    raw = path.read_bytes()
    if not raw:
        return [], None, None, {"kind": "empty", "eligible": False}

    lines = raw.splitlines(keepends=True)
    events: list[dict[str, Any]] = []
    prev_hash = GENESIS_HASH
    expected_seq = 1

    for idx, line in enumerate(lines):
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            # blank line only allowed as nothing; treat as damage at this index
            if idx == len(lines) - 1:
                return (
                    events,
                    expected_seq - 1 if events else 0,
                    "truncated or blank final line",
                    {
                        "kind": "truncated_tail",
                        "eligible": True,
                        "valid_prefix_events": len(events),
                        "valid_prefix_seq": events[-1]["sequence"] if events else 0,
                        "valid_prefix_hash": events[-1]["event_hash"]
                        if events
                        else GENESIS_HASH,
                        "line_index": idx,
                    },
                )
            return (
                [],
                None,
                f"blank line mid-ledger at line {idx + 1}",
                {
                    "kind": "mid_chain_blank",
                    "eligible": False,
                    "line_index": idx,
                },
            )
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            if idx == len(lines) - 1:
                return (
                    events,
                    expected_seq - 1 if events else 0,
                    f"invalid final JSON: {exc}",
                    {
                        "kind": "invalid_final_json",
                        "eligible": True,
                        "valid_prefix_events": len(events),
                        "valid_prefix_seq": events[-1]["sequence"] if events else 0,
                        "valid_prefix_hash": events[-1]["event_hash"]
                        if events
                        else GENESIS_HASH,
                        "line_index": idx,
                        "error": str(exc),
                    },
                )
            return (
                [],
                None,
                f"invalid JSON mid-ledger at line {idx + 1}: {exc}",
                {
                    "kind": "mid_chain_invalid_json",
                    "eligible": False,
                    "line_index": idx,
                    "error": str(exc),
                },
            )
        if not isinstance(obj, dict):
            return (
                [],
                None,
                f"non-object event at line {idx + 1}",
                {"kind": "non_object", "eligible": False, "line_index": idx},
            )
        seq = obj.get("sequence")
        if seq != expected_seq:
            return (
                [],
                None,
                f"sequence gap at line {idx + 1}: expected {expected_seq} got {seq!r}",
                {
                    "kind": "sequence_gap",
                    "eligible": False,
                    "line_index": idx,
                    "expected": expected_seq,
                    "got": seq,
                },
            )
        if obj.get("prev_hash") != prev_hash:
            return (
                [],
                None,
                f"prev_hash mismatch at sequence {seq}",
                {
                    "kind": "prev_hash_mismatch",
                    "eligible": False,
                    "line_index": idx,
                    "sequence": seq,
                },
            )
        event_hash = obj.get("event_hash")
        without = {k: v for k, v in obj.items() if k != "event_hash"}
        recomputed = compute_event_hash(prev_hash, without)
        if event_hash != recomputed:
            return (
                [],
                None,
                f"event_hash mismatch at sequence {seq}",
                {
                    "kind": "event_hash_mismatch",
                    "eligible": False,
                    "line_index": idx,
                    "sequence": seq,
                },
            )
        events.append(obj)
        prev_hash = str(event_hash)
        expected_seq += 1

    # Detect truncated final line without newline when last line incomplete JSON
    # already handled above. Full valid:
    return events, None, None, {"kind": "valid", "eligible": False, "events": len(events)}


def _assert_snapshot_matches_ledger(
    snapshot: Mapping[str, Any], events: list[dict[str, Any]]
) -> None:
    if not events:
        if snapshot.get("tail_sequence", 0) not in (0, None):
            raise GoalError("snapshot tail disagrees with empty ledger")
        if snapshot.get("tail_hash", GENESIS_HASH) not in (GENESIS_HASH, None):
            if snapshot.get("tail_hash") != GENESIS_HASH:
                raise GoalError("snapshot tail hash disagrees with empty ledger")
        return
    last = events[-1]
    if snapshot.get("tail_sequence") != last["sequence"]:
        raise GoalError(
            "snapshot/ledger tail sequence disagreement "
            f"(snapshot={snapshot.get('tail_sequence')!r} "
            f"ledger={last['sequence']!r})"
        )
    if snapshot.get("tail_hash") != last["event_hash"]:
        raise GoalError("snapshot/ledger tail hash disagreement")


def _load_consistent(root: Path, goal_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    snapshot = _read_snapshot(root, goal_id)
    path = ledger_path(root, goal_id)
    events, _boundary, reason, diagnosis = _scan_ledger(path)
    if reason is not None:
        # any corruption blocks mutation until repair
        raise GoalError(f"ledger not appendable: {reason}")
    try:
        _assert_snapshot_matches_ledger(snapshot, events)
    except GoalError:
        # unexplained snapshot disagreement is forensic (not auto-repair)
        raise GoalError(
            "unexplained snapshot/ledger disagreement; forensic restore required"
        )
    return snapshot, events


def _build_event(
    *,
    sequence: int,
    prev_hash: str,
    event_type: str,
    goal_id: str,
    payload: Mapping[str, Any],
    invocation_id: str | None = None,
) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise GoalError(f"unknown event type: {event_type}")
    event: dict[str, Any] = {
        "schema_version": 1,
        "writer": CLI_WRITER,
        "sequence": sequence,
        "prev_hash": prev_hash,
        "type": event_type,
        "goal_id": goal_id,
        "ts": _utc_now(),
        "payload": dict(payload),
    }
    if invocation_id:
        event["invocation_id"] = invocation_id
    event["event_hash"] = compute_event_hash(prev_hash, event)
    return event


def _append_event_and_snapshot(
    root: Path,
    goal_id: str,
    snapshot: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    path = ledger_path(root, goal_id)
    line = (
        json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    snapshot = dict(snapshot)
    snapshot["tail_sequence"] = event["sequence"]
    snapshot["tail_hash"] = event["event_hash"]
    snapshot["updated_at"] = event["ts"]
    snapshot["revision"] = int(snapshot.get("revision") or 0) + 1
    snapshot["writer"] = CLI_WRITER
    _atomic_write_json(snapshot_path(root, goal_id), snapshot)
    return snapshot


def init_goal(
    root: Path | str,
    goal_id: str,
    stories: list[dict[str, Any]],
    *,
    title: str | None = None,
    source_spec_hash: str | None = None,
    source_plan_hash: str | None = None,
    objective: str | None = None,
) -> dict[str, Any]:
    """Create a dependency-valid goal with empty hash-chained ledger."""
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    goal_id = validate_identifier(goal_id, label="goal_id")
    story_map = _validate_story_graph(stories)
    gdir = goal_dir(root, goal_id)
    if snapshot_path(root, goal_id).exists() or ledger_path(root, goal_id).exists():
        raise GoalError(f"goal already exists: {goal_id}")

    with goal_lock(root, goal_id):
        if snapshot_path(root, goal_id).exists():
            raise GoalError(f"goal already exists: {goal_id}")
        invocation_id = uuid.uuid4().hex
        payload = {
            "title": (title or goal_id).strip(),
            "objective": (objective or "").strip(),
            "source_spec_hash": source_spec_hash,
            "source_plan_hash": source_plan_hash,
            "stories": list(story_map.values()),
        }
        event = _build_event(
            sequence=1,
            prev_hash=GENESIS_HASH,
            event_type="goal_created",
            goal_id=goal_id,
            payload=payload,
            invocation_id=invocation_id,
        )
        snapshot = {
            "schema_version": 1,
            "writer": CLI_WRITER,
            "goal_id": goal_id,
            "title": payload["title"],
            "objective": payload["objective"],
            "status": "active",
            "verified": False,
            "stories": story_map,
            "linked_runs": [],
            "source_spec_hash": source_spec_hash,
            "source_plan_hash": source_plan_hash,
            "tail_sequence": 0,
            "tail_hash": GENESIS_HASH,
            "revision": 0,
            "created_at": event["ts"],
            "updated_at": event["ts"],
            "blocker": None,
            "created_by_invocation_id": invocation_id,
        }
        gdir.mkdir(parents=True, exist_ok=True)
        # write empty then append
        ledger_path(root, goal_id).write_bytes(b"")
        snapshot = _append_event_and_snapshot(root, goal_id, snapshot, event)
    return goal_status(root, goal_id)


def link_run(root: Path | str, goal_id: str, run_id: str) -> dict[str, Any]:
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    goal_id = validate_identifier(goal_id, label="goal_id")
    run_id = validate_identifier(run_id, label="run_id")
    run = load_run(root, run_id)
    if run is None:
        raise GoalError(f"run not found: {run_id}")
    with goal_lock(root, goal_id):
        snapshot, events = _load_consistent(root, goal_id)
        if run_id in snapshot.get("linked_runs", []):
            return goal_status(root, goal_id)
        prev = events[-1]["event_hash"] if events else GENESIS_HASH
        seq = (events[-1]["sequence"] if events else 0) + 1
        event = _build_event(
            sequence=seq,
            prev_hash=prev,
            event_type="run_linked",
            goal_id=goal_id,
            payload={"run_id": run_id, "run_mode": run.get("mode"), "run_status": run.get("status")},
            invocation_id=uuid.uuid4().hex,
        )
        linked = list(snapshot.get("linked_runs") or [])
        linked.append(run_id)
        snapshot["linked_runs"] = linked
        _append_event_and_snapshot(root, goal_id, snapshot, event)
    return goal_status(root, goal_id)


def _get_story(snapshot: dict[str, Any], story_id: str) -> dict[str, Any]:
    stories = snapshot.get("stories") or {}
    if story_id not in stories:
        raise GoalError(f"unknown story: {story_id}")
    return stories[story_id]


def start_story(root: Path | str, goal_id: str, story_id: str) -> dict[str, Any]:
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    goal_id = validate_identifier(goal_id, label="goal_id")
    story_id = validate_identifier(story_id, label="story_id")
    with goal_lock(root, goal_id):
        snapshot, events = _load_consistent(root, goal_id)
        if snapshot.get("status") == "forensic":
            raise GoalError("goal is forensic-blocked; restore before mutation")
        _refresh_ready(snapshot["stories"])
        story = _get_story(snapshot, story_id)
        if story["status"] == "blocked":
            raise GoalError(f"story {story_id} is blocked; use resume")
        if story["status"] != "ready":
            raise GoalError(
                f"story {story_id} is not ready (status={story['status']})"
            )
        story["status"] = "in_progress"
        story["block_reason"] = None
        prev = events[-1]["event_hash"]
        seq = events[-1]["sequence"] + 1
        event = _build_event(
            sequence=seq,
            prev_hash=prev,
            event_type="story_started",
            goal_id=goal_id,
            payload={"story_id": story_id},
            invocation_id=uuid.uuid4().hex,
        )
        _append_event_and_snapshot(root, goal_id, snapshot, event)
    return goal_status(root, goal_id)


def checkpoint(
    root: Path | str,
    goal_id: str,
    story_id: str,
    *,
    evidence_path: str | Path,
    message: str,
) -> dict[str, Any]:
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    goal_id = validate_identifier(goal_id, label="goal_id")
    story_id = validate_identifier(story_id, label="story_id")
    message = (message or "").strip()
    if not message:
        raise GoalError("checkpoint message is required")
    epath = Path(evidence_path)
    if not epath.is_file():
        # allow relative to root
        candidate = root / epath
        if candidate.is_file():
            epath = candidate
        else:
            raise GoalError(f"evidence file missing: {evidence_path}")
    digest = sha256_file(epath)
    try:
        rel = str(epath.resolve().relative_to(root))
    except ValueError:
        rel = str(epath.resolve())

    with goal_lock(root, goal_id):
        snapshot, events = _load_consistent(root, goal_id)
        story = _get_story(snapshot, story_id)
        if story["status"] != "in_progress":
            raise GoalError(
                f"checkpoint requires in_progress story (got {story['status']})"
            )
        story["checkpoints"] = int(story.get("checkpoints") or 0) + 1
        story["evidence"] = list(story.get("evidence") or []) + [
            {"path": rel, "sha256": digest, "message": message, "at": _utc_now()}
        ]
        prev = events[-1]["event_hash"]
        seq = events[-1]["sequence"] + 1
        event = _build_event(
            sequence=seq,
            prev_hash=prev,
            event_type="checkpoint",
            goal_id=goal_id,
            payload={
                "story_id": story_id,
                "message": message,
                "evidence_path": rel,
                "evidence_sha256": digest,
            },
            invocation_id=uuid.uuid4().hex,
        )
        _append_event_and_snapshot(root, goal_id, snapshot, event)
    return goal_status(root, goal_id)


def block_story(
    root: Path | str,
    goal_id: str,
    story_id: str,
    *,
    reason: str,
    next_action: str | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    goal_id = validate_identifier(goal_id, label="goal_id")
    story_id = validate_identifier(story_id, label="story_id")
    reason = (reason or "").strip()
    if not reason:
        raise GoalError("block reason is required")
    with goal_lock(root, goal_id):
        snapshot, events = _load_consistent(root, goal_id)
        story = _get_story(snapshot, story_id)
        if story["status"] not in {"in_progress", "ready"}:
            raise GoalError(f"cannot block story in status {story['status']}")
        story["status"] = "blocked"
        story["block_reason"] = reason
        story["next_action"] = (next_action or "").strip() or None
        snapshot["status"] = "blocked"
        snapshot["blocker"] = {
            "story_id": story_id,
            "reason": reason,
            "next_action": story["next_action"],
        }
        prev = events[-1]["event_hash"]
        seq = events[-1]["sequence"] + 1
        event = _build_event(
            sequence=seq,
            prev_hash=prev,
            event_type="story_blocked",
            goal_id=goal_id,
            payload={
                "story_id": story_id,
                "reason": reason,
                "next_action": story["next_action"],
            },
            invocation_id=uuid.uuid4().hex,
        )
        _append_event_and_snapshot(root, goal_id, snapshot, event)
    return goal_status(root, goal_id)


def resume_story(root: Path | str, goal_id: str, story_id: str) -> dict[str, Any]:
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    goal_id = validate_identifier(goal_id, label="goal_id")
    story_id = validate_identifier(story_id, label="story_id")
    with goal_lock(root, goal_id):
        snapshot, events = _load_consistent(root, goal_id)
        story = _get_story(snapshot, story_id)
        if story["status"] != "blocked":
            raise GoalError(f"story {story_id} is not blocked")
        story["status"] = "in_progress"
        story["block_reason"] = None
        story["next_action"] = None
        if snapshot.get("blocker", {}).get("story_id") == story_id:
            snapshot["blocker"] = None
            snapshot["status"] = "active"
        prev = events[-1]["event_hash"]
        seq = events[-1]["sequence"] + 1
        event = _build_event(
            sequence=seq,
            prev_hash=prev,
            event_type="story_resumed",
            goal_id=goal_id,
            payload={"story_id": story_id},
            invocation_id=uuid.uuid4().hex,
        )
        _append_event_and_snapshot(root, goal_id, snapshot, event)
    return goal_status(root, goal_id)


def complete_story(root: Path | str, goal_id: str, story_id: str) -> dict[str, Any]:
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    goal_id = validate_identifier(goal_id, label="goal_id")
    story_id = validate_identifier(story_id, label="story_id")
    with goal_lock(root, goal_id):
        snapshot, events = _load_consistent(root, goal_id)
        story = _get_story(snapshot, story_id)
        if story["status"] != "in_progress":
            raise GoalError(
                f"complete requires in_progress story (got {story['status']})"
            )
        if int(story.get("checkpoints") or 0) < 1:
            raise GoalError("complete requires at least one checkpoint with evidence")
        story["status"] = "complete"
        story["block_reason"] = None
        _refresh_ready(snapshot["stories"])
        if all(s["status"] == "complete" for s in snapshot["stories"].values()):
            snapshot["status"] = "complete"
        elif snapshot.get("status") == "blocked" and not snapshot.get("blocker"):
            snapshot["status"] = "active"
        prev = events[-1]["event_hash"]
        seq = events[-1]["sequence"] + 1
        event = _build_event(
            sequence=seq,
            prev_hash=prev,
            event_type="story_completed",
            goal_id=goal_id,
            payload={"story_id": story_id},
            invocation_id=uuid.uuid4().hex,
        )
        _append_event_and_snapshot(root, goal_id, snapshot, event)
    return goal_status(root, goal_id)


def import_proposal_event(
    root: Path | str,
    goal_id: str,
    *,
    proposal_path: str | Path,
    proposal_sha256: str,
    note: str = "",
) -> dict[str, Any]:
    """Record a CLI-validated proposal import (does not trust agent writes)."""
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    goal_id = validate_identifier(goal_id, label="goal_id")
    p = Path(proposal_path)
    if not p.is_file():
        candidate = root / p
        if candidate.is_file():
            p = candidate
        else:
            raise GoalError(f"proposal missing: {proposal_path}")
    actual = sha256_file(p)
    if actual != proposal_sha256:
        raise GoalError("proposal sha256 mismatch; refusing import")
    # proposals must live under proposals root
    try:
        rel = str(p.resolve().relative_to(root))
    except ValueError as exc:
        raise GoalError("proposal path must be inside project root") from exc
    if not rel.startswith(".omg/artifacts/proposals/"):
        raise GoalError("proposal must live under .omg/artifacts/proposals/")
    with goal_lock(root, goal_id):
        snapshot, events = _load_consistent(root, goal_id)
        prev = events[-1]["event_hash"]
        seq = events[-1]["sequence"] + 1
        event = _build_event(
            sequence=seq,
            prev_hash=prev,
            event_type="proposal_imported",
            goal_id=goal_id,
            payload={
                "proposal_path": rel,
                "proposal_sha256": actual,
                "note": (note or "").strip(),
                "stamp": "omg-cli-import",
            },
            invocation_id=uuid.uuid4().hex,
        )
        _append_event_and_snapshot(root, goal_id, snapshot, event)
    return goal_status(root, goal_id)


def verify_goal(root: Path | str, goal_id: str, *, run_id: str | None = None) -> dict[str, Any]:
    """Mark goal verified only when a linked run is CLI-verified."""
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    goal_id = validate_identifier(goal_id, label="goal_id")
    with goal_lock(root, goal_id):
        snapshot, events = _load_consistent(root, goal_id)
        if snapshot.get("verified") is True:
            return goal_status(root, goal_id)
        if snapshot.get("status") != "complete":
            raise GoalError("goal cannot verify until all stories are complete")
        linked = list(snapshot.get("linked_runs") or [])
        if not linked:
            raise GoalError("goal cannot verify without a linked run")
        candidates = [run_id] if run_id else linked
        from omg_cli.acceptance import (
            is_cli_acceptance_result,
            is_trusted_acceptance,
        )

        verified_run = None
        for rid in candidates:
            if rid not in linked:
                raise GoalError(f"run {rid} is not linked to goal")
            run = load_run(root, rid)
            if run is None:
                continue
            # Disk status alone is insufficient. Prefer same-process token;
            # allow multi-process CLI path when acceptance.result is a valid
            # CLI stamp matching frozen manifest (require_token=False).
            disk_verified = (
                run.get("verified") is True or run.get("status") == "verified"
            )
            if not disk_verified:
                continue
            trusted = False
            cli_stamped = False
            try:
                trusted = bool(is_trusted_acceptance(root, rid))
            except Exception:
                trusted = False
            try:
                cli_stamped = bool(
                    is_cli_acceptance_result(
                        None, root=root, run_id=rid, require_token=False
                    )
                )
            except Exception:
                cli_stamped = False
            if trusted or cli_stamped:
                verified_run = rid
                break
            # Disk-verified without stamp: try next linked run (do not abort list)
            continue
        if verified_run is None:
            raise GoalError(
                "goal cannot verify before a linked run is CLI-verified "
                "(need disk verified + CLI acceptance stamp or same-process token)"
            )
        prev = events[-1]["event_hash"]
        seq = events[-1]["sequence"] + 1
        event = _build_event(
            sequence=seq,
            prev_hash=prev,
            event_type="goal_verified",
            goal_id=goal_id,
            payload={"run_id": verified_run},
            invocation_id=uuid.uuid4().hex,
        )
        snapshot["verified"] = True
        snapshot["status"] = "verified"
        snapshot["verified_run_id"] = verified_run
        snapshot["verified_at"] = event["ts"]
        _append_event_and_snapshot(root, goal_id, snapshot, event)
    return goal_status(root, goal_id)


def goal_status(root: Path | str, goal_id: str) -> dict[str, Any]:
    root = Path(root).resolve()
    goal_id = validate_identifier(goal_id, label="goal_id")
    # status may be read even when ledger is corrupt; surface diagnosis
    path = ledger_path(root, goal_id)
    try:
        snapshot = _read_snapshot(root, goal_id)
    except GoalError as exc:
        return {"ok": False, "goal_id": goal_id, "error": str(exc)}
    events, _b, reason, diagnosis = _scan_ledger(path)
    healthy = reason is None
    if healthy:
        try:
            _assert_snapshot_matches_ledger(snapshot, events)
        except GoalError as exc:
            healthy = False
            reason = str(exc)
            diagnosis = {
                "kind": "snapshot_disagreement",
                "eligible": False,
                "error": str(exc),
            }
    return {
        "ok": healthy,
        "goal_id": goal_id,
        "title": snapshot.get("title"),
        "status": snapshot.get("status"),
        "verified": snapshot.get("verified"),
        "stories": snapshot.get("stories"),
        "linked_runs": snapshot.get("linked_runs"),
        "tail_sequence": snapshot.get("tail_sequence"),
        "tail_hash": snapshot.get("tail_hash"),
        "blocker": snapshot.get("blocker"),
        "revision": snapshot.get("revision"),
        "ledger_healthy": healthy,
        "ledger_error": reason,
        "diagnosis": diagnosis if not healthy else None,
        "event_count": len(events) if healthy else diagnosis.get("valid_prefix_events"),
    }


def diagnose_repair(root: Path | str, goal_id: str) -> dict[str, Any]:
    root = Path(root).resolve()
    goal_id = validate_identifier(goal_id, label="goal_id")
    path = ledger_path(root, goal_id)
    if not path.is_file():
        raise GoalError(f"ledger missing: {goal_id}")
    original_sha = sha256_file(path)
    events, _boundary, reason, diagnosis = _scan_ledger(path)
    snapshot = None
    try:
        snapshot = _read_snapshot(root, goal_id)
    except GoalError as exc:
        diagnosis = {
            "kind": "snapshot_unreadable",
            "eligible": False,
            "error": str(exc),
        }
        reason = str(exc)
    if reason is None and snapshot is not None:
        try:
            _assert_snapshot_matches_ledger(snapshot, events)
        except GoalError as exc:
            reason = str(exc)
            diagnosis = {
                "kind": "snapshot_disagreement",
                "eligible": False,
                "error": str(exc),
            }
    eligible = bool(diagnosis and diagnosis.get("eligible"))
    return {
        "ok": reason is None,
        "goal_id": goal_id,
        "original_sha256": original_sha,
        "eligible_for_tail_repair": eligible,
        "reason": reason,
        "diagnosis": diagnosis,
        "valid_prefix_events": (
            diagnosis.get("valid_prefix_events")
            if diagnosis and diagnosis.get("eligible")
            else (len(events) if reason is None else 0)
        ),
        "valid_prefix_seq": (
            diagnosis.get("valid_prefix_seq")
            if diagnosis and diagnosis.get("eligible")
            else (events[-1]["sequence"] if events and reason is None else 0)
        ),
        "valid_prefix_hash": (
            diagnosis.get("valid_prefix_hash")
            if diagnosis and diagnosis.get("eligible")
            else (events[-1]["event_hash"] if events and reason is None else GENESIS_HASH)
        ),
    }


def repair_goal(
    root: Path | str,
    goal_id: str,
    *,
    dry_run: bool = True,
    yes: bool = False,
) -> dict[str, Any]:
    """Dry-run or confirmed tail-only repair with byte-for-byte backup."""
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    goal_id = validate_identifier(goal_id, label="goal_id")
    with goal_lock(root, goal_id):
        diagnosis = diagnose_repair(root, goal_id)
        if diagnosis["ok"]:
            return {**diagnosis, "action": "none", "message": "ledger already healthy"}
        if dry_run or not yes:
            return {
                **diagnosis,
                "action": "dry_run",
                "message": (
                    "eligible final-tail repair"
                    if diagnosis["eligible_for_tail_repair"]
                    else "automatic repair refused; forensic restore required"
                ),
            }
        if not diagnosis["eligible_for_tail_repair"]:
            # record forensic blocker on snapshot without truncating ledger
            try:
                snapshot = _read_snapshot(root, goal_id)
            except GoalError:
                snapshot = {
                    "schema_version": 1,
                    "writer": CLI_WRITER,
                    "goal_id": goal_id,
                    "status": "forensic",
                    "stories": {},
                    "linked_runs": [],
                    "tail_sequence": 0,
                    "tail_hash": GENESIS_HASH,
                    "revision": 0,
                }
            snapshot["status"] = "forensic"
            snapshot["blocker"] = {
                "kind": "forensic_restore",
                "reason": diagnosis["reason"],
                "diagnosis": diagnosis["diagnosis"],
            }
            snapshot["writer"] = CLI_WRITER
            snapshot["updated_at"] = _utc_now()
            _atomic_write_json(snapshot_path(root, goal_id), snapshot)
            raise GoalRepairRefused(
                f"automatic repair refused: {diagnosis['reason']}"
            )

        path = ledger_path(root, goal_id)
        original_bytes = path.read_bytes()
        original_sha = sha256_bytes(original_bytes)
        if original_sha != diagnosis["original_sha256"]:
            raise GoalError(
                "source ledger changed between review and repair; aborting unchanged"
            )

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_name = f"{goal_id}-{ts}-{original_sha}.ledger.jsonl"
        bdir = backups_dir(root)
        bdir.mkdir(parents=True, exist_ok=True)
        backup_path = bdir / backup_name
        try:
            with backup_path.open("wb") as handle:
                handle.write(original_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name == "posix":
                dir_fd = os.open(bdir, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except OSError as exc:
            raise GoalError(f"backup failed; active ledger unchanged: {exc}") from exc

        backup_sha = sha256_file(backup_path)
        if backup_sha != original_sha or backup_path.read_bytes() != original_bytes:
            try:
                backup_path.unlink()
            except OSError:
                pass
            raise GoalError("backup verification failed; active ledger unchanged")

        # re-check source race after backup
        if path.read_bytes() != original_bytes:
            raise GoalError(
                "source ledger changed after backup; aborting without mutation"
            )

        events, _b, reason, diag = _scan_ledger(path)
        # re-scan: for eligible tail damage, events is valid prefix
        if not diag.get("eligible"):
            raise GoalError("repair eligibility lost before mutation")
        valid_prefix = events  # _scan_ledger returns valid prefix on eligible tail

        # rebuild snapshot stories by replaying events
        snapshot = _rebuild_snapshot_from_events(goal_id, valid_prefix)
        # write truncated ledger then append ledger_repaired
        new_body = b"".join(
            (
                json.dumps(ev, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            for ev in valid_prefix
        )
        _atomic_write_bytes(path, new_body)

        prev = (
            valid_prefix[-1]["event_hash"] if valid_prefix else GENESIS_HASH
        )
        seq = (valid_prefix[-1]["sequence"] if valid_prefix else 0) + 1
        repair_event = _build_event(
            sequence=seq,
            prev_hash=prev,
            event_type="ledger_repaired",
            goal_id=goal_id,
            payload={
                "original_sha256": original_sha,
                "backup_path": str(backup_path.relative_to(root)),
                "backup_sha256": backup_sha,
                "valid_prefix_seq": diagnosis["valid_prefix_seq"],
                "valid_prefix_hash": diagnosis["valid_prefix_hash"],
                "reason": diagnosis["reason"],
                "diagnosis_kind": diagnosis["diagnosis"].get("kind"),
            },
            invocation_id=uuid.uuid4().hex,
        )
        snapshot = _append_event_and_snapshot(root, goal_id, snapshot, repair_event)
        return {
            "ok": True,
            "action": "repaired",
            "goal_id": goal_id,
            "original_sha256": original_sha,
            "backup_path": str(backup_path.relative_to(root)),
            "backup_sha256": backup_sha,
            "tail_sequence": snapshot["tail_sequence"],
            "tail_hash": snapshot["tail_hash"],
            "status": goal_status(root, goal_id),
        }


def _rebuild_snapshot_from_events(
    goal_id: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    """Rebuild authoritative snapshot by replaying CLI events."""
    if not events:
        return {
            "schema_version": 1,
            "writer": CLI_WRITER,
            "goal_id": goal_id,
            "title": goal_id,
            "objective": "",
            "status": "active",
            "verified": False,
            "stories": {},
            "linked_runs": [],
            "tail_sequence": 0,
            "tail_hash": GENESIS_HASH,
            "revision": 0,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "blocker": None,
        }
    created = events[0]
    if created.get("type") != "goal_created":
        raise GoalError("valid prefix must begin with goal_created")
    payload = created["payload"]
    stories_list = payload.get("stories") or []
    stories = {s["id"]: dict(s) for s in stories_list}
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "writer": CLI_WRITER,
        "goal_id": goal_id,
        "title": payload.get("title") or goal_id,
        "objective": payload.get("objective") or "",
        "status": "active",
        "verified": False,
        "stories": stories,
        "linked_runs": [],
        "source_spec_hash": payload.get("source_spec_hash"),
        "source_plan_hash": payload.get("source_plan_hash"),
        "tail_sequence": created["sequence"],
        "tail_hash": created["event_hash"],
        "revision": 0,
        "created_at": created.get("ts") or _utc_now(),
        "updated_at": created.get("ts") or _utc_now(),
        "blocker": None,
        "created_by_invocation_id": created.get("invocation_id"),
    }
    for event in events[1:]:
        et = event["type"]
        p = event.get("payload") or {}
        if et == "run_linked":
            rid = p.get("run_id")
            if rid and rid not in snapshot["linked_runs"]:
                snapshot["linked_runs"].append(rid)
        elif et == "story_started":
            sid = p["story_id"]
            snapshot["stories"][sid]["status"] = "in_progress"
            snapshot["stories"][sid]["block_reason"] = None
        elif et == "checkpoint":
            sid = p["story_id"]
            st = snapshot["stories"][sid]
            st["checkpoints"] = int(st.get("checkpoints") or 0) + 1
            st.setdefault("evidence", []).append(
                {
                    "path": p.get("evidence_path"),
                    "sha256": p.get("evidence_sha256"),
                    "message": p.get("message"),
                    "at": event.get("ts"),
                }
            )
        elif et == "story_blocked":
            sid = p["story_id"]
            snapshot["stories"][sid]["status"] = "blocked"
            snapshot["stories"][sid]["block_reason"] = p.get("reason")
            snapshot["stories"][sid]["next_action"] = p.get("next_action")
            snapshot["status"] = "blocked"
            snapshot["blocker"] = {
                "story_id": sid,
                "reason": p.get("reason"),
                "next_action": p.get("next_action"),
            }
        elif et == "story_resumed":
            sid = p["story_id"]
            snapshot["stories"][sid]["status"] = "in_progress"
            snapshot["stories"][sid]["block_reason"] = None
            snapshot["stories"][sid]["next_action"] = None
            if snapshot.get("blocker", {}).get("story_id") == sid:
                snapshot["blocker"] = None
                snapshot["status"] = "active"
        elif et == "story_completed":
            sid = p["story_id"]
            snapshot["stories"][sid]["status"] = "complete"
            _refresh_ready(snapshot["stories"])
            if all(s["status"] == "complete" for s in snapshot["stories"].values()):
                snapshot["status"] = "complete"
        elif et == "goal_verified":
            snapshot["verified"] = True
            snapshot["status"] = "verified"
            snapshot["verified_run_id"] = p.get("run_id")
            snapshot["verified_at"] = event.get("ts")
        elif et in {"proposal_imported", "ledger_repaired", "forensic_blocker"}:
            pass
        else:
            # unknown historical types: ignore for rebuild but keep chain
            pass
        snapshot["tail_sequence"] = event["sequence"]
        snapshot["tail_hash"] = event["event_hash"]
        snapshot["updated_at"] = event.get("ts") or snapshot["updated_at"]
    return snapshot


def list_goals(root: Path | str) -> list[dict[str, Any]]:
    root = Path(root).resolve()
    base = ultragoal_root(root) / "goals"
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / "snapshot.json").is_file():
            try:
                out.append(goal_status(root, child.name))
            except GoalError as exc:
                out.append({"ok": False, "goal_id": child.name, "error": str(exc)})
    return out


__all__ = [
    "GENESIS_HASH",
    "GoalError",
    "GoalRepairRefused",
    "block_story",
    "checkpoint",
    "complete_story",
    "compute_event_hash",
    "diagnose_repair",
    "goal_status",
    "import_proposal_event",
    "init_goal",
    "ledger_path",
    "link_run",
    "list_goals",
    "repair_goal",
    "resume_story",
    "snapshot_path",
    "start_story",
    "verify_goal",
]
