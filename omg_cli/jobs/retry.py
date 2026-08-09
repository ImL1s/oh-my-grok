"""Retry admission + classification helpers (#68 PR3–PR5).

Public ``omg job retry`` remains explicit (``RetryIntent.EXPLICIT``).
Automatic admission is stricter and is used by the bounded auto-retry
scheduler (#68 PR5) via ``RetryIntent.AUTOMATIC``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping

from omg_cli.jobs.models import (
    RETRY_CLASS_AUTOMATIC,
    RETRY_CLASS_MANUAL_ONLY,
    RETRY_CLASS_NEVER,
    RETRY_CLASS_UNKNOWN,
    JobRecord,
    JobState,
    JobStoreError,
)
from omg_cli.jobs.providers import ACP_SESSION_PROVIDER

# Terminal states that may be explicitly retried (never succeeded).
RETRYABLE_TERMINAL_STATES: frozenset[JobState] = frozenset(
    {
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.LOST,
    }
)

_KNOWN_RETRY_CLASSES = frozenset(
    {
        RETRY_CLASS_AUTOMATIC,
        RETRY_CLASS_MANUAL_ONLY,
        RETRY_CLASS_NEVER,
        RETRY_CLASS_UNKNOWN,
    }
)

# Backoff constants (shared with scheduler; duplicated names live there too).
AUTO_RETRY_BASE_DELAY_S = 10.0
AUTO_RETRY_MAX_DELAY_S = 300.0
AUTO_RETRY_CLOCK_SKEW_S = 5.0
_MAX_BACKOFF_EXPONENT = 16  # cap pathological attempt values


class RetryIntent(str, Enum):
    EXPLICIT = "explicit"
    AUTOMATIC = "automatic"


def classify_retry(
    *,
    state: JobState | str,
    exit_obj: Mapping[str, Any] | None,
) -> tuple[str, str | None]:
    """Return ``(retry_class, reason)`` from a terminal exit envelope.

    Classification is advisory for operators; public retry is still explicit.
    Unknown / malformed exits fail closed as ``unknown``.
    """
    if isinstance(state, JobState):
        state_name = state
    else:
        try:
            state_name = JobState(str(state))
        except ValueError:
            return RETRY_CLASS_UNKNOWN, "unknown_state"

    if state_name == JobState.SUCCEEDED:
        return RETRY_CLASS_NEVER, "success"

    if state_name == JobState.CANCELLED:
        return RETRY_CLASS_MANUAL_ONLY, "cancelled"

    if state_name == JobState.LOST:
        return RETRY_CLASS_UNKNOWN, "lost"

    if not isinstance(exit_obj, Mapping):
        return RETRY_CLASS_UNKNOWN, "missing_exit"

    exit_class = str(exit_obj.get("class") or "").strip().lower()
    if not exit_class:
        return RETRY_CLASS_UNKNOWN, "missing_exit_class"

    if exit_class == "success":
        return RETRY_CLASS_NEVER, "success"

    if exit_class == "cancelled":
        return RETRY_CLASS_MANUAL_ONLY, "cancelled"

    if exit_class in {"auth_blocked", "permission", "destructive_confirmation"}:
        return RETRY_CLASS_MANUAL_ONLY, exit_class

    if exit_class in {"timeout", "overflow"}:
        return RETRY_CLASS_AUTOMATIC, exit_class

    if bool(exit_obj.get("timed_out")) or bool(exit_obj.get("overflow")):
        return RETRY_CLASS_AUTOMATIC, "timeout_or_overflow"

    if exit_class in {"nonzero", "retryable"}:
        if exit_obj.get("retryable") is False:
            return RETRY_CLASS_MANUAL_ONLY, "nonzero_non_retryable"
        if exit_obj.get("retryable") is True or exit_class == "retryable":
            return RETRY_CLASS_AUTOMATIC, "retryable_nonzero"
        # nonzero without explicit retryable flag → unknown (fail closed)
        return RETRY_CLASS_UNKNOWN, "nonzero_unclassified"

    if exit_class in {"spawn_error", "malformed", "parse_error", "unknown"}:
        return RETRY_CLASS_UNKNOWN, exit_class

    return RETRY_CLASS_UNKNOWN, f"unclassified:{exit_class}"


def classified_terminal_updates(
    *,
    state: JobState | str,
    exit_obj: Mapping[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build terminal transition updates that always stamp retry metadata.

    Every parent/runner path that lands a terminal state must merge these
    fields so ``retry_class`` is never left ``None`` on a fresh stamp.
    """
    from omg_cli.jobs.store import utc_now

    retry_class, retry_reason = classify_retry(state=state, exit_obj=exit_obj)
    updates: dict[str, Any] = {
        "terminal_at": utc_now(),
        "retry_class": retry_class,
        "retry_reason": retry_reason,
    }
    if exit_obj is not None and "exit" not in extra:
        updates["exit"] = dict(exit_obj)
    updates.update(extra)
    return updates


def parse_terminal_at(raw: str | None) -> datetime | None:
    """Parse a timezone-aware terminal timestamp; naive/malformed → None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        return None
    return ts.astimezone(timezone.utc)


def auto_retry_delay_s(attempt: int) -> float:
    """Deterministic exponential backoff for failed attempt *n*."""
    try:
        n = int(attempt)
    except (TypeError, ValueError):
        n = 1
    exponent = max(0, min(_MAX_BACKOFF_EXPONENT, n - 1))
    return min(AUTO_RETRY_BASE_DELAY_S * (2**exponent), AUTO_RETRY_MAX_DELAY_S)


def auto_retry_due_at(terminal_at: datetime, attempt: int) -> datetime:
    return terminal_at + timedelta(seconds=auto_retry_delay_s(attempt))


def _coerce_now(now: datetime | None) -> datetime:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def assert_automatic_retry_admission(
    record: JobRecord,
    *,
    attempt: int,
    now: datetime | None = None,
) -> datetime:
    """Fail-closed automatic-only gates. Returns validated ``due_at``.

    Raises ``JobStoreError`` with ``E_JOB_AUTO_RETRY_*`` / shared retry codes.
    """
    if record.state != JobState.FAILED:
        raise JobStoreError(
            f"automatic retry requires state=failed; got {record.state.value}",
            code="E_JOB_RETRY_STATE",
        )

    if record.cancel_requested_at is not None or record.cancel_reason is not None:
        raise JobStoreError(
            "automatic retry refuses cancelled terminal records",
            code="E_JOB_RETRY_STATE",
        )

    try:
        next_attempt = int(attempt)
    except (TypeError, ValueError) as exc:
        raise JobStoreError(
            f"invalid --attempt {attempt!r}",
            code="E_JOB_RETRY_ATTEMPT",
        ) from exc

    if next_attempt != int(record.attempt) + 1:
        raise JobStoreError(
            f"retry requires --attempt {int(record.attempt) + 1} "
            f"(current attempt={record.attempt}); got {next_attempt}",
            code="E_JOB_RETRY_ATTEMPT",
        )

    budget = int(record.attempt_budget)
    if next_attempt > budget:
        raise JobStoreError(
            f"attempt budget exhausted "
            f"(attempt_budget={budget}, requested_attempt={next_attempt})",
            code="E_JOB_RETRY_BUDGET",
        )

    if record.retry_class != RETRY_CLASS_AUTOMATIC:
        raise JobStoreError(
            f"automatic retry requires retry_class=automatic; "
            f"got {record.retry_class!r}",
            code="E_JOB_RETRY_CLASS",
        )

    reason = record.retry_reason
    if not isinstance(reason, str) or not reason.strip():
        raise JobStoreError(
            "automatic retry requires non-empty retry_reason",
            code="E_JOB_AUTO_RETRY_META",
        )

    if not isinstance(record.exit, Mapping):
        raise JobStoreError(
            "automatic retry requires exit object",
            code="E_JOB_AUTO_RETRY_META",
        )

    computed_class, computed_reason = classify_retry(
        state=record.state,
        exit_obj=record.exit,
    )
    if (
        computed_class != RETRY_CLASS_AUTOMATIC
        or computed_class != record.retry_class
        or computed_reason != record.retry_reason
    ):
        raise JobStoreError(
            "persisted retry metadata disagrees with recomputed classification",
            code="E_JOB_AUTO_RETRY_META",
        )

    terminal = parse_terminal_at(record.terminal_at)
    if terminal is None:
        raise JobStoreError(
            "missing or malformed timezone-aware terminal_at",
            code="E_JOB_AUTO_RETRY_TIME",
        )

    tick = _coerce_now(now)
    if terminal > tick + timedelta(seconds=AUTO_RETRY_CLOCK_SKEW_S):
        raise JobStoreError(
            "terminal_at is implausibly in the future",
            code="E_JOB_AUTO_RETRY_TIME",
        )

    due = auto_retry_due_at(terminal, int(record.attempt))
    if tick < due:
        raise JobStoreError(
            f"automatic retry not due until {due.isoformat()}",
            code="E_JOB_AUTO_RETRY_TIME",
        )
    return due


def assert_retry_admission(
    record: JobRecord,
    *,
    attempt: int,
    intent: RetryIntent = RetryIntent.EXPLICIT,
    now: datetime | None = None,
) -> None:
    """Fail-closed admission checks before archive/requeue (no side effects)."""
    if record.provider == ACP_SESSION_PROVIDER:
        raise JobStoreError(
            f"job provider {ACP_SESSION_PROVIDER!r} cannot be retried via public CLI",
            code="E_JOB_PROVIDER_INTERNAL",
        )

    if intent is RetryIntent.AUTOMATIC:
        assert_automatic_retry_admission(record, attempt=attempt, now=now)
        return

    if record.state not in RETRYABLE_TERMINAL_STATES:
        raise JobStoreError(
            f"job {record.job_id} is not retryable from state={record.state.value}",
            code="E_JOB_RETRY_STATE",
        )

    try:
        next_attempt = int(attempt)
    except (TypeError, ValueError) as exc:
        raise JobStoreError(
            f"invalid --attempt {attempt!r}",
            code="E_JOB_RETRY_ATTEMPT",
        ) from exc

    if next_attempt != int(record.attempt) + 1:
        raise JobStoreError(
            f"retry requires --attempt {int(record.attempt) + 1} "
            f"(current attempt={record.attempt}); got {next_attempt}",
            code="E_JOB_RETRY_ATTEMPT",
        )

    budget = int(record.attempt_budget)
    if next_attempt > budget:
        raise JobStoreError(
            f"attempt budget exhausted "
            f"(attempt_budget={budget}, requested_attempt={next_attempt})",
            code="E_JOB_RETRY_BUDGET",
        )

    # Explicit classification required — missing class fails closed.
    # (Legacy schema-v1 records default attempt_budget=1 so they cannot retry.)
    if record.retry_class is None:
        raise JobStoreError(
            "job missing retry_class; refusing retry (fail closed)",
            code="E_JOB_RETRY_CLASS",
        )

    if record.retry_class not in _KNOWN_RETRY_CLASSES:
        raise JobStoreError(
            f"malformed retry_class {record.retry_class!r}",
            code="E_JOB_RETRY_META",
        )

    if record.retry_class == RETRY_CLASS_NEVER:
        raise JobStoreError(
            "job classified retry_class=never; refusing retry",
            code="E_JOB_RETRY_CLASS",
        )


__all__ = [
    "AUTO_RETRY_BASE_DELAY_S",
    "AUTO_RETRY_CLOCK_SKEW_S",
    "AUTO_RETRY_MAX_DELAY_S",
    "RETRYABLE_TERMINAL_STATES",
    "RetryIntent",
    "assert_automatic_retry_admission",
    "assert_retry_admission",
    "auto_retry_delay_s",
    "auto_retry_due_at",
    "classify_retry",
    "classified_terminal_updates",
    "parse_terminal_at",
]
