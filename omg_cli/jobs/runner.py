"""Job runner child entry — owns ``ProviderAdapter.run`` after parent commit (#68).

Invoked as::

    python -m omg_cli.jobs.runner --job-id ID --project-root PATH

Parent alone owns ``starting → running`` (pid/pgid/handle). This process waits
on a readiness barrier until that commit is visible, then runs the adapter and
stamps only ``running → succeeded|failed``. Never transitions to ``running``.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

from omg_cli.jobs.models import TERMINAL_STATES, JobRecord, JobState, JobStoreError
from omg_cli.jobs.store import (
    append_jsonl,
    job_dir,
    read_job_record,
    transition_job,
    utc_now,
)
from omg_cli.providers.base import ProviderAdapter
from omg_cli.providers.models import ProviderRunRequest

# Child polls until parent commits running (or terminal / timeout).
DEFAULT_READY_TIMEOUT_S = 60.0
DEFAULT_READY_POLL_S = 0.02


def resolve_adapter(provider: str) -> ProviderAdapter:
    if provider == "fake":
        from omg_cli.jobs.fake import FakeProvider

        return FakeProvider()
    if provider == "antigravity":
        from omg_cli.providers.antigravity import get_adapter

        return get_adapter()
    raise JobStoreError(f"unknown provider {provider!r}", code="E_JOB_PROVIDER")


def _append_stdout_lines(project_root: Path, job_id: str, text: str) -> None:
    path = job_dir(project_root, job_id) / "stdout.jsonl"
    now = utc_now()
    for i, line in enumerate((text or "").splitlines()):
        append_jsonl(
            path,
            {"ts": now, "index": i, "line": line},
        )


def _append_event(project_root: Path, job_id: str, event_type: str, **payload: object) -> None:
    path = job_dir(project_root, job_id) / "events.jsonl"
    append_jsonl(
        path,
        {"ts": utc_now(), "type": event_type, "payload": dict(payload)},
    )


def wait_until_parent_running(
    project_root: Path,
    job_id: str,
    *,
    timeout_s: float = DEFAULT_READY_TIMEOUT_S,
    poll_s: float = DEFAULT_READY_POLL_S,
    expected_pid: int | None = None,
) -> JobRecord | None:
    """Barrier: wait until parent committed ``running`` with our PID/handle.

    Returns the running record, or ``None`` when the child must exit without
    calling ``ProviderAdapter.run`` (terminal state or timeout).
    """
    my_pid = int(expected_pid if expected_pid is not None else os.getpid())
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while time.monotonic() < deadline:
        try:
            rec = read_job_record(project_root, job_id)
        except JobStoreError:
            time.sleep(max(0.01, float(poll_s)))
            continue
        if rec.state in TERMINAL_STATES:
            return None
        if (
            rec.state == JobState.RUNNING
            and rec.pid is not None
            and int(rec.pid) == my_pid
            and rec.handle
        ):
            return rec
        time.sleep(max(0.01, float(poll_s)))
    return None


def _stamp_running_terminal(
    project_root: Path,
    job_id: str,
    *,
    ok: bool,
    exit_obj: dict,
    usage: dict | None,
    artifacts: list,
    result_desc: str | None,
    error_message: str | None,
) -> None:
    cur = read_job_record(project_root, job_id)
    if cur.state in TERMINAL_STATES:
        _append_event(
            project_root,
            job_id,
            "runner.skip_terminal",
            state=cur.state.value,
        )
        return
    if cur.state != JobState.RUNNING:
        # Parent never committed / cancel won while we waited — do not invent running.
        _append_event(
            project_root,
            job_id,
            "runner.skip_non_running",
            state=cur.state.value,
        )
        return
    target = JobState.SUCCEEDED if ok else JobState.FAILED
    transition_job(
        project_root,
        job_id,
        target,
        updates={
            "exit": exit_obj,
            "usage": usage,
            "artifacts": artifacts,
            "result": result_desc,
            "error_message": error_message if not ok else None,
        },
    )
    _append_event(
        project_root,
        job_id,
        "runner.terminal",
        state=target.value,
        ok=bool(ok),
    )


def run_job(project_root: Path, job_id: str) -> int:
    """Wait for parent running commit, then Adapter.run, then terminal stamp."""
    try:
        record = read_job_record(project_root, job_id)
    except JobStoreError as exc:
        print(f"omg job runner: {exc}", file=sys.stderr)
        return 2

    if record.state in TERMINAL_STATES:
        return 0

    jdir = job_dir(project_root, job_id)
    os.environ["OMG_JOB_ID"] = job_id
    os.environ["OMG_JOB_DIR"] = str(jdir)
    os.environ["OMG_PROJECT_ROOT"] = str(project_root)

    ready_timeout = float(
        (record.worker or {}).get("ready_timeout_s") or DEFAULT_READY_TIMEOUT_S
    )
    _append_event(
        project_root,
        job_id,
        "runner.barrier_wait",
        provider=record.provider,
        pid=os.getpid(),
    )
    ready = wait_until_parent_running(
        project_root,
        job_id,
        timeout_s=ready_timeout,
    )
    if ready is None:
        _append_event(project_root, job_id, "runner.barrier_abort")
        # Terminal already, or parent never committed — exit without Adapter.run.
        return 0

    worker = ready.worker or {}
    if worker.get("fail"):
        os.environ["OMG_JOB_FAKE_FAIL"] = "1"
    if worker.get("large_output"):
        os.environ["OMG_JOB_FAKE_LARGE"] = "1"
    if worker.get("ignore_sigterm"):
        os.environ["OMG_JOB_FAKE_IGNORE_SIGTERM"] = "1"
    if "sleep_s" in worker:
        os.environ["OMG_JOB_FAKE_SLEEP"] = str(worker.get("sleep_s"))

    prompt_path = jdir / "prompt.md"
    _append_event(project_root, job_id, "runner.start", provider=ready.provider)

    try:
        adapter = resolve_adapter(ready.provider)
        assert isinstance(adapter, ProviderAdapter)
        request = ProviderRunRequest(
            prompt_file=str(prompt_path),
            cwd=str(project_root),
            timeout_s=float(worker.get("timeout_s") or 3600.0),
            output_format="text",
        )
        result = adapter.run(request)
    except BaseException as exc:  # noqa: BLE001 — stamp failed; re-raise SystemExit-ish
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        _append_event(
            project_root,
            job_id,
            "runner.error",
            error=str(exc),
            traceback=traceback.format_exc()[-2000:],
        )
        try:
            _stamp_running_terminal(
                project_root,
                job_id,
                ok=False,
                exit_obj={"class": "spawn_error", "returncode": 1},
                usage=None,
                artifacts=[],
                result_desc=None,
                error_message=str(exc),
            )
        except JobStoreError:
            pass
        print(f"omg job runner: {exc}", file=sys.stderr)
        return 1

    _append_stdout_lines(project_root, job_id, result.stdout or result.output or "")
    for ev in result.events:
        _append_event(
            project_root,
            job_id,
            "provider.event",
            type=ev.type,
            index=ev.index,
            malformed=ev.malformed,
        )

    artifacts = [a.to_dict() for a in result.artifacts]
    result_desc = None
    for a in artifacts:
        if a.get("kind") == "result":
            result_desc = a.get("path")
            break
    large_path = jdir / "artifacts" / "result.md"
    if large_path.is_file() and result_desc is None:
        result_desc = "artifacts/result.md"
        if not any(a.get("path") == result_desc for a in artifacts):
            artifacts.append(
                {
                    "path": result_desc,
                    "kind": "result",
                    "media_type": "text/markdown",
                    "sha256": "",
                }
            )

    usage = result.usage.to_dict() if result.usage is not None else None
    exit_obj = {
        "class": result.exit_class,
        "returncode": int(result.returncode),
        "ok": bool(result.ok),
        "timed_out": bool(result.timed_out),
        "cancelled": bool(result.cancelled),
    }

    try:
        _stamp_running_terminal(
            project_root,
            job_id,
            ok=bool(result.ok),
            exit_obj=exit_obj,
            usage=usage,
            artifacts=artifacts,
            result_desc=result_desc,
            error_message=result.error_message or None,
        )
    except JobStoreError as exc:
        print(f"omg job runner: stamp failed: {exc}", file=sys.stderr)
        return 1

    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omg_cli.jobs.runner")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--project-root", required=True)
    args = parser.parse_args(argv)
    root = Path(args.project_root).resolve()
    return int(run_job(root, str(args.job_id)))


if __name__ == "__main__":
    raise SystemExit(main())
