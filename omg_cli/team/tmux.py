"""Split-pane tmux transport for OMX-like ``omg team`` launch.

Legacy ``plane._create_tmux_session`` uses one window per task (``new-window``).
Shorthand launch uses this module so workers share one window via ``split-window``.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Sequence

from omg_cli.madmax import tmux_available, tmux_env_args

_TMUX_SESSION_ID = re.compile(r"^\$[0-9]{1,16}$")


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


def create_split_team_session(
    *,
    session: str,
    tasks: list[dict[str, Any]],
    env_pairs: list[tuple[str, str]],
) -> tuple[str, str]:
    """Create one detached session; split remaining tasks into the same window.

    Returns ``(session_name, session_id)``.
    """
    if not tmux_available():
        raise TmuxTeamError(
            "tmux is required for omg team launch (non-dry-run).\n"
            "  Install: brew install tmux\n"
            "  Or use --dry-run to write state without launching."
        )
    if not tasks:
        raise TmuxTeamError("no tasks for tmux session")

    first = tasks[0]
    first_env = tmux_env_args(list(first.get("_env_pairs") or env_pairs))
    create = _tmux_run(
        [
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{session_name}\t#{session_id}",
            "-s",
            session,
            "-n",
            "team",
            "-c",
            str(first["worktree"]),
            *first_env,
            str(first["pane_command"]),
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
        len(parts) != 2
        or parts[0] != session
        or _TMUX_SESSION_ID.fullmatch(parts[1]) is None
    ):
        cleanup = _cleanup_session((session, session))
        message = "tmux create did not return an exact session handle"
        if cleanup:
            message += f"; {cleanup}"
        raise TmuxTeamError(message)
    handle = (parts[0], parts[1])

    try:
        for task in tasks[1:]:
            task_env = tmux_env_args(list(task.get("_env_pairs") or env_pairs))
            split = _tmux_run(
                [
                    "split-window",
                    "-t",
                    handle[1],
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
        layout = _tmux_run(["select-layout", "-t", handle[1], "tiled"])
        if layout.returncode != 0:
            raise TmuxTeamError("failed to apply tiled layout")
        option = _tmux_run(["set-option", "-t", handle[1], "mouse", "on"])
        if option.returncode != 0:
            raise TmuxTeamError("failed to configure created tmux session")
    except (TmuxTeamError, OSError) as exc:
        cleanup = _cleanup_session(handle)
        if cleanup:
            raise TmuxTeamError(f"{exc}; {cleanup}") from exc
        raise
    return handle
