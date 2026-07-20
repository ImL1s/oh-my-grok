"""Tests for omg worker prepare/seal — worktree + ULW envelope."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omg_cli.state import create_run, load_run
from omg_cli.workers import (
    WorkerError,
    envelope_path,
    prepare_task,
    seal_task,
    worktree_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_OMG = REPO_ROOT / "bin" / "omg"
PYTHON = sys.executable


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


def _run_omg(*args, cwd=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return subprocess.run(
        [PYTHON, str(BIN_OMG), *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_prepare_and_seal_writes_envelope(tmp_path):
    base = _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="seal", extra={"base_sha": base})
    rid = run["run_id"]

    wt = prepare_task(tmp_path, rid, "task-a")
    assert wt.is_dir()
    assert wt == worktree_dir(tmp_path, rid, "task-a")
    # Linked worktree should have .git
    assert (wt / ".git").exists() or (wt / ".git").is_file()

    (wt / "feature.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    env = seal_task(tmp_path, rid, "task-a", message="add feature")
    assert env["status"] == "ok"
    assert env["base_sha"] == base
    assert env["head_sha"] != base
    assert "feature.py" in env["changed_files"]
    assert env["writer"] == "omg-cli"

    epath = envelope_path(tmp_path, "task-a", run_id=rid)
    assert epath.is_file()
    disk = json.loads(epath.read_text(encoding="utf-8"))
    assert disk["task_id"] == "task-a"
    assert disk["status"] == "ok"
    assert disk["head_sha"] == env["head_sha"]


def test_seal_no_changes_fails(tmp_path):
    base = _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="empty", extra={"base_sha": base})
    prepare_task(tmp_path, run["run_id"], "empty-t")
    env = seal_task(tmp_path, run["run_id"], "empty-t")
    assert env["status"] == "failed"
    assert "no changes" in (env.get("note") or env.get("evidence") or "")


def test_seal_dirty_no_commit_fails(tmp_path, monkeypatch):
    """Dirty worktree that produces no new commit must not seal status=ok."""
    base = _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="dirty", extra={"base_sha": base})
    rid = run["run_id"]
    prepare_task(tmp_path, rid, "dirty-t")
    wt = worktree_dir(tmp_path, rid, "dirty-t")
    # Untracked file that gets add'd but we force "no staged changes" path:
    # write file then mock cached-diff quiet (no staged) while porcelain was dirty.
    (wt / "noise.bin").write_bytes(b"\x00\x01")
    import omg_cli.workers as workers

    real_run = workers._run_git

    def selective_git(args, cwd=None, timeout=30.0):
        # After add -A, pretend index has nothing staged (returncode 0 for --quiet)
        if list(args[:3]) == ["diff", "--cached", "--quiet"]:
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        return real_run(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(workers, "_run_git", selective_git)
    env = seal_task(tmp_path, rid, "dirty-t")
    assert env["status"] == "failed"
    assert env["head_sha"] == base or env["head_sha"] == env["base_sha"]
    blob = (env.get("note") or "") + (env.get("evidence") or "")
    assert "no new commit" in blob or "head_sha==base_sha" in blob or "dirty" in blob


def test_prepare_invalid_task_id(tmp_path):
    _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="bad")
    with pytest.raises(WorkerError, match="task_id"):
        prepare_task(tmp_path, run["run_id"], "../evil")


def test_cli_worker_prepare_seal(tmp_path):
    base = _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="cli-w", extra={"base_sha": base})
    rid = run["run_id"]

    r1 = _run_omg("worker", "prepare", "--task", "w1", "--run", rid, cwd=tmp_path)
    assert r1.returncode == 0, r1.stderr + r1.stdout
    wt = worktree_dir(tmp_path, rid, "w1")
    assert wt.is_dir()
    (wt / "x.txt").write_text("x\n", encoding="utf-8")

    r2 = _run_omg(
        "worker",
        "seal",
        "--task",
        "w1",
        "--run",
        rid,
        "--message",
        "cli seal",
        cwd=tmp_path,
    )
    assert r2.returncode == 0, r2.stderr + r2.stdout
    assert envelope_path(tmp_path, "w1", run_id=rid).is_file()


def test_cli_worker_help():
    r = _run_omg("worker", "--help")
    assert r.returncode == 0
    assert "prepare" in r.stdout or "seal" in r.stdout


def test_ownership_manifest_collision_and_join(tmp_path):
    from omg_cli.workers import (
        WorkerError,
        build_ownership_manifest,
        join_worker_results,
        prepare_task,
        seal_task,
    )

    base = _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="own", extra={"base_sha": base})
    rid = run["run_id"]

    with pytest.raises(WorkerError, match="collision"):
        build_ownership_manifest(
            tmp_path,
            rid,
            [
                {"task_id": "a", "owned_files": ["x.py"]},
                {"task_id": "b", "owned_files": ["x.py"]},
            ],
        )

    manifest = build_ownership_manifest(
        tmp_path,
        rid,
        [
            {
                "task_id": "t1",
                "owned_files": ["a.py"],
                "capability_mode": "read-write",
            },
            {
                "task_id": "t2",
                "owned_files": ["b.py"],
                "capability_mode": "read-write",
            },
        ],
    )
    assert manifest["writer"] == "omg-cli"
    assert len(manifest["tasks"]) == 2

    # missing envelopes block join
    joined = join_worker_results(tmp_path, rid)
    assert joined["complete"] is False
    assert set(joined["missing"]) == {"t1", "t2"}

    # seal both tasks on disjoint files
    for tid, fname in (("t1", "a.py"), ("t2", "b.py")):
        wt = prepare_task(tmp_path, rid, tid)
        (wt / fname).write_text(f"{tid}\n", encoding="utf-8")
        env = seal_task(tmp_path, rid, tid, message=f"add {fname}")
        assert env["status"] == "ok", env

    joined2 = join_worker_results(tmp_path, rid)
    assert joined2["complete"] is True
    assert joined2["missing"] == []
    assert joined2["failed"] == []
    assert joined2["task_count"] == 2


def test_join_rejects_untrusted_writer(tmp_path):
    from omg_cli.workers import (
        build_ownership_manifest,
        envelope_path,
        join_worker_results,
    )

    base = _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="forge", extra={"base_sha": base})
    rid = run["run_id"]
    build_ownership_manifest(
        tmp_path,
        rid,
        [{"task_id": "only", "owned_files": ["z.py"]}],
    )
    epath = envelope_path(tmp_path, "only", run_id=rid)
    epath.parent.mkdir(parents=True, exist_ok=True)
    epath.write_text(
        json.dumps(
            {
                "task_id": "only",
                "status": "ok",
                "writer": "agent",
                "base_sha": base,
                "head_sha": "deadbeef",
            }
        ),
        encoding="utf-8",
    )
    joined = join_worker_results(tmp_path, rid)
    assert joined["complete"] is False
    assert "only" in joined["failed"]


def test_join_rejects_ownership_violation(tmp_path):
    from omg_cli.workers import (
        build_ownership_manifest,
        envelope_path,
        join_worker_results,
    )

    base = _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="own-v", extra={"base_sha": base})
    rid = run["run_id"]
    build_ownership_manifest(
        tmp_path,
        rid,
        [{"task_id": "only", "owned_files": ["z.py"]}],
    )
    epath = envelope_path(tmp_path, "only", run_id=rid)
    epath.parent.mkdir(parents=True, exist_ok=True)
    epath.write_text(
        json.dumps(
            {
                "task_id": "only",
                "status": "ok",
                "writer": "omg-cli",
                "base_sha": base,
                "head_sha": "c" * 40,
                "changed_files": ["z.py", "other/secret.py"],
            }
        ),
        encoding="utf-8",
    )
    joined = join_worker_results(tmp_path, rid)
    assert joined["complete"] is False
    assert "only" in joined["failed"]
    assert any(r.get("status") == "ownership_violation" for r in joined["results"])


def test_ownership_seal_join_integrate_closed_path(tmp_path):
    """I-06-style: own → seal both → join → integrate; missing blocks integrate."""
    from omg_cli.integrate import integrate_results, result_path
    from omg_cli.workers import (
        build_ownership_manifest,
        join_worker_results,
        prepare_task,
        seal_task,
    )

    base = _init_repo(tmp_path)
    run = create_run(
        tmp_path,
        mode="ulw",
        goal="integrate-path",
        extra={
            "base_sha": base,
            "schema_version": 2,
            "lifecycle_version": 2,
        },
    )
    rid = run["run_id"]
    build_ownership_manifest(
        tmp_path,
        rid,
        [
            {"task_id": "t1", "owned_files": ["a.py"]},
            {"task_id": "t2", "owned_files": ["b.py"]},
        ],
    )

    # Missing envelopes: join incomplete AND integrate not complete
    j0 = join_worker_results(tmp_path, rid)
    assert j0["complete"] is False
    integ_missing = integrate_results(tmp_path, rid, dry_run=True)
    assert integ_missing["status"] in {"missing", "failed"}
    assert integ_missing["status"] != "ok"

    for tid, fname in (("t1", "a.py"), ("t2", "b.py")):
        wt = prepare_task(tmp_path, rid, tid)
        (wt / fname).write_text(f"{tid} body\n", encoding="utf-8")
        env = seal_task(tmp_path, rid, tid, message=f"add {fname}")
        assert env["status"] == "ok", env

    j1 = join_worker_results(tmp_path, rid)
    assert j1["complete"] is True
    assert j1["task_count"] == 2

    # integrate_results acquires a short execution lease for strict-v2 status writes
    result = integrate_results(tmp_path, rid)
    assert result["status"] == "ok", result
    assert len(result.get("applied") or []) == 2
    disk = json.loads(result_path(tmp_path, rid).read_text(encoding="utf-8"))
    assert disk["status"] == "ok"
    assert disk["writer"] == "omg-cli"
