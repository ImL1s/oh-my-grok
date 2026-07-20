"""U-09 structured hash-bound review gate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from omg_cli.review import (
    ReviewError,
    compute_diff_hash,
    evaluate_lane,
    run_structured_review,
)
from omg_cli.state import create_run


def test_clean_requires_approve_and_clear_on_current_hash(tmp_path: Path) -> None:
    run = create_run(
        tmp_path,
        mode="dual-review",
        goal="rev",
        extra={"schema_version": 2, "lifecycle_version": 2},
    )
    rid = run["run_id"]
    diff = "diff --git a/x b/x\n+hello\n"
    st = run_structured_review(
        tmp_path,
        rid,
        diff_text=diff,
        code_reviewer_payload={"verdict": "APPROVE", "findings": []},
        architect_payload={"verdict": "CLEAR", "findings": []},
    )
    assert st["clean"] is True
    assert st["disposition"] == "clean"
    assert st["diff_hash"] == compute_diff_hash(diff)
    assert st["writer"] == "omg-cli"


def test_stale_hash_and_wrong_role_fail(tmp_path: Path) -> None:
    run = create_run(tmp_path, mode="dual-review", goal="stale")
    rid = run["run_id"]
    diff = "current"
    # Build a stamp for old hash then evaluate against new
    st = run_structured_review(
        tmp_path,
        rid,
        diff_text=diff,
        code_reviewer_payload={"verdict": "APPROVE", "findings": []},
        architect_payload={"verdict": "CLEAR", "findings": []},
    )
    old_cr = st["code_reviewer_stamp"]
    # Force wrong hash evaluation
    lane = evaluate_lane(
        role="code-reviewer",
        expected_diff_hash=compute_diff_hash("other"),
        proposal=None,
        stamped=old_cr,
    )
    assert lane["clean"] is False
    assert lane["reason"] == "stale_or_wrong_diff_hash"

    bad_role = dict(old_cr)
    bad_role["role"] = "architect"
    lane2 = evaluate_lane(
        role="code-reviewer",
        expected_diff_hash=old_cr["diff_hash"],
        proposal=None,
        stamped=bad_role,
    )
    assert lane2["reason"] == "wrong_role"


def test_forged_writer_and_major_finding(tmp_path: Path) -> None:
    forged = {
        "writer": "agent",
        "role": "code-reviewer",
        "diff_hash": compute_diff_hash("d"),
        "payload": {"verdict": "APPROVE", "findings": []},
    }
    lane = evaluate_lane(
        role="code-reviewer",
        expected_diff_hash=forged["diff_hash"],
        proposal=None,
        stamped=forged,
    )
    assert lane["clean"] is False
    assert lane["reason"] == "forged_or_untrusted_writer"

    run = create_run(tmp_path, mode="dual-review", goal="major")
    st = run_structured_review(
        tmp_path,
        run["run_id"],
        diff_text="d2",
        code_reviewer_payload={
            "verdict": "APPROVE",
            "findings": [
                {
                    "severity": "blocker",
                    "file": "a.py",
                    "line": 1,
                    "evidence": "bad",
                }
            ],
        },
        architect_payload={"verdict": "CLEAR", "findings": []},
    )
    assert st["clean"] is False
    assert st["disposition"] in {"rework", "blocked"}


def test_replan_disposition(tmp_path: Path) -> None:
    run = create_run(tmp_path, mode="dual-review", goal="replan")
    st = run_structured_review(
        tmp_path,
        run["run_id"],
        diff_text="d3",
        code_reviewer_payload={
            "verdict": "REQUEST_CHANGES",
            "findings": [
                {
                    "severity": "major",
                    "kind": "requirement",
                    "file": "spec",
                    "evidence": "scope change",
                }
            ],
        },
        architect_payload={"verdict": "CLEAR", "findings": []},
    )
    assert st["disposition"] == "replan"
