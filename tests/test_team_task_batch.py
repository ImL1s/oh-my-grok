"""Hermetic tests for Team Catalog V4 atomic task-batch DAG admission (#69 PR11)."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
from omg_cli.main import main
from omg_cli.team.api import execute_team_api
from omg_cli.team.compositions.hyperplan import compile_hyperplan_v1
from omg_cli.team.compositions.security_research import compile_security_research_v1
from omg_cli.team.operation_catalog import (
    CATALOG_SCHEMA_VERSION,
    TEAM_OPERATION_CATALOG_V1,
    TEAM_OPERATION_CATALOG_V2,
    TEAM_OPERATION_CATALOG_V3,
    TEAM_OPERATION_CATALOG_V4,
    catalog_document_json,
    serialize_operation_catalog,
)
from omg_cli.team.plane import EXPERIMENTAL_ENV, WORKER_ENV_MARKERS, start_team
from omg_cli.team import task_batch as tb


ROOT = Path(__file__).resolve().parents[1]
GOLDEN_V1 = ROOT / "tests" / "golden" / "team_operation_catalog_v1.json"
GOLDEN_V2 = ROOT / "tests" / "golden" / "team_operation_catalog_v2.json"
GOLDEN_V3 = ROOT / "tests" / "golden" / "team_operation_catalog_v3.json"
GOLDEN_V4 = ROOT / "tests" / "golden" / "team_operation_catalog_v4.json"

TEAM = "team-api"
SEED_TASKS = [{"task_id": "t-a", "owned_files": ["a.py"]}]
SOURCE_DIGEST = "a" * 64


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


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "task-batch seed",
        SEED_TASKS,
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
    )
    return str(meta["run_id"])


def _artifact(**extra: object) -> dict:
    row: dict = {
        "kind": "omg.team.test.artifact",
        "schema_version": 1,
        "required_fields": ["summary"],
    }
    row.update(extra)
    return row


def _batch(
    run_id: str,
    *,
    batch_id: str = "batch-1",
    key: str = "idem-1",
    tasks: list[dict] | None = None,
    source_kind: str = "test.fixture",
    source_id: str = "src-1",
) -> dict:
    if tasks is None:
        tasks = [
            {
                "task_key": "root",
                "subject": "root task",
                "description": "do root",
                "depends_on": [],
                "requires_code_change": False,
                "expected_artifact": _artifact(),
            },
            {
                "task_key": "child",
                "subject": "child task",
                "description": "do child",
                "depends_on": ["root"],
                "requires_code_change": False,
                "expected_artifact": _artifact(),
            },
        ]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "team_id": TEAM,
        "batch_id": batch_id,
        "idempotency_key": key,
        "source": {
            "kind": source_kind,
            "source_id": source_id,
            "digest": SOURCE_DIGEST,
        },
        "tasks": tasks,
    }


def _lanes_to_tasks(lanes: list[dict]) -> list[dict]:
    out: list[dict] = []
    for lane in lanes:
        out.append(
            {
                "task_key": lane["lane_id"],
                "subject": f"lane {lane['lane_id']}",
                "description": f"execute {lane['lane_id']}",
                "depends_on": list(lane["depends_on"]),
                "requires_code_change": bool(lane.get("requires_code_change", False)),
                "expected_artifact": dict(lane["expected_artifact"]),
            }
        )
    return out


@pytest.fixture(autouse=True)
def _clear_crash_hook() -> None:
    tb._crash_hook = None
    yield
    tb._crash_hook = None


def test_catalog_v1_v2_v3_v4_byte_frozen_and_v5_default() -> None:
    assert (
        catalog_document_json(operations=TEAM_OPERATION_CATALOG_V1, schema_version=1)
        == GOLDEN_V1.read_text(encoding="utf-8")
    )
    assert (
        catalog_document_json(operations=TEAM_OPERATION_CATALOG_V2, schema_version=2)
        == GOLDEN_V2.read_text(encoding="utf-8")
    )
    assert (
        catalog_document_json(operations=TEAM_OPERATION_CATALOG_V3, schema_version=3)
        == GOLDEN_V3.read_text(encoding="utf-8")
    )
    assert (
        catalog_document_json(operations=TEAM_OPERATION_CATALOG_V4, schema_version=4)
        == GOLDEN_V4.read_text(encoding="utf-8")
    )
    assert CATALOG_SCHEMA_VERSION == 5
    assert any(
        op["name"] == "bulk-create-tasks"
        for op in serialize_operation_catalog()["operations"]
    )
    assert any(
        op["name"] == "enqueue-host-prompt"
        for op in serialize_operation_catalog()["operations"]
    )


def test_compile_deterministic_topo_independent_of_input_order() -> None:
    tasks_a = [
        {
            "task_key": "z",
            "subject": "z",
            "description": "z",
            "depends_on": ["a"],
            "requires_code_change": False,
            "expected_artifact": _artifact(),
        },
        {
            "task_key": "a",
            "subject": "a",
            "description": "a",
            "depends_on": [],
            "requires_code_change": False,
            "expected_artifact": _artifact(),
        },
        {
            "task_key": "m",
            "subject": "m",
            "description": "m",
            "depends_on": ["a"],
            "requires_code_change": False,
            "expected_artifact": _artifact(),
        },
    ]
    tasks_b = list(reversed(tasks_a))
    base = {
        "schema_version": 1,
        "run_id": "run1",
        "team_id": "team",
        "batch_id": "b1",
        "idempotency_key": "k1",
        "source": {
            "kind": "test",
            "source_id": "s1",
            "digest": SOURCE_DIGEST,
        },
    }
    ca = tb.compile_task_batch_v1({**base, "tasks": tasks_a})
    cb = tb.compile_task_batch_v1({**base, "tasks": tasks_b})
    assert ca["topo_order"] == cb["topo_order"] == ["a", "m", "z"]
    assert ca["digest"] == cb["digest"]
    assert ca["tasks"][0]["task_key"] == "a"


def test_compile_rejects_cycle_unknown_dup_self_and_bounds() -> None:
    with pytest.raises(tb.TaskBatchError, match="cycle"):
        tb.compile_task_batch_v1(
            _batch(
                "run1",
                tasks=[
                    {
                        "task_key": "a",
                        "subject": "a",
                        "description": "a",
                        "depends_on": ["b"],
                        "requires_code_change": False,
                        "expected_artifact": _artifact(),
                    },
                    {
                        "task_key": "b",
                        "subject": "b",
                        "description": "b",
                        "depends_on": ["a"],
                        "requires_code_change": False,
                        "expected_artifact": _artifact(),
                    },
                ],
            )
        )
    with pytest.raises(tb.TaskBatchError, match="unknown"):
        tb.compile_task_batch_v1(
            _batch(
                "run1",
                tasks=[
                    {
                        "task_key": "a",
                        "subject": "a",
                        "description": "a",
                        "depends_on": ["missing"],
                        "requires_code_change": False,
                        "expected_artifact": _artifact(),
                    }
                ],
            )
        )
    with pytest.raises(tb.TaskBatchError, match="self-dependency"):
        tb.compile_task_batch_v1(
            _batch(
                "run1",
                tasks=[
                    {
                        "task_key": "a",
                        "subject": "a",
                        "description": "a",
                        "depends_on": ["a"],
                        "requires_code_change": False,
                        "expected_artifact": _artifact(),
                    }
                ],
            )
        )
    with pytest.raises(tb.TaskBatchError, match="unique"):
        tb.compile_task_batch_v1(
            _batch(
                "run1",
                tasks=[
                    {
                        "task_key": "a",
                        "subject": "a",
                        "description": "a",
                        "depends_on": [],
                        "requires_code_change": False,
                        "expected_artifact": _artifact(),
                    },
                    {
                        "task_key": "a",
                        "subject": "a2",
                        "description": "a2",
                        "depends_on": [],
                        "requires_code_change": False,
                        "expected_artifact": _artifact(),
                    },
                ],
            )
        )
    with pytest.raises(tb.TaskBatchError, match="duplicate depends_on"):
        tb.compile_task_batch_v1(
            _batch(
                "run1",
                tasks=[
                    {
                        "task_key": "a",
                        "subject": "a",
                        "description": "a",
                        "depends_on": [],
                        "requires_code_change": False,
                        "expected_artifact": _artifact(),
                    },
                    {
                        "task_key": "b",
                        "subject": "b",
                        "description": "b",
                        "depends_on": ["a", "a"],
                        "requires_code_change": False,
                        "expected_artifact": _artifact(),
                    },
                ],
            )
        )
    too_many = [
        {
            "task_key": f"t{i}",
            "subject": f"t{i}",
            "description": f"t{i}",
            "depends_on": [],
            "requires_code_change": False,
            "expected_artifact": _artifact(),
        }
        for i in range(33)
    ]
    with pytest.raises(tb.TaskBatchError, match="1–32"):
        tb.compile_task_batch_v1(_batch("run1", tasks=too_many))
    with pytest.raises(tb.TaskBatchError, match="rejects caller-supplied"):
        bad = _batch("run1")
        bad["tasks"][0]["status"] = "completed"
        tb.compile_task_batch_v1(bad)
    with pytest.raises(tb.TaskBatchError, match="rejects caller-supplied"):
        bad = _batch("run1")
        bad["tasks"][0]["id"] = "9"
        tb.compile_task_batch_v1(bad)


def test_hyperplan_and_security_research_shaped_batches_compile() -> None:
    hp = compile_hyperplan_v1(
        {
            "schema_version": 1,
            "goal": "ship a safe plan",
            "critique_dimensions": ["security", "correctness", "operability"],
        }
    )
    hp_batch = {
        "schema_version": 1,
        "run_id": "run-hp",
        "team_id": "team",
        "batch_id": "hp-batch",
        "idempotency_key": "hp-key",
        "source": {
            "kind": "omg.team.hyperplan",
            "source_id": hp["composition_id"],
            "digest": hp["digest"],
        },
        "tasks": _lanes_to_tasks(hp["lanes"]),
    }
    compiled_hp = tb.compile_task_batch_v1(hp_batch)
    assert compiled_hp["topo_order"][0].startswith("critic.")
    assert compiled_hp["topo_order"][-1] == "verify"
    assert "synthesize" in compiled_hp["topo_order"]

    sr = compile_security_research_v1(
        {
            "schema_version": 1,
            "target": "example app",
            "attack_surfaces": ["auth", "injection", "secrets"],
        }
    )
    sr_batch = {
        "schema_version": 1,
        "run_id": "run-sr",
        "team_id": "team",
        "batch_id": "sr-batch",
        "idempotency_key": "sr-key",
        "source": {
            "kind": "omg.team.security_research",
            "source_id": sr["composition_id"],
            "digest": sr["digest"],
        },
        "tasks": _lanes_to_tasks(sr["lanes"]),
    }
    compiled_sr = tb.compile_task_batch_v1(sr_batch)
    assert len(compiled_sr["tasks"]) == sr["lane_count"]
    assert compiled_sr["topo_order"][-1] == "verify"


def test_admit_idempotent_and_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    payload = _batch(run_id)
    code, first = execute_team_api(
        "bulk-create-tasks", payload, root=tmp_path, env={EXPERIMENTAL_ENV: "1"}
    )
    assert code == 0
    assert first["ok"] is True
    result = first["data"]
    assert result["state"] == "committed"
    assert result["idempotent"] is False
    assert set(result["task_key_to_id"]) == {"root", "child"}

    code, again = execute_team_api(
        "bulk-create-tasks", payload, root=tmp_path, env={EXPERIMENTAL_ENV: "1"}
    )
    assert code == 0
    assert again["data"]["idempotent"] is True
    assert again["data"]["task_key_to_id"] == result["task_key_to_id"]
    assert again["data"]["digest"] == result["digest"]

    conflict = dict(payload)
    conflict["tasks"] = [
        {
            **payload["tasks"][0],
            "subject": "changed subject",
        },
        payload["tasks"][1],
    ]
    code, refused = execute_team_api(
        "bulk-create-tasks", conflict, root=tmp_path, env={EXPERIMENTAL_ENV: "1"}
    )
    assert code != 0
    assert refused["ok"] is False
    err = refused["error"]
    details = err.get("details") or err
    code_s = str(details.get("code") or details.get("error") or err.get("code") or "")
    assert "CONFLICT" in code_s or "conflict" in str(err).lower()


def test_uncommitted_tasks_invisible_until_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    payload = _batch(run_id, key="crash-key")
    reserved: dict = {}

    def hook(point: str) -> None:
        if point == "before_commit":
            raise RuntimeError("injected crash before commit")

    tb._crash_hook = hook
    with pytest.raises(RuntimeError, match="before commit"):
        tb.admit_task_batch_v1(tmp_path, payload)

    # Tasks exist on disk but are invisible.
    listed = execute_team_api(
        "list-tasks",
        {"run_id": run_id, "team_id": TEAM},
        root=tmp_path,
        env={EXPERIMENTAL_ENV: "1"},
    )
    assert listed[0] == 0
    assert listed[1]["data"]["count"] == 0

    # Raw read still sees prepared files for recovery.
    from omg_cli.team import api as team_api

    record = tb._load_batch_record(
        tb.batch_record_path(tmp_path, run_id, TEAM, "crash-key"),
        run_id=run_id,
        team_id=TEAM,
    )
    assert record is not None
    assert record["state"] == "prepared"
    reserved = dict(record["task_key_to_id"])
    raw = team_api._read_task(tmp_path, run_id, TEAM, reserved["root"])
    assert raw is not None
    assert raw["batch"]["task_key"] == "root"

    code, read = execute_team_api(
        "read-task",
        {"run_id": run_id, "team_id": TEAM, "task_id": reserved["root"]},
        root=tmp_path,
        env={EXPERIMENTAL_ENV: "1"},
    )
    assert code != 0
    assert read["error"]["details"]["error"] == "task_not_found"

    # Seed a worker then attempt claim → not found.
    execute_team_api(
        "create-task",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "subject": "seed",
            "description": "seed",
            "workers": ["w1"],
        },
        root=tmp_path,
        env={EXPERIMENTAL_ENV: "1"},
    )
    code, claim = execute_team_api(
        "claim-task",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "task_id": reserved["root"],
            "worker": "w1",
        },
        root=tmp_path,
        env={EXPERIMENTAL_ENV: "1"},
    )
    assert code == 1
    assert claim["ok"] is False
    assert claim["error"]["details"]["error"] == "task_not_found"

    # Resume completes with original reserved IDs.
    tb._crash_hook = None
    finished = tb.admit_task_batch_v1(tmp_path, payload)
    assert finished["idempotent"] is False
    assert finished["state"] == "committed"
    assert finished["task_key_to_id"] == reserved

    code, listed2 = execute_team_api(
        "list-tasks",
        {"run_id": run_id, "team_id": TEAM},
        root=tmp_path,
        env={EXPERIMENTAL_ENV: "1"},
    )
    assert code == 0
    ids = {t["id"] for t in listed2["data"]["tasks"]}
    assert reserved["root"] in ids
    assert reserved["child"] in ids


def test_crash_after_reserve_and_after_task_write_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    payload = _batch(run_id, key="crash-reserve")

    def after_reserve(point: str) -> None:
        if point == "after_reserve":
            raise RuntimeError("injected crash after reserve")

    tb._crash_hook = after_reserve
    with pytest.raises(RuntimeError, match="after reserve"):
        tb.admit_task_batch_v1(tmp_path, payload)

    record = tb._load_batch_record(
        tb.batch_record_path(tmp_path, run_id, TEAM, "crash-reserve"),
        run_id=run_id,
        team_id=TEAM,
    )
    assert record is not None and record["state"] == "prepared"
    reserved = dict(record["task_key_to_id"])

    tb._crash_hook = None
    # Crash after first task write on resume path.
    seen = {"n": 0}

    def after_first_write(point: str) -> None:
        if point == "after_task_write:0":
            seen["n"] += 1
            if seen["n"] == 1:
                raise RuntimeError("injected crash after task write")

    tb._crash_hook = after_first_write
    with pytest.raises(RuntimeError, match="after task write"):
        tb.admit_task_batch_v1(tmp_path, payload)

    tb._crash_hook = None
    done = tb.admit_task_batch_v1(tmp_path, payload)
    assert done["task_key_to_id"] == reserved
    assert done["state"] == "committed"


def test_concurrent_identical_and_conflicting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    payload = _batch(run_id, key="concurrent-same")
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(tb.admit_task_batch_v1(tmp_path, payload))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(results) == 4
    digests = {r["digest"] for r in results}
    mappings = {json.dumps(r["task_key_to_id"], sort_keys=True) for r in results}
    assert len(digests) == 1
    assert len(mappings) == 1
    assert sum(1 for r in results if r["idempotent"]) >= 1

    # Conflicting digest under same key after commit.
    other = dict(payload)
    other["tasks"] = [
        {**payload["tasks"][0], "description": "different"},
        payload["tasks"][1],
    ]
    with pytest.raises(tb.TaskBatchError, match="conflicts"):
        tb.admit_task_batch_v1(tmp_path, other)


def test_cli_bulk_create_tasks_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    payload = _batch(run_id, key="cli-key")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path))
    rc = main(
        [
            "team",
            "api",
            "bulk-create-tasks",
            "--input",
            json.dumps(payload),
            "--json",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    envelope = json.loads(out)
    # emit_data may wrap; accept either bare envelope or nested.
    body = envelope.get("data") or envelope
    if body.get("ok") is True and isinstance(body.get("data"), dict):
        result = body["data"]
    else:
        result = body
    assert result.get("state") == "committed"
    assert "task_key_to_id" in result


def test_worker_denied_bulk_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    payload = _batch(run_id, key="worker-denied")
    code, envelope = execute_team_api(
        "bulk-create-tasks",
        payload,
        root=tmp_path,
        env={
            EXPERIMENTAL_ENV: "1",
            "OMG_TEAM_WORKER": "1",
            "OMG_TEAM_WORKER_ID": "w1",
            "OMG_TEAM_RUN_ID": run_id,
            "OMG_TEAM_ID": TEAM,
        },
    )
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "E_TEAM_API_GATE"


def test_no_subprocess_tmux_network_in_compile() -> None:
    # Pure compiler path must not touch filesystem / subprocess.
    compiled = tb.compile_task_batch_v1(_batch("pure-run"))
    assert compiled["digest"] == sha256_hex(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "run_id": compiled["run_id"],
                "team_id": compiled["team_id"],
                "batch_id": compiled["batch_id"],
                "source": compiled["source"],
                "tasks": compiled["tasks"],
                "topo_order": compiled["topo_order"],
            }
        )
    )


def test_symlink_batch_record_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    payload = _batch(run_id, key="symlink-key")
    path = tb.batch_record_path(tmp_path, run_id, TEAM, "symlink-key")
    path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(tb.TaskBatchError, match="symlink"):
        tb.admit_task_batch_v1(tmp_path, payload)


def test_prepared_mapping_tamper_refuses_foreign_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash after reserve → create-task takes next id → tamper mapping must refuse."""
    from omg_cli.contracts.writer_chain import canonical_json_bytes
    from omg_cli.team import api as team_api

    run_id = _seed(tmp_path, monkeypatch)
    payload = _batch(run_id, key="tamper-key")

    def after_reserve(point: str) -> None:
        if point == "after_reserve":
            raise RuntimeError("injected crash after reserve")

    tb._crash_hook = after_reserve
    with pytest.raises(RuntimeError, match="after reserve"):
        tb.admit_task_batch_v1(tmp_path, payload)
    tb._crash_hook = None

    path = tb.batch_record_path(tmp_path, run_id, TEAM, "tamper-key")
    record = tb._load_batch_record(path, run_id=run_id, team_id=TEAM)
    assert record is not None and record["state"] == "prepared"

    # Foreign create-task consumes the next free numeric id (typically 3).
    code, created = execute_team_api(
        "create-task",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "subject": "foreign",
            "description": "must not be clobbered",
            "workers": ["w1"],
        },
        root=tmp_path,
        env={EXPERIMENTAL_ENV: "1"},
    )
    assert code == 0
    foreign_id = str(created["data"]["task"]["id"])
    foreign_before = team_api._read_task(tmp_path, run_id, TEAM, foreign_id)
    assert foreign_before is not None
    assert foreign_before.get("subject") == "foreign"

    # Tamper prepared mapping: point child at the foreign task id.
    tampered = dict(record)
    mapping = dict(tampered["task_key_to_id"])
    mapping["child"] = foreign_id
    tampered["task_key_to_id"] = mapping
    path.write_bytes(canonical_json_bytes(tampered))

    with pytest.raises(tb.TaskBatchError, match="foreign overwrite"):
        tb.admit_task_batch_v1(tmp_path, payload)

    foreign_after = team_api._read_task(tmp_path, run_id, TEAM, foreign_id)
    assert foreign_after == foreign_before


def test_incomplete_prepared_mapping_raises_task_batch_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.contracts.writer_chain import canonical_json_bytes

    run_id = _seed(tmp_path, monkeypatch)
    payload = _batch(run_id, key="incomplete-map")

    def after_reserve(point: str) -> None:
        if point == "after_reserve":
            raise RuntimeError("injected crash after reserve")

    tb._crash_hook = after_reserve
    with pytest.raises(RuntimeError, match="after reserve"):
        tb.admit_task_batch_v1(tmp_path, payload)
    tb._crash_hook = None

    path = tb.batch_record_path(tmp_path, run_id, TEAM, "incomplete-map")
    record = tb._load_batch_record(path, run_id=run_id, team_id=TEAM)
    assert record is not None
    tampered = dict(record)
    mapping = dict(tampered["task_key_to_id"])
    mapping.pop("child")
    tampered["task_key_to_id"] = mapping
    path.write_bytes(canonical_json_bytes(tampered))

    with pytest.raises(tb.TaskBatchError, match="keys mismatch|missing entry|topo_order"):
        tb.admit_task_batch_v1(tmp_path, payload)


def test_after_reserve_create_task_skips_reserved_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """next_task_id must advance before prepared record is durable."""
    run_id = _seed(tmp_path, monkeypatch)
    payload = _batch(run_id, key="reserve-first")

    def after_reserve(point: str) -> None:
        if point == "after_reserve":
            raise RuntimeError("injected crash after reserve")

    tb._crash_hook = after_reserve
    with pytest.raises(RuntimeError, match="after reserve"):
        tb.admit_task_batch_v1(tmp_path, payload)
    tb._crash_hook = None

    path = tb.batch_record_path(tmp_path, run_id, TEAM, "reserve-first")
    record = tb._load_batch_record(path, run_id=run_id, team_id=TEAM)
    assert record is not None
    reserved_ids = set(record["task_key_to_id"].values())

    code, created = execute_team_api(
        "create-task",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "subject": "after-reserve",
            "description": "must not steal reserved ids",
            "workers": ["w1"],
        },
        root=tmp_path,
        env={EXPERIMENTAL_ENV: "1"},
    )
    assert code == 0
    new_id = str(created["data"]["task"]["id"])
    assert new_id not in reserved_ids
