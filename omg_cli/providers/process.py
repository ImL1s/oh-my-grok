"""Bounded argv probe runner with process-group cleanup (#67-A).

Probes use ``shell=False``, an allowlisted env (caller-supplied), byte-capped
stdout/stderr, and on POSIX ``start_new_session`` so timeout/cancel/overflow
can ``killpg`` the whole child tree. Windows gets best-effort ``proc.kill()``
only — process-tree cancel is not claimed there.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Final, Mapping, Sequence

DEFAULT_PROBE_TIMEOUT_S: Final[float] = 8.0
DEFAULT_MAX_OUTPUT_BYTES: Final[int] = 256_000


@dataclass(frozen=True, slots=True)
class ProbeProcessResult:
    """Outcome of one fixed-argv provider probe."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    overflow: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    bytes_stdout: int = 0
    bytes_stderr: int = 0

    def combined_text(self) -> str:
        return ((self.stdout or "") + (self.stderr or "")).strip()


class ProbeProcessError(RuntimeError):
    """Invalid argv / launch contract for a provider probe."""


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ProbeProcessError("probe argv must be a non-empty list/tuple of str")
    out: list[str] = []
    for i, part in enumerate(argv):
        if not isinstance(part, str):
            raise ProbeProcessError(f"probe argv[{i}] must be str, got {type(part).__name__}")
        if part == "":
            raise ProbeProcessError(f"probe argv[{i}] must not be empty")
        if "\x00" in part:
            raise ProbeProcessError(f"probe argv[{i}] must not contain NUL")
        out.append(part)
    return tuple(out)


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Best-effort kill of the probe process group (POSIX) or process (else)."""
    try:
        if os.name == "posix" and proc.pid:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def _drain_pipe(
    pipe,
    *,
    max_bytes: int,
    sink: bytearray,
    overflow_flag: list[bool],
    stop: threading.Event,
) -> None:
    try:
        while not stop.is_set():
            chunk = pipe.read(4096)
            if not chunk:
                break
            remaining = max_bytes - len(sink)
            if remaining <= 0:
                overflow_flag[0] = True
                break
            if len(chunk) > remaining:
                sink.extend(chunk[:remaining])
                overflow_flag[0] = True
                break
            sink.extend(chunk)
    except (ValueError, OSError):
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def run_probe_process(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    cancel_event: threading.Event | None = None,
) -> ProbeProcessResult:
    """Run a fixed argv probe; kill the process group on timeout/cancel/overflow."""
    clean_argv = _validate_argv(argv)
    if timeout_s <= 0:
        raise ProbeProcessError("timeout_s must be positive")
    if max_output_bytes <= 0:
        raise ProbeProcessError("max_output_bytes must be positive")

    child_env = dict(env) if env is not None else {}
    popen_kwargs: dict = {
        "args": list(clean_argv),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "shell": False,
        "env": child_env,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(**popen_kwargs)
    except OSError as exc:
        raise ProbeProcessError(f"failed to spawn probe: {exc}") from exc

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    stdout_overflow = [False]
    stderr_overflow = [False]
    stop_readers = threading.Event()
    readers = [
        threading.Thread(
            target=_drain_pipe,
            kwargs={
                "pipe": proc.stdout,
                "max_bytes": max_output_bytes,
                "sink": stdout_buf,
                "overflow_flag": stdout_overflow,
                "stop": stop_readers,
            },
            daemon=True,
        ),
        threading.Thread(
            target=_drain_pipe,
            kwargs={
                "pipe": proc.stderr,
                "max_bytes": max_output_bytes,
                "sink": stderr_buf,
                "overflow_flag": stderr_overflow,
                "stop": stop_readers,
            },
            daemon=True,
        ),
    ]
    for t in readers:
        t.start()

    timed_out = False
    cancelled = False
    overflow = False
    deadline = time.monotonic() + float(timeout_s)
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _kill_tree(proc)
                break
            if stdout_overflow[0] or stderr_overflow[0]:
                overflow = True
                _kill_tree(proc)
                break
            rc = proc.poll()
            if rc is not None:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _kill_tree(proc)
                break
            time.sleep(0.02)
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
    finally:
        stop_readers.set()
        for t in readers:
            t.join(timeout=1.0)
        # Ensure pipes closed if readers died early
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:
                    pass

    overflow = overflow or stdout_overflow[0] or stderr_overflow[0]
    returncode = int(proc.returncode if proc.returncode is not None else -9)
    if timed_out or cancelled or overflow:
        # Distinct sentinel when we forced termination before a natural exit.
        if proc.returncode is None:
            returncode = -9

    def _decode(buf: bytearray) -> str:
        return bytes(buf).decode("utf-8", errors="replace")

    return ProbeProcessResult(
        argv=clean_argv,
        returncode=returncode,
        stdout=_decode(stdout_buf),
        stderr=_decode(stderr_buf),
        timed_out=timed_out,
        cancelled=cancelled,
        overflow=overflow,
        stdout_truncated=stdout_overflow[0],
        stderr_truncated=stderr_overflow[0],
        bytes_stdout=len(stdout_buf),
        bytes_stderr=len(stderr_buf),
    )


__all__ = [
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_PROBE_TIMEOUT_S",
    "ProbeProcessError",
    "ProbeProcessResult",
    "run_probe_process",
]
