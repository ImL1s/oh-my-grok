"""Bounded auto-retry scheduler tick (#68 PR5).

Caller-driven one-pass scheduler over the existing ``retry_job`` path.
Never sleeps, never recovers active jobs, never signals processes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from omg_cli.jobs.models import (
    RETRY_CLASS_AUTOMATIC,
    JobRecord,
    JobState,
    JobStoreError,
)
from omg_cli.jobs.providers import ACP_SESSION_PROVIDER
from omg_cli.jobs.retry import (
    AUTO_RETRY_BASE_DELAY_S,
    AUTO_RETRY_CLOCK_SKEW_S,
    AUTO_RETRY_MAX_DELAY_S,
    RetryIntent,
    auto_retry_due_at,
    classify_retry,
    parse_terminal_at,
)
from omg_cli.jobs.store import (
    auto_retry_lock,
    list_job_ids,
    read_job_record,
    safe_job_id,
)

DEFAULT_AUTO_RETRY_LIMIT = 1
MAX_AUTO_RETRY_LIMIT = 32
AUTO_RETRY_LOCK_TIMEOUT_S = 5.0

_NONTERMINAL = frozenset(
    {JobState.QUEUED, JobState.STARTING, JobState.RUNNING}
)


@dataclass(frozen=True, slots=True)
class AutoRetryDecision:
    action: str
    reason: str
    due_at: str | None
    next_attempt: int | None
    retry_class: str | None
    retry_reason: str | None


@dataclass(frozen=True, slots=True)
class AutoRetryResult:
    ok: bool
    job_id: str
    action: str
    reason: str
    dry_run: bool
    before_state: str | None
    after_state: str | None
    attempt_before: int | None
    attempt_after: int | None
    attempt_budget: int | None
    retry_class: str | None
    retry_reason: str | None
    due_at: str | None
    error_code: str | None = None
    error_message: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "action": self.action,
            "reason": self.reason,
            "dry_run": bool(self.dry_run),
            "before_state": self.before_state,
            "after_state": self.after_state,
            "attempt_before": self.attempt_before,
            "attempt_after": self.attempt_after,
            "attempt_budget": self.attempt_budget,
            "retry_class": self.retry_class,
            "retry_reason": self.retry_reason,
            "due_at": self.due_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class AutoRetryBatchResult:
    ok: bool
    dry_run: bool
    limit: int
    results: list[AutoRetryResult]
    scanned: int
    considered: int
    due: int

    @property
    def counts(self) -> dict[str, int]:
        launched = would_launch = deferred = skipped = blocked = 0
        conflicts = launch_failed = limit_reached = processed = 0
        for r in self.results:
            action = r.action
            if action == "launched":
                launched += 1
                processed += 1
            elif action == "would_launch":
                would_launch += 1
                processed += 1
            elif action == "deferred":
                deferred += 1
            elif action == "skipped":
                skipped += 1
            elif action == "blocked":
                blocked += 1
                if r.reason == "automatic_retry_due":
                    processed += 1
            elif action == "conflict":
                conflicts += 1
                processed += 1
            elif action == "launch_failed":
                launch_failed += 1
                processed += 1
            elif action == "limit_reached":
                limit_reached += 1
            elif action == "protected_internal":
                blocked += 1
        return {
            "scanned": int(self.scanned),
            "considered": int(self.considered),
            "due": int(self.due),
            "processed": int(processed),
            "launched": int(launched),
            "would_launch": int(would_launch),
            "deferred": int(deferred),
            "skipped": int(skipped),
            "blocked": int(blocked),
            "conflicts": int(conflicts),
            "launch_failed": int(launch_failed),
            "limit_reached": int(limit_reached),
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "dry_run": bool(self.dry_run),
            "limit": int(self.limit),
            "counts": self.counts,
            "results": [r.to_public_dict() for r in self.results],
        }


def _coerce_now(now: datetime | None) -> datetime:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def validate_auto_retry_limit(limit: int) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError) as exc:
        raise JobStoreError(
            f"invalid auto-retry limit {limit!r}",
            code="E_JOB_AUTO_RETRY_LIMIT",
        ) from exc
    if n < 1 or n > MAX_AUTO_RETRY_LIMIT:
        raise JobStoreError(
            f"auto-retry limit must be 1..{MAX_AUTO_RETRY_LIMIT}; got {n}",
            code="E_JOB_AUTO_RETRY_LIMIT",
        )
    return n


def evaluate_auto_retry(
    record: JobRecord,
    *,
    now: datetime,
) -> AutoRetryDecision:
    """Pure metadata decision — no process probing and no mutation."""
    tick = _coerce_now(now)
    retry_class = record.retry_class
    retry_reason = record.retry_reason

    if record.state != JobState.FAILED:
        return AutoRetryDecision(
            action="skipped",
            reason=f"state_{record.state.value}",
            due_at=None,
            next_attempt=None,
            retry_class=retry_class,
            retry_reason=retry_reason,
        )

    if record.cancel_requested_at is not None or record.cancel_reason is not None:
        return AutoRetryDecision(
            action="skipped",
            reason="cancel_marker_present",
            due_at=None,
            next_attempt=None,
            retry_class=retry_class,
            retry_reason=retry_reason,
        )

    next_attempt = int(record.attempt) + 1
    if next_attempt > int(record.attempt_budget):
        return AutoRetryDecision(
            action="skipped",
            reason="budget_exhausted",
            due_at=None,
            next_attempt=next_attempt,
            retry_class=retry_class,
            retry_reason=retry_reason,
        )

    if retry_class != RETRY_CLASS_AUTOMATIC:
        return AutoRetryDecision(
            action="skipped",
            reason=f"retry_class_{retry_class or 'missing'}",
            due_at=None,
            next_attempt=next_attempt,
            retry_class=retry_class,
            retry_reason=retry_reason,
        )

    if not isinstance(retry_reason, str) or not retry_reason.strip():
        return AutoRetryDecision(
            action="blocked",
            reason="missing_retry_reason",
            due_at=None,
            next_attempt=next_attempt,
            retry_class=retry_class,
            retry_reason=retry_reason,
        )

    if not isinstance(record.exit, dict):
        return AutoRetryDecision(
            action="blocked",
            reason="missing_exit",
            due_at=None,
            next_attempt=next_attempt,
            retry_class=retry_class,
            retry_reason=retry_reason,
        )

    computed_class, computed_reason = classify_retry(
        state=record.state,
        exit_obj=record.exit,
    )
    if (
        computed_class != RETRY_CLASS_AUTOMATIC
        or computed_class != retry_class
        or computed_reason != retry_reason
    ):
        return AutoRetryDecision(
            action="blocked",
            reason="retry_meta_mismatch",
            due_at=None,
            next_attempt=next_attempt,
            retry_class=retry_class,
            retry_reason=retry_reason,
        )

    terminal = parse_terminal_at(record.terminal_at)
    if terminal is None:
        return AutoRetryDecision(
            action="blocked",
            reason="bad_terminal_at",
            due_at=None,
            next_attempt=next_attempt,
            retry_class=retry_class,
            retry_reason=retry_reason,
        )

    if terminal > tick + timedelta(seconds=AUTO_RETRY_CLOCK_SKEW_S):
        return AutoRetryDecision(
            action="blocked",
            reason="future_terminal_at",
            due_at=None,
            next_attempt=next_attempt,
            retry_class=retry_class,
            retry_reason=retry_reason,
        )

    due = auto_retry_due_at(terminal, int(record.attempt))
    due_s = due.isoformat()
    if tick < due:
        return AutoRetryDecision(
            action="deferred",
            reason="backoff_wait",
            due_at=due_s,
            next_attempt=next_attempt,
            retry_class=retry_class,
            retry_reason=retry_reason,
        )

    return AutoRetryDecision(
        action="eligible",
        reason="automatic_retry_due",
        due_at=due_s,
        next_attempt=next_attempt,
        retry_class=retry_class,
        retry_reason=retry_reason,
    )


def _result_from_decision(
    *,
    job_id: str,
    record: JobRecord | None,
    decision: AutoRetryDecision,
    dry_run: bool,
    action: str | None = None,
    ok: bool = True,
    error_code: str | None = None,
    error_message: str | None = None,
    after_state: str | None = None,
    attempt_after: int | None = None,
) -> AutoRetryResult:
    before = record.state.value if record is not None else None
    return AutoRetryResult(
        ok=ok,
        job_id=job_id,
        action=action or decision.action,
        reason=decision.reason,
        dry_run=dry_run,
        before_state=before,
        after_state=after_state if after_state is not None else before,
        attempt_before=int(record.attempt) if record is not None else None,
        attempt_after=attempt_after
        if attempt_after is not None
        else (int(record.attempt) if record is not None else None),
        attempt_budget=int(record.attempt_budget) if record is not None else None,
        retry_class=decision.retry_class
        if decision.retry_class is not None
        else (record.retry_class if record is not None else None),
        retry_reason=decision.retry_reason
        if decision.retry_reason is not None
        else (record.retry_reason if record is not None else None),
        due_at=decision.due_at,
        error_code=error_code,
        error_message=error_message,
    )


def _is_safe_conflict(exc: JobStoreError, before_attempt: int, cur: JobRecord | None) -> bool:
    """True when another retry/scheduler advanced the job (do not double-launch).

    Only treat retry-state/attempt races as safe conflicts when the job has
    actually progressed: attempt advanced, or state became nonterminal.
    Same-attempt terminal mutations (e.g. cancel-marker while still ``failed``)
    remain blocked (``ok=False``), not soft-conflict ``ok=True``.
    """
    code = getattr(exc, "code", None) or ""
    if code not in {"E_JOB_RETRY_ATTEMPT", "E_JOB_RETRY_STATE"}:
        return False
    if cur is None:
        # Re-read failed after a racing mutation (e.g. lease/attempt skew) —
        # still treat as conflict so we never launch a duplicate runner.
        return True
    if cur.state in _NONTERMINAL:
        return True
    if int(cur.attempt) > int(before_attempt):
        return True
    return False


def _dispatch_one(
    project_root: Path,
    job_id: str,
    *,
    record: JobRecord,
    decision: AutoRetryDecision,
    dry_run: bool,
    now: datetime,
    runner_python: str | None,
) -> AutoRetryResult:
    from omg_cli.jobs.runtime import preflight_retry_job, retry_job

    jid = record.job_id
    before_attempt = int(record.attempt)
    next_attempt = int(decision.next_attempt or before_attempt + 1)

    try:
        if dry_run:
            preflight_retry_job(
                project_root,
                jid,
                attempt=next_attempt,
                intent=RetryIntent.AUTOMATIC,
                now=now,
            )
            return _result_from_decision(
                job_id=jid,
                record=record,
                decision=decision,
                dry_run=True,
                action="would_launch",
                ok=True,
            )

        started = retry_job(
            project_root,
            jid,
            attempt=next_attempt,
            intent=RetryIntent.AUTOMATIC,
            now=now,
            runner_python=runner_python,
        )
        after = started.record
        return _result_from_decision(
            job_id=jid,
            record=record,
            decision=decision,
            dry_run=False,
            action="launched",
            ok=True,
            after_state=after.state.value,
            attempt_after=int(after.attempt),
        )
    except JobStoreError as exc:
        code = getattr(exc, "code", None) or "E_JOB_AUTO_RETRY"
        # Missing after GC — never recreate.
        if code == "E_JOB_UNKNOWN":
            return _result_from_decision(
                job_id=jid,
                record=record,
                decision=decision,
                dry_run=dry_run,
                action="blocked",
                ok=False,
                error_code=code,
                error_message=str(exc),
            )
        try:
            cur = read_job_record(project_root, jid)
        except JobStoreError:
            cur = None
        if _is_safe_conflict(exc, before_attempt, cur):
            return _result_from_decision(
                job_id=jid,
                record=record,
                decision=decision,
                dry_run=dry_run,
                action="conflict",
                ok=True,
                after_state=cur.state.value if cur is not None else None,
                attempt_after=int(cur.attempt) if cur is not None else None,
                error_code=None,
                error_message=None,
            )
        # Preflight / identity / meta / launch failures — no chained retry.
        action = "launch_failed" if code in {"E_JOB_LAUNCH", "E_JOB_RETRY_ARCHIVE"} else "blocked"
        return _result_from_decision(
            job_id=jid,
            record=record,
            decision=decision,
            dry_run=dry_run,
            action=action,
            ok=False,
            after_state=cur.state.value if cur is not None else record.state.value,
            attempt_after=int(cur.attempt) if cur is not None else before_attempt,
            error_code=code,
            error_message=str(exc),
        )


def auto_retry_job(
    project_root: Path,
    job_id: str,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    runner_python: str | None = None,
) -> AutoRetryResult:
    """Single-job auto-retry tick (implicit limit=1)."""
    root = Path(project_root).resolve()
    tick = _coerce_now(now)
    jid = safe_job_id(job_id)

    try:
        with auto_retry_lock(root, timeout_s=AUTO_RETRY_LOCK_TIMEOUT_S):
            record = read_job_record(root, jid)
            if record.provider == ACP_SESSION_PROVIDER:
                return AutoRetryResult(
                    ok=False,
                    job_id=jid,
                    action="protected_internal",
                    reason="protected_internal",
                    dry_run=dry_run,
                    before_state=record.state.value,
                    after_state=record.state.value,
                    attempt_before=int(record.attempt),
                    attempt_after=int(record.attempt),
                    attempt_budget=int(record.attempt_budget),
                    retry_class=record.retry_class,
                    retry_reason=record.retry_reason,
                    due_at=None,
                    error_code="E_JOB_PROVIDER_INTERNAL",
                    error_message="protected_internal",
                )
            decision = evaluate_auto_retry(record, now=tick)
            if decision.action != "eligible":
                ok = decision.action != "blocked"
                error_code = None
                error_message = None
                if decision.action == "blocked":
                    if decision.reason in {"retry_meta_mismatch", "missing_exit", "missing_retry_reason"}:
                        error_code = "E_JOB_AUTO_RETRY_META"
                    elif decision.reason in {"bad_terminal_at", "future_terminal_at"}:
                        error_code = "E_JOB_AUTO_RETRY_TIME"
                    else:
                        error_code = "E_JOB_AUTO_RETRY"
                    error_message = decision.reason
                return _result_from_decision(
                    job_id=jid,
                    record=record,
                    decision=decision,
                    dry_run=dry_run,
                    ok=ok,
                    error_code=error_code,
                    error_message=error_message,
                )
            return _dispatch_one(
                root,
                jid,
                record=record,
                decision=decision,
                dry_run=dry_run,
                now=tick,
                runner_python=runner_python,
            )
    except JobStoreError as exc:
        code = getattr(exc, "code", None) or "E_JOB_AUTO_RETRY"
        return AutoRetryResult(
            ok=False,
            job_id=jid,
            action="blocked",
            reason=code,
            dry_run=dry_run,
            before_state=None,
            after_state=None,
            attempt_before=None,
            attempt_after=None,
            attempt_budget=None,
            retry_class=None,
            retry_reason=None,
            due_at=None,
            error_code=code,
            error_message=str(exc),
        )


def auto_retry_jobs(
    project_root: Path,
    *,
    run_id: str | None = None,
    provider: str | None = None,
    limit: int = DEFAULT_AUTO_RETRY_LIMIT,
    dry_run: bool = False,
    now: datetime | None = None,
    runner_python: str | None = None,
) -> AutoRetryBatchResult:
    """Batch auto-retry tick: snapshot → evaluate → bound → dispatch once."""
    root = Path(project_root).resolve()
    lim = validate_auto_retry_limit(limit)
    tick = _coerce_now(now)
    results: list[AutoRetryResult] = []

    try:
        with auto_retry_lock(root, timeout_s=AUTO_RETRY_LOCK_TIMEOUT_S):
            job_ids = list_job_ids(root)
            scanned = len(job_ids)

            # Snapshot + validate every record before first mutation.
            records: list[JobRecord] = []
            for jid in job_ids:
                rec = read_job_record(root, jid)  # raises → abort whole tick
                records.append(rec)

            candidates: list[tuple[JobRecord, AutoRetryDecision]] = []
            deferred_results: list[AutoRetryResult] = []
            skipped_results: list[AutoRetryResult] = []
            blocked_meta: list[AutoRetryResult] = []

            for rec in records:
                if rec.provider == ACP_SESSION_PROVIDER:
                    continue
                if run_id is not None and (rec.run_id or "") != run_id:
                    continue
                if provider is not None and rec.provider != provider:
                    continue
                decision = evaluate_auto_retry(rec, now=tick)
                if decision.action == "eligible":
                    candidates.append((rec, decision))
                elif decision.action == "deferred":
                    deferred_results.append(
                        _result_from_decision(
                            job_id=rec.job_id,
                            record=rec,
                            decision=decision,
                            dry_run=dry_run,
                            ok=True,
                        )
                    )
                elif decision.action == "blocked":
                    error_code = "E_JOB_AUTO_RETRY_META"
                    if decision.reason in {"bad_terminal_at", "future_terminal_at"}:
                        error_code = "E_JOB_AUTO_RETRY_TIME"
                    blocked_meta.append(
                        _result_from_decision(
                            job_id=rec.job_id,
                            record=rec,
                            decision=decision,
                            dry_run=dry_run,
                            ok=False,
                            error_code=error_code,
                            error_message=decision.reason,
                        )
                    )
                else:
                    skipped_results.append(
                        _result_from_decision(
                            job_id=rec.job_id,
                            record=rec,
                            decision=decision,
                            dry_run=dry_run,
                            ok=True,
                        )
                    )

            considered = (
                len(candidates)
                + len(deferred_results)
                + len(skipped_results)
                + len(blocked_meta)
            )
            due_n = len(candidates)

            def _sort_key(item: tuple[JobRecord, AutoRetryDecision]) -> tuple:
                rec, decision = item
                due_s = decision.due_at or ""
                term_s = rec.terminal_at or ""
                return (due_s, term_s, rec.job_id)

            candidates.sort(key=_sort_key)

            selected = candidates[:lim]
            overflow = candidates[lim:]

            for rec, decision in selected:
                results.append(
                    _dispatch_one(
                        root,
                        rec.job_id,
                        record=rec,
                        decision=decision,
                        dry_run=dry_run,
                        now=tick,
                        runner_python=runner_python,
                    )
                )

            for rec, decision in overflow:
                results.append(
                    _result_from_decision(
                        job_id=rec.job_id,
                        record=rec,
                        decision=decision,
                        dry_run=dry_run,
                        action="limit_reached",
                        ok=True,
                    )
                )

            # Include non-due evaluations for operator visibility.
            results.extend(deferred_results)
            results.extend(blocked_meta)
            results.extend(skipped_results)

            batch = AutoRetryBatchResult(
                ok=all(r.ok for r in results),
                dry_run=dry_run,
                limit=lim,
                results=results,
                scanned=scanned,
                considered=considered,
                due=due_n,
            )
            return batch

    except JobStoreError as exc:
        code = getattr(exc, "code", None) or "E_JOB_AUTO_RETRY"
        # Lock busy or malformed snapshot — zero mutation.
        blocked = AutoRetryResult(
            ok=False,
            job_id="*",
            action="blocked",
            reason=code,
            dry_run=dry_run,
            before_state=None,
            after_state=None,
            attempt_before=None,
            attempt_after=None,
            attempt_budget=None,
            retry_class=None,
            retry_reason=None,
            due_at=None,
            error_code=code,
            error_message=str(exc),
        )
        return AutoRetryBatchResult(
            ok=False,
            dry_run=dry_run,
            limit=lim,
            results=[blocked],
            scanned=0,
            considered=0,
            due=0,
        )


__all__ = [
    "AUTO_RETRY_BASE_DELAY_S",
    "AUTO_RETRY_CLOCK_SKEW_S",
    "AUTO_RETRY_LOCK_TIMEOUT_S",
    "AUTO_RETRY_MAX_DELAY_S",
    "AutoRetryBatchResult",
    "AutoRetryDecision",
    "AutoRetryResult",
    "DEFAULT_AUTO_RETRY_LIMIT",
    "MAX_AUTO_RETRY_LIMIT",
    "auto_retry_job",
    "auto_retry_jobs",
    "evaluate_auto_retry",
    "validate_auto_retry_limit",
]
