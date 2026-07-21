"""Tests for omg_cli.ralplan — CLI-owned FSM + APPROVE gate + dry_run."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omg_cli.ralplan import (
    DEFAULT_MAX_ROUNDS,
    READ_ONLY_STAGES,
    artifact_contains_approve,
    build_stage_prompt,
    load_ralplan_state,
    ralplan_state_path,
    run_ralplan,
    stage_artifact_json_path,
    stage_artifact_path,
    stage_prompt_path,
    verifier_has_approve,
)
from omg_cli.state import load_active_run, load_run


def test_default_max_rounds_is_three():
    assert DEFAULT_MAX_ROUNDS == 3


def test_artifact_approve_detection(tmp_path):
    md = tmp_path / "v.md"
    md.write_text("## Verdict\nAPPROVE\n\nAll good.\n", encoding="utf-8")
    assert artifact_contains_approve(md) is True

    # case-sensitive: approve lowercase is not enough
    md.write_text("we approve this\n", encoding="utf-8")
    assert artifact_contains_approve(md) is False

    # substring of larger word should not match
    md.write_text("DISAPPROVE\n", encoding="utf-8")
    assert artifact_contains_approve(md) is False

    # Codex P0: negation must not accept
    md.write_text("Do not APPROVE this plan yet.\n", encoding="utf-8")
    assert artifact_contains_approve(md) is False

    # free-floating APPROVE in body (prompt echo) is not terminal
    md.write_text(
        "Verdict must be explicit: **APPROVE** | **REQUEST CHANGES**.\n"
        "Still deciding.\n",
        encoding="utf-8",
    )
    assert artifact_contains_approve(md) is False

    js = tmp_path / "v.json"
    js.write_text(json.dumps({"verdict": "APPROVE"}), encoding="utf-8")
    assert artifact_contains_approve(js) is True

    js.write_text(json.dumps({"approve": True}), encoding="utf-8")
    assert artifact_contains_approve(js) is True

    js.write_text(json.dumps({"verdict": "REQUEST CHANGES"}), encoding="utf-8")
    assert artifact_contains_approve(js) is False

    assert artifact_contains_approve(tmp_path / "missing.md") is False


def test_critic_and_verifier_prompts_force_read_only():
    for stage in READ_ONLY_STAGES:
        text = build_stage_prompt(
            stage, "goal X", run_id="r1", round_n=1, max_rounds=3
        )
        assert "READ-ONLY" in text or "read-only" in text
        assert "goal X" in text
        assert stage in text
        assert "product code" in text.lower() or "Never" in text

    draft = build_stage_prompt(
        "draft", "goal Y", run_id="r1", round_n=1, max_rounds=3
    )
    assert "Draft" in draft or "draft" in draft
    assert "goal Y" in draft


def test_ralplan_ro_stages_disallow_shell_in_argv(monkeypatch, tmp_path):
    """critic/verifier argv get --disallowed-tools; draft/revise do not."""
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    # max_rounds=1: draft, critic, revise, verifier once
    rc = run_ralplan("plan X", root=tmp_path, max_rounds=1, dry_run=True)
    assert rc == 1  # no APPROVE
    active = load_active_run(tmp_path)
    assert active is not None
    rid = active["run_id"]
    sdir = tmp_path / ".omg" / "state" / "runs" / rid / "stages"
    for stage, expect_disallow in (
        ("draft", False),
        ("critic", True),
        ("revise", False),
        ("verifier", True),
    ):
        argv_path = sdir / f"{stage}-01.argv.json"
        assert argv_path.is_file(), stage
        argv = json.loads(argv_path.read_text(encoding="utf-8"))
        has = "--disallowed-tools" in argv
        assert has is expect_disallow, f"{stage}: disallow={has}"


def test_ralplan_ro_stages_ignore_yolo(monkeypatch, tmp_path):
    """yolo=True must not elevate critic/verifier; draft/revise may elevate."""
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    rc = run_ralplan(
        "plan Y", root=tmp_path, max_rounds=1, dry_run=True, yolo=True
    )
    assert rc == 1
    active = load_active_run(tmp_path)
    rid = active["run_id"]
    sdir = tmp_path / ".omg" / "state" / "runs" / rid / "stages"
    for stage in ("critic", "verifier"):
        argv = json.loads(
            (sdir / f"{stage}-01.argv.json").read_text(encoding="utf-8")
        )
        joined = " ".join(argv)
        assert "bypassPermissions" not in joined, stage
        assert "--always-approve" not in argv, stage
        assert argv[argv.index("--permission-mode") + 1] == "plan", stage
    # draft may still carry parent yolo elevation
    draft = json.loads((sdir / "draft-01.argv.json").read_text(encoding="utf-8"))
    assert "bypassPermissions" in " ".join(draft)


def test_dry_run_without_approve_fails_after_max_rounds(monkeypatch, tmp_path):
    """dry_run records stages; stubs lack APPROVE → failed after max_rounds."""
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )

    rc = run_ralplan(
        "consensus plan for schema",
        root=tmp_path,
        max_rounds=2,
        dry_run=True,
    )
    assert rc == 1

    active = load_active_run(tmp_path)
    assert active is not None
    assert active["mode"] == "ralplan"
    assert active["status"] == "failed"
    assert active.get("verified") is False

    rid = active["run_id"]
    state = load_ralplan_state(tmp_path, rid)
    assert state is not None
    assert state["status"] == "failed"
    assert state["accepted"] is False
    assert state["max_rounds"] == 2
    # first pass + one revise/verifier loop → 2 verifier attempts
    stages_done = [h["stage"] for h in state["history"]]
    assert stages_done[0] == "draft"
    assert stages_done[1] == "critic"
    assert "revise" in stages_done
    assert stages_done.count("verifier") == 2

    # prompts and artifacts written for each stage
    assert stage_prompt_path(tmp_path, rid, "draft", 1).is_file()
    assert stage_artifact_path(tmp_path, rid, "draft", 1).is_file()
    assert stage_prompt_path(tmp_path, rid, "verifier", 1).is_file()
    art1 = stage_artifact_path(tmp_path, rid, "verifier", 1)
    assert art1.is_file()
    assert artifact_contains_approve(art1) is False
    assert verifier_has_approve(tmp_path, rid, 1) is False


def test_dry_run_accepts_when_stage_writes_approve(monkeypatch, tmp_path):
    """Simulate stages: custom executor writes verifier APPROVE → accepted."""
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )

    from omg_cli import ralplan as rp

    def exec_with_approve(
        stage,
        *,
        root,
        run_id,
        goal,
        round_n,
        max_rounds,
        yolo,
        safe,
        dry_run,
        timeout,
        extra=None,
    ):
        # real stage write (prompt + stub)
        rc = rp._execute_stage(
            stage,
            root=root,
            run_id=run_id,
            goal=goal,
            round_n=round_n,
            max_rounds=max_rounds,
            yolo=yolo,
            safe=safe,
            dry_run=dry_run,
            timeout=timeout,
            extra=extra,
        )
        if stage == "verifier":
            art = stage_artifact_path(root, run_id, "verifier", round_n)
            art.write_text(
                "## Verdict\nAPPROVE\n\nPlan is coherent and testable.\n",
                encoding="utf-8",
            )
        return rc

    rc = run_ralplan(
        "steelman the plan",
        root=tmp_path,
        max_rounds=3,
        dry_run=True,
        stage_executor=exec_with_approve,
    )
    assert rc == 0

    active = load_active_run(tmp_path)
    assert active is not None
    assert active["status"] == "completed"
    assert active.get("ralplan_status") == "accepted"
    # never product-verified via ralplan
    assert active.get("verified") is False

    rid = active["run_id"]
    state = load_ralplan_state(tmp_path, rid)
    assert state is not None
    assert state["status"] == "accepted"
    assert state["accepted"] is True
    assert ralplan_state_path(tmp_path, rid).is_file()

    # first verifier round only
    assert state["round"] == 1
    assert any(h.get("approve") is True for h in state["history"] if h["stage"] == "verifier")
    assert verifier_has_approve(tmp_path, rid, 1) is True


def test_dry_run_accepts_via_json_verdict(monkeypatch, tmp_path):
    """JSON verdict field APPROVE also accepts."""
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    from omg_cli import ralplan as rp

    def exec_json_approve(
        stage,
        *,
        root,
        run_id,
        goal,
        round_n,
        max_rounds,
        yolo,
        safe,
        dry_run,
        timeout,
        extra=None,
    ):
        rc = rp._execute_stage(
            stage,
            root=root,
            run_id=run_id,
            goal=goal,
            round_n=round_n,
            max_rounds=max_rounds,
            yolo=yolo,
            safe=safe,
            dry_run=dry_run,
            timeout=timeout,
            extra=extra,
        )
        if stage == "verifier":
            # leave md stub without APPROVE; put APPROVE in JSON instead
            js = stage_artifact_json_path(root, run_id, "verifier", round_n)
            js.write_text(
                json.dumps({"verdict": "APPROVE", "notes": "ok"}) + "\n",
                encoding="utf-8",
            )
        return rc

    rc = run_ralplan(
        "json approve path",
        root=tmp_path,
        max_rounds=1,
        dry_run=True,
        stage_executor=exec_json_approve,
    )
    assert rc == 0
    state = load_ralplan_state(tmp_path, load_active_run(tmp_path)["run_id"])
    assert state["accepted"] is True


def test_verifier_has_approve_cross_artifact_severity_rc_beats_sibling_approve(
    tmp_path,
):
    """A2a false-green: real md REQUEST_CHANGES must beat sibling unbound JSON APPROVE.

    Pre-fix used raw ``or`` across siblings; path-bound md REQUEST_CHANGES was
    overridden by a legacy-exempt (no run_id) json APPROVE. Aggregate must be
    most-severe: FAILED > REQUEST_CHANGES > APPROVE.
    """
    rid = "run-a2a-rc"
    sdir = tmp_path / ".omg" / "state" / "runs" / rid / "stages"
    sdir.mkdir(parents=True)
    md = stage_artifact_path(tmp_path, rid, "verifier", 1)
    js = stage_artifact_json_path(tmp_path, rid, "verifier", 1)
    md.write_text("## Verdict\nREQUEST CHANGES\n\nPlan is incomplete.\n", encoding="utf-8")
    js.write_text(json.dumps({"verdict": "APPROVE", "notes": "stray"}) + "\n", encoding="utf-8")
    assert verifier_has_approve(tmp_path, rid, 1) is False


def test_verifier_has_approve_cross_artifact_failed_beats_sibling_approve(tmp_path):
    """A2a: real md FAILED must beat sibling unbound JSON APPROVE."""
    rid = "run-a2a-failed"
    sdir = tmp_path / ".omg" / "state" / "runs" / rid / "stages"
    sdir.mkdir(parents=True)
    md = stage_artifact_path(tmp_path, rid, "verifier", 1)
    js = stage_artifact_json_path(tmp_path, rid, "verifier", 1)
    md.write_text("## Verdict\nFAILED\n\nBlocking defects.\n", encoding="utf-8")
    js.write_text(json.dumps({"verdict": "APPROVE"}) + "\n", encoding="utf-8")
    assert verifier_has_approve(tmp_path, rid, 1) is False


def test_verifier_has_approve_json_rc_beats_sibling_md_approve(tmp_path):
    """A2a: REQUEST_CHANGES in either sibling wins (json reject + md approve)."""
    rid = "run-a2a-json-rc"
    sdir = tmp_path / ".omg" / "state" / "runs" / rid / "stages"
    sdir.mkdir(parents=True)
    md = stage_artifact_path(tmp_path, rid, "verifier", 1)
    js = stage_artifact_json_path(tmp_path, rid, "verifier", 1)
    md.write_text("## Verdict\nAPPROVE\n", encoding="utf-8")
    js.write_text(json.dumps({"verdict": "REQUEST_CHANGES"}) + "\n", encoding="utf-8")
    assert verifier_has_approve(tmp_path, rid, 1) is False


def test_revise_loop_then_approve(monkeypatch, tmp_path):
    """First verifier rejects; second round APPROVE → accepted; round==2."""
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    from omg_cli import ralplan as rp

    def exec_second_round_approve(
        stage,
        *,
        root,
        run_id,
        goal,
        round_n,
        max_rounds,
        yolo,
        safe,
        dry_run,
        timeout,
        extra=None,
    ):
        rc = rp._execute_stage(
            stage,
            root=root,
            run_id=run_id,
            goal=goal,
            round_n=round_n,
            max_rounds=max_rounds,
            yolo=yolo,
            safe=safe,
            dry_run=dry_run,
            timeout=timeout,
            extra=extra,
        )
        if stage == "verifier" and round_n >= 2:
            art = stage_artifact_path(root, run_id, "verifier", round_n)
            art.write_text("## Verdict\nAPPROVE\n", encoding="utf-8")
        elif stage == "verifier":
            art = stage_artifact_path(root, run_id, "verifier", round_n)
            art.write_text("## Verdict\nREQUEST CHANGES\n", encoding="utf-8")
        return rc

    rc = run_ralplan(
        "needs one revise",
        root=tmp_path,
        max_rounds=3,
        dry_run=True,
        stage_executor=exec_second_round_approve,
    )
    assert rc == 0
    rid = load_active_run(tmp_path)["run_id"]
    state = load_ralplan_state(tmp_path, rid)
    assert state["accepted"] is True
    assert state["round"] == 2
    verifiers = [h for h in state["history"] if h["stage"] == "verifier"]
    assert len(verifiers) == 2
    assert verifiers[0]["approve"] is False
    assert verifiers[1]["approve"] is True


def test_run_mode_delegates_to_ralplan(monkeypatch, tmp_path):
    """modes.run_mode('ralplan') uses FSM, not single-launch loop."""
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    from omg_cli.modes import run_mode

    # without APPROVE → failed (rc 1), but ralplan.json exists
    rc = run_mode("ralplan", "via run_mode", root=tmp_path, max_iter=1, dry_run=True)
    assert rc == 1
    active = load_active_run(tmp_path)
    assert active is not None
    assert active["mode"] == "ralplan"
    rid = active["run_id"]
    assert ralplan_state_path(tmp_path, rid).is_file()
    state = load_ralplan_state(tmp_path, rid)
    assert state["status"] == "failed"
    assert state["max_rounds"] == 1


def test_cli_ralplan_dry_run(tmp_path):
    """omg ralplan --dry-run creates FSM state (fails without APPROVE)."""
    import os
    import sys

    env = os.environ.copy()
    env["PYTHONPATH"] = str(
        Path(__file__).resolve().parents[1]
    ) + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "omg_cli.main",
            "ralplan",
            "cli dry",
            "--dry-run",
            "--max-iter",
            "1",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 1, r.stderr + r.stdout
    # find ralplan.json under runs
    runs = list((tmp_path / ".omg" / "state" / "runs").glob("*/ralplan.json"))
    assert len(runs) == 1
    data = json.loads(runs[0].read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert data["accepted"] is False
