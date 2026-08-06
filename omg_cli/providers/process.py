"""Bounded argv probe runner with process-group cleanup (#67-A).

Probes use ``shell=False``, an allowlisted env (caller-supplied), byte-capped
stdout/stderr, and on POSIX ``start_new_session`` so timeout/cancel/overflow/
KeyboardInterrupt (and other BaseException unwind) can ``killpg`` the whole
child tree. Windows gets best-effort ``proc.kill()`` only — process-tree cancel
is not claimed there.

Any exception after a successful ``Popen`` must ``_kill_tree`` (including
failures while starting reader threads). ``cancel_event`` is honored through
the wait/join/close window so Ctrl-C cannot silently skip cleanup. Success
paths drain readers to EOF before forcing ``stop``; premature stop sets
``stdout_truncated`` / ``stderr_truncated``.
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
    early_stop_flag: list[bool],
) -> None:
    """Read until EOF, byte cap, or forced ``stop`` (which marks early truncation)."""
    try:
        while True:
            if stop.is_set():
                early_stop_flag[0] = True
                break
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


def _join_readers(
    readers: list[threading.Thread],
    *,
    stop: threading.Event,
    stdout_early: list[bool],
    stderr_early: list[bool],
    proc: subprocess.Popen[bytes],
) -> None:
    """Prefer EOF drain; force-stop + truncation flags only if readers hang."""
    for t in readers:
        try:
            t.join(timeout=2.0)
        except BaseException:
            stop.set()
            _kill_tree(proc)
            raise
    hung = False
    for idx, t in enumerate(readers):
        if t.is_alive():
            hung = True
            if idx == 0:
                stdout_early[0] = True
            else:
                stderr_early[0] = True
    if hung:
        stop.set()
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:
                    pass
        for t in readers:
            try:
                t.join(timeout=1.0)
            except BaseException:
                _kill_tree(proc)
                raise
    else:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:
                    pass


def _close_pipes(proc: subprocess.Popen[bytes]) -> None:
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
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
    """Run a fixed argv probe; kill the process group on timeout/cancel/overflow/interrupt."""
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
    stdout_early_stop = [False]
    stderr_early_stop = [False]
    stop_readers = threading.Event()
    readers: list[threading.Thread] = []
    started_readers: list[threading.Thread] = []
    timed_out = False
    cancelled = False
    overflow = False

    def _cancel_requested() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def _note_cancel_and_kill() -> None:
        nonlocal cancelled
        if _cancel_requested():
            cancelled = True
        _kill_tree(proc)

    try:
        try:
            readers = [
                threading.Thread(
                    target=_drain_pipe,
                    kwargs={
                        "pipe": proc.stdout,
                        "max_bytes": max_output_bytes,
                        "sink": stdout_buf,
                        "overflow_flag": stdout_overflow,
                        "stop": stop_readers,
                        "early_stop_flag": stdout_early_stop,
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
                        "early_stop_flag": stderr_early_stop,
                    },
                    daemon=True,
                ),
            ]
            for t in readers:
                t.start()
                started_readers.append(t)

            deadline = time.monotonic() + float(timeout_s)
            while True:
                if _cancel_requested():
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
            # SIGINT may arrive after poll() saw exit — honor cancel through
            # the wait/join window so descendants still get killpg.
            if _cancel_requested():
                cancelled = True
                _kill_tree(proc)
        except BaseException:
            # Covers reader start failures, KeyboardInterrupt during poll/wait,
            # and any other unwind after a successful Popen.
            _note_cancel_and_kill()
            try:
                proc.wait(timeout=2.0)
            except (subprocess.TimeoutExpired, OSError):
                pass
            raise
    finally:
        if _cancel_requested():
            cancelled = True
            _kill_tree(proc)
        try:
            if started_readers:
                _join_readers(
                    started_readers,
                    stop=stop_readers,
                    stdout_early=stdout_early_stop,
                    stderr_early=stderr_early_stop,
                    proc=proc,
                )
            else:
                _close_pipes(proc)
        except BaseException:
            _note_cancel_and_kill()
            stop_readers.set()
            _close_pipes(proc)
            for t in started_readers:
                try:
                    t.join(timeout=0.5)
                except BaseException:
                    pass
            raise

    overflow = overflow or stdout_overflow[0] or stderr_overflow[0]
    stdout_truncated = bool(stdout_overflow[0] or stdout_early_stop[0])
    stderr_truncated = bool(stderr_overflow[0] or stderr_early_stop[0])
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
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
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
