"""Golden contract for the versioned Team operation catalog (schema v1+v2+v3)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omg_cli.main import main
from omg_cli.team import api as team_api
from omg_cli.team.operation_catalog import (
    CATALOG_KIND,
    CATALOG_SCHEMA_VERSION,
    P0_OPERATIONS,
    TEAM_API_OPERATIONS,
    TEAM_OPERATION_CATALOG_V1,
    TEAM_OPERATION_CATALOG_V2,
    TEAM_OPERATION_CATALOG_V3,
    WORKER_ALLOWED_OPS,
    WORKER_DENIED_OPS,
    catalog_document_json,
    serialize_operation_catalog,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_V1 = ROOT / "tests" / "golden" / "team_operation_catalog_v1.json"
GOLDEN_V2 = ROOT / "tests" / "golden" / "team_operation_catalog_v2.json"
GOLDEN_V3 = ROOT / "tests" / "golden" / "team_operation_catalog_v3.json"
GOLDEN = GOLDEN_V3

# Legacy constant snapshots (pre-catalog module) for export parity.
# Worker ACL partition now includes leader-only ``read-shutdown-ack`` in denied.
_LEGACY_TEAM_API_OPERATIONS = (
    "send-message",
    "broadcast",
    "mailbox-list",
    "mailbox-mark-delivered",
    "mailbox-mark-notified",
    "create-task",
    "read-task",
    "list-tasks",
    "update-task",
    "claim-task",
    "transition-task-status",
    "release-task-claim",
    "renew-task-claim",
    "read-config",
    "read-manifest",
    "read-worker-status",
    "read-worker-heartbeat",
    "update-worker-heartbeat",
    "write-worker-inbox",
    "write-worker-identity",
    "append-event",
    "read-events",
    "await-event",
    "read-idle-state",
    "read-stall-state",
    "get-summary",
    "cleanup",
    "orphan-cleanup",
    "write-shutdown-request",
    "read-shutdown-request",
    "write-shutdown-ack",
    "read-shutdown-ack",
    "read-monitor-snapshot",
    "write-monitor-snapshot",
    "read-task-approval",
    "write-task-approval",
)
_LEGACY_P0_OPERATIONS = frozenset(
    {
        "send-message",
        "mailbox-list",
        "mailbox-mark-delivered",
        "create-task",
        "read-task",
        "list-tasks",
        "update-task",
        "claim-task",
        "transition-task-status",
        "release-task-claim",
        "renew-task-claim",
        "get-summary",
        "read-config",
        "read-manifest",
        "write-worker-inbox",
        "update-worker-heartbeat",
        "read-worker-heartbeat",
        "read-worker-status",
        "write-shutdown-request",
        "read-shutdown-request",
        "write-shutdown-ack",
        "read-shutdown-ack",
        "orphan-cleanup",
        "append-event",
        "read-events",
    }
)
_LEGACY_WORKER_ALLOWED = frozenset(
    {
        "send-message",
        "mailbox-list",
        "mailbox-mark-delivered",
        "read-task",
        "list-tasks",
        "claim-task",
        "transition-task-status",
        "release-task-claim",
        "renew-task-claim",
        "get-summary",
        "read-config",
        "read-manifest",
        "update-worker-heartbeat",
        "read-worker-heartbeat",
        "read-worker-status",
        "read-shutdown-request",
        "write-shutdown-ack",
        "append-event",
        "read-events",
    }
)


def test_operation_catalog_v1_golden_unchanged() -> None:
    expected = json.loads(GOLDEN_V1.read_text(encoding="utf-8"))
    actual = serialize_operation_catalog(
        operations=TEAM_OPERATION_CATALOG_V1, schema_version=1
    )
    assert actual == expected
    assert (
        catalog_document_json(operations=TEAM_OPERATION_CATALOG_V1, schema_version=1)
        == GOLDEN_V1.read_text(encoding="utf-8")
    )


def test_operation_catalog_v2_golden_unchanged() -> None:
    expected = json.loads(GOLDEN_V2.read_text(encoding="utf-8"))
    actual = serialize_operation_catalog(
        operations=TEAM_OPERATION_CATALOG_V2, schema_version=2
    )
    assert actual == expected
    assert (
        catalog_document_json(operations=TEAM_OPERATION_CATALOG_V2, schema_version=2)
        == GOLDEN_V2.read_text(encoding="utf-8")
    )


def test_operation_catalog_matches_golden() -> None:
    expected = json.loads(GOLDEN_V3.read_text(encoding="utf-8"))
    actual = serialize_operation_catalog()
    assert actual == expected
    assert catalog_document_json() == GOLDEN_V3.read_text(encoding="utf-8")
    assert actual["schema_version"] == 3
    assert any(op["name"] == "replace-worker" for op in actual["operations"])
    assert any(op["name"] == "read-presentation-state" for op in actual["operations"])


def test_operation_catalog_has_unique_names() -> None:
    names = [op.name for op in TEAM_OPERATION_CATALOG_V3]
    assert len(names) == len(set(names))
    assert names == list(TEAM_API_OPERATIONS)
    assert len(TEAM_OPERATION_CATALOG_V1) == 36
    assert len(TEAM_OPERATION_CATALOG_V2) == 37
    assert len(TEAM_OPERATION_CATALOG_V3) == 38


def test_operation_catalog_handler_coverage() -> None:
    implemented = {op.name for op in TEAM_OPERATION_CATALOG_V3 if op.implemented}
    assert implemented == set(team_api._HANDLERS)
    assert implemented == set(P0_OPERATIONS)
    assert "replace-worker" in implemented
    assert "read-presentation-state" in implemented


def test_operation_catalog_worker_acl_partition() -> None:
    implemented = set(P0_OPERATIONS)
    assert WORKER_ALLOWED_OPS | WORKER_DENIED_OPS == implemented
    assert WORKER_ALLOWED_OPS & WORKER_DENIED_OPS == frozenset()
    for op in TEAM_OPERATION_CATALOG_V3:
        if not op.implemented:
            assert op.name not in WORKER_ALLOWED_OPS
            assert op.name not in WORKER_DENIED_OPS
            assert op.worker_allowed is False
        elif op.worker_allowed:
            assert op.name in WORKER_ALLOWED_OPS
        else:
            assert op.name in WORKER_DENIED_OPS
    assert "replace-worker" in WORKER_DENIED_OPS
    assert "read-presentation-state" in WORKER_DENIED_OPS


def test_operation_catalog_exports_match_legacy_constants() -> None:
    # v3 = legacy v1 names + replace-worker + read-presentation-state
    assert TEAM_API_OPERATIONS == _LEGACY_TEAM_API_OPERATIONS + (
        "replace-worker",
        "read-presentation-state",
    )
    assert set(P0_OPERATIONS) == _LEGACY_P0_OPERATIONS | {
        "replace-worker",
        "read-presentation-state",
    }
    assert team_api.TEAM_API_OPERATIONS is TEAM_API_OPERATIONS
    assert team_api.P0_OPERATIONS is P0_OPERATIONS
    assert team_api.WORKER_ALLOWED_OPS == WORKER_ALLOWED_OPS
    assert team_api.WORKER_DENIED_OPS == WORKER_DENIED_OPS
    assert WORKER_ALLOWED_OPS == _LEGACY_WORKER_ALLOWED
    # Leader-only implemented ops must sit in denied (closes prior ACL hole).
    assert "read-shutdown-ack" in WORKER_DENIED_OPS
    assert "replace-worker" in WORKER_DENIED_OPS
    assert "read-presentation-state" in WORKER_DENIED_OPS
    assert WORKER_ALLOWED_OPS | WORKER_DENIED_OPS == _LEGACY_P0_OPERATIONS | {
        "replace-worker",
        "read-presentation-state",
    }


def test_team_api_catalog_cli_is_state_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OMG_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    # Refuse accidental state / tmux / subprocess from catalog path.
    monkeypatch.setattr(
        "omg_cli.commands.team.project_root",
        lambda: (_ for _ in ()).throw(AssertionError("project_root must not run")),
    )

    def _no_run(*_a, **_k):  # noqa: ANN001
        raise AssertionError("subprocess must not run for catalog")

    monkeypatch.setattr(subprocess, "run", _no_run)
    monkeypatch.setattr(subprocess, "Popen", _no_run)
    assert not (tmp_path / ".omg").exists()
    rc = main(["team", "api", "catalog"])
    assert rc == 0
    assert not (tmp_path / ".omg").exists()
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["kind"] == CATALOG_KIND
    assert doc["schema_version"] == CATALOG_SCHEMA_VERSION
    assert doc["schema_version"] == 3
    assert doc == serialize_operation_catalog()


def test_team_api_catalog_requires_no_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    rc = main(["team", "api", "catalog"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["kind"] == CATALOG_KIND
    # Non-catalog ops still require --input.
    rc2 = main(["team", "api", "send-message"])
    assert rc2 == 2
    err = capsys.readouterr().err
    assert "--input JSON required" in err


def test_team_api_catalog_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["team", "api", "catalog"]) == 0
    first = capsys.readouterr().out
    assert main(["team", "api", "catalog"]) == 0
    second = capsys.readouterr().out
    assert first == second
    assert first == catalog_document_json()
    # Serializer path matches CLI bytes.
    assert first == GOLDEN_V3.read_text(encoding="utf-8")


def test_catalog_document_schema_shape() -> None:
    doc = serialize_operation_catalog()
    assert set(doc) == {"kind", "schema_version", "operations"}
    assert doc["kind"] == "omg.team.operation_catalog"
    assert doc["schema_version"] == 3
    required = {
        "name",
        "domain",
        "dispatch_state",
        "implemented",
        "reserved",
        "planned",
        "mutates_state",
        "worker_allowed",
    }
    for row in doc["operations"]:
        assert set(row) == required
        assert (
            sum(1 for k in ("implemented", "reserved", "planned") if row[k]) == 1
        )
