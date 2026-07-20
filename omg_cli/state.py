# omg_cli/state.py
"""Authoritative run-state single-writer for oh-my-grok.

Only the omg CLI (this module) may mutate status / passes / verified under
``.omg/state/runs/<run_id>/``. Hooks and agents may only append events or write
proposals under ``.omg/artifacts/``.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, IO

try:
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX
    fcntl = None  # type: ignore[assignment]

# Keep the real low-level launcher for PID identity probes.  Test suites and
# callers commonly monkeypatch ``subprocess.Popen`` to isolate Grok launches;
# that must not corrupt the lifecycle owner's own start-time identity.
_SYSTEM_POPEN = subprocess.Popen


OMG_SUBDIRS = (
    "state",
    "state/runs",
    "plans",
    "research",
    "handoffs",
    "artifacts",
    "ultragoal",
)


def ensure_omg_dirs(root: Path) -> Path:
    root = Path(root)
    for sub in OMG_SUBDIRS:
        (root / ".omg" / sub).mkdir(parents=True, exist_ok=True)
    return root


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runs_dir(root: Path) -> Path:
    return Path(root) / ".omg" / "state" / "runs"


def _active_path(root: Path) -> Path:
    return Path(root) / ".omg" / "state" / "active.json"


def _create_lock_path(root: Path) -> Path:
    return Path(root) / ".omg" / "state" / "create.lock"


def _status_path(root: Path, run_id: str) -> Path:
    return _runs_dir(root) / run_id / "status.json"


def _run_dir(root: Path, run_id: str) -> Path:
    return _runs_dir(root) / run_id


def _execution_lock_path(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "execution.lock"


def _execution_lease_path(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "execution.lease.json"


def _transition_lock_path(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "transition.lock"


def _cancel_request_path(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "cancel.request.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Durably replace JSON on the same filesystem.

    File fsync + atomic replace prevents torn JSON; directory fsync makes the
    replacement durable across a crash.  Concurrency is provided separately by
    the execution/transition locks below.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if os.name == "posix":
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _make_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{uuid.uuid4().hex[:8]}"


# Statuses that block a second concurrent create_run (active mutex).
ACTIVE_NON_TERMINAL_STATUSES = frozenset(
    {"initialized", "running", "verifying", "blocked"}
)
TERMINAL_STATUSES = frozenset(
    {"cancelled", "completed", "failed", "verified"}
)


class RunSchema(str, Enum):
    """Frozen dispatch labels for legacy and strict lifecycle state."""

    LEGACY_V1 = "legacy-v1"
    STRICT_V2 = "strict-v2"


class LifecycleLockError(RuntimeError):
    """Base class for strict lifecycle locking/fencing failures."""


class LockUnavailableError(LifecycleLockError):
    """Reliable POSIX advisory locking is unavailable."""


class ExecutionLeaseBusy(LifecycleLockError):
    """Another process owns the bounded per-run execution lease."""


class FencingError(PermissionError):
    """A stale or non-owner execution generation attempted a strict write."""


_LOCK_LOCAL = threading.local()


def _held_lock_kinds() -> list[str]:
    stack = getattr(_LOCK_LOCAL, "stack", None)
    if stack is None:
        stack = []
        _LOCK_LOCAL.stack = stack
    return stack


def _push_lock(kind: str) -> None:
    stack = _held_lock_kinds()
    if kind == "execution" and "transition" in stack:
        raise LifecycleLockError(
            "lock-order violation: execution.lock cannot be acquired while "
            "transition.lock is held"
        )
    stack.append(kind)


def _pop_lock(kind: str) -> None:
    stack = _held_lock_kinds()
    if not stack or stack[-1] != kind:
        raise LifecycleLockError(f"lock stack corruption while releasing {kind}")
    stack.pop()


def transition_guard_held() -> bool:
    """Diagnostic/test hook: true only inside the current thread's short guard."""
    return "transition" in _held_lock_kinds()


def classify_run_schema(run: dict[str, Any]) -> RunSchema:
    """Classify a run without inferring its schema from files or command name.

    Missing ``schema_version`` and integer ``1`` are the frozen legacy-v1
    adapter.  Strict-v2 requires both integer version fields to be ``2``.
    Booleans are rejected explicitly because ``bool`` is an ``int`` subclass.
    Malformed, negative and future versions fail closed.
    """

    if not isinstance(run, dict):
        raise TypeError("run schema classification requires an object")

    if "schema_version" not in run:
        return RunSchema.LEGACY_V1

    schema = run.get("schema_version")
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise TypeError("schema_version must be an integer 1 or 2")
    if schema == 1:
        lifecycle = run.get("lifecycle_version", 1)
        if isinstance(lifecycle, bool) or not isinstance(lifecycle, int):
            raise TypeError("lifecycle_version must be integer 1 for legacy schema")
        if lifecycle != 1:
            raise ValueError(
                f"unsupported legacy lifecycle_version={lifecycle!r}; expected 1"
            )
        return RunSchema.LEGACY_V1
    if schema == 2:
        lifecycle = run.get("lifecycle_version")
        if isinstance(lifecycle, bool) or not isinstance(lifecycle, int):
            raise TypeError("strict schema requires integer lifecycle_version=2")
        if lifecycle != 2:
            raise ValueError(
                f"unsupported strict lifecycle_version={lifecycle!r}; expected 2"
            )
        return RunSchema.STRICT_V2
    if schema < 0:
        raise ValueError(f"schema_version must not be negative: {schema}")
    raise ValueError(f"unsupported schema_version={schema!r}; expected 1 or 2")


def _pid_alive(pid: int) -> bool | None:
    """Check whether *pid* exists (signal 0).

    Returns True if alive (or PermissionError — process exists), False if
    ESRCH / ProcessLookupError (gone), None if other OSError.
    """
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def _run_pid_path(root: Path, run_id: str) -> Path:
    return _runs_dir(root) / run_id / "pid"


def _run_pid_json_path(root: Path, run_id: str) -> Path:
    return _runs_dir(root) / run_id / "pid.json"


def process_starttime(pid: int) -> str | None:
    """Best-effort process start time string (macOS/Linux ``ps -o lstart=``)."""
    if pid <= 0:
        return None
    try:
        proc = _SYSTEM_POPEN(
            ["ps", "-p", str(pid), "-o", "lstart="],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, _stderr = proc.communicate(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return None
    if proc.returncode != 0:
        return None
    text = (stdout or "").strip()
    return text or None


def write_pid_metadata(
    path: Path,
    *,
    pid: int,
    pgid: int | None = None,
    starttime: str | None = None,
) -> dict[str, Any]:
    """Write ``pid.json`` (and legacy plain ``pid`` file when path is run-dir pid.json).

    Shape: ``{pid, starttime, pgid}``. Used by leader launch and workers/*.pid.json.
    """
    path = Path(path)
    if starttime is None:
        starttime = process_starttime(pid)
    if pgid is None and os.name == "posix":
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = pid
    meta: dict[str, Any] = {
        "pid": int(pid),
        "starttime": starttime,
        "pgid": int(pgid) if pgid is not None else None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Legacy plain pid sibling for older cancel readers
    if path.name == "pid.json":
        plain = path.with_name("pid")
        plain.write_text(f"{int(pid)}\n", encoding="utf-8")
    return meta


def read_pid_metadata(root: Path, run_id: str) -> dict[str, Any] | None:
    """Load pid.json if present; else fall back to plain pid file."""
    root = Path(root)
    jpath = _run_pid_json_path(root, run_id)
    if jpath.is_file():
        data = _read_json(jpath)
        if data and isinstance(data.get("pid"), int):
            return data
        # tolerate pid as string
        if data and data.get("pid") is not None:
            try:
                data = dict(data)
                data["pid"] = int(data["pid"])
                return data
            except (TypeError, ValueError):
                pass
    pid_path = _run_pid_path(root, run_id)
    if pid_path.is_file():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return {"pid": pid, "starttime": None, "pgid": None}
    return None


def pid_matches_recorded(
    pid: int,
    recorded_starttime: str | None,
) -> bool:
    """True when it is safe to signal *pid* for this recorded starttime.

    Fail-closed (strictest kill safety):
    * No recorded starttime (legacy plain ``pid`` file) → **False** — never
      auto-kill without a starttime identity check.
    * ``ps`` fails / unavailable → **False** — do not signal on uncertainty.
    * Process dead (ESRCH) → **False**.
    * starttime mismatch → **False** (PID reuse).
    * Alive + matching ``ps -o lstart=`` → **True**.
    """
    if pid <= 0:
        return False
    if not recorded_starttime:
        return False
    alive = _pid_alive(pid)
    if alive is False:
        return False
    current = process_starttime(pid)
    if current is None:
        # ps failed — fail-closed, do not kill
        return False
    return current == recorded_starttime


def _require_posix_flock() -> None:
    if os.name != "posix" or fcntl is None:
        raise LockUnavailableError(
            "strict cross-process lifecycle requires POSIX fcntl.flock; "
            "refusing before host launch"
        )


def _flock_bounded(lockf: IO[str], *, timeout_s: float, label: str) -> None:
    """Acquire an exclusive flock without waiting beyond ``timeout_s``."""
    _require_posix_flock()
    if timeout_s < 0:
        raise ValueError("lock timeout must be non-negative")
    deadline = time.monotonic() + float(timeout_s)
    while True:
        try:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except (BlockingIOError, OSError) as exc:
            # EACCES/EAGAIN are represented as BlockingIOError on supported
            # POSIX hosts.  Other OSErrors fail closed instead of pretending an
            # atomic replace is a concurrency primitive.
            if not isinstance(exc, BlockingIOError):
                import errno

                if getattr(exc, "errno", None) not in (errno.EACCES, errno.EAGAIN):
                    raise LockUnavailableError(f"cannot acquire {label}: {exc}") from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ExecutionLeaseBusy(f"timed out acquiring {label}") from exc
            time.sleep(min(0.025, remaining))


def _validated_generation(meta: dict[str, Any] | None) -> int:
    if meta is None:
        return 0
    value = meta.get("generation", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LifecycleLockError("execution lease generation is malformed")
    return value


def _load_lease_metadata(root: Path, run_id: str) -> dict[str, Any] | None:
    path = _execution_lease_path(root, run_id)
    data = _read_json(path)
    if path.exists() and data is None:
        raise LifecycleLockError(
            f"execution lease metadata is unreadable for run_id={run_id!r}"
        )
    return data


def _lease_owner_is_live(meta: dict[str, Any]) -> bool:
    pid = meta.get("pid")
    started = meta.get("process_starttime")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if not isinstance(started, str) or not started:
        return False
    return pid_matches_recorded(pid, started)


@dataclass
class ExecutionLease:
    """Held POSIX execution lease and monotonic fencing token."""

    root: Path
    run_id: str
    intent: str
    timeout_s: float = 5.0
    invocation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    generation: int = 0
    pid: int = field(default_factory=os.getpid)
    process_starttime: str | None = None
    acquired_at: str | None = None
    stale_owner_recovered: bool = False
    _lockf: IO[str] | None = field(default=None, init=False, repr=False)
    _acquired: bool = field(default=False, init=False, repr=False)

    def __enter__(self) -> "ExecutionLease":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self) -> "ExecutionLease":
        if self._acquired:
            return self
        if "transition" in _held_lock_kinds():
            raise LifecycleLockError(
                "lock-order violation: cannot wait for execution while transition is held"
            )
        _require_posix_flock()

        current = load_run(self.root, self.run_id)
        if current is None:
            raise FileNotFoundError(f"no status.json for run_id={self.run_id!r}")
        if classify_run_schema(current) is not RunSchema.STRICT_V2:
            raise LifecycleLockError("execution lease is reserved for strict-v2 runs")

        self.process_starttime = process_starttime(self.pid)
        if not self.process_starttime:
            raise LockUnavailableError(
                "cannot establish execution-owner PID starttime; refusing strict launch"
            )

        path = _execution_lock_path(self.root, self.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lockf = path.open("a+", encoding="utf-8")
        try:
            _flock_bounded(
                lockf,
                timeout_s=self.timeout_s,
                label=f"execution.lock for run {self.run_id}",
            )
        except BaseException:
            lockf.close()
            owner = None
            try:
                owner = _load_lease_metadata(self.root, self.run_id)
            except LifecycleLockError:
                pass
            if owner:
                raise ExecutionLeaseBusy(
                    "execution lease busy for "
                    f"run_id={self.run_id!r}; owner={owner.get('invocation_id')!r} "
                    f"pid={owner.get('pid')!r} generation={owner.get('generation')!r}; "
                    f"retry: omg ralph --resume {self.run_id}"
                )
            raise

        _push_lock("execution")
        self._lockf = lockf
        try:
            # Re-read after lock acquisition; a malformed/future schema never
            # receives lease/session mutation.
            current = load_run(self.root, self.run_id)
            if current is None:
                raise FileNotFoundError(f"no status.json for run_id={self.run_id!r}")
            if classify_run_schema(current) is not RunSchema.STRICT_V2:
                raise LifecycleLockError("run schema changed while acquiring execution lease")

            previous = _load_lease_metadata(self.root, self.run_id)
            self.generation = _validated_generation(previous) + 1
            self.stale_owner_recovered = bool(
                previous
                and previous.get("state") == "held"
                and not _lease_owner_is_live(previous)
            )
            self.acquired_at = _utc_now()
            payload: dict[str, Any] = {
                "writer": "omg-cli",
                "run_id": self.run_id,
                "invocation_id": self.invocation_id,
                "pid": self.pid,
                "process_starttime": self.process_starttime,
                "intent": self.intent,
                "generation": self.generation,
                "state": "held",
                "acquired_at": self.acquired_at,
                "heartbeat_at": self.acquired_at,
                "stale_owner_recovered": self.stale_owner_recovered,
            }
            if previous:
                payload["previous_invocation_id"] = previous.get("invocation_id")
                payload["previous_generation"] = previous.get("generation")
            _atomic_write_json(_execution_lease_path(self.root, self.run_id), payload)
            self._acquired = True
            return self
        except BaseException:
            try:
                _pop_lock("execution")
            finally:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
                lockf.close()
                self._lockf = None
            raise

    def assert_current(self) -> dict[str, Any]:
        if not self._acquired or self._lockf is None:
            raise FencingError("execution lease is not held")
        meta = _load_lease_metadata(self.root, self.run_id)
        if meta is None:
            raise FencingError("execution lease metadata is missing")
        expected = (
            self.invocation_id,
            self.generation,
            self.pid,
            self.process_starttime,
            "held",
        )
        actual = (
            meta.get("invocation_id"),
            meta.get("generation"),
            meta.get("pid"),
            meta.get("process_starttime"),
            meta.get("state"),
        )
        if actual != expected:
            raise FencingError(
                f"stale execution fencing token for run_id={self.run_id!r}: "
                f"expected invocation/generation={expected[:2]!r}, "
                f"current={actual[:2]!r}"
            )
        return meta

    def release(self) -> None:
        if not self._acquired or self._lockf is None:
            return
        lockf = self._lockf
        try:
            try:
                meta = self.assert_current()
                released = dict(meta)
                released["state"] = "released"
                released["released_at"] = _utc_now()
                released["heartbeat_at"] = released["released_at"]
                _atomic_write_json(
                    _execution_lease_path(self.root, self.run_id), released
                )
            finally:
                self._acquired = False
                self._lockf = None
                _pop_lock("execution")
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
                lockf.close()
        except BaseException:
            # The OS lock must never leak because diagnostic metadata failed.
            if not lockf.closed:
                try:
                    fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
                finally:
                    lockf.close()
            self._acquired = False
            self._lockf = None
            if _held_lock_kinds() and _held_lock_kinds()[-1] == "execution":
                _pop_lock("execution")
            raise


def execution_lease(
    root: Path,
    run_id: str,
    *,
    intent: str,
    timeout_s: float = 5.0,
    invocation_id: str | None = None,
) -> ExecutionLease:
    """Build a lease context manager; acquisition occurs on ``with`` entry."""
    kwargs: dict[str, Any] = {}
    if invocation_id is not None:
        kwargs["invocation_id"] = invocation_id
    return ExecutionLease(
        root=Path(root),
        run_id=run_id,
        intent=intent,
        timeout_s=float(timeout_s),
        **kwargs,
    )


@dataclass
class TransitionGuard:
    """Short per-run linearization guard for strict status/cancel commits."""

    root: Path
    run_id: str
    timeout_s: float = 5.0
    _lockf: IO[str] | None = field(default=None, init=False, repr=False)

    def __enter__(self) -> "TransitionGuard":
        _require_posix_flock()
        path = _transition_lock_path(self.root, self.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lockf = path.open("a+", encoding="utf-8")
        try:
            _flock_bounded(
                lockf,
                timeout_s=self.timeout_s,
                label=f"transition.lock for run {self.run_id}",
            )
            _push_lock("transition")
        except BaseException:
            lockf.close()
            raise
        self._lockf = lockf
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        lockf = self._lockf
        if lockf is None:
            return
        self._lockf = None
        _pop_lock("transition")
        fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
        lockf.close()


def transition_guard(
    root: Path, run_id: str, *, timeout_s: float = 5.0
) -> TransitionGuard:
    return TransitionGuard(Path(root), run_id, float(timeout_s))


def is_stale_run(root: Path, run_id: str) -> bool:
    """True when a pid file exists and the process is gone (ESRCH).

    No pid file, unreadable pid, or indeterminate liveness → not stale
    (mutex still applies unless force supersede). When starttime is recorded
    and no longer matches (PID reuse), treat as stale so create can proceed.
    """
    meta = read_pid_metadata(Path(root), run_id)
    if meta is None:
        return False
    pid = int(meta["pid"])
    recorded = meta.get("starttime")
    recorded_s = recorded if isinstance(recorded, str) and recorded else None
    if recorded_s and not pid_matches_recorded(pid, recorded_s):
        # Dead or PID reused under a recorded starttime → reclaimable
        return True
    return _pid_alive(pid) is False


def _create_run_unlocked(
    root: Path,
    *,
    mode: str,
    goal: str,
    extra: dict[str, Any] | None = None,
    force: bool = False,
    kill_grace_s: float = 0.0,
) -> dict[str, Any]:
    """Inner create_run body (caller holds create.lock when available)."""
    active = load_active_run(root)
    if active is not None:
        st = str(active.get("status") or "")
        if st in ACTIVE_NON_TERMINAL_STATUSES:
            old_id = str(active.get("run_id") or "")
            stale = bool(old_id) and is_stale_run(root, old_id)
            if not force and not stale:
                raise RuntimeError(
                    "active run already exists: "
                    f"run_id={active.get('run_id')!r} status={st!r}; "
                    "cancel it first or pass force=True"
                )
            # Supersede (force) or reclaim stale: cancel/kill old active run.
            if old_id:
                try:
                    cancel_run(
                        root,
                        old_id,
                        kill_grace_s=kill_grace_s if force else 0.0,
                    )
                except FileNotFoundError:
                    # Race: status vanished; clear pointer and continue.
                    clear_active(root, old_id)

    run_id = _make_run_id()
    now = _utc_now()
    status: dict[str, Any] = {
        "run_id": run_id,
        "mode": mode,
        "goal": goal,
        "status": "initialized",
        "verified": False,
        "passes": 0,
        "created_at": now,
        "updated_at": now,
    }
    if extra:
        # Never allow callers to smuggle verified=true on create
        safe_extra = {k: v for k, v in extra.items() if k != "verified"}
        status.update(safe_extra)
        status["verified"] = False
        status["run_id"] = run_id
        status["status"] = "initialized"

    run_dir = _runs_dir(root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "workers").mkdir(exist_ok=True)
    _atomic_write_json(_status_path(root, run_id), status)
    _atomic_write_json(_active_path(root), {"run_id": run_id, "updated_at": now})
    return status


def create_run(
    root: Path,
    *,
    mode: str,
    goal: str,
    extra: dict[str, Any] | None = None,
    force: bool = False,
    kill_grace_s: float = 0.0,
) -> dict[str, Any]:
    """Create a new run directory + status.json and point active.json at it.

    Serializes concurrent creates via ``fcntl.flock`` on
    ``.omg/state/create.lock`` (POSIX). Refuses when an active run exists with
    status in ``{initialized, running, verifying}`` unless:

    * ``force=True`` — **supersede**: cancel/kill the old active run first
      (best-effort process kill via pid file, then mark cancelled), or
    * the active run is **stale** (pid file present and process ESRCH) — the
      dead run is cancelled and create proceeds without force.

    Terminal statuses (cancelled/completed/failed/verified) do not block.
    """
    root = Path(root)
    ensure_omg_dirs(root)

    lock_path = _create_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None:
        return _create_run_unlocked(
            root,
            mode=mode,
            goal=goal,
            extra=extra,
            force=force,
            kill_grace_s=kill_grace_s,
        )

    with lock_path.open("a+", encoding="utf-8") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            return _create_run_unlocked(
                root,
                mode=mode,
                goal=goal,
                extra=extra,
                force=force,
                kill_grace_s=kill_grace_s,
            )
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


# Keys that extra must never override; status param / identity fields win.
_WRITE_STATUS_RESERVED = frozenset(
    {
        "status",
        "run_id",
        "verified",
        "created_at",
        # Acceptance authority is process-local by contract.  It may never be
        # smuggled into JSON through generic status extras.
        "acceptance_capability",
        "acceptance_token",
        "cli_acceptance_token",
    }
)

_STRICT_STATUSES = frozenset(
    {"initialized", "running", "blocked", "cancelled", "verified"}
)


def _apply_status_fields(
    current: dict[str, Any],
    status: str,
    *,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    preserved_run_id = current.get("run_id")
    preserved_created_at = current.get("created_at")
    preserved_verified = current.get("verified", False)
    updated = dict(current)
    if extra:
        for key, value in extra.items():
            if key not in _WRITE_STATUS_RESERVED:
                updated[key] = value
    updated["status"] = status
    updated["run_id"] = preserved_run_id
    if preserved_created_at is not None:
        updated["created_at"] = preserved_created_at
    updated["verified"] = preserved_verified
    updated["updated_at"] = _utc_now()
    return updated


def _read_cancel_request(root: Path, run_id: str) -> dict[str, Any] | None:
    path = _cancel_request_path(root, run_id)
    data = _read_json(path)
    if path.exists() and data is None:
        raise LifecycleLockError(
            f"cancel request is unreadable for run_id={run_id!r}"
        )
    return data


def _require_current_lease(
    root: Path,
    run_id: str,
    lease: ExecutionLease | None,
) -> dict[str, Any]:
    if lease is None:
        raise FencingError(
            f"strict-v2 status write requires execution lease for run_id={run_id!r}"
        )
    if lease.root.resolve() != Path(root).resolve() or lease.run_id != run_id:
        raise FencingError("execution lease is bound to a different root or run")
    return lease.assert_current()


def _commit_strict_status_locked(
    root: Path,
    run_id: str,
    status: str,
    *,
    extra: dict[str, Any] | None,
    lease: ExecutionLease | None,
    cancellation_request_id: str | None = None,
    verified_authorized: bool = False,
) -> dict[str, Any]:
    """Commit strict status while ``transition.lock`` is already held."""
    if not transition_guard_held():
        raise LifecycleLockError("strict status commit requires transition.lock")
    if status not in _STRICT_STATUSES:
        raise ValueError(
            f"invalid strict-v2 status {status!r}; expected one of "
            f"{sorted(_STRICT_STATUSES)!r}"
        )
    if status == "verified" and not verified_authorized:
        raise PermissionError(
            "verified status is reserved for set_verified with in-process acceptance"
        )

    path = _status_path(root, run_id)
    current = _read_json(path)
    if current is None:
        raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
    if classify_run_schema(current) is not RunSchema.STRICT_V2:
        raise LifecycleLockError("strict status commit dispatched to non-v2 run")

    request = _read_cancel_request(root, run_id)
    current_status = str(current.get("status") or "")
    if current_status == "verified":
        if status == "verified":
            return current
        raise PermissionError("verified is absorbing; later status replacement refused")
    if current_status == "cancelled":
        if status == "cancelled":
            return current
        raise PermissionError("cancelled is absorbing; later status replacement refused")

    lease_meta: dict[str, Any] | None = None
    if status == "cancelled" and request is None:
        raise PermissionError(
            "strict cancellation requires a committed cancel.request.json"
        )
    if status == "cancelled" and request is not None:
        request_id = request.get("request_id")
        if (
            not isinstance(request_id, str)
            or not request_id
            or cancellation_request_id != request_id
        ):
            raise FencingError("cancelled finalization must match committed request_id")
        observed = request.get("observed_generation")
        current_lease = _load_lease_metadata(root, run_id)
        current_generation = _validated_generation(current_lease)
        if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
            raise LifecycleLockError("cancellation request generation is malformed")
        if lease is None:
            if observed != current_generation:
                raise FencingError(
                    "cancellation request generation no longer matches current "
                    "execution lease; the current or next owner must finalize it"
                )
        else:
            # A request may outlive the owner/generation it interrupted.  The
            # next execution owner is allowed to finalize that same durable
            # request, but only with its freshly validated fencing token.
            lease_meta = _require_current_lease(root, run_id, lease)
            if observed > current_generation:
                raise FencingError(
                    "cancellation request observes a future execution generation"
                )
    else:
        lease_meta = _require_current_lease(root, run_id, lease)
        if request is not None:
            raise PermissionError(
                "cancellation request committed first; only matching cancelled may commit"
            )

    updated = _apply_status_fields(current, status, extra=extra)
    if lease_meta is not None:
        updated["execution_owner_invocation_id"] = lease_meta["invocation_id"]
        updated["execution_generation"] = lease_meta["generation"]
    if status == "cancelled" and request is not None:
        updated["verified"] = False
        updated["cancelled_at"] = updated["updated_at"]
        updated["cancellation_request_id"] = request["request_id"]
        updated["cancellation_generation"] = request["observed_generation"]
        updated["cancellation_finalizer_generation"] = (
            lease_meta["generation"] if lease_meta is not None else current_generation
        )
    if status == "verified":
        updated["verified"] = True
        updated["verified_at"] = updated.get("verified_at") or updated["updated_at"]
    _atomic_write_json(path, updated)
    return updated


def write_status(
    root: Path,
    run_id: str,
    status: str,
    *,
    extra: dict[str, Any] | None = None,
    lease: ExecutionLease | None = None,
) -> dict[str, Any]:
    """Update status field (CLI-only path). Does not set verified=true."""
    root = Path(root)
    path = _status_path(root, run_id)
    current = _read_json(path)
    if current is None:
        raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
    schema = classify_run_schema(current)
    if schema is RunSchema.STRICT_V2:
        # Operations needing both locks arrive with execution held, then take
        # transition here.  A stale token is revalidated while guarded.
        with transition_guard(root, run_id):
            return _commit_strict_status_locked(
                root,
                run_id,
                status,
                extra=extra,
                lease=lease,
            )

    # Frozen v1 adapter: preserve historical write behavior exactly.
    updated = _apply_status_fields(current, status, extra=extra)
    if updated.get("verified") is True and not _has_acceptance_artifact(root, run_id):
        updated["verified"] = False
    _atomic_write_json(path, updated)
    return updated


def load_run(root: Path, run_id: str) -> dict[str, Any] | None:
    return _read_json(_status_path(root, run_id))


def load_active_run(root: Path) -> dict[str, Any] | None:
    """Load active pointer + corresponding status.json. None if missing."""
    root = Path(root)
    active = _read_json(_active_path(root))
    if not active:
        return None
    run_id = active.get("run_id")
    if not run_id or not isinstance(run_id, str):
        return None
    status = load_run(root, run_id)
    return status


def load_run_view(root: Path, run_id: str) -> dict[str, Any] | None:
    """Return status plus non-authoritative lock/cancel diagnostics for display."""
    root = Path(root)
    status = load_run(root, run_id)
    if status is None:
        return None
    view = dict(status)
    view["schema_classification"] = classify_run_schema(status).value
    lease = _load_lease_metadata(root, run_id)
    if lease is not None:
        view["execution_lease"] = lease
    request = _read_cancel_request(root, run_id)
    if request is not None:
        view["cancellation_request"] = request
    return view


def load_cancellation_request(root: Path, run_id: str) -> dict[str, Any] | None:
    """Read a committed strict cancellation request (diagnostic/public helper)."""
    return _read_cancel_request(Path(root), run_id)


def clear_active(root: Path, run_id: str | None = None) -> None:
    """Clear active pointer if it matches run_id (or always if run_id is None)."""
    root = Path(root)
    path = _active_path(root)
    if not path.exists():
        return
    if run_id is not None:
        active = _read_json(path)
        if not active or active.get("run_id") != run_id:
            return
    try:
        path.unlink()
    except OSError:
        # overwrite with empty marker if unlink fails
        _atomic_write_json(path, {"run_id": None, "updated_at": _utc_now()})


def _kill_run_process_group(pid: int, *, grace_s: float = 0.0) -> list[str]:
    """Best-effort kill of process group then process. Returns actions taken.

    Prefer ``os.killpg`` when the pid is a session leader (``start_new_session``).
    Ignores ESRCH / ProcessLookupError. Optional grace then SIGKILL.
    """
    actions: list[str] = []
    # Prefer process-group signal when possible (POSIX session leader)
    try:
        os.killpg(pid, signal.SIGTERM)
        actions.append("killpg:SIGTERM")
    except (ProcessLookupError, PermissionError, OSError):
        # Fall back to single-pid SIGTERM
        try:
            os.kill(pid, signal.SIGTERM)
            actions.append("kill:SIGTERM")
        except (ProcessLookupError, PermissionError, OSError):
            pass

    if grace_s and grace_s > 0:
        import time

        time.sleep(grace_s)
        # Escalate to SIGKILL on group, then pid
        try:
            os.killpg(pid, signal.SIGKILL)
            actions.append("killpg:SIGKILL")
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
                actions.append("kill:SIGKILL")
            except (ProcessLookupError, PermissionError, OSError):
                pass
    return actions


def _kill_from_pid_meta(
    meta: dict[str, Any],
    *,
    grace_s: float = 0.0,
    label: str = "leader",
) -> list[str]:
    """Kill process for a pid.json meta dict only when starttime still matches.

    Fail-closed: missing starttime or ``ps`` failure → skip kill (log warning);
    state cancel still proceeds via ``cancel_run``.
    """
    actions: list[str] = []
    try:
        pid = int(meta["pid"])
    except (KeyError, TypeError, ValueError):
        return actions
    if pid <= 0:
        return actions

    recorded = meta.get("starttime")
    recorded_s = recorded if isinstance(recorded, str) and recorded else None
    if not recorded_s:
        actions.append(f"skip:{label}:missing_starttime:{pid}")
        print(
            f"omg cancel: warning: skip kill {label} pid={pid} "
            f"(no recorded starttime; legacy plain pid or incomplete pid.json)",
            file=sys.stderr,
        )
        return actions

    alive = _pid_alive(pid)
    if alive is False:
        actions.append(f"skip:{label}:dead:{pid}")
        return actions

    current = process_starttime(pid)
    if current is None:
        actions.append(f"skip:{label}:ps_failed:{pid}")
        print(
            f"omg cancel: warning: skip kill {label} pid={pid} "
            f"(ps starttime unavailable; fail-closed)",
            file=sys.stderr,
        )
        return actions

    if current != recorded_s:
        actions.append(f"skip:{label}:pid_reuse_or_dead:{pid}")
        return actions

    # Prefer recorded pgid when present (session leader from start_new_session)
    kill_target = pid
    pgid = meta.get("pgid")
    if isinstance(pgid, int) and pgid > 0:
        kill_target = pgid

    sub = _kill_run_process_group(kill_target, grace_s=grace_s)
    if not sub and kill_target != pid:
        sub = _kill_run_process_group(pid, grace_s=grace_s)
    for a in sub:
        actions.append(f"{label}:{a}")
    if not sub:
        actions.append(f"{label}:no_signal_delivered:{pid}")
    return actions


def _cancel_targets_snapshot(root: Path, run_id: str) -> list[tuple[str, dict[str, Any]]]:
    targets: list[tuple[str, dict[str, Any]]] = []
    meta = read_pid_metadata(root, run_id)
    if meta is not None:
        targets.append(("leader", dict(meta)))
    workers_dir = _runs_dir(root) / run_id / "workers"
    if workers_dir.is_dir():
        for wpath in sorted(workers_dir.glob("*.pid.json")):
            wmeta = _read_json(wpath)
            if not wmeta:
                continue
            if "pid" in wmeta and not isinstance(wmeta["pid"], int):
                try:
                    wmeta = dict(wmeta)
                    wmeta["pid"] = int(wmeta["pid"])
                except (TypeError, ValueError):
                    continue
            targets.append((f"worker:{wpath.stem}", dict(wmeta)))
    return targets


def _signal_cancel_targets(
    targets: list[tuple[str, dict[str, Any]]], *, kill_grace_s: float
) -> list[str]:
    if transition_guard_held():
        raise LifecycleLockError("transition.lock must be released before signalling")
    actions: list[str] = []
    for label, meta in targets:
        actions.extend(
            _kill_from_pid_meta(meta, grace_s=kill_grace_s, label=label)
        )
    return actions


def _cancel_run_legacy(
    root: Path,
    run_id: str,
    *,
    kill_grace_s: float = 0.0,
) -> dict[str, Any]:
    current = load_run(root, run_id)
    if current is None:
        raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
    kill_actions = _signal_cancel_targets(
        _cancel_targets_snapshot(root, run_id), kill_grace_s=kill_grace_s
    )

    current["status"] = "cancelled"
    current["verified"] = False
    current["updated_at"] = _utc_now()
    current["cancelled_at"] = current["updated_at"]
    if kill_actions:
        current["kill_actions"] = kill_actions
    _atomic_write_json(_status_path(root, run_id), current)
    clear_active(root, run_id)
    return current


def _cancel_run_strict(
    root: Path,
    run_id: str,
    *,
    kill_grace_s: float,
    lease: ExecutionLease | None,
) -> dict[str, Any]:
    """Linearizable strict cancellation; transition guard never spans signals."""
    with transition_guard(root, run_id):
        current = load_run(root, run_id)
        if current is None:
            raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
        if classify_run_schema(current) is not RunSchema.STRICT_V2:
            raise LifecycleLockError("strict cancellation dispatched to non-v2 run")
        if current.get("status") == "verified" or current.get("verified") is True:
            result = dict(current)
            result["cancel_outcome"] = "already complete"
            return result
        if current.get("status") == "cancelled":
            result = dict(current)
            result["cancel_outcome"] = "already cancelled"
            return result

        lease_meta = _load_lease_metadata(root, run_id)
        generation = _validated_generation(lease_meta)
        request = _read_cancel_request(root, run_id)
        if request is None:
            request = {
                "writer": "omg-cli",
                "run_id": run_id,
                "request_id": str(uuid.uuid4()),
                "requested_at": _utc_now(),
                "observed_generation": generation,
            }
            if lease_meta:
                request["observed_owner_invocation_id"] = lease_meta.get(
                    "invocation_id"
                )
            _atomic_write_json(_cancel_request_path(root, run_id), request)
        targets = _cancel_targets_snapshot(root, run_id)

    # This is intentionally outside transition.lock.  Tests can assert the
    # guard is not held even when grace waiting is requested.
    kill_actions = _signal_cancel_targets(targets, kill_grace_s=kill_grace_s)

    with transition_guard(root, run_id):
        current = load_run(root, run_id)
        if current is None:
            raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
        if current.get("status") == "verified" or current.get("verified") is True:
            # A verified replacement after request commit would violate the
            # common guard contract; preserve bytes and fail loudly.
            raise LifecycleLockError(
                "verified status observed after committed cancellation request"
            )
        if current.get("status") == "cancelled":
            result = dict(current)
            result["cancel_outcome"] = "already cancelled"
            return result
        request = _read_cancel_request(root, run_id)
        assert request is not None  # committed above; corruption raises earlier
        try:
            cancelled = _commit_strict_status_locked(
                root,
                run_id,
                "cancelled",
                extra={
                    "kill_actions": kill_actions,
                    "cancel_outcome": "cancelled",
                },
                lease=lease,
                cancellation_request_id=str(request["request_id"]),
            )
        except FencingError:
            # Another execution generation won after the request.  The request
            # remains authoritative and forbids host launch/non-cancel writes;
            # its current owner or the next recovery owner must finalize it.
            result = dict(current)
            result["cancel_outcome"] = "cancellation requested"
            result["cancellation_request_id"] = request.get("request_id")
            return result

    clear_active(root, run_id)
    return cancelled


def cancel_run(
    root: Path,
    run_id: str | None = None,
    *,
    kill_grace_s: float = 0.0,
    lease: ExecutionLease | None = None,
) -> dict[str, Any]:
    """Cancel a run without deleting artifacts.

    Legacy-v1 preserves the historical mark-and-signal behavior.  Strict-v2
    commits a durable request and every status replacement under the distinct
    short transition guard, then releases it before signalling/waiting.
    """
    root = Path(root)
    if run_id is None:
        active = load_active_run(root)
        if active is None:
            raise FileNotFoundError("no active run to cancel")
        run_id = str(active["run_id"])
    current = load_run(root, run_id)
    if current is None:
        raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
    schema = classify_run_schema(current)
    if schema is RunSchema.STRICT_V2:
        return _cancel_run_strict(
            root,
            run_id,
            kill_grace_s=float(kill_grace_s),
            lease=lease,
        )
    return _cancel_run_legacy(root, run_id, kill_grace_s=float(kill_grace_s))


def _has_acceptance_artifact(root: Path, run_id: str) -> bool:
    """True only for process-trusted CLI acceptance (disk stamp + in-process token).

    Agent-forged ``{passed: true}`` — even with ``writer=omg-cli`` and a
    matching manifest sha — is rejected unless ``run_acceptance`` registered a
    process-local token in this process.
    """
    from omg_cli.acceptance import is_trusted_acceptance

    return is_trusted_acceptance(Path(root), run_id)


def set_verified(
    root: Path,
    run_id: str,
    *,
    force: bool = False,
    lease: ExecutionLease | None = None,
) -> dict[str, Any]:
    """Mark verified only when trusted CLI acceptance exists (unless force=True).

    Requires ``acceptance.result.json`` with ``writer=="omg-cli"``, ``passed``
    true, matching frozen manifest sha, **and** a process-local token from
    ``run_acceptance`` in this process. Disk-only forgeries are rejected.
    force is intentionally not exposed by the CLI router.
    """
    root = Path(root)
    current = load_run(root, run_id)
    if current is None:
        raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
    if not force and not _has_acceptance_artifact(root, run_id):
        raise PermissionError(
            "refusing to set verified=true without trusted CLI acceptance "
            f"(writer=omg-cli, passed=true, matching manifest sha, "
            f"in-process run_acceptance token) for run_id={run_id!r}"
        )
    schema = classify_run_schema(current)
    if schema is RunSchema.STRICT_V2:
        # Acceptance ran before this short guard.  The process-local token is
        # checked above and never serialized; the guarded replacement rechecks
        # request and fencing so request-first cannot be overwritten.
        with transition_guard(root, run_id):
            updated = _commit_strict_status_locked(
                root,
                run_id,
                "verified",
                extra={"verified_at": _utc_now()},
                lease=lease,
                verified_authorized=True,
            )
            return updated

    current["verified"] = True
    current["status"] = "verified"
    current["updated_at"] = _utc_now()
    current["verified_at"] = current["updated_at"]
    _atomic_write_json(_status_path(root, run_id), current)
    return current
