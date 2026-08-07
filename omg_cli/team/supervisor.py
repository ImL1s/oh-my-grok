"""Pane supervisor: spawn provider child and prove readiness (#99).

Consumes a vetted JSON descriptor (argv list + delivery metadata). Never
reconstructs shell command strings from untrusted text. Owns process-level
readiness so read-only workers do not need mailbox ACK.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
)
from omg_cli.team.provider_ready import get_readiness_strategy
from omg_cli.team.startup import (
    EvidenceCode,
    StartupError,
    StartupPhase,
    append_startup_diagnostics,
    write_startup_phase,
)

DESCRIPTOR_SCHEMA_VERSION = 1
DESCRIPTOR_KIND = "team_provider_descriptor"
DEFAULT_READY_WAIT_S = 30.0
_CAPTURE_MAX_LINES = 48
_CAPTURE_MAX_BYTES = 16_384


class SupervisorError(RuntimeError):
    """Supervisor identity / descriptor / spawn failure."""


def write_provider_descriptor(
    path: Path | str,
    *,
    provider: str,
    argv: Sequence[str],
    prompt_delivery: str = "prompt-file",
    prompt_file: Path | str | None = None,
    needs_pty: bool = False,
    cwd: Path | str | None = None,
) -> Path:
    """Write a schema-versioned provider argv descriptor (atomic)."""
    target = Path(path)
    ensure_managed_dir(target.parent)
    argv_list = [str(x) for x in argv]
    if not argv_list:
        raise SupervisorError("provider descriptor requires non-empty argv")
    payload: dict[str, Any] = {
        "schema_version": DESCRIPTOR_SCHEMA_VERSION,
        "kind": DESCRIPTOR_KIND,
        "provider": str(provider or "unknown"),
        "argv": argv_list,
        "prompt_delivery": str(prompt_delivery or "prompt-file"),
        "needs_pty": bool(needs_pty),
    }
    if prompt_file is not None:
        payload["prompt_file"] = str(prompt_file)
    if cwd is not None:
        payload["cwd"] = str(cwd)
    body = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(target, body, mode=DATA_FILE_MODE)
    return target


def load_provider_descriptor(path: Path | str) -> dict[str, Any]:
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SupervisorError(f"provider descriptor unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise SupervisorError("provider descriptor must be a JSON object")
    if data.get("schema_version") != DESCRIPTOR_SCHEMA_VERSION:
        raise SupervisorError("provider descriptor schema_version mismatch")
    if data.get("kind") != DESCRIPTOR_KIND:
        raise SupervisorError("provider descriptor kind mismatch")
    argv = data.get("argv")
    if not isinstance(argv, list) or not argv or not all(
        isinstance(x, str) and x for x in argv
    ):
        raise SupervisorError("provider descriptor argv must be non-empty string list")
    return data


def _env_identity() -> tuple[str, str, str, Path]:
    worker_id = (os.environ.get("OMG_TEAM_WORKER_ID") or "").strip()
    run_id = (os.environ.get("OMG_TEAM_RUN_ID") or "").strip()
    team_id = (os.environ.get("OMG_TEAM_ID") or "team").strip() or "team"
    leader = (
        os.environ.get("OMG_TEAM_LEADER_ROOT")
        or os.environ.get("OMG_PROJECT_ROOT")
        or ""
    ).strip()
    if not worker_id or not run_id:
        raise SupervisorError(
            "team supervisor requires OMG_TEAM_WORKER_ID and OMG_TEAM_RUN_ID"
        )
    if not leader:
        raise SupervisorError(
            "team supervisor requires OMG_TEAM_LEADER_ROOT or OMG_PROJECT_ROOT"
        )
    return run_id, team_id, worker_id, Path(leader).resolve()


def _pid_start(pid: int) -> str | None:
    from omg_cli.team.plane import _pid_start_identity

    return _pid_start_identity(pid)


def _pgid(pid: int) -> int | None:
    from omg_cli.team.plane import _pgid_for_pid

    return _pgid_for_pid(pid)


def _resolve_positional_argv(
    argv: list[str], *, prompt_file: Path | None
) -> list[str]:
    if prompt_file is None:
        raise SupervisorError("positional-text delivery requires prompt_file")
    try:
        body = prompt_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise SupervisorError(f"cannot read prompt_file: {exc}") from exc
    # Substitute path tokens that equal the prompt_file path.
    needle = str(prompt_file)
    return [body if tok == needle else tok for tok in argv]


def _spawn_provider(
    argv: list[str],
    *,
    cwd: Path | None,
    needs_pty: bool,
    stdin_path: Path | None,
) -> subprocess.Popen[bytes]:
    if needs_pty:
        # Keep pty.spawn in a child Python so the supervisor retains PID
        # authority over the wrapper and can still reap/signal.
        payload = json.dumps(argv, ensure_ascii=False)
        py = (
            "import json,pty,sys;"
            " argv=json.loads(sys.argv[1]);"
            " rc=pty.spawn(argv);"
            " sys.exit(0 if rc in (0, None) else int(rc or 1))"
        )
        cmd = [sys.executable, "-c", py, payload]
        stdin = subprocess.DEVNULL
        if stdin_path is not None:
            stdin = open(stdin_path, "rb")  # noqa: SIM115 — closed by Popen
        return subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    stdin: Any = subprocess.DEVNULL
    if stdin_path is not None:
        stdin = open(stdin_path, "rb")  # noqa: SIM115
    return subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd else None,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _drain_capture(proc: subprocess.Popen[bytes], bucket: list[str]) -> None:
    """Non-blocking drain of provider stdout into a bounded line bucket.

    Must use ``os.read`` on the raw fd — buffered ``file.read()`` can block
    until EOF even after ``select`` reports readability.
    """
    if proc.stdout is None:
        return
    try:
        import select

        fd = proc.stdout.fileno()
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            try:
                chunk = os.read(fd, 1024)
            except BlockingIOError:
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            for line in text.splitlines():
                if line:
                    bucket.append(line[:512])
            if sum(len(x) for x in bucket) > _CAPTURE_MAX_BYTES:
                del bucket[: max(0, len(bucket) - _CAPTURE_MAX_LINES)]
                break
    except (OSError, ValueError):
        return
    if len(bucket) > _CAPTURE_MAX_LINES:
        del bucket[: len(bucket) - _CAPTURE_MAX_LINES]


def _forward_signals(child_pgid: int | None, child_pid: int | None) -> None:
    def _handler(signum: int, _frame: Any) -> None:
        target = child_pgid if child_pgid and child_pgid > 0 else child_pid
        if not target:
            return
        try:
            if child_pgid and child_pgid > 0:
                os.killpg(child_pgid, signum)
            else:
                os.kill(int(target), signum)
        except (ProcessLookupError, PermissionError, OSError):
            return

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


def _child_alive(proc: subprocess.Popen[bytes]) -> bool:
    return proc.poll() is None


def run_supervisor(
    *,
    descriptor_path: Path | str,
    ready_timeout_s: float | None = None,
    env: Mapping[str, str] | None = None,
    poll_s: float = 0.1,
) -> int:
    """Main supervisor entry: spawn provider, write phases, reap child.

    Returns the provider exit code (or 1 on supervisor-level failure).
    """
    source = dict(env) if env is not None else dict(os.environ)
    run_id, team_id, worker_id, root = _env_identity()
    desc = load_provider_descriptor(descriptor_path)
    provider = str(desc.get("provider") or "unknown")
    argv = [str(x) for x in desc["argv"]]
    delivery = str(desc.get("prompt_delivery") or "prompt-file")
    needs_pty = bool(desc.get("needs_pty"))
    prompt_file_raw = desc.get("prompt_file")
    prompt_file = Path(str(prompt_file_raw)) if prompt_file_raw else None
    cwd_raw = desc.get("cwd")
    cwd = Path(str(cwd_raw)) if cwd_raw else None
    timeout_s = (
        float(ready_timeout_s)
        if ready_timeout_s is not None
        else float(source.get("OMG_TEAM_SUPERVISOR_READY_S") or DEFAULT_READY_WAIT_S)
    )

    supervisor_pid = os.getpid()
    write_startup_phase(
        root,
        run_id=run_id,
        team_id=team_id,
        worker_id=worker_id,
        phase=StartupPhase.PANE_CREATED,
        provider=provider,
        supervisor_pid=supervisor_pid,
        evidence_code=EvidenceCode.PANE_BOUND,
    )

    stdin_path: Path | None = None
    if delivery == "positional-text":
        argv = _resolve_positional_argv(argv, prompt_file=prompt_file)
    elif delivery == "stdin":
        if prompt_file is None:
            raise SupervisorError("stdin delivery requires prompt_file")
        stdin_path = prompt_file
    elif delivery != "prompt-file":
        raise SupervisorError(f"unknown prompt_delivery: {delivery!r}")

    strategy = get_readiness_strategy(provider, env=source)
    proc: subprocess.Popen[bytes] | None = None
    capture: list[str] = []
    provider_pid: int | None = None
    provider_pgid: int | None = None
    provider_start: str | None = None
    reached_ready = False
    reached_dispatch = False

    try:
        proc = _spawn_provider(
            argv, cwd=cwd, needs_pty=needs_pty, stdin_path=stdin_path
        )
        provider_pid = int(proc.pid)
        # Brief settle so start identity is observable.
        time.sleep(0.02)
        provider_pgid = _pgid(provider_pid)
        provider_start = _pid_start(provider_pid)
        if not provider_start:
            # Fail closed: cannot prove identity.
            try:
                if provider_pgid:
                    os.killpg(provider_pgid, signal.SIGTERM)
                else:
                    proc.terminate()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            write_startup_phase(
                root,
                run_id=run_id,
                team_id=team_id,
                worker_id=worker_id,
                phase=StartupPhase.FAILED,
                provider=provider,
                supervisor_pid=supervisor_pid,
                provider_pid=provider_pid,
                provider_pgid=provider_pgid,
                evidence_code=EvidenceCode.MALFORMED,
                failure_reason="provider_pid_start unavailable after spawn",
            )
            return 1

        write_startup_phase(
            root,
            run_id=run_id,
            team_id=team_id,
            worker_id=worker_id,
            phase=StartupPhase.PROVIDER_SPAWNED,
            provider=provider,
            supervisor_pid=supervisor_pid,
            provider_pid=provider_pid,
            provider_pgid=provider_pgid,
            provider_pid_start=provider_start,
            evidence_code=EvidenceCode.PROVIDER_SPAWNED,
        )
        _forward_signals(provider_pgid, provider_pid)

        deadline = time.monotonic() + max(0.05, timeout_s)
        started = time.monotonic()
        while time.monotonic() < deadline:
            _drain_capture(proc, capture)
            alive = _child_alive(proc)
            obs = strategy.observe(
                provider_pid=provider_pid,
                alive=alive,
                capture_lines=capture,
                elapsed_s=time.monotonic() - started,
                env=source,
            )
            if obs.status == "blocked":
                append_startup_diagnostics(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    lines=capture[-16:],
                )
                write_startup_phase(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase=StartupPhase.BLOCKED,
                    provider=provider,
                    supervisor_pid=supervisor_pid,
                    provider_pid=provider_pid,
                    provider_pgid=provider_pgid,
                    provider_pid_start=provider_start,
                    evidence_code=obs.evidence_code,
                    blocked_reason=obs.blocked_reason,
                    failure_reason=obs.detail,
                )
                # Keep child alive for human intervention; supervisor waits.
                break
            if obs.status == "failed":
                append_startup_diagnostics(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    lines=capture[-16:],
                )
                write_startup_phase(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase=StartupPhase.FAILED,
                    provider=provider,
                    supervisor_pid=supervisor_pid,
                    provider_pid=provider_pid,
                    provider_pgid=provider_pgid,
                    provider_pid_start=provider_start,
                    evidence_code=obs.evidence_code,
                    failure_reason=obs.failure_reason or obs.detail,
                )
                break
            if obs.status == "unknown":
                # Stay pending until timeout — never optimistic ready.
                pass
            elif obs.status == "ready" and not reached_ready:
                write_startup_phase(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase=StartupPhase.PROVIDER_READY,
                    provider=provider,
                    supervisor_pid=supervisor_pid,
                    provider_pid=provider_pid,
                    provider_pgid=provider_pgid,
                    provider_pid_start=provider_start,
                    evidence_code=obs.evidence_code,
                )
                reached_ready = True
                # Prompt already delivered via argv/stdin/positional contract.
                write_startup_phase(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase=StartupPhase.TASK_DISPATCHED,
                    provider=provider,
                    supervisor_pid=supervisor_pid,
                    provider_pid=provider_pid,
                    provider_pgid=provider_pgid,
                    provider_pid_start=provider_start,
                    evidence_code=EvidenceCode.PROMPT_CONTRACT_ACCEPTED,
                )
                reached_dispatch = True
                break
            if not alive:
                write_startup_phase(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase=StartupPhase.FAILED,
                    provider=provider,
                    supervisor_pid=supervisor_pid,
                    provider_pid=provider_pid,
                    provider_pgid=provider_pgid,
                    provider_pid_start=provider_start,
                    evidence_code=EvidenceCode.PROVIDER_EXITED,
                    failure_reason="provider exited before ready",
                )
                break
            time.sleep(max(0.05, float(poll_s)))
        else:
            # Timeout path.
            append_startup_diagnostics(
                root,
                run_id=run_id,
                team_id=team_id,
                worker_id=worker_id,
                lines=capture[-16:],
            )
            if _child_alive(proc):
                write_startup_phase(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase=StartupPhase.FAILED,
                    provider=provider,
                    supervisor_pid=supervisor_pid,
                    provider_pid=provider_pid,
                    provider_pgid=provider_pgid,
                    provider_pid_start=provider_start,
                    evidence_code=EvidenceCode.TIMEOUT,
                    failure_reason=f"provider readiness timed out after {timeout_s}s",
                )
            else:
                write_startup_phase(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase=StartupPhase.FAILED,
                    provider=provider,
                    supervisor_pid=supervisor_pid,
                    provider_pid=provider_pid,
                    provider_pgid=provider_pgid,
                    provider_pid_start=provider_start,
                    evidence_code=EvidenceCode.PROVIDER_EXITED,
                    failure_reason="provider exited before ready (timeout loop)",
                )

        # Reap / wait for child for the rest of the pane lifetime.
        assert proc is not None
        rc = proc.wait()
        if reached_dispatch and rc != 0:
            # Provider died after ready — record terminal note but do not
            # reverse phases (monotonic). Leader wait checks liveness.
            append_startup_diagnostics(
                root,
                run_id=run_id,
                team_id=team_id,
                worker_id=worker_id,
                lines=[f"provider_exit_after_ready rc={rc}"],
            )
        return int(rc if rc is not None else 1)
    except SupervisorError as exc:
        write_startup_phase(
            root,
            run_id=run_id,
            team_id=team_id,
            worker_id=worker_id,
            phase=StartupPhase.FAILED,
            provider=provider,
            supervisor_pid=supervisor_pid,
            provider_pid=provider_pid,
            provider_pgid=provider_pgid,
            provider_pid_start=provider_start,
            evidence_code=EvidenceCode.MALFORMED,
            failure_reason=str(exc),
        )
        if proc is not None and _child_alive(proc):
            try:
                if provider_pgid:
                    os.killpg(provider_pgid, signal.SIGTERM)
                else:
                    proc.terminate()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    if provider_pgid:
                        os.killpg(provider_pgid, signal.SIGKILL)
                    else:
                        proc.kill()
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        return 1
    except StartupError as exc:
        sys.stderr.write(f"team supervisor startup error: {exc}\n")
        return 1
    finally:
        if proc is not None and proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass


__all__ = [
    "DESCRIPTOR_SCHEMA_VERSION",
    "DESCRIPTOR_KIND",
    "SupervisorError",
    "write_provider_descriptor",
    "load_provider_descriptor",
    "run_supervisor",
]
