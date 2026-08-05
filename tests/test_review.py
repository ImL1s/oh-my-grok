"""U-09 structured hash-bound review gate."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from omg_cli.review import (
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


def test_run_structured_review_records_workspace_fingerprint(tmp_path: Path) -> None:
    """R2-2: every stamp records the current implement-workspace fingerprint
    so the autopilot review gate can bind clean=true to the workspace it
    actually describes."""
    from omg_cli.autopilot import _implement_workspace_fingerprint

    run = create_run(tmp_path, mode="dual-review", goal="fp")
    rid = run["run_id"]
    st = run_structured_review(
        tmp_path,
        rid,
        diff_text="diff body",
        code_reviewer_payload={"verdict": "APPROVE", "findings": []},
        architect_payload={"verdict": "CLEAR", "findings": []},
    )
    assert st["workspace_fp"] == _implement_workspace_fingerprint(tmp_path)


def test_cmd_review_rejects_empty_diff_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R2-2: --diff-text must be non-empty on the CLI path — a clean stamp
    bound to an empty diff hash is not a real per-diff review."""
    from omg_cli.commands import modes as modes_cmds

    monkeypatch.setattr(modes_cmds, "project_root", lambda: tmp_path)
    run = create_run(tmp_path, mode="dual-review", goal="empty diff")
    ns = argparse.Namespace(
        run_id=run["run_id"],
        diff_text="   ",
        code_reviewer_json="{}",
        architect_json="{}",
    )
    rc = modes_cmds.cmd_review(ns)
    assert rc == 2
    assert "diff-text" in capsys.readouterr().err


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
