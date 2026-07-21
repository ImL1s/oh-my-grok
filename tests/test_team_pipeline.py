"""Hermetic tests for staged team pipeline (D2).

FSM + dry-run only. NO live tmux/exec. dry-run must never call tmux/subprocess.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omg_cli.evidence import CLI_WRITER
from omg_cli.state import load_run
from omg_cli.team import plane
from omg_cli.team.plane import EXPERIMENTAL_ENV, TEAM_WORKER_ENV
from omg_cli.team.pipeline import (
    DEFAULT_MAX_FIX,
    LEGAL_TRANSITIONS,
    TeamPipelineError,
    assert_legal_transition,
    invalidate_team_verify_stamp,
    load_team_pipeline,
    parse_team_verify_verdict,
    run_team_pipeline,
    stage_verify_is_approve,
    start_team_pipeline,
    status_team_pipeline,
    team_pipeline_state_path,
    team_verifier_artifact_paths,
    team_verify_stamp_path,
    transition,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_OMG = REPO_ROOT / "bin" / "omg"
PYTHON = sys.executable

TASKS_ONE = [{"task_id": "t1", "owned_files": ["a.py"]}]


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "omg-test@example.com")
    _git(path, "config", "user.name", "omg-test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / ".gitignore").write_text(".omg/\n", encoding="utf-8")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md", ".gitignore")
    _git(path, "commit", "-m", "initial")
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def _enable_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    for key in plane.WORKER_ENV_MARKERS:
        monkeypatch.delenv(key, raising=False)


def _boom_tmux(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("tmux_available must not be called in dry_run")


def _boom_subprocess(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("subprocess must not be called in dry_run")


def _write_verifier(
    root: Path,
    run_id: str,
    body: str,
    *,
    which: str = "json",
) -> Path:
    md, js = team_verifier_artifact_paths(root, run_id)
    path = js if which == "json" else md
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# FSM: legal transitions + terminals
# ---------------------------------------------------------------------------


def test_legal_transition_table() -> None:
    assert_legal_transition("team-plan", "team-prd")
    assert_legal_transition("team-prd", "team-exec")
    assert_legal_transition("team-exec", "team-verify")
    assert_legal_transition("team-verify", "complete")
    assert_legal_transition("team-verify", "team-fix")
    assert_legal_transition("team-fix", "team-exec")
    with pytest.raises(TeamPipelineError, match="illegal"):
        assert_legal_transition("team-plan", "team-verify")
    with pytest.raises(TeamPipelineError, match="illegal"):
        assert_legal_transition("team-exec", "complete")
    with pytest.raises(TeamPipelineError, match="unknown"):
        assert_legal_transition("nope", "team-plan")
    # terminals have no exits
    assert LEGAL_TRANSITIONS["complete"] == frozenset()
    assert LEGAL_TRANSITIONS["failed"] == frozenset()


def test_start_and_transition_enforced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    st = start_team_pipeline(tmp_path, "goal", TASKS_ONE, dry_run=True)
    rid = st["run_id"]
    assert st["phase"] == "team-plan"
    path = team_pipeline_state_path(tmp_path, rid)
    assert path.is_file()
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["writer"] == CLI_WRITER

    with pytest.raises(TeamPipelineError, match="illegal"):
        transition(tmp_path, rid, "team-verify")

    transition(tmp_path, rid, "team-prd")
    transition(tmp_path, rid, "team-exec")
    st2 = status_team_pipeline(tmp_path, rid)
    assert st2["phase"] == "team-exec"
    assert st2["verified"] is False


# ---------------------------------------------------------------------------
# team-verify gate (POST-A2 parse_verdict_file)
# ---------------------------------------------------------------------------


def test_verify_request_changes_goes_to_fix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _boom_subprocess)

    # Plant REQUEST_CHANGES before verify by wrapping start_team
    real_start = plane.start_team

    def start_and_plant(*a: Any, **k: Any) -> dict[str, Any]:
        meta = real_start(*a, **k)
        rid = str(meta["run_id"])
        _write_verifier(
            tmp_path,
            rid,
            json.dumps(
                {"run_id": rid, "verdict": "REQUEST_CHANGES"},
                indent=2,
            )
            + "\n",
        )
        return meta

    monkeypatch.setattr(plane, "start_team", start_and_plant)
    # pipeline imports start_team at module level — patch there too
    monkeypatch.setattr(
        "omg_cli.team.pipeline.start_team", start_and_plant
    )

    out = run_team_pipeline(
        "rc path",
        root=tmp_path,
        tasks_json=TASKS_ONE,
        dry_run=True,
        max_fix=0,  # first fix exceeds → failed without re-exec loop
        force=True,
    )
    # max_fix=0: verify REQUEST_CHANGES → team-fix (round 1) → failed
    assert out["phase"] == "failed"
    assert out["verified"] is False
    assert out["fix_round"] >= 1
    rid = out["run_id"]
    assert parse_team_verify_verdict(tmp_path, rid) == "REQUEST_CHANGES"
    assert stage_verify_is_approve(tmp_path, rid) is False


def test_verify_approve_completes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _boom_subprocess)

    real_start = plane.start_team

    def start_and_plant(*a: Any, **k: Any) -> dict[str, Any]:
        meta = real_start(*a, **k)
        rid = str(meta["run_id"])
        _write_verifier(
            tmp_path,
            rid,
            json.dumps({"run_id": rid, "verdict": "APPROVE"}, indent=2) + "\n",
        )
        return meta

    monkeypatch.setattr("omg_cli.team.pipeline.start_team", start_and_plant)

    out = run_team_pipeline(
        "approve path",
        root=tmp_path,
        tasks_json=TASKS_ONE,
        dry_run=True,
        max_fix=3,
        force=True,
    )
    assert out["phase"] == "complete"
    assert out["verified"] is False  # never sets verified
    rid = out["run_id"]
    assert stage_verify_is_approve(tmp_path, rid) is True
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("verified") is not True


def test_post_a2_stray_approve_next_to_request_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run_id-less stray APPROVE must not beat a real REQUEST_CHANGES (A2)."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    st = start_team_pipeline(tmp_path, "poison", TASKS_ONE, dry_run=True)
    rid = st["run_id"]
    # Same shape as A2 tests: fenced/stray APPROVE + bound REQUEST_CHANGES
    body = (
        '{"verdict": "APPROVE"}\n'
        f'{{"run_id": "{rid}", "verdict": "REQUEST_CHANGES"}}\n'
    )
    _write_verifier(tmp_path, rid, body)
    assert parse_team_verify_verdict(tmp_path, rid) == "REQUEST_CHANGES"
    assert stage_verify_is_approve(tmp_path, rid) is False


def test_forged_clean_true_does_not_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    st = start_team_pipeline(tmp_path, "forge", TASKS_ONE, dry_run=True)
    rid = st["run_id"]
    stamp = team_verify_stamp_path(tmp_path, rid)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    # Leader-authored / forged stamps without CLI writer or real APPROVE verdict
    stamp.write_text(
        json.dumps(
            {
                "clean": True,
                "verified": True,
                "run_id": rid,
                "writer": "leader-agent",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert stage_verify_is_approve(tmp_path, rid) is False

    # CLI writer + clean but no APPROVE verdict → still refuse
    stamp.write_text(
        json.dumps(
            {
                "clean": True,
                "verified": True,
                "run_id": rid,
                "writer": CLI_WRITER,
                "verdict": "REQUEST_CHANGES",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert stage_verify_is_approve(tmp_path, rid) is False

    # complete requires durable APPROVE stamp
    transition(tmp_path, rid, "team-prd")
    transition(tmp_path, rid, "team-exec")
    transition(tmp_path, rid, "team-verify")
    with pytest.raises(TeamPipelineError, match="APPROVE"):
        transition(tmp_path, rid, "complete")


# ---------------------------------------------------------------------------
# stale-stamp invalidation
# ---------------------------------------------------------------------------


def test_stale_stamp_invalidated_on_exec_and_fix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    st = start_team_pipeline(tmp_path, "stale", TASKS_ONE, dry_run=True)
    rid = st["run_id"]
    # Walk to verify and plant a real APPROVE stamp (simulate prior gate)
    transition(tmp_path, rid, "team-prd")
    transition(tmp_path, rid, "team-exec")
    transition(tmp_path, rid, "team-verify")
    stamp = team_verify_stamp_path(tmp_path, rid)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(
        json.dumps(
            {
                "writer": CLI_WRITER,
                "run_id": rid,
                "verdict": "APPROVE",
                "clean": True,
                "invalidated": False,
                "status": "clean",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert stage_verify_is_approve(tmp_path, rid) is True

    # Enter fix → invalidates
    transition(tmp_path, rid, "team-fix", reason="rework")
    assert stage_verify_is_approve(tmp_path, rid) is False
    data = json.loads(stamp.read_text(encoding="utf-8"))
    assert data["invalidated"] is True
    assert data["clean"] is False

    # Re-stamp APPROVE then re-enter exec → invalidated again
    stamp.write_text(
        json.dumps(
            {
                "writer": CLI_WRITER,
                "run_id": rid,
                "verdict": "APPROVE",
                "clean": True,
                "invalidated": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert stage_verify_is_approve(tmp_path, rid) is True
    transition(tmp_path, rid, "team-exec", reason="re-exec")
    assert stage_verify_is_approve(tmp_path, rid) is False

    # explicit helper mirrors autopilot
    stamp.write_text(
        json.dumps(
            {
                "writer": CLI_WRITER,
                "run_id": rid,
                "verdict": "APPROVE",
                "clean": True,
                "invalidated": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    invalidate_team_verify_stamp(tmp_path, rid, reason="test")
    assert stage_verify_is_approve(tmp_path, rid) is False


# ---------------------------------------------------------------------------
# bounded fix loop
# ---------------------------------------------------------------------------


def test_max_fix_exceeded_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _boom_subprocess)

    real_start = plane.start_team

    def start_and_plant_rc(*a: Any, **k: Any) -> dict[str, Any]:
        meta = real_start(*a, **k)
        rid = str(meta["run_id"])
        _write_verifier(
            tmp_path,
            rid,
            json.dumps(
                {"run_id": rid, "verdict": "REQUEST_CHANGES"},
                indent=2,
            )
            + "\n",
        )
        return meta

    monkeypatch.setattr("omg_cli.team.pipeline.start_team", start_and_plant_rc)

    out = run_team_pipeline(
        "loop",
        root=tmp_path,
        tasks_json=TASKS_ONE,
        dry_run=True,
        max_fix=2,
        force=True,
    )
    assert out["phase"] == "failed"
    assert out["fix_round"] > 2
    assert out["verified"] is False
    # history must show multiple exec/fix cycles, not infinite
    hist = out.get("history") or []
    fix_entries = [h for h in hist if h.get("phase") == "team-fix"]
    assert len(fix_entries) >= 2
    assert len(fix_entries) <= 5  # bounded


# ---------------------------------------------------------------------------
# dry-run full sequence + CLI
# ---------------------------------------------------------------------------


def test_dry_run_sequences_no_tmux_no_verified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _boom_subprocess)

    out = run_team_pipeline(
        "dry seq",
        root=tmp_path,
        tasks_json=TASKS_ONE,
        dry_run=True,
        max_fix=1,
        force=True,
    )
    # no verifier artifact → UNKNOWN → fix → fail (or complete never)
    assert out["phase"] in ("failed", "blocked")
    assert out["verified"] is False
    rid = out["run_id"]
    state = load_team_pipeline(tmp_path, rid)
    assert state["writer"] == CLI_WRITER
    assert state["dry_run"] is True
    phases = [h.get("phase") for h in state.get("history") or []]
    assert "team-prd" in phases
    assert "team-exec" in phases
    assert "team-verify" in phases
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("verified") is not True
    # forged verified in pipeline state would be ignored by status
    assert status_team_pipeline(tmp_path, rid)["verified"] is False


def test_cli_team_run_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    env = os.environ.copy()
    env[EXPERIMENTAL_ENV] = "1"
    for k in plane.WORKER_ENV_MARKERS:
        env.pop(k, None)
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    r = subprocess.run(
        [
            PYTHON,
            str(BIN_OMG),
            "team",
            "run",
            "--dry-run",
            "--force",
            "--goal",
            "cli dry",
            "--tasks-json",
            json.dumps(TASKS_ONE),
            "--max-fix",
            "0",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    # failed (no approve) → exit 1; must not crash
    assert r.returncode in (0, 1), r.stderr + r.stdout
    payload = json.loads(r.stdout)
    assert payload["writer"] if "writer" in payload else True
    assert payload.get("verified") is not True
    assert payload["phase"] in ("failed", "complete", "blocked")
    rid = payload["run_id"]
    assert team_pipeline_state_path(tmp_path, rid).is_file()
    disk = json.loads(team_pipeline_state_path(tmp_path, rid).read_text())
    assert disk["writer"] == CLI_WRITER


def test_cli_team_run_refuses_without_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    env = os.environ.copy()
    env.pop(EXPERIMENTAL_ENV, None)
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    r = subprocess.run(
        [
            PYTHON,
            str(BIN_OMG),
            "team",
            "run",
            "--dry-run",
            "--goal",
            "x",
            "--tasks-json",
            json.dumps(TASKS_ONE),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2
    assert EXPERIMENTAL_ENV in (r.stderr + r.stdout)


def test_refuses_spawned_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setenv(TEAM_WORKER_ENV, "1")
    from omg_cli.team.plane import TeamGateError

    with pytest.raises(TeamGateError, match="spawned-worker"):
        run_team_pipeline(
            "nested",
            root=tmp_path,
            tasks_json=TASKS_ONE,
            dry_run=True,
            force=True,
        )


def test_tasks_path_loads_decomposition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _boom_subprocess)

    tasks_file = tmp_path / "ralplan-tasks.json"
    tasks_file.write_text(
        json.dumps({"tasks": TASKS_ONE}, indent=2) + "\n",
        encoding="utf-8",
    )
    out = run_team_pipeline(
        "from path",
        root=tmp_path,
        tasks_path=tasks_file,
        dry_run=True,
        max_fix=0,
        force=True,
    )
    assert out["phase"] == "failed"
    state = load_team_pipeline(tmp_path, out["run_id"])
    assert state["tasks"][0]["task_id"] == "t1"


def test_default_max_fix_constant() -> None:
    assert DEFAULT_MAX_FIX == 3
