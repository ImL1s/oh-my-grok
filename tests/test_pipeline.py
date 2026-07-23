"""Tests for omg pipeline FSM — dry-run stage order, no allow env, hooks."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from omg_cli.integrate import default_envelopes_dir
from omg_cli.pipeline import load_pipeline_state, report_path, run_pipeline
from omg_cli.state import create_run, load_active_run, load_run


def test_pipeline_dry_run_plan_implement_order(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    # Ensure parent does not have allow
    monkeypatch.delenv("OMG_ALLOW_EXTERNAL_CLI", raising=False)

    rc = run_pipeline(
        "noop design",
        root=tmp_path,
        dry_run=True,
        plan_only=False,
        dual_review=True,
        require_acceptance=False,
        max_plan_rounds=1,
        max_iter=1,
    )
    assert rc == 0, "dry_run pipeline should exit 0 without require_acceptance"

    active = load_active_run(tmp_path)
    assert active is not None
    assert active["mode"] == "pipeline"
    assert active.get("verified") is False

    rid = active["run_id"]
    state = load_pipeline_state(tmp_path, rid)
    assert state is not None
    # Expect plan then implement then dual_review then accept
    enter_stages = [h["stage"] for h in state["history"] if h.get("event") == "enter"]
    assert enter_stages[0] == "plan"
    assert "implement" in enter_stages
    assert "dual_review" in enter_stages
    assert "accept" in enter_stages
    # Order: plan before implement before dual before accept
    assert enter_stages.index("plan") < enter_stages.index("implement")
    assert enter_stages.index("implement") < enter_stages.index("dual_review")
    assert enter_stages.index("dual_review") < enter_stages.index("accept")
    # report.json written
    rpath = report_path(tmp_path, rid)
    assert rpath.is_file()
    report = json.loads(rpath.read_text(encoding="utf-8"))
    assert report["writer"] == "omg-cli"
    assert report["run_id"] == rid
    assert "stages" in report
    assert report.get("verified") is False


def test_pipeline_skip_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    rc = run_pipeline(
        "already planned",
        root=tmp_path,
        dry_run=True,
        skip_plan=True,
        dual_review=False,
        require_acceptance=False,
        max_iter=1,
    )
    assert rc == 0
    active = load_active_run(tmp_path)
    state = load_pipeline_state(tmp_path, active["run_id"])
    enter = [h["stage"] for h in state["history"] if h.get("event") == "enter"]
    assert "plan" not in enter or any(
        h.get("event") == "skip" for h in state["history"] if h.get("stage") == "plan"
    )
    assert enter[0] == "implement"


def test_pipeline_plan_only(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    rc = run_pipeline(
        "design only",
        root=tmp_path,
        dry_run=True,
        plan_only=True,
        max_plan_rounds=1,
    )
    assert rc == 0
    active = load_active_run(tmp_path)
    state = load_pipeline_state(tmp_path, active["run_id"])
    enter = [h["stage"] for h in state["history"] if h.get("event") == "enter"]
    assert "plan" in enter
    assert "implement" not in enter
    assert state.get("status") == "completed"


def test_pipeline_never_sets_allow_env(monkeypatch, tmp_path):
    monkeypatch.delenv("OMG_ALLOW_EXTERNAL_CLI", raising=False)
    launch_envs = []

    def tracking_popen(argv, **kwargs):
        launch_envs.append(kwargs.get("env"))
        raise AssertionError("should not launch in dry_run")

    monkeypatch.setattr(subprocess, "Popen", tracking_popen)

    run_pipeline(
        "secure",
        root=tmp_path,
        dry_run=True,
        skip_plan=True,
        dual_review=False,
        require_acceptance=False,
        max_iter=1,
    )
    # dry_run: no popen; also ensure we never set parent env
    assert "OMG_ALLOW_EXTERNAL_CLI" not in os.environ
    assert launch_envs == []


def test_pipeline_failed_plan_fails_pipeline(tmp_path):
    def fail_plan(**_k):
        return 1

    # Don't use dry_run path that forces plan_ok
    rc = run_pipeline(
        "bad plan",
        root=tmp_path,
        dry_run=False,
        plan_fn=fail_plan,
        dual_review=False,
        max_plan_rounds=1,
        # override plan acceptance: plan_fn returns 1 and no ralplan state
    )
    # Without ralplan state, plan_ok = (rc == 0) → False
    assert rc == 1
    active = load_active_run(tmp_path)
    state = load_pipeline_state(tmp_path, active["run_id"])
    assert state["status"] == "failed"


def test_pipeline_active_mutex(tmp_path):
    create_run(tmp_path, mode="ralph", goal="blocker")
    rc = run_pipeline("second", root=tmp_path, dry_run=True, force=False)
    assert rc == 1


def test_pipeline_with_hooks_no_allow_on_launch(monkeypatch, tmp_path):
    """Implement stage dry_run via real run_mode still never sets allow on parent."""
    monkeypatch.delenv("OMG_ALLOW_EXTERNAL_CLI", raising=False)

    rc = run_pipeline(
        "hooks check",
        root=tmp_path,
        dry_run=True,
        skip_plan=True,
        dual_review=False,
        require_acceptance=False,
        max_iter=1,
        implement="ulw",
    )
    assert rc == 0
    assert "OMG_ALLOW_EXTERNAL_CLI" not in os.environ
    active = load_run(tmp_path, load_active_run(tmp_path)["run_id"])
    assert active.get("verified") is False


def test_pipeline_integrate_with_envelope_mock(monkeypatch, tmp_path):
    """Implement writes envelope → integrate stage runs before dual_review."""
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    integrate_calls: list[dict] = []

    def impl_write_envelope(**_k):
        active = load_active_run(tmp_path)
        assert active is not None
        env_dir = default_envelopes_dir(tmp_path, active["run_id"])
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / "t1.json").write_text(
            json.dumps(
                {
                    "writer": "omg-cli",
                    "run_id": active["run_id"],
                    "task_id": "t1",
                    "base_sha": "abc1234",
                    "head_sha": "def5678",
                    "worktree_path": str(tmp_path),
                    "status": "ok",
                    "changed_files": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return 0

    def fake_integrate(*_a, **kwargs):
        integrate_calls.append(dict(kwargs))
        return {
            "status": "ok",
            "dry_run": bool(kwargs.get("dry_run")),
            "applied": [{"task_id": "t1", "status": "dry_run_ok"}],
            "writer": "omg-cli",
        }

    rc = run_pipeline(
        "with envelope",
        root=tmp_path,
        dry_run=True,
        skip_plan=True,
        dual_review=False,
        require_acceptance=False,
        implement="ralph",
        implement_fn=impl_write_envelope,
        integrate_fn=fake_integrate,
        max_iter=1,
    )
    assert rc == 0
    assert len(integrate_calls) == 1
    active = load_active_run(tmp_path)
    state = load_pipeline_state(tmp_path, active["run_id"])
    enter = [h["stage"] for h in state["history"] if h.get("event") == "enter"]
    assert "integrate" in enter
    assert enter.index("implement") < enter.index("integrate")
    assert state.get("integrate_status") == "ok"
    report = json.loads(report_path(tmp_path, active["run_id"]).read_text(encoding="utf-8"))
    assert report["integrate_status"] == "ok"
    assert report["writer"] == "omg-cli"


def test_pipeline_ulw_missing_envelopes_fails(tmp_path):
    """ULW implement without envelopes → integrate missing → fail (not dry_run)."""

    def ok_impl(**_k):
        return 0

    rc = run_pipeline(
        "ulw no envelopes",
        root=tmp_path,
        dry_run=False,
        skip_plan=True,
        dual_review=False,
        require_acceptance=False,
        implement="ulw",
        implement_fn=ok_impl,
        max_iter=1,
    )
    assert rc == 1
    active = load_active_run(tmp_path)
    state = load_pipeline_state(tmp_path, active["run_id"])
    assert state["status"] == "failed"
    assert state.get("integrate_status") == "missing"
    report = json.loads(report_path(tmp_path, active["run_id"]).read_text(encoding="utf-8"))
    assert report["integrate_status"] == "missing"
    assert report["verified"] is False


def test_pipeline_report_json_on_plan_fail(tmp_path):
    def fail_plan(**_k):
        return 1

    rc = run_pipeline(
        "bad plan report",
        root=tmp_path,
        dry_run=False,
        plan_fn=fail_plan,
        dual_review=False,
        max_plan_rounds=1,
    )
    assert rc == 1
    active = load_active_run(tmp_path)
    rpath = report_path(tmp_path, active["run_id"])
    assert rpath.is_file()
    report = json.loads(rpath.read_text(encoding="utf-8"))
    assert report["writer"] == "omg-cli"
    assert report["status"] == "failed"


def test_pipeline_reintegrate_after_request_changes(tmp_path):
    """AC4: after dual_review REQUEST_CHANGES + re-implement, re-run integrate.

    When resealed ULW envelopes change head_sha, integrate must run again
    before the next dual_review — not leave the new head unintegrated.
    """
    heads = {"n": 0}
    integrated_heads: list[str] = []
    dual_round = {"n": 0}

    def current_env_path() -> Path:
        active = load_active_run(tmp_path)
        assert active is not None
        return default_envelopes_dir(tmp_path, active["run_id"]) / "t1.json"

    def write_envelope(head: str) -> None:
        active = load_active_run(tmp_path)
        assert active is not None
        env_path = current_env_path()
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            json.dumps(
                {
                    "writer": "omg-cli",
                    "run_id": active["run_id"],
                    "task_id": "t1",
                    "base_sha": "base0001",
                    "head_sha": head,
                    "worktree_path": str(tmp_path),
                    "status": "ok",
                    "changed_files": ["a.py"],
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def impl_fn(**_k):
        heads["n"] += 1
        write_envelope(f"head{heads['n']:04d}")
        return 0

    def integrate_fn(*_a, **kwargs):
        env_path = current_env_path()
        data = json.loads(env_path.read_text(encoding="utf-8"))
        head = data["head_sha"]
        integrated_heads.append(head)
        return {
            "status": "ok",
            "dry_run": bool(kwargs.get("dry_run")),
            "applied": [
                {
                    "task_id": "t1",
                    "head_sha": head,
                    "status": "applied",
                    "pick": head,
                }
            ],
            "writer": "omg-cli",
        }

    def dual_fn(**_k):
        dual_round["n"] += 1
        # First review rejects; second (after re-implement + re-integrate) approves
        if dual_round["n"] == 1:
            return "REQUEST_CHANGES"
        return "APPROVE"

    rc = run_pipeline(
        "reintegrate loop",
        root=tmp_path,
        dry_run=False,
        skip_plan=True,
        dual_review=True,
        max_dual_review_rounds=2,
        require_acceptance=False,
        implement="ulw",
        implement_fn=impl_fn,
        integrate_fn=integrate_fn,
        dual_review_fn=dual_fn,
        max_iter=1,
    )
    assert rc == 0
    # implement twice (initial + re-implement) → two distinct heads
    assert heads["n"] == 2
    # integrate must run after EACH implement (not once)
    assert len(integrated_heads) == 2, (
        f"expected 2 integrate calls after re-implement; got {integrated_heads!r}"
    )
    assert integrated_heads[0] == "head0001"
    assert integrated_heads[1] == "head0002"
    # Final integrated head must match final envelope (no stale integrate)
    final_env = json.loads(current_env_path().read_text(encoding="utf-8"))
    assert integrated_heads[-1] == final_env["head_sha"]

    active = load_active_run(tmp_path)
    state = load_pipeline_state(tmp_path, active["run_id"])
    integrate_exits = [
        h
        for h in state["history"]
        if h.get("stage") == "integrate" and h.get("event") == "exit"
    ]
    assert len(integrate_exits) >= 2
    assert any("after-re-implement" in (h.get("detail") or "") for h in integrate_exits)


def test_pipeline_resume_reintegrates_stale_envelopes(tmp_path):
    """Resume after re-implement (before integrate) must re-integrate new heads.

    Simulates crash between re-implement and integrate: history has integrate
    exit + implement exit, stage still dual_review, but envelope head changed.
    """
    run = create_run(tmp_path, mode="pipeline", goal="resume reint")
    rid = run["run_id"]
    env_dir = default_envelopes_dir(tmp_path, rid)
    env_dir.mkdir(parents=True, exist_ok=True)
    env_path = env_dir / "t1.json"
    integrated_heads: list[str] = []

    def write_envelope(head: str) -> None:
        env_path.write_text(
            json.dumps(
                {
                    "writer": "omg-cli",
                    "run_id": rid,
                    "task_id": "t1",
                    "base_sha": "base0001",
                    "head_sha": head,
                    "worktree_path": str(tmp_path),
                    "status": "ok",
                    "changed_files": ["a.py"],
                }
            )
            + "\n",
            encoding="utf-8",
        )

    # First pipeline run: implement + integrate + REQUEST_CHANGES + re-implement
    # then we stop via dual_fn raising after first REQUEST_CHANGES path... 
    # Better: craft pipeline.json manually after partial first run.

    def impl_fn(**_k):
        write_envelope("headAAAA")
        return 0

    def integrate_fn(*_a, **kwargs):
        data = json.loads(env_path.read_text(encoding="utf-8"))
        head = data["head_sha"]
        integrated_heads.append(head)
        return {
            "status": "ok",
            "applied": [{"task_id": "t1", "head_sha": head, "status": "applied"}],
            "writer": "omg-cli",
        }

    dual_calls = {"n": 0}

    def dual_fn(**_k):
        dual_calls["n"] += 1
        if dual_calls["n"] == 1:
            return "REQUEST_CHANGES"
        return "APPROVE"

    # First call: full loop — will re-implement and re-integrate in-process
    # We need crash simulation: run until after re-implement without second integrate.
    # Instead craft state:
    write_envelope("headOLD1")
    from omg_cli.pipeline import initial_pipeline_state, save_pipeline_state

    state = initial_pipeline_state(
        run_id=rid,
        goal="resume reint",
        implement="ulw",
        skip_plan=True,
        dual_review=True,
        max_dual_review_rounds=2,
        require_acceptance=False,
    )
    # History as if: implement, integrate(old), dual REQUEST_CHANGES, re-implement
    # but crash before after-re-implement integrate. Envelope already resealed to NEW.
    write_envelope("headNEW2")
    state["stage"] = "dual_review"
    state["plan_accepted"] = True
    state["skip_plan"] = True
    state["last_integrated_heads"] = ["headold1"]  # stale vs headNEW2
    state["history"] = [
        {"ts": "t0", "stage": "implement", "event": "enter", "detail": ""},
        {"ts": "t1", "stage": "implement", "event": "exit", "detail": "rc=0"},
        {"ts": "t2", "stage": "integrate", "event": "enter", "detail": "after-implement"},
        {"ts": "t3", "stage": "integrate", "event": "exit", "detail": "status=ok"},
        {"ts": "t4", "stage": "dual_review", "event": "enter", "detail": "round=1"},
        {
            "ts": "t5",
            "stage": "dual_review",
            "event": "exit",
            "detail": "verdict=REQUEST_CHANGES",
        },
        {
            "ts": "t6",
            "stage": "implement",
            "event": "enter",
            "detail": "re-implement after REQUEST_CHANGES",
        },
        {"ts": "t7", "stage": "implement", "event": "exit", "detail": "rc=0"},
    ]
    save_pipeline_state(tmp_path, rid, state)
    from omg_cli.state import write_status

    write_status(tmp_path, rid, "running", extra={"stage": "dual_review"})

    # Resume: dual_fn approves on first call of this resume; implement should skip
    # but integrate must run because heads stale
    dual_resume = {"n": 0}

    def dual_resume_fn(**_k):
        dual_resume["n"] += 1
        return "APPROVE"

    def impl_should_not_run(**_k):
        raise AssertionError("implement should be skipped on this resume")

    rc = run_pipeline(
        "resume reint",
        root=tmp_path,
        dry_run=False,
        resume_run_id=rid,
        skip_plan=True,
        dual_review=True,
        max_dual_review_rounds=2,
        require_acceptance=False,
        implement="ulw",
        implement_fn=impl_should_not_run,
        integrate_fn=integrate_fn,
        dual_review_fn=dual_resume_fn,
        max_iter=1,
    )
    assert rc == 0
    assert [h.lower() for h in integrated_heads] == ["headnew2"], (
        f"resume must integrate current envelope head; got {integrated_heads!r}"
    )
    state2 = load_pipeline_state(tmp_path, rid)
    assert state2 is not None
    assert "headnew2" in [str(x).lower() for x in (state2.get("last_integrated_heads") or [])]
