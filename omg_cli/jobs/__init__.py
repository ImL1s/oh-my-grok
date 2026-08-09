"""Durable background job runtime (#68 PR1–PR5).

Canonical state under ``.omg/jobs/<job-id>/``. Provider execution always goes
through :class:`~omg_cli.providers.base.ProviderAdapter.run` (no second
launcher). Public start admits hermetic ``fake`` and ``antigravity``; lease
recovery is explicit via ``omg job recover``; bounded auto-retry is a
caller-driven tick via ``omg job auto-retry`` (no resident daemon).
"""

from __future__ import annotations

from omg_cli.jobs.models import (
    JOB_SCHEMA,
    TERMINAL_STATES,
    JobRecord,
    JobState,
    JobStoreError,
    TransitionError,
)
from omg_cli.jobs.recovery import (
    JobHealth,
    JobObservation,
    RecoveryBatchResult,
    RecoveryResult,
    observe_job,
    recover_job,
    recover_jobs,
)
from omg_cli.jobs.runtime import (
    cancel_job,
    collect_job,
    job_status,
    list_jobs,
    start_job,
    wait_job,
)
from omg_cli.jobs.scheduler import (
    AutoRetryBatchResult,
    AutoRetryDecision,
    AutoRetryResult,
    auto_retry_job,
    auto_retry_jobs,
    evaluate_auto_retry,
)

__all__ = [
    "JOB_SCHEMA",
    "TERMINAL_STATES",
    "AutoRetryBatchResult",
    "AutoRetryDecision",
    "AutoRetryResult",
    "JobHealth",
    "JobObservation",
    "JobRecord",
    "JobState",
    "JobStoreError",
    "RecoveryBatchResult",
    "RecoveryResult",
    "TransitionError",
    "auto_retry_job",
    "auto_retry_jobs",
    "cancel_job",
    "collect_job",
    "evaluate_auto_retry",
    "job_status",
    "list_jobs",
    "observe_job",
    "recover_job",
    "recover_jobs",
    "start_job",
    "wait_job",
]
