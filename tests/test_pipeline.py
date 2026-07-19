"""Tests for omg pipeline FSM — dry-run stage order, no allow env, hooks."""
from __future__ import annotations

import os
import subprocess

from omg_cli.pipeline import load_pipeline_state, run_pipeline
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
    stages = [h["stage"] for h in state["history"] if h.get("event") in ("enter", "exit")]
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

    real_popen = subprocess.Popen

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
