# tests/test_integrate.py
"""ULW integrator: clean-tree preflight, envelopes, cherry-pick (temp git repos)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omg_cli.integrate import (
    IntegrateError,
    default_envelopes_dir,
    git_rev_parse_head,
    integrate_results,
    load_envelopes,
    preflight_clean_tree,
    record_base_sha,
    result_path,
    validate_envelope,
)
from omg_cli.state import create_run, load_run

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


def _init_repo(path: Path, *, first_file: str = "README.md", content: str = "base\n") -> str:
    """Create a minimal git repo with one commit; return HEAD sha.

    Ignores ``.omg/`` so create_run / envelopes do not dirty the tree
    (matches real projects after ``omg setup`` gitignore merge).
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "omg-test@example.com")
    _git(path, "config", "user.name", "omg-test")
    # Avoid parent-repo pollution / template hooks noise
    _git(path, "config", "commit.gpgsign", "false")
    (path / ".gitignore").write_text(".omg/\n", encoding="utf-8")
    (path / first_file).write_text(content, encoding="utf-8")
    _git(path, "add", first_file, ".gitignore")
    _git(path, "commit", "-m", "initial")
    sha = _git(path, "rev-parse", "HEAD").stdout.strip()
    return sha


def _write_envelope(dir_path: Path, envelope: dict) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    task_id = envelope["task_id"]
    path = dir_path / f"{task_id}.json"
    path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    return path


def _run_omg(*args, cwd=None):
    cmd = [PYTHON, str(BIN_OMG), *args]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return subprocess.run(
        cmd,
        cwd=cwd or REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# validate_envelope / preflight / record_base_sha
# ---------------------------------------------------------------------------


def test_validate_envelope_ok():
    env = validate_envelope(
        {
            "task_id": "t1",
            "base_sha": "abc1234",
            "head_sha": "def5678",
            "worktree_path": "/tmp/wt",
            "status": "ok",
            "changed_files": ["a.py"],
            "evidence": "tests green",
        }
    )
    assert env["task_id"] == "t1"
    assert env["base_sha"] == "abc1234"
    assert env["status"] == "ok"
    assert env["changed_files"] == ["a.py"]


def test_validate_envelope_missing_keys():
    with pytest.raises(ValueError, match="missing keys"):
        validate_envelope({"task_id": "x"})


def test_validate_envelope_bad_status():
    with pytest.raises(ValueError, match="status"):
        validate_envelope(
            {
                "task_id": "t1",
                "base_sha": "abc1234",
                "head_sha": "def5678",
                "worktree_path": "/tmp/wt",
                "status": "maybe",
                "changed_files": [],
            }
        )


def test_validate_envelope_bad_sha():
    with pytest.raises(ValueError, match="head_sha"):
        validate_envelope(
            {
                "task_id": "t1",
                "base_sha": "abc1234",
                "head_sha": "not-a-sha!",
                "worktree_path": "/tmp/wt",
                "status": "ok",
                "changed_files": [],
            }
        )


def test_preflight_clean_tree_ok(tmp_path):
    _init_repo(tmp_path)
    preflight_clean_tree(tmp_path)  # must not raise


def test_preflight_clean_tree_dirty(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(IntegrateError, match="dirty"):
        preflight_clean_tree(tmp_path)


def test_preflight_not_a_repo(tmp_path):
    with pytest.raises(IntegrateError, match="git"):
        preflight_clean_tree(tmp_path)


def test_git_rev_parse_and_record_base_sha(tmp_path):
    head = _init_repo(tmp_path)
    assert git_rev_parse_head(tmp_path) == head
    run = create_run(tmp_path, mode="ulw", goal="record")
    sha = record_base_sha(tmp_path, run["run_id"])
    assert sha == head
    loaded = load_run(tmp_path, run["run_id"])
    assert loaded is not None
    assert loaded.get("base_sha") == head


# ---------------------------------------------------------------------------
# integrate_results with real git
# ---------------------------------------------------------------------------


def test_integrate_missing_envelopes(tmp_path):
    base = _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="none", extra={"base_sha": base})
    result = integrate_results(tmp_path, run["run_id"])
    assert result["status"] == "missing"
    assert result["writer"] == "omg-cli"
    assert result_path(tmp_path, run["run_id"]).is_file()
    assert "ulw-results" in (result.get("note") or "")


def test_integrate_cherry_pick_ok(tmp_path):
    """Worker worktree commit is cherry-picked onto the leader."""
    leader = tmp_path / "leader"
    base = _init_repo(leader)

    # Worktrees must live under project root or root/.omg/worktrees (path whitelist)
    wt = leader / ".omg" / "worktrees" / "worker-a"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(leader, "worktree", "add", str(wt), "HEAD")
    # Make a commit only on the worktree branch
    (wt / "feature_a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    _git(wt, "add", "feature_a.py")
    _git(wt, "commit", "-m", "worker a feature")
    head_sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

    # Leader still at base (worktree has its own branch tip)
    assert git_rev_parse_head(leader) == base
    preflight_clean_tree(leader)

    run = create_run(leader, mode="ulw", goal="pick", extra={"base_sha": base})
    env_dir = default_envelopes_dir(leader)
    _write_envelope(
        env_dir,
        {
            "task_id": "t-a",
            "base_sha": base,
            "head_sha": head_sha,
            "worktree_path": str(wt),
            "status": "ok",
            "changed_files": ["feature_a.py"],
            "evidence": "unit ok",
        },
    )

    result = integrate_results(leader, run["run_id"])
    assert result["status"] == "ok", result
    assert len(result["applied"]) == 1
    assert result["applied"][0]["status"] == "applied"
    # multi-commit range when base != head
    assert result["applied"][0].get("pick") == f"{base}..{head_sha}"
    assert (leader / "feature_a.py").is_file()
    assert "def a()" in (leader / "feature_a.py").read_text(encoding="utf-8")
    # HEAD advanced
    assert git_rev_parse_head(leader) != base


def test_integrate_sorts_by_task_id(tmp_path):
    leader = tmp_path / "leader"
    base = _init_repo(leader)

    # Two sequential commits on a side branch via one worktree
    wt = leader / ".omg" / "worktrees" / "wt"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(leader, "worktree", "add", "-b", "worker", str(wt), "HEAD")

    (wt / "b.txt").write_text("b\n", encoding="utf-8")
    _git(wt, "add", "b.txt")
    _git(wt, "commit", "-m", "add b")
    head_b = _git(wt, "rev-parse", "HEAD").stdout.strip()

    (wt / "a.txt").write_text("a\n", encoding="utf-8")
    _git(wt, "add", "a.txt")
    _git(wt, "commit", "-m", "add a")
    head_a = _git(wt, "rev-parse", "HEAD").stdout.strip()

    run = create_run(leader, mode="ulw", goal="order", extra={"base_sha": base})
    env_dir = default_envelopes_dir(leader)
    # Write out of order on disk; integrate sorts by task_id
    _write_envelope(
        env_dir,
        {
            "task_id": "task-b",
            "base_sha": base,
            "head_sha": head_b,
            "worktree_path": str(wt),
            "status": "ok",
            "changed_files": ["b.txt"],
        },
    )
    _write_envelope(
        env_dir,
        {
            "task_id": "task-a",
            "base_sha": base,
            "head_sha": head_a,
            "worktree_path": str(wt),
            "status": "ok",
            "changed_files": ["a.txt"],
        },
    )

    loaded = load_envelopes(env_dir)
    assert [e["task_id"] for e in loaded] == ["task-a", "task-b"]

    # task-a is the second commit (depends on b) — cherry-pick in task_id
    # order may conflict or apply empty; we only assert ordering of attempt
    result = integrate_results(leader, run["run_id"], dry_run=True)
    assert result["status"] == "ok"
    assert [a["task_id"] for a in result["applied"]] == ["task-a", "task-b"]
    assert all(a["status"] == "dry_run_ok" for a in result["applied"])


def test_integrate_base_sha_mismatch(tmp_path):
    leader = tmp_path / "leader"
    base = _init_repo(leader)
    wt = leader / ".omg" / "worktrees" / "wt"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(leader, "worktree", "add", str(wt), "HEAD")
    (wt / "x.py").write_text("x\n", encoding="utf-8")
    _git(wt, "add", "x.py")
    _git(wt, "commit", "-m", "x")
    head_sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

    run = create_run(
        leader, mode="ulw", goal="mismatch", extra={"base_sha": base}
    )
    _write_envelope(
        default_envelopes_dir(leader),
        {
            "task_id": "t1",
            "base_sha": "0" * 40,  # wrong base
            "head_sha": head_sha,
            "worktree_path": str(wt),
            "status": "ok",
            "changed_files": ["x.py"],
        },
    )
    result = integrate_results(leader, run["run_id"])
    assert result["status"] == "failed"
    assert result["failed_task"] == "t1"
    assert "base_sha" in (result.get("error") or "")


def test_integrate_failed_envelope_stops(tmp_path):
    leader = tmp_path / "leader"
    base = _init_repo(leader)
    run = create_run(leader, mode="ulw", goal="fail-env", extra={"base_sha": base})
    _write_envelope(
        default_envelopes_dir(leader),
        {
            "task_id": "t-fail",
            "base_sha": base,
            "head_sha": base,
            "worktree_path": str(leader),
            "status": "failed",
            "changed_files": [],
            "evidence": "worker crashed",
        },
    )
    result = integrate_results(leader, run["run_id"])
    assert result["status"] == "failed"
    assert result["failed_task"] == "t-fail"
    assert result["applied"][0]["status"] == "skipped_failed_envelope"


def test_integrate_conflict_marks_failed(tmp_path):
    """Same file edited differently → cherry-pick conflict → failed + abort."""
    leader = tmp_path / "leader"
    base = _init_repo(leader, first_file="shared.txt", content="line1\n")

    wt = leader / ".omg" / "worktrees" / "wt"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(leader, "worktree", "add", "-b", "conflict-branch", str(wt), "HEAD")
    (wt / "shared.txt").write_text("from-worker\n", encoding="utf-8")
    _git(wt, "add", "shared.txt")
    _git(wt, "commit", "-m", "worker edits shared")
    head_sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

    # Divergent edit on leader after base
    (leader / "shared.txt").write_text("from-leader\n", encoding="utf-8")
    _git(leader, "add", "shared.txt")
    _git(leader, "commit", "-m", "leader edits shared")
    # Update recorded base to current leader HEAD so base_sha check passes;
    # conflict is still expected because histories diverged on same lines.
    leader_head = git_rev_parse_head(leader)
    assert leader_head is not None

    run = create_run(
        leader, mode="ulw", goal="conflict", extra={"base_sha": base}
    )
    # Envelope still claims old base (protocol); force-match by using current
    # base_sha=base would fail base check after leader moved. Use base from
    # envelope equal to run base_sha; cherry-pick of sibling commit conflicts.
    # Re-create run with base_sha=base (original), but leader has moved —
    # envelope base_sha must match run. Worker was based on original base.
    # For conflict: run.base_sha = original base, envelope.base_sha = original.
    # Leader has extra commit; cherry-pick worker commit should conflict.
    _write_envelope(
        default_envelopes_dir(leader),
        {
            "task_id": "t-conflict",
            "base_sha": base,
            "head_sha": head_sha,
            "worktree_path": str(wt),
            "status": "ok",
            "changed_files": ["shared.txt"],
        },
    )

    result = integrate_results(leader, run["run_id"])
    assert result["status"] == "failed", result
    assert result["failed_task"] == "t-conflict"
    assert "cherry-pick" in (result.get("error") or "").lower() or "conflict" in (
        result.get("error") or ""
    ).lower()
    # First-envelope conflict: no prior applies → no partial_reset
    assert result.get("partial_reset") is False
    # Tree should not stay mid-cherry-pick
    st = _git(leader, "status", "--porcelain").stdout
    # After abort, only clean or at worst no CHERRY_PICK_HEAD
    assert not (leader / ".git" / "CHERRY_PICK_HEAD").exists() or "gitdir:" in (
        (leader / ".git").read_text(encoding="utf-8")
        if (leader / ".git").is_file()
        else ""
    )
    r = _git(leader, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD", check=False)
    assert r.returncode != 0  # no in-progress cherry-pick
    del st  # status may still show nothing interesting


def test_integrate_partial_reset_on_second_conflict(tmp_path):
    """HIGH: first cherry-pick ok, second conflicts → reset --hard to start_sha."""
    leader = tmp_path / "leader"
    base = _init_repo(leader, first_file="shared.txt", content="base-line\n")

    # Worker A: additive file (clean cherry-pick onto leader)
    wt_a = leader / ".omg" / "worktrees" / "wt-a"
    wt_a.parent.mkdir(parents=True, exist_ok=True)
    _git(leader, "worktree", "add", "-b", "worker-a", str(wt_a), "HEAD")
    (wt_a / "feature_a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    _git(wt_a, "add", "feature_a.py")
    _git(wt_a, "commit", "-m", "worker a")
    head_a = _git(wt_a, "rev-parse", "HEAD").stdout.strip()

    # Worker B: edits shared.txt from base (will conflict after leader also edits)
    wt_b = leader / ".omg" / "worktrees" / "wt-b"
    _git(leader, "worktree", "add", "-b", "worker-b", str(wt_b), "HEAD")
    (wt_b / "shared.txt").write_text("from-worker-b\n", encoding="utf-8")
    _git(wt_b, "add", "shared.txt")
    _git(wt_b, "commit", "-m", "worker b conflict")
    head_b = _git(wt_b, "rev-parse", "HEAD").stdout.strip()

    # Divergent leader edit so worker-b cherry-pick conflicts
    (leader / "shared.txt").write_text("from-leader\n", encoding="utf-8")
    _git(leader, "add", "shared.txt")
    _git(leader, "commit", "-m", "leader edits shared")
    start_sha = git_rev_parse_head(leader)
    assert start_sha is not None
    assert start_sha != base

    run = create_run(
        leader, mode="ulw", goal="partial", extra={"base_sha": base}
    )
    env_dir = default_envelopes_dir(leader)
    # task-a sorts before task-b → first applies, second conflicts
    _write_envelope(
        env_dir,
        {
            "task_id": "task-a",
            "base_sha": base,
            "head_sha": head_a,
            "worktree_path": str(wt_a),
            "status": "ok",
            "changed_files": ["feature_a.py"],
        },
    )
    _write_envelope(
        env_dir,
        {
            "task_id": "task-b",
            "base_sha": base,
            "head_sha": head_b,
            "worktree_path": str(wt_b),
            "status": "ok",
            "changed_files": ["shared.txt"],
        },
    )

    result = integrate_results(leader, run["run_id"])
    assert result["status"] == "failed", result
    assert result["failed_task"] == "task-b"
    assert result.get("partial_reset") is True, result
    assert result.get("reset_to") == start_sha
    assert result.get("start_sha") == start_sha
    # Applied list: first ok, second failed
    statuses = [a["status"] for a in result["applied"]]
    assert statuses == ["applied", "failed"]

    # Leader HEAD restored to pre-integrate start (feature_a gone)
    assert git_rev_parse_head(leader) == start_sha
    assert not (leader / "feature_a.py").exists()
    assert (leader / "shared.txt").read_text(encoding="utf-8") == "from-leader\n"

    disk = json.loads(result_path(leader, run["run_id"]).read_text(encoding="utf-8"))
    assert disk.get("partial_reset") is True
    assert disk.get("reset_to") == start_sha


def test_integrate_dirty_tree_refuses(tmp_path):
    leader = tmp_path / "leader"
    base = _init_repo(leader)
    (leader / "dirt").write_text("z\n", encoding="utf-8")
    run = create_run(leader, mode="ulw", goal="dirty", extra={"base_sha": base})
    with pytest.raises(IntegrateError, match="dirty"):
        integrate_results(leader, run["run_id"])


def test_integrate_dry_run_skips_preflight_and_pick(tmp_path):
    leader = tmp_path / "leader"
    base = _init_repo(leader)
    (leader / "dirt").write_text("z\n", encoding="utf-8")  # dirty ok for dry_run
    run = create_run(leader, mode="ulw", goal="dry", extra={"base_sha": base})
    _write_envelope(
        default_envelopes_dir(leader),
        {
            "task_id": "t1",
            "base_sha": base,
            "head_sha": base,
            "worktree_path": str(leader),
            "status": "ok",
            "changed_files": [],
        },
    )
    result = integrate_results(leader, run["run_id"], dry_run=True)
    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["applied"][0]["status"] == "dry_run_ok"


def test_ulw_create_run_records_base_sha(tmp_path):
    """modes.run_mode(ulw) stores base_sha when project is a git repo."""
    from omg_cli.modes import run_mode

    base = _init_repo(tmp_path)
    # dry_run: no grok Popen; git_rev_parse_head still uses subprocess.run
    rc = run_mode("ulw", "with git", root=tmp_path, dry_run=True)
    assert rc == 0
    active = json.loads(
        (tmp_path / ".omg" / "state" / "active.json").read_text(encoding="utf-8")
    )
    run = load_run(tmp_path, active["run_id"])
    assert run is not None
    assert run.get("base_sha") == base


def test_cli_integrate_dry_run(tmp_path):
    base = _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="cli", extra={"base_sha": base})
    rid = run["run_id"]
    _write_envelope(
        default_envelopes_dir(tmp_path),
        {
            "task_id": "cli-t",
            "base_sha": base,
            "head_sha": base,
            "worktree_path": str(tmp_path),
            "status": "ok",
            "changed_files": [],
        },
    )
    r = _run_omg("integrate", "--run", rid, "--dry-run", cwd=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "integrate result" in r.stdout.lower() or rid in r.stdout
    data = json.loads(result_path(tmp_path, rid).read_text(encoding="utf-8"))
    assert data["writer"] == "omg-cli"
    assert data["status"] == "ok"


def test_cli_integrate_help():
    r = _run_omg("integrate", "--help")
    assert r.returncode == 0
    assert "--run" in r.stdout
    assert "--dry-run" in r.stdout


def test_integrate_rejects_worktree_outside_allowlist(tmp_path):
    """worktree_path outside project root / .omg/worktrees → failed."""
    from omg_cli.integrate import worktree_path_allowed

    leader = tmp_path / "leader"
    base = _init_repo(leader)
    outside = tmp_path / "evil-wt"
    outside.mkdir()
    assert worktree_path_allowed(leader, outside) is False
    assert worktree_path_allowed(leader, leader) is True
    allowed = leader / ".omg" / "worktrees" / "ok"
    allowed.mkdir(parents=True)
    assert worktree_path_allowed(leader, allowed) is True

    run = create_run(leader, mode="ulw", goal="deny-path", extra={"base_sha": base})
    _write_envelope(
        default_envelopes_dir(leader),
        {
            "task_id": "t-evil",
            "base_sha": base,
            "head_sha": base,
            "worktree_path": str(outside),
            "status": "ok",
            "changed_files": [],
        },
    )
    result = integrate_results(leader, run["run_id"], dry_run=True)
    assert result["status"] == "failed"
    assert result["failed_task"] == "t-evil"
    assert result["applied"][0]["status"] == "worktree_path_denied"
    assert "allowlist" in (result.get("error") or "").lower()


def test_integrate_multi_commit_range(tmp_path):
    """When base_sha != head_sha, cherry-pick base..head (multiple commits)."""
    leader = tmp_path / "leader"
    base = _init_repo(leader)

    wt = leader / ".omg" / "worktrees" / "multi"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(leader, "worktree", "add", "-b", "multi-br", str(wt), "HEAD")

    (wt / "c1.txt").write_text("one\n", encoding="utf-8")
    _git(wt, "add", "c1.txt")
    _git(wt, "commit", "-m", "commit one")
    (wt / "c2.txt").write_text("two\n", encoding="utf-8")
    _git(wt, "add", "c2.txt")
    _git(wt, "commit", "-m", "commit two")
    head_sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
    assert head_sha != base

    run = create_run(leader, mode="ulw", goal="multi", extra={"base_sha": base})
    _write_envelope(
        default_envelopes_dir(leader),
        {
            "task_id": "t-multi",
            "base_sha": base,
            "head_sha": head_sha,
            "worktree_path": str(wt),
            "status": "ok",
            "changed_files": ["c1.txt", "c2.txt"],
        },
    )
    result = integrate_results(leader, run["run_id"])
    assert result["status"] == "ok", result
    assert result["applied"][0]["status"] == "applied"
    assert result["applied"][0]["pick"] == f"{base}..{head_sha}"
    assert (leader / "c1.txt").read_text(encoding="utf-8") == "one\n"
    assert (leader / "c2.txt").read_text(encoding="utf-8") == "two\n"
