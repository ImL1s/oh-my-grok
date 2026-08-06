"""Split-pane tmux transport for OMX-like ``omg team`` launch.

Legacy ``plane._create_tmux_session`` uses one window per task (``new-window``).
Shorthand launch uses this module so workers share one window via ``split-window``.

Attach modes:
- ``detached`` — create a new session (outside tmux; requires TTY or ``--detach``)
- ``inside`` — create a dedicated window in the *current* session and split there;
  never kill the leader pane or the whole session on cleanup/stop
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from omg_cli.madmax import tmux_available, tmux_env_args

_TMUX_SESSION_ID = re.compile(r"^\$[0-9]{1,16}$")
_TMUX_PANE_ID = re.compile(r"^%[0-9]{1,16}$")
_TMUX_WINDOW_ID = re.compile(r"^@[0-9]{1,16}$")
_SAFE_INTENT_TOKEN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
TEAM_LAUNCH_LOCK_NAME = "team-launch.lock"


class TmuxTeamError(RuntimeError):
    """tmux transport failure for team launch."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")



def _process_start_identity(pid: int) -> str | None:
    """OS start identity for *pid* (changes when the PID is reused)."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    if raw:
        close = raw.rfind(")")
        fields = raw[close + 2 :].split()
        if close >= 0 and len(fields) > 19:
            return f"proc:{fields[19]}"
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = " ".join((result.stdout or "").split())
    return f"ps:{value}" if result.returncode == 0 and value else None


def team_launch_intents_dir(root: Path | str) -> Path:
    """Durable WAL dir for inside ``new-window`` launch intents."""
    return Path(root).resolve() / ".omg" / "state" / "team-launch-intents"


def team_launch_lock_path(root: Path | str) -> Path:
    """Project-scoped lock serializing start_team sweep→receipt."""
    return Path(root).resolve() / ".omg" / "state" / TEAM_LAUNCH_LOCK_NAME


@contextmanager
def acquire_team_launch_lock(root: Path | str) -> Iterator[Path]:
    """Exclusive project lock for launch-intent sweep + start transaction.

    Non-blocking: concurrent ``start_team`` refuses rather than interleaving
    sweep with another in-flight new-window→receipt window.
    """
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover
        raise TmuxTeamError(
            "team launch lock requires POSIX fcntl.flock"
        ) from exc

    from omg_cli.contracts.path_keys import (
        ContractPathError,
        open_managed_dir_fd,
    )

    path = team_launch_lock_path(root)
    try:
        parent_fd = open_managed_dir_fd(path.parent)
    except ContractPathError as exc:
        raise TmuxTeamError(f"team launch lock parent open refused: {exc}") from exc
    fd = -1
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(TEAM_LAUNCH_LOCK_NAME, flags, 0o644, dir_fd=parent_fd)
        except OSError as exc:
            raise TmuxTeamError(
                f"team launch lock open refused (symlink or non-regular?): {exc}"
            ) from exc
        try:
            import stat as stat_mod

            st = os.fstat(fd)
            if not stat_mod.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise TmuxTeamError(
                    f"team launch lock must be a unique regular file "
                    f"(nlink={st.st_nlink})"
                )
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            st = os.fstat(fd)
            if not stat_mod.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise TmuxTeamError(
                    f"team launch lock inode must be a unique regular file "
                    f"(nlink={st.st_nlink})"
                )
        except BlockingIOError as exc:
            holder = ""
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                holder = os.read(fd, 64).decode("utf-8", errors="replace").strip()
            except OSError:
                pass
            raise TmuxTeamError(
                "team launch lock held"
                + (f" (pid={holder})" if holder else "")
                + f"; refuse concurrent start ({path})"
            ) from exc
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
            try:
                os.fsync(fd)
            except OSError:
                pass
            yield path
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.close(parent_fd)
        except OSError:
            pass


def team_launch_intent_path(root: Path | str, run_id: str, nonce: str) -> Path:
    if not _SAFE_INTENT_TOKEN.fullmatch(run_id):
        raise TmuxTeamError(f"invalid run_id for launch intent: {run_id!r}")
    if not _SAFE_INTENT_TOKEN.fullmatch(nonce):
        raise TmuxTeamError(f"invalid nonce for launch intent: {nonce!r}")
    return team_launch_intents_dir(root) / f"{run_id}-{nonce}.json"


def write_team_launch_intent(
    root: Path | str,
    *,
    run_id: str,
    session_id: str,
    window_name: str,
    nonce: str,
) -> Path:
    """Atomically persist launch intent *before* ``new-window`` side effects."""
    from omg_cli.contracts.path_keys import (
        DATA_FILE_MODE,
        ContractPathError,
        atomic_write_bytes,
        ensure_managed_dir,
    )

    owner_pid = os.getpid()
    owner_start = _process_start_identity(owner_pid)
    if not owner_start:
        raise TmuxTeamError(
            "launch intent write refused: owner pid_start identity unavailable"
        )
    path = team_launch_intent_path(root, run_id, nonce)
    payload = {
        "run_id": run_id,
        "session_id": session_id,
        "window_name": window_name,
        "nonce": nonce,
        "owner_pid": owner_pid,
        "owner_pid_start": owner_start,
        "created_at": _utc_now_iso(),
    }
    body = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        ensure_managed_dir(path.parent)
        atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=True)
    except ContractPathError as exc:
        raise TmuxTeamError(f"launch intent write refused: {exc}") from exc
    return path


def clear_team_launch_intent(path: Path | str | None) -> None:
    """Durably remove a launch intent after receipt publish or proven cleanup.

    Unlink is fail-closed: OSError (other than already-absent) propagates so
    callers cannot leave a stale WAL that later sweeps a receipt-bound worker.
    Parent directory is fsync'd after unlink when the file existed.
    """
    if path is None:
        return
    intent = Path(path)
    from omg_cli.contracts.path_keys import (
        ContractPathError,
        open_existing_managed_dir_fd,
    )

    try:
        parent_fd = open_existing_managed_dir_fd(intent.parent)
    except (OSError, ContractPathError, FileNotFoundError, ValueError) as exc:
        # Parent missing with no intent file is already-clean.
        if not intent.exists():
            return
        raise TmuxTeamError(
            f"launch intent clear refused (parent): {intent}: {exc}"
        ) from exc
    try:
        try:
            os.unlink(intent.name, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise TmuxTeamError(
                f"launch intent clear failed: {intent}: {exc}"
            ) from exc
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise TmuxTeamError(
                f"launch intent clear fsync failed: {intent.parent}: {exc}"
            ) from exc
    finally:
        os.close(parent_fd)


def _intent_owner_blocks_sweep(raw: Mapping[str, Any]) -> str | None:
    """Return a refuse reason when the intent owner is still the live launcher.

    Missing owner fields (legacy intents) do not block — those are treated as
    crash orphans eligible for kill-by-name. Live owner with matching start-id
    must never be killed by another start_team sweep.
    """
    pid = raw.get("owner_pid")
    start = raw.get("owner_pid_start")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if not isinstance(start, str) or not start:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return "in-flight launch owner pid probe permission denied"
    except OSError as exc:
        return f"in-flight launch owner pid probe failed: {exc}"
    live_start = _process_start_identity(pid)
    if live_start is None:
        return "in-flight launch owner start-id probe unknown"
    if live_start == start:
        return "in-flight launch (owner alive)"
    return None


def _intent_receipt_matches(root: Path, intent: Mapping[str, Any]) -> bool:
    """True only when durable receipt binds the *exact* intent identity.

    Schema-v1 (#106) launch receipts omit ``intent_nonce`` / ``window_name`` and
    must never be adopted for a new launch intent — they cannot prove intent
    identity. Only schema-v2 receipts that pass the same authority checks as
    :func:`omg_cli.team.plane._load_team_launch_receipt` (exact key set,
    generation, tasks continuity, canonical body hash / meta
    ``launch_receipt_sha256``) with matching binding fields qualify.
    """
    from omg_cli.team.plane import (
        LAUNCH_RECEIPT_SCHEMA_VERSION,
        TeamError,
        _load_team_launch_receipt,
        load_team_meta,
    )

    intent_run = intent.get("run_id")
    intent_session = intent.get("session_id")
    intent_name = intent.get("window_name")
    intent_nonce = intent.get("nonce")
    if not isinstance(intent_run, str) or not _SAFE_INTENT_TOKEN.fullmatch(intent_run):
        return False
    if not isinstance(intent_session, str) or not isinstance(intent_name, str):
        return False
    if not isinstance(intent_nonce, str) or not intent_nonce:
        return False
    try:
        meta = load_team_meta(root, intent_run)
        receipt = _load_team_launch_receipt(root, intent_run, meta)
    except TeamError:
        return False
    # Fail-closed: legacy v1 (and any non-v2) cannot prove intent identity.
    if receipt.get("schema_version") != LAUNCH_RECEIPT_SCHEMA_VERSION:
        return False
    if receipt.get("session_id") != intent_session:
        return False
    receipt_intent = receipt.get("intent_nonce")
    receipt_window = receipt.get("window_name")
    if not isinstance(receipt_intent, str) or not receipt_intent:
        return False
    if not isinstance(receipt_window, str) or not receipt_window:
        return False
    if receipt_intent != intent_nonce:
        return False
    if receipt_window != intent_name:
        return False
    return True


def sweep_stale_team_launch_intents(
    root: Path | str,
    *,
    run_id: str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Kill-by-name + absence proof for leftover launch intents (fail-closed).

    Called at ``start_team`` entry so a prior crash after ``new-window`` cannot
    leave an unrecepted worker across CLI restarts. Clears intent only when
    absence is proven, or when a durable launch receipt already binds the
    *exact* intent identity (adopt — never kill a receipt-bound worker).

    Each result has ``ok: bool``. Callers must treat any ``ok=False`` (or an
    OSError from this function) as a **launch gate** — refuse ``new-window``.
    When ``run_id`` is None, every pending intent under the project is scanned.
    """
    root_path = Path(root).resolve()
    intents_dir = team_launch_intents_dir(root_path)
    if not intents_dir.is_dir():
        return []
    results: list[dict[str, Any]] = []
    try:
        entries = sorted(intents_dir.iterdir())
    except OSError as exc:
        raise TmuxTeamError(
            f"launch intent sweep failed listing {intents_dir}: {exc}"
        ) from exc
    for entry in entries:
        if not entry.is_file() or entry.suffix != ".json":
            continue
        if run_id is not None and not entry.name.startswith(f"{run_id}-"):
            continue
        try:
            raw = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            results.append(
                {"path": str(entry), "ok": False, "error": "unreadable intent"}
            )
            continue
        if not isinstance(raw, dict):
            results.append(
                {"path": str(entry), "ok": False, "error": "invalid intent"}
            )
            continue
        intent_run = raw.get("run_id")
        intent_session = raw.get("session_id")
        intent_name = raw.get("window_name")
        if session_id is not None and intent_session != session_id:
            continue
        if not isinstance(intent_session, str) or not isinstance(intent_name, str):
            results.append(
                {"path": str(entry), "ok": False, "error": "incomplete intent"}
            )
            continue
        owner_block = _intent_owner_blocks_sweep(raw)
        if owner_block is not None:
            results.append(
                {
                    "path": str(entry),
                    "ok": False,
                    "session_id": intent_session,
                    "window_name": intent_name,
                    "error": owner_block,
                }
            )
            continue
        if _intent_receipt_matches(root_path, raw):
            try:
                clear_team_launch_intent(entry)
            except TmuxTeamError as exc:
                results.append(
                    {
                        "path": str(entry),
                        "ok": False,
                        "session_id": intent_session,
                        "window_name": intent_name,
                        "error": f"adopt clear failed: {exc}",
                    }
                )
                continue
            results.append(
                {
                    "path": str(entry),
                    "ok": True,
                    "adopted": True,
                    "session_id": intent_session,
                    "window_name": intent_name,
                }
            )
            continue
        if isinstance(intent_run, str) and _SAFE_INTENT_TOKEN.fullmatch(intent_run):
            receipt_path = (
                root_path
                / ".omg"
                / "state"
                / "runs"
                / intent_run
                / "team"
                / "launch-receipt.json"
            )
            try:
                receipt_present = receipt_path.is_file() and not receipt_path.is_symlink()
            except OSError:
                receipt_present = False
            if receipt_present:
                results.append(
                    {
                        "path": str(entry),
                        "ok": False,
                        "session_id": intent_session,
                        "window_name": intent_name,
                        "error": "receipt present but intent identity unbound",
                    }
                )
                continue
        cleanup = _kill_inside_windows_by_name(
            session_id=intent_session, window_name=intent_name
        )
        if cleanup is None:
            try:
                clear_team_launch_intent(entry)
            except TmuxTeamError as exc:
                results.append(
                    {
                        "path": str(entry),
                        "ok": False,
                        "session_id": intent_session,
                        "window_name": intent_name,
                        "error": f"clear after absence failed: {exc}",
                    }
                )
                continue
            results.append(
                {
                    "path": str(entry),
                    "ok": True,
                    "session_id": intent_session,
                    "window_name": intent_name,
                }
            )
        else:
            results.append(
                {
                    "path": str(entry),
                    "ok": False,
                    "session_id": intent_session,
                    "window_name": intent_name,
                    "error": cleanup,
                }
            )
    return results


def require_clean_team_launch_intents(root: Path | str) -> list[dict[str, Any]]:
    """Sweep all project launch intents; raise if any cannot be proven clean.

    Launch gate for ``start_team``: must run *before* ``create_run`` / active-run
    refusal so crash recovery reaches orphans even when an active run blocks
    a new start.
    """
    results = sweep_stale_team_launch_intents(root, run_id=None)
    failures = [r for r in results if not r.get("ok")]
    if failures:
        detail = "; ".join(
            f"{r.get('path')}: {r.get('error') or 'unproven'}" for r in failures[:5]
        )
        raise TmuxTeamError(
            "stale team launch intents not proven cleaned — refusing start: "
            + detail
        )
    return results



def _tmux_run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _cleanup_session(handle: tuple[str, str]) -> str | None:
    name, session_id = handle
    for target in (session_id, name):
        killed = _tmux_run(["kill-session", "-t", target])
        if killed.returncode == 0:
            return None
    return f"failed to cleanup tmux session {name!r}"


def _kill_panes(pane_ids: Sequence[str]) -> str | None:
    """Best-effort kill of worker panes only (never a whole session)."""
    errors: list[str] = []
    for pane_id in pane_ids:
        if _TMUX_PANE_ID.fullmatch(pane_id) is None:
            errors.append(f"refused kill-pane for non-pane id {pane_id!r}")
            continue
        killed = _tmux_run(["kill-pane", "-t", pane_id])
        if killed.returncode not in (0, 1):
            err = (killed.stderr or killed.stdout or "").strip()
            errors.append(f"kill-pane {pane_id}: exit {killed.returncode} {err}")
    return "; ".join(errors) if errors else None


def pane_alive(pane_id: str) -> bool | None:
    """True/False when tmux available; None when tmux unavailable.

    Only proves the pane object exists and is not dead — not that it still
    hosts the original Team worker. Prefer plane status identity checks.
    """
    if not tmux_available():
        return None
    if not isinstance(pane_id, str) or _TMUX_PANE_ID.fullmatch(pane_id) is None:
        return False
    probe = _tmux_run(
        ["display-message", "-p", "-t", pane_id, "#{pane_id}\t#{pane_dead}"]
    )
    if probe.returncode != 0:
        return False
    return (probe.stdout or "").strip() == f"{pane_id}\t0"


def probe_worker_pane_identity(pane_id: str) -> dict[str, Any] | None:
    """Return ``{pane_id, dead, session_id, pane_pid}`` or None on probe failure.

    Fail-closed: missing tmux / spawn OSError / malformed fields → None
    (never raise into status). ``pane_dead`` must be exactly ``0``/``1`` and
    ``session_id`` must match ``$N``; otherwise treat as UNKNOWN.
    """
    if not tmux_available():
        return None
    if not isinstance(pane_id, str) or _TMUX_PANE_ID.fullmatch(pane_id) is None:
        return None
    try:
        probe = _tmux_run(
            [
                "display-message",
                "-p",
                "-t",
                pane_id,
                "#{pane_id}\t#{pane_dead}\t#{session_id}\t#{pane_pid}",
            ]
        )
    except OSError:
        return None
    if probe.returncode != 0:
        return None
    parts = (probe.stdout or "").strip().split("\t")
    if len(parts) != 4 or parts[0] != pane_id:
        return None
    if parts[1] not in ("0", "1"):
        return None
    if _TMUX_SESSION_ID.fullmatch(parts[2]) is None:
        return None
    try:
        pane_pid = int(parts[3])
    except ValueError:
        return None
    if pane_pid <= 0:
        return None
    return {
        "pane_id": parts[0],
        "dead": parts[1] == "1",
        "session_id": parts[2],
        "pane_pid": pane_pid,
    }


def respawn_worker_pane(
    *,
    target: str,
    worktree: str,
    pane_command: str,
    env_pairs: Sequence[tuple[str, str]] | None = None,
) -> str:
    """Split a replacement worker pane into ``target``; return new ``pane_id``.

    Used by resume when a worker pane died but the team session/window remains.
    Never kills the leader pane or the whole session.
    """
    if not tmux_available():
        raise TmuxTeamError("tmux is required to respawn a team worker pane")
    if not target or not str(target).strip():
        raise TmuxTeamError("respawn target (session/window id) required")
    if not worktree or not pane_command:
        raise TmuxTeamError("respawn requires worktree and pane_command")
    task_env = tmux_env_args(list(env_pairs or []))
    split = _tmux_run(
        [
            "split-window",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            str(target),
            "-c",
            str(worktree),
            *task_env,
            str(pane_command),
        ]
    )
    if split.returncode != 0:
        err = (split.stderr or split.stdout or "").strip()
        raise TmuxTeamError(f"failed to respawn worker pane: {err}")
    pane_id = (split.stdout or "").strip()
    if _TMUX_PANE_ID.fullmatch(pane_id) is None:
        raise TmuxTeamError("respawn split-window did not return pane id")
    return pane_id


def _kill_window(window_id: str) -> str | None:
    if _TMUX_WINDOW_ID.fullmatch(window_id) is None:
        return f"refused kill-window for non-window id {window_id!r}"
    killed = _tmux_run(["kill-window", "-t", window_id])
    if killed.returncode not in (0, 1):
        err = (killed.stderr or killed.stdout or "").strip()
        return f"kill-window {window_id}: exit {killed.returncode} {err}"
    return None


def interactive_tty(
    isatty: Callable[[], bool] | None = None,
) -> bool:
    if isatty is not None:
        return bool(isatty())
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )


def resolve_attach_mode(
    *,
    detach: bool = False,
    env: Mapping[str, str] | None = None,
    isatty: Callable[[], bool] | None = None,
) -> str:
    """Return ``inside`` or ``detached``; fail closed for bare non-TTY live launch."""
    environ = env if env is not None else os.environ
    if str(environ.get("TMUX") or "").strip():
        return "inside"
    if detach or interactive_tty(isatty):
        return "detached"
    raise TmuxTeamError(
        "non-interactive live team launch refused without --detach.\n"
        "  Re-run from a TTY, inside tmux, or pass --detach.\n"
        "  Later attach: tmux attach -t <session>\n"
        "  Do not silently fall back to dry-run or ULW."
    )


def _parse_session_handle(stdout: str, *, expected_name: str | None = None) -> tuple[str, str]:
    parts = (stdout or "").strip().split("\t")
    if len(parts) != 2 or _TMUX_SESSION_ID.fullmatch(parts[1]) is None:
        raise TmuxTeamError("tmux did not return an exact session handle")
    if expected_name is not None and parts[0] != expected_name:
        raise TmuxTeamError(
            f"tmux session name mismatch: expected {expected_name!r} got {parts[0]!r}"
        )
    return parts[0], parts[1]


def resolve_invoking_pane(
    *,
    pane: str | None = None,
    env: Mapping[str, str] | None = None,
    require_exact: bool = True,
) -> str:
    """Resolve the exact leader pane that invoked Team launch.

    Inside-tmux launches require an explicit ``pane`` or exact ``TMUX_PANE``
    (``%N``). Untargeted ``display-message`` is never used — that would bind
    whichever client is current and can retarget mid-launch (#97 / Pro P1).
    """
    if pane is not None:
        candidate = str(pane).strip()
        if _TMUX_PANE_ID.fullmatch(candidate) is None:
            raise TmuxTeamError(f"invalid invoking pane id {pane!r}")
        return candidate
    environ = env if env is not None else os.environ
    from_env = str(environ.get("TMUX_PANE") or "").strip()
    if from_env:
        if _TMUX_PANE_ID.fullmatch(from_env) is None:
            raise TmuxTeamError(f"invalid TMUX_PANE {from_env!r}")
        return from_env
    if require_exact:
        raise TmuxTeamError(
            "inside-tmux Team launch requires exact TMUX_PANE (%N) "
            "or an explicit invoking pane id"
        )
    raise TmuxTeamError(
        "failed to resolve invoking leader pane "
        "(set TMUX_PANE or pass an exact pane id)"
    )


_INVOCATION_FMT = (
    "#{session_name}\t#{session_id}\t#{window_id}\t#{pane_id}\t#{pane_pid}"
)


def snapshot_invoking_identity(pane_id: str) -> dict[str, str | int]:
    """Capture immutable invocation coordinates for the leader pane."""
    if _TMUX_PANE_ID.fullmatch(pane_id) is None:
        raise TmuxTeamError(f"invalid pane id for identity snapshot {pane_id!r}")
    probe = _tmux_run(
        ["display-message", "-p", "-t", pane_id, _INVOCATION_FMT]
    )
    if probe.returncode != 0:
        err = (probe.stderr or probe.stdout or "").strip()
        raise TmuxTeamError(f"failed to snapshot invoking pane {pane_id}: {err}")
    parts = (probe.stdout or "").strip().split("\t")
    if len(parts) != 5:
        raise TmuxTeamError("invoking pane identity probe returned malformed output")
    session_name, session_id, window_id, got_pane, pid_s = parts
    if (
        not session_name
        or _TMUX_SESSION_ID.fullmatch(session_id) is None
        or _TMUX_WINDOW_ID.fullmatch(window_id) is None
        or got_pane != pane_id
        or _TMUX_PANE_ID.fullmatch(got_pane) is None
    ):
        raise TmuxTeamError("invoking pane identity probe failed validation")
    try:
        pane_pid = int(pid_s)
    except ValueError as exc:
        raise TmuxTeamError(f"invalid pane pid in identity snapshot: {pid_s!r}") from exc
    if pane_pid <= 0:
        raise TmuxTeamError(f"non-positive pane pid in identity snapshot: {pane_pid}")
    return {
        "session_name": session_name,
        "session_id": session_id,
        "window_id": window_id,
        "pane_id": got_pane,
        "pane_pid": pane_pid,
    }


def assert_invoking_identity(snapshot: Mapping[str, Any]) -> dict[str, str | int]:
    """Re-query the invoking pane and require an exact match to *snapshot*."""
    pane_id = str(snapshot.get("pane_id") or "")
    live = snapshot_invoking_identity(pane_id)
    for key in ("session_name", "session_id", "window_id", "pane_id", "pane_pid"):
        if live.get(key) != snapshot.get(key):
            raise TmuxTeamError(
                f"invoking pane identity drifted before worker create "
                f"({key}: expected {snapshot.get(key)!r} got {live.get(key)!r})"
            )
    return live


def _session_handle_for_pane(pane_id: str) -> tuple[str, str]:
    snap = snapshot_invoking_identity(pane_id)
    return str(snap["session_name"]), str(snap["session_id"])


def _current_session_handle() -> tuple[str, str]:
    """Legacy untargeted probe — prefer :func:`_session_handle_for_pane`."""
    probe = _tmux_run(
        ["display-message", "-p", "#{session_name}\t#{session_id}"]
    )
    if probe.returncode != 0:
        err = (probe.stderr or probe.stdout or "").strip()
        raise TmuxTeamError(f"failed to read current tmux session: {err}")
    return _parse_session_handle(probe.stdout)


def _current_leader_pane() -> str:
    """Deprecated alias — use :func:`resolve_invoking_pane`."""
    return resolve_invoking_pane()


def _restore_leader_focus(leader_pane: str) -> None:
    """Reselect the exact leader pane after focus-detached worker creation."""
    if _TMUX_PANE_ID.fullmatch(leader_pane) is None:
        raise TmuxTeamError(f"invalid leader pane for focus restore {leader_pane!r}")
    selected = _tmux_run(["select-pane", "-t", leader_pane])
    if selected.returncode != 0:
        err = (selected.stderr or selected.stdout or "").strip()
        raise TmuxTeamError(f"failed to restore leader focus on {leader_pane}: {err}")


def _bind_pane_pid(pane_id: str) -> int:
    probe = _tmux_run(["display-message", "-p", "-t", pane_id, "#{pane_pid}"])
    if probe.returncode != 0:
        raise TmuxTeamError(f"failed to read pane pid for {pane_id}")
    try:
        pid = int((probe.stdout or "").strip())
    except ValueError as exc:
        raise TmuxTeamError(f"invalid pane pid for {pane_id}") from exc
    if pid <= 0:
        raise TmuxTeamError(f"non-positive pane pid for {pane_id}")
    return pid


def _launch_first_detached(
    *,
    session: str,
    task: dict[str, Any],
    env_pairs: list[tuple[str, str]],
) -> tuple[tuple[str, str], str]:
    first_env = tmux_env_args(list(task.get("_env_pairs") or env_pairs))
    create = _tmux_run(
        [
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{session_name}\t#{session_id}\t#{pane_id}",
            "-s",
            session,
            "-n",
            "team",
            "-c",
            str(task["worktree"]),
            *first_env,
            str(task["pane_command"]),
        ]
    )
    if create.returncode != 0:
        err = (create.stderr or create.stdout or "").strip()
        raise TmuxTeamError(
            f"failed to create tmux session {session!r} "
            f"(exit {create.returncode}): {err}"
        )
    parts = (create.stdout or "").strip().split("\t")
    if (
        len(parts) != 3
        or parts[0] != session
        or _TMUX_SESSION_ID.fullmatch(parts[1]) is None
        or _TMUX_PANE_ID.fullmatch(parts[2]) is None
    ):
        cleanup = _cleanup_session((session, session))
        message = "tmux create did not return an exact session/pane handle"
        if cleanup:
            message += f"; {cleanup}"
        raise TmuxTeamError(message)
    return (parts[0], parts[1]), parts[2]


def _discover_inside_windows_by_name(
    *,
    session_id: str,
    window_name: str,
) -> tuple[str, list[str], str | None]:
    """Discover windows in *session_id* by exact name for orphan recovery.

    Returns ``(status, window_ids, detail)`` where *status* is one of:
    - ``found`` — exactly one matching window id
    - ``absent`` — successful list with zero matches
    - ``ambiguous`` — successful list with multiple matches
    - ``unknown`` — list-windows non-zero / OSError / malformed (not absence)
    """
    if _TMUX_SESSION_ID.fullmatch(session_id) is None:
        return (
            "unknown",
            [],
            f"inside window discovery requires session id, got {session_id!r}",
        )
    if not window_name or not isinstance(window_name, str):
        return (
            "unknown",
            [],
            "inside window discovery requires a unique window name",
        )
    try:
        listed = _tmux_run(
            [
                "list-windows",
                "-t",
                session_id,
                "-F",
                "#{window_id}\t#{window_name}\t#{session_id}",
            ]
        )
    except OSError as exc:
        return "unknown", [], f"list-windows OSError: {exc}"
    if listed.returncode != 0:
        err = (listed.stderr or listed.stdout or "").strip()
        return (
            "unknown",
            [],
            f"list-windows exit {listed.returncode}"
            + (f" {err}" if err else ""),
        )
    matches: list[str] = []
    valid_rows = 0
    for line in (listed.stdout or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        parts = raw.split("\t")
        if len(parts) != 3:
            continue
        wid, name, sid = parts
        if _TMUX_WINDOW_ID.fullmatch(wid) is None:
            continue
        valid_rows += 1
        if name == window_name and sid == session_id:
            matches.append(wid)
    if matches:
        if len(matches) > 1:
            return "ambiguous", matches, None
        return "found", matches, None
    raw_content = (listed.stdout or "").strip()
    if raw_content and valid_rows == 0:
        return "unknown", [], "list-windows returned malformed output"
    return "absent", [], None


def _discover_inside_window_by_name(
    *,
    session_id: str,
    window_name: str,
) -> str | None:
    """Find a unique window in *session_id* by exact name (orphan recovery).

    Returns the window id, ``None`` if absent, or raises on ambiguity/unknown.
    """
    status, matches, detail = _discover_inside_windows_by_name(
        session_id=session_id, window_name=window_name
    )
    if status == "found":
        return matches[0]
    if status == "absent":
        return None
    if status == "ambiguous":
        raise TmuxTeamError(
            f"ambiguous team window name {window_name!r} in session {session_id}"
        )
    raise TmuxTeamError(
        detail
        or f"inside window discovery unknown for {window_name!r} in {session_id}"
    )


def _kill_inside_windows_by_name(
    *,
    session_id: str,
    window_name: str,
) -> str | None:
    """Kill windows matching *window_name* in *session_id*; require absence proof.

    After kill attempts, re-runs a successful ``list-windows`` and requires the
    transaction name to be **absent**. Bare ``kill-window`` rc 0/1 is never
    treated as success by itself. Returns ``None`` only when absence is proven;
    otherwise returns an error detail (unknown list / still present / OSError).
    """
    status, matches, detail = _discover_inside_windows_by_name(
        session_id=session_id, window_name=window_name
    )
    errors: list[str] = []
    killed_ids: list[str] = []
    if status in ("found", "ambiguous") and matches:
        for wid in matches:
            err = _kill_window(wid)
            if err:
                errors.append(err)
            else:
                killed_ids.append(wid)
    # Always also target session:name so a discovery-unknown path still
    # attempts kill-window against the unique launch name we stamped.
    name_target = f"{session_id}:{window_name}"
    try:
        by_name = _tmux_run(["kill-window", "-t", name_target])
    except OSError as exc:
        errors.append(f"kill-window -t {name_target!r} OSError: {exc}")
        by_name = None
    if by_name is not None and by_name.returncode not in (0, 1):
        err = (by_name.stderr or by_name.stdout or "").strip()
        errors.append(
            f"kill-window -t {name_target!r}: exit {by_name.returncode}"
            + (f" {err}" if err else "")
        )
    if status == "unknown" and detail:
        errors.append(f"discovery {detail}")
    if status == "ambiguous":
        errors.append(
            f"ambiguous name {window_name!r}; killed ids={killed_ids or matches}"
        )

    # Absence proof — required; kill rc alone is insufficient.
    proof_status, proof_matches, proof_detail = _discover_inside_windows_by_name(
        session_id=session_id, window_name=window_name
    )
    if proof_status == "absent":
        return None
    if proof_status in ("found", "ambiguous"):
        return (
            f"window {window_name!r} still present after kill "
            f"(ids={proof_matches}); "
            + ("; ".join(errors) if errors else "absence unproven")
        )
    return (
        f"absence unproven for {window_name!r}: "
        + (proof_detail or proof_status)
        + (("; " + "; ".join(errors)) if errors else "")
    )


def _launch_first_inside(
    *,
    task: dict[str, Any],
    env_pairs: list[tuple[str, str]],
    window_name: str,
    target_window: str,
    expected_session_id: str,
    intent_path: Path | None = None,
) -> tuple[str, str]:
    """Create a new window beside *target_window* (``@N``); return (window_id, pane_id).

    Uses ``-d`` so the client stays on the invoking leader pane. Target must be
    a window id — tmux rejects ``new-window -a -t %pane`` (CMD_FIND_WINDOW).

    When ``new-window`` returns rc=0 but stdout is empty/malformed, always
    attempt discover-by-name + kill-window with absence proof before raising
    so the caller never leaves an unrecepted orphan silently OK.
    """
    if _TMUX_WINDOW_ID.fullmatch(target_window) is None:
        raise TmuxTeamError(
            f"new-window target must be a window id (@N), got {target_window!r}"
        )
    if _TMUX_SESSION_ID.fullmatch(expected_session_id) is None:
        raise TmuxTeamError(
            f"new-window requires expected session id, got {expected_session_id!r}"
        )
    first_env = tmux_env_args(list(task.get("_env_pairs") or env_pairs))
    try:
        create = _tmux_run(
            [
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{window_id}\t#{pane_id}",
                "-a",
                "-t",
                target_window,
                "-n",
                window_name,
                "-c",
                str(task["worktree"]),
                *first_env,
                str(task["pane_command"]),
            ]
        )
    except OSError as exc:
        # Creation itself failed — still name cleanup in case the window was
        # created before the client lost the reply channel.
        cleanup = _kill_inside_windows_by_name(
            session_id=expected_session_id, window_name=window_name
        )
        if cleanup is None:
            clear_team_launch_intent(intent_path)
        message = f"tmux new-window OSError: {exc}"
        if cleanup:
            message = f"{message}; orphan cleanup: {cleanup}"
        raise TmuxTeamError(message) from exc
    if create.returncode != 0:
        err = (create.stderr or create.stdout or "").strip()
        raise TmuxTeamError(
            f"failed to create team window in current session: {err}"
        )
    parts = (create.stdout or "").strip().split("\t")
    if (
        len(parts) == 2
        and _TMUX_WINDOW_ID.fullmatch(parts[0]) is not None
        and _TMUX_PANE_ID.fullmatch(parts[1]) is not None
    ):
        return parts[0], parts[1]

    # Side effect succeeded; result publication failed — always kill by name.
    cleanup = _kill_inside_windows_by_name(
        session_id=expected_session_id, window_name=window_name
    )
    if cleanup is None:
        clear_team_launch_intent(intent_path)
        message = (
            f"tmux new-window did not return window/pane ids; "
            f"killed orphan window(s) named {window_name!r} "
            f"in session {expected_session_id} (absence proven)"
        )
    else:
        message = (
            f"tmux new-window did not return window/pane ids; "
            f"orphan cleanup: {cleanup}"
        )
    raise TmuxTeamError(message)


def _split_remaining(
    *,
    target: str,
    tasks: Sequence[dict[str, Any]],
    env_pairs: list[tuple[str, str]],
) -> list[str]:
    """Split remaining tasks into ``target`` (session id or window id).

    Uses ``-d`` so splits do not steal client focus from the leader pane.
    """
    created: list[str] = []
    for task in tasks:
        task_env = tmux_env_args(list(task.get("_env_pairs") or env_pairs))
        split = _tmux_run(
            [
                "split-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                target,
                "-c",
                str(task["worktree"]),
                *task_env,
                str(task["pane_command"]),
            ]
        )
        if split.returncode != 0:
            err = (split.stderr or split.stdout or "").strip()
            raise TmuxTeamError(
                f"failed to split pane for task {task['task_id']!r}: {err}"
            )
        pane_id = (split.stdout or "").strip()
        if _TMUX_PANE_ID.fullmatch(pane_id) is None:
            raise TmuxTeamError(
                f"split-window did not return pane id for {task['task_id']!r}"
            )
        created.append(pane_id)
    return created


def create_split_team_session(
    *,
    session: str,
    tasks: list[dict[str, Any]],
    env_pairs: list[tuple[str, str]],
    attach_mode: str | None = None,
    detach: bool = False,
    env: Mapping[str, str] | None = None,
    isatty: Callable[[], bool] | None = None,
    invoking_pane: str | None = None,
    root: Path | str | None = None,
    run_id: str | None = None,
) -> tuple[str, str]:
    """Create worker panes in one window; return ``(session_name, session_id)``.

    Mutates each task with ``pane_id``. Sets ``_tmux_launch`` on ``tasks[0]``
    (shared dict key consumed by plane) describing attach policy:
    ``attach_mode``, ``session_owned``, ``leader_pane_id``, ``window_id``,
    ``attach_hint``.

    When *root* and *run_id* are provided (inside mode), a durable launch
    intent is written before ``new-window`` and cleared only after successful
    create (caller clears after receipt) or cleanup with absence proof.
    """
    if not tmux_available():
        raise TmuxTeamError(
            "tmux is required for omg team launch (non-dry-run).\n"
            "  Install: brew install tmux\n"
            "  Or use --dry-run to write state without launching."
        )
    if not tasks:
        raise TmuxTeamError("no tasks for tmux session")

    mode = attach_mode or resolve_attach_mode(detach=detach, env=env, isatty=isatty)
    if mode not in ("inside", "detached"):
        raise TmuxTeamError(f"unsupported attach_mode {mode!r}")

    if mode == "inside":
        return _create_inside(
            session=session,
            tasks=tasks,
            env_pairs=env_pairs,
            env=env,
            invoking_pane=invoking_pane,
            root=root,
            run_id=run_id,
        )
    return _create_detached(session=session, tasks=tasks, env_pairs=env_pairs)


def _stamp_launch_meta(
    tasks: list[dict[str, Any]],
    *,
    attach_mode: str,
    session_owned: bool,
    leader_pane_id: str | None,
    window_id: str | None,
    attach_hint: str | None,
) -> None:
    tasks[0]["_tmux_launch"] = {
        "attach_mode": attach_mode,
        "session_owned": session_owned,
        "leader_pane_id": leader_pane_id,
        "window_id": window_id,
        "attach_hint": attach_hint,
    }


def _create_detached(
    *,
    session: str,
    tasks: list[dict[str, Any]],
    env_pairs: list[tuple[str, str]],
) -> tuple[str, str]:
    handle, first_pane = _launch_first_detached(
        session=session, task=tasks[0], env_pairs=env_pairs
    )
    created_panes = [first_pane]
    try:
        created_panes.extend(
            _split_remaining(target=handle[1], tasks=tasks[1:], env_pairs=env_pairs)
        )
        if len(created_panes) != len(tasks):
            raise TmuxTeamError("pane count mismatch after detached split")
        for task, pane_id in zip(tasks, created_panes, strict=True):
            task["pane_id"] = pane_id
        layout = _tmux_run(["select-layout", "-t", handle[1], "tiled"])
        if layout.returncode != 0:
            raise TmuxTeamError("failed to apply tiled layout")
        option = _tmux_run(["set-option", "-t", handle[1], "mouse", "on"])
        if option.returncode != 0:
            raise TmuxTeamError("failed to configure created tmux session")
        _stamp_launch_meta(
            tasks,
            attach_mode="detached",
            session_owned=True,
            leader_pane_id=None,
            window_id=None,
            attach_hint=f"tmux attach -t {handle[0]}",
        )
    except (TmuxTeamError, OSError) as exc:
        cleanup = _cleanup_session(handle)
        if cleanup:
            raise TmuxTeamError(f"{exc}; {cleanup}") from exc
        raise
    return handle


def _create_inside(
    *,
    session: str,
    tasks: list[dict[str, Any]],
    env_pairs: list[tuple[str, str]],
    env: Mapping[str, str] | None = None,
    invoking_pane: str | None = None,
    root: Path | str | None = None,
    run_id: str | None = None,
) -> tuple[str, str]:
    """Create a dedicated worker window bound to the invoking leader pane.

    Never kills the leader pane or the whole session on cleanup. Worker
    creation uses focus-detached tmux flags and restores the leader selection.
    """
    leader_pane = resolve_invoking_pane(pane=invoking_pane, env=env, require_exact=True)
    snap = snapshot_invoking_identity(leader_pane)
    live_name = str(snap["session_name"])
    live_id = str(snap["session_id"])
    if session and live_name != session:
        # Join the leader's real session regardless of planned name.
        pass
    window_nonce = secrets.token_hex(8)
    window_name = f"omg-team-{window_nonce}"
    window_id: str | None = None
    created_panes: list[str] = []
    intent_path: Path | None = None
    try:
        # Re-validate immediately before the first mutation so a mid-launch
        # client move cannot bind workers to a different session (#97 Pro P1).
        assert_invoking_identity(snap)
        leader_window = str(snap["window_id"])
        if root is not None and run_id is not None:
            intent_path = write_team_launch_intent(
                root,
                run_id=str(run_id),
                session_id=live_id,
                window_name=window_name,
                nonce=window_nonce,
            )
        window_id, first_pane = _launch_first_inside(
            task=tasks[0],
            env_pairs=env_pairs,
            window_name=window_name,
            target_window=leader_window,
            expected_session_id=live_id,
            intent_path=intent_path,
        )
        # Prove the new window belongs to the snapshotted session.
        win_probe = _tmux_run(
            [
                "display-message",
                "-p",
                "-t",
                window_id,
                "#{session_id}\t#{window_id}",
            ]
        )
        win_parts = (win_probe.stdout or "").strip().split("\t")
        if (
            win_probe.returncode != 0
            or len(win_parts) != 2
            or win_parts[0] != live_id
            or win_parts[1] != window_id
        ):
            raise TmuxTeamError(
                "created team window is not in the invoking session "
                f"(expected session_id={live_id!r})"
            )
        if first_pane == leader_pane:
            raise TmuxTeamError("refusing to overwrite leader pane with worker")
        created_panes.append(first_pane)
        created_panes.extend(
            _split_remaining(target=window_id, tasks=tasks[1:], env_pairs=env_pairs)
        )
        if leader_pane in created_panes:
            raise TmuxTeamError("worker pane list incorrectly includes leader pane")
        if len(created_panes) != len(tasks):
            raise TmuxTeamError("pane count mismatch after inside split")
        # Close the post-new-window TOCTOU: every worker pane must still sit
        # in the snapshotted session + created window before we stamp meta.
        for pane_id in created_panes:
            pane_probe = _tmux_run(
                [
                    "display-message",
                    "-p",
                    "-t",
                    pane_id,
                    "#{pane_id}\t#{session_id}\t#{window_id}",
                ]
            )
            pane_parts = (pane_probe.stdout or "").strip().split("\t")
            if (
                pane_probe.returncode != 0
                or len(pane_parts) != 3
                or pane_parts[0] != pane_id
                or pane_parts[1] != live_id
                or pane_parts[2] != window_id
            ):
                raise TmuxTeamError(
                    "worker pane left the invoking session/window before commit "
                    f"(pane={pane_id!r}, expected session_id={live_id!r} "
                    f"window_id={window_id!r})"
                )
        win_recheck = _tmux_run(
            [
                "display-message",
                "-p",
                "-t",
                window_id,
                "#{session_id}\t#{window_id}",
            ]
        )
        win_recheck_parts = (win_recheck.stdout or "").strip().split("\t")
        if (
            win_recheck.returncode != 0
            or len(win_recheck_parts) != 2
            or win_recheck_parts[0] != live_id
            or win_recheck_parts[1] != window_id
        ):
            raise TmuxTeamError(
                "team window left the invoking session before commit "
                f"(expected session_id={live_id!r})"
            )
        for task, pane_id in zip(tasks, created_panes, strict=True):
            task["pane_id"] = pane_id
        layout = _tmux_run(["select-layout", "-t", window_id, "tiled"])
        if layout.returncode != 0:
            raise TmuxTeamError("failed to apply tiled layout")
        _restore_leader_focus(leader_pane)
        _stamp_launch_meta(
            tasks,
            attach_mode="inside",
            session_owned=False,
            leader_pane_id=leader_pane,
            window_id=window_id,
            attach_hint=f"tmux select-pane -t {leader_pane}",
        )
        # Stash intent path for plane to clear after launch receipt publish.
        if intent_path is not None:
            tasks[0]["_tmux_launch_intent"] = str(intent_path)
    except (TmuxTeamError, OSError) as exc:
        # Never kill-session: only the team window / worker panes we created.
        # When window_id was never published, still kill by the unique launch
        # name so a failed new-window readback cannot leave an unrecepted orphan.
        cleanup_bits: list[str] = []
        cleanup_ok = False
        if window_id:
            err = _kill_window(window_id)
            if err:
                cleanup_bits.append(err)
            # Also require name-level absence proof when we have session+name.
            name_err = _kill_inside_windows_by_name(
                session_id=live_id, window_name=window_name
            )
            if name_err:
                cleanup_bits.append(name_err)
            else:
                cleanup_ok = True
        else:
            err = _kill_inside_windows_by_name(
                session_id=live_id, window_name=window_name
            )
            if err:
                cleanup_bits.append(err)
            else:
                cleanup_ok = True
            err = _kill_panes(created_panes)
            if err:
                cleanup_bits.append(err)
                cleanup_ok = False
        if cleanup_ok:
            clear_team_launch_intent(intent_path)
        if cleanup_bits:
            raise TmuxTeamError(f"{exc}; " + "; ".join(cleanup_bits)) from exc
        raise
    return live_name, live_id
