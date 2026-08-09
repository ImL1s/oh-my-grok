"""omg job — durable background jobs CLI (#68).

Commands: ``omg job {start,status,wait,collect,cancel,list,retry,gc}``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omg_cli.cli_envelope import emit_data, emit_json, failure, success, wants_json
from omg_cli.jobs.models import JobStoreError
from omg_cli.jobs.runtime import (
    cancel_job,
    collect_job,
    gc_jobs,
    job_status,
    list_jobs,
    retry_job,
    start_job,
    wait_job,
)
from omg_cli.project_root import project_root
from omg_cli.redaction import redact_text, redact_value


def _root(args: argparse.Namespace) -> Path:
    ctx = getattr(args, "omg_ctx", None)
    if ctx is not None and getattr(ctx, "project_root", None) is not None:
        return Path(ctx.project_root)
    return project_root()


def cmd_job(args: argparse.Namespace) -> int:
    action = getattr(args, "job_action", None)
    if action == "start":
        return _cmd_start(args)
    if action == "status":
        return _cmd_status(args)
    if action == "wait":
        return _cmd_wait(args)
    if action == "collect":
        return _cmd_collect(args)
    if action == "cancel":
        return _cmd_cancel(args)
    if action == "list":
        return _cmd_list(args)
    if action == "retry":
        return _cmd_retry(args)
    if action == "gc":
        return _cmd_gc(args)
    print(
        "usage: omg job {start,status,wait,collect,cancel,list,retry,gc}",
        file=sys.stderr,
    )
    return 2


def _cmd_start(args: argparse.Namespace) -> int:
    cmd = "job.start"
    provider = getattr(args, "provider", None) or ""
    role = getattr(args, "role", None) or "researcher"
    prompt_file = getattr(args, "prompt_file", None)
    if not prompt_file:
        emit_json(
            failure(
                cmd,
                "E_JOB_PROMPT",
                "require --prompt-file PATH",
                next_action="Pass --prompt-file PATH",
            )
        )
        return 2

    # Fake-only flags with antigravity: fail before start_job materialization
    # (runtime also enforces; CLI gives a clearer usage error).
    fake_flags = any(
        [
            getattr(args, "sleep", None) is not None,
            bool(getattr(args, "fail", False)),
            bool(getattr(args, "large_output", False)),
            bool(getattr(args, "ignore_sigterm", False)),
        ]
    )
    if provider == "antigravity" and fake_flags:
        emit_json(
            failure(
                cmd,
                "E_JOB_PROVIDER_OPTIONS",
                "fake-only flags (--sleep/--fail/--large-output/--ignore-sigterm) "
                "are not allowed with --provider antigravity",
            )
        )
        return 2

    try:
        result = start_job(
            _root(args),
            provider=str(provider),
            role=str(role),
            prompt_file=Path(str(prompt_file)),
            run_id=getattr(args, "run_id", None) or None,
            sleep_s=getattr(args, "sleep", None),
            fail=bool(getattr(args, "fail", False)),
            large_output=bool(getattr(args, "large_output", False)),
            ignore_sigterm=bool(getattr(args, "ignore_sigterm", False)),
            model=getattr(args, "model", None) or None,
            effort=getattr(args, "effort", None) or None,
            mode=getattr(args, "mode", None) or None,
            output_format=getattr(args, "output_format", None) or None,
            provider_timeout_s=getattr(args, "provider_timeout", None),
            attempt_budget=int(getattr(args, "attempt_budget", 1) or 1),
        )
    except JobStoreError as exc:
        emit_json(
            failure(
                cmd,
                getattr(exc, "code", None) or "E_JOB_START",
                redact_text(str(exc)),
            )
        )
        return 1

    rec = result.record
    from omg_cli.jobs.providers import public_request_summary

    payload = {
        "job_id": rec.job_id,
        "state": rec.state.value,
        "provider": rec.provider,
        "role": rec.role,
        "pid": rec.pid,
        "pgid": rec.pgid,
        "handle": rec.handle,
        "created_at": rec.created_at,
        "attempt": rec.attempt,
        "attempt_budget": rec.attempt_budget,
        "remaining_attempts": rec.remaining_attempts(),
        "request": public_request_summary(rec.request),
    }
    if wants_json(args):
        emit_json(success(cmd, **redact_value(payload)))
    else:
        print(
            f"job {rec.job_id} state={rec.state.value} "
            f"provider={rec.provider} pid={rec.pid}"
        )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    cmd = "job.status"
    job_id = getattr(args, "job_id", None)
    if not job_id:
        emit_json(failure(cmd, "E_JOB_UNKNOWN", "JOB_ID required"))
        return 2
    try:
        rec = job_status(_root(args), str(job_id))
    except JobStoreError as exc:
        emit_json(
            failure(
                cmd,
                getattr(exc, "code", None) or "E_JOB_UNKNOWN",
                redact_text(str(exc)),
            )
        )
        return 1
    body = redact_value(rec.public_status())
    if wants_json(args):
        emit_json(success(cmd, job=body))
    else:
        emit_data(args, cmd, body)
    return 0


def _cmd_wait(args: argparse.Namespace) -> int:
    cmd = "job.wait"
    job_id = getattr(args, "job_id", None)
    if not job_id:
        emit_json(failure(cmd, "E_JOB_UNKNOWN", "JOB_ID required"))
        return 2
    timeout = float(getattr(args, "timeout", None) or 0.0)
    try:
        rec, timed_out = wait_job(
            _root(args),
            str(job_id),
            timeout_s=timeout,
        )
    except JobStoreError as exc:
        emit_json(
            failure(
                cmd,
                getattr(exc, "code", None) or "E_JOB_UNKNOWN",
                redact_text(str(exc)),
            )
        )
        return 1

    body = redact_value(rec.public_status())
    if timed_out:
        emit_json(
            failure(
                cmd,
                "E_JOB_TIMEOUT",
                f"wait timed out after {timeout}s; job still {rec.state.value}",
                details={"job": body, "timed_out": True},
                next_action="Re-run omg job wait / status; timeout does not cancel",
            )
        )
        return 1

    if wants_json(args):
        emit_json(success(cmd, timed_out=False, job=body))
    else:
        print(f"job {rec.job_id} state={rec.state.value}")
    return 0


def _cmd_collect(args: argparse.Namespace) -> int:
    cmd = "job.collect"
    job_id = getattr(args, "job_id", None)
    if not job_id:
        emit_json(failure(cmd, "E_JOB_UNKNOWN", "JOB_ID required"))
        return 2
    try:
        summary = collect_job(_root(args), str(job_id))
    except JobStoreError as exc:
        emit_json(
            failure(
                cmd,
                getattr(exc, "code", None) or "E_JOB_COLLECT",
                redact_text(str(exc)),
            )
        )
        return 1
    body = redact_value(summary)
    if wants_json(args):
        emit_json(success(cmd, collect=body))
    else:
        emit_data(args, cmd, body)
    return 0


def _cmd_cancel(args: argparse.Namespace) -> int:
    cmd = "job.cancel"
    job_id = getattr(args, "job_id", None)
    if not job_id:
        emit_json(failure(cmd, "E_JOB_UNKNOWN", "JOB_ID required"))
        return 2
    reason = getattr(args, "reason", None) or "operator"
    try:
        rec = cancel_job(_root(args), str(job_id), reason=str(reason))
    except JobStoreError as exc:
        emit_json(
            failure(
                cmd,
                getattr(exc, "code", None) or "E_JOB_CANCEL",
                redact_text(str(exc)),
            )
        )
        return 1
    body = redact_value(rec.public_status())
    if wants_json(args):
        emit_json(success(cmd, job=body))
    else:
        print(f"job {rec.job_id} state={rec.state.value} reason={rec.cancel_reason}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    cmd = "job.list"
    try:
        jobs = list_jobs(
            _root(args),
            state=getattr(args, "state", None) or None,
            provider=getattr(args, "provider", None) or None,
            run_id=getattr(args, "run_id", None) or None,
        )
    except JobStoreError as exc:
        emit_json(
            failure(
                cmd,
                getattr(exc, "code", None) or "E_JOB_LIST",
                redact_text(str(exc)),
            )
        )
        return 1
    body = redact_value(jobs)
    if wants_json(args):
        emit_json(success(cmd, jobs=body, count=len(jobs)))
    else:
        if not jobs:
            print("(no jobs)")
        for j in jobs:
            print(
                f"{j.get('job_id')} state={j.get('state')} "
                f"provider={j.get('provider')} role={j.get('role')} "
                f"attempt={j.get('attempt')}/{j.get('attempt_budget')}"
            )
    return 0


def _cmd_retry(args: argparse.Namespace) -> int:
    cmd = "job.retry"
    job_id = getattr(args, "job_id", None)
    attempt = getattr(args, "attempt", None)
    if not job_id:
        emit_json(failure(cmd, "E_JOB_UNKNOWN", "JOB_ID required"))
        return 2
    if attempt is None:
        emit_json(
            failure(
                cmd,
                "E_JOB_RETRY_ATTEMPT",
                "require --attempt N (exact next attempt)",
                next_action="Pass --attempt current+1",
            )
        )
        return 2
    try:
        result = retry_job(
            _root(args),
            str(job_id),
            attempt=int(attempt),
        )
    except JobStoreError as exc:
        emit_json(
            failure(
                cmd,
                getattr(exc, "code", None) or "E_JOB_RETRY",
                redact_text(str(exc)),
            )
        )
        return 1
    rec = result.record
    payload = {
        "job_id": rec.job_id,
        "state": rec.state.value,
        "provider": rec.provider,
        "role": rec.role,
        "attempt": rec.attempt,
        "attempt_budget": rec.attempt_budget,
        "remaining_attempts": rec.remaining_attempts(),
        "pid": rec.pid,
        "pgid": rec.pgid,
        "handle": rec.handle,
        "retry_class": rec.retry_class,
    }
    if wants_json(args):
        emit_json(success(cmd, **redact_value(payload)))
    else:
        print(
            f"job {rec.job_id} retried attempt={rec.attempt} "
            f"state={rec.state.value} pid={rec.pid}"
        )
    return 0


def _cmd_gc(args: argparse.Namespace) -> int:
    cmd = "job.gc"
    retention = getattr(args, "retention_days", None)
    if retention is None:
        emit_json(
            failure(
                cmd,
                "E_JOB_GC",
                "require --retention-days N",
                next_action="Pass --retention-days N",
            )
        )
        return 2
    try:
        result = gc_jobs(_root(args), retention_days=float(retention))
    except JobStoreError as exc:
        emit_json(
            failure(
                cmd,
                getattr(exc, "code", None) or "E_JOB_GC",
                redact_text(str(exc)),
            )
        )
        return 1
    payload = {
        "deleted": list(result.deleted),
        "skipped": list(result.skipped),
        "deleted_count": len(result.deleted),
        "skipped_count": len(result.skipped),
        "retention_days": float(retention),
    }
    if wants_json(args):
        emit_json(success(cmd, **redact_value(payload)))
    else:
        print(
            f"gc deleted={len(result.deleted)} skipped={len(result.skipped)} "
            f"retention_days={retention}"
        )
    return 0


def register_job_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    """Register ``job`` family parsers (#68 PR1–PR3)."""
    p_job = sub.add_parser(
        "job",
        parents=[common],
        help=(
            "durable background jobs "
            "(start/status/wait/collect/cancel/list/retry/gc; #68 PR1–PR3)"
        ),
    )
    job_sub = p_job.add_subparsers(dest="job_action")

    p_start = job_sub.add_parser(
        "start",
        parents=[common],
        help="start a durable background job (--provider fake|antigravity)",
    )
    p_start.add_argument(
        "--provider",
        required=True,
        choices=("fake", "antigravity"),
        help="provider adapter (fake hermetic; antigravity via ProviderAdapter.run)",
    )
    p_start.add_argument(
        "--role",
        default="researcher",
        help="job role label (default researcher; audit only, not --agent)",
    )
    p_start.add_argument(
        "--prompt-file",
        required=True,
        help="path to prompt file (copied into job dir as prompt.md)",
    )
    p_start.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="optional parent run id",
    )
    p_start.add_argument(
        "--attempt-budget",
        type=int,
        default=1,
        help="immutable max attempts including the first (default 1)",
    )
    p_start.add_argument(
        "--model",
        default=None,
        help="provider model (Antigravity)",
    )
    p_start.add_argument(
        "--effort",
        default=None,
        help="provider effort (Antigravity)",
    )
    p_start.add_argument(
        "--mode",
        default=None,
        help="provider mode (Antigravity)",
    )
    p_start.add_argument(
        "--output-format",
        default=None,
        choices=("text", "json", "stream-json"),
        help="Antigravity output format (default stream-json)",
    )
    p_start.add_argument(
        "--provider-timeout",
        type=float,
        default=None,
        help="provider run timeout seconds",
    )
    p_start.add_argument(
        "--sleep",
        type=float,
        default=None,
        help="fake worker sleep seconds (test/hermetic; fake-only)",
    )
    p_start.add_argument(
        "--fail",
        action="store_true",
        help="fake worker: exit nonzero (hermetic; fake-only)",
    )
    p_start.add_argument(
        "--large-output",
        action="store_true",
        help="fake worker: write ≥100KiB artifact (hermetic; fake-only)",
    )
    p_start.add_argument(
        "--ignore-sigterm",
        action="store_true",
        help="fake worker: ignore SIGTERM to force SIGKILL path (hermetic; fake-only)",
    )
    p_start.set_defaults(func=cmd_job, job_action="start")

    p_status = job_sub.add_parser(
        "status",
        parents=[common],
        help="show job status (descriptors only)",
    )
    p_status.add_argument("job_id", help="job id")
    p_status.set_defaults(func=cmd_job, job_action="status")

    p_wait = job_sub.add_parser(
        "wait",
        parents=[common],
        help="wait until terminal or timeout (timeout does not cancel)",
    )
    p_wait.add_argument("job_id", help="job id")
    p_wait.add_argument(
        "--timeout",
        type=float,
        required=True,
        help="seconds to wait (does not cancel on expiry)",
    )
    p_wait.set_defaults(func=cmd_job, job_action="wait")

    p_collect = job_sub.add_parser(
        "collect",
        parents=[common],
        help="collect summary + artifact descriptors (idempotent)",
    )
    p_collect.add_argument("job_id", help="job id")
    p_collect.set_defaults(func=cmd_job, job_action="collect")

    p_cancel = job_sub.add_parser(
        "cancel",
        parents=[common],
        help="cancel by recorded PID/PGID only (sibling-safe)",
    )
    p_cancel.add_argument("job_id", help="job id")
    p_cancel.add_argument(
        "--reason",
        default="operator",
        help="cancel reason (default operator)",
    )
    p_cancel.set_defaults(func=cmd_job, job_action="cancel")

    p_list = job_sub.add_parser(
        "list",
        parents=[common],
        help="list jobs with optional filters",
    )
    p_list.add_argument(
        "--state",
        default=None,
        help="filter by state",
    )
    p_list.add_argument(
        "--provider",
        default=None,
        help="filter by provider",
    )
    p_list.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="filter by parent run id",
    )
    p_list.set_defaults(func=cmd_job, job_action="list")

    p_retry = job_sub.add_parser(
        "retry",
        parents=[common],
        help="explicit retry of a terminal job (--attempt current+1)",
    )
    p_retry.add_argument("job_id", help="job id")
    p_retry.add_argument(
        "--attempt",
        type=int,
        required=True,
        help="exact next attempt number (must be current+1)",
    )
    p_retry.set_defaults(func=cmd_job, job_action="retry")

    p_gc = job_sub.add_parser(
        "gc",
        parents=[common],
        help="garbage-collect terminal jobs older than retention",
    )
    p_gc.add_argument(
        "--retention-days",
        type=float,
        required=True,
        help="delete terminal jobs with terminal_at older than N days",
    )
    p_gc.set_defaults(func=cmd_job, job_action="gc")

    p_job.set_defaults(func=cmd_job)


__all__ = [
    "cmd_job",
    "register_job_parsers",
]
