"""omg job — durable background jobs CLI (#68 PR1).

Commands: ``omg job {start,status,wait,collect,cancel,list}``.
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
    job_status,
    list_jobs,
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
    print(
        "usage: omg job {start,status,wait,collect,cancel,list}",
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
                f"provider={j.get('provider')} role={j.get('role')}"
            )
    return 0


def register_job_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    """Register ``job`` family parsers (#68 PR1)."""
    p_job = sub.add_parser(
        "job",
        parents=[common],
        help="durable background jobs (start/status/wait/collect/cancel/list; #68 PR1)",
    )
    job_sub = p_job.add_subparsers(dest="job_action")

    p_start = job_sub.add_parser(
        "start",
        parents=[common],
        help="start a durable background job (PR1: --provider fake)",
    )
    p_start.add_argument(
        "--provider",
        required=True,
        choices=("fake", "antigravity"),
        help="provider adapter (PR1 live spawn: fake only)",
    )
    p_start.add_argument(
        "--role",
        default="researcher",
        help="job role label (default researcher)",
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
        "--sleep",
        type=float,
        default=None,
        help="fake worker sleep seconds (test/hermetic)",
    )
    p_start.add_argument(
        "--fail",
        action="store_true",
        help="fake worker: exit nonzero (hermetic)",
    )
    p_start.add_argument(
        "--large-output",
        action="store_true",
        help="fake worker: write ≥100KiB artifact (hermetic)",
    )
    p_start.add_argument(
        "--ignore-sigterm",
        action="store_true",
        help="fake worker: ignore SIGTERM to force SIGKILL path (hermetic)",
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

    p_job.set_defaults(func=cmd_job)


__all__ = [
    "cmd_job",
    "register_job_parsers",
]
