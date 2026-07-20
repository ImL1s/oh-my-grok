"""U-10 UltraQA bounded repair FSM."""
from __future__ import annotations

from pathlib import Path

import pytest

from omg_cli.qa import QAError, freeze_scenarios, load_qa, run_qa_cycle, _save
from omg_cli.state import create_run


def test_freeze_and_clean_never_verified(tmp_path: Path) -> None:
    run = create_run(tmp_path, mode="qa", goal="q")
    rid = run["run_id"]
    state = freeze_scenarios(
        tmp_path,
        rid,
        [{"id": "s1", "check": "always_pass"}],
        allow_always_pass=True,
    )
    assert state["writer"] == "omg-cli"
    assert state["verified"] is False
    out = run_qa_cycle(tmp_path, rid)
    assert out["clean"] is True
    assert out["verified"] is False
    assert out["status"] == "clean"


def test_successful_retest_clears_invalidation(tmp_path: Path) -> None:
    from omg_cli.autopilot import invalidate_quality_stages, stage_qa_is_clean

    run = create_run(tmp_path, mode="qa", goal="clear-inv")
    rid = run["run_id"]
    freeze_scenarios(
        tmp_path,
        rid,
        [{"id": "s1", "check": "always_pass"}],
        allow_always_pass=True,
    )
    assert run_qa_cycle(tmp_path, rid)["clean"] is True
    invalidate_quality_stages(tmp_path, rid, reason="rework")
    assert stage_qa_is_clean(tmp_path, rid) is False
    # Re-run successful cycle must clear invalidated flag
    out = run_qa_cycle(tmp_path, rid)
    assert out["clean"] is True
    assert stage_qa_is_clean(tmp_path, rid) is True


def test_command_policy_denies_python_c_at_freeze(tmp_path: Path) -> None:
    """Policy is fail-closed at freeze (not deferred until run)."""
    run = create_run(tmp_path, mode="qa", goal="policy")
    rid = run["run_id"]
    with pytest.raises(QAError, match="rejected at freeze|-c"):
        freeze_scenarios(
            tmp_path,
            rid,
            [{"id": "bad", "command": "python3 -c 'import sys; sys.exit(1)'"}],
        )


def test_freeze_rejects_grep_with_tip(tmp_path: Path) -> None:
    run = create_run(tmp_path, mode="qa", goal="grep")
    rid = run["run_id"]
    with pytest.raises(QAError, match="grep|project .py"):
        freeze_scenarios(
            tmp_path,
            rid,
            [{"id": "bad", "command": "grep -q PARTIAL out.md"}],
        )


def test_failure_then_unchanged_hash_blocks(tmp_path: Path) -> None:
    run = create_run(tmp_path, mode="qa", goal="fail")
    rid = run["run_id"]
    freeze_scenarios(
        tmp_path,
        rid,
        [{"id": "bad", "command": "false"}],
    )
    failed = run_qa_cycle(tmp_path, rid)
    assert failed["clean"] is False
    assert failed["status"] == "failed"

    blocked = run_qa_cycle(
        tmp_path, rid, repair_classification="product_change"
    )
    assert blocked["status"] == "blocked"
    assert blocked["blocker"]["kind"] == "unchanged_hash"


def test_test_harness_correction_skips_hash_gate(tmp_path: Path) -> None:
    run = create_run(tmp_path, mode="qa", goal="harness")
    rid = run["run_id"]
    freeze_scenarios(
        tmp_path,
        rid,
        [{"id": "x", "command": "false"}],
    )
    run_qa_cycle(tmp_path, rid)
    again = run_qa_cycle(
        tmp_path, rid, repair_classification="test_harness_correction"
    )
    assert again.get("blocker", {}).get("kind") != "unchanged_hash"
    assert again["clean"] is False


def test_max_cycles_block(tmp_path: Path) -> None:
    run = create_run(tmp_path, mode="qa", goal="max")
    rid = run["run_id"]
    freeze_scenarios(
        tmp_path,
        rid,
        [{"id": "x", "command": "false"}],
    )
    state = load_qa(tmp_path, rid)
    state["cycle_count"] = 5
    _save(tmp_path, rid, state)
    with pytest.raises(QAError, match="max_cycles"):
        run_qa_cycle(tmp_path, rid)
