"""Durable Team host-prompt queue (#69 catalog v5).

This is **not** the mailbox and **not** a task claim/ACK path. Queued host
prompts stay ordered, remain listable while ``waiting`` is true, and can be
reordered by the leader. Host probe ``CAPABILITY_KEYS`` does not advertise
``grok.prompt_queue.*``; Team still consumes those catalogued capabilities as
an OMG-owned LEGACY queue (never silent host-TUI wiring).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
    safe_path_key,
)
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_integer,
    require_nonempty_string,
    require_safe_id,
    require_sha256,
)
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
    sha256_hex,
)
from omg_cli.host_models import CAPABILITY_KEYS, HostCapabilitySet
from omg_cli.host_probe import evaluate_feature_gate
from omg_cli.redaction import redact_value


CLI_WRITER = "omg-cli"
QUEUE_KIND = "omg.team.host_prompt_queue"
QUEUE_SCHEMA_VERSION = 1
QUEUE_FILENAME = "host_prompt_queue.json"
MAX_PROMPT_BYTES = 65_536
MAX_QUEUE_ENTRIES = 256
MAX_PREVIEW_CHARS = 120
DEFAULT_KIND = "host-prompt"
PROMPT_QUEUE_CAP_IDS: tuple[str, ...] = (
    "grok.prompt_queue.lossless_ordered",
    "grok.prompt_queue.visible_while_waiting",
    "grok.prompt_queue.reorderable",
)
# Kinds that would collapse this queue into mailbox/task protocol.
FORBIDDEN_KINDS: frozenset[str] = frozenset(
    {
        "ack",
        "claim",
        "claim-task",
        "mailbox-ack",
        "mailbox-mark-delivered",
        "message",
        "task-ack",
        "task-claim",
    }
)


class PromptQueueError(RuntimeError):
    """Host-prompt queue identity, ordering, or protocol-collision error."""

    def __init__(self, message: str, *, code: str = "E_TEAM_PROMPT_QUEUE") -> None:
        super().__init__(message)
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def queue_path(root: Path | str, run_id: str, team_id: str) -> Path:
    require_safe_id(run_id, label="run_id")
    require_safe_id(team_id, label="team_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "team"
        / safe_path_key(team_id, namespace="team")
        / QUEUE_FILENAME
    )


def mailbox_dir(root: Path | str, run_id: str, team_id: str) -> Path:
    require_safe_id(run_id, label="run_id")
    require_safe_id(team_id, label="team_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "team"
        / safe_path_key(team_id, namespace="team")
        / "mailbox"
    )


def prompt_queue_consume_receipt(
    caps: HostCapabilitySet | None = None,
) -> dict[str, Any]:
    """Honest consume receipt: host probe does not advertise queue caps."""
    host = caps if caps is not None else HostCapabilitySet()
    host_gates = []
    for cap_id in PROMPT_QUEUE_CAP_IDS:
        gate = evaluate_feature_gate(cap_id, host, required=False)
        host_gates.append(gate.to_dict())
    advertised = any(cap_id in CAPABILITY_KEYS for cap_id in PROMPT_QUEUE_CAP_IDS)
    return {
        "kind": "omg.team.host_prompt_queue.consume",
        "schema_version": 1,
        "host_capability_ids": list(PROMPT_QUEUE_CAP_IDS),
        "host_probe_keys": list(CAPABILITY_KEYS),
        "host_advertised": advertised,
        "host_gates": host_gates,
        "team_consume": "available",
        "gate_state": "LEGACY",
        "reason": (
            "grok.prompt_queue.* are Team-consumed; host probe CAPABILITY_KEYS "
            "does not advertise them. Queued prompts are not mailbox or task ACKs."
        ),
        "not_mailbox": True,
        "not_task_ack": True,
    }


def _empty_queue(run_id: str, team_id: str) -> dict[str, Any]:
    return {
        "kind": QUEUE_KIND,
        "schema_version": QUEUE_SCHEMA_VERSION,
        "writer": CLI_WRITER,
        "run_id": run_id,
        "team_id": team_id,
        "waiting": False,
        "next_sequence": 0,
        "entries": [],
        "consume": prompt_queue_consume_receipt(),
    }


def _validate_queue(
    state: Mapping[str, Any], *, run_id: str, team_id: str
) -> dict[str, Any]:
    if not isinstance(state, Mapping):
        raise PromptQueueError("host prompt queue must be an object")
    if state.get("kind") != QUEUE_KIND:
        raise PromptQueueError("host prompt queue kind mismatch")
    if state.get("schema_version") != QUEUE_SCHEMA_VERSION:
        raise PromptQueueError("host prompt queue schema_version mismatch")
    if state.get("writer") != CLI_WRITER:
        raise PromptQueueError("host prompt queue writer is not omg-cli")
    if state.get("run_id") != run_id or state.get("team_id") != team_id:
        raise PromptQueueError("host prompt queue run/team identity mismatch")
    entries = state.get("entries")
    if not isinstance(entries, list):
        raise PromptQueueError("host prompt queue entries must be a list")
    next_seq = require_integer(state.get("next_sequence"), label="next_sequence", minimum=0)
    seen: set[str] = set()
    for index, item in enumerate(entries):
        if not isinstance(item, Mapping):
            raise PromptQueueError("host prompt queue entry must be an object")
        pid = require_safe_id(item.get("prompt_id"), label="prompt_id")
        if pid in seen:
            raise PromptQueueError("host prompt queue has duplicate prompt_id")
        seen.add(pid)
        seq = require_integer(item.get("sequence"), label="sequence", minimum=0)
        if seq != index:
            raise PromptQueueError("host prompt queue sequence does not match stored order")
        if seq >= next_seq:
            raise PromptQueueError("host prompt queue sequence is at or past next_sequence")
        kind = require_safe_id(item.get("kind"), label="kind")
        if kind in FORBIDDEN_KINDS:
            raise PromptQueueError(
                f"kind {kind!r} is a mailbox/task protocol token, not a host prompt",
                code="E_TEAM_PROMPT_QUEUE_KIND",
            )
        if "body" not in item:
            raise PromptQueueError("host prompt queue entry is missing body")
        require_nonempty_string(item.get("enqueued_at"), label="enqueued_at")
        preview = item.get("body_preview")
        if not isinstance(preview, str):
            raise PromptQueueError("host prompt queue entry body_preview must be a string")
        digest = require_sha256(item.get("content_hash"), label="content_hash")
        expected = sha256_hex(canonical_json_bytes(item.get("body")))
        if digest != expected:
            raise PromptQueueError("host prompt queue entry content_hash mismatch")
    if next_seq < len(entries):
        raise PromptQueueError("host prompt queue next_sequence is behind entries")
    waiting = state.get("waiting")
    if not isinstance(waiting, bool):
        raise PromptQueueError("host prompt queue waiting must be a boolean")
    return dict(state)


def _load_locked(path: Path, *, run_id: str, team_id: str) -> dict[str, Any]:
    if not path.is_file():
        return _empty_queue(run_id, team_id)
    try:
        raw = parse_canonical_json_bytes(path.read_bytes())
    except (OSError, ContractValidationError, ValueError) as exc:
        raise PromptQueueError(f"host prompt queue is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise PromptQueueError("host prompt queue document must be an object")
    try:
        return _validate_queue(raw, run_id=run_id, team_id=team_id)
    except ContractValidationError as exc:
        raise PromptQueueError(f"host prompt queue is invalid: {exc}") from exc


def _preview(body: Any) -> str:
    if isinstance(body, str):
        text = body
    else:
        text = str(body)
    text = text.replace("\n", " ").strip()
    if len(text) <= MAX_PREVIEW_CHARS:
        return text
    return text[: MAX_PREVIEW_CHARS - 1] + "…"


def enqueue_host_prompt(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    body: Any,
    kind: str | None = None,
    prompt_id: str | None = None,
    enqueued_at: str | None = None,
) -> dict[str, Any]:
    """Append one host prompt. Never writes mailbox or task files."""
    require_safe_id(run_id, label="run_id")
    require_safe_id(team_id, label="team_id")
    op_kind = (kind or DEFAULT_KIND).strip() or DEFAULT_KIND
    require_safe_id(op_kind, label="kind")
    if op_kind in FORBIDDEN_KINDS:
        raise PromptQueueError(
            f"kind {op_kind!r} is a mailbox/task protocol token, not a host prompt",
            code="E_TEAM_PROMPT_QUEUE_KIND",
        )
    if body is None or (isinstance(body, str) and not body.strip()):
        raise PromptQueueError("host prompt body is required")
    redacted = redact_value(body)
    if len(canonical_json_bytes(redacted)) > MAX_PROMPT_BYTES:
        raise PromptQueueError("host prompt exceeds bounded byte limit")
    content_hash = sha256_hex(canonical_json_bytes(redacted))
    path = queue_path(root, run_id, team_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        state = _load_locked(path, run_id=run_id, team_id=team_id)
        if len(state["entries"]) >= MAX_QUEUE_ENTRIES:
            raise PromptQueueError("host prompt queue reached hard cap")
        sequence = int(state["next_sequence"])
        pid = prompt_id or f"hp-{sequence:08d}"
        require_safe_id(pid, label="prompt_id")
        if any(item["prompt_id"] == pid for item in state["entries"]):
            raise PromptQueueError("prompt_id already exists on this queue")
        entry = {
            "prompt_id": pid,
            "sequence": sequence,
            "kind": op_kind,
            "body": redacted,
            "body_preview": _preview(redacted),
            "content_hash": content_hash,
            "enqueued_at": enqueued_at or _utc_now(),
        }
        updated = {
            **state,
            "next_sequence": sequence + 1,
            "entries": [*state["entries"], entry],
            "consume": prompt_queue_consume_receipt(),
        }
        _validate_queue(updated, run_id=run_id, team_id=team_id)
        atomic_write_bytes(
            path, canonical_json_bytes(updated), mode=DATA_FILE_MODE, replace=True
        )
    return {**entry, "waiting": bool(updated["waiting"]), "index": sequence}


def list_host_prompt_queue(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
) -> dict[str, Any]:
    """Return ordered entries. Visibility does not depend on ``waiting``."""
    path = queue_path(root, run_id, team_id)
    if not path.is_file():
        empty = _empty_queue(run_id, team_id)
        return {
            "waiting": False,
            "count": 0,
            "prompt_ids": [],
            "entries": [],
            "consume": empty["consume"],
        }
    with exclusive_lock(path.with_suffix(".lock")):
        state = _load_locked(path, run_id=run_id, team_id=team_id)
    rows = [
        {
            "prompt_id": item["prompt_id"],
            "sequence": item["sequence"],
            "kind": item["kind"],
            "body_preview": item.get("body_preview"),
            "content_hash": item["content_hash"],
            "enqueued_at": item["enqueued_at"],
        }
        for item in state["entries"]
    ]
    return {
        "waiting": bool(state["waiting"]),
        "count": len(rows),
        "prompt_ids": [row["prompt_id"] for row in rows],
        "entries": rows,
        "consume": state.get("consume") or prompt_queue_consume_receipt(),
    }


def reorder_host_prompt_queue(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    order: list[str],
) -> dict[str, Any]:
    """Replace queue order with a permutation of existing prompt ids."""
    if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
        raise PromptQueueError("order must be a list of prompt_id strings")
    wanted = [require_safe_id(item.strip(), label="prompt_id") for item in order]
    if len(wanted) != len(set(wanted)):
        raise PromptQueueError("order contains duplicate prompt_id values")
    path = queue_path(root, run_id, team_id)
    if not path.is_file():
        raise PromptQueueError("host prompt queue does not exist")
    with exclusive_lock(path.with_suffix(".lock")):
        state = _load_locked(path, run_id=run_id, team_id=team_id)
        current = {item["prompt_id"]: item for item in state["entries"]}
        if set(wanted) != set(current):
            raise PromptQueueError(
                "order must be a permutation of current prompt_id values",
                code="E_TEAM_PROMPT_QUEUE_ORDER",
            )
        reordered = []
        for index, pid in enumerate(wanted):
            item = dict(current[pid])
            item["sequence"] = index
            reordered.append(item)
        updated = {
            **state,
            "entries": reordered,
            "consume": prompt_queue_consume_receipt(),
        }
        _validate_queue(updated, run_id=run_id, team_id=team_id)
        atomic_write_bytes(
            path, canonical_json_bytes(updated), mode=DATA_FILE_MODE, replace=True
        )
    return list_host_prompt_queue(root, run_id=run_id, team_id=team_id)


def mark_host_prompt_queue_waiting(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    waiting: bool,
) -> dict[str, Any]:
    """Set the waiting flag. Listing remains available (visible_while_waiting)."""
    if not isinstance(waiting, bool):
        raise PromptQueueError("waiting must be a boolean")
    path = queue_path(root, run_id, team_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        state = _load_locked(path, run_id=run_id, team_id=team_id)
        updated = {**state, "waiting": waiting, "consume": prompt_queue_consume_receipt()}
        _validate_queue(updated, run_id=run_id, team_id=team_id)
        atomic_write_bytes(
            path, canonical_json_bytes(updated), mode=DATA_FILE_MODE, replace=True
        )
    return list_host_prompt_queue(root, run_id=run_id, team_id=team_id)


__all__ = [
    "DEFAULT_KIND",
    "FORBIDDEN_KINDS",
    "PROMPT_QUEUE_CAP_IDS",
    "PromptQueueError",
    "enqueue_host_prompt",
    "list_host_prompt_queue",
    "mailbox_dir",
    "mark_host_prompt_queue_waiting",
    "prompt_queue_consume_receipt",
    "queue_path",
    "reorder_host_prompt_queue",
]
