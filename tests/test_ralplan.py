"""Tests for omg_cli.ralplan — CLI-owned FSM + APPROVE gate + dry_run."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from omg_cli.ralplan import (
    DEFAULT_MAX_ROUNDS,
    READ_ONLY_STAGES,
    artifact_contains_approve,
    build_stage_prompt,
    invalidate_ralplan_consensus,
    load_ralplan_state,
    ralplan_state_path,
    run_ralplan,
    stage_artifact_json_path,
    stage_artifact_path,
    stage_prompt_path,
    verifier_has_approve,
)
from omg_cli.state import load_active_run


def test_default_max_rounds_is_three():
    assert DEFAULT_MAX_ROUNDS == 3


def test_invalidate_ralplan_consensus_fail_closed_on_writer_mismatch(tmp_path):
    """Mirrors invalidate_implementation_receipt's fail-closed guard: only
    mutate a stamp that is actually CLI-owned for this run — an untrusted
    or foreign-run stamp must be left alone rather than silently rewritten."""
    path = ralplan_state_path(tmp_path, "run-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    forged = {"writer": "not-omg-cli", "run_id": "run-a", "accepted": True}
    path.write_text(json.dumps(forged), encoding="utf-8")

    invalidate_ralplan_consensus(tmp_path, "run-a", reason="test")

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == forged  # untouched: wrong writer


def test_invalidate_ralplan_consensus_fail_closed_on_run_id_mismatch(tmp_path):
    path = ralplan_state_path(tmp_path, "run-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    from omg_cli.evidence import CLI_WRITER

    foreign = {"writer": CLI_WRITER, "run_id": "some-other-run", "accepted": True}
    path.write_text(json.dumps(foreign), encoding="utf-8")

    invalidate_ralplan_consensus(tmp_path, "run-a", reason="test")

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == foreign  # untouched: run_id mismatch


def test_invalidate_ralplan_consensus_mutates_valid_cli_stamp(tmp_path):
    path = ralplan_state_path(tmp_path, "run-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    from omg_cli.evidence import CLI_WRITER

    valid = {"writer": CLI_WRITER, "run_id": "run-a", "accepted": True}
    path.write_text(json.dumps(valid), encoding="utf-8")

    invalidate_ralplan_consensus(tmp_path, "run-a", reason="test")

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk.get("accepted") is False
    assert on_disk.get("invalidated") is True


def _v2_approve_payload(stage: str, *, run_id: str, round_n: int, **identity):
    """Full structured proposal accepted by ``_validate_v2_proposal``."""
    payload = {
        "schema_version": 2,
        "run_id": run_id,
        "stage": stage,
        "role": stage,
        "round": round_n,
        **identity,
    }
    if stage == "planner":
        payload.update(
            {
                "verdict": "READY",
                "plan": "do the thing",
                "principles": "kiss",
                "drivers": "ship it",
                "options": "one viable path",
                "acceptance": "tests pass",
            }
        )
    elif stage == "architect":
        payload.update(
            {
                "verdict": "APPROVE",
                "steelman": "strongest viable interpretation",
                "tradeoff": "safety before breadth",
                "synthesis": "use the strict lifecycle",
            }
        )
    elif stage == "critic":
        payload.update(
            {
                "verdict": "APPROVE",
                "options_assessment": "reviewed",
                "premortem": "no blockers found",
                "acceptance_assessment": "meets acceptance bar",
                "test_plan": "unit + integration",
                "synthesis": "approve",
            }
        )
    return payload


def _v2_full_approve_executor():
    """Real-shaped stage executor: planner READY, architect/critic APPROVE."""

    def execute(stage, **kwargs):
        payload = _v2_approve_payload(
            stage,
            run_id=kwargs["run_id"],
            round_n=kwargs["round_n"],
            invocation_id=kwargs["invocation_id"],
            session_id=kwargs["session_id"],
            input_sha256=kwargs["input_sha256"],
        )
        artifact = stage_artifact_json_path(
            Path(kwargs["root"]), kwargs["run_id"], stage, kwargs["round_n"]
        )
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return 0

    return execute


def test_fresh_accept_clears_invalidation_strict_v2(tmp_path):
    """R5-1: a fresh ``accepted: true`` write must clear a prior
    ``invalidated``/``invalidated_reason``/``invalidated_at`` stamp so
    ``_consensus_ready`` unlocks again — mirrors the real accept path used
    by autopilot's ralplan phase (strict-v2 consensus), not a hand-crafted
    stamp overwrite."""
    from omg_cli.autopilot import _consensus_ready
    from omg_cli.state import create_run

    goal = "clear invalidation on fresh accept"
    run = create_run(
        tmp_path,
        mode="ralplan",
        goal=goal,
        extra={"schema_version": 2, "lifecycle_version": 2},
    )
    run_id = run["run_id"]

    rc = run_ralplan(
        goal,
        root=tmp_path,
        existing_run_id=run_id,
        dry_run=True,
        stage_executor=_v2_full_approve_executor(),
    )
    assert rc == 0
    state = load_ralplan_state(tmp_path, run_id)
    assert state is not None
    assert state["accepted"] is True
    assert _consensus_ready(tmp_path, run_id) is True

    invalidate_ralplan_consensus(tmp_path, run_id, reason="replan from review")
    state = load_ralplan_state(tmp_path, run_id)
    assert state is not None
    assert state["accepted"] is False
    assert state["invalidated"] is True
    assert state["invalidated_reason"]
    assert state["invalidated_at"]
    assert _consensus_ready(tmp_path, run_id) is False

    # Fresh consensus attempt (round 2) via the real strict-v2 accept path —
    # not a hand-crafted stamp — must clear the invalidation on accept.
    rc = run_ralplan(
        goal,
        root=tmp_path,
        existing_run_id=run_id,
        dry_run=True,
        stage_executor=_v2_full_approve_executor(),
    )
    assert rc == 0

    state = load_ralplan_state(tmp_path, run_id)
    assert state is not None
    assert state["accepted"] is True
    assert "invalidated" not in state
    assert "invalidated_reason" not in state
    assert "invalidated_at" not in state
    assert _consensus_ready(tmp_path, run_id) is True


def test_invalidated_flag_cleared_at_fresh_v2_cycle_start(tmp_path):
    """R5-1: entering a new strict-v2 consensus attempt under lease while
    ``invalidated=True``/``accepted=False`` must clear the invalidation
    stamp at cycle start — a mid-run resume must not stay fenced by stale
    invalidation history even before any new accept happens."""
    from omg_cli.evidence import CLI_WRITER

    goal = "clear invalidation at cycle start"
    from omg_cli.state import create_run

    run = create_run(
        tmp_path,
        mode="ralplan",
        goal=goal,
        extra={"schema_version": 2, "lifecycle_version": 2},
    )
    run_id = run["run_id"]

    path = ralplan_state_path(tmp_path, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    stale_state = {
        "writer": CLI_WRITER,
        "schema_version": 2,
        "lifecycle_version": 2,
        "run_id": run_id,
        "goal": goal,
        "status": "draft",
        "stage": "draft",
        "round": 0,
        "max_rounds": 5,
        "history": [],
        "sessions": {
            role: {"session_id": "s-" + role, "attempts": 0}
            for role in ("planner", "architect", "critic")
        },
        "accepted": False,
        "invalidated": True,
        "invalidated_reason": "stale replan",
        "invalidated_at": "2026-01-01T00:00:00+00:00",
    }
    path.write_text(json.dumps(stale_state), encoding="utf-8")

    # Executor that never writes a proposal: this cycle does not accept, it
    # only proves the invalidation stamp is cleared at cycle start.
    rc = run_ralplan(
        goal,
        root=tmp_path,
        existing_run_id=run_id,
        max_rounds=1,
        dry_run=True,
        stage_executor=lambda stage, **kwargs: 0,
    )
    assert rc != 0  # no APPROVE produced; consensus not reached this cycle

    state = load_ralplan_state(tmp_path, run_id)
    assert state is not None
    assert "invalidated" not in state
    assert "invalidated_reason" not in state
    assert "invalidated_at" not in state


def test_reset_for_fresh_cycle_mints_new_session_ids_and_zeroes_attempts():
    """R8-1/P2-5: ``_reset_for_fresh_cycle`` must mint a brand-new
    ``session_id`` per role, not just zero ``attempts`` on the old one —
    reusing the prior cycle's UUID with a reset counter would let the
    executor mistake this for a continuation of the invalidated session."""
    from omg_cli import ralplan as rp

    old_session_ids = {
        role: f"old-{role}" for role in ("planner", "architect", "critic")
    }
    state = {
        "sessions": {
            role: {"session_id": old_session_ids[role], "attempts": 3}
            for role in ("planner", "architect", "critic")
        },
    }

    rp._reset_for_fresh_cycle(state)

    for role in ("planner", "architect", "critic"):
        binding = state["sessions"][role]
        assert binding["session_id"] != old_session_ids[role]
        assert binding["attempts"] == 0


def test_high_prior_history_invalidate_rerun_still_executes_stages(tmp_path):
    """R6-1: seeded history through round 5 + invalidate + a low-ceiling
    re-run must still execute stages, not immediately block with zero
    stages. Without resetting history/round/session-attempts on a fresh
    cycle, ``first_round`` (derived from max prior history round + 1) stays
    pinned past the configured ceiling forever, so every future replan
    instantly blocks. Identity fields (run_id/goal/writer/schema) must
    survive the reset."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.state import create_run

    goal = "reset history on fresh replan cycle"
    run = create_run(
        tmp_path,
        mode="ralplan",
        goal=goal,
        extra={"schema_version": 2, "lifecycle_version": 2},
    )
    run_id = run["run_id"]

    path = ralplan_state_path(tmp_path, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    prior_history = [
        {
            "stage": stage,
            "role": stage,
            "round": 5,
            "verdict": "APPROVE" if stage != "planner" else "READY",
            "exit_code": 0,
            "valid": True,
        }
        for stage in ("planner", "architect", "critic")
    ]
    stale_state = {
        "writer": CLI_WRITER,
        "schema_version": 2,
        "lifecycle_version": 2,
        "run_id": run_id,
        "goal": goal,
        "status": "critic",
        "stage": "critic",
        "round": 5,
        "max_rounds": 5,
        "history": prior_history,
        "sessions": {
            role: {"session_id": "s-" + role, "attempts": 5}
            for role in ("planner", "architect", "critic")
        },
        "accepted": False,
        "invalidated": True,
        "invalidated_reason": "stale replan",
        "invalidated_at": "2026-01-01T00:00:00+00:00",
    }
    path.write_text(json.dumps(stale_state), encoding="utf-8")

    rc = run_ralplan(
        goal,
        root=tmp_path,
        existing_run_id=run_id,
        max_rounds=1,
        dry_run=True,
        stage_executor=_v2_full_approve_executor(),
    )
    assert rc == 0  # a fresh round 1 must be able to reach APPROVE, not block

    state = load_ralplan_state(tmp_path, run_id)
    assert state is not None
    assert state["accepted"] is True
    # Identity fields survive the reset untouched.
    assert state["run_id"] == run_id
    assert state["goal"] == goal
    assert state["writer"] == CLI_WRITER
    assert state["schema_version"] == 2
    # New stages actually executed at round 1 (not stuck past the ceiling).
    new_rounds = {item["round"] for item in state["history"]}
    assert new_rounds == {1}
    assert "invalidated" not in state


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


def test_ralplan_run_rejects_non_embeddable_mode(tmp_path, capsys):
    """`--run` must not rewrite ralph/ulw status into a ralplan FSM."""
    from omg_cli.state import create_run, load_run

    ralph = create_run(tmp_path, mode="ralph", goal="unrelated ralph")
    rid = ralph["run_id"]
    before = load_run(tmp_path, rid)
    assert before is not None
    rc = run_ralplan(
        "should not attach",
        root=tmp_path,
        dry_run=True,
        max_rounds=1,
        existing_run_id=rid,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "only embeddable" in err
    assert "ralph" in err
    after = load_run(tmp_path, rid)
    assert after is not None
    assert after.get("mode") == "ralph"
    assert not (tmp_path / ".omg" / "state" / "runs" / rid / "ralplan.json").is_file()


def test_ralplan_run_allows_autopilot_embed(tmp_path):
    from omg_cli.autopilot import start_autopilot

    st = start_autopilot(tmp_path, "embed me", skip_interview=True)
    rid = st["run_id"]
    rc = run_ralplan(
        "embed me",
        root=tmp_path,
        dry_run=True,
        max_rounds=1,
        existing_run_id=rid,
    )
    # dry-run without verifier APPROVE → failed (1), but FSM attached
    assert rc == 1
    assert (tmp_path / ".omg" / "state" / "runs" / rid / "ralplan.json").is_file()
    active = load_active_run(tmp_path)
    assert active is not None
    assert active["run_id"] == rid
    assert active.get("mode") == "autopilot"


def test_ralplan_run_rejects_mismatched_goal_for_autopilot(tmp_path, capsys):
    """R5-4: embedding ralplan into an autopilot run must bind to the
    frozen run goal — a mismatched CLI goal is rejected rather than
    silently re-targeting a running autopilot's plan."""
    from omg_cli.autopilot import start_autopilot

    st = start_autopilot(tmp_path, "embed me", skip_interview=True)
    rid = st["run_id"]
    rc = run_ralplan(
        "a totally different goal",
        root=tmp_path,
        dry_run=True,
        max_rounds=1,
        existing_run_id=rid,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "goal" in err.lower()
    assert not (tmp_path / ".omg" / "state" / "runs" / rid / "ralplan.json").is_file()


def test_ralplan_run_rejects_wrong_autopilot_phase(tmp_path, capsys):
    """R5-4: embedding ralplan into an autopilot run parked outside the
    ``ralplan`` phase (e.g. still at ``interview``) must be rejected —
    the CLI FSM must never rewrite a phase it does not own."""
    from omg_cli.autopilot import start_autopilot

    st = start_autopilot(tmp_path, "embed me", skip_interview=False)
    rid = st["run_id"]
    assert st["phase"] == "interview"
    rc = run_ralplan(
        "embed me",
        root=tmp_path,
        dry_run=True,
        max_rounds=1,
        existing_run_id=rid,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "phase" in err.lower()
    assert not (tmp_path / ".omg" / "state" / "runs" / rid / "ralplan.json").is_file()


def test_ralplan_run_rejects_legacy_schema_for_autopilot(tmp_path, capsys):
    """R7-4: autopilot embedding must require strict-v2 — a legacy-v1
    autopilot run (e.g. pre-migration status.json missing schema_version)
    must be rejected with a clear error, never silently downgraded into
    ``_run_ralplan_v1``'s frozen single-run FSM."""
    from omg_cli.autopilot import start_autopilot

    st = start_autopilot(tmp_path, "embed me", skip_interview=True)
    rid = st["run_id"]

    status_path = tmp_path / ".omg" / "state" / "runs" / rid / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status.get("schema_version") == 2  # sanity: real autopilot starts strict-v2
    del status["schema_version"]
    del status["lifecycle_version"]
    status_path.write_text(json.dumps(status), encoding="utf-8")

    rc = run_ralplan(
        "embed me",
        root=tmp_path,
        dry_run=True,
        max_rounds=1,
        existing_run_id=rid,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "autopilot" in err.lower()
    assert "strict-v2" in err.lower()
    assert not (tmp_path / ".omg" / "state" / "runs" / rid / "ralplan.json").is_file()


def test_ralplan_run_rejects_terminal_autopilot_run(tmp_path, capsys):
    """R8-2 (P2-3): an autopilot run that has already reached a terminal
    status (cancelled/completed/failed/verified) must never accept a new
    ralplan embedding — a dead run's plan is not a valid target."""
    from omg_cli.autopilot import start_autopilot

    st = start_autopilot(tmp_path, "embed me", skip_interview=True)
    rid = st["run_id"]

    status_path = tmp_path / ".omg" / "state" / "runs" / rid / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["status"] = "cancelled"
    status_path.write_text(json.dumps(status), encoding="utf-8")

    rc = run_ralplan(
        "embed me",
        root=tmp_path,
        dry_run=True,
        max_rounds=1,
        existing_run_id=rid,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "terminal" in err.lower()
    assert not (tmp_path / ".omg" / "state" / "runs" / rid / "ralplan.json").is_file()


def test_ralplan_run_rejects_pending_cancellation_request(tmp_path, capsys):
    """R8-2 (P2-3): a committed-but-not-yet-finalized cancellation request
    must also block embedding. ``omg cancel`` commits its request under
    the distinct transition lock; there is a real window where the request
    exists but ``status`` has not flipped to ``cancelled`` yet — the CLI
    must fail closed in that window too, not just once status is terminal."""
    from omg_cli.autopilot import start_autopilot

    st = start_autopilot(tmp_path, "embed me", skip_interview=True)
    rid = st["run_id"]

    request_path = tmp_path / ".omg" / "state" / "runs" / rid / "cancel.request.json"
    request_path.write_text(
        json.dumps(
            {
                "writer": "omg-cli",
                "run_id": rid,
                "request_id": "pending-request",
                "requested_at": "2026-08-05T00:00:00+00:00",
                "observed_generation": 0,
            }
        ),
        encoding="utf-8",
    )

    rc = run_ralplan(
        "embed me",
        root=tmp_path,
        dry_run=True,
        max_rounds=1,
        existing_run_id=rid,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "cancellation" in err.lower()
    assert not (tmp_path / ".omg" / "state" / "runs" / rid / "ralplan.json").is_file()


def test_ralplan_v2_reauthorizes_before_accept_rejects_mid_round_cancellation(
    tmp_path, capsys
):
    """R8-2 (P2-3), TDD core case: a strict-v2 ralplan embedding holds the
    execution lease across the planner/architect/critic stages of a round.
    A concurrent ``omg cancel`` commits its request under the distinct
    transition lock mid-round (simulated here by the critic-stage
    executor), landing *after* the lease-scoped authorization at loop entry
    but *before* the accepted=True write. The re-authorization immediately
    before that write must catch it: run_ralplan must return non-zero and
    must not stamp accepted=True even though critic itself returned
    APPROVE."""
    from omg_cli.autopilot import start_autopilot

    st = start_autopilot(tmp_path, "embed me", skip_interview=True)
    rid = st["run_id"]
    request_path = tmp_path / ".omg" / "state" / "runs" / rid / "cancel.request.json"

    def exec_with_cancel_race(stage, **kwargs):
        payload = _v2_approve_payload(
            stage,
            run_id=kwargs["run_id"],
            round_n=kwargs["round_n"],
            invocation_id=kwargs["invocation_id"],
            session_id=kwargs["session_id"],
            input_sha256=kwargs["input_sha256"],
        )
        artifact = stage_artifact_json_path(
            Path(kwargs["root"]), kwargs["run_id"], stage, kwargs["round_n"]
        )
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        if stage == "critic":
            request_path.write_text(
                json.dumps(
                    {
                        "writer": "omg-cli",
                        "run_id": kwargs["run_id"],
                        "request_id": "race-request",
                        "requested_at": "2026-08-05T00:00:00+00:00",
                        "observed_generation": 0,
                    }
                ),
                encoding="utf-8",
            )
        return 0

    rc = run_ralplan(
        "embed me",
        root=tmp_path,
        dry_run=True,
        existing_run_id=rid,
        stage_executor=exec_with_cancel_race,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "cancellation" in err.lower()

    state = load_ralplan_state(tmp_path, rid)
    assert state is not None
    assert state.get("accepted") is not True

    status = json.loads(
        (tmp_path / ".omg" / "state" / "runs" / rid / "status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status.get("ralplan_status") != "accepted"
