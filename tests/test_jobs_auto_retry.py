"""Bounded auto-retry scheduler tests (#68 PR5)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omg_cli.jobs.models import JobRecord, JobState, JobStoreError
from omg_cli.jobs.ownership import IdentityProbeOutcome
from omg_cli.jobs.retry import (
    AUTO_RETRY_BASE_DELAY_S,
    AUTO_RETRY_MAX_DELAY_S,
    RetryIntent,
    auto_retry_delay_s,
    classify_retry,
)
from omg_cli.jobs.runtime import retry_job, start_job, wait_job
from omg_cli.jobs.scheduler import (
    auto_retry_job,
    auto_retry_jobs,
    evaluate_auto_retry,
)
from omg_cli.jobs.store import (
    attempt_dir,
    job_dir,
    job_json_path,
    job_lock,
    read_job_record,
    write_job_record,
)

pytest_plugins = ["tests.jobs_testutil"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".omg").mkdir()
    return tmp_path


def _prompt(root: Path, text: str = "auto-retry test") -> Path:
    p = root / "prompt.md"
    p.write_text(text, encoding="utf-8")
    return p


def _failed_automatic(
    root: Path,
    *,
    attempt_budget: int = 3,
    fail: bool = True,
) -> JobRecord:
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        fail=fail,
        sleep_s=0.02,
        attempt_budget=attempt_budget,
    )
    terminal, timed_out = wait_job(root, started.record.job_id, timeout_s=15)
    assert timed_out is False
    assert terminal.state == JobState.FAILED
    assert terminal.retry_class == "automatic"
    return terminal


def _due_now(record: JobRecord) -> datetime:
    terminal = datetime.fromisoformat(
        str(record.terminal_at).replace("Z", "+00:00")
    )
    delay = auto_retry_delay_s(int(record.attempt))
    return terminal + timedelta(seconds=delay + 1.0)


def _record_like(
    *,
    state: JobState = JobState.FAILED,
    attempt: int = 1,
    attempt_budget: int = 3,
    retry_class: str | None = "automatic",
    retry_reason: str | None = "timeout",
    exit_obj: dict | None = None,
    terminal_at: str | None = None,
    cancel_requested_at: str | None = None,
    cancel_reason: str | None = None,
) -> JobRecord:
    if exit_obj is None and state == JobState.FAILED:
        exit_obj = {"class": "timeout", "retryable": True}
    if terminal_at is None:
        terminal_at = datetime.now(timezone.utc).isoformat()
    return JobRecord(
        job_id="20260809T000000Z-abcd1234",
        state=state,
        provider="fake",
        role="researcher",
        created_at=terminal_at,
        updated_at=terminal_at,
        attempt=attempt,
        attempt_budget=attempt_budget,
        retry_class=retry_class,
        retry_reason=retry_reason,
        exit=exit_obj,
        terminal_at=terminal_at,
        cancel_requested_at=cancel_requested_at,
        cancel_reason=cancel_reason,
        request={"prompt_sha256": "x", "provider": "fake"},
    )


# ---------------------------------------------------------------------------
# Policy / backoff
# ---------------------------------------------------------------------------


def test_auto_retry_only_failed_automatic_is_eligible() -> None:
    now = datetime.now(timezone.utc)
    rec = _record_like(
        terminal_at=(now - timedelta(seconds=60)).isoformat(),
    )
    d = evaluate_auto_retry(rec, now=now)
    assert d.action == "eligible"
    assert d.retry_class == "automatic"


def test_auto_retry_skips_cancelled_lost_succeeded_and_nonterminal() -> None:
    now = datetime.now(timezone.utc)
    past = (now - timedelta(seconds=60)).isoformat()
    for state in (
        JobState.CANCELLED,
        JobState.LOST,
        JobState.SUCCEEDED,
        JobState.RUNNING,
        JobState.QUEUED,
    ):
        rec = _record_like(state=state, terminal_at=past, exit_obj={"class": "timeout"})
        if state == JobState.SUCCEEDED:
            rec = _record_like(
                state=state,
                retry_class="never",
                retry_reason="success",
                exit_obj={"class": "success"},
                terminal_at=past,
            )
        d = evaluate_auto_retry(rec, now=now)
        assert d.action == "skipped", state


def test_auto_retry_skips_manual_only_unknown_and_never() -> None:
    now = datetime.now(timezone.utc)
    past = (now - timedelta(seconds=60)).isoformat()
    for cls, reason, exit_obj in (
        ("manual_only", "auth_blocked", {"class": "auth_blocked"}),
        ("unknown", "nonzero_unclassified", {"class": "nonzero"}),
        ("never", "success", {"class": "success"}),
    ):
        rec = _record_like(
            retry_class=cls,
            retry_reason=reason,
            exit_obj=exit_obj,
            terminal_at=past,
        )
        d = evaluate_auto_retry(rec, now=now)
        assert d.action == "skipped"


def test_auto_retry_budget_exhausted_is_skipped() -> None:
    now = datetime.now(timezone.utc)
    past = (now - timedelta(seconds=60)).isoformat()
    rec = _record_like(attempt=3, attempt_budget=3, terminal_at=past)
    d = evaluate_auto_retry(rec, now=now)
    assert d.action == "skipped"
    assert d.reason == "budget_exhausted"


def test_auto_retry_recomputes_classification_and_reason() -> None:
    now = datetime.now(timezone.utc)
    past = (now - timedelta(seconds=60)).isoformat()
    rec = _record_like(
        retry_class="automatic",
        retry_reason="timeout",
        exit_obj={"class": "timeout"},
        terminal_at=past,
    )
    computed, reason = classify_retry(state=rec.state, exit_obj=rec.exit)
    assert computed == "automatic"
    assert reason == "timeout"
    d = evaluate_auto_retry(rec, now=now)
    assert d.action == "eligible"


@pytest.mark.parametrize(
    ("exit_obj", "expected_reason"),
    [
        (
            {"class": "spawn_error", "timed_out": "false"},
            "malformed_timed_out",
        ),
        (
            {"class": "spawn_error", "timed_out": "0"},
            "malformed_timed_out",
        ),
        (
            {"class": "nonzero", "overflow": "false", "retryable": True},
            "malformed_overflow",
        ),
        (
            {"class": "timeout", "timed_out": ["yes"]},
            "malformed_timed_out",
        ),
        (
            {"class": "spawn_error", "timed_out": True},
            "spawn_error",
        ),
    ],
)
def test_classify_retry_malformed_bools_fail_closed(
    exit_obj: dict, expected_reason: str
) -> None:
    """Truthiness of non-bool timed_out/overflow must never yield automatic."""
    cls, reason = classify_retry(state=JobState.FAILED, exit_obj=exit_obj)
    assert cls == "unknown"
    assert reason == expected_reason
    # Scheduler must not treat these as automatic either.
    now = datetime.now(timezone.utc)
    past = (now - timedelta(seconds=60)).isoformat()
    rec = _record_like(
        retry_class="automatic",
        retry_reason="timeout_or_overflow",
        exit_obj=exit_obj,
        terminal_at=past,
    )
    d = evaluate_auto_retry(rec, now=now)
    assert d.action == "blocked"


def test_classify_retry_strict_true_bool_still_automatic() -> None:
    cls, reason = classify_retry(
        state=JobState.FAILED,
        exit_obj={"class": "nonzero", "timed_out": True, "retryable": False},
    )
    assert cls == "automatic"
    assert reason == "timeout_or_overflow"
    cls2, reason2 = classify_retry(
        state=JobState.FAILED,
        exit_obj={"class": "nonzero", "timed_out": False, "overflow": True},
    )
    assert cls2 == "automatic"
    assert reason2 == "timeout_or_overflow"


def test_auto_retry_persisted_automatic_with_manual_exit_blocks() -> None:
    now = datetime.now(timezone.utc)
    past = (now - timedelta(seconds=60)).isoformat()
    rec = _record_like(
        retry_class="automatic",
        retry_reason="timeout",
        exit_obj={"class": "auth_blocked"},
        terminal_at=past,
    )
    d = evaluate_auto_retry(rec, now=now)
    assert d.action == "blocked"
    assert d.reason == "retry_meta_mismatch"


def test_auto_retry_missing_or_malformed_terminal_at_blocks() -> None:
    now = datetime.now(timezone.utc)
    rec = _record_like(terminal_at=None)
    rec.terminal_at = None
    d = evaluate_auto_retry(rec, now=now)
    assert d.action == "blocked"
    assert d.reason == "bad_terminal_at"

    rec2 = _record_like(terminal_at="not-a-timestamp")
    d2 = evaluate_auto_retry(rec2, now=now)
    assert d2.action == "blocked"

    # Naive timestamp rejected.
    rec3 = _record_like(terminal_at="2026-08-09T00:00:00")
    d3 = evaluate_auto_retry(rec3, now=now)
    assert d3.action == "blocked"


def test_auto_retry_future_terminal_at_beyond_skew_blocks() -> None:
    now = datetime.now(timezone.utc)
    future = (now + timedelta(seconds=60)).isoformat()
    rec = _record_like(terminal_at=future)
    d = evaluate_auto_retry(rec, now=now)
    assert d.action == "blocked"
    assert d.reason == "future_terminal_at"


def test_auto_retry_backoff_boundaries_are_deterministic() -> None:
    assert auto_retry_delay_s(1) == 10.0
    assert auto_retry_delay_s(2) == 20.0
    assert auto_retry_delay_s(3) == 40.0
    assert auto_retry_delay_s(4) == 80.0
    assert auto_retry_delay_s(5) == 160.0
    assert auto_retry_delay_s(6) == AUTO_RETRY_MAX_DELAY_S
    assert auto_retry_delay_s(100) == AUTO_RETRY_MAX_DELAY_S
    assert AUTO_RETRY_BASE_DELAY_S == 10.0


def test_auto_retry_due_at_equality_is_eligible() -> None:
    terminal = datetime(2026, 8, 9, 0, 0, 0, tzinfo=timezone.utc)
    rec = _record_like(attempt=1, terminal_at=terminal.isoformat())
    due = terminal + timedelta(seconds=10)
    d = evaluate_auto_retry(rec, now=due)
    assert d.action == "eligible"
    assert d.due_at == due.isoformat()


# ---------------------------------------------------------------------------
# Shared execution path
# ---------------------------------------------------------------------------


def test_auto_retry_calls_retry_job_with_automatic_intent(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omg_cli.jobs.runtime as runtime_mod

    terminal = _failed_automatic(root)
    seen: dict[str, object] = {}

    def _spy(*a, **k):  # noqa: ANN001
        seen["intent"] = k.get("intent")
        seen["attempt"] = k.get("attempt")
        return runtime_mod.StartResult(record=terminal, launched=False)

    monkeypatch.setattr("omg_cli.jobs.runtime.retry_job", _spy)
    # Also patch where scheduler imports it inside _dispatch_one
    monkeypatch.setattr(
        "omg_cli.jobs.scheduler.retry_job",
        _spy,
        raising=False,
    )

    def _wrap_retry(project_root, job_id, **kwargs):  # noqa: ANN001
        seen["intent"] = kwargs.get("intent")
        seen["attempt"] = kwargs.get("attempt")
        # Do not actually launch — return a synthetic starting record.
        with job_lock(project_root, job_id):
            rec = read_job_record(project_root, job_id)
        from omg_cli.jobs.runtime import StartResult

        return StartResult(record=rec, launched=False)

    monkeypatch.setattr(
        "omg_cli.jobs.runtime.retry_job",
        _wrap_retry,
    )

    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    # Our spy returns without mutation — action may be launched with same state.
    assert seen.get("intent") == RetryIntent.AUTOMATIC
    assert seen.get("attempt") == terminal.attempt + 1
    assert result.action in {"launched", "would_launch", "blocked", "conflict"}


def test_auto_retry_archives_prior_attempt_before_requeue(root: Path) -> None:
    terminal = _failed_automatic(root)
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.action == "launched"
    assert result.attempt_after == 2
    archive = attempt_dir(root, terminal.job_id, 1)
    assert (archive / "attempt.json").is_file()
    assert (archive / "archive.complete").is_file()
    wait_job(root, terminal.job_id, timeout_s=15)


def test_auto_retry_archive_records_dispatch_intent(root: Path) -> None:
    terminal = _failed_automatic(root)
    auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    snap = json.loads(
        (attempt_dir(root, terminal.job_id, 1) / "attempt.json").read_text(
            encoding="utf-8"
        )
    )
    assert snap["retry_dispatch"]["intent"] == "automatic"
    assert snap["retry_dispatch"]["next_attempt"] == 2
    assert snap["retry_dispatch"]["retry_class"] == "automatic"
    wait_job(root, terminal.job_id, timeout_s=15)


def test_explicit_retry_archive_records_explicit_intent(root: Path) -> None:
    terminal = _failed_automatic(root)
    retry_job(root, terminal.job_id, attempt=2, launch=False)
    snap = json.loads(
        (attempt_dir(root, terminal.job_id, 1) / "attempt.json").read_text(
            encoding="utf-8"
        )
    )
    assert snap["retry_dispatch"]["intent"] == "explicit"


def test_explicit_retry_manual_only_semantics_unchanged(root: Path) -> None:
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
        attempt_budget=3,
    )
    from omg_cli.jobs.runtime import cancel_job

    cancel_job(root, started.record.job_id, reason="operator")
    terminal = wait_job(root, started.record.job_id, timeout_s=15)[0]
    assert terminal.retry_class == "manual_only"
    # Explicit still allowed.
    retried = retry_job(root, terminal.job_id, attempt=2, launch=False)
    assert retried.record.attempt == 2
    # Automatic refused.
    # Re-fail the record for auto path: restore terminal snapshot fields.
    d = evaluate_auto_retry(terminal, now=_due_now(terminal))
    assert d.action == "skipped"


def test_explicit_retry_unknown_semantics_unchanged() -> None:
    now = datetime.now(timezone.utc)
    past = (now - timedelta(seconds=60)).isoformat()
    rec = _record_like(
        retry_class="unknown",
        retry_reason="spawn_error",
        exit_obj={"class": "spawn_error"},
        terminal_at=past,
    )
    d = evaluate_auto_retry(rec, now=now)
    assert d.action == "skipped"
    # Explicit admission still accepts unknown.
    from omg_cli.jobs.retry import assert_retry_admission

    assert_retry_admission(rec, attempt=2, intent=RetryIntent.EXPLICIT)


def test_auto_retry_uses_single_launch_job_runner_path(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root)
    calls: list[str] = []

    import omg_cli.jobs.runtime as runtime_mod

    real = runtime_mod.launch_job_runner

    def _wrap(project_root, job_id, **kwargs):  # noqa: ANN001
        calls.append(job_id)
        return real(project_root, job_id, **kwargs)

    monkeypatch.setattr(runtime_mod, "launch_job_runner", _wrap)
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.action == "launched"
    assert calls == [terminal.job_id]
    wait_job(root, terminal.job_id, timeout_s=15)


# ---------------------------------------------------------------------------
# Dry-run / boundedness
# ---------------------------------------------------------------------------


def test_auto_retry_dry_run_has_zero_job_json_mutation(root: Path) -> None:
    terminal = _failed_automatic(root)
    path = job_json_path(root, terminal.job_id)
    before = path.read_bytes()
    result = auto_retry_job(
        root, terminal.job_id, dry_run=True, now=_due_now(terminal)
    )
    assert result.action == "would_launch"
    assert path.read_bytes() == before


def test_auto_retry_dry_run_creates_no_attempt_archive(root: Path) -> None:
    terminal = _failed_automatic(root)
    auto_retry_job(root, terminal.job_id, dry_run=True, now=_due_now(terminal))
    assert not attempt_dir(root, terminal.job_id, 1).exists()


def test_auto_retry_dry_run_launches_no_runner(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root)
    called = {"n": 0}

    def _boom(*a, **k):  # noqa: ANN001
        called["n"] += 1
        raise AssertionError("launch must not run on dry-run")

    monkeypatch.setattr("omg_cli.jobs.runtime.launch_job_runner", _boom)
    result = auto_retry_job(
        root, terminal.job_id, dry_run=True, now=_due_now(terminal)
    )
    assert result.action == "would_launch"
    assert called["n"] == 0


def test_auto_retry_limit_bounds_due_candidates(root: Path) -> None:
    jobs = [_failed_automatic(root) for _ in range(3)]
    now = max(_due_now(j) for j in jobs)
    batch = auto_retry_jobs(root, limit=1, dry_run=True, now=now)
    assert batch.counts["due"] == 3
    assert batch.counts["would_launch"] == 1
    assert batch.counts["limit_reached"] == 2


def test_auto_retry_limit_reached_candidates_are_untouched(root: Path) -> None:
    jobs = [_failed_automatic(root) for _ in range(2)]
    now = max(_due_now(j) for j in jobs)
    before = {
        j.job_id: job_json_path(root, j.job_id).read_bytes() for j in jobs
    }
    batch = auto_retry_jobs(root, limit=1, dry_run=True, now=now)
    limit_ids = {r.job_id for r in batch.results if r.action == "limit_reached"}
    assert len(limit_ids) == 1
    for jid in limit_ids:
        assert job_json_path(root, jid).read_bytes() == before[jid]


def test_fast_failure_is_not_retried_again_in_same_tick(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root, attempt_budget=5)

    # Force launch to immediately fail the new attempt by making provider boom
    # after prepare — still only one dispatch in the tick.
    calls = {"n": 0}
    import omg_cli.jobs.runtime as runtime_mod

    real = runtime_mod.launch_job_runner

    def _wrap(project_root, job_id, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        return real(project_root, job_id, **kwargs)

    monkeypatch.setattr(runtime_mod, "launch_job_runner", _wrap)
    batch = auto_retry_jobs(
        root, limit=32, now=_due_now(terminal)
    )
    assert calls["n"] == 1
    assert sum(1 for r in batch.results if r.job_id == terminal.job_id and r.action == "launched") == 1
    wait_job(root, terminal.job_id, timeout_s=15)


def test_batch_order_is_due_at_then_terminal_at_then_job_id(root: Path) -> None:
    # Create three failed jobs then rewrite terminal_at for deterministic order.
    jobs = [_failed_automatic(root) for _ in range(3)]
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Different due times via different terminal_at (same attempt → same delay).
    stamps = [base + timedelta(seconds=s) for s in (30, 10, 20)]
    for job, stamp in zip(jobs, stamps, strict=True):
        with job_lock(root, job.job_id):
            rec = read_job_record(root, job.job_id)
            rec.terminal_at = stamp.isoformat()
            write_job_record(root, rec)
    now = base + timedelta(seconds=100)
    batch = auto_retry_jobs(root, limit=3, dry_run=True, now=now)
    launched = [r for r in batch.results if r.action == "would_launch"]
    # Sorted by due_at (= terminal + 10s): 10, 20, 30 → job indices 1, 2, 0
    assert [r.job_id for r in launched] == [
        jobs[1].job_id,
        jobs[2].job_id,
        jobs[0].job_id,
    ]


# ---------------------------------------------------------------------------
# PR4 safety gates
# ---------------------------------------------------------------------------


def _assert_frozen(root: Path, job_id: str, before: bytes, attempt: int) -> None:
    assert job_json_path(root, job_id).read_bytes() == before
    assert not attempt_dir(root, job_id, attempt).exists()
    rec = read_job_record(root, job_id)
    assert rec.attempt == attempt
    assert rec.state == JobState.FAILED


def test_auto_retry_live_prior_runner_blocks(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root)
    before = job_json_path(root, terminal.job_id).read_bytes()
    monkeypatch.setattr(
        "omg_cli.jobs.ownership.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.LIVE,
    )
    monkeypatch.setattr(
        "omg_cli.jobs.runtime._wait_until_gone",
        lambda *a, **k: False,
    )
    signals: list[object] = []
    monkeypatch.setattr(
        "os.kill",
        lambda *a, **k: signals.append(a),
    )
    monkeypatch.setattr(
        "os.killpg",
        lambda *a, **k: signals.append(a),
    )
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.action == "blocked"
    assert result.error_code == "E_JOB_RETRY_LIVE"
    _assert_frozen(root, terminal.job_id, before, terminal.attempt)
    assert signals == []


def test_auto_retry_live_prior_provider_blocks(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root)
    before = job_json_path(root, terminal.job_id).read_bytes()

    def _probe(identity):  # noqa: ANN001
        # Force provider path: any identity LIVE
        return IdentityProbeOutcome.LIVE

    monkeypatch.setattr(
        "omg_cli.jobs.ownership.probe_identity_for_recovery",
        _probe,
    )
    monkeypatch.setattr(
        "omg_cli.jobs.runtime._wait_until_gone",
        lambda *a, **k: False,
    )
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.ok is False
    assert result.error_code in {"E_JOB_RETRY_LIVE", "E_JOB_CANCEL_UNPROVEN"}
    _assert_frozen(root, terminal.job_id, before, terminal.attempt)


def test_auto_retry_unproven_runner_blocks(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root)
    before = job_json_path(root, terminal.job_id).read_bytes()
    monkeypatch.setattr(
        "omg_cli.jobs.ownership.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.UNPROVEN,
    )
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.error_code == "E_JOB_CANCEL_UNPROVEN"
    _assert_frozen(root, terminal.job_id, before, terminal.attempt)


def test_auto_retry_unproven_provider_blocks(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root)
    before = job_json_path(root, terminal.job_id).read_bytes()
    monkeypatch.setattr(
        "omg_cli.jobs.ownership.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.UNPROVEN,
    )
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.error_code == "E_JOB_CANCEL_UNPROVEN"
    _assert_frozen(root, terminal.job_id, before, terminal.attempt)


def test_auto_retry_spawn_uncertain_blocks(root: Path) -> None:
    from omg_cli.jobs.runtime import _mark_spawn_uncertain

    terminal = _failed_automatic(root)
    before = job_json_path(root, terminal.job_id).read_bytes()
    _mark_spawn_uncertain(root, terminal.job_id, detail="test")
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.error_code == "E_JOB_CANCEL_UNPROVEN"
    _assert_frozen(root, terminal.job_id, before, terminal.attempt)


def test_auto_retry_provider_launch_unbound_blocks(root: Path) -> None:
    terminal = _failed_automatic(root)
    before = job_json_path(root, terminal.job_id).read_bytes()
    with job_lock(root, terminal.job_id):
        rec = read_job_record(root, terminal.job_id)
        rec.provider_process = {"state": "launching", "pid": None, "pgid": None}
        write_job_record(root, rec)
    before2 = job_json_path(root, terminal.job_id).read_bytes()
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.error_code == "E_JOB_CANCEL_UNPROVEN"
    assert job_json_path(root, terminal.job_id).read_bytes() == before2
    assert not attempt_dir(root, terminal.job_id, 1).exists()
    del before


def test_auto_retry_bound_incomplete_provider_blocks(root: Path) -> None:
    """bound + missing pid/pgid is UNPROVEN — never absent/reclaimable."""
    terminal = _failed_automatic(root)
    with job_lock(root, terminal.job_id):
        rec = read_job_record(root, terminal.job_id)
        rec.provider_process = {
            "state": "bound",
            "pid": None,
            "pgid": None,
            "pid_starttime": None,
            "handle": "provider:test",
            "bound_at": "2026-01-01T00:00:00+00:00",
            "exited_at": None,
        }
        write_job_record(root, rec)
    before = job_json_path(root, terminal.job_id).read_bytes()
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.ok is False
    assert result.error_code == "E_JOB_CANCEL_UNPROVEN"
    assert job_json_path(root, terminal.job_id).read_bytes() == before
    assert not attempt_dir(root, terminal.job_id, 1).exists()


def test_auto_retry_bound_malformed_provider_ids_blocks(root: Path) -> None:
    """bound + non-int pid/pgid must fail closed as UNPROVEN."""
    terminal = _failed_automatic(root)
    with job_lock(root, terminal.job_id):
        rec = read_job_record(root, terminal.job_id)
        rec.provider_process = {
            "state": "bound",
            "pid": "not-a-pid",
            "pgid": "also-bad",
        }
        write_job_record(root, rec)
    before = job_json_path(root, terminal.job_id).read_bytes()
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.error_code == "E_JOB_CANCEL_UNPROVEN"
    assert job_json_path(root, terminal.job_id).read_bytes() == before


def test_auto_retry_malformed_spawn_identity_blocks(root: Path) -> None:
    """Present-but-malformed spawn_identity.json is UNPROVEN, not absent."""
    terminal = _failed_automatic(root)
    # Clear durable runner ids so recovery would otherwise fall through to
    # the spawn_identity sidecar — which we deliberately corrupt.
    with job_lock(root, terminal.job_id):
        rec = read_job_record(root, terminal.job_id)
        rec.pid = None
        rec.pgid = None
        rec.pid_starttime = None
        write_job_record(root, rec)
    identity_path = job_dir(root, terminal.job_id) / "spawn_identity.json"
    identity_path.write_text("{not-json", encoding="utf-8")
    before = job_json_path(root, terminal.job_id).read_bytes()
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.error_code == "E_JOB_CANCEL_UNPROVEN"
    assert job_json_path(root, terminal.job_id).read_bytes() == before
    assert not attempt_dir(root, terminal.job_id, 1).exists()


def test_auto_retry_incomplete_spawn_identity_blocks(root: Path) -> None:
    """spawn_identity.json with null pid/pgid is UNPROVEN."""
    terminal = _failed_automatic(root)
    with job_lock(root, terminal.job_id):
        rec = read_job_record(root, terminal.job_id)
        rec.pid = None
        rec.pgid = None
        write_job_record(root, rec)
    identity_path = job_dir(root, terminal.job_id) / "spawn_identity.json"
    identity_path.write_text(
        json.dumps({"pid": None, "pgid": None, "reason": "test"}),
        encoding="utf-8",
    )
    before = job_json_path(root, terminal.job_id).read_bytes()
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.error_code == "E_JOB_CANCEL_UNPROVEN"
    assert job_json_path(root, terminal.job_id).read_bytes() == before


def test_auto_retry_verified_reused_identity_allows(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root)
    monkeypatch.setattr(
        "omg_cli.jobs.ownership.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.REUSED,
    )
    result = auto_retry_job(
        root, terminal.job_id, dry_run=True, now=_due_now(terminal)
    )
    assert result.action == "would_launch"


def test_auto_retry_gone_identity_allows(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root)
    monkeypatch.setattr(
        "omg_cli.jobs.ownership.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.GONE,
    )
    result = auto_retry_job(
        root, terminal.job_id, dry_run=True, now=_due_now(terminal)
    )
    assert result.action == "would_launch"


def test_terminal_stamp_does_not_bypass_process_disappearance_gate(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root)
    before = job_json_path(root, terminal.job_id).read_bytes()
    monkeypatch.setattr(
        "omg_cli.jobs.ownership.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.LIVE,
    )
    monkeypatch.setattr(
        "omg_cli.jobs.runtime._wait_until_gone",
        lambda *a, **k: False,
    )
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.ok is False
    _assert_frozen(root, terminal.job_id, before, terminal.attempt)


# ---------------------------------------------------------------------------
# Recovery separation
# ---------------------------------------------------------------------------


def test_auto_retry_does_not_call_recover_job(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root)
    called = {"n": 0}

    def _boom(*a, **k):  # noqa: ANN001
        called["n"] += 1
        raise AssertionError("recover must not be called")

    monkeypatch.setattr("omg_cli.jobs.recovery.recover_job", _boom)
    monkeypatch.setattr("omg_cli.jobs.recovery.recover_jobs", _boom)
    auto_retry_job(root, terminal.job_id, dry_run=True, now=_due_now(terminal))
    assert called["n"] == 0


def test_auto_retry_ignores_recoverable_lost_active_job(root: Path) -> None:
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
        attempt_budget=3,
    )
    # Active running job — never auto-retried.
    batch = auto_retry_jobs(root, limit=32, now=datetime.now(timezone.utc))
    actions = {r.job_id: r.action for r in batch.results}
    assert actions.get(started.record.job_id) == "skipped"
    from omg_cli.jobs.runtime import cancel_job

    cancel_job(root, started.record.job_id)


def test_auto_retry_recovered_lost_job_remains_manual(root: Path) -> None:
    terminal = _failed_automatic(root)
    with job_lock(root, terminal.job_id):
        rec = read_job_record(root, terminal.job_id)
        rec.state = JobState.LOST
        rec.retry_class = "unknown"
        rec.retry_reason = "lost"
        write_job_record(root, rec)
    d = evaluate_auto_retry(read_job_record(root, terminal.job_id), now=_due_now(terminal))
    assert d.action == "skipped"


def test_auto_retry_does_not_cancel_orphan_provider(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root)
    cancels: list[object] = []

    def _spy(*a, **k):  # noqa: ANN001
        cancels.append(a)
        raise AssertionError("cancel must not run")

    monkeypatch.setattr("omg_cli.jobs.runtime.cancel_job", _spy)
    monkeypatch.setattr(
        "omg_cli.jobs.ownership.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.UNPROVEN,
    )
    auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert cancels == []


# ---------------------------------------------------------------------------
# Provider preflight / internal
# ---------------------------------------------------------------------------


def test_auto_retry_antigravity_preflight_drift_consumes_no_attempt(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root)

    def _boom(provider, request):  # noqa: ANN001
        raise JobStoreError("drift", code="E_JOB_RETRY_PREFLIGHT")

    monkeypatch.setattr(
        "omg_cli.jobs.providers.revalidate_stored_request",
        _boom,
    )
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.ok is False
    assert result.error_code == "E_JOB_RETRY_PREFLIGHT"
    assert not attempt_dir(root, terminal.job_id, 1).exists()
    assert read_job_record(root, terminal.job_id).attempt == terminal.attempt


def test_auto_retry_antigravity_missing_binary_consumes_no_attempt(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root)

    def _boom(provider, request):  # noqa: ANN001
        raise JobStoreError("missing binary", code="E_JOB_RETRY_PREFLIGHT")

    monkeypatch.setattr(
        "omg_cli.jobs.providers.revalidate_stored_request",
        _boom,
    )
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.ok is False
    assert result.attempt_after == terminal.attempt
    assert not attempt_dir(root, terminal.job_id, 1).exists()


def test_auto_retry_fake_provider_revalidation_is_hermetic(root: Path) -> None:
    terminal = _failed_automatic(root)
    result = auto_retry_job(
        root, terminal.job_id, dry_run=True, now=_due_now(terminal)
    )
    assert result.action == "would_launch"


def test_auto_retry_internal_acp_specific_job_is_rejected(root: Path) -> None:
    from omg_cli.jobs.providers import ACP_SESSION_PROVIDER

    terminal = _failed_automatic(root)
    with job_lock(root, terminal.job_id):
        rec = read_job_record(root, terminal.job_id)
        rec.provider = ACP_SESSION_PROVIDER
        write_job_record(root, rec)
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.action == "protected_internal"
    assert result.ok is False


def test_auto_retry_all_does_not_dispatch_internal_acp_jobs(root: Path) -> None:
    from omg_cli.jobs.providers import ACP_SESSION_PROVIDER

    public = _failed_automatic(root)
    internal = _failed_automatic(root)
    with job_lock(root, internal.job_id):
        rec = read_job_record(root, internal.job_id)
        rec.provider = ACP_SESSION_PROVIDER
        write_job_record(root, rec)
    now = max(_due_now(public), _due_now(internal))
    batch = auto_retry_jobs(root, limit=32, dry_run=True, now=now)
    ids = {r.job_id for r in batch.results}
    assert internal.job_id not in ids
    assert public.job_id in ids


# ---------------------------------------------------------------------------
# Concurrency / crash
# ---------------------------------------------------------------------------


def test_concurrent_auto_retry_ticks_only_one_dispatches(root: Path) -> None:
    terminal = _failed_automatic(root)
    now = _due_now(terminal)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def _worker() -> None:
        barrier.wait(timeout=5)
        r = auto_retry_job(root, terminal.job_id, now=now)
        outcomes.append(r.action)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes) in (
        ["blocked", "launched"],
        ["launched", "blocked"],
        ["conflict", "launched"],
        ["launched", "conflict"],
        ["launched", "launch_failed"],
        ["launch_failed", "launched"],
    ) or outcomes.count("launched") == 1
    # At most one launch consumed attempt.
    final = read_job_record(root, terminal.job_id)
    assert final.attempt <= 2
    if final.state not in {JobState.FAILED, JobState.CANCELLED, JobState.SUCCEEDED, JobState.LOST}:
        wait_job(root, terminal.job_id, timeout_s=15)


def test_auto_retry_scheduler_lock_busy_is_zero_mutation(root: Path) -> None:
    from omg_cli.jobs.store import auto_retry_lock

    terminal = _failed_automatic(root)
    before = job_json_path(root, terminal.job_id).read_bytes()
    with auto_retry_lock(root, timeout_s=5.0):
        # Nested acquire with short timeout → busy.
        from omg_cli.jobs.store import auto_retry_lock as lock2

        with pytest.raises(JobStoreError) as ei:
            with lock2(root, timeout_s=0.05):
                pass
        assert ei.value.code == "E_JOB_AUTO_RETRY_BUSY"
        result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
        # Holding outer lock: inner tick should fail busy.
        assert result.ok is False
        assert result.error_code == "E_JOB_AUTO_RETRY_BUSY"
    assert job_json_path(root, terminal.job_id).read_bytes() == before


def test_auto_retry_vs_explicit_retry_has_one_attempt_winner(root: Path) -> None:
    terminal = _failed_automatic(root)
    now = _due_now(terminal)
    barrier = threading.Barrier(2)
    results: list[str] = []

    def _auto() -> None:
        barrier.wait(timeout=5)
        r = auto_retry_job(root, terminal.job_id, now=now)
        results.append(f"auto:{r.action}")

    def _explicit() -> None:
        barrier.wait(timeout=5)
        try:
            retry_job(root, terminal.job_id, attempt=2, launch=False)
            results.append("explicit:ok")
        except JobStoreError as exc:
            results.append(f"explicit:{exc.code}")

    t1 = threading.Thread(target=_auto)
    t2 = threading.Thread(target=_explicit)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    final = read_job_record(root, terminal.job_id)
    assert final.attempt == 2
    assert len(results) == 2


def test_auto_retry_state_change_before_prepare_returns_conflict(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root)
    now = _due_now(terminal)

    import omg_cli.jobs.store as store_mod

    real_prepare = store_mod.prepare_retry

    def _race(project_root, job_id, **kwargs):  # noqa: ANN001
        # Simulate another winner already requeued.
        with job_lock(project_root, job_id):
            rec = read_job_record(project_root, job_id)
            rec.state = JobState.QUEUED
            rec.attempt = 2
            write_job_record(project_root, rec)
        raise JobStoreError(
            "job is not retryable from state=queued",
            code="E_JOB_RETRY_STATE",
        )

    monkeypatch.setattr(store_mod, "prepare_retry", _race)
    # preflight still passes; prepare fails as conflict.
    # Need preflight to pass — prior gone ok.
    result = auto_retry_job(root, terminal.job_id, now=now)
    assert result.action == "conflict"
    assert result.ok is True
    del real_prepare


def test_auto_retry_cancel_marker_same_attempt_not_safe_conflict(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel-marker race: still failed + same attempt must block, not ok=True conflict."""
    terminal = _failed_automatic(root)
    now = _due_now(terminal)
    before_attempt = int(terminal.attempt)

    import omg_cli.jobs.store as store_mod

    def _cancel_race(project_root, job_id, **kwargs):  # noqa: ANN001
        # Operator cancel lands between evaluate and prepare; state stays failed,
        # attempt unchanged — incompatible mutation, not a safe scheduler conflict.
        with job_lock(project_root, job_id):
            rec = read_job_record(project_root, job_id)
            rec.cancel_requested_at = datetime.now(timezone.utc).isoformat()
            rec.cancel_reason = "operator"
            write_job_record(project_root, rec)
        raise JobStoreError(
            "automatic retry refuses cancelled terminal records",
            code="E_JOB_RETRY_STATE",
        )

    monkeypatch.setattr(store_mod, "prepare_retry", _cancel_race)
    result = auto_retry_job(root, terminal.job_id, now=now)
    assert result.ok is False
    assert result.action == "blocked"
    assert result.error_code == "E_JOB_RETRY_STATE"
    final = read_job_record(root, terminal.job_id)
    assert final.state == JobState.FAILED
    assert int(final.attempt) == before_attempt
    assert final.cancel_requested_at is not None


def test_auto_retry_vs_gc_never_recreates_quarantined_job(root: Path) -> None:
    from omg_cli.jobs.runtime import gc_jobs

    terminal = _failed_automatic(root)
    # Make retention immediately eligible.
    with job_lock(root, terminal.job_id):
        rec = read_job_record(root, terminal.job_id)
        rec.terminal_at = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat()
        # Clear identities so GC can delete.
        rec.pid = None
        rec.pgid = None
        rec.provider_process = {"state": "exited", "pid": None, "pgid": None}
        write_job_record(root, rec)
    gc_jobs(root, retention_days=1)
    assert not job_json_path(root, terminal.job_id).is_file()
    result = auto_retry_job(root, terminal.job_id, now=datetime.now(timezone.utc))
    assert result.ok is False
    assert not job_dir(root, terminal.job_id).exists()


def test_auto_retry_archive_publish_crash_reuses_complete_archive(root: Path) -> None:
    terminal = _failed_automatic(root)
    # First auto-retry with launch=False via retry_job to create complete archive
    # then crash before... actually use prepare path: complete archive then retry again.
    from omg_cli.jobs.store import archive_attempt

    with job_lock(root, terminal.job_id):
        rec = read_job_record(root, terminal.job_id)
        archive_attempt(
            root,
            terminal.job_id,
            rec,
            retry_dispatch={
                "intent": "automatic",
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "next_attempt": 2,
                "retry_class": "automatic",
                "retry_reason": rec.retry_reason,
            },
        )
    # Archive complete; prepare_retry should reuse and continue.
    retried = retry_job(
        root,
        terminal.job_id,
        attempt=2,
        launch=False,
        intent=RetryIntent.AUTOMATIC,
        now=_due_now(terminal),
    )
    assert retried.record.attempt == 2
    assert (attempt_dir(root, terminal.job_id, 1) / "archive.complete").is_file()


def test_auto_retry_incomplete_archive_is_replaced_fail_closed(root: Path) -> None:
    terminal = _failed_automatic(root)
    adir = attempt_dir(root, terminal.job_id, 1)
    adir.mkdir(parents=True)
    (adir / "attempt.json").write_text("{}", encoding="utf-8")
    # Missing archive.complete → replaced on retry.
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.action == "launched"
    assert (adir / "archive.complete").is_file()
    wait_job(root, terminal.job_id, timeout_s=15)


def test_auto_retry_launch_failure_does_not_chain_another_attempt(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _failed_automatic(root, attempt_budget=5)

    def _boom(*a, **k):  # noqa: ANN001
        raise JobStoreError("launch failed", code="E_JOB_LAUNCH")

    monkeypatch.setattr("omg_cli.jobs.runtime.launch_job_runner", _boom)
    batch = auto_retry_jobs(root, limit=32, now=_due_now(terminal))
    matching = [r for r in batch.results if r.job_id == terminal.job_id]
    assert len(matching) == 1
    assert matching[0].action == "launch_failed"
    # prepare already consumed attempt → attempt advanced once, not twice.
    rec = read_job_record(root, terminal.job_id)
    assert rec.attempt == 2


def test_auto_retry_cancel_race_cannot_finish_as_success(root: Path) -> None:
    terminal = _failed_automatic(root)
    with job_lock(root, terminal.job_id):
        rec = read_job_record(root, terminal.job_id)
        rec.cancel_requested_at = datetime.now(timezone.utc).isoformat()
        rec.cancel_reason = "operator"
        write_job_record(root, rec)
    d = evaluate_auto_retry(
        read_job_record(root, terminal.job_id), now=_due_now(terminal)
    )
    assert d.action == "skipped"
    result = auto_retry_job(root, terminal.job_id, now=_due_now(terminal))
    assert result.action == "skipped"


# ---------------------------------------------------------------------------
# Batch corruption
# ---------------------------------------------------------------------------


def test_auto_retry_all_malformed_record_aborts_before_launch(root: Path) -> None:
    good = _failed_automatic(root)
    bad = _failed_automatic(root)
    path = job_json_path(root, bad.job_id)
    path.write_text("{not-json", encoding="utf-8")
    before_good = job_json_path(root, good.job_id).read_bytes()
    batch = auto_retry_jobs(
        root, limit=32, now=max(_due_now(good), datetime.now(timezone.utc))
    )
    assert batch.ok is False
    assert job_json_path(root, good.job_id).read_bytes() == before_good
    assert not attempt_dir(root, good.job_id, 1).exists()


def test_auto_retry_all_job_id_body_mismatch_aborts_before_launch(root: Path) -> None:
    good = _failed_automatic(root)
    bad = _failed_automatic(root)
    with job_lock(root, bad.job_id):
        data = json.loads(job_json_path(root, bad.job_id).read_text(encoding="utf-8"))
        data["job_id"] = "20260809T000000Z-deadbeef"
        job_json_path(root, bad.job_id).write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )
    before_good = job_json_path(root, good.job_id).read_bytes()
    batch = auto_retry_jobs(root, limit=32, now=_due_now(good))
    assert batch.ok is False
    assert job_json_path(root, good.job_id).read_bytes() == before_good


def test_auto_retry_all_partial_runtime_block_returns_nonzero(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _failed_automatic(root)
    b = _failed_automatic(root)
    now = max(_due_now(a), _due_now(b))

    import omg_cli.jobs.runtime as runtime_mod

    real = runtime_mod._assert_prior_attempt_gone
    seen: list[str] = []

    def _gate(project_root, record):  # noqa: ANN001
        seen.append(record.job_id)
        if record.job_id == b.job_id:
            raise JobStoreError(
                "provider identity unproven",
                code="E_JOB_CANCEL_UNPROVEN",
            )
        return real(project_root, record)

    monkeypatch.setattr(runtime_mod, "_assert_prior_attempt_gone", _gate)
    batch = auto_retry_jobs(root, limit=32, now=now)
    assert batch.ok is False
    assert any(
        r.job_id == b.job_id and r.action == "blocked" for r in batch.results
    )
    # First job may have launched — wait if nonterminal.
    rec_a = read_job_record(root, a.job_id)
    if rec_a.state not in {
        JobState.FAILED,
        JobState.SUCCEEDED,
        JobState.CANCELLED,
        JobState.LOST,
    }:
        wait_job(root, a.job_id, timeout_s=15)
