"""Tests for omg worker prepare/seal — worktree + ULW envelope."""
from __future__ import annotations

import json
import os
import re
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


def test_ownership_dotfile_not_collapsed(tmp_path):
    """`.lstrip("./")` wrongly maps ".config" -> "config"; dotfiles must stay distinct.

    Owning ["a.py"] + changing [".config"] is foreign (ownership_violation).
    Owning [".config"] + changing [".config"] is OK (not foreign).
    """
    from omg_cli.workers import (
        build_ownership_manifest,
        envelope_path,
        join_worker_results,
    )

    base = _init_repo(tmp_path)

    # Case 1: owned "a.py" must NOT accept changed ".config" as owned
    run1 = create_run(tmp_path, mode="ulw", goal="dot-foreign", extra={"base_sha": base})
    rid1 = run1["run_id"]
    build_ownership_manifest(
        tmp_path,
        rid1,
        [{"task_id": "t-foreign", "owned_files": ["a.py"]}],
    )
    epath1 = envelope_path(tmp_path, "t-foreign", run_id=rid1)
    epath1.parent.mkdir(parents=True, exist_ok=True)
    epath1.write_text(
        json.dumps(
            {
                "task_id": "t-foreign",
                "status": "ok",
                "writer": "omg-cli",
                "base_sha": base,
                "head_sha": "c" * 40,
                "changed_files": [".config"],
            }
        ),
        encoding="utf-8",
    )
    joined1 = join_worker_results(tmp_path, rid1)
    assert joined1["complete"] is False
    assert "t-foreign" in joined1["failed"]
    viol = [r for r in joined1["results"] if r.get("status") == "ownership_violation"]
    assert viol, joined1["results"]
    assert ".config" in (viol[0].get("foreign_files") or [])

    # Case 2: owning ".config" + changing ".config" is legitimate
    # force=True: same tmp_path still has run1 as active non-terminal.
    run2 = create_run(
        tmp_path, mode="ulw", goal="dot-ok", extra={"base_sha": base}, force=True
    )
    rid2 = run2["run_id"]
    build_ownership_manifest(
        tmp_path,
        rid2,
        [{"task_id": "t-ok", "owned_files": [".config"]}],
    )
    epath2 = envelope_path(tmp_path, "t-ok", run_id=rid2)
    epath2.parent.mkdir(parents=True, exist_ok=True)
    epath2.write_text(
        json.dumps(
            {
                "task_id": "t-ok",
                "status": "ok",
                "writer": "omg-cli",
                "base_sha": base,
                "head_sha": "d" * 40,
                "changed_files": [".config"],
            }
        ),
        encoding="utf-8",
    )
    joined2 = join_worker_results(tmp_path, rid2)
    assert joined2["complete"] is True
    assert joined2["failed"] == []
    assert not any(r.get("status") == "ownership_violation" for r in joined2["results"])


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

    # Missing envelopes: join incomplete; non-dry integrate must raise
    j0 = join_worker_results(tmp_path, rid)
    assert j0["complete"] is False
    integ_missing = integrate_results(tmp_path, rid, dry_run=True)
    assert integ_missing["status"] in {"missing", "failed"}
    assert integ_missing["status"] != "ok"
    from omg_cli.integrate import IntegrateError

    with pytest.raises(IntegrateError, match="ownership join incomplete"):
        integrate_results(tmp_path, rid)

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


def test_join_empty_owned_files_fails_closed(tmp_path):
    """A manifest task with empty owned_files must fail closed: any changed
    file counts as foreign, so the ownership guard cannot be silently disabled
    by a malformed/hand-edited on-disk manifest."""
    from omg_cli.workers import (
        build_ownership_manifest,
        envelope_path,
        join_worker_results,
        ownership_manifest_path,
    )

    base = _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="empty-own", extra={"base_sha": base})
    rid = run["run_id"]
    build_ownership_manifest(
        tmp_path,
        rid,
        [{"task_id": "only", "owned_files": ["z.py"]}],
    )
    # Tamper on disk: blank out owned_files (build_ownership_manifest would
    # reject this, but load_ownership_manifest does not re-validate it).
    mpath = ownership_manifest_path(tmp_path, rid)
    data = json.loads(mpath.read_text(encoding="utf-8"))
    data["tasks"][0]["owned_files"] = []
    mpath.write_text(json.dumps(data), encoding="utf-8")

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
                "changed_files": ["anything.py"],
            }
        ),
        encoding="utf-8",
    )
    joined = join_worker_results(tmp_path, rid)
    assert joined["complete"] is False
    assert any(r.get("status") == "ownership_violation" for r in joined["results"])


def test_seal_all_tasks_seals_both_and_join_succeeds(tmp_path):
    """seal_all_tasks seals every prepared worktree; join then succeeds."""
    from omg_cli.workers import (
        build_ownership_manifest,
        envelope_path,
        join_worker_results,
        prepare_task,
        seal_all_tasks,
    )

    base = _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="seal-all", extra={"base_sha": base})
    rid = run["run_id"]
    build_ownership_manifest(
        tmp_path,
        rid,
        [
            {"task_id": "a", "owned_files": ["a.py"]},
            {"task_id": "b", "owned_files": ["b.py"]},
        ],
    )
    for tid, fname in (("a", "a.py"), ("b", "b.py")):
        wt = prepare_task(tmp_path, rid, tid)
        (wt / fname).write_text(f"{tid} body\n", encoding="utf-8")
        # Real commit in worktree (leader seal_all reuses seal_task which
        # also commits dirty trees; pre-commit keeps head_sha real either way).
        _git(wt, "add", fname)
        _git(wt, "commit", "-m", f"add {fname}")

    results = seal_all_tasks(tmp_path, rid)
    assert [r["task_id"] for r in results] == ["a", "b"]
    assert all(r["status"] == "sealed" for r in results), results
    for r in results:
        assert re.fullmatch(r"[0-9a-f]{40}", r["head_sha"]), r
        assert r["changed_files_count"] >= 1
        assert envelope_path(tmp_path, r["task_id"], run_id=rid).is_file()

    joined = join_worker_results(tmp_path, rid)
    assert joined["complete"] is True
    assert joined["missing"] == []
    assert joined["failed"] == []


def test_seal_all_skipped_no_worktree_and_already_sealed(tmp_path):
    """Missing worktree is skipped (not fabricated); existing envelope is already-sealed."""
    from omg_cli.workers import (
        build_ownership_manifest,
        envelope_path,
        prepare_task,
        seal_all_tasks,
        seal_task,
        worktree_dir,
    )

    base = _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="seal-skip", extra={"base_sha": base})
    rid = run["run_id"]
    build_ownership_manifest(
        tmp_path,
        rid,
        [
            {"task_id": "a", "owned_files": ["a.py"]},
            {"task_id": "b", "owned_files": ["b.py"]},
        ],
    )
    # Prepare + seal only task a
    wt_a = prepare_task(tmp_path, rid, "a")
    (wt_a / "a.py").write_text("a\n", encoding="utf-8")
    env = seal_task(tmp_path, rid, "a", message="add a")
    assert env["status"] == "ok"
    assert envelope_path(tmp_path, "a", run_id=rid).is_file()
    # Task b has no worktree
    assert not worktree_dir(tmp_path, rid, "b").is_dir()

    results = seal_all_tasks(tmp_path, rid)
    by_id = {r["task_id"]: r for r in results}
    assert by_id["a"]["status"] == "already-sealed"
    assert by_id["b"]["status"] == "skipped-no-worktree"
    # Must not fabricate envelope for absent worktree
    assert not envelope_path(tmp_path, "b", run_id=rid).is_file()


def test_seal_all_only_touches_run_worktrees(tmp_path):
    """seal_all only seals worktrees under .omg/worktrees/<run_id>/."""
    from omg_cli.workers import (
        build_ownership_manifest,
        envelope_path,
        prepare_task,
        seal_all_tasks,
        worktree_dir,
    )

    base = _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="seal-scope", extra={"base_sha": base})
    rid = run["run_id"]
    build_ownership_manifest(
        tmp_path,
        rid,
        [
            {"task_id": "present", "owned_files": ["p.py"]},
            {"task_id": "absent", "owned_files": ["q.py"]},
        ],
    )
    wt = prepare_task(tmp_path, rid, "present")
    (wt / "p.py").write_text("p\n", encoding="utf-8")
    _git(wt, "add", "p.py")
    _git(wt, "commit", "-m", "add p")

    # External path that must not be treated as this run's worktree
    foreign = tmp_path / "foreign-wt" / "absent"
    foreign.mkdir(parents=True)
    (foreign / "q.py").write_text("foreign\n", encoding="utf-8")
    assert not worktree_dir(tmp_path, rid, "absent").is_dir()

    results = seal_all_tasks(tmp_path, rid)
    by_id = {r["task_id"]: r for r in results}
    assert by_id["present"]["status"] == "sealed"
    assert by_id["absent"]["status"] == "skipped-no-worktree"
    assert envelope_path(tmp_path, "present", run_id=rid).is_file()
    assert not envelope_path(tmp_path, "absent", run_id=rid).is_file()


def test_cli_worker_seal_all(tmp_path):
    """omg worker seal --all returns 0 and prints a per-task table."""
    from omg_cli.workers import build_ownership_manifest, prepare_task

    base = _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="cli-seal-all", extra={"base_sha": base})
    rid = run["run_id"]
    build_ownership_manifest(
        tmp_path,
        rid,
        [
            {"task_id": "a", "owned_files": ["a.py"]},
            {"task_id": "b", "owned_files": ["b.py"]},
        ],
    )
    for tid, fname in (("a", "a.py"), ("b", "b.py")):
        wt = prepare_task(tmp_path, rid, tid)
        (wt / fname).write_text(f"{tid}\n", encoding="utf-8")
        _git(wt, "add", fname)
        _git(wt, "commit", "-m", f"add {fname}")

    r = _run_omg("worker", "seal", "--all", "--run", rid, cwd=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    out = r.stdout
    assert "a" in out and "b" in out
    assert "sealed" in out
    assert "sealed 2" in out or "sealed 2," in out
    assert "failed 0" in out
    assert "error 0" in out


def test_seal_all_head_eq_base_reports_failed_not_sealed(tmp_path):
    """Prepared worktree with no commit (head==base) must be failed, not sealed.

    seal_task returns status=='failed' envelope without raising; seal_all must
    surface that as result status 'failed' (not mask as 'sealed').
    """
    from omg_cli.workers import (
        build_ownership_manifest,
        envelope_path,
        prepare_task,
        seal_all_tasks,
    )

    base = _init_repo(tmp_path)
    run = create_run(
        tmp_path, mode="ulw", goal="seal-head-base", extra={"base_sha": base}
    )
    rid = run["run_id"]
    build_ownership_manifest(
        tmp_path,
        rid,
        [{"task_id": "clean", "owned_files": ["clean.py"]}],
    )
    prepare_task(tmp_path, rid, "clean")
    # No edits / no commit → head == base

    results = seal_all_tasks(tmp_path, rid)
    assert len(results) == 1
    row = results[0]
    assert row["task_id"] == "clean"
    assert row["status"] == "failed", (
        f"expected failed for head==base, got {row!r} "
        "(masking envelope failed as sealed is the bug)"
    )
    assert row.get("envelope_status") == "failed"
    # Envelope is still written (failed seal is an envelope, not a skip)
    assert envelope_path(tmp_path, "clean", run_id=rid).is_file()


def test_cli_worker_seal_all_head_eq_base_nonzero(tmp_path):
    """omg worker seal --all must return nonzero when any task seal failed."""
    from omg_cli.workers import build_ownership_manifest, prepare_task

    base = _init_repo(tmp_path)
    run = create_run(
        tmp_path, mode="ulw", goal="cli-seal-fail", extra={"base_sha": base}
    )
    rid = run["run_id"]
    build_ownership_manifest(
        tmp_path,
        rid,
        [{"task_id": "clean", "owned_files": ["clean.py"]}],
    )
    prepare_task(tmp_path, rid, "clean")

    r = _run_omg("worker", "seal", "--all", "--run", rid, cwd=tmp_path)
    assert r.returncode != 0, (
        f"expected nonzero when head==base failed seal; got 0\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert "failed" in r.stdout
    assert "failed 1" in r.stdout or "failed 1," in r.stdout


def test_cli_worker_seal_all_missing_only_returns_zero(tmp_path):
    """Missing-worktree-only batch is benign: CLI returns 0."""
    from omg_cli.workers import build_ownership_manifest

    base = _init_repo(tmp_path)
    run = create_run(
        tmp_path, mode="ulw", goal="cli-seal-skip", extra={"base_sha": base}
    )
    rid = run["run_id"]
    build_ownership_manifest(
        tmp_path,
        rid,
        [
            {"task_id": "a", "owned_files": ["a.py"]},
            {"task_id": "b", "owned_files": ["b.py"]},
        ],
    )
    # No prepare → both skipped-no-worktree

    r = _run_omg("worker", "seal", "--all", "--run", rid, cwd=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "skipped" in r.stdout
    assert "failed 0" in r.stdout
    assert "error 0" in r.stdout


def test_seal_all_non_worktree_missing_worker_error_is_error(tmp_path):
    """WorkerError that is not 'worktree missing' must be status=error, not skip.

    Delete run status.json after prepare so seal_task raises a non-missing error.
    """
    from omg_cli.workers import (
        build_ownership_manifest,
        prepare_task,
        seal_all_tasks,
    )

    base = _init_repo(tmp_path)
    run = create_run(
        tmp_path, mode="ulw", goal="seal-err", extra={"base_sha": base}
    )
    rid = run["run_id"]
    build_ownership_manifest(
        tmp_path,
        rid,
        [{"task_id": "a", "owned_files": ["a.py"]}],
    )
    wt = prepare_task(tmp_path, rid, "a")
    (wt / "a.py").write_text("a\n", encoding="utf-8")
    _git(wt, "add", "a.py")
    _git(wt, "commit", "-m", "add a")

    status_path = tmp_path / ".omg" / "state" / "runs" / rid / "status.json"
    assert status_path.is_file()
    status_path.unlink()

    results = seal_all_tasks(tmp_path, rid)
    assert len(results) == 1
    row = results[0]
    assert row["status"] == "error", row
    assert "worktree missing" not in str(row.get("error") or "").lower()
    assert row["status"] != "skipped-no-worktree"


def test_seal_all_force_reseals_advanced_worktree(tmp_path):
    """Without --force, existing envelope is already-sealed; with force, re-seal."""
    from omg_cli.workers import (
        build_ownership_manifest,
        envelope_path,
        prepare_task,
        seal_all_tasks,
        seal_task,
    )

    base = _init_repo(tmp_path)
    run = create_run(
        tmp_path, mode="ulw", goal="seal-force", extra={"base_sha": base}
    )
    rid = run["run_id"]
    build_ownership_manifest(
        tmp_path,
        rid,
        [{"task_id": "a", "owned_files": ["a.py"]}],
    )
    wt = prepare_task(tmp_path, rid, "a")
    (wt / "a.py").write_text("v1\n", encoding="utf-8")
    env1 = seal_task(tmp_path, rid, "a", message="v1")
    assert env1["status"] == "ok"
    head1 = env1["head_sha"]
    assert envelope_path(tmp_path, "a", run_id=rid).is_file()

    # Post-seal commit advances worktree head
    (wt / "a.py").write_text("v2\n", encoding="utf-8")
    _git(wt, "add", "a.py")
    _git(wt, "commit", "-m", "v2")
    head2 = _git(wt, "rev-parse", "HEAD").stdout.strip().lower()
    assert head2 != head1

    # Default: already-sealed (does not pick up post-seal commits)
    results = seal_all_tasks(tmp_path, rid)
    assert results[0]["status"] == "already-sealed"
    env_on_disk = json.loads(
        envelope_path(tmp_path, "a", run_id=rid).read_text(encoding="utf-8")
    )
    assert env_on_disk["head_sha"] == head1

    # force=True: re-seal picks up advanced head
    results_f = seal_all_tasks(tmp_path, rid, force=True)
    assert results_f[0]["status"] == "sealed", results_f
    assert results_f[0]["head_sha"] == head2
    env_on_disk2 = json.loads(
        envelope_path(tmp_path, "a", run_id=rid).read_text(encoding="utf-8")
    )
    assert env_on_disk2["head_sha"] == head2
    assert env_on_disk2["status"] == "ok"

    # CLI --force also re-seals (third advance)
    (wt / "a.py").write_text("v3\n", encoding="utf-8")
    _git(wt, "add", "a.py")
    _git(wt, "commit", "-m", "v3")
    head3 = _git(wt, "rev-parse", "HEAD").stdout.strip().lower()
    r = _run_omg("worker", "seal", "--all", "--force", "--run", rid, cwd=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    env_on_disk3 = json.loads(
        envelope_path(tmp_path, "a", run_id=rid).read_text(encoding="utf-8")
    )
    assert env_on_disk3["head_sha"] == head3
