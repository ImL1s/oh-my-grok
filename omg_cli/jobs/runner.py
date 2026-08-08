"""Job runner child entry — owns ``ProviderAdapter.run`` after parent commit (#68).

Invoked as::

    python -m omg_cli.jobs.runner --job-id ID --project-root PATH

Parent alone owns ``starting → running`` (pid/pgid/handle). This process waits
on a readiness barrier until that commit is visible, then runs the adapter and
stamps only ``running → succeeded|failed|cancelled``. Never transitions to
``running``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import sys
import threading
import time
import traceback
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes, ensure_managed_dir
from omg_cli.jobs.models import TERMINAL_STATES, JobRecord, JobState, JobStoreError
from omg_cli.jobs.ownership import capture_identity
from omg_cli.jobs.providers import resolve_job_provider
from omg_cli.jobs.store import (
    append_jsonl,
    bind_provider_process,
    job_dir,
    mark_provider_exited,
    mark_provider_launching,
    read_job_record,
    transition_job,
    utc_now,
)
from omg_cli.providers.base import ProviderAdapter
from omg_cli.providers.models import ProviderRunRequest
from omg_cli.redaction import redact_text

# Child polls until parent commits running (or terminal / timeout).
DEFAULT_READY_TIMEOUT_S = 15.0
DEFAULT_READY_POLL_S = 0.02
_MAX_STREAM_CHARS = 256_000


@contextmanager
def _env_scope(updates: Mapping[str, str | None]) -> Iterator[None]:
    """Apply env updates and restore prior values (or absence) on exit."""
    previous: dict[str, str | None] = {}
    for key, value in updates.items():
        previous[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def resolve_adapter(provider: str) -> ProviderAdapter:
    """Resolve via the jobs-scoped registry (shared with parent start_job)."""
    adapter, _meta = resolve_job_provider(provider)
    return adapter


def _append_stdout_lines(project_root: Path, job_id: str, text: str) -> None:
    path = job_dir(project_root, job_id) / "stdout.jsonl"
    now = utc_now()
    bounded = (text or "")[:_MAX_STREAM_CHARS]
    for i, line in enumerate(bounded.splitlines()):
        append_jsonl(
            path,
            {"ts": now, "index": i, "line": line},
        )


def _append_stderr_lines(project_root: Path, job_id: str, text: str) -> None:
    path = job_dir(project_root, job_id) / "stderr.jsonl"
    now = utc_now()
    bounded = (text or "")[:_MAX_STREAM_CHARS]
    for i, line in enumerate(bounded.splitlines()):
        append_jsonl(
            path,
            {"ts": now, "index": i, "line": redact_text(line)},
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


def _map_terminal_state(*, ok: bool, cancelled: bool, exit_class: str) -> JobState:
    if cancelled or exit_class == "cancelled":
        return JobState.CANCELLED
    if ok and exit_class == "success":
        return JobState.SUCCEEDED
    return JobState.FAILED


def _stamp_running_terminal(
    project_root: Path,
    job_id: str,
    *,
    ok: bool,
    cancelled: bool,
    exit_class: str,
    exit_obj: dict,
    usage: dict | None,
    artifacts: list,
    result_desc: str | None,
    error_message: str | None,
    session: dict | None = None,
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
    target = _map_terminal_state(ok=ok, cancelled=cancelled, exit_class=exit_class)
    updates: dict = {
        "exit": exit_obj,
        "usage": usage,
        "artifacts": artifacts,
        "result": result_desc,
        "error_message": error_message if target != JobState.SUCCEEDED else None,
    }
    if session is not None:
        updates["session"] = session
    if target == JobState.CANCELLED and not cur.cancel_reason:
        updates["cancel_reason"] = "runner"
    transition_job(
        project_root,
        job_id,
        target,
        updates=updates,
    )
    _append_event(
        project_root,
        job_id,
        "runner.terminal",
        state=target.value,
        ok=bool(ok),
        cancelled=bool(cancelled),
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
    env_updates: dict[str, str | None] = {
        "OMG_JOB_ID": job_id,
        "OMG_JOB_DIR": str(jdir),
        "OMG_PROJECT_ROOT": str(project_root),
        # Clear fake-mode leftovers unless worker opts in below.
        "OMG_JOB_FAKE_FAIL": None,
        "OMG_JOB_FAKE_LARGE": None,
        "OMG_JOB_FAKE_IGNORE_SIGTERM": None,
        "OMG_JOB_FAKE_SLEEP": None,
    }

    with _env_scope(env_updates):
        return _run_job_with_env(project_root, job_id, record, jdir)


def _run_job_with_env(
    project_root: Path,
    job_id: str,
    record: JobRecord,
    jdir: Path,
) -> int:
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
    fake_env: dict[str, str | None] = {
        "OMG_JOB_FAKE_FAIL": "1" if worker.get("fail") else None,
        "OMG_JOB_FAKE_LARGE": "1" if worker.get("large_output") else None,
        "OMG_JOB_FAKE_IGNORE_SIGTERM": "1" if worker.get("ignore_sigterm") else None,
        "OMG_JOB_FAKE_SLEEP": (
            str(worker.get("sleep_s")) if "sleep_s" in worker else None
        ),
    }
    # Nested scope so fake flags do not leak past Adapter.run either.
    with _env_scope(fake_env):
        return _execute_adapter(project_root, job_id, ready, jdir, worker)


def _install_cancel_handlers(cancel_event: threading.Event) -> list[tuple[int, object]]:
    """Install SIGTERM/SIGINT handlers that set cancel_event (do not exit)."""
    previous: list[tuple[int, object]] = []

    def _on_signal(signum: int, frame: object) -> None:  # noqa: ARG001
        cancel_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prev = signal.getsignal(sig)
            signal.signal(sig, _on_signal)
            previous.append((sig, prev))
        except (ValueError, OSError):
            pass
    return previous


def _restore_handlers(previous: list[tuple[int, object]]) -> None:
    for sig, prev in previous:
        try:
            signal.signal(sig, prev)  # type: ignore[arg-type]
        except (ValueError, OSError, TypeError):
            pass


def _write_result_artifact(jdir: Path, body: str) -> tuple[str, str]:
    """Write artifacts/result.md; return (relative path, sha256 hex)."""
    art_dir = jdir / "artifacts"
    ensure_managed_dir(art_dir)
    path = art_dir / "result.md"
    data = (body or "").encode("utf-8")
    atomic_write_bytes(path, data, mode=DATA_FILE_MODE, replace=True)
    digest = hashlib.sha256(data).hexdigest()
    return "artifacts/result.md", digest


def _execute_adapter(
    project_root: Path,
    job_id: str,
    ready: JobRecord,
    jdir: Path,
    worker: dict,
) -> int:
    prompt_path = jdir / "prompt.md"
    cancel_event = threading.Event()
    prev_handlers = _install_cancel_handlers(cancel_event)
    runner_pid = os.getpid()

    try:
        _append_event(project_root, job_id, "runner.start", provider=ready.provider)
        adapter = resolve_adapter(ready.provider)
        assert isinstance(adapter, ProviderAdapter)

        req = dict(ready.request or {})
        output_format = str(req.get("output_format") or "text")
        timeout_s = float(
            worker.get("timeout_s")
            or req.get("timeout_s")
            or 3600.0
        )
        binary = req.get("provider_binary")
        if isinstance(binary, str) and binary.strip():
            binary_path: str | None = binary.strip()
        else:
            binary_path = None

        # Antigravity (and future out-of-process providers) bind an inner
        # process group. Fake runs in-process — leave provider_process pending
        # so cancel only targets the outer runner (PR1 behavior).
        bind_inner = ready.provider != "fake"

        if bind_inner:
            mark_provider_launching(
                project_root,
                job_id,
                expected_runner_pid=runner_pid,
                expected_attempt=int(ready.attempt),
            )

        def _on_process_started(proc: object) -> None:
            pid = int(getattr(proc, "pid", 0) or 0)
            try:
                identity = capture_identity(pid)
            except JobStoreError:
                # Still try with pid==pgid fallback for start_new_session.
                identity = capture_identity(pid, pgid=pid)
            handle = f"provider:{job_id}:pid={identity.pid}"
            bind_provider_process(
                project_root,
                job_id,
                pid=identity.pid,
                pgid=identity.pgid,
                pid_starttime=identity.pid_starttime,
                handle=handle,
                expected_runner_pid=runner_pid,
                expected_attempt=int(ready.attempt),
            )

        request = ProviderRunRequest(
            prompt_file=str(prompt_path),
            cwd=str(project_root),
            timeout_s=timeout_s,
            output_format=output_format,  # type: ignore[arg-type]
            model=req.get("model"),
            effort=req.get("effort"),
            mode=req.get("mode"),
            binary=binary_path,
            cancel_event=cancel_event,
            on_process_started=_on_process_started if bind_inner else None,
        )
        result = adapter.run(request)
        if bind_inner:
            try:
                mark_provider_exited(project_root, job_id)
            except JobStoreError:
                pass

        # Evidence artifacts (never verified/passes).
        stdout_text = result.stdout or result.output or ""
        stderr_text = result.stderr or ""
        _append_stdout_lines(project_root, job_id, stdout_text)
        _append_stderr_lines(project_root, job_id, stderr_text)

        for ev in result.events:
            payload = dict(ev.payload) if isinstance(ev.payload, dict) else {}
            raw = redact_text(ev.raw or "")[:4000] if ev.raw else ""
            _append_event(
                project_root,
                job_id,
                "provider.event",
                type=ev.type,
                index=ev.index,
                malformed=ev.malformed,
                payload=payload,
                raw=raw,
            )

        artifacts = [a.to_dict() for a in result.artifacts]
        result_body = result.output or stdout_text
        # Preserve partial output on timeout/cancel/overflow/parse/nonzero.
        # Do not overwrite a provider-supplied result artifact (e.g. fake large).
        has_result_art = any(
            isinstance(a, dict) and a.get("kind") == "result" and a.get("path")
            for a in artifacts
        )
        if has_result_art:
            result_desc = None
            for a in artifacts:
                if a.get("kind") == "result":
                    result_desc = a.get("path")
                    break
            # Ensure sha if missing and file exists under job dir.
            if result_desc:
                target = jdir / str(result_desc)
                if target.is_file():
                    for a in artifacts:
                        if a.get("path") == result_desc and not a.get("sha256"):
                            a["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        elif result_body or result.partial_output or result.cancelled or result.timed_out:
            rel, sha = _write_result_artifact(jdir, result_body)
            result_desc = rel
            artifacts.append(
                {
                    "path": rel,
                    "kind": "result",
                    "media_type": "text/markdown",
                    "sha256": sha,
                }
            )
        else:
            result_desc = None

        # Confinement: provider-supplied artifact paths must stay under job dir.
        for a in list(artifacts):
            path = a.get("path") if isinstance(a, dict) else None
            if not isinstance(path, str) or not path:
                continue
            candidate = Path(path)
            if candidate.is_absolute() or any(p == ".." for p in candidate.parts):
                artifacts = [x for x in artifacts if x is not a]
                _append_event(
                    project_root,
                    job_id,
                    "runner.artifact_rejected",
                    path=path,
                )

        usage = result.usage.to_dict() if result.usage is not None else None
        session = {
            "session_id": result.session_id,
            "resume_token": result.resume_token,
            "resume_supported": bool(result.resume_supported),
        }
        exit_obj = {
            "class": result.exit_class,
            "returncode": int(result.returncode),
            "ok": bool(result.ok),
            "timed_out": bool(result.timed_out),
            "cancelled": bool(result.cancelled),
            "retryable": bool(result.retryable),
            "partial_output": bool(result.partial_output),
            "overflow": bool(result.overflow),
            "stdout_truncated": bool(result.stdout_truncated),
            "stderr_truncated": bool(result.stderr_truncated),
        }
        # auth_blocked stays failed even if returncode==0
        ok = bool(result.ok) and result.exit_class == "success" and not result.cancelled
        _stamp_running_terminal(
            project_root,
            job_id,
            ok=ok,
            cancelled=bool(result.cancelled) or result.exit_class == "cancelled",
            exit_class=str(result.exit_class),
            exit_obj=exit_obj,
            usage=usage,
            artifacts=artifacts,
            result_desc=result_desc,
            error_message=result.error_message or None,
            session=session,
        )
        if result.cancelled or result.exit_class == "cancelled":
            return 0
        return 0 if ok else 1
    except BaseException as exc:  # noqa: BLE001 — stamp failed; re-raise SystemExit-ish
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        try:
            mark_provider_exited(project_root, job_id)
        except Exception:
            pass
        try:
            _append_event(
                project_root,
                job_id,
                "runner.error",
                error=str(exc),
                traceback=traceback.format_exc()[-2000:],
            )
        except Exception:
            pass
        try:
            # If cancel won during bind failure, map to cancelled when event set.
            cancelled = cancel_event.is_set()
            _stamp_running_terminal(
                project_root,
                job_id,
                ok=False,
                cancelled=cancelled,
                exit_class="cancelled" if cancelled else "spawn_error",
                exit_obj={
                    "class": "cancelled" if cancelled else "spawn_error",
                    "returncode": 1,
                    "ok": False,
                    "timed_out": False,
                    "cancelled": cancelled,
                },
                usage=None,
                artifacts=[],
                result_desc=None,
                error_message=str(exc),
            )
        except JobStoreError:
            pass
        print(f"omg job runner: {exc}", file=sys.stderr)
        return 1
    finally:
        _restore_handlers(prev_handlers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omg_cli.jobs.runner")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--project-root", required=True)
    args = parser.parse_args(argv)
    root = Path(args.project_root).resolve()
    return int(run_job(root, str(args.job_id)))


if __name__ == "__main__":
    raise SystemExit(main())
