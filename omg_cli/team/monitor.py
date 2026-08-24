"""Schema-versioned, redacted Team monitor snapshot (no secrets)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
    safe_path_key,
)
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_iso8601,
    require_safe_id,
)
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from omg_cli.redaction import redact_value


CLI_WRITER = "omg-cli"
MONITOR_KIND = "omg.team.monitor_snapshot"
MONITOR_SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 65_536

_MONITOR_KEYS = frozenset(
    {
        "kind",
        "schema_version",
        "writer",
        "run_id",
        "team_id",
        "captured_at",
        "redacted",
        "tmux_probed",
        "snapshot",
    }
)


class MonitorError(RuntimeError):
    """Monitor snapshot confinement or schema failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def monitor_snapshot_path(root: Path | str, run_id: str, team_id: str) -> Path:
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
        / "monitor_snapshot.json"
    )


def _validate_monitor(
    value: Mapping[str, Any], *, run_id: str, team_id: str
) -> dict[str, Any]:
    row = dict(value)
    require_exact_keys(row, required=_MONITOR_KEYS, label="team monitor snapshot")
    if (
        row["kind"] != MONITOR_KIND
        or row["schema_version"] != MONITOR_SCHEMA_VERSION
        or row["writer"] != CLI_WRITER
    ):
        raise ContractValidationError("team monitor snapshot header mismatch")
    if row["run_id"] != run_id or row["team_id"] != team_id:
        raise ContractValidationError("team monitor snapshot identity mismatch")
    require_iso8601(row["captured_at"], label="captured_at")
    if row["redacted"] is not True:
        raise ContractValidationError("team monitor snapshot must be redacted")
    if row["tmux_probed"] is not False:
        raise ContractValidationError("team monitor snapshot must not probe tmux")
    if not isinstance(row["snapshot"], (dict, list)):
        raise ContractValidationError("team monitor snapshot body must be an object or list")
    return row


def read_monitor_snapshot(
    root: Path | str, *, run_id: str, team_id: str
) -> dict[str, Any] | None:
    path = monitor_snapshot_path(root, run_id, team_id)
    if not path.is_file():
        return None
    parsed = parse_canonical_json_bytes(path.read_bytes())
    if not isinstance(parsed, dict):
        raise ContractValidationError("team monitor snapshot must be an object")
    return _validate_monitor(parsed, run_id=run_id, team_id=team_id)


def write_monitor_snapshot(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    snapshot: Any,
    captured_at: str | None = None,
) -> dict[str, Any]:
    """Persist a redacted snapshot. Never stores live tmux or secrets."""

    if snapshot is None:
        snapshot = {}
    redacted = redact_value(snapshot)
    blob = canonical_json_bytes(redacted)
    if len(blob) > MAX_SNAPSHOT_BYTES:
        raise MonitorError("monitor snapshot exceeds bounded byte limit")
    path = monitor_snapshot_path(root, run_id, team_id)
    ensure_managed_dir(path.parent)
    record = {
        "kind": MONITOR_KIND,
        "schema_version": MONITOR_SCHEMA_VERSION,
        "writer": CLI_WRITER,
        "run_id": run_id,
        "team_id": team_id,
        "captured_at": captured_at or _utc_now(),
        "redacted": True,
        "tmux_probed": False,
        "snapshot": redacted,
    }
    _validate_monitor(record, run_id=run_id, team_id=team_id)
    with exclusive_lock(path.with_suffix(".lock")):
        atomic_write_bytes(
            path, canonical_json_bytes(record), mode=DATA_FILE_MODE, replace=True
        )
    return {**record, "path": str(path)}


__all__ = [
    "MONITOR_KIND",
    "MONITOR_SCHEMA_VERSION",
    "MonitorError",
    "monitor_snapshot_path",
    "read_monitor_snapshot",
    "write_monitor_snapshot",
]
