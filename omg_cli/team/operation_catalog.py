"""Versioned Team API operation catalog (schema v1 + v2 + v3 + v4).

Single source of truth for operation names and metadata. Derived exports
(``TEAM_API_OPERATIONS``, ``P0_OPERATIONS``, worker ACL sets) must not be
hand-maintained elsewhere. Handler wiring remains in ``omg_cli.team.api``;
golden tests enforce ``implemented == _HANDLERS.keys()``.

This module is pure data + serialization: no team state, tmux, filesystem
mutation, or subprocess.

v1 remains frozen (golden ``team_operation_catalog_v1.json``).
v2 remains frozen (golden ``team_operation_catalog_v2.json`` — adds
``replace-worker``).
v3 remains frozen (golden ``team_operation_catalog_v3.json`` — adds
leader-only read-only ``read-presentation-state``).
Default dispatch / CLI catalog is **v4** (adds leader-only mutating
``bulk-create-tasks``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping

DispatchState = Literal["implemented", "reserved", "planned"]

CATALOG_KIND = "omg.team.operation_catalog"
CATALOG_SCHEMA_VERSION_V1 = 1
CATALOG_SCHEMA_VERSION_V2 = 2
CATALOG_SCHEMA_VERSION_V3 = 3
CATALOG_SCHEMA_VERSION_V4 = 4
# Default / active catalog schema (CLI ``omg team api catalog``).
CATALOG_SCHEMA_VERSION = 4

_OP_FIELDS = (
    "name",
    "domain",
    "dispatch_state",
    "implemented",
    "reserved",
    "planned",
    "mutates_state",
    "worker_allowed",
)


@dataclass(frozen=True, slots=True)
class TeamOperation:
    """One catalog entry. Booleans mirror ``dispatch_state`` (exactly one true)."""

    name: str
    domain: str
    dispatch_state: DispatchState
    implemented: bool
    reserved: bool
    planned: bool
    mutates_state: bool
    worker_allowed: bool

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        return {key: raw[key] for key in _OP_FIELDS}


def _op(
    name: str,
    *,
    domain: str,
    dispatch_state: DispatchState,
    mutates_state: bool,
    worker_allowed: bool = False,
) -> TeamOperation:
    implemented = dispatch_state == "implemented"
    reserved = dispatch_state == "reserved"
    planned = dispatch_state == "planned"
    if worker_allowed and not implemented:
        raise ValueError(f"{name}: worker_allowed requires implemented")
    return TeamOperation(
        name=name,
        domain=domain,
        dispatch_state=dispatch_state,
        implemented=implemented,
        reserved=reserved,
        planned=planned,
        mutates_state=mutates_state,
        worker_allowed=worker_allowed,
    )


# Immutable catalog v1 — names match the OMX-shaped surface; P0′ is implemented.
TEAM_OPERATION_CATALOG_V1: tuple[TeamOperation, ...] = (
    # mailbox
    _op(
        "send-message",
        domain="mailbox",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=True,
    ),
    _op(
        "broadcast",
        domain="mailbox",
        dispatch_state="reserved",
        mutates_state=True,
    ),
    _op(
        "mailbox-list",
        domain="mailbox",
        dispatch_state="implemented",
        mutates_state=False,
        worker_allowed=True,
    ),
    _op(
        "mailbox-mark-delivered",
        domain="mailbox",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=True,
    ),
    _op(
        "mailbox-mark-notified",
        domain="mailbox",
        dispatch_state="reserved",
        mutates_state=True,
    ),
    # task
    _op(
        "create-task",
        domain="task",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=False,
    ),
    _op(
        "read-task",
        domain="task",
        dispatch_state="implemented",
        mutates_state=False,
        worker_allowed=True,
    ),
    _op(
        "list-tasks",
        domain="task",
        dispatch_state="implemented",
        mutates_state=False,
        worker_allowed=True,
    ),
    _op(
        "update-task",
        domain="task",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=False,
    ),
    _op(
        "claim-task",
        domain="task",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=True,
    ),
    _op(
        "transition-task-status",
        domain="task",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=True,
    ),
    _op(
        "release-task-claim",
        domain="task",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=True,
    ),
    _op(
        "renew-task-claim",
        domain="task",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=True,
    ),
    # config / manifest
    _op(
        "read-config",
        domain="config",
        dispatch_state="implemented",
        mutates_state=False,
        worker_allowed=True,
    ),
    _op(
        "read-manifest",
        domain="config",
        dispatch_state="implemented",
        mutates_state=False,
        worker_allowed=True,
    ),
    # worker
    _op(
        "read-worker-status",
        domain="worker",
        dispatch_state="implemented",
        mutates_state=False,
        worker_allowed=True,
    ),
    _op(
        "read-worker-heartbeat",
        domain="worker",
        dispatch_state="implemented",
        mutates_state=False,
        worker_allowed=True,
    ),
    _op(
        "update-worker-heartbeat",
        domain="worker",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=True,
    ),
    _op(
        "write-worker-inbox",
        domain="worker",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=False,
    ),
    _op(
        "write-worker-identity",
        domain="worker",
        dispatch_state="reserved",
        mutates_state=True,
    ),
    # event
    _op(
        "append-event",
        domain="event",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=True,
    ),
    _op(
        "read-events",
        domain="event",
        dispatch_state="implemented",
        mutates_state=False,
        worker_allowed=True,
    ),
    _op(
        "await-event",
        domain="event",
        dispatch_state="reserved",
        mutates_state=False,
    ),
    # summary / idle
    _op(
        "read-idle-state",
        domain="summary",
        dispatch_state="reserved",
        mutates_state=False,
    ),
    _op(
        "read-stall-state",
        domain="summary",
        dispatch_state="reserved",
        mutates_state=False,
    ),
    _op(
        "get-summary",
        domain="summary",
        dispatch_state="implemented",
        mutates_state=False,
        worker_allowed=True,
    ),
    # lifecycle
    _op(
        "cleanup",
        domain="lifecycle",
        dispatch_state="reserved",
        mutates_state=True,
    ),
    _op(
        "orphan-cleanup",
        domain="lifecycle",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=False,
    ),
    _op(
        "write-shutdown-request",
        domain="lifecycle",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=False,
    ),
    _op(
        "read-shutdown-request",
        domain="lifecycle",
        dispatch_state="implemented",
        mutates_state=False,
        worker_allowed=True,
    ),
    _op(
        "write-shutdown-ack",
        domain="lifecycle",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=True,
    ),
    _op(
        "read-shutdown-ack",
        domain="lifecycle",
        dispatch_state="implemented",
        mutates_state=False,
        worker_allowed=False,
    ),
    # monitor
    _op(
        "read-monitor-snapshot",
        domain="monitor",
        dispatch_state="reserved",
        mutates_state=False,
    ),
    _op(
        "write-monitor-snapshot",
        domain="monitor",
        dispatch_state="reserved",
        mutates_state=True,
    ),
    # task approval (reserved)
    _op(
        "read-task-approval",
        domain="task",
        dispatch_state="reserved",
        mutates_state=False,
    ),
    _op(
        "write-task-approval",
        domain="task",
        dispatch_state="reserved",
        mutates_state=True,
    ),
)


# Catalog v2 = v1 + leader-only replace-worker (#69 PR5).
TEAM_OPERATION_CATALOG_V2: tuple[TeamOperation, ...] = TEAM_OPERATION_CATALOG_V1 + (
    _op(
        "replace-worker",
        domain="worker",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=False,
    ),
)

# Catalog v3 = v2 + leader-only read-presentation-state (#69 PR6).
TEAM_OPERATION_CATALOG_V3: tuple[TeamOperation, ...] = TEAM_OPERATION_CATALOG_V2 + (
    _op(
        "read-presentation-state",
        domain="summary",
        dispatch_state="implemented",
        mutates_state=False,
        worker_allowed=False,
    ),
)

# Catalog v4 = v3 + leader-only bulk-create-tasks (#69 PR11).
TEAM_OPERATION_CATALOG_V4: tuple[TeamOperation, ...] = TEAM_OPERATION_CATALOG_V3 + (
    _op(
        "bulk-create-tasks",
        domain="task",
        dispatch_state="implemented",
        mutates_state=True,
        worker_allowed=False,
    ),
)

# Active catalog alias (default dispatch).
TEAM_OPERATION_CATALOG = TEAM_OPERATION_CATALOG_V4


def _validate_catalog(ops: tuple[TeamOperation, ...]) -> None:
    names = [op.name for op in ops]
    if len(names) != len(set(names)):
        raise ValueError("team operation catalog has duplicate names")
    for op in ops:
        flags = (op.implemented, op.reserved, op.planned)
        if sum(1 for flag in flags if flag) != 1:
            raise ValueError(f"{op.name}: exactly one of implemented/reserved/planned")
        expected: DispatchState
        if op.implemented:
            expected = "implemented"
        elif op.reserved:
            expected = "reserved"
        else:
            expected = "planned"
        if op.dispatch_state != expected:
            raise ValueError(f"{op.name}: dispatch_state mismatch")
        if op.worker_allowed and not op.implemented:
            raise ValueError(f"{op.name}: worker_allowed requires implemented")


_validate_catalog(TEAM_OPERATION_CATALOG_V1)
_validate_catalog(TEAM_OPERATION_CATALOG_V2)
_validate_catalog(TEAM_OPERATION_CATALOG_V3)
_validate_catalog(TEAM_OPERATION_CATALOG_V4)

# Derived exports — do not hand-edit; change TEAM_OPERATION_CATALOG_V4 instead.
TEAM_API_OPERATIONS: tuple[str, ...] = tuple(
    op.name for op in TEAM_OPERATION_CATALOG_V4
)
P0_OPERATIONS: tuple[str, ...] = tuple(
    op.name for op in TEAM_OPERATION_CATALOG_V4 if op.implemented
)
WORKER_ALLOWED_OPS: frozenset[str] = frozenset(
    op.name for op in TEAM_OPERATION_CATALOG_V4 if op.implemented and op.worker_allowed
)
WORKER_DENIED_OPS: frozenset[str] = frozenset(
    op.name
    for op in TEAM_OPERATION_CATALOG_V4
    if op.implemented and not op.worker_allowed
)


def serialize_operation_catalog(
    *,
    operations: tuple[TeamOperation, ...] | None = None,
    schema_version: int | None = None,
) -> dict[str, Any]:
    """Machine-readable catalog document (kind + schema_version + operations)."""
    if operations is None:
        ops = TEAM_OPERATION_CATALOG_V4
        version = (
            CATALOG_SCHEMA_VERSION_V4 if schema_version is None else schema_version
        )
    else:
        ops = operations
        if schema_version is not None:
            version = schema_version
        elif ops is TEAM_OPERATION_CATALOG_V1:
            version = CATALOG_SCHEMA_VERSION_V1
        elif ops is TEAM_OPERATION_CATALOG_V2:
            version = CATALOG_SCHEMA_VERSION_V2
        elif ops is TEAM_OPERATION_CATALOG_V3:
            version = CATALOG_SCHEMA_VERSION_V3
        else:
            version = CATALOG_SCHEMA_VERSION_V4
    return {
        "kind": CATALOG_KIND,
        "schema_version": version,
        "operations": [op.to_dict() for op in ops],
    }


def catalog_document_json(
    *,
    operations: tuple[TeamOperation, ...] | None = None,
    schema_version: int | None = None,
) -> str:
    """Deterministic JSON text for CLI / golden freeze (sorted keys, 2-space)."""
    import json

    return (
        json.dumps(
            serialize_operation_catalog(
                operations=operations, schema_version=schema_version
            ),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )


def operation_by_name(
    name: str,
    *,
    operations: tuple[TeamOperation, ...] | None = None,
) -> TeamOperation | None:
    ops = TEAM_OPERATION_CATALOG_V4 if operations is None else operations
    for op in ops:
        if op.name == name:
            return op
    return None


def catalog_from_mapping(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a loaded golden/doc mapping (identity for schema checks)."""
    return {
        "kind": doc["kind"],
        "schema_version": doc["schema_version"],
        "operations": list(doc["operations"]),
    }


__all__ = [
    "CATALOG_KIND",
    "CATALOG_SCHEMA_VERSION",
    "CATALOG_SCHEMA_VERSION_V1",
    "CATALOG_SCHEMA_VERSION_V2",
    "CATALOG_SCHEMA_VERSION_V3",
    "CATALOG_SCHEMA_VERSION_V4",
    "P0_OPERATIONS",
    "TEAM_API_OPERATIONS",
    "TEAM_OPERATION_CATALOG",
    "TEAM_OPERATION_CATALOG_V1",
    "TEAM_OPERATION_CATALOG_V2",
    "TEAM_OPERATION_CATALOG_V3",
    "TEAM_OPERATION_CATALOG_V4",
    "TeamOperation",
    "WORKER_ALLOWED_OPS",
    "WORKER_DENIED_OPS",
    "catalog_document_json",
    "catalog_from_mapping",
    "operation_by_name",
    "serialize_operation_catalog",
]
