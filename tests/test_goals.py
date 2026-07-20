"""U-07 / I-05 durable goal ledger tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omg_cli.evidence import sha256_file
from omg_cli.goals import (
    GENESIS_HASH,
    GoalError,
    GoalRepairRefused,
    block_story,
    checkpoint,
    complete_story,
    compute_event_hash,
    diagnose_repair,
    goal_status,
    import_proposal_event,
    init_goal,
    ledger_path,
    link_run,
    repair_goal,
    resume_story,
    snapshot_path,
    start_story,
    verify_goal,
)
from omg_cli.state import create_run, set_verified


REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_OMG = REPO_ROOT / "bin" / "omg"


def _stories() -> list[dict]:
    return [
        {
            "id": "s1",
            "title": "Story one",
            "depends_on": [],
            "acceptance": "s1 tests pass",
        },
        {
            "id": "s2",
            "title": "Story two",
            "depends_on": ["s1"],
            "acceptance": "s2 tests pass",
        },
        {
            "id": "s3",
            "title": "Story three",
            "depends_on": ["s2"],
            "acceptance": "s3 tests pass",
        },
    ]


def _evidence(root: Path, name: str = "ev.txt", body: str = "ok") -> Path:
    path = root / ".omg" / "artifacts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _run_omg(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return subprocess.run(
        [sys.executable, str(BIN_OMG), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def test_init_rejects_duplicate_cycle_unknown_and_missing_acceptance(tmp_path: Path) -> None:
    with pytest.raises(GoalError, match="duplicate"):
        init_goal(
            tmp_path,
            "g-dup",
            [
                {"id": "a", "depends_on": [], "acceptance": "x"},
                {"id": "a", "depends_on": [], "acceptance": "y"},
            ],
        )
    with pytest.raises(GoalError, match="unknown"):
        init_goal(
            tmp_path,
            "g-unk",
            [{"id": "a", "depends_on": ["missing"], "acceptance": "x"}],
        )
    with pytest.raises(GoalError, match="cycle"):
        init_goal(
            tmp_path,
            "g-cycle",
            [
                {"id": "a", "depends_on": ["b"], "acceptance": "x"},
                {"id": "b", "depends_on": ["a"], "acceptance": "y"},
            ],
        )
    with pytest.raises(GoalError, match="acceptance"):
        init_goal(tmp_path, "g-acc", [{"id": "a", "depends_on": []}])


def test_ready_in_progress_checkpoint_and_hash_chain(tmp_path: Path) -> None:
    st = init_goal(tmp_path, "g1", _stories(), title="Demo")
    assert st["ok"] is True
    assert st["stories"]["s1"]["status"] == "ready"
    assert st["stories"]["s2"]["status"] == "pending"
    assert st["tail_sequence"] == 1
    assert st["tail_hash"] != GENESIS_HASH

    with pytest.raises(GoalError, match="not ready"):
        start_story(tmp_path, "g1", "s2")

    start_story(tmp_path, "g1", "s1")
    ev = _evidence(tmp_path)
    checkpoint(tmp_path, "g1", "s1", evidence_path=ev, message="s1 done work")
    complete_story(tmp_path, "g1", "s1")

    st = goal_status(tmp_path, "g1")
    assert st["stories"]["s1"]["status"] == "complete"
    assert st["stories"]["s2"]["status"] == "ready"

    # recompute full chain
    lines = ledger_path(tmp_path, "g1").read_text(encoding="utf-8").splitlines()
    prev = GENESIS_HASH
    for i, line in enumerate(lines, start=1):
        obj = json.loads(line)
        assert obj["sequence"] == i
        assert obj["prev_hash"] == prev
        without = {k: v for k, v in obj.items() if k != "event_hash"}
        assert obj["event_hash"] == compute_event_hash(prev, without)
        prev = obj["event_hash"]
    snap = json.loads(snapshot_path(tmp_path, "g1").read_text(encoding="utf-8"))
    assert snap["tail_sequence"] == len(lines)
    assert snap["tail_hash"] == prev


def test_block_resume_and_multi_run_link(tmp_path: Path) -> None:
    init_goal(tmp_path, "g2", _stories())
    start_story(tmp_path, "g2", "s1")
    checkpoint(
        tmp_path, "g2", "s1", evidence_path=_evidence(tmp_path, "a.txt"), message="a"
    )
    complete_story(tmp_path, "g2", "s1")

    run_a = create_run(
        tmp_path,
        mode="ralph",
        goal="story1",
        force=True,
        extra={"schema_version": 2, "lifecycle_version": 2},
    )
    link_run(tmp_path, "g2", run_a["run_id"])

    start_story(tmp_path, "g2", "s2")
    block_story(
        tmp_path,
        "g2",
        "s2",
        reason="need more evidence",
        next_action="gather fixture",
    )
    st = goal_status(tmp_path, "g2")
    assert st["status"] == "blocked"
    assert st["stories"]["s2"]["status"] == "blocked"

    run_b = create_run(
        tmp_path,
        mode="ralph",
        goal="story2",
        force=True,
        extra={"schema_version": 2, "lifecycle_version": 2},
    )
    link_run(tmp_path, "g2", run_b["run_id"])
    resume_story(tmp_path, "g2", "s2")
    checkpoint(
        tmp_path, "g2", "s2", evidence_path=_evidence(tmp_path, "b.txt"), message="b"
    )
    complete_story(tmp_path, "g2", "s2")

    st = goal_status(tmp_path, "g2")
    assert st["stories"]["s2"]["status"] == "complete"
    assert run_a["run_id"] in st["linked_runs"]
    assert run_b["run_id"] in st["linked_runs"]
    # history survived process boundary (reload from disk)
    st2 = goal_status(tmp_path, "g2")
    assert st2["tail_sequence"] == st["tail_sequence"]
    assert st2["event_count"] == st["event_count"]


def test_verify_requires_cli_verified_linked_run(tmp_path: Path) -> None:
    init_goal(
        tmp_path,
        "g3",
        [
            {
                "id": "only",
                "depends_on": [],
                "acceptance": "done",
            }
        ],
    )
    start_story(tmp_path, "g3", "only")
    checkpoint(
        tmp_path,
        "g3",
        "only",
        evidence_path=_evidence(tmp_path),
        message="done",
    )
    complete_story(tmp_path, "g3", "only")

    with pytest.raises(GoalError, match="without a linked run"):
        verify_goal(tmp_path, "g3")

    run = create_run(
        tmp_path,
        mode="pipeline",
        goal="g3",
        force=True,
    )
    link_run(tmp_path, "g3", run["run_id"])
    with pytest.raises(GoalError, match="before a linked run is CLI-verified"):
        verify_goal(tmp_path, "g3")

    # Disk-only verified status must not promote the goal
    set_verified(tmp_path, run["run_id"], force=True)
    with pytest.raises(GoalError, match="trusted acceptance"):
        verify_goal(tmp_path, "g3")

    # Same-process freeze_and_run + set_verified is required
    from omg_cli.acceptance import clear_cli_acceptance_tokens, freeze_and_run

    clear_cli_acceptance_tokens()
    prd = {
        "version": 1,
        "goal": "g3",
        "stories": [{"id": "s1", "title": "ok", "commands": [["true"]]}],
        "global_commands": [],
    }
    assert freeze_and_run(tmp_path, run["run_id"], prd) is True
    set_verified(tmp_path, run["run_id"])
    st = verify_goal(tmp_path, "g3")
    assert st["verified"] is True
    assert st["status"] == "verified"


def test_direct_file_write_is_not_authoritative(tmp_path: Path) -> None:
    init_goal(
        tmp_path,
        "g4",
        [{"id": "s", "depends_on": [], "acceptance": "x"}],
    )
    # agent forges snapshot
    snap_path = snapshot_path(tmp_path, "g4")
    forged = json.loads(snap_path.read_text(encoding="utf-8"))
    forged["verified"] = True
    forged["status"] = "verified"
    forged["writer"] = "agent"
    snap_path.write_text(json.dumps(forged), encoding="utf-8")
    st = goal_status(tmp_path, "g4")
    assert st["ok"] is False
    assert "writer" in (st.get("error") or "").lower() or st.get("error")

    # restore for further tests on proposal import path
    init_goal(
        tmp_path,
        "g4b",
        [{"id": "s", "depends_on": [], "acceptance": "x"}],
    )
    prop_dir = tmp_path / ".omg" / "artifacts" / "proposals" / "run1" / "inv1"
    prop_dir.mkdir(parents=True)
    prop = prop_dir / "note.json"
    prop.write_text('{"note":"proposal only"}\n', encoding="utf-8")
    digest = sha256_file(prop)
    import_proposal_event(
        tmp_path, "g4b", proposal_path=prop, proposal_sha256=digest, note="import"
    )
    st = goal_status(tmp_path, "g4b")
    assert st["ok"] is True
    assert st["tail_sequence"] >= 2

    # wrong hash refused
    with pytest.raises(GoalError, match="sha256"):
        import_proposal_event(
            tmp_path,
            "g4b",
            proposal_path=prop,
            proposal_sha256="0" * 64,
        )


def test_corrupt_tail_blocks_mutation_dry_run_and_repair(tmp_path: Path) -> None:
    init_goal(
        tmp_path,
        "g5",
        [{"id": "s", "depends_on": [], "acceptance": "x"}],
    )
    start_story(tmp_path, "g5", "s")
    checkpoint(
        tmp_path, "g5", "s", evidence_path=_evidence(tmp_path), message="c1"
    )
    path = ledger_path(tmp_path, "g5")
    original = path.read_bytes()
    # truncate final line
    path.write_bytes(original[:-12] + b"\n")

    with pytest.raises(GoalError, match="not appendable"):
        checkpoint(
            tmp_path,
            "g5",
            "s",
            evidence_path=_evidence(tmp_path, "x.txt"),
            message="should fail",
        )

    dry = repair_goal(tmp_path, "g5", dry_run=True)
    assert dry["ok"] is False
    assert dry["eligible_for_tail_repair"] is True
    assert dry["action"] == "dry_run"
    # dry-run does not mutate
    assert path.read_bytes() != original  # still truncated
    after_dry = path.read_bytes()

    repaired = repair_goal(tmp_path, "g5", dry_run=False, yes=True)
    assert repaired["ok"] is True
    assert repaired["action"] == "repaired"
    assert "backups/" in repaired["backup_path"]
    backup = tmp_path / repaired["backup_path"]
    assert backup.is_file()
    assert sha256_file(backup) == repaired["original_sha256"]

    st = goal_status(tmp_path, "g5")
    assert st["ok"] is True
    assert st["ledger_healthy"] is True
    # resume append works
    checkpoint(
        tmp_path,
        "g5",
        "s",
        evidence_path=_evidence(tmp_path, "after.txt"),
        message="after repair",
    )
    st2 = goal_status(tmp_path, "g5")
    assert st2["tail_sequence"] == st["tail_sequence"] + 1


def test_mid_chain_corruption_refuses_repair(tmp_path: Path) -> None:
    init_goal(
        tmp_path,
        "g6",
        [{"id": "s", "depends_on": [], "acceptance": "x"}],
    )
    start_story(tmp_path, "g6", "s")
    checkpoint(
        tmp_path, "g6", "s", evidence_path=_evidence(tmp_path), message="c1"
    )
    complete_story(tmp_path, "g6", "s")
    path = ledger_path(tmp_path, "g6")
    lines = path.read_bytes().splitlines(keepends=True)
    assert len(lines) >= 3
    # mutate a middle line body but keep as JSON-ish by flipping event_hash
    mid = json.loads(lines[1])
    mid["event_hash"] = "a" * 64
    lines[1] = (
        json.dumps(mid, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    corrupted = b"".join(lines)
    path.write_bytes(corrupted)

    diag = diagnose_repair(tmp_path, "g6")
    assert diag["ok"] is False
    assert diag["eligible_for_tail_repair"] is False

    with pytest.raises(GoalRepairRefused):
        repair_goal(tmp_path, "g6", dry_run=False, yes=True)

    # active ledger unchanged after refused repair (except possible forensic snapshot)
    assert path.read_bytes() == corrupted


def test_cli_goal_init_status_and_repair(tmp_path: Path) -> None:
    stories = json.dumps(
        [{"id": "s1", "depends_on": [], "acceptance": "pass"}],
        ensure_ascii=False,
    )
    proc = _run_omg(
        "goal",
        "init",
        "--goal",
        "cli-g1",
        "--stories-json",
        stories,
        "--title",
        "CLI goal",
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["goal_id"] == "cli-g1"
    assert data["ok"] is True

    proc = _run_omg("goal", "status", "--goal", "cli-g1", cwd=tmp_path)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["tail_sequence"] == 1

    # break tail then dry-run via CLI
    path = ledger_path(tmp_path, "cli-g1")
    path.write_bytes(path.read_bytes() + b"{not-json")
    proc = _run_omg("goal", "repair", "--goal", "cli-g1", "--dry-run", cwd=tmp_path)
    assert proc.returncode == 0
    dry = json.loads(proc.stdout)
    assert dry["eligible_for_tail_repair"] is True

    proc = _run_omg("goal", "repair", "--goal", "cli-g1", "--yes", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    repaired = json.loads(proc.stdout)
    assert repaired["action"] == "repaired"
