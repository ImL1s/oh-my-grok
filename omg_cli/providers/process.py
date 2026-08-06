"""Bounded argv probe runner with process-group cleanup (#67-A).

Probes use ``shell=False``, an allowlisted env (caller-supplied), byte-capped
stdout/stderr, and on POSIX ``start_new_session`` so timeout/cancel/overflow/
KeyboardInterrupt (and other BaseException unwind) can ``killpg`` the whole
child tree. Windows gets best-effort ``proc.kill()`` only — process-tree cancel
is not claimed there.

Any exception after a successful ``Popen`` must ``_kill_tree`` (including
failures while allocating lists / closures / buffers / starting reader
threads, and during result construction). ``Popen`` itself sits inside the
same BaseException kill region (``proc = None`` then nested OSError convert)
so async exceptions cannot skip cleanup between spawn and setup.
``cancel_event`` is honored through the wait/join/close/result window so
Ctrl-C cannot silently skip cleanup. Success paths drain readers to EOF before
forcing ``stop``; premature stop sets ``stdout_truncated`` / ``stderr_truncated``.
Hung readers kill the process tree *before* closing pipes / joining, and pipe
closes are time-bounded so an escaped helper holding a buffered-I/O lock cannot
deadlock cancel forever.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Final, Mapping, Sequence

# Local aliases so post-Popen allocation failures remain injectable in tests.
_bytearray = bytearray


def _post_popen_begin() -> None:
    """First action inside the post-Popen kill-on-exception region (test hook)."""
    return None


DEFAULT_PROBE_TIMEOUT_S: Final[float] = 8.0
DEFAULT_MAX_OUTPUT_BYTES: Final[int] = 256_000
_PIPE_CLOSE_TIMEOUT_S: float = 1.0


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
    # POSIX session leader / process-group id (child pid with start_new_session).
    # Callers may killpg(pid) if cancel flips after return.
    pid: int = 0

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


def _close_pipe_bounded(pipe, *, timeout_s: float | None = None) -> None:
    """Close a pipe without blocking forever on a contended buffered-I/O lock.

    Escaped helpers that left the process group may still hold the write end;
    a synchronous ``pipe.close()`` in the parent can then wait on the reader
    thread's lock indefinitely. Fail-closed: bound the wait and move on.
    """
    if pipe is None:
        return
    limit = float(_PIPE_CLOSE_TIMEOUT_S if timeout_s is None else timeout_s)
    done = threading.Event()

    def _do_close() -> None:
        try:
            pipe.close()
        except Exception:
            pass
        finally:
            done.set()

    closer = threading.Thread(target=_do_close, daemon=True)
    closer.start()
    if not done.wait(timeout=limit):
        # Abandon the close; caller already killed the tree (or will).
        return
    try:
        closer.join(timeout=0.1)
    except BaseException:
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
        # Kill the tree first so orphan pipe holders release FDs; closing a
        # still-owned buffered pipe can otherwise block on the I/O lock.
        _kill_tree(proc)
        stop.set()
        for pipe in (proc.stdout, proc.stderr):
            _close_pipe_bounded(pipe)
        for t in readers:
            try:
                t.join(timeout=1.0)
            except BaseException:
                _kill_tree(proc)
                raise
        # Still hung after kill + bounded close: kill again and return
        # (never block forever waiting on pipe holders).
        if any(t.is_alive() for t in readers):
            _kill_tree(proc)
    else:
        for pipe in (proc.stdout, proc.stderr):
            _close_pipe_bounded(pipe)


def _close_pipes(proc: subprocess.Popen[bytes]) -> None:
    for pipe in (proc.stdout, proc.stderr):
        _close_pipe_bounded(pipe)


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

    # Unbound-safe defaults BEFORE Popen — if allocation fails here there is no
    # child to reap. Popen + all post-spawn work share ONE BaseException region
    # so async exceptions cannot skip _kill_tree between spawn and setup.
    proc: subprocess.Popen[bytes] | None = None
    started_readers: list[threading.Thread] = []
    stop_readers: threading.Event | None = None
    stdout_buf: bytearray | None = None
    stderr_buf: bytearray | None = None
    stdout_overflow = [False]
    stderr_overflow = [False]
    stdout_early_stop = [False]
    stderr_early_stop = [False]
    timed_out = False
    cancelled = False
    overflow = False

    try:
        try:
            proc = subprocess.Popen(**popen_kwargs)
        except OSError as exc:
            raise ProbeProcessError(f"failed to spawn probe: {exc}") from exc

        # Earliest post-Popen window — injectable for OOM coverage before any
        # list/closure/buffer work (must still hit kill-on-BaseException).
        _post_popen_begin()

        started_readers = []
        stdout_overflow = [False]
        stderr_overflow = [False]
        stdout_early_stop = [False]
        stderr_early_stop = [False]

        def _cancel_requested() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        def _honor_late_cancel() -> None:
            """Re-check cancel_event through wait/join/close (SIGINT sets event only)."""
            nonlocal cancelled
            if _cancel_requested():
                cancelled = True
                _kill_tree(proc)

        # Buffer / Event setup remains inside the same kill-on-BaseException region.
        stdout_buf = _bytearray()
        stderr_buf = _bytearray()
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
        _honor_late_cancel()
    except BaseException:
        # Covers Popen-boundary async exceptions, earliest post-Popen setup,
        # buffer/Event alloc, reader start, KeyboardInterrupt during poll/wait.
        # Do not depend on nested defs — they may not exist yet.
        if proc is not None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
            _kill_tree(proc)
            try:
                proc.wait(timeout=2.0)
            except (subprocess.TimeoutExpired, OSError):
                pass
        raise
    finally:
        if proc is None:
            pass
        else:
            # cancel_event may flip during join/close; check before, after join,
            # and once more so late Ctrl-C still reaps the tree.
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _kill_tree(proc)
            try:
                if started_readers and stop_readers is not None:
                    _join_readers(
                        started_readers,
                        stop=stop_readers,
                        stdout_early=stdout_early_stop,
                        stderr_early=stderr_early_stop,
                        proc=proc,
                    )
                else:
                    _close_pipes(proc)
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    _kill_tree(proc)
            except BaseException:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                _kill_tree(proc)
                if stop_readers is not None:
                    stop_readers.set()
                _close_pipes(proc)
                for t in started_readers:
                    try:
                        t.join(timeout=0.5)
                    except BaseException:
                        pass
                raise
            finally:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    _kill_tree(proc)

    # Result construction is still kill-protected: cancel / BaseException here
    # must _kill_tree before unwinding (descendants may outlive the direct child).
    assert proc is not None
    try:
        overflow = overflow or stdout_overflow[0] or stderr_overflow[0]
        stdout_truncated = bool(stdout_overflow[0] or stdout_early_stop[0])
        stderr_truncated = bool(stderr_overflow[0] or stderr_early_stop[0])
        returncode = int(proc.returncode if proc.returncode is not None else -9)
        if timed_out or cancelled or overflow:
            # Distinct sentinel when we forced termination before a natural exit.
            if proc.returncode is None:
                returncode = -9

        assert stdout_buf is not None and stderr_buf is not None

        def _decode(buf: bytearray) -> str:
            return bytes(buf).decode("utf-8", errors="replace")

        result = ProbeProcessResult(
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
            pid=int(proc.pid or 0),
        )
        # Final cancel check after construction / before return.
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            _kill_tree(proc)
            if not result.cancelled:
                result = ProbeProcessResult(
                    argv=result.argv,
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    timed_out=result.timed_out,
                    cancelled=True,
                    overflow=result.overflow,
                    stdout_truncated=result.stdout_truncated,
                    stderr_truncated=result.stderr_truncated,
                    bytes_stdout=result.bytes_stdout,
                    bytes_stderr=result.bytes_stderr,
                    pid=result.pid,
                )
        return result
    except BaseException:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
        _kill_tree(proc)
        raise


__all__ = [
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_PROBE_TIMEOUT_S",
    "ProbeProcessError",
    "ProbeProcessResult",
    "run_probe_process",
]
