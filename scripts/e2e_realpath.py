#!/usr/bin/env python3
"""Real-path e2e for oh-my-grok (temp git project). Exit 0 only if all gates pass."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def main() -> int:
    from omg_cli.state import create_run, set_verified, load_run, ensure_omg_dirs
    from omg_cli.workers import prepare_task, seal_task
    from omg_cli.integrate import integrate_results, default_envelopes_dir
    from omg_cli.acceptance import freeze_and_run, freeze_acceptance, is_trusted_acceptance
    from omg_cli.command_policy import check_command_policy, CommandPolicyError
    from omg_cli.pipeline import run_pipeline, report_path, load_pipeline_state

    root = Path(tempfile.mkdtemp(prefix="omg-e2e-"))
    print("e2e root", root)
    git(root, "init", "-q")
    git(root, "config", "user.email", "e2e@omg.test")
    git(root, "config", "user.name", "omg-e2e")
    git(root, "config", "commit.gpgsign", "false")
    (root / "app.txt").write_text("hello\n", encoding="utf-8")
    (root / ".gitignore").write_text(".omg/\n", encoding="utf-8")
    git(root, "add", "app.txt", ".gitignore")
    git(root, "commit", "-qm", "init")
    base = git(root, "rev-parse", "HEAD").stdout.strip()

    # 1) policy matrix
    def allow(cmd):
        try:
            check_command_policy(cmd, project_root=root)
            return True
        except CommandPolicyError:
            return False

    assert allow(["true"])
    assert allow(["pytest", "-q"])
    assert allow(["python3", "-m", "pytest", "-q"])
    assert not allow(["python3", "-c", "print(1)"])
    assert not allow(["python3", "-cimport os"])
    assert not allow(["npx", "x"])
    print("PASS policy")

    # 2) prepare/seal/integrate/accept
    run = create_run(root, mode="ulw", goal="e2e", extra={"base_sha": base})
    rid = run["run_id"]
    wt = prepare_task(root, rid, "task-e2e")
    (wt / "feature.txt").write_text("feature\n", encoding="utf-8")
    env = seal_task(root, rid, "task-e2e", message="add feature")
    assert env["status"] == "ok", env
    assert env["head_sha"] != env["base_sha"]
    res = integrate_results(root, rid, dry_run=False)
    assert res["status"] == "ok", res
    assert (root / "feature.txt").is_file()
    prd = {
        "version": 1,
        "goal": "e2e",
        "stories": [{"id": "s1", "title": "t", "commands": [["true"]]}],
    }
    assert freeze_and_run(root, rid, prd, dry_run=False)
    set_verified(root, rid)
    assert load_run(root, rid)["verified"] is True
    print("PASS seal+integrate+accept")

    # 3) multi-commit + require_squash
    base2 = git(root, "rev-parse", "HEAD").stdout.strip()
    run2 = create_run(
        root, mode="ulw", goal="multi", extra={"base_sha": base2}, force=True
    )
    rid2 = run2["run_id"]
    wt2 = prepare_task(root, rid2, "multi")
    (wt2 / "m1.txt").write_text("1\n", encoding="utf-8")
    git(wt2, "add", "m1.txt")
    git(wt2, "commit", "-qm", "c1")
    (wt2 / "m2.txt").write_text("2\n", encoding="utf-8")
    git(wt2, "add", "m2.txt")
    git(wt2, "commit", "-qm", "c2")
    head2 = git(wt2, "rev-parse", "HEAD").stdout.strip()
    files = [
        ln
        for ln in git(wt2, "diff", "--name-only", base2, head2).stdout.splitlines()
        if ln.strip()
    ]
    env_dir = default_envelopes_dir(root)
    # Clear prior envelopes so integrate only sees this multi task
    if env_dir.is_dir():
        for old in env_dir.glob("*.json"):
            old.unlink()
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "multi.json").write_text(
        json.dumps(
            {
                "task_id": "multi",
                "base_sha": base2,
                "head_sha": head2,
                "worktree_path": str(wt2),
                "status": "ok",
                "changed_files": files,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    bad = integrate_results(root, rid2, dry_run=True, require_squash=True)
    assert bad["status"] == "failed", bad
    good = integrate_results(root, rid2, dry_run=False, require_squash=False)
    assert good["status"] == "ok", good
    assert (root / "m1.txt").is_file() and (root / "m2.txt").is_file()
    print("PASS multi-commit integrate + require_squash")

    # 4) forge denied
    run3 = create_run(root, mode="ralph", goal="forge", force=True)
    rid3 = run3["run_id"]
    freeze_acceptance(root, rid3, prd)
    sha = (root / ".omg" / "state" / "runs" / rid3 / "acceptance.sha256").read_text().strip()
    (
        root / ".omg" / "state" / "runs" / rid3 / "acceptance.result.json"
    ).write_text(
        json.dumps(
            {
                "writer": "omg-cli",
                "passed": True,
                "manifest_sha256": sha,
                "results": [],
            }
        ),
        encoding="utf-8",
    )
    assert is_trusted_acceptance(root, rid3) is False
    try:
        set_verified(root, rid3)
        raise AssertionError("forge must not set_verified")
    except PermissionError:
        print("PASS forge denied")

    # 5) pipeline dry + reintegrate unit already in pytest; dry pipeline CLI-like
    rc = run_pipeline(
        "dry",
        root=root,
        dry_run=True,
        skip_plan=True,
        dual_review=False,
        require_acceptance=False,
        implement="ralph",
        force=True,
    )
    assert rc == 0
    # find latest pipeline run
    from omg_cli.state import load_active_run

    active = load_active_run(root)
    assert active is not None
    assert report_path(root, active["run_id"]).is_file()
    print("PASS pipeline dry + report")

    # 6) fanout gate
    env = os.environ.copy()
    env.pop("OMG_EXPERIMENTAL_PROCESS_FANOUT", None)
    env["PYTHONPATH"] = str(REPO)
    r = subprocess.run(
        [sys.executable, str(REPO / "bin" / "omg"), "ulw", "x", "--fanout", "process", "--workers", "2", "--dry-run"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2, (r.returncode, r.stderr)
    env["OMG_EXPERIMENTAL_PROCESS_FANOUT"] = "1"
    r2 = subprocess.run(
        [sys.executable, str(REPO / "bin" / "omg"), "ulw", "x", "--fanout", "process", "--workers", "2", "--dry-run"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r2.returncode == 0, r2.stderr
    print("PASS fanout gate")

    # 7) CLI accept / deny
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO)
    run4 = create_run(root, mode="ralph", goal="cli", force=True)
    rid4 = run4["run_id"]
    (root / ".omg" / "state" / "runs" / rid4 / "prd.json").write_text(
        json.dumps(prd), encoding="utf-8"
    )
    r = subprocess.run(
        [sys.executable, str(REPO / "bin" / "omg"), "accept", "--run", rid4, "--yes"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert load_run(root, rid4)["verified"] is True
    run5 = create_run(root, mode="ralph", goal="deny", force=True)
    rid5 = run5["run_id"]
    bad_prd = {
        "version": 1,
        "goal": "g",
        "stories": [{"id": "s1", "title": "t", "commands": [["python3", "-c", "print(1)"]]}],
    }
    (root / ".omg" / "state" / "runs" / rid5 / "prd.json").write_text(
        json.dumps(bad_prd), encoding="utf-8"
    )
    r = subprocess.run(
        [sys.executable, str(REPO / "bin" / "omg"), "accept", "--run", rid5, "--yes"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    assert load_run(root, rid5).get("verified") is not True
    print("PASS cli accept/deny")

    print("ALL_REAL_E2E_OK", root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
