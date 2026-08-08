"""Durable background job runtime (#68 PR1).

Canonical state under ``.omg/jobs/<job-id>/``. Provider execution always goes
through :class:`~omg_cli.providers.base.ProviderAdapter.run` (no second
launcher). PR1 admits hermetic ``fake`` starts only; Antigravity live spawn
is deferred.
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
    "JobRecord",
    "JobState",
    "JobStoreError",
    "TransitionError",
    "cancel_job",
    "collect_job",
    "job_status",
    "list_jobs",
    "start_job",
    "wait_job",
]
