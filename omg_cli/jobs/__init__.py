"""Durable background job runtime (#68 PR1–PR4).

Canonical state under ``.omg/jobs/<job-id>/``. Provider execution always goes
through :class:`~omg_cli.providers.base.ProviderAdapter.run` (no second
launcher). Public start admits hermetic ``fake`` and ``antigravity``; lease
recovery is explicit via ``omg job recover`` (no auto-retry scheduler).
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

__all__ = [
    "JOB_SCHEMA",
    "TERMINAL_STATES",
    "JobHealth",
    "JobObservation",
    "JobRecord",
    "JobState",
    "JobStoreError",
    "RecoveryBatchResult",
    "RecoveryResult",
    "TransitionError",
    "cancel_job",
    "collect_job",
    "job_status",
    "list_jobs",
    "observe_job",
    "recover_job",
    "recover_jobs",
    "start_job",
    "wait_job",
]
