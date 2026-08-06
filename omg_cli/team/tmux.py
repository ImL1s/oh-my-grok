"""Split-pane tmux transport for OMX-like ``omg team`` launch.

Legacy ``plane._create_tmux_session`` uses one window per task (``new-window``).
Shorthand launch uses this module so workers share one window via ``split-window``.

Attach modes:
- ``detached`` — create a new session (outside tmux; requires TTY or ``--detach``)
- ``inside`` — create a dedicated window in the *current* session and split there;
  never kill the leader pane or the whole session on cleanup/stop
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence

from omg_cli.madmax import tmux_available, tmux_env_args

_TMUX_SESSION_ID = re.compile(r"^\$[0-9]{1,16}$")
_TMUX_PANE_ID = re.compile(r"^%[0-9]{1,16}$")
_TMUX_WINDOW_ID = re.compile(r"^@[0-9]{1,16}$")


class TmuxTeamError(RuntimeError):
    """tmux transport failure for team launch."""


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

    Fail-closed: missing tmux / spawn OSError → None (never raise into status).
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
    try:
        pane_pid = int(parts[3])
    except ValueError:
        return None
    if pane_pid <= 0:
        return None
    return {
        "pane_id": parts[0],
        "dead": parts[1] != "0",
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


def _launch_first_inside(
    *,
    task: dict[str, Any],
    env_pairs: list[tuple[str, str]],
    window_name: str,
    target_window: str,
) -> tuple[str, str]:
    """Create a new window beside *target_window* (``@N``); return (window_id, pane_id).

    Uses ``-d`` so the client stays on the invoking leader pane. Target must be
    a window id — tmux rejects ``new-window -a -t %pane`` (CMD_FIND_WINDOW).
    """
    if _TMUX_WINDOW_ID.fullmatch(target_window) is None:
        raise TmuxTeamError(
            f"new-window target must be a window id (@N), got {target_window!r}"
        )
    first_env = tmux_env_args(list(task.get("_env_pairs") or env_pairs))
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
    if create.returncode != 0:
        err = (create.stderr or create.stdout or "").strip()
        raise TmuxTeamError(
            f"failed to create team window in current session: {err}"
        )
    parts = (create.stdout or "").strip().split("\t")
    if (
        len(parts) != 2
        or _TMUX_WINDOW_ID.fullmatch(parts[0]) is None
        or _TMUX_PANE_ID.fullmatch(parts[1]) is None
    ):
        raise TmuxTeamError("tmux new-window did not return window/pane ids")
    return parts[0], parts[1]


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
) -> tuple[str, str]:
    """Create worker panes in one window; return ``(session_name, session_id)``.

    Mutates each task with ``pane_id``. Sets ``_tmux_launch`` on ``tasks[0]``
    (shared dict key consumed by plane) describing attach policy:
    ``attach_mode``, ``session_owned``, ``leader_pane_id``, ``window_id``,
    ``attach_hint``.
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
    window_name = "omg-team"
    window_id: str | None = None
    created_panes: list[str] = []
    try:
        # Re-validate immediately before the first mutation so a mid-launch
        # client move cannot bind workers to a different session (#97 Pro P1).
        assert_invoking_identity(snap)
        leader_window = str(snap["window_id"])
        window_id, first_pane = _launch_first_inside(
            task=tasks[0],
            env_pairs=env_pairs,
            window_name=window_name,
            target_window=leader_window,
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
    except (TmuxTeamError, OSError) as exc:
        # Never kill-session: only the team window / worker panes we created.
        cleanup_bits: list[str] = []
        if window_id:
            err = _kill_window(window_id)
            if err:
                cleanup_bits.append(err)
        else:
            err = _kill_panes(created_panes)
            if err:
                cleanup_bits.append(err)
        if cleanup_bits:
            raise TmuxTeamError(f"{exc}; " + "; ".join(cleanup_bits)) from exc
        raise
    return live_name, live_id
