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
    )
    assert state["writer"] == "omg-cli"
    assert state["verified"] is False
    out = run_qa_cycle(tmp_path, rid)
    assert out["clean"] is True
    assert out["verified"] is False
    assert out["status"] == "clean"


def test_failure_then_unchanged_hash_blocks(tmp_path: Path) -> None:
    run = create_run(tmp_path, mode="qa", goal="fail")
    rid = run["run_id"]
    freeze_scenarios(
        tmp_path,
        rid,
        [{"id": "bad", "command": "python3 -c 'import sys; sys.exit(1)'"}],
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
        [{"id": "x", "command": "python3 -c 'import sys; sys.exit(1)'"}],
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
        [{"id": "x", "command": "python3 -c 'import sys; sys.exit(1)'"}],
    )
    state = load_qa(tmp_path, rid)
    state["cycle_count"] = 5
    _save(tmp_path, rid, state)
    with pytest.raises(QAError, match="max_cycles"):
        run_qa_cycle(tmp_path, rid)
