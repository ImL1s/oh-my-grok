"""Job runner child entry — owns ``ProviderAdapter.run`` (#68 PR1).

Invoked as::

    python -m omg_cli.jobs.runner --job-id ID --project-root PATH

Survives leader exit. Never sets ``verified``. Writes terminal state under
``.omg/jobs/<id>/`` after Adapter.run returns (or on uncaught error → failed).
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

from omg_cli.jobs.models import TERMINAL_STATES, JobState, JobStoreError
from omg_cli.jobs.store import (
    append_jsonl,
    job_dir,
    read_job_record,
    transition_job,
    update_job_fields,
    utc_now,
)
from omg_cli.providers.base import ProviderAdapter
from omg_cli.providers.models import ProviderRunRequest


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


def run_job(project_root: Path, job_id: str) -> int:
    """Load job, call Adapter.run, stamp terminal state. Returns process exit."""
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

    worker = record.worker or {}
    if worker.get("fail"):
        os.environ["OMG_JOB_FAKE_FAIL"] = "1"
    if worker.get("large_output"):
        os.environ["OMG_JOB_FAKE_LARGE"] = "1"
    if worker.get("ignore_sigterm"):
        os.environ["OMG_JOB_FAKE_IGNORE_SIGTERM"] = "1"
    if "sleep_s" in worker:
        os.environ["OMG_JOB_FAKE_SLEEP"] = str(worker.get("sleep_s"))

    prompt_path = jdir / "prompt.md"
    _append_event(project_root, job_id, "runner.start", provider=record.provider)

    try:
        adapter = resolve_adapter(record.provider)
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
            cur = read_job_record(project_root, job_id)
            if cur.state not in TERMINAL_STATES:
                # starting|running → failed
                if cur.state == JobState.STARTING:
                    transition_job(
                        project_root,
                        job_id,
                        JobState.FAILED,
                        updates={
                            "exit": {
                                "class": "spawn_error",
                                "returncode": 1,
                            },
                            "error_message": str(exc),
                        },
                    )
                elif cur.state == JobState.RUNNING:
                    transition_job(
                        project_root,
                        job_id,
                        JobState.FAILED,
                        updates={
                            "exit": {
                                "class": "spawn_error",
                                "returncode": 1,
                            },
                            "error_message": str(exc),
                        },
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
    # Prefer explicit large-output path if present on disk
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
        cur = read_job_record(project_root, job_id)
        if cur.state in TERMINAL_STATES:
            # Cancel won the race — do not overwrite cancelled/lost.
            _append_event(project_root, job_id, "runner.skip_terminal", state=cur.state.value)
            return 0 if result.ok else 1

        target = JobState.SUCCEEDED if result.ok else JobState.FAILED
        # Parent may still be on starting if update races; allow both.
        if cur.state == JobState.STARTING:
            # First commit running handle fields if parent missed, then terminal —
            # but PR1 parent always sets running. If still starting, go failed/succeeded
            # via starting→failed or starting→running→terminal.
            # Legal: starting → failed; starting → running only.
            if target == JobState.FAILED:
                transition_job(
                    project_root,
                    job_id,
                    JobState.FAILED,
                    updates={
                        "exit": exit_obj,
                        "usage": usage,
                        "artifacts": artifacts,
                        "result": result_desc,
                        "error_message": result.error_message or None,
                        "handle": cur.handle or f"fake:{job_id}",
                    },
                )
            else:
                transition_job(
                    project_root,
                    job_id,
                    JobState.RUNNING,
                    updates={
                        "handle": cur.handle or f"{cur.provider}:{job_id}",
                        "pid": cur.pid or os.getpid(),
                        "pgid": cur.pgid or os.getpgid(0),
                    },
                )
                transition_job(
                    project_root,
                    job_id,
                    JobState.SUCCEEDED,
                    updates={
                        "exit": exit_obj,
                        "usage": usage,
                        "artifacts": artifacts,
                        "result": result_desc,
                    },
                )
        elif cur.state == JobState.RUNNING:
            transition_job(
                project_root,
                job_id,
                target,
                updates={
                    "exit": exit_obj,
                    "usage": usage,
                    "artifacts": artifacts,
                    "result": result_desc,
                    "error_message": (result.error_message or None)
                    if not result.ok
                    else None,
                },
            )
        else:
            update_job_fields(
                project_root,
                job_id,
                exit=exit_obj,
                usage=usage,
                artifacts=artifacts,
                result=result_desc,
            )
        _append_event(
            project_root,
            job_id,
            "runner.terminal",
            state=target.value,
            ok=bool(result.ok),
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
