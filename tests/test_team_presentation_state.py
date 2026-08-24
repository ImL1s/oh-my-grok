"""Hermetic tests for Team Presentation State V1 (#69 PR6)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omg_cli.jobs.runtime import wait_job
from omg_cli.main import main
from omg_cli.mcp.tools import dispatch_tool
from omg_cli.team import api as team_api
from omg_cli.team.operation_catalog import (
    CATALOG_SCHEMA_VERSION,
    TEAM_OPERATION_CATALOG_V1,
    TEAM_OPERATION_CATALOG_V2,
    TEAM_OPERATION_CATALOG_V3,
    TEAM_OPERATION_CATALOG_V4,
    serialize_operation_catalog,
)
from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    WORKER_ENV_MARKERS,
    load_team_meta,
    mutate_team_meta,
    start_team,
    status_locked_view,
    team_status,
)
from omg_cli.team.presentation import (
    MCP_PROJECTION_V1,
    PRESENTATION_KIND,
    PresentationError,
    build_team_presentation_v1,
    stamp_route_on_task,
    unknown_route,
)
from omg_cli.team.replacement import replace_worker

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_V1 = ROOT / "tests" / "golden" / "team_operation_catalog_v1.json"
GOLDEN_V2 = ROOT / "tests" / "golden" / "team_operation_catalog_v2.json"
GOLDEN_V3 = ROOT / "tests" / "golden" / "team_operation_catalog_v3.json"
GOLDEN_PRESENTATION = ROOT / "tests" / "golden" / "team_presentation_state_v1_dry_run.json"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "omg-test@example.com")
    _git(path, "config", "user.name", "omg-test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def _env_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    for key in (
        *WORKER_ENV_MARKERS,
        "OMG_TEAM_WORKER_ID",
        "OMG_TEAM_RUN_ID",
        "OMG_TEAM_ID",
        "OMG_TEAM_LEADER_ROOT",
        "OMG_TEAM_STATE_ROOT",
        "OMG_TEAM_OWNER_TOKEN",
        "OMG_PROJECT_ROOT",
    ):
        monkeypatch.delenv(key, raising=False)


def _tree_digest(root: Path) -> str:
    rows: list[str] = []
    base = root / ".omg"
    if not base.exists():
        return hashlib.sha256(b"").hexdigest()
    for path in sorted(base.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rel = path.relative_to(root).as_posix()
            body = path.read_bytes()
            rows.append(f"{rel}:{hashlib.sha256(body).hexdigest()}")
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def _start_dry(
    tmp_path: Path, *, tasks: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return start_team(
        "presentation dry",
        tasks
        or [{"task_id": "t1", "owned_files": ["a.py"], "role": "executor"}],
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        team_id="team",
    )


def _start_job(tmp_path: Path) -> dict[str, Any]:
    return start_team(
        "presentation jobs",
        [{"task_id": "t1", "owned_files": ["a.py"], "provider": "fake"}],
        root=tmp_path,
        dry_run=False,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
        team_id="team",
    )


def test_catalog_v1_v2_goldens_frozen() -> None:
    assert serialize_operation_catalog(
        operations=TEAM_OPERATION_CATALOG_V1, schema_version=1
    ) == json.loads(GOLDEN_V1.read_text(encoding="utf-8"))
    assert serialize_operation_catalog(
        operations=TEAM_OPERATION_CATALOG_V2, schema_version=2
    ) == json.loads(GOLDEN_V2.read_text(encoding="utf-8"))
    assert serialize_operation_catalog(
        operations=TEAM_OPERATION_CATALOG_V3, schema_version=3
    ) == json.loads(GOLDEN_V3.read_text(encoding="utf-8"))
    assert CATALOG_SCHEMA_VERSION == 6
    assert len(TEAM_OPERATION_CATALOG_V3) == 38
    assert any(op.name == "read-presentation-state" for op in TEAM_OPERATION_CATALOG_V3)
    assert any(op.name == "bulk-create-tasks" for op in TEAM_OPERATION_CATALOG_V4)


def test_presentation_dry_run_stamps_route_and_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_dry(tmp_path)
    run_id = str(meta["run_id"])
    task = load_team_meta(tmp_path, run_id)["tasks"][0]
    assert task["route"]["kind"] == "external_executor"
    assert task["route"]["schema"] == 1
    before = _tree_digest(tmp_path)
    state = build_team_presentation_v1(tmp_path, run_id)
    after = _tree_digest(tmp_path)
    assert before == after
    assert state["kind"] == PRESENTATION_KIND
    assert state["schema_version"] == 1
    assert state["run_id"] == run_id
    assert len(state["members"]) == 1
    member = state["members"][0]
    assert member["route"]["kind"] == "external_executor"
    assert member["capability_floor"] == "read-write"
    assert member["worktree"]["relative_path"].startswith(".omg/worktrees/")
    assert not member["worktree"]["relative_path"].startswith("/")
    assert member["current_attempt"]["start"] == "committed"
    assert member["current_attempt"]["execution"]["topology"] == "pane"
    # #147: presentation projects fail-closed I/O (schema v1 additive).
    assert member["io"]["io_mode"] == "headless_stream"
    assert member["io"]["provider_tty_owner"] == "supervisor"
    assert member["io"]["input_ready"] is False
    assert member["io"]["operator_input_supported"] is False
    assert member["io"]["interaction_evidence"] is None
    blob = json.dumps(state)
    assert "owner_token" not in blob
    assert "claim_token" not in blob
    assert "/Users/" not in blob
    assert "/private/" not in blob


def test_presentation_surfaces_equal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_dry(tmp_path)
    run_id = str(meta["run_id"])
    team_id = str(meta["team_id"])
    expected = build_team_presentation_v1(tmp_path, run_id, team_id=team_id)

    code, envelope = team_api.execute_team_api(
        "read-presentation-state",
        {"run_id": run_id, "team_id": team_id},
        root=tmp_path,
        env={EXPERIMENTAL_ENV: "1"},
    )
    assert code == 0 and envelope.get("ok") is True
    assert envelope["data"] == expected

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path))
    rc = main(["team", "status", "--run", run_id, "--presentation", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    cli_payload = json.loads(out)
    # emit_data may wrap; accept bare or envelope.
    if "kind" in cli_payload and cli_payload["kind"] == PRESENTATION_KIND:
        assert cli_payload == expected
    else:
        assert cli_payload.get("data") == expected or cli_payload.get("team") == expected

    mcp = dispatch_tool(
        "team_status.read",
        {"run_id": run_id, "team_id": team_id, "projection": MCP_PROJECTION_V1},
        root=tmp_path,
    )
    assert mcp["ok"] is True and mcp["found"] is True
    assert mcp["team"] == expected
    assert mcp["projection"] == MCP_PROJECTION_V1


def test_presentation_default_status_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_dry(tmp_path)
    run_id = str(meta["run_id"])
    locked = status_locked_view(team_status(tmp_path, run_id, probe_tmux=False))
    assert set(locked.keys()) == {
        "run_id",
        "session",
        "dry_run",
        "workspace_mode",
        "tasks",
    }
    # #147: frozen locked task keys must not gain I/O fields.
    from omg_cli.team.plane import STATUS_TASK_KEYS, STATUS_TOP_KEYS

    assert STATUS_TOP_KEYS == (
        "run_id",
        "session",
        "dry_run",
        "workspace_mode",
        "tasks",
    )
    assert STATUS_TASK_KEYS == (
        "task_id",
        "window_index",
        "worktree",
        "status",
        "alive",
    )
    for t in locked["tasks"]:
        assert set(t.keys()) == set(STATUS_TASK_KEYS)
        assert "io_mode" not in t
        assert "operator_input_supported" not in t
    mcp = dispatch_tool("team_status.read", {"run_id": run_id}, root=tmp_path)
    assert mcp["ok"] is True
    assert "projection" not in mcp
    assert set(mcp["team"].keys()) <= set(locked.keys()) | {"tasks"}


def test_aggregate_status_projects_io_and_human_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """status_for_identity / format_status_table show I/O; locked view stays pure."""
    from omg_cli.team.launch import worker_status_view
    from omg_cli.team.plane import format_status_table
    from omg_cli.team.runtime import status_for_identity

    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_dry(tmp_path)
    run_id = str(meta["run_id"])
    # worker_status_view projects normalize I/O.
    task = load_team_meta(tmp_path, run_id)["tasks"][0]
    view = worker_status_view(task)
    assert view["io"]["io_mode"] == "headless_stream"
    assert view["io"]["operator_input_supported"] is False
    # startup_status=running must not flip input_ready.
    task_running = {
        **task,
        "status": "running",
        "io_mode": "headless_stream",
        "provider_tty_owner": "supervisor",
        "input_ready": False,
        "operator_input_supported": False,
    }
    assert worker_status_view(task_running)["io"]["input_ready"] is False

    st = status_for_identity(tmp_path, run_id)
    assert st["workers"]
    assert st["workers"][0]["io"]["operator_input_supported"] is False
    assert st["worktrees"][0]["worker"]["io"]["io_mode"] == "headless_stream"
    locked = status_locked_view(st)
    assert "workers" not in locked
    assert "io_mode" not in (locked["tasks"][0] if locked["tasks"] else {})

    table = format_status_table(st)
    assert "io_mode" in table
    assert "headless_stream" in table
    assert "op_in" in table or "op_input" in table
    # Human table must not imply interactivity from dry_run/running alone.
    assert "operator_input_supported" not in table or "op_input=no" in table or "  no  " in table


def test_presentation_fake_job_and_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_job(tmp_path)
    run_id = str(meta["run_id"])
    team_id = str(meta["team_id"])
    old_job = meta["tasks"][0]["execution"]["job_id"]
    wait_job(tmp_path, old_job, timeout_s=30.0)
    first = build_team_presentation_v1(tmp_path, run_id)
    assert first["members"][0]["current_attempt"]["execution"]["topology"] == "job"
    assert first["members"][0]["current_attempt"]["execution"]["job_id"] == old_job
    assert first["members"][0]["route"]["kind"] == "external_executor"

    result = replace_worker(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        worker_id="t1",
        mode="lost",
        expected_attempt=1,
        expected_launch_generation=1,
        idempotency_key="pres-repl-1",
    )
    assert result.ok is True
    wait_job(tmp_path, result.execution["job_id"], timeout_s=30.0)
    second = build_team_presentation_v1(tmp_path, run_id)
    member = second["members"][0]
    assert len(member["attempts"]) == 2
    assert member["attempts"][0]["attempt"] == 1
    assert member["attempts"][0]["execution"]["job_id"] == old_job
    assert member["attempts"][1]["attempt"] == 2
    assert member["current_attempt"]["attempt"] == 2
    # Replay stable.
    assert build_team_presentation_v1(tmp_path, run_id) == second


def test_presentation_legacy_unknown_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_dry(tmp_path)
    run_id = str(meta["run_id"])

    def _strip(current: dict[str, Any]) -> dict[str, Any]:
        tasks = []
        for raw in current.get("tasks") or []:
            row = dict(raw)
            row.pop("route", None)
            tasks.append(row)
        current["tasks"] = tasks
        return current

    mutate_team_meta(tmp_path, run_id, _strip)
    state = build_team_presentation_v1(tmp_path, run_id)
    assert state["members"][0]["route"] == unknown_route()


def test_presentation_dual_handle_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_dry(tmp_path)
    run_id = str(meta["run_id"])

    def _poison(current: dict[str, Any]) -> dict[str, Any]:
        tasks = []
        for raw in current.get("tasks") or []:
            row = dict(raw)
            execution = dict(row.get("execution") or {})
            execution["topology"] = "pane"
            execution["pane_id"] = "%1"
            execution["job_id"] = "job-x"
            execution["launch_generation"] = 1
            row["execution"] = execution
            tasks.append(row)
        current["tasks"] = tasks
        return current

    mutate_team_meta(tmp_path, run_id, _poison)
    with pytest.raises(PresentationError) as exc:
        build_team_presentation_v1(tmp_path, run_id)
    assert exc.value.code == "E_TEAM_PRESENTATION_DUAL_HANDLE"


def test_presentation_duplicate_attempt_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_dry(tmp_path)
    run_id = str(meta["run_id"])

    def _dup(current: dict[str, Any]) -> dict[str, Any]:
        tasks = []
        for raw in current.get("tasks") or []:
            row = dict(raw)
            execution = dict(row.get("execution") or {})
            row["prior_attempts"] = [
                {
                    "schema": 1,
                    "attempt": int(row.get("attempt") or 1),
                    "launch_generation": int(execution.get("launch_generation") or 1),
                    "execution": execution,
                    "reason": "dup",
                    "status": "failed",
                }
            ]
            tasks.append(row)
        current["tasks"] = tasks
        return current

    mutate_team_meta(tmp_path, run_id, _dup)
    with pytest.raises(PresentationError) as exc:
        build_team_presentation_v1(tmp_path, run_id)
    assert exc.value.code == "E_TEAM_PRESENTATION_DUP_ATTEMPT"


def test_presentation_native_receipt_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_dry(tmp_path)
    run_id = str(meta["run_id"])
    receipt_rel = ".omg/artifacts/native-receipt-fixture.json"
    receipt_path = tmp_path / receipt_rel
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    body = b'{"kind":"host_receipt","ok":true}\n'
    receipt_path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()

    def _native(current: dict[str, Any]) -> dict[str, Any]:
        tasks = []
        for raw in current.get("tasks") or []:
            row = dict(raw)
            row["route"] = {
                "schema": 1,
                "kind": "native_host_receipt",
                "receipt_ref": receipt_rel,
                "receipt_digest": digest,
            }
            tasks.append(row)
        current["tasks"] = tasks
        return current

    mutate_team_meta(tmp_path, run_id, _native)
    state = build_team_presentation_v1(tmp_path, run_id)
    assert state["members"][0]["route"]["kind"] == "native_host_receipt"

    # Digest mismatch fails closed.
    receipt_path.write_bytes(b'{"tampered":true}\n')
    with pytest.raises(PresentationError) as exc:
        build_team_presentation_v1(tmp_path, run_id)
    assert exc.value.code == "E_TEAM_PRESENTATION_NATIVE_RECEIPT"


def test_presentation_zero_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_dry(tmp_path)
    run_id = str(meta["run_id"])

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    state = build_team_presentation_v1(tmp_path, run_id)
    assert state["kind"] == PRESENTATION_KIND


def test_presentation_worker_acl_denies_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_dry(tmp_path)
    run_id = str(meta["run_id"])
    team_id = str(meta["team_id"])
    env = {
        EXPERIMENTAL_ENV: "1",
        "OMG_TEAM_WORKER": "1",
        "OMG_TEAM_WORKER_ID": "t1",
        "OMG_TEAM_RUN_ID": run_id,
        "OMG_TEAM_ID": team_id,
    }
    code, envelope = team_api.execute_team_api(
        "read-presentation-state",
        {"run_id": run_id, "team_id": team_id},
        root=tmp_path,
        env=env,
    )
    assert code != 0 or envelope.get("ok") is False


def test_stamp_route_helper_additive() -> None:
    task: dict[str, Any] = {
        "task_id": "t1",
        "provider": "fake",
        "role": "executor",
        "posture": "read-write",
    }
    route = stamp_route_on_task(task, executor="fixture")
    assert route["kind"] == "external_executor"
    assert task["route"]["executor"] == "fixture"
    assert task["route"]["provider"] == "fake"


def test_presentation_dry_run_golden_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stable key shape for dry-run presentation (values scrubbed)."""
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_dry(tmp_path)
    state = build_team_presentation_v1(tmp_path, str(meta["run_id"]))

    def _scrub(obj: Any) -> Any:
        if isinstance(obj, dict):
            out = {}
            for key, value in obj.items():
                if key in {"run_id", "job_id", "pane_id", "team_name"}:
                    out[key] = "<id>"
                elif key == "relative_path":
                    out[key] = "<relpath>"
                elif key == "state_generation":
                    out[key] = 0
                else:
                    out[key] = _scrub(value)
            return out
        if isinstance(obj, list):
            return [_scrub(item) for item in obj]
        return obj

    scrubbed = _scrub(state)
    if not GOLDEN_PRESENTATION.exists():
        GOLDEN_PRESENTATION.write_text(
            json.dumps(scrubbed, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    expected = json.loads(GOLDEN_PRESENTATION.read_text(encoding="utf-8"))
    assert scrubbed == expected
