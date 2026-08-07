"""Split-pane tmux transport for OMX-like ``omg team`` launch.

Legacy ``plane._create_tmux_session`` uses one window per task (``new-window``).
Shorthand launch uses this module so workers share one window via ``split-window``.

Attach modes:
- ``detached`` — create a new session (outside tmux; requires TTY or ``--detach``)
- ``inside`` — bind to the invoking leader session (#96):
  - ``same_window`` (default) — split workers beside the leader
  - ``dedicated_window`` — create an ``omg-team-*`` window and split there

Cleanup never kills the leader pane, leader window (same_window), or the
shared session.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from omg_cli.madmax import tmux_available, tmux_env_args

_TMUX_SESSION_ID = re.compile(r"^\$[0-9]{1,16}$")
_TMUX_PANE_ID = re.compile(r"^%[0-9]{1,16}$")
_TMUX_WINDOW_ID = re.compile(r"^@[0-9]{1,16}$")
_SAFE_INTENT_TOKEN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
TEAM_LAUNCH_LOCK_NAME = "team-launch.lock"
# Immutable launch-intent nonce stamped on the created window/pane (pane
# self-stamp + post-handle exact ``@N``/``%pane`` stamp+readback). Survives
# rename/move so sweep can find a worker that crashed after create but before
# WAL ``@N`` bind. Never stamped via targetless create-queue ``set-option``.
INTENT_NONCE_OPTION = "@omg_intent_nonce"


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


_TMUX_SERVER_FMT = "#{socket_path}\t#{pid}"


def _probe_tmux_server_identity(
    *,
    socket_path: str | None = None,
) -> dict[str, Any] | None:
    """Return ambient (or ``-S``-scoped) tmux server socket + pid start identity.

    ``$N`` / ``@N`` are only meaningful within one tmux server process. Crash
    recovery must refuse to act on those ids unless the live server matches the
    WAL-stamped socket path **and** server pid start-id.
    """
    try:
        result = _tmux_run(
            ["display-message", "-p", _TMUX_SERVER_FMT],
            socket_path=socket_path,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    parts = (result.stdout or "").strip().split("\t")
    if len(parts) != 2:
        return None
    sock, pid_s = parts[0].strip(), parts[1].strip()
    if not sock or "\x00" in sock:
        return None
    if not pid_s.isdigit():
        return None
    pid = int(pid_s)
    if pid <= 0:
        return None
    start = _process_start_identity(pid)
    if not start:
        return None
    return {
        "tmux_socket_path": sock,
        "tmux_server_pid": pid,
        "tmux_server_pid_start": start,
    }


def _intent_tmux_server(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract WAL-stamped tmux server identity, or None if incomplete."""
    sock = raw.get("tmux_socket_path")
    pid = raw.get("tmux_server_pid")
    start = raw.get("tmux_server_pid_start")
    if not isinstance(sock, str) or not sock or "\x00" in sock:
        return None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if not isinstance(start, str) or not start:
        return None
    return {
        "tmux_socket_path": sock,
        "tmux_server_pid": pid,
        "tmux_server_pid_start": start,
    }


def _tmux_server_matches(
    expected: Mapping[str, Any],
    live: Mapping[str, Any] | None,
) -> bool:
    if live is None:
        return False
    return (
        live.get("tmux_socket_path") == expected.get("tmux_socket_path")
        and live.get("tmux_server_pid") == expected.get("tmux_server_pid")
        and live.get("tmux_server_pid_start")
        == expected.get("tmux_server_pid_start")
    )


def _sh_single_quote(value: str) -> str:
    """POSIX single-quote *value* for embedding in an ``if-shell`` script."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _tmux_join_command(argv: Sequence[str]) -> str:
    """Join *argv* into one tmux command string for ``if-shell`` success/fail."""
    parts: list[str] = []
    for arg in argv:
        text = str(arg)
        if text and not any(ch in text for ch in " \t\n'\"\\#{};"):
            parts.append(text)
        else:
            parts.append(_sh_single_quote(text))
    return " ".join(parts)


def _tmux_identity_shell_predicate(
    *,
    expected_server: Mapping[str, Any],
    window_id: str | None = None,
    expected_session_id: str | None = None,
    pane_id: str | None = None,
) -> str:
    """Build a shell predicate for ``if-shell`` (no ``-F``).

    tmux expands ``#{…}`` before ``sh`` runs. Includes server **pid start-id**
    so a same-PID replacement after the Python pre-probe cannot satisfy the
    atomic kill/create gate. Optional *pane_id* gates splits against an exact
    leader / worker-stack pane (#96 same_window).
    """
    pid = expected_server.get("tmux_server_pid")
    start = expected_server.get("tmux_server_pid_start")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise TmuxTeamError("tmux identity predicate: invalid server pid")
    if not isinstance(start, str) or not start:
        raise TmuxTeamError("tmux identity predicate: invalid server pid_start")
    checks: list[str] = []
    if pane_id is not None:
        if _TMUX_PANE_ID.fullmatch(pane_id) is None:
            raise TmuxTeamError(
                f"tmux identity predicate: invalid pane id {pane_id!r}"
            )
        checks.append(f"[ '#{{pane_id}}' = {_sh_single_quote(pane_id)} ]")
    if window_id is not None:
        if _TMUX_WINDOW_ID.fullmatch(window_id) is None:
            raise TmuxTeamError(
                f"tmux identity predicate: invalid window id {window_id!r}"
            )
        # Single-quote both sides: tmux expands #{…} before sh runs, and a
        # double-quoted "$N" session/window token would be a positional param.
        checks.append(f"[ '#{{window_id}}' = {_sh_single_quote(window_id)} ]")
    if expected_session_id is not None:
        if _TMUX_SESSION_ID.fullmatch(expected_session_id) is None:
            raise TmuxTeamError(
                f"tmux identity predicate: invalid session {expected_session_id!r}"
            )
        checks.append(
            f"[ '#{{session_id}}' = {_sh_single_quote(expected_session_id)} ]"
        )
    checks.append(f"[ '#{{pid}}' = {_sh_single_quote(str(pid))} ]")
    if start.startswith("proc:") and start[5:].isdigit():
        # Mirror _process_start_identity: field 20 after `pid (comm) `.
        checks.append(
            '[ "$(sed \'s/.*) //\' /proc/#{pid}/stat 2>/dev/null | '
            f"awk '{{print $20}}')\" = {_sh_single_quote(start[5:])} ]"
        )
    elif start.startswith("ps:") and start[3:]:
        checks.append(
            '[ "$(ps -o lstart= -p #{pid} 2>/dev/null | '
            "tr -s '[:space:]' ' ' | sed 's/^ //;s/ $//')\" = "
            f"{_sh_single_quote(start[3:])} ]"
        )
    else:
        # Unknown start form — never authorize mutation.
        checks.append("[ 0 -eq 1 ]")
    return " && ".join(checks)


# tmux command used as if-shell *else* so a false identity predicate fails
# the client (empty else would return rc 0 and look like success).
_TMUX_IF_SHELL_REJECT = "run-shell 'false'"


def _tmux_run_if_identity(
    argv: Sequence[str],
    *,
    target: str,
    expected_server: Mapping[str, Any],
    socket_path: str | None = None,
    window_id: str | None = None,
    expected_session_id: str | None = None,
    pane_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *argv* only when *target* still matches full server identity.

    Gates create/split mutations so a replacement server on the same socket
    cannot receive the side effect between Python precheck and the client call.
    """
    server = _intent_tmux_server(expected_server)
    if server is None:
        raise TmuxTeamError("identity-gated tmux run refused: invalid expected server")
    predicate = _tmux_identity_shell_predicate(
        expected_server=server,
        window_id=window_id,
        expected_session_id=expected_session_id,
        pane_id=pane_id,
    )
    return _tmux_run(
        [
            "if-shell",
            "-t",
            target,
            predicate,
            _tmux_join_command(argv),
            _TMUX_IF_SHELL_REJECT,
        ],
        socket_path=socket_path,
    )


def _tmux_server_identity_proven_gone(expected: Mapping[str, Any]) -> bool:
    """True only when WAL server PID is OS-proven dead or start-id replaced.

    A failed/None ``_probe_tmux_server_identity`` is **not** proof the original
    server is gone — transient socket errors must not abandon WAL authority.
    """
    server = _intent_tmux_server(expected)
    if server is None:
        return False
    pid = int(server["tmux_server_pid"])
    start = str(server["tmux_server_pid_start"])
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    live_start = _process_start_identity(pid)
    if live_start is None:
        return False
    return live_start != start


def _server_identity_from_create(
    *,
    pid: int,
    socket_path: str,
) -> dict[str, Any]:
    """Build durable server identity from ``new-session``/``new-window`` fields.

    Prefer OS start-id for the create-reported pid so a mocked ambient probe
    cannot disagree with the server that actually performed the create (live
    tests). Fall back to a matching probe only for hermetic fake PIDs that
    have no ``/proc``/``ps`` start identity.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise TmuxTeamError("create server identity refused: invalid pid")
    if not isinstance(socket_path, str) or not socket_path or "\x00" in socket_path:
        raise TmuxTeamError("create server identity refused: invalid socket_path")
    start = _process_start_identity(pid)
    if not start:
        probed = _probe_tmux_server_identity(socket_path=socket_path)
        if (
            probed is not None
            and probed.get("tmux_socket_path") == socket_path
            and probed.get("tmux_server_pid") == pid
            and isinstance(probed.get("tmux_server_pid_start"), str)
            and probed["tmux_server_pid_start"]
        ):
            start = str(probed["tmux_server_pid_start"])
        else:
            raise TmuxTeamError(
                "create server identity refused: pid_start unavailable"
            )
    return {
        "tmux_socket_path": socket_path,
        "tmux_server_pid": pid,
        "tmux_server_pid_start": start,
    }


def _require_tmux_server(
    expected: Mapping[str, Any] | None,
    *,
    socket_path: str | None = None,
    action: str,
) -> dict[str, Any]:
    """Fail closed unless live server matches *expected* (socket+pid+start)."""
    server = _intent_tmux_server(expected) if expected is not None else None
    if server is None:
        raise TmuxTeamError(f"{action} refused: tmux server identity required")
    sock = socket_path or str(server["tmux_socket_path"])
    live = _probe_tmux_server_identity(socket_path=sock)
    if not _tmux_server_matches(server, live):
        raise TmuxTeamError(
            f"{action} refused: tmux server identity mismatch "
            f"(wal_pid={server['tmux_server_pid']})"
        )
    return server


def _intent_requires_durable_window_id(raw: Mapping[str, Any]) -> bool:
    """True when unbound ``side_effect`` WAL must not clear on name-only absence.

    After ``side_effect_started`` (or legacy/missing flag), an unbound WAL may
    represent mark-before-dispatch (never created) *or* a renamed live worker
    that crashed before ``@N`` bind. Name absence alone must never authorize
    clear — sweep must prove create-time intent nonce absence **only** after
    publication is acknowledged (or the nonce was positively observed then
    removed). Only an explicit ``side_effect_started: false`` (pre-``new-window``)
    skips that bar.
    """
    if _intent_known_window_ids(raw):
        return True
    flag = raw.get("side_effect_started")
    if flag is False:
        return False
    return True


def _intent_nonce_value(raw: Mapping[str, Any]) -> str | None:
    """Return the WAL launch-intent nonce when present and non-empty."""
    nonce = raw.get("nonce")
    if isinstance(nonce, str) and nonce:
        return nonce
    return None


def _intent_nonce_published(raw: Mapping[str, Any]) -> bool:
    """True when WAL durably acknowledges create-time nonce publication."""
    return raw.get("nonce_published") is True


def write_team_launch_intent(
    root: Path | str,
    *,
    run_id: str,
    session_id: str,
    window_name: str,
    nonce: str,
    tmux_server: Mapping[str, Any] | None = None,
    view_mode: str | None = None,
    leader_pane_id: str | None = None,
    leader_window_id: str | None = None,
) -> Path:
    """Atomically persist launch intent *before* create/split side effects.

    Stamps the ambient tmux **server** identity (socket + pid start-id) so
    later recovery cannot kill a same-numbered ``@N``/``%pane`` on another
    restarted server. ``window_id`` is omitted until
    :func:`bind_team_launch_intent_window_id` stamps the immutable ``@N``
    (dedicated path). ``same_window`` intents stamp ``leader_pane_id`` /
    ``leader_window_id`` / ``created_pane_ids`` and must never authorize
    ``kill-window`` on the leader window.
    """
    from omg_cli.contracts.path_keys import (
        DATA_FILE_MODE,
        ContractPathError,
        atomic_write_bytes,
        ensure_managed_dir,
    )
    from omg_cli.team.topology import VIEW_MODES

    owner_pid = os.getpid()
    owner_start = _process_start_identity(owner_pid)
    if not owner_start:
        raise TmuxTeamError(
            "launch intent write refused: owner pid_start identity unavailable"
        )
    server = (
        dict(tmux_server)
        if tmux_server is not None
        else _probe_tmux_server_identity()
    )
    if server is None or _intent_tmux_server(server) is None:
        raise TmuxTeamError(
            "launch intent write refused: tmux server identity unavailable"
        )
    # Normalize through the extractor so JSON always carries the exact key set.
    server = _intent_tmux_server(server)
    assert server is not None
    path = team_launch_intent_path(root, run_id, nonce)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "session_id": session_id,
        "window_name": window_name,
        "nonce": nonce,
        "owner_pid": owner_pid,
        "owner_pid_start": owner_start,
        "side_effect_started": False,
        "nonce_published": False,
        "tmux_socket_path": server["tmux_socket_path"],
        "tmux_server_pid": server["tmux_server_pid"],
        "tmux_server_pid_start": server["tmux_server_pid_start"],
        "created_at": _utc_now_iso(),
        "created_pane_ids": [],
    }
    if view_mode is not None:
        if view_mode not in VIEW_MODES:
            raise TmuxTeamError(
                f"launch intent write refused: unsupported view_mode {view_mode!r}"
            )
        payload["view_mode"] = view_mode
    if leader_pane_id is not None:
        if _TMUX_PANE_ID.fullmatch(leader_pane_id) is None:
            raise TmuxTeamError(
                f"launch intent write refused: invalid leader pane {leader_pane_id!r}"
            )
        payload["leader_pane_id"] = leader_pane_id
    if leader_window_id is not None:
        if _TMUX_WINDOW_ID.fullmatch(leader_window_id) is None:
            raise TmuxTeamError(
                "launch intent write refused: invalid leader window "
                f"{leader_window_id!r}"
            )
        payload["leader_window_id"] = leader_window_id
    body = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        ensure_managed_dir(path.parent)
        atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=True)
    except ContractPathError as exc:
        raise TmuxTeamError(f"launch intent write refused: {exc}") from exc
    return path


def append_team_launch_intent_pane_id(path: Path | str, pane_id: str) -> None:
    """Atomically append a created worker pane id to the launch-intent WAL."""
    from omg_cli.contracts.path_keys import (
        DATA_FILE_MODE,
        ContractPathError,
        atomic_write_bytes,
    )

    if _TMUX_PANE_ID.fullmatch(pane_id) is None:
        raise TmuxTeamError(
            f"launch intent pane append refused: invalid pane id {pane_id!r}"
        )
    intent = Path(path)
    try:
        raw = json.loads(intent.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TmuxTeamError(
            f"launch intent pane append refused (unreadable): {intent}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise TmuxTeamError(
            f"launch intent pane append refused (invalid): {intent}"
        )
    leader = raw.get("leader_pane_id")
    if isinstance(leader, str) and leader == pane_id:
        raise TmuxTeamError(
            "launch intent pane append refused: refusing leader pane id"
        )
    existing = raw.get("created_pane_ids")
    panes: list[str] = []
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, str) and _TMUX_PANE_ID.fullmatch(item):
                panes.append(item)
    if pane_id in panes:
        return
    panes.append(pane_id)
    payload = dict(raw)
    payload["created_pane_ids"] = panes
    payload["side_effect_started"] = True
    body = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        atomic_write_bytes(intent, body, mode=DATA_FILE_MODE, replace=True)
    except ContractPathError as exc:
        raise TmuxTeamError(f"launch intent pane append refused: {exc}") from exc


def _intent_known_pane_ids(raw: Mapping[str, Any]) -> list[str]:
    """Return durable worker pane ids stamped on a same_window launch intent."""
    raw_panes = raw.get("created_pane_ids")
    if not isinstance(raw_panes, list):
        return []
    out: list[str] = []
    for item in raw_panes:
        if isinstance(item, str) and _TMUX_PANE_ID.fullmatch(item) is not None:
            out.append(item)
    return out


def _intent_view_mode(raw: Mapping[str, Any]) -> str | None:
    mode = raw.get("view_mode")
    if isinstance(mode, str) and mode:
        return mode
    return None


def _kill_panes_scoped(
    pane_ids: Sequence[str],
    *,
    expected_server: Mapping[str, Any] | None,
    expected_session_id: str | None,
    expected_window_id: str | None,
    intent_or_launch_nonce: str | None,
    leader_pane_id: str | None,
    socket_path: str | None = None,
) -> str | None:
    """Kill only transaction-owned worker panes with identity + absence proof.

    Never kills *leader_pane_id*. Probe unknown / identity drift / nonce
    mismatch retain authority (return error; do not claim success).
    """
    server = (
        _intent_tmux_server(expected_server) if expected_server is not None else None
    )
    if expected_server is not None and server is None:
        return "scoped kill-pane refused: invalid expected server"
    if server is not None and socket_path is None:
        socket_path = str(server["tmux_socket_path"])
    if server is not None:
        live = _probe_tmux_server_identity(socket_path=socket_path)
        if not _tmux_server_matches(server, live):
            return (
                "scoped kill-pane refused: tmux server identity mismatch — "
                "refuse foreign/restarted %pane"
            )
    errors: list[str] = []
    for pane_id in pane_ids:
        if _TMUX_PANE_ID.fullmatch(pane_id) is None:
            errors.append(f"refused kill-pane for non-pane id {pane_id!r}")
            continue
        if leader_pane_id is not None and pane_id == leader_pane_id:
            errors.append(f"refused kill-pane for leader pane {pane_id}")
            continue
        probe = _tmux_run(
            [
                "display-message",
                "-p",
                "-t",
                pane_id,
                "#{pane_id}\t#{session_id}\t#{window_id}\t#{pid}\t"
                "#{pane_dead}\t#{" + INTENT_NONCE_OPTION + "}",
            ],
            socket_path=socket_path,
        )
        if probe.returncode != 0:
            # Pane already gone — treat as success for this id.
            continue
        parts = (probe.stdout or "").strip().split("\t")
        if len(parts) < 5:
            errors.append(f"kill-pane {pane_id}: identity probe malformed")
            continue
        got_pane, got_session, got_window, got_pid, dead = parts[:5]
        nonce_live = parts[5] if len(parts) > 5 else ""
        if got_pane != pane_id:
            errors.append(f"kill-pane {pane_id}: pane id drift ({got_pane!r})")
            continue
        if expected_session_id is not None and got_session != expected_session_id:
            errors.append(
                f"kill-pane {pane_id}: session drift "
                f"(expected {expected_session_id!r} got {got_session!r})"
            )
            continue
        if expected_window_id is not None and got_window != expected_window_id:
            errors.append(
                f"kill-pane {pane_id}: window drift "
                f"(expected {expected_window_id!r} got {got_window!r})"
            )
            continue
        if server is not None:
            if not got_pid.isdigit() or int(got_pid) != server["tmux_server_pid"]:
                errors.append(
                    f"kill-pane {pane_id}: server pid mismatch "
                    f"(expected {server['tmux_server_pid']})"
                )
                continue
        if (
            intent_or_launch_nonce is not None
            and nonce_live
            and nonce_live != intent_or_launch_nonce
        ):
            errors.append(
                f"kill-pane {pane_id}: intent nonce mismatch "
                f"(expected {intent_or_launch_nonce!r} got {nonce_live!r})"
            )
            continue
        if dead == "1":
            # Dead but still addressable — still kill to remove the pane object.
            pass
        if server is not None:
            try:
                predicate = _tmux_identity_shell_predicate(
                    expected_server=server,
                    window_id=expected_window_id,
                    expected_session_id=expected_session_id,
                    pane_id=pane_id,
                )
            except TmuxTeamError as exc:
                errors.append(f"kill-pane {pane_id}: {exc}")
                continue
            killed = _tmux_run(
                [
                    "if-shell",
                    "-t",
                    pane_id,
                    predicate,
                    f"kill-pane -t {pane_id}",
                    _TMUX_IF_SHELL_REJECT,
                ],
                socket_path=socket_path,
            )
        else:
            killed = _tmux_run(
                ["kill-pane", "-t", pane_id], socket_path=socket_path
            )
        if killed.returncode not in (0, 1):
            err = (killed.stderr or killed.stdout or "").strip()
            errors.append(f"kill-pane {pane_id}: exit {killed.returncode} {err}")
            continue
        # Absence proof via full list-panes -a.
        listed = _tmux_run(
            ["list-panes", "-a", "-F", "#{pane_id}"], socket_path=socket_path
        )
        if listed.returncode != 0:
            errors.append(
                f"kill-pane {pane_id}: absence probe exit {listed.returncode}"
            )
            continue
        still = {
            line.strip()
            for line in (listed.stdout or "").splitlines()
            if line.strip()
        }
        if pane_id in still:
            errors.append(f"kill-pane {pane_id}: still present after kill")
    return "; ".join(errors) if errors else None


def mark_team_launch_intent_side_effect(path: Path | str) -> None:
    """Stamp ``side_effect_started`` immediately before ``new-window``.

    After this mark, crash recovery must not clear the WAL on name-only
    absence. Unbound intents require a bound ``@window_id``, a positively
    observed-then-removed create-time nonce, or a durable
    ``nonce_published`` ack before nonce-absence may authorize clear.
    """
    from omg_cli.contracts.path_keys import (
        DATA_FILE_MODE,
        ContractPathError,
        atomic_write_bytes,
    )

    intent = Path(path)
    try:
        raw = json.loads(intent.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TmuxTeamError(
            f"launch intent side-effect mark refused (unreadable): {intent}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise TmuxTeamError(
            f"launch intent side-effect mark refused (invalid): {intent}"
        )
    if raw.get("side_effect_started") is True:
        return
    payload = dict(raw)
    payload["side_effect_started"] = True
    body = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        atomic_write_bytes(intent, body, mode=DATA_FILE_MODE, replace=True)
    except ContractPathError as exc:
        raise TmuxTeamError(
            f"launch intent side-effect mark refused: {exc}"
        ) from exc


def unmark_team_launch_intent_side_effect(path: Path | str) -> None:
    """Revert ``side_effect_started`` when ``new-window`` is proven not to create.

    Used after a synchronous ``new-window`` failure (non-zero rc) when session
    discovery shows the launch name absent and no durable ``@N`` was bound.
    Without this, a false-positive mark permanently wedges project launches.
    Refuses to unmark when a durable ``window_id`` is already bound.
    """
    from omg_cli.contracts.path_keys import (
        DATA_FILE_MODE,
        ContractPathError,
        atomic_write_bytes,
    )

    intent = Path(path)
    try:
        raw = json.loads(intent.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TmuxTeamError(
            f"launch intent side-effect unmark refused (unreadable): {intent}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise TmuxTeamError(
            f"launch intent side-effect unmark refused (invalid): {intent}"
        )
    if _intent_known_window_ids(raw):
        raise TmuxTeamError(
            "launch intent side-effect unmark refused: durable window_id bound"
        )
    if raw.get("side_effect_started") is False:
        return
    payload = dict(raw)
    payload["side_effect_started"] = False
    body = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        atomic_write_bytes(intent, body, mode=DATA_FILE_MODE, replace=True)
    except ContractPathError as exc:
        raise TmuxTeamError(
            f"launch intent side-effect unmark refused: {exc}"
        ) from exc


def ack_team_launch_intent_nonce_published(path: Path | str) -> None:
    """Stamp ``nonce_published`` after exact-handle publication is proven.

    Callers must bind durable ``@window_id``, stamp ``@omg_intent_nonce`` onto
    the returned ``@N``/``%pane`` handles, and read the option back from those
    same handles before acking. A bare ``new-window -P`` handle is **not**
    proof of publication (``after-new-window`` can retarget targetless queue
    stamps onto the leader). Idempotent when already true; refuses a
    missing/unreadable intent.
    """
    from omg_cli.contracts.path_keys import (
        DATA_FILE_MODE,
        ContractPathError,
        atomic_write_bytes,
    )

    intent = Path(path)
    try:
        raw = json.loads(intent.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TmuxTeamError(
            f"launch intent nonce-published ack refused (unreadable): "
            f"{intent}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise TmuxTeamError(
            f"launch intent nonce-published ack refused (invalid): {intent}"
        )
    if raw.get("nonce_published") is True:
        return
    payload = dict(raw)
    payload["nonce_published"] = True
    # Publication implies the create side effect occurred.
    payload["side_effect_started"] = True
    body = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        atomic_write_bytes(intent, body, mode=DATA_FILE_MODE, replace=True)
    except ContractPathError as exc:
        raise TmuxTeamError(
            f"launch intent nonce-published ack refused: {exc}"
        ) from exc


def bind_team_launch_intent_window_id(
    path: Path | str,
    window_id: str,
) -> None:
    """Atomically stamp immutable ``@window_id`` onto an existing launch intent.

    Must run as soon as ``new-window`` publishes a handle — before receipt —
    so a crash + rename cannot leave a live worker with only a name-bound WAL.
    Idempotent when the same id is already bound; refuses a conflicting id or
    a missing/unreadable intent.
    """
    from omg_cli.contracts.path_keys import (
        DATA_FILE_MODE,
        ContractPathError,
        atomic_write_bytes,
    )

    if not isinstance(window_id, str) or _TMUX_WINDOW_ID.fullmatch(window_id) is None:
        raise TmuxTeamError(
            f"launch intent window_id bind refused: invalid id {window_id!r}"
        )
    intent = Path(path)
    try:
        raw = json.loads(intent.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TmuxTeamError(
            f"launch intent window_id bind refused (unreadable): {intent}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise TmuxTeamError(
            f"launch intent window_id bind refused (invalid): {intent}"
        )
    existing = raw.get("window_id")
    if existing is not None:
        if existing == window_id:
            return
        raise TmuxTeamError(
            f"launch intent window_id bind refused: already bound to "
            f"{existing!r}, not {window_id!r}"
        )
    payload = dict(raw)
    payload["window_id"] = window_id
    # Binding implies the side effect occurred.
    payload["side_effect_started"] = True
    body = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        atomic_write_bytes(intent, body, mode=DATA_FILE_MODE, replace=True)
    except ContractPathError as exc:
        raise TmuxTeamError(
            f"launch intent window_id bind refused: {exc}"
        ) from exc


def _intent_known_window_ids(raw: Mapping[str, Any]) -> list[str]:
    """Return durable ``@window_id`` values stamped on a launch intent WAL."""
    wid = raw.get("window_id")
    if isinstance(wid, str) and _TMUX_WINDOW_ID.fullmatch(wid) is not None:
        return [wid]
    return []


def clear_team_launch_intent(path: Path | str | None) -> None:
    """Durably remove a launch intent after commit or proven cleanup.

    Callers must clear only after the durable commit point (immutable launch
    receipt **and** hash-bound ``team.json``) or after absence-proven kill.
    Unlink is fail-closed: OSError (other than already-absent) propagates so
    callers cannot leave a stale WAL that later sweeps a receipt-bound worker.
    Parent-directory fsync after a successful unlink is best-effort: raising
    there would force start_team rollback to delete receipt/team.json after the
    WAL is already gone, hiding a live worker. Already-absent is success.
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
        # Best-effort directory durability only — WAL is already gone.
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
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
    """True only when durable receipt **and** full identity-chain bind intent.

    Receipt-only states (immutable receipt on disk, no ``team.json``) must not
    adopt or clear the WAL — stop cannot load meta, and silent clear would hide
    an unrecovered live worker. Schema-v1 (#106) launch receipts omit
    ``intent_nonce`` / ``window_name`` and must never be adopted. Only schema-v2
    receipts that pass :func:`omg_cli.team.plane._load_team_identity_chain`
    against loaded team meta (exact key set, valid ``identity_generation``,
    complete generation receipts, tasks continuity, canonical body hash /
    ``launch_receipt_sha256``, session / launch_nonce) with matching intent
    binding fields qualify. Non-zero / malformed generation without a complete
    chain must refuse adopt — never clear the WAL.
    """
    from omg_cli.team.plane import (
        LAUNCH_RECEIPT_SCHEMA_VERSION,
        TeamError,
        _load_team_identity_chain,
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
        # Full chain (gen-0 launch + every scaled identity receipt) — refuse
        # adopt when identity_generation is non-zero/malformed without receipts.
        chain = _load_team_identity_chain(root, intent_run, meta)
    except TeamError:
        return False
    if not chain:
        return False
    receipt = chain[0]
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
    absence is proven, or when a durable launch receipt **and** hash-bound
    ``team.json`` already bind the *exact* intent identity (adopt — never kill
    a receipt-bound worker; never clear for receipt-only orphans).

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
        known_ids = _intent_known_window_ids(raw)
        expected_server = _intent_tmux_server(raw)
        if expected_server is None:
            results.append(
                {
                    "path": str(entry),
                    "ok": False,
                    "session_id": intent_session,
                    "window_name": intent_name,
                    "error": (
                        "launch intent missing tmux server identity — "
                        "refuse kill/clear of unscoped $N/@N"
                    ),
                }
            )
            continue
        live_server = _probe_tmux_server_identity(
            socket_path=str(expected_server["tmux_socket_path"])
        )
        if not _tmux_server_matches(expected_server, live_server):
            # Abandon only when the original server is *proven* gone/replaced:
            # (1) probe returned a different live identity on the socket, or
            # (2) OS proves the WAL pid is dead / start-id reused.
            # probe→None / malformed / missing start-id is UNKNOWN — keep WAL.
            proven_gone = live_server is not None or _tmux_server_identity_proven_gone(
                expected_server
            )
            if not proven_gone:
                results.append(
                    {
                        "path": str(entry),
                        "ok": False,
                        "session_id": intent_session,
                        "window_name": intent_name,
                        "error": (
                            "tmux server identity probe unknown — "
                            "refuse abandon of unrecovered launch WAL"
                        ),
                    }
                )
                continue
            # Never kill same-numbered @N on a foreign/replacement socket.
            try:
                clear_team_launch_intent(entry)
            except TmuxTeamError as exc:
                results.append(
                    {
                        "path": str(entry),
                        "ok": False,
                        "session_id": intent_session,
                        "window_name": intent_name,
                        "error": (
                            "tmux server identity mismatch — abandon clear "
                            f"failed: {exc}"
                        ),
                    }
                )
                continue
            results.append(
                {
                    "path": str(entry),
                    "ok": True,
                    "abandoned_server": True,
                    "session_id": intent_session,
                    "window_name": intent_name,
                }
            )
            continue
        # same_window: never kill-window — only scoped worker panes.
        if _intent_view_mode(raw) == "same_window":
            leader_pane = raw.get("leader_pane_id")
            leader_window = raw.get("leader_window_id")
            if not isinstance(leader_pane, str) or not isinstance(leader_window, str):
                results.append(
                    {
                        "path": str(entry),
                        "ok": False,
                        "session_id": intent_session,
                        "window_name": intent_name,
                        "error": (
                            "same_window launch intent missing leader pane/window "
                            "— refuse unscoped cleanup"
                        ),
                    }
                )
                continue
            pane_ids = _intent_known_pane_ids(raw)
            if not pane_ids and raw.get("side_effect_started") is True:
                # Side effect claimed but no pane ids — keep WAL (fail closed).
                results.append(
                    {
                        "path": str(entry),
                        "ok": False,
                        "session_id": intent_session,
                        "window_name": intent_name,
                        "error": (
                            "same_window side_effect without created_pane_ids — "
                            "refuse clear"
                        ),
                    }
                )
                continue
            cleanup = _kill_panes_scoped(
                pane_ids,
                expected_server=expected_server,
                expected_session_id=intent_session,
                expected_window_id=leader_window,
                intent_or_launch_nonce=_intent_nonce_value(raw),
                leader_pane_id=leader_pane,
                socket_path=str(expected_server["tmux_socket_path"]),
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
                            "error": f"clear after pane absence failed: {exc}",
                        }
                    )
                    continue
                results.append(
                    {
                        "path": str(entry),
                        "ok": True,
                        "session_id": intent_session,
                        "window_name": intent_name,
                        "view_mode": "same_window",
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
            continue
        cleanup = _kill_inside_windows_by_name(
            session_id=intent_session,
            window_name=intent_name,
            known_window_ids=known_ids,
            socket_path=str(expected_server["tmux_socket_path"]),
            expected_server=expected_server,
            require_durable_window_id=_intent_requires_durable_window_id(raw),
            intent_nonce=_intent_nonce_value(raw),
            nonce_published=_intent_nonce_published(raw),
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



def _tmux_run(
    args: Sequence[str],
    *,
    socket_path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a tmux client command, optionally pinned to ``-S socket_path``.

    Recovery paths that act on WAL-stamped ``@N`` / ``$N`` **must** pass the
    WAL socket so the ambient default server cannot satisfy a foreign id.
    """
    argv: list[str] = ["tmux"]
    if socket_path is not None:
        if (
            not isinstance(socket_path, str)
            or not socket_path
            or "\x00" in socket_path
        ):
            raise TmuxTeamError(
                f"refused tmux -S with invalid socket_path {socket_path!r}"
            )
        argv.extend(["-S", socket_path])
    argv.extend(args)
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
    )


def _cleanup_session(
    handle: tuple[str, str],
    *,
    socket_path: str | None = None,
    expected_server: Mapping[str, Any] | None = None,
) -> str | None:
    """Kill a Team-owned detached session; refuse foreign/restarted servers."""
    name, session_id = handle
    if expected_server is not None:
        server = _intent_tmux_server(expected_server)
        if server is None:
            return (
                f"refused kill-session for {session_id}: invalid expected server"
            )
        if socket_path is None:
            socket_path = str(server["tmux_socket_path"])
        live = _probe_tmux_server_identity(socket_path=socket_path)
        if not _tmux_server_matches(server, live):
            return (
                f"kill-session {session_id}: tmux server identity mismatch — "
                "refuse foreign/restarted $N"
            )
        if _TMUX_SESSION_ID.fullmatch(session_id) is None:
            return f"refused kill-session for non-session id {session_id!r}"
        try:
            predicate = _tmux_identity_shell_predicate(
                expected_server=server,
                expected_session_id=session_id,
            )
        except TmuxTeamError as exc:
            return f"kill-session {session_id}: {exc}"
        try:
            killed = _tmux_run(
                [
                    "if-shell",
                    "-t",
                    session_id,
                    predicate,
                    f"kill-session -t {session_id}",
                    "",
                    ";",
                    "has-session",
                    "-t",
                    session_id,
                ],
                socket_path=socket_path,
            )
        except OSError as exc:
            return f"kill-session {session_id}: OSError {exc}"
        # has-session exit 1 → gone; 0 → still present (predicate refused or kill failed).
        if killed.returncode == 1:
            return None
        return (
            f"kill-session {session_id}: still present or identity refused "
            f"(exit {killed.returncode})"
        )
    for target in (session_id, name):
        killed = _tmux_run(
            ["kill-session", "-t", target], socket_path=socket_path
        )
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
    socket_path: str | None = None,
    expected_server: Mapping[str, Any] | None = None,
    expected_session_id: str | None = None,
    expected_window_id: str | None = None,
) -> str:
    """Split a replacement worker pane into ``target``; return new ``pane_id``.

    Used by resume when a worker pane died but the team session/window remains.
    Never kills the leader pane or the whole session.

    When *expected_server* is set, ``split-window`` runs under a PID+start
    ``if-shell`` gate (same socket / ``$session`` / ``@window``) so a
    replacement server on the ambient socket cannot receive the pane mutation.
    """
    if not tmux_available():
        raise TmuxTeamError("tmux is required to respawn a team worker pane")
    if not target or not str(target).strip():
        raise TmuxTeamError("respawn target (session/window id) required")
    if not worktree or not pane_command:
        raise TmuxTeamError("respawn requires worktree and pane_command")
    task_env = tmux_env_args(list(env_pairs or []))
    split_argv = [
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
    server = (
        _intent_tmux_server(expected_server) if expected_server is not None else None
    )
    if server is not None:
        if socket_path is None:
            socket_path = str(server["tmux_socket_path"])
        window_id = expected_window_id
        if window_id is None and _TMUX_WINDOW_ID.fullmatch(str(target)):
            window_id = str(target)
        split = _tmux_run_if_identity(
            split_argv,
            target=str(target),
            expected_server=server,
            socket_path=socket_path,
            window_id=window_id,
            expected_session_id=expected_session_id,
        )
    else:
        split = _tmux_run(split_argv, socket_path=socket_path)
    if split.returncode != 0:
        err = (split.stderr or split.stdout or "").strip()
        raise TmuxTeamError(f"failed to respawn worker pane: {err}")
    pane_id = (split.stdout or "").strip()
    if _TMUX_PANE_ID.fullmatch(pane_id) is None:
        raise TmuxTeamError("respawn split-window did not return pane id")
    return pane_id


def _and_tmux_formats(checks: Sequence[str]) -> str:
    """Combine already-safe tmux boolean formats without invoking a shell."""
    if not checks:
        return "0"
    combined = checks[0]
    for check in checks[1:]:
        combined = f"#{{&&:{combined},{check}}}"
    return combined


def _kill_window(
    window_id: str,
    *,
    socket_path: str | None = None,
    expected_session_id: str | None = None,
    expected_server: Mapping[str, Any] | None = None,
) -> str | None:
    """Kill *window_id* and require global absence proof on the scoped server.

    Bare ``kill-window`` rc 0/1 is never success by itself — list-windows -a
    must omit the target. When *expected_server* and/or *expected_session_id*
    are provided, identity check and kill run as **one** server-side
    ``if-shell`` conditional in the same client command queue as the absence
    ``list-windows`` — closing the probe→kill TOCTOU where a restarted server
    could reuse ``@N`` between separate client calls. Foreign/restarted
    servers must not satisfy a stale ``@N``.
    """
    if _TMUX_WINDOW_ID.fullmatch(window_id) is None:
        return f"refused kill-window for non-window id {window_id!r}"
    if expected_server is not None:
        # Defense-in-depth start-id gate before the atomic queue (PID reuse).
        live = _probe_tmux_server_identity(socket_path=socket_path)
        if not _tmux_server_matches(expected_server, live):
            return (
                f"kill-window {window_id}: tmux server identity mismatch — "
                "refuse foreign/restarted @N"
            )
        if socket_path is None:
            socket_path = str(expected_server.get("tmux_socket_path") or "") or None
    if expected_session_id is not None:
        if _TMUX_SESSION_ID.fullmatch(expected_session_id) is None:
            return (
                f"kill-window {window_id}: invalid expected session "
                f"{expected_session_id!r}"
            )
    if expected_server is not None or expected_session_id is not None:
        return _kill_window_atomic(
            window_id,
            socket_path=socket_path,
            expected_session_id=expected_session_id,
            expected_server=expected_server,
        )
    # Unscoped path (no WAL server/session): still kill+list in one queue so
    # a mid-cleanup socket swap cannot satisfy kill without absence proof.
    try:
        listed = _tmux_run(
            [
                "kill-window",
                "-t",
                window_id,
                ";",
                "list-windows",
                "-a",
                "-F",
                "#{window_id}",
            ],
            socket_path=socket_path,
        )
    except OSError as exc:
        return f"kill-window {window_id}: OSError {exc}"
    return _absence_from_list_windows(
        window_id, listed, allow_dead_server=True
    )


def _kill_window_atomic(
    window_id: str,
    *,
    socket_path: str | None,
    expected_session_id: str | None,
    expected_server: Mapping[str, Any] | None,
) -> str | None:
    """Server-side conditional kill + absence list in one tmux client queue.

    When *expected_server* is set, the predicate is a **shell** ``if-shell``
    (no ``-F``) that checks ``#{pid}`` **and** the durable pid start-id, so a
    same-PID OS reuse after the Python pre-probe cannot authorize kill.
    """
    if expected_server is not None:
        server = _intent_tmux_server(expected_server)
        if server is None:
            return f"kill-window {window_id}: invalid expected server identity"
        try:
            predicate = _tmux_identity_shell_predicate(
                expected_server=server,
                window_id=window_id,
                expected_session_id=expected_session_id,
            )
        except TmuxTeamError as exc:
            return f"kill-window {window_id}: {exc}"
        if_shell_argv = [
            "if-shell",
            "-t",
            window_id,
            predicate,
            f"kill-window -t {window_id}",
            "",
        ]
    else:
        checks: list[str] = [f"#{{==:#{{window_id}},{window_id}}}"]
        if expected_session_id is not None:
            checks.append(f"#{{==:#{{session_id}},{expected_session_id}}}")
        predicate = _and_tmux_formats(checks)
        if_shell_argv = [
            "if-shell",
            "-F",
            "-t",
            window_id,
            predicate,
            f"kill-window -t {window_id}",
            "",
        ]
    try:
        listed = _tmux_run(
            [
                *if_shell_argv,
                ";",
                "list-windows",
                "-a",
                "-F",
                "#{window_id}\t#{session_id}\t#{pid}",
            ],
            socket_path=socket_path,
        )
    except OSError as exc:
        return f"kill-window {window_id}: OSError {exc}"
    # Parse rich rows so a still-present foreign @N is refused, not retried.
    stdout = listed.stdout or ""
    stderr = (listed.stderr or "").strip()
    if listed.returncode != 0 and not stdout.strip():
        # Last-window kill can tear down the server ("no current target" /
        # "no server running") — absence is then proven on this socket.
        low = stderr.lower()
        if "no server" in low or "no current target" in low or "no such" in low:
            return None
        return (
            f"kill-window {window_id}: absence probe exit {listed.returncode}"
            + (f" {stderr}" if stderr else "")
        )
    present_rows: list[tuple[str, str, str]] = []
    for line in stdout.splitlines():
        token = line.strip()
        if not token:
            continue
        parts = token.split("\t")
        if len(parts) != 3:
            return (
                f"kill-window {window_id}: absence probe malformed row {token!r}"
            )
        wid, sid, pid_s = (p.strip() for p in parts)
        if _TMUX_WINDOW_ID.fullmatch(wid) is None:
            return (
                f"kill-window {window_id}: absence probe malformed row {token!r}"
            )
        present_rows.append((wid, sid, pid_s))
    matches = [row for row in present_rows if row[0] == window_id]
    if not matches:
        return None
    # Still addressable: distinguish foreign identity (refused kill) from
    # same-identity kill failure.
    for wid, sid, pid_s in matches:
        foreign = False
        if (
            expected_session_id is not None
            and sid != expected_session_id
        ):
            foreign = True
        if expected_server is not None:
            exp_pid = expected_server.get("tmux_server_pid")
            if not pid_s.isdigit() or int(pid_s) != exp_pid:
                foreign = True
        if foreign:
            detail = f"session {sid!r}"
            if expected_session_id is not None and sid != expected_session_id:
                detail = (
                    f"belongs to session {sid!r}, not WAL session "
                    f"{expected_session_id!r}"
                )
            return (
                f"kill-window {window_id}: {detail} — refuse foreign/restarted @N"
            )
    return f"kill-window {window_id}: still present after kill"


def _kill_window_allowing_intent_nonce_move(
    window_id: str,
    *,
    expected_session_id: str,
    intent_nonce: str | None,
    socket_path: str | None = None,
    expected_server: Mapping[str, Any] | None = None,
) -> str | None:
    """Kill *window_id*; relax session only when live intent-nonce proves ours.

    Session-scoped kill is preferred. When ``@N`` was moved to another session
    after bind+nonce publication, a foreign-session refuse is retried without
    the stale WAL ``$session`` constraint **only** if server-global nonce
    discovery still lists this exact ``@N`` under *intent_nonce* on the WAL
    server. Never relax on bare ``@N`` (leader-kill / foreign reuse).
    """
    err = _kill_window(
        window_id,
        socket_path=socket_path,
        expected_session_id=expected_session_id,
        expected_server=expected_server,
    )
    if err is None:
        return None
    if not isinstance(intent_nonce, str) or not intent_nonce:
        return err
    # Only the moved-session refuse shape is eligible for nonce-scoped retry.
    if "belongs to session" not in err and "not WAL session" not in err:
        return err
    n_status, n_matches, _detail = _discover_inside_windows_by_intent_nonce(
        session_id=expected_session_id,
        intent_nonce=intent_nonce,
        socket_path=socket_path,
    )
    if n_status not in ("found", "ambiguous") or window_id not in n_matches:
        return err
    return _kill_window(
        window_id,
        socket_path=socket_path,
        expected_session_id=None,
        expected_server=expected_server,
    )


def _absence_from_list_windows(
    window_id: str,
    listed: subprocess.CompletedProcess[str],
    *,
    allow_dead_server: bool = False,
) -> str | None:
    """Interpret list-windows -a stdout as absence proof for *window_id*."""
    stdout = listed.stdout or ""
    stderr = (listed.stderr or "").strip()
    if listed.returncode != 0 and not stdout.strip():
        if allow_dead_server:
            low = stderr.lower()
            if "no server" in low or "no current target" in low or "no such" in low:
                return None
        return (
            f"kill-window {window_id}: absence probe exit {listed.returncode}"
            + (f" {stderr}" if stderr else "")
        )
    present: list[str] = []
    for line in stdout.splitlines():
        token = line.strip()
        if not token:
            continue
        # Rich rows from atomic path may include tabs — take first field.
        wid = token.split("\t", 1)[0].strip()
        if _TMUX_WINDOW_ID.fullmatch(wid) is None:
            return (
                f"kill-window {window_id}: absence probe malformed row {token!r}"
            )
        present.append(wid)
    if window_id in present:
        return f"kill-window {window_id}: still present after kill"
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
    """Capture immutable invocation coordinates for the leader pane.

    Also stamps the ambient tmux **server** identity (socket + pid start-id)
    so WAL write and later ``new-window`` authorize against the same server
    that owned the snapshotted leader — not a later socket replacement.
    """
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
    server = _probe_tmux_server_identity()
    if server is None or _intent_tmux_server(server) is None:
        raise TmuxTeamError(
            "invoking pane identity snapshot refused: tmux server identity unavailable"
        )
    server = _intent_tmux_server(server)
    assert server is not None
    return {
        "session_name": session_name,
        "session_id": session_id,
        "window_id": window_id,
        "pane_id": got_pane,
        "pane_pid": pane_pid,
        "tmux_socket_path": server["tmux_socket_path"],
        "tmux_server_pid": server["tmux_server_pid"],
        "tmux_server_pid_start": server["tmux_server_pid_start"],
    }


def assert_invoking_identity(snapshot: Mapping[str, Any]) -> dict[str, str | int]:
    """Re-query the invoking pane and require an exact match to *snapshot*."""
    pane_id = str(snapshot.get("pane_id") or "")
    live = snapshot_invoking_identity(pane_id)
    for key in (
        "session_name",
        "session_id",
        "window_id",
        "pane_id",
        "pane_pid",
        "tmux_socket_path",
        "tmux_server_pid",
        "tmux_server_pid_start",
    ):
        if key not in snapshot:
            # Legacy snapshots without server fields: still require pane coords.
            if key.startswith("tmux_"):
                continue
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


def _restore_leader_focus(
    leader_pane: str, *, socket_path: str | None = None
) -> None:
    """Reselect the exact leader pane after focus-detached worker creation.

    ``select-pane -t %N`` alone sets that pane active *within its window* but
    does **not** flip session ``window_active`` when another window is current
    (tmux 3.x). Issue #104 / B1: operators must see the leader window, so we
    ``select-window -t %pane`` first (pane target switches to that window),
    then ``select-pane`` so the leader pane is active inside it.
    """
    if _TMUX_PANE_ID.fullmatch(leader_pane) is None:
        raise TmuxTeamError(f"invalid leader pane for focus restore {leader_pane!r}")
    selected_win = _tmux_run(
        ["select-window", "-t", leader_pane], socket_path=socket_path
    )
    if selected_win.returncode != 0:
        err = (selected_win.stderr or selected_win.stdout or "").strip()
        raise TmuxTeamError(
            f"failed to restore leader window for {leader_pane}: {err}"
        )
    selected = _tmux_run(
        ["select-pane", "-t", leader_pane], socket_path=socket_path
    )
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
) -> tuple[tuple[str, str], str, str, dict[str, Any]]:
    """Create detached session; return handle, window_id, first pane, server."""
    first_env = tmux_env_args(list(task.get("_env_pairs") or env_pairs))
    create = _tmux_run(
        [
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{session_name}\t#{session_id}\t#{window_id}\t#{pane_id}\t#{pid}\t#{socket_path}",
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
        len(parts) != 6
        or parts[0] != session
        or _TMUX_SESSION_ID.fullmatch(parts[1]) is None
        or _TMUX_WINDOW_ID.fullmatch(parts[2]) is None
        or _TMUX_PANE_ID.fullmatch(parts[3]) is None
        or not parts[4].isdigit()
        or not parts[5]
    ):
        cleanup = _cleanup_session((session, session))
        message = "tmux create did not return an exact session/window/pane/server handle"
        if cleanup:
            message += f"; {cleanup}"
        raise TmuxTeamError(message)
    try:
        server = _server_identity_from_create(
            pid=int(parts[4]), socket_path=parts[5].strip()
        )
    except TmuxTeamError as exc:
        cleanup = _cleanup_session((parts[0], parts[1]))
        message = str(exc)
        if cleanup:
            message += f"; {cleanup}"
        raise TmuxTeamError(message) from exc
    return (parts[0], parts[1]), parts[2], parts[3], server


def _discover_inside_windows_by_name(
    *,
    session_id: str,
    window_name: str,
    socket_path: str | None = None,
) -> tuple[str, list[str], str | None]:
    """Discover windows in *session_id* by exact name for orphan recovery.

    Uses ``list-windows -a`` (server-global) and filters rows to *session_id*
    + *window_name*. A successful global enumeration with no matching row is
    ``absent`` — including when the WAL session itself is gone (no rows for
    that ``$session``). Session-scoped ``list-windows -t`` cannot prove name
    absence when the session disappeared while the same tmux server remains
    alive; do not infer absence from ``can't find session`` text alone.

    Returns ``(status, window_ids, detail)`` where *status* is one of:
    - ``found`` — exactly one matching window id
    - ``absent`` — successful list with zero matches for the WAL session+name
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
                "-a",
                "-F",
                "#{window_id}\t#{window_name}\t#{session_id}",
            ],
            socket_path=socket_path,
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
    socket_path: str | None = None,
) -> str | None:
    """Find a unique window in *session_id* by exact name (orphan recovery).

    Returns the window id, ``None`` if absent, or raises on ambiguity/unknown.
    """
    status, matches, detail = _discover_inside_windows_by_name(
        session_id=session_id,
        window_name=window_name,
        socket_path=socket_path,
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


def _discover_inside_windows_by_intent_nonce(
    *,
    session_id: str,
    intent_nonce: str,
    socket_path: str | None = None,
) -> tuple[str, list[str], str | None]:
    """Discover windows on the scoped tmux server carrying create-time intent nonce.

    Uses ``list-windows -a`` (server-global) so a window moved to another session
    on the same WAL-scoped server still proves nonce presence and blocks WAL
    clear. *session_id* is validated for call-site contract only — matches are
    **not** filtered to that session.

    Returns ``(status, window_ids, detail)`` with the same status vocabulary as
    :func:`_discover_inside_windows_by_name`. The option survives rename, so
    this closes the create→bind crash window where the launch name is gone.
    """
    if _TMUX_SESSION_ID.fullmatch(session_id) is None:
        return (
            "unknown",
            [],
            f"intent nonce discovery requires session id, got {session_id!r}",
        )
    if not isinstance(intent_nonce, str) or not intent_nonce:
        return "unknown", [], "intent nonce discovery requires a non-empty nonce"
    try:
        listed = _tmux_run(
            [
                "list-windows",
                "-a",
                "-F",
                f"#{{window_id}}\t#{{{INTENT_NONCE_OPTION}}}\t#{{session_id}}",
            ],
            socket_path=socket_path,
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
        wid, nonce, _sid = parts
        if _TMUX_WINDOW_ID.fullmatch(wid) is None:
            continue
        valid_rows += 1
        if nonce == intent_nonce:
            matches.append(wid)
    if matches:
        if len(matches) > 1:
            return "ambiguous", matches, None
        return "found", matches, None
    raw_content = (listed.stdout or "").strip()
    if raw_content and valid_rows == 0:
        return "unknown", [], "list-windows returned malformed output"
    return "absent", [], None


def _kill_inside_windows_by_name(
    *,
    session_id: str,
    window_name: str,
    known_window_ids: Sequence[str] | None = None,
    socket_path: str | None = None,
    expected_server: Mapping[str, Any] | None = None,
    require_durable_window_id: bool = False,
    intent_nonce: str | None = None,
    nonce_published: bool = False,
) -> str | None:
    """Kill windows matching *window_name* in *session_id*; require absence proof.

    After kill attempts, re-runs a successful server-global ``list-windows -a``
    filtered to the WAL ``$session`` + transaction name and requires that name
    to be **absent** (a vanished WAL session with no matching rows counts as
    absent — do not leave the WAL uncleared solely because session-scoped
    listing is unavailable). When discovery previously returned exact window
    IDs — or the launch WAL already stamped durable ``known_window_ids`` —
    each ID must also be globally absent on the **WAL-scoped** tmux server.
    A rename can hide the launch name while ``@N`` still lives; name-only
    absence must never authorize WAL clear when a durable id is known.

    Unbound ``side_effect_started`` intents (*require_durable_window_id* with
    empty targets) must also resolve the create-time :data:`INTENT_NONCE_OPTION`:
    nonce absence alone is **not** proof until publication is durably
    acknowledged (*nonce_published*) or the nonce was positively observed on
    this sweep and then removed (happens-before). Pane-child self-stamp is
    asynchronous; treating pre-publication absence as "never created" would
    clear the WAL while a hook-renamed live window still exists.

    Bound intents with a known ``@N`` also run server-global nonce discovery:
    after bind+ACK a synchronous ``after-new-window`` move can leave ``@N`` in
    another session on the same WAL server. A live intent-nonce match on that
    durable id authorizes kill/absence under *expected_server* + exact ``@N``
    **without** the stale WAL ``$session`` constraint (still never leader-kill
    or foreign ``@N`` without nonce proof). When the ambient server does not
    match the WAL server identity, refuse foreign ``@N`` kills. Bare
    ``kill-window`` rc 0/1 is never treated as success by itself. Returns
    ``None`` only when absence is proven; otherwise returns an error detail.
    """
    if expected_server is not None:
        live = _probe_tmux_server_identity(socket_path=socket_path)
        if not _tmux_server_matches(expected_server, live):
            return (
                "tmux server identity mismatch — refuse foreign/restarted "
                f"@N kill for {window_name!r}"
            )
        if socket_path is None:
            socket_path = str(expected_server.get("tmux_socket_path") or "") or None
    durable_ids: list[str] = []
    for wid in known_window_ids or ():
        if not isinstance(wid, str) or _TMUX_WINDOW_ID.fullmatch(wid) is None:
            return f"refused kill with non-window known id {wid!r}"
        if wid not in durable_ids:
            durable_ids.append(wid)
    # Unbound side_effect recovery needs the intent nonce to distinguish
    # "never created" from "created then renamed before @N bind". Missing
    # nonce still attempts name/@N kills (orphan cleanup) but must not
    # authorize WAL clear on name-only absence — see final proof below.
    unbound_durable = bool(require_durable_window_id and not durable_ids)
    status, matches, detail = _discover_inside_windows_by_name(
        session_id=session_id,
        window_name=window_name,
        socket_path=socket_path,
    )
    nonce_matches: list[str] = []
    nonce_observed = False
    # Bound *and* unbound: scan nonce server-globally when present so a
    # post-bind session move remains recoverable (durable ∩ nonce).
    if isinstance(intent_nonce, str) and intent_nonce:
        n_status, nonce_matches, n_detail = _discover_inside_windows_by_intent_nonce(
            session_id=session_id,
            intent_nonce=intent_nonce,
            socket_path=socket_path,
        )
        if n_status in ("found", "ambiguous"):
            nonce_observed = True
        if n_status == "unknown":
            # Do not abort kills — final nonce absence proof refuses clear.
            nonce_matches = []
            if n_detail:
                errors_pre = f"intent nonce scan unknown: {n_detail}"
            else:
                errors_pre = "intent nonce scan unknown"
        else:
            errors_pre = None
    else:
        errors_pre = None
    errors: list[str] = []
    if errors_pre:
        errors.append(errors_pre)
    killed_ids: list[str] = []
    # Union discovery matches with WAL-stamped ids and intent-nonce hits so a
    # rename before the first name probe still targets the immutable @N.
    targets: list[str] = []
    for wid in list(matches) + durable_ids + list(nonce_matches):
        if wid not in targets:
            targets.append(wid)
    # Nonce-proven @N may live in another session on this server (including
    # durable_ids ∩ nonce_matches after bind+ACK + move). Gate those kills on
    # server+@N only — not the stale WAL $session. Name hits in the WAL
    # session keep the session constraint. Never relax without a live nonce
    # match (blocks foreign @N reuse / leader-kill attacks).
    nonce_relocated = {
        wid for wid in nonce_matches if wid not in matches
    }
    if targets:
        for wid in targets:
            err = _kill_window(
                wid,
                socket_path=socket_path,
                expected_session_id=(
                    None if wid in nonce_relocated else session_id
                ),
                expected_server=expected_server,
            )
            if err:
                errors.append(err)
            else:
                killed_ids.append(wid)
    # Always also target session:name so a discovery-unknown path still
    # attempts kill against the unique launch name we stamped. When a WAL
    # server identity is present, use the same shell if-shell+start-id gate
    # as @N kills — never a bare kill-window that could hit a replacement.
    name_target = f"{session_id}:{window_name}"
    if expected_server is not None:
        server = _intent_tmux_server(expected_server)
        if server is None:
            errors.append(
                f"kill-window -t {name_target!r}: invalid expected server"
            )
            by_name = None
        else:
            try:
                predicate = _tmux_identity_shell_predicate(
                    expected_server=server,
                    expected_session_id=session_id,
                )
            except TmuxTeamError as exc:
                errors.append(f"kill-window -t {name_target!r}: {exc}")
                by_name = None
            else:
                try:
                    by_name = _tmux_run(
                        [
                            "if-shell",
                            "-t",
                            name_target,
                            predicate,
                            f"kill-window -t {name_target}",
                            "",
                        ],
                        socket_path=socket_path,
                    )
                except OSError as exc:
                    errors.append(
                        f"kill-window -t {name_target!r} OSError: {exc}"
                    )
                    by_name = None
    else:
        try:
            by_name = _tmux_run(
                ["kill-window", "-t", name_target], socket_path=socket_path
            )
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
        session_id=session_id,
        window_name=window_name,
        socket_path=socket_path,
    )
    if proof_status == "absent":
        # Name gone is not enough when we knew immutable IDs — re-prove each
        # discovered or WAL-stamped @N is globally absent (rename / probe races).
        for wid in targets:
            id_err = _kill_window(
                wid,
                socket_path=socket_path,
                expected_session_id=(
                    None if wid in nonce_relocated else session_id
                ),
                expected_server=expected_server,
            )
            if id_err:
                return (
                    f"window id {wid} unproven absent after name "
                    f"{window_name!r} gone; {id_err}"
                    + (("; " + "; ".join(errors)) if errors else "")
                )
        if isinstance(intent_nonce, str) and intent_nonce:
            n_proof, n_left, n_detail = _discover_inside_windows_by_intent_nonce(
                session_id=session_id,
                intent_nonce=intent_nonce,
                socket_path=socket_path,
            )
            if n_proof == "unknown":
                return (
                    f"intent nonce absence unproven for {window_name!r}: "
                    + (n_detail or "unknown")
                    + (("; " + "; ".join(errors)) if errors else "")
                )
            if n_proof in ("found", "ambiguous"):
                return (
                    f"intent nonce still present after name {window_name!r} "
                    f"gone (ids={n_left}); refuse WAL clear"
                    + (("; " + "; ".join(errors)) if errors else "")
                )
            if unbound_durable:
                # Nonce absent: only authorize clear when publication completed
                # (durable ack) or we positively observed then removed it on this
                # sweep. Scanning twice without that happens-before is insufficient
                # — the pane-child stamp may still be pending after a rename.
                if not nonce_published and not nonce_observed:
                    return (
                        "unbound side_effect WAL: create-time nonce publication "
                        "unacknowledged — refuse clear on nonce absence alone"
                        + (("; " + "; ".join(errors)) if errors else "")
                    )
        elif unbound_durable:
            return (
                "unbound side_effect WAL requires intent nonce scan — "
                "refuse name-only clear"
                + (("; " + "; ".join(errors)) if errors else "")
            )
        # Final PID+start revalidation — absence probes are separate clients;
        # a mid-proof socket swap to a empty server B must not authorize clear.
        if expected_server is not None:
            live = _probe_tmux_server_identity(socket_path=socket_path)
            if not _tmux_server_matches(expected_server, live):
                return (
                    "tmux server identity mismatch after absence proof — "
                    f"refuse WAL clear for {window_name!r}"
                    + (("; " + "; ".join(errors)) if errors else "")
                )
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


def _parse_new_window_create_handle(
    stdout: str,
    *,
    expected_session_id: str,
) -> tuple[str, str, str, int] | None:
    """Parse ``new-window -P`` handle from client stdout (first valid row).

    Returns ``(window_id, pane_id, session_id, server_pid)`` or ``None``.
    Tolerates trailing noise so a later failed queue item (hook-renamed
    name-targeted stamp) does not hide a successful create ``@N``.
    """
    for line in (stdout or "").splitlines():
        parts = line.strip().split("\t")
        if (
            len(parts) == 4
            and _TMUX_WINDOW_ID.fullmatch(parts[0]) is not None
            and _TMUX_PANE_ID.fullmatch(parts[1]) is not None
            and parts[2] == expected_session_id
            and parts[3].isdigit()
        ):
            return parts[0], parts[1], parts[2], int(parts[3])
    return None


def _pane_command_with_intent_nonce_stamp(
    pane_command: str,
    intent_nonce: str,
    *,
    stamp_window: bool = True,
) -> str:
    """Prefix *pane_command* so the new pane self-stamps intent nonce on start.

    Defense in depth beside the post-handle exact ``@N``/``%pane`` stamp:
    runs inside the created pane (window object identity), so an
    ``after-new-window`` rename/move cannot defeat the stamp the way a
    targetless queued ``set-option`` (or ``$session:name`` target) can.
    The pane child is asynchronous — recovery must not treat nonce absence
    as proof until publication is acknowledged or positively observed.

    *stamp_window* must be False for same_window (#96): the shared leader
    window must never receive the Team intent nonce.
    """
    nq = shlex.quote(intent_nonce)
    opt = INTENT_NONCE_OPTION
    if stamp_window:
        return (
            f"tmux set-option -wq {opt} {nq} && "
            f"tmux set-option -pq {opt} {nq} && "
            f"exec /bin/sh -c {shlex.quote(pane_command)}"
        )
    return (
        f"tmux set-option -pq {opt} {nq} && "
        f"exec /bin/sh -c {shlex.quote(pane_command)}"
    )


def _stamp_intent_nonce_on_handles(
    *,
    window_id: str,
    pane_id: str,
    intent_nonce: str,
    socket_path: str | None,
    expected_server: Mapping[str, Any] | None,
    expected_session_id: str | None = None,
) -> bool:
    """Stamp intent nonce onto immutable ``@N`` / ``%pane``; return success.

    Targets the create ``-P`` handles directly so an ``after-new-window``
    ``move-window`` cannot retarget publication onto the source leader the way
    a targetless create-queue ``set-option`` can. When *expected_session_id*
    is omitted, the identity gate is server+``@N`` only — required after a
    session move (P2-1b). Failures return False (no raise).
    """
    win_argv = [
        "set-option",
        "-w",
        "-t",
        window_id,
        INTENT_NONCE_OPTION,
        intent_nonce,
    ]
    pane_argv = [
        "set-option",
        "-p",
        "-t",
        pane_id,
        INTENT_NONCE_OPTION,
        intent_nonce,
    ]
    try:
        if expected_server is not None:
            server = _intent_tmux_server(expected_server)
            if server is None:
                return False
            win = _tmux_run_if_identity(
                win_argv,
                target=window_id,
                expected_server=server,
                socket_path=socket_path,
                window_id=window_id,
                expected_session_id=expected_session_id,
            )
            pane = _tmux_run_if_identity(
                pane_argv,
                target=pane_id,
                expected_server=server,
                socket_path=socket_path,
                window_id=window_id,
                expected_session_id=expected_session_id,
            )
            return win.returncode == 0 and pane.returncode == 0
        stamped = _tmux_run(
            [*win_argv, ";", *pane_argv],
            socket_path=socket_path,
        )
        return stamped.returncode == 0
    except (OSError, TmuxTeamError):
        return False


def _readback_intent_nonce_on_handles(
    *,
    window_id: str,
    pane_id: str,
    intent_nonce: str,
    socket_path: str | None,
) -> bool:
    """True only when both ``@N`` and ``%pane`` carry *intent_nonce*.

    Proves publication hit the created worker handles — never the ambient
    client current target. A valid ``new-window -P`` string alone is
    insufficient (create-queue targetless stamps may have hit the leader).
    """
    try:
        win = _tmux_run(
            [
                "show-options",
                "-wv",
                "-t",
                window_id,
                INTENT_NONCE_OPTION,
            ],
            socket_path=socket_path,
        )
        pane = _tmux_run(
            [
                "show-options",
                "-pv",
                "-t",
                pane_id,
                INTENT_NONCE_OPTION,
            ],
            socket_path=socket_path,
        )
    except OSError:
        return False
    if win.returncode != 0 or pane.returncode != 0:
        return False
    win_val = (win.stdout or "").strip()
    pane_val = (pane.stdout or "").strip()
    return win_val == intent_nonce and pane_val == intent_nonce


def _publish_intent_nonce_on_created_handles(
    *,
    window_id: str,
    pane_id: str,
    intent_nonce: str,
    socket_path: str | None,
    expected_server: Mapping[str, Any] | None,
) -> bool:
    """Stamp + readback on exact create handles; True only when proven.

    Session gate is omitted so an ``after-new-window`` session move still
    stamps the moved worker ``@N`` (server-global), never the source leader.
    """
    if not _stamp_intent_nonce_on_handles(
        window_id=window_id,
        pane_id=pane_id,
        intent_nonce=intent_nonce,
        socket_path=socket_path,
        expected_server=expected_server,
        expected_session_id=None,
    ):
        return False
    return _readback_intent_nonce_on_handles(
        window_id=window_id,
        pane_id=pane_id,
        intent_nonce=intent_nonce,
        socket_path=socket_path,
    )


def _publish_intent_nonce_on_pane(
    *,
    pane_id: str,
    intent_nonce: str,
    socket_path: str | None,
    expected_server: Mapping[str, Any] | None,
    expected_session_id: str | None = None,
    expected_window_id: str | None = None,
) -> bool:
    """Pane-only nonce stamp+readback for same_window (#96).

    Never stamps the shared leader window option — that would poison the
    invoking window with the Team intent nonce.
    """
    pane_argv = [
        "set-option",
        "-p",
        "-t",
        pane_id,
        INTENT_NONCE_OPTION,
        intent_nonce,
    ]
    try:
        if expected_server is not None:
            server = _intent_tmux_server(expected_server)
            if server is None:
                return False
            stamped = _tmux_run_if_identity(
                pane_argv,
                target=pane_id,
                expected_server=server,
                socket_path=socket_path,
                window_id=expected_window_id,
                expected_session_id=expected_session_id,
                pane_id=pane_id,
            )
        else:
            stamped = _tmux_run(pane_argv, socket_path=socket_path)
        if stamped.returncode != 0:
            return False
        readback = _tmux_run(
            [
                "show-options",
                "-pqv",
                "-t",
                pane_id,
                INTENT_NONCE_OPTION,
            ],
            socket_path=socket_path,
        )
        return (
            readback.returncode == 0
            and (readback.stdout or "").strip() == intent_nonce
        )
    except (OSError, TmuxTeamError):
        return False


def _launch_first_inside(
    *,
    task: dict[str, Any],
    env_pairs: list[tuple[str, str]],
    window_name: str,
    target_window: str,
    expected_session_id: str,
    intent_path: Path | None = None,
    publish_created: Callable[[str, str], None] | None = None,
    socket_path: str | None = None,
    expected_server: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """Create a new window beside *target_window* (``@N``); return (window_id, pane_id).

    Uses ``-d`` so the client stays on the invoking leader pane. Target must be
    a window id — tmux rejects ``new-window -a -t %pane`` (CMD_FIND_WINDOW).

    When ``new-window`` returns a valid handle, *publish_created* (if any) runs
    **before** the WAL bind so a synchronous bind failure still leaves the
    outer ``window_id`` set for exact-ID cleanup. The launch intent WAL is then
    stamped with that immutable ``@window_id`` before returning. When rc=0 but
    stdout is empty/malformed (or the reply channel raises OSError), always
    attempt discover-by-name, bind any found id, then kill with absence proof
    before raising so the caller never leaves an unrecepted orphan silently OK.

    *socket_path* / *expected_server* pin create + orphan cleanup to the WAL
    tmux server so a restarted ambient server cannot receive the side effect.
    Create is authorized against the full WAL identity (pid **and** start-id)
    before mutation; successful handles must re-verify that identity before bind.

    Create-time intent nonce is published via exact ``@N``/``%pane`` stamp +
    readback after the ``-P`` handle is parsed (never via targetless create-
    queue ``set-option``, which an ``after-new-window`` ``move-window`` can
    retarget onto the source leader). The pane command also self-stamps as
    defense in depth. ``nonce_published`` is acked **only** after durable
    ``@N`` bind and proven handle-targeted publication — a bare ``@N`` string
    in ``-P`` stdout is not sufficient. A valid ``@N`` in ``-P`` stdout is
    consumed even when a later queue item makes the overall client non-zero;
    that path binds WAL and must not unmark as "never created".
    """
    if _TMUX_WINDOW_ID.fullmatch(target_window) is None:
        raise TmuxTeamError(
            f"new-window target must be a window id (@N), got {target_window!r}"
        )
    if _TMUX_SESSION_ID.fullmatch(expected_session_id) is None:
        raise TmuxTeamError(
            f"new-window requires expected session id, got {expected_session_id!r}"
        )
    server: dict[str, Any] | None = None
    if expected_server is not None:
        server = _require_tmux_server(
            expected_server,
            socket_path=socket_path,
            action="new-window",
        )
        if socket_path is None:
            socket_path = str(server["tmux_socket_path"])
    first_env = tmux_env_args(list(task.get("_env_pairs") or env_pairs))
    # Mark immediately before the client mutation so a pre-call crash cannot
    # leave side_effect_started without attempting create. Synchronous rc!=0
    # with name absence unmarks (see below) only when no @N was emitted.
    intent_nonce: str | None = None
    if intent_path is not None:
        try:
            intent_raw = json.loads(Path(intent_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TmuxTeamError(
                f"launch intent unreadable before new-window: {exc}"
            ) from exc
        if not isinstance(intent_raw, dict):
            raise TmuxTeamError("launch intent must be an object before new-window")
        intent_nonce = _intent_nonce_value(intent_raw)
        if not intent_nonce:
            raise TmuxTeamError(
                "launch intent missing nonce before new-window — refuse create"
            )
        mark_team_launch_intent_side_effect(intent_path)
    pane_command = str(task["pane_command"])
    if intent_nonce is not None:
        pane_command = _pane_command_with_intent_nonce_stamp(
            pane_command, intent_nonce
        )
    # Always detach (-d): stay on the leader. Nonce publication uses exact
    # @N/%pane after -P parse — never targetless set-option in this queue
    # (after-new-window move-window invalidates queue current-target and
    # would retarget targetless stamps onto the source leader).
    nw_argv = [
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{window_id}\t#{pane_id}\t#{session_id}\t#{pid}",
        "-a",
        "-t",
        target_window,
        "-n",
        window_name,
        "-c",
        str(task["worktree"]),
        *first_env,
        pane_command,
    ]
    create_argv: list[str] = list(nw_argv)
    # if-shell success is one tmux command-string; join subcommands with
    # bare " ; " — _tmux_join_command would quote ";" and break the list.
    if_shell_success = _tmux_join_command(nw_argv)
    try:
        if server is not None:
            # Atomic server-side gate: new-window only runs when the leader
            # window still belongs to the WAL server (pid + start-id). Intent
            # nonce is stamped via exact @N/%pane after handle acceptance —
            # not via targetless create-queue set-option or $session:name.
            predicate = _tmux_identity_shell_predicate(
                expected_server=server,
                window_id=target_window,
                expected_session_id=expected_session_id,
            )
            create = _tmux_run(
                [
                    "if-shell",
                    "-t",
                    target_window,
                    predicate,
                    if_shell_success,
                    "",
                ],
                socket_path=socket_path,
            )
        else:
            create = _tmux_run(create_argv, socket_path=socket_path)
    except OSError as exc:
        # Creation itself failed — still name cleanup in case the window was
        # created before the client lost the reply channel. Prefer discover →
        # bind → ID kill so a rename cannot authorize WAL clear.
        message = f"tmux new-window OSError: {exc}"
        cleanup = _cleanup_unreplied_inside_window(
            session_id=expected_session_id,
            window_name=window_name,
            intent_path=intent_path,
        )
        if cleanup:
            message = f"{message}; orphan cleanup: {cleanup}"
        raise TmuxTeamError(message) from exc

    handle = _parse_new_window_create_handle(
        create.stdout or "",
        expected_session_id=expected_session_id,
    )
    if handle is None and create.returncode != 0:
        err = (create.stderr or create.stdout or "").strip()
        # Synchronous failure with no @N emitted: prove whether a window was
        # created. If the launch name is absent and no @N is bound, unmark
        # side_effect_started so the false-positive mark cannot permanently
        # wedge project launches.
        status, matches, _detail = _discover_inside_windows_by_name(
            session_id=expected_session_id,
            window_name=window_name,
            socket_path=socket_path,
        )
        if status == "found" and matches:
            cleanup = _cleanup_unreplied_inside_window(
                session_id=expected_session_id,
                window_name=window_name,
                intent_path=intent_path,
            )
            message = f"failed to create team window in current session: {err}"
            if cleanup:
                message = f"{message}; orphan cleanup: {cleanup}"
            raise TmuxTeamError(message)
        if status == "absent" and intent_path is not None:
            try:
                unmark_team_launch_intent_side_effect(intent_path)
            except TmuxTeamError:
                pass
        raise TmuxTeamError(
            f"failed to create team window in current session: {err}"
        )

    if handle is not None:
        window_id, pane_id, _sid, create_pid = handle
        if server is not None:
            if create_pid != server["tmux_server_pid"]:
                cleanup = _cleanup_unreplied_inside_window(
                    session_id=expected_session_id,
                    window_name=window_name,
                    intent_path=intent_path,
                )
                message = (
                    "new-window returned foreign/restarted server pid — refuse bind"
                )
                if cleanup:
                    message = f"{message}; orphan cleanup: {cleanup}"
                raise TmuxTeamError(message)
            # Full identity recheck (pid start-id) before accepting the handle.
            try:
                _require_tmux_server(
                    server,
                    socket_path=socket_path,
                    action="new-window readback",
                )
            except TmuxTeamError as exc:
                cleanup = _cleanup_unreplied_inside_window(
                    session_id=expected_session_id,
                    window_name=window_name,
                    intent_path=intent_path,
                )
                message = str(exc)
                if cleanup:
                    message = f"{message}; orphan cleanup: {cleanup}"
                raise TmuxTeamError(message) from exc
        # Publish handles to the caller *before* WAL bind so a bind raise
        # cannot lose the exact @N for exception cleanup.
        if publish_created is not None:
            publish_created(window_id, pane_id)
        if intent_path is not None:
            # Durable @N *before* nonce ACK — closes the crash window where
            # nonce_published=true but window_id is unbound (recovery would
            # otherwise act on a false create-queue leader stamp). Bind even
            # when overall client rc!=0 (later stamp noise).
            bind_team_launch_intent_window_id(intent_path, window_id)
        if intent_path is not None and intent_nonce is not None:
            # Exact-handle stamp+readback only. Never ACK on -P parse alone.
            if _publish_intent_nonce_on_created_handles(
                window_id=window_id,
                pane_id=pane_id,
                intent_nonce=intent_nonce,
                socket_path=socket_path,
                expected_server=server,
            ):
                try:
                    ack_team_launch_intent_nonce_published(intent_path)
                except TmuxTeamError:
                    pass
        return window_id, pane_id

    # Side effect succeeded; result publication failed — discover/bind/kill.
    # Also covers if-shell predicate-false (empty stdout, rc 0).
    cleanup = _cleanup_unreplied_inside_window(
        session_id=expected_session_id,
        window_name=window_name,
        intent_path=intent_path,
    )
    if cleanup is None:
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


def _cleanup_unreplied_inside_window(
    *,
    session_id: str,
    window_name: str,
    intent_path: Path | None,
) -> str | None:
    """Discover → bind durable @N → kill with ID absence proof; clear WAL iff proven.

    Used when ``new-window`` may have created a window but the reply handle is
    missing. Prefer binding any discovered id onto the intent WAL before kill
    so a later crash+rename cannot clear on name-only absence.
    """
    known_ids: list[str] = []
    expected_server: dict[str, Any] | None = None
    socket_path: str | None = None
    require_durable = True
    intent_nonce: str | None = None
    nonce_published = False
    if intent_path is not None:
        try:
            raw = json.loads(Path(intent_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict):
            known_ids.extend(_intent_known_window_ids(raw))
            expected_server = _intent_tmux_server(raw)
            if expected_server is not None:
                socket_path = str(expected_server["tmux_socket_path"])
            require_durable = _intent_requires_durable_window_id(raw)
            intent_nonce = _intent_nonce_value(raw)
            nonce_published = _intent_nonce_published(raw)
            if expected_server is not None:
                live = _probe_tmux_server_identity(socket_path=socket_path)
                if not _tmux_server_matches(expected_server, live):
                    return (
                        "tmux server identity mismatch — refuse foreign "
                        f"@N orphan cleanup for {window_name!r}"
                    )
    discovered: str | None = None
    try:
        discovered = _discover_inside_window_by_name(
            session_id=session_id,
            window_name=window_name,
            socket_path=socket_path,
        )
    except TmuxTeamError as exc:
        # Ambiguous / unknown discovery — still attempt kill-by-name with any
        # already-bound ids; surface the discovery error if absence unproven.
        cleanup = _kill_inside_windows_by_name(
            session_id=session_id,
            window_name=window_name,
            known_window_ids=known_ids,
            socket_path=socket_path,
            expected_server=expected_server,
            require_durable_window_id=require_durable,
            intent_nonce=intent_nonce,
            nonce_published=nonce_published,
        )
        if cleanup is None:
            clear_team_launch_intent(intent_path)
            return None
        return f"{exc}; {cleanup}"
    if discovered is not None:
        if discovered not in known_ids:
            known_ids.append(discovered)
        if intent_path is not None:
            try:
                bind_team_launch_intent_window_id(intent_path, discovered)
            except TmuxTeamError:
                # Kill with in-memory id even if WAL stamp fails.
                pass
    cleanup = _kill_inside_windows_by_name(
        session_id=session_id,
        window_name=window_name,
        known_window_ids=known_ids,
        socket_path=socket_path,
        expected_server=expected_server,
        require_durable_window_id=require_durable,
        intent_nonce=intent_nonce,
        nonce_published=nonce_published,
    )
    if cleanup is None:
        clear_team_launch_intent(intent_path)
    return cleanup


def _split_remaining(
    *,
    target: str,
    tasks: Sequence[dict[str, Any]],
    env_pairs: list[tuple[str, str]],
    socket_path: str | None = None,
    expected_server: Mapping[str, Any] | None = None,
    expected_session_id: str | None = None,
    expected_window_id: str | None = None,
    expected_pane_id: str | None = None,
    vertical: bool = False,
) -> list[str]:
    """Split remaining tasks into ``target`` (session/window/pane id).

    Uses ``-d`` so splits do not steal client focus from the leader pane.
    When *expected_server* is set, each ``split-window`` runs under a PID+start
    ``if-shell`` gate so a replacement server on the same socket cannot receive
    the worker command before Python postcheck. *vertical* forces ``-v`` for
    same_window worker-stack splits (#96).
    """
    created: list[str] = []
    server = (
        _intent_tmux_server(expected_server) if expected_server is not None else None
    )
    for task in tasks:
        task_env = tmux_env_args(list(task.get("_env_pairs") or env_pairs))
        split_argv = [
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
        if vertical:
            split_argv.insert(1, "-v")
        if server is not None:
            split = _tmux_run_if_identity(
                split_argv,
                target=target,
                expected_server=server,
                socket_path=socket_path,
                window_id=expected_window_id,
                expected_session_id=expected_session_id,
                pane_id=expected_pane_id,
            )
        else:
            split = _tmux_run(split_argv, socket_path=socket_path)
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


def _split_worker_pane_gated(
    *,
    target: str,
    task: dict[str, Any],
    env_pairs: list[tuple[str, str]],
    horizontal: bool,
    socket_path: str | None,
    expected_server: Mapping[str, Any] | None,
    expected_session_id: str,
    expected_window_id: str,
    expected_pane_id: str,
    intent_path: Path | None = None,
    intent_nonce: str | None = None,
) -> str:
    """Identity-gated focus-detached split against an exact pane target."""
    task_env = tmux_env_args(list(task.get("_env_pairs") or env_pairs))
    pane_command = str(task["pane_command"])
    if intent_nonce is not None:
        pane_command = _pane_command_with_intent_nonce_stamp(
            pane_command, intent_nonce, stamp_window=False
        )
    split_argv = [
        "split-window",
        "-h" if horizontal else "-v",
        "-d",
        "-P",
        "-F",
        "#{pane_id}",
        "-t",
        target,
        "-c",
        str(task["worktree"]),
        *task_env,
        pane_command,
    ]
    server = (
        _intent_tmux_server(expected_server) if expected_server is not None else None
    )
    if server is None:
        raise TmuxTeamError("same_window split refused: tmux server identity required")
    if intent_path is not None:
        mark_team_launch_intent_side_effect(intent_path)
    split = _tmux_run_if_identity(
        split_argv,
        target=target,
        expected_server=server,
        socket_path=socket_path,
        window_id=expected_window_id,
        expected_session_id=expected_session_id,
        pane_id=expected_pane_id,
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
    return pane_id


def _verify_worker_pane_membership(
    *,
    pane_id: str,
    leader_pane: str,
    expected_session_id: str,
    expected_window_id: str,
    expected_server: Mapping[str, Any] | None,
    socket_path: str | None,
) -> None:
    if pane_id == leader_pane:
        raise TmuxTeamError("refusing to overwrite leader pane with worker")
    probe = _tmux_run(
        [
            "display-message",
            "-p",
            "-t",
            pane_id,
            "#{pane_id}\t#{session_id}\t#{window_id}\t#{pid}",
        ],
        socket_path=socket_path,
    )
    parts = (probe.stdout or "").strip().split("\t")
    server = (
        _intent_tmux_server(expected_server) if expected_server is not None else None
    )
    if (
        probe.returncode != 0
        or len(parts) != 4
        or parts[0] != pane_id
        or parts[1] != expected_session_id
        or parts[2] != expected_window_id
        or (
            server is not None
            and (
                not parts[3].isdigit()
                or int(parts[3]) != server["tmux_server_pid"]
            )
        )
    ):
        raise TmuxTeamError(
            "worker pane left the invoking session/window before commit "
            f"(pane={pane_id!r}, expected session_id={expected_session_id!r} "
            f"window_id={expected_window_id!r})"
        )


def _apply_same_window_layout(
    *,
    window_id: str,
    leader_pane: str,
    worker_count: int,
    socket_path: str | None,
) -> int:
    """Select leader, apply main-vertical with clamped width; return width used."""
    from omg_cli.team.topology import clamp_main_vertical_leader_width

    width_probe = _tmux_run(
        ["display-message", "-p", "-t", window_id, "#{window_width}"],
        socket_path=socket_path,
    )
    try:
        window_width = int((width_probe.stdout or "").strip())
    except ValueError:
        window_width = 0
    if width_probe.returncode != 0 or window_width <= 0:
        raise TmuxTeamError("failed to read window width for main-vertical layout")
    leader_width = clamp_main_vertical_leader_width(
        window_width, worker_count=worker_count
    )
    # Select leader first so main-vertical treats it as the main pane.
    _restore_leader_focus(leader_pane, socket_path=socket_path)
    set_w = _tmux_run(
        [
            "set-window-option",
            "-t",
            window_id,
            "main-pane-width",
            str(leader_width),
        ],
        socket_path=socket_path,
    )
    if set_w.returncode != 0:
        err = (set_w.stderr or set_w.stdout or "").strip()
        raise TmuxTeamError(f"failed to set main-pane-width: {err}")
    layout = _tmux_run(
        ["select-layout", "-t", window_id, "main-vertical"],
        socket_path=socket_path,
    )
    if layout.returncode != 0:
        err = (layout.stderr or layout.stdout or "").strip()
        raise TmuxTeamError(f"failed to apply main-vertical layout: {err}")
    return leader_width


def _assert_leader_postconditions(
    *,
    snap: Mapping[str, Any],
    socket_path: str | None,
) -> dict[str, str | int]:
    """Re-select leader and prove pane/pid/session/window + session-visible."""
    leader_pane = str(snap["pane_id"])
    _restore_leader_focus(leader_pane, socket_path=socket_path)
    probe = _tmux_run(
        [
            "display-message",
            "-p",
            "-t",
            leader_pane,
            "#{session_id}\t#{window_id}\t#{pane_id}\t#{pane_pid}\t"
            "#{pane_active}\t#{window_active}",
        ],
        socket_path=socket_path,
    )
    parts = (probe.stdout or "").strip().split("\t")
    if probe.returncode != 0 or len(parts) != 6:
        raise TmuxTeamError("leader postcondition probe failed")
    session_id, window_id, pane_id, pid_s, active, window_active = parts
    try:
        pane_pid = int(pid_s)
    except ValueError as exc:
        raise TmuxTeamError(f"invalid leader pane pid {pid_s!r}") from exc
    if (
        session_id != str(snap["session_id"])
        or window_id != str(snap["window_id"])
        or pane_id != leader_pane
        or pane_pid != int(snap["pane_pid"])
        or active != "1"
        or window_active != "1"
    ):
        raise TmuxTeamError(
            "leader identity/selection postcondition failed "
            f"(session={session_id!r} window={window_id!r} pane={pane_id!r} "
            f"pid={pane_pid!r} active={active!r} window_active={window_active!r})"
        )
    return {
        "session_id": session_id,
        "window_id": window_id,
        "pane_id": pane_id,
        "pane_pid": pane_pid,
        "pane_active": 1,
        "window_active": 1,
    }


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
    view_mode: str | None = None,
) -> tuple[str, str]:
    """Create worker panes; return ``(session_name, session_id)``.

    Mutates each task with ``pane_id``. Sets ``_tmux_launch`` on ``tasks[0]``
    describing attach policy including ``view_mode`` (#96).
    """
    from omg_cli.team.topology import (
        VIEW_MODE_DEDICATED_WINDOW,
        VIEW_MODE_DETACHED_SESSION,
        VIEW_MODE_SAME_WINDOW,
        VIEW_MODES,
    )

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

    if mode == "detached":
        if view_mode is not None and view_mode != VIEW_MODE_DETACHED_SESSION:
            raise TmuxTeamError(
                f"detached launch refuses view_mode {view_mode!r}"
            )
        return _create_detached(session=session, tasks=tasks, env_pairs=env_pairs)

    resolved = view_mode or VIEW_MODE_SAME_WINDOW
    if resolved not in (VIEW_MODE_SAME_WINDOW, VIEW_MODE_DEDICATED_WINDOW):
        raise TmuxTeamError(f"unsupported inside view_mode {resolved!r}")
    if resolved not in VIEW_MODES:
        raise TmuxTeamError(f"unsupported view_mode {resolved!r}")
    return _create_inside(
        session=session,
        tasks=tasks,
        env_pairs=env_pairs,
        env=env,
        invoking_pane=invoking_pane,
        root=root,
        run_id=run_id,
        view_mode=resolved,
    )


def _stamp_launch_meta(
    tasks: list[dict[str, Any]],
    *,
    attach_mode: str,
    session_owned: bool,
    leader_pane_id: str | None,
    window_id: str | None,
    attach_hint: str | None,
    session_id: str | None = None,
    tmux_server: Mapping[str, Any] | None = None,
    view_mode: str | None = None,
    layout: str | None = None,
    leader_pane_pid: int | None = None,
) -> None:
    meta: dict[str, Any] = {
        "attach_mode": attach_mode,
        "session_owned": session_owned,
        "leader_pane_id": leader_pane_id,
        "window_id": window_id,
        "attach_hint": attach_hint,
    }
    if isinstance(session_id, str) and _TMUX_SESSION_ID.fullmatch(session_id):
        meta["session_id"] = session_id
    server = _intent_tmux_server(tmux_server) if tmux_server is not None else None
    if server is not None:
        meta["tmux_socket_path"] = server["tmux_socket_path"]
        meta["tmux_server_pid"] = server["tmux_server_pid"]
        meta["tmux_server_pid_start"] = server["tmux_server_pid_start"]
    if view_mode is not None:
        meta["view_mode"] = view_mode
    if layout is not None:
        meta["layout"] = layout
    if isinstance(leader_pane_pid, int) and not isinstance(leader_pane_pid, bool):
        meta["leader_pane_pid"] = leader_pane_pid
    tasks[0]["_tmux_launch"] = meta


def _create_detached(
    *,
    session: str,
    tasks: list[dict[str, Any]],
    env_pairs: list[tuple[str, str]],
) -> tuple[str, str]:
    from omg_cli.team.topology import LAYOUT_TILED, VIEW_MODE_DETACHED_SESSION

    handle, window_id, first_pane, server = _launch_first_detached(
        session=session, task=tasks[0], env_pairs=env_pairs
    )
    sock = str(server["tmux_socket_path"])
    created_panes = [first_pane]
    try:
        created_panes.extend(
            _split_remaining(
                target=handle[1],
                tasks=tasks[1:],
                env_pairs=env_pairs,
                socket_path=sock,
                expected_server=server,
                expected_session_id=handle[1],
            )
        )
        if len(created_panes) != len(tasks):
            raise TmuxTeamError("pane count mismatch after detached split")
        _require_tmux_server(
            server, socket_path=sock, action="detached create commit"
        )
        for task, pane_id in zip(tasks, created_panes, strict=True):
            task["pane_id"] = pane_id
        layout = _tmux_run(
            ["select-layout", "-t", handle[1], "tiled"], socket_path=sock
        )
        if layout.returncode != 0:
            raise TmuxTeamError("failed to apply tiled layout")
        option = _tmux_run(
            ["set-option", "-t", handle[1], "mouse", "on"], socket_path=sock
        )
        if option.returncode != 0:
            raise TmuxTeamError("failed to configure created tmux session")
        _stamp_launch_meta(
            tasks,
            attach_mode="detached",
            session_owned=True,
            leader_pane_id=None,
            window_id=window_id,
            attach_hint=f"tmux attach -t {handle[0]}",
            session_id=handle[1],
            tmux_server=server,
            view_mode=VIEW_MODE_DETACHED_SESSION,
            layout=LAYOUT_TILED,
        )
    except (TmuxTeamError, OSError) as exc:
        cleanup = _cleanup_session(
            handle, socket_path=sock, expected_server=server
        )
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
    view_mode: str = "same_window",
) -> tuple[str, str]:
    """Dispatch inside topology: same_window (default) or dedicated_window."""
    from omg_cli.team.topology import (
        VIEW_MODE_DEDICATED_WINDOW,
        VIEW_MODE_SAME_WINDOW,
    )

    if view_mode == VIEW_MODE_SAME_WINDOW:
        return _create_inside_same_window(
            session=session,
            tasks=tasks,
            env_pairs=env_pairs,
            env=env,
            invoking_pane=invoking_pane,
            root=root,
            run_id=run_id,
        )
    if view_mode == VIEW_MODE_DEDICATED_WINDOW:
        return _create_inside_dedicated_window(
            session=session,
            tasks=tasks,
            env_pairs=env_pairs,
            env=env,
            invoking_pane=invoking_pane,
            root=root,
            run_id=run_id,
        )
    raise TmuxTeamError(f"unsupported view_mode {view_mode!r}")


def _create_inside_same_window(
    *,
    session: str,
    tasks: list[dict[str, Any]],
    env_pairs: list[tuple[str, str]],
    env: Mapping[str, str] | None = None,
    invoking_pane: str | None = None,
    root: Path | str | None = None,
    run_id: str | None = None,
) -> tuple[str, str]:
    """Split workers into the invoking leader window (#96).

    First worker: ``split-window -h -d`` against the leader pane.
    Remaining: ``split-window -v -d`` against the worker-stack pane.
    Never calls kill-window / kill-session on failure.
    """
    from omg_cli.team.topology import (
        LAYOUT_MAIN_VERTICAL,
        VIEW_MODE_SAME_WINDOW,
    )

    leader_pane = resolve_invoking_pane(pane=invoking_pane, env=env, require_exact=True)
    snap = snapshot_invoking_identity(leader_pane)
    live_name = str(snap["session_name"])
    live_id = str(snap["session_id"])
    leader_window = str(snap["window_id"])
    leader_pid = int(snap["pane_pid"])
    if leader_pid <= 0:
        raise TmuxTeamError(
            f"same_window launch refused: non-positive leader pane pid {leader_pid}"
        )
    intent_nonce = secrets.token_hex(8)
    # Synthetic name for WAL keying only — must never match a real window.
    window_name = f"omg-same-{intent_nonce}"
    created_panes: list[str] = []
    intent_path: Path | None = None
    intent_server = _intent_tmux_server(snap)
    sock = (
        str(intent_server["tmux_socket_path"]) if intent_server is not None else None
    )
    stack_pane: str | None = None
    try:
        assert_invoking_identity(snap)
        if intent_server is None:
            raise TmuxTeamError(
                "same_window launch refused: invoking server identity unavailable"
            )
        if root is not None and run_id is not None:
            intent_path = write_team_launch_intent(
                root,
                run_id=str(run_id),
                session_id=live_id,
                window_name=window_name,
                nonce=intent_nonce,
                tmux_server=intent_server,
                view_mode=VIEW_MODE_SAME_WINDOW,
                leader_pane_id=leader_pane,
                leader_window_id=leader_window,
            )

        # First worker: horizontal split from exact leader pane.
        first_pane = _split_worker_pane_gated(
            target=leader_pane,
            task=tasks[0],
            env_pairs=env_pairs,
            horizontal=True,
            socket_path=sock,
            expected_server=intent_server,
            expected_session_id=live_id,
            expected_window_id=leader_window,
            expected_pane_id=leader_pane,
            intent_path=intent_path,
            intent_nonce=intent_nonce,
        )
        _verify_worker_pane_membership(
            pane_id=first_pane,
            leader_pane=leader_pane,
            expected_session_id=live_id,
            expected_window_id=leader_window,
            expected_server=intent_server,
            socket_path=sock,
        )
        created_panes.append(first_pane)
        stack_pane = first_pane
        if intent_path is not None:
            append_team_launch_intent_pane_id(intent_path, first_pane)
            if _publish_intent_nonce_on_pane(
                pane_id=first_pane,
                intent_nonce=intent_nonce,
                socket_path=sock,
                expected_server=intent_server,
                expected_session_id=live_id,
                expected_window_id=leader_window,
            ):
                try:
                    ack_team_launch_intent_nonce_published(intent_path)
                except TmuxTeamError:
                    pass

        # Remaining workers: vertical stack on the first worker pane.
        for task in tasks[1:]:
            assert stack_pane is not None
            pane_id = _split_worker_pane_gated(
                target=stack_pane,
                task=task,
                env_pairs=env_pairs,
                horizontal=False,
                socket_path=sock,
                expected_server=intent_server,
                expected_session_id=live_id,
                expected_window_id=leader_window,
                expected_pane_id=stack_pane,
                intent_path=intent_path,
                intent_nonce=intent_nonce,
            )
            _verify_worker_pane_membership(
                pane_id=pane_id,
                leader_pane=leader_pane,
                expected_session_id=live_id,
                expected_window_id=leader_window,
                expected_server=intent_server,
                socket_path=sock,
            )
            created_panes.append(pane_id)
            if intent_path is not None:
                append_team_launch_intent_pane_id(intent_path, pane_id)
                _publish_intent_nonce_on_pane(
                    pane_id=pane_id,
                    intent_nonce=intent_nonce,
                    socket_path=sock,
                    expected_server=intent_server,
                    expected_session_id=live_id,
                    expected_window_id=leader_window,
                )

        if leader_pane in created_panes:
            raise TmuxTeamError("worker pane list incorrectly includes leader pane")
        if len(created_panes) != len(tasks):
            raise TmuxTeamError("pane count mismatch after same_window split")

        _require_tmux_server(
            intent_server, socket_path=sock, action="same_window create commit"
        )
        for pane_id in created_panes:
            _verify_worker_pane_membership(
                pane_id=pane_id,
                leader_pane=leader_pane,
                expected_session_id=live_id,
                expected_window_id=leader_window,
                expected_server=intent_server,
                socket_path=sock,
            )
        for task, pane_id in zip(tasks, created_panes, strict=True):
            task["pane_id"] = pane_id

        leader_width = _apply_same_window_layout(
            window_id=leader_window,
            leader_pane=leader_pane,
            worker_count=len(created_panes),
            socket_path=sock,
        )
        post = _assert_leader_postconditions(snap=snap, socket_path=sock)
        if int(post["pane_pid"]) != leader_pid:
            raise TmuxTeamError(
                "leader pane pid drifted during same_window create "
                f"(expected {leader_pid}, got {post['pane_pid']})"
            )
        _stamp_launch_meta(
            tasks,
            attach_mode="inside",
            session_owned=False,
            leader_pane_id=leader_pane,
            window_id=leader_window,
            attach_hint=f"tmux select-pane -t {leader_pane}",
            session_id=live_id,
            tmux_server=intent_server,
            view_mode=VIEW_MODE_SAME_WINDOW,
            layout=LAYOUT_MAIN_VERTICAL,
            leader_pane_pid=int(post["pane_pid"]),
        )
        tasks[0]["_tmux_launch"]["leader_width"] = leader_width
        if intent_path is not None:
            tasks[0]["_tmux_launch_intent"] = str(intent_path)
    except (TmuxTeamError, OSError) as exc:
        cleanup_bits: list[str] = []
        cleanup_ok = False
        if intent_server is not None and sock is None:
            sock = str(intent_server["tmux_socket_path"])
        panes_to_kill = list(created_panes)
        if intent_path is not None:
            try:
                raw_intent = json.loads(Path(intent_path).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                raw_intent = None
            if isinstance(raw_intent, dict):
                for pid in _intent_known_pane_ids(raw_intent):
                    if pid not in panes_to_kill:
                        panes_to_kill.append(pid)
        err = _kill_panes_scoped(
            panes_to_kill,
            expected_server=intent_server,
            expected_session_id=live_id,
            expected_window_id=leader_window,
            intent_or_launch_nonce=intent_nonce,
            leader_pane_id=leader_pane,
            socket_path=sock,
        )
        if err:
            cleanup_bits.append(err)
        else:
            cleanup_ok = True
        if cleanup_ok:
            clear_team_launch_intent(intent_path)
        if cleanup_bits:
            raise TmuxTeamError(f"{exc}; " + "; ".join(cleanup_bits)) from exc
        raise
    return live_name, live_id


def _create_inside_dedicated_window(
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

    Preserves main (#97/#98/#108) WAL / server identity / absence-proof cleanup.
    Never kills the leader pane or the whole session on cleanup.
    """
    from omg_cli.team.topology import LAYOUT_TILED, VIEW_MODE_DEDICATED_WINDOW

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
    intent_server = _intent_tmux_server(snap)
    sock = (
        str(intent_server["tmux_socket_path"]) if intent_server is not None else None
    )
    try:
        # Re-validate immediately before the first mutation so a mid-launch
        # client move cannot bind workers to a different session (#97 Pro P1).
        assert_invoking_identity(snap)
        leader_window = str(snap["window_id"])
        if root is not None and run_id is not None:
            if intent_server is None:
                raise TmuxTeamError(
                    "inside launch refused: invoking server identity unavailable"
                )
            intent_path = write_team_launch_intent(
                root,
                run_id=str(run_id),
                session_id=live_id,
                window_name=window_name,
                nonce=window_nonce,
                tmux_server=intent_server,
                view_mode=VIEW_MODE_DEDICATED_WINDOW,
                leader_pane_id=leader_pane,
                leader_window_id=leader_window,
            )

        def _publish_created(wid: str, pane: str) -> None:
            nonlocal window_id
            window_id = wid

        window_id, first_pane = _launch_first_inside(
            task=tasks[0],
            env_pairs=env_pairs,
            window_name=window_name,
            target_window=leader_window,
            expected_session_id=live_id,
            intent_path=intent_path,
            publish_created=_publish_created,
            socket_path=sock,
            expected_server=intent_server,
        )
        if intent_server is not None:
            _require_tmux_server(
                intent_server,
                socket_path=sock,
                action="inside window readback",
            )
        win_probe = _tmux_run(
            [
                "display-message",
                "-p",
                "-t",
                window_id,
                "#{session_id}\t#{window_id}\t#{pid}",
            ],
            socket_path=sock,
        )
        win_parts = (win_probe.stdout or "").strip().split("\t")
        if (
            win_probe.returncode != 0
            or len(win_parts) != 3
            or win_parts[0] != live_id
            or win_parts[1] != window_id
            or (
                intent_server is not None
                and (
                    not win_parts[2].isdigit()
                    or int(win_parts[2]) != intent_server["tmux_server_pid"]
                )
            )
        ):
            raise TmuxTeamError(
                "created team window is not in the invoking session "
                f"(expected session_id={live_id!r})"
            )
        if first_pane == leader_pane:
            raise TmuxTeamError("refusing to overwrite leader pane with worker")
        created_panes.append(first_pane)
        created_panes.extend(
            _split_remaining(
                target=window_id,
                tasks=tasks[1:],
                env_pairs=env_pairs,
                socket_path=sock,
                expected_server=intent_server,
                expected_session_id=live_id,
                expected_window_id=window_id,
            )
        )
        if leader_pane in created_panes:
            raise TmuxTeamError("worker pane list incorrectly includes leader pane")
        if len(created_panes) != len(tasks):
            raise TmuxTeamError("pane count mismatch after inside split")
        for pane_id in created_panes:
            _verify_worker_pane_membership(
                pane_id=pane_id,
                leader_pane=leader_pane,
                expected_session_id=live_id,
                expected_window_id=window_id,
                expected_server=intent_server,
                socket_path=sock,
            )
        if intent_server is not None:
            _require_tmux_server(
                intent_server,
                socket_path=sock,
                action="inside create commit",
            )
        win_recheck = _tmux_run(
            [
                "display-message",
                "-p",
                "-t",
                window_id,
                "#{session_id}\t#{window_id}\t#{pid}",
            ],
            socket_path=sock,
        )
        win_recheck_parts = (win_recheck.stdout or "").strip().split("\t")
        if (
            win_recheck.returncode != 0
            or len(win_recheck_parts) != 3
            or win_recheck_parts[0] != live_id
            or win_recheck_parts[1] != window_id
            or (
                intent_server is not None
                and (
                    not win_recheck_parts[2].isdigit()
                    or int(win_recheck_parts[2]) != intent_server["tmux_server_pid"]
                )
            )
        ):
            raise TmuxTeamError(
                "team window left the invoking session before commit "
                f"(expected session_id={live_id!r})"
            )
        for task, pane_id in zip(tasks, created_panes, strict=True):
            task["pane_id"] = pane_id
        layout = _tmux_run(
            ["select-layout", "-t", window_id, "tiled"], socket_path=sock
        )
        if layout.returncode != 0:
            raise TmuxTeamError("failed to apply tiled layout")
        _restore_leader_focus(leader_pane, socket_path=sock)
        _stamp_launch_meta(
            tasks,
            attach_mode="inside",
            session_owned=False,
            leader_pane_id=leader_pane,
            window_id=window_id,
            attach_hint=f"tmux select-pane -t {leader_pane}",
            session_id=live_id,
            tmux_server=intent_server,
            view_mode=VIEW_MODE_DEDICATED_WINDOW,
            layout=LAYOUT_TILED,
            leader_pane_pid=int(snap["pane_pid"]),
        )
        if intent_path is not None:
            tasks[0]["_tmux_launch_intent"] = str(intent_path)
    except (TmuxTeamError, OSError) as exc:
        cleanup_bits: list[str] = []
        cleanup_ok = False
        if intent_server is not None and sock is None:
            sock = str(intent_server["tmux_socket_path"])
        if window_id:
            id_err = _kill_window_allowing_intent_nonce_move(
                window_id,
                expected_session_id=live_id,
                intent_nonce=window_nonce,
                socket_path=sock,
                expected_server=intent_server,
            )
            if id_err:
                cleanup_bits.append(id_err)
            cleanup_nonce_published = False
            if intent_path is not None:
                try:
                    raw_intent = json.loads(
                        Path(intent_path).read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    raw_intent = None
                if isinstance(raw_intent, dict):
                    cleanup_nonce_published = _intent_nonce_published(raw_intent)
            name_err = _kill_inside_windows_by_name(
                session_id=live_id,
                window_name=window_name,
                known_window_ids=[window_id],
                socket_path=sock,
                expected_server=intent_server,
                require_durable_window_id=True,
                intent_nonce=window_nonce,
                nonce_published=cleanup_nonce_published,
            )
            if name_err:
                cleanup_bits.append(name_err)
            if id_err is None and name_err is None:
                cleanup_ok = True
        else:
            known_ids: list[str] = []
            require_durable = True
            cleanup_intent_nonce: str | None = window_nonce
            cleanup_nonce_published = False
            if intent_path is not None:
                try:
                    raw_intent = json.loads(
                        Path(intent_path).read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    raw_intent = None
                if isinstance(raw_intent, dict):
                    known_ids.extend(_intent_known_window_ids(raw_intent))
                    if intent_server is None:
                        intent_server = _intent_tmux_server(raw_intent)
                        if intent_server is not None:
                            sock = str(intent_server["tmux_socket_path"])
                    require_durable = _intent_requires_durable_window_id(raw_intent)
                    cleanup_intent_nonce = (
                        _intent_nonce_value(raw_intent) or cleanup_intent_nonce
                    )
                    cleanup_nonce_published = _intent_nonce_published(raw_intent)
            err = _kill_inside_windows_by_name(
                session_id=live_id,
                window_name=window_name,
                known_window_ids=known_ids,
                socket_path=sock,
                expected_server=intent_server,
                require_durable_window_id=require_durable,
                intent_nonce=cleanup_intent_nonce,
                nonce_published=cleanup_nonce_published,
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


# ---------------------------------------------------------------------------
# #102 lifecycle primitives — topology-aware spawn / bind / layout reconcile
# ---------------------------------------------------------------------------

WORKER_PANE_NONCE_OPTION = "@omg_worker_nonce"


@dataclass(frozen=True)
class SpawnedWorkerPane:
    """Exact pane identity returned by a topology-aware spawn."""

    session_id: str
    window_id: str
    pane_id: str
    pane_pid: int
    pane_owner_nonce: str


@dataclass(frozen=True)
class LayoutReconcileResult:
    """Outcome of post-commit layout projection (never process authority)."""

    status: str  # clean | repair_needed
    last_error_code: str | None = None
    layout_name: str | None = None


def bind_worker_pane_owner(
    *,
    pane_id: str,
    pane_owner_nonce: str,
    expected_server: Mapping[str, Any],
    expected_session_id: str,
    expected_window_id: str,
    launch_nonce: str | None = None,
    socket_path: str | None = None,
) -> None:
    """Stamp per-attempt ``@omg_worker_nonce`` and strict-read it back."""
    if _TMUX_PANE_ID.fullmatch(pane_id) is None:
        raise TmuxTeamError(f"bind_worker_pane_owner: invalid pane id {pane_id!r}")
    if not isinstance(pane_owner_nonce, str) or not pane_owner_nonce:
        raise TmuxTeamError("bind_worker_pane_owner: pane_owner_nonce required")
    server = _intent_tmux_server(expected_server)
    if server is None:
        raise TmuxTeamError("bind_worker_pane_owner: durable tmux server required")
    sock = socket_path or str(server["tmux_socket_path"])
    set_argv = [
        "set-option",
        "-p",
        "-t",
        pane_id,
        WORKER_PANE_NONCE_OPTION,
        pane_owner_nonce,
    ]
    set_r = _tmux_run_if_identity(
        set_argv,
        target=pane_id,
        expected_server=server,
        socket_path=sock,
        window_id=expected_window_id,
        expected_session_id=expected_session_id,
        pane_id=pane_id,
    )
    if set_r.returncode != 0:
        err = (set_r.stderr or set_r.stdout or "").strip()[:400]
        raise TmuxTeamError(f"failed to stamp worker pane owner nonce: {err}")
    show = _tmux_run(
        ["show-options", "-p", "-v", "-t", pane_id, WORKER_PANE_NONCE_OPTION],
        socket_path=sock,
    )
    if show.returncode != 0 or (show.stdout or "").strip() != pane_owner_nonce:
        raise TmuxTeamError("worker pane owner nonce readback failed")
    probe = _tmux_run(
        [
            "display-message",
            "-p",
            "-t",
            pane_id,
            "#{pane_id}\t#{window_id}\t#{session_id}\t"
            f"#{{{WORKER_PANE_NONCE_OPTION}}}"
            + (
                "\t#{@omg_launch_nonce}"
                if launch_nonce is not None
                else ""
            ),
        ],
        socket_path=sock,
    )
    parts = (probe.stdout or "").strip().split("\t")
    expected_len = 4 if launch_nonce is None else 5
    if (
        probe.returncode != 0
        or len(parts) != expected_len
        or parts[0] != pane_id
        or parts[1] != expected_window_id
        or parts[2] != expected_session_id
        or parts[3] != pane_owner_nonce
        or (launch_nonce is not None and parts[4] != launch_nonce)
    ):
        raise TmuxTeamError("worker pane owner identity readback mismatch")


def read_exact_worker_pane_identity(
    *,
    pane_id: str,
    expected_session_id: str,
    expected_window_id: str,
    pane_owner_nonce: str,
    socket_path: str | None = None,
) -> int:
    """Return pane PID when session/window/owner nonce still match exactly."""
    if _TMUX_PANE_ID.fullmatch(pane_id) is None:
        raise TmuxTeamError(f"invalid pane id {pane_id!r}")
    probe = _tmux_run(
        [
            "display-message",
            "-p",
            "-t",
            pane_id,
            "#{pane_id}\t#{window_id}\t#{session_id}\t#{pane_pid}\t"
            f"#{{{WORKER_PANE_NONCE_OPTION}}}\t#{{pane_dead}}",
        ],
        socket_path=socket_path,
    )
    parts = (probe.stdout or "").strip().split("\t")
    if (
        probe.returncode != 0
        or len(parts) != 6
        or parts[0] != pane_id
        or parts[1] != expected_window_id
        or parts[2] != expected_session_id
        or parts[4] != pane_owner_nonce
        or parts[5] != "0"
    ):
        raise TmuxTeamError(
            f"exact worker pane identity drift pane={pane_id!r}"
        )
    try:
        pid = int(parts[3])
    except ValueError as exc:
        raise TmuxTeamError(f"invalid pane pid {parts[3]!r}") from exc
    if pid <= 0:
        raise TmuxTeamError(f"non-positive pane pid {pid}")
    return pid


def discover_worker_pane_by_owner_nonce(
    *,
    session_id: str,
    window_id: str,
    pane_owner_nonce: str,
    socket_path: str | None = None,
) -> str | None:
    """Find the unique pane in *window_id* stamped with *pane_owner_nonce*."""
    listed = _tmux_run(
        [
            "list-panes",
            "-t",
            window_id,
            "-F",
            "#{pane_id}\t#{session_id}\t#{window_id}\t"
            f"#{{{WORKER_PANE_NONCE_OPTION}}}",
        ],
        socket_path=socket_path,
    )
    if listed.returncode != 0:
        raise TmuxTeamError("failed to enumerate panes for owner-nonce discovery")
    matches: list[str] = []
    for line in (listed.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            raise TmuxTeamError("malformed pane discovery row")
        pane_id, row_session, row_window, marker = parts
        if (
            row_session != session_id
            or row_window != window_id
            or _TMUX_PANE_ID.fullmatch(pane_id) is None
        ):
            raise TmuxTeamError("pane discovery identity malformed")
        if marker == pane_owner_nonce:
            matches.append(pane_id)
    if len(matches) > 1:
        raise TmuxTeamError(
            f"ambiguous worker pane owner nonce {pane_owner_nonce!r}"
        )
    return matches[0] if matches else None


def _ensure_window_split_capacity(
    window_id: str,
    *,
    socket_path: str | None,
    min_width: int = 120,
    min_height: int = 36,
) -> None:
    """Grow a Team window so ``split-window`` has geometric room.

    Headless/CI tmux defaults can be too small after main-vertical packing;
    ``split-window`` then fails with ``no space for a new pane`` (seen on
    macOS GHA for same_window scale-up). Never shrinks. If the geometry
    probe fails, leave the window unchanged — the subsequent split still
    fails closed with the real tmux error.
    """
    if _TMUX_WINDOW_ID.fullmatch(window_id) is None:
        return
    probe = _tmux_run(
        [
            "display-message",
            "-p",
            "-t",
            window_id,
            "#{window_width}\t#{window_height}",
        ],
        socket_path=socket_path,
    )
    parts = (probe.stdout or "").strip().split("\t")
    try:
        width = int(parts[0]) if len(parts) == 2 else 0
        height = int(parts[1]) if len(parts) == 2 else 0
    except ValueError:
        return
    if probe.returncode != 0 or width <= 0 or height <= 0:
        return
    target_w = max(width, int(min_width))
    target_h = max(height, int(min_height))
    if target_w == width and target_h == height:
        return
    _tmux_run(
        [
            "resize-window",
            "-t",
            window_id,
            "-x",
            str(target_w),
            "-y",
            str(target_h),
        ],
        socket_path=socket_path,
    )


def _spawn_worker_split_pane(
    *,
    target_pane_id: str | None,
    target_window_id: str,
    worktree: str,
    pane_command: str,
    env_pairs: Sequence[tuple[str, str]] | None,
    horizontal: bool,
    expected_server: Mapping[str, Any],
    expected_session_id: str,
    pane_owner_nonce: str,
    launch_nonce: str | None = None,
    leader_pane_id: str | None = None,
    socket_path: str | None = None,
) -> SpawnedWorkerPane:
    """Identity-gated detached split into an exact Team window."""
    server = _intent_tmux_server(expected_server)
    if server is None:
        raise TmuxTeamError("spawn split requires durable tmux server identity")
    sock = socket_path or str(server["tmux_socket_path"])
    split_target = target_pane_id or target_window_id
    if leader_pane_id is not None and split_target == leader_pane_id and not horizontal:
        # Vertical split against leader is refused — callers must use -h first.
        raise TmuxTeamError("refusing vertical split against leader pane")
    _ensure_window_split_capacity(target_window_id, socket_path=sock)
    task_env = tmux_env_args(list(env_pairs or []))
    split_argv = [
        "split-window",
        "-h" if horizontal else "-v",
        "-d",
        "-P",
        "-F",
        "#{pane_id}",
        "-t",
        split_target,
        "-c",
        str(worktree),
        *task_env,
        str(pane_command),
    ]
    split = _tmux_run_if_identity(
        split_argv,
        target=split_target,
        expected_server=server,
        socket_path=sock,
        window_id=target_window_id,
        expected_session_id=expected_session_id,
        pane_id=target_pane_id if target_pane_id else None,
    )
    if split.returncode != 0:
        # Lost-stdout recovery via owner nonce is impossible pre-bind; surface.
        err = (split.stderr or split.stdout or "").strip()[:400]
        # Try orphan discovery only if a previous attempt already bound nonce.
        orphan = discover_worker_pane_by_owner_nonce(
            session_id=expected_session_id,
            window_id=target_window_id,
            pane_owner_nonce=pane_owner_nonce,
            socket_path=sock,
        )
        if orphan is None:
            raise TmuxTeamError(f"split-window failed: {err}")
        pane_id = orphan
    else:
        pane_id = (split.stdout or "").strip()
        if _TMUX_PANE_ID.fullmatch(pane_id) is None:
            orphan = discover_worker_pane_by_owner_nonce(
                session_id=expected_session_id,
                window_id=target_window_id,
                pane_owner_nonce=pane_owner_nonce,
                socket_path=sock,
            )
            if orphan is None:
                raise TmuxTeamError("split-window did not return pane id")
            pane_id = orphan
    if leader_pane_id is not None and pane_id == leader_pane_id:
        raise TmuxTeamError("spawn produced leader pane id")
    _verify_worker_pane_membership(
        pane_id=pane_id,
        leader_pane=leader_pane_id or "",
        expected_session_id=expected_session_id,
        expected_window_id=target_window_id,
        expected_server=server,
        socket_path=sock,
    )
    bind_worker_pane_owner(
        pane_id=pane_id,
        pane_owner_nonce=pane_owner_nonce,
        expected_server=server,
        expected_session_id=expected_session_id,
        expected_window_id=target_window_id,
        launch_nonce=launch_nonce,
        socket_path=sock,
    )
    pid = read_exact_worker_pane_identity(
        pane_id=pane_id,
        expected_session_id=expected_session_id,
        expected_window_id=target_window_id,
        pane_owner_nonce=pane_owner_nonce,
        socket_path=sock,
    )
    return SpawnedWorkerPane(
        session_id=expected_session_id,
        window_id=target_window_id,
        pane_id=pane_id,
        pane_pid=pid,
        pane_owner_nonce=pane_owner_nonce,
    )


def spawn_worker_same_window(
    *,
    target_pane_id: str,
    team_window_id: str,
    worktree: str,
    pane_command: str,
    env_pairs: Sequence[tuple[str, str]] | None,
    horizontal: bool,
    expected_server: Mapping[str, Any],
    expected_session_id: str,
    pane_owner_nonce: str,
    launch_nonce: str | None = None,
    leader_pane_id: str | None = None,
    socket_path: str | None = None,
) -> SpawnedWorkerPane:
    """Spawn into the shared leader window (never ``new-window``)."""
    return _spawn_worker_split_pane(
        target_pane_id=target_pane_id,
        target_window_id=team_window_id,
        worktree=worktree,
        pane_command=pane_command,
        env_pairs=env_pairs,
        horizontal=horizontal,
        expected_server=expected_server,
        expected_session_id=expected_session_id,
        pane_owner_nonce=pane_owner_nonce,
        launch_nonce=launch_nonce,
        leader_pane_id=leader_pane_id,
        socket_path=socket_path,
    )


def spawn_worker_dedicated_window(
    *,
    team_window_id: str,
    target_pane_id: str | None,
    worktree: str,
    pane_command: str,
    env_pairs: Sequence[tuple[str, str]] | None,
    expected_server: Mapping[str, Any],
    expected_session_id: str,
    pane_owner_nonce: str,
    launch_nonce: str | None = None,
    horizontal: bool = False,
    socket_path: str | None = None,
) -> SpawnedWorkerPane:
    """Spawn into the exact dedicated Team window (never a second Team window)."""
    return _spawn_worker_split_pane(
        target_pane_id=target_pane_id,
        target_window_id=team_window_id,
        worktree=worktree,
        pane_command=pane_command,
        env_pairs=env_pairs,
        horizontal=horizontal,
        expected_server=expected_server,
        expected_session_id=expected_session_id,
        pane_owner_nonce=pane_owner_nonce,
        launch_nonce=launch_nonce,
        leader_pane_id=None,
        socket_path=socket_path,
    )


def spawn_worker_detached_session(
    *,
    team_window_id: str,
    target_pane_id: str | None,
    worktree: str,
    pane_command: str,
    env_pairs: Sequence[tuple[str, str]] | None,
    expected_server: Mapping[str, Any],
    expected_session_id: str,
    pane_owner_nonce: str,
    launch_nonce: str | None = None,
    horizontal: bool = False,
    socket_path: str | None = None,
) -> SpawnedWorkerPane:
    """Spawn into the Team-owned detached session window (no attach)."""
    return _spawn_worker_split_pane(
        target_pane_id=target_pane_id,
        target_window_id=team_window_id,
        worktree=worktree,
        pane_command=pane_command,
        env_pairs=env_pairs,
        horizontal=horizontal,
        expected_server=expected_server,
        expected_session_id=expected_session_id,
        pane_owner_nonce=pane_owner_nonce,
        launch_nonce=launch_nonce,
        leader_pane_id=None,
        socket_path=socket_path,
    )


def kill_exact_worker_pane(
    *,
    pane_id: str,
    expected_server: Mapping[str, Any] | None,
    expected_session_id: str | None,
    expected_window_id: str | None,
    intent_or_launch_nonce: str | None = None,
    leader_pane_id: str | None = None,
    socket_path: str | None = None,
) -> str | None:
    """Kill one exact worker pane; never the leader. Returns error or None."""
    if leader_pane_id is not None and pane_id == leader_pane_id:
        return f"refused kill of leader pane {pane_id}"
    return _kill_panes_scoped(
        [pane_id],
        expected_server=expected_server,
        expected_session_id=expected_session_id,
        expected_window_id=expected_window_id,
        intent_or_launch_nonce=intent_or_launch_nonce,
        leader_pane_id=leader_pane_id,
        socket_path=socket_path,
    )


def reconcile_layout(
    *,
    mode: str,
    team_window_id: str,
    leader_pane_id: str | None,
    leader_pane_pid: int | None,
    session_id: str,
    worker_count: int,
    expected_server: Mapping[str, Any] | None = None,
    socket_path: str | None = None,
    layout_name: str | None = None,
) -> LayoutReconcileResult:
    """Reapply visual layout after a lifecycle commit.

    Layout failure is never process authority — callers must not roll back
    committed identity when this returns ``repair_needed``.
    """
    from omg_cli.team.topology import (
        LAYOUT_MAIN_VERTICAL,
        LAYOUT_STATUS_CLEAN,
        LAYOUT_STATUS_REPAIR_NEEDED,
        LAYOUT_TILED,
        VIEW_MODE_SAME_WINDOW,
        layout_for_view_mode,
    )

    resolved_layout = layout_name or layout_for_view_mode(mode)
    try:
        server = (
            _intent_tmux_server(expected_server)
            if expected_server is not None
            else None
        )
        sock = socket_path
        if sock is None and server is not None:
            sock = str(server["tmux_socket_path"])
        if mode == VIEW_MODE_SAME_WINDOW:
            if not isinstance(leader_pane_id, str) or not leader_pane_id:
                raise TmuxTeamError("same_window layout requires leader_pane_id")
            if leader_pane_pid is None:
                raise TmuxTeamError("same_window layout requires leader_pane_pid")
            _apply_same_window_layout(
                window_id=team_window_id,
                leader_pane=leader_pane_id,
                worker_count=max(1, worker_count),
                socket_path=sock,
            )
            _assert_leader_postconditions(
                snap={
                    "pane_id": leader_pane_id,
                    "pane_pid": leader_pane_pid,
                    "session_id": session_id,
                    "window_id": team_window_id,
                },
                socket_path=sock,
            )
        else:
            # dedicated / detached: tiled (or persisted) without client navigation.
            layout = _tmux_run(
                [
                    "select-layout",
                    "-t",
                    team_window_id,
                    resolved_layout if resolved_layout else LAYOUT_TILED,
                ],
                socket_path=sock,
            )
            if layout.returncode != 0:
                err = (layout.stderr or layout.stdout or "").strip()[:200]
                raise TmuxTeamError(f"select-layout failed: {err}")
        return LayoutReconcileResult(
            status=LAYOUT_STATUS_CLEAN,
            last_error_code=None,
            layout_name=resolved_layout or LAYOUT_MAIN_VERTICAL,
        )
    except (TmuxTeamError, OSError) as exc:
        code = "select_layout_failed"
        if "leader" in str(exc).lower():
            code = "leader_postcondition_failed"
        return LayoutReconcileResult(
            status=LAYOUT_STATUS_REPAIR_NEEDED,
            last_error_code=code,
            layout_name=resolved_layout,
        )


# ---------------------------------------------------------------------------
# #101 identity-fenced operator effects (capture / focus / literal / key)
# ---------------------------------------------------------------------------

MAX_OPERATOR_CAPTURE_BYTES = 16_384
MAX_OPERATOR_INPUT_BYTES = 4_096
MAX_OPERATOR_CAPTURE_LINES = 2_000

ALLOWED_OPERATOR_KEYS: frozenset[str] = frozenset(
    {
        "Enter",
        "Escape",
        "Tab",
        "BTab",
        "Up",
        "Down",
        "Left",
        "Right",
        "PageUp",
        "PageDown",
        "Home",
        "End",
        "Backspace",
        "Delete",
        "BSpace",
        "DC",
        "C-c",
        "C-d",
        "C-z",
        "C-l",
        "C-a",
        "C-e",
        "C-u",
        "C-k",
        "C-w",
    }
)


def _require_exact_pane_id(pane_id: str) -> str:
    if not isinstance(pane_id, str) or _TMUX_PANE_ID.fullmatch(pane_id) is None:
        raise TmuxTeamError(f"refused non-exact pane id {pane_id!r}")
    return pane_id


def capture_pane(
    pane_id: str,
    *,
    lines: int = 200,
    raw: bool = False,
    socket_path: str | None = None,
) -> str:
    """Bounded ``capture-pane -p`` against an exact ``%N`` pane id.

    Always enforces line + byte caps. ``raw=True`` skips join/escape normalize
    but never removes size bounds. Callers must still redact.
    """
    if not tmux_available():
        raise TmuxTeamError("tmux is required for pane capture")
    target = _require_exact_pane_id(pane_id)
    if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
        raise TmuxTeamError("capture lines must be a positive int")
    bound = min(int(lines), MAX_OPERATOR_CAPTURE_LINES)
    argv = [
        "capture-pane",
        "-p",
        "-t",
        target,
        "-S",
        f"-{bound}",
    ]
    if not raw:
        # Join wrapped lines for readable operator output.
        argv.append("-J")
    result = _tmux_run(argv, socket_path=socket_path)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:200]
        raise TmuxTeamError(f"capture-pane failed: {err}")
    text = result.stdout or ""
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_OPERATOR_CAPTURE_BYTES:
        text = encoded[:MAX_OPERATOR_CAPTURE_BYTES].decode("utf-8", errors="ignore")
    return text


def focus_pane(
    pane_id: str,
    *,
    socket_path: str | None = None,
) -> None:
    """Select an exact pane (``select-pane -t %N``). No session kill/create."""
    if not tmux_available():
        raise TmuxTeamError("tmux is required for pane focus")
    target = _require_exact_pane_id(pane_id)
    result = _tmux_run(["select-pane", "-t", target], socket_path=socket_path)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:200]
        raise TmuxTeamError(f"select-pane failed: {err}")


def send_literal(
    pane_id: str,
    text: str,
    *,
    socket_path: str | None = None,
) -> None:
    """Send literal text via ``send-keys -l`` (never interprets key names)."""
    if not tmux_available():
        raise TmuxTeamError("tmux is required for pane input")
    target = _require_exact_pane_id(pane_id)
    if not isinstance(text, str):
        raise TmuxTeamError("literal input must be a string")
    if "\0" in text:
        raise TmuxTeamError("literal input refuses NUL")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_OPERATOR_INPUT_BYTES:
        raise TmuxTeamError(
            f"literal input exceeds {MAX_OPERATOR_INPUT_BYTES} UTF-8 bytes"
        )
    # ``--`` ends option parsing so text cannot smuggle tmux flags.
    result = _tmux_run(
        ["send-keys", "-l", "-t", target, "--", text],
        socket_path=socket_path,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:200]
        raise TmuxTeamError(f"send-keys -l failed: {err}")


def send_key(
    pane_id: str,
    key: str,
    *,
    socket_path: str | None = None,
) -> None:
    """Send one allowlisted key name via argv ``send-keys`` (shell=False)."""
    if not tmux_available():
        raise TmuxTeamError("tmux is required for pane key delivery")
    target = _require_exact_pane_id(pane_id)
    if not isinstance(key, str) or key not in ALLOWED_OPERATOR_KEYS:
        raise TmuxTeamError(f"key not allowlisted: {key!r}")
    if any(ch.isspace() for ch in key) or ";" in key:
        raise TmuxTeamError(f"key injection refused: {key!r}")
    result = _tmux_run(
        ["send-keys", "-t", target, key],
        socket_path=socket_path,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:200]
        raise TmuxTeamError(f"send-keys failed: {err}")


def attach_argv_for_target(
    *,
    pane_id: str,
    session_id: str,
    window_id: str | None = None,
    takeover: bool = False,
    socket_path: str | None = None,
) -> list[str]:
    """Return a safe argv hint for attaching then selecting a proved pane.

    Always targets exact ``session_id`` (``$N``) — never a mutable session
    name (name-reuse TOCTOU). ``takeover=True`` adds ``attach-session -d``.
    Optional ``socket_path`` pins ``tmux -S`` for non-default servers.
    """
    _require_exact_pane_id(pane_id)
    if not isinstance(session_id, str) or _TMUX_SESSION_ID.fullmatch(session_id) is None:
        raise TmuxTeamError(f"refused attach without exact session_id {session_id!r}")
    if window_id is not None:
        if not isinstance(window_id, str) or _TMUX_WINDOW_ID.fullmatch(window_id) is None:
            raise TmuxTeamError(f"refused non-exact window id {window_id!r}")
    argv: list[str] = ["tmux"]
    if (
        isinstance(socket_path, str)
        and socket_path
        and "\0" not in socket_path
    ):
        argv.extend(["-S", socket_path])
    argv.append("attach-session")
    if takeover:
        argv.append("-d")
    argv.extend(["-t", session_id, ";"])
    if isinstance(window_id, str) and window_id:
        argv.extend(["select-window", "-t", window_id, ";"])
    argv.extend(["select-pane", "-t", pane_id])
    return argv


def select_window(
    window_id: str,
    *,
    socket_path: str | None = None,
) -> None:
    """Select an exact window (``select-window -t @N``)."""
    if not tmux_available():
        raise TmuxTeamError("tmux is required for window select")
    if not isinstance(window_id, str) or _TMUX_WINDOW_ID.fullmatch(window_id) is None:
        raise TmuxTeamError(f"refused non-exact window id {window_id!r}")
    result = _tmux_run(
        ["select-window", "-t", window_id], socket_path=socket_path
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:200]
        raise TmuxTeamError(f"select-window failed: {err}")


def switch_client(
    session_id: str,
    *,
    socket_path: str | None = None,
) -> None:
    """Switch the current client to an exact session (``$N``). Never attach."""
    if not tmux_available():
        raise TmuxTeamError("tmux is required for switch-client")
    if not isinstance(session_id, str) or _TMUX_SESSION_ID.fullmatch(session_id) is None:
        raise TmuxTeamError(f"refused non-exact session id {session_id!r}")
    result = _tmux_run(
        ["switch-client", "-t", session_id], socket_path=socket_path
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:200]
        raise TmuxTeamError(f"switch-client failed: {err}")


def probe_current_session_id(
    *,
    pane_id: str | None = None,
    socket_path: str | None = None,
) -> str | None:
    """Return the current client's ``session_id`` (``$N``) or ``None``."""
    if not tmux_available():
        return None
    argv: list[str] = ["display-message", "-p"]
    if isinstance(pane_id, str) and _TMUX_PANE_ID.fullmatch(pane_id):
        argv.extend(["-t", pane_id])
    argv.append("#{session_id}")
    result = _tmux_run(argv, socket_path=socket_path)
    if result.returncode != 0:
        return None
    sid = (result.stdout or "").strip()
    if _TMUX_SESSION_ID.fullmatch(sid) is None:
        return None
    return sid


def prove_view_target_live(
    *,
    session_id: str,
    session_name: str,
    launch_nonce: str,
    window_id: str | None,
    pane_id: str,
    expected_pid: int | None = None,
    socket_path: str | None = None,
) -> dict[str, Any]:
    """Re-probe exact Team view identity immediately before a client effect.

    Proves session id/name, optional window, pane, and launch nonce. Nonce may
    live on the pane or the Team window (leader panes in same_window often
    carry only the window-scoped stamp). Never falls back to current
    focus/index/title.
    """
    if not tmux_available():
        raise TmuxTeamError("tmux is required for view target proof")
    target = _require_exact_pane_id(pane_id)
    if _TMUX_SESSION_ID.fullmatch(session_id) is None:
        raise TmuxTeamError(f"refused non-exact session id {session_id!r}")
    if (
        not isinstance(session_name, str)
        or not session_name
        or "\0" in session_name
        or any(ch.isspace() for ch in session_name)
    ):
        raise TmuxTeamError(f"refused unsafe session name {session_name!r}")
    if not isinstance(launch_nonce, str) or not launch_nonce:
        raise TmuxTeamError("view proof requires launch_nonce")
    if window_id is not None and _TMUX_WINDOW_ID.fullmatch(window_id) is None:
        raise TmuxTeamError(f"refused non-exact window id {window_id!r}")

    probe = _tmux_run(
        [
            "display-message",
            "-p",
            "-t",
            target,
            "#{pane_id}\t#{session_id}\t#{session_name}\t#{window_id}\t"
            "#{pane_pid}\t#{pane_dead}",
        ],
        socket_path=socket_path,
    )
    parts = (probe.stdout or "").strip().split("\t")
    if probe.returncode != 0 or len(parts) < 6:
        raise TmuxTeamError(f"view target pane missing or unreadable: {target}")
    got_pane, got_sid, got_name, got_wid, got_pid, dead = parts[:6]
    if got_pane != target:
        raise TmuxTeamError(
            f"view target pane id drift (expected {target!r} got {got_pane!r})"
        )
    if got_sid != session_id:
        raise TmuxTeamError(
            f"view target session_id mismatch "
            f"(expected {session_id!r} got {got_sid!r})"
        )
    if got_name != session_name:
        raise TmuxTeamError(
            f"view target session_name mismatch "
            f"(expected {session_name!r} got {got_name!r})"
        )
    if window_id is not None and got_wid != window_id:
        raise TmuxTeamError(
            f"view target window_id mismatch "
            f"(expected {window_id!r} got {got_wid!r})"
        )
    if dead != "0":
        raise TmuxTeamError(f"view target pane is dead: {target}")
    try:
        pid = int(got_pid)
    except ValueError as exc:
        raise TmuxTeamError(f"invalid view target pane pid {got_pid!r}") from exc
    if pid <= 0:
        raise TmuxTeamError(f"non-positive view target pane pid {pid}")
    if expected_pid is not None and pid != expected_pid:
        raise TmuxTeamError(
            f"view target pane pid mismatch "
            f"(expected {expected_pid} got {pid})"
        )

    # Launch nonce: pane option first, then exact Team window (never session
    # fallback for inside/shared sessions — would race concurrent Teams).
    nonce: str | None = None
    pane_nonce = _tmux_run(
        ["show-options", "-p", "-v", "-t", target, "@omg_launch_nonce"],
        socket_path=socket_path,
    )
    if pane_nonce.returncode == 0:
        candidate = (pane_nonce.stdout or "").strip()
        if candidate:
            nonce = candidate
    if nonce is None and isinstance(window_id, str) and window_id:
        win_nonce = _tmux_run(
            ["show-options", "-w", "-v", "-t", window_id, "@omg_launch_nonce"],
            socket_path=socket_path,
        )
        if win_nonce.returncode == 0:
            candidate = (win_nonce.stdout or "").strip()
            if candidate:
                nonce = candidate
    if nonce is None and window_id is None:
        # Detached session-owned Teams may stamp session option only.
        sess_nonce = _tmux_run(
            ["show-options", "-v", "-t", session_id, "@omg_launch_nonce"],
            socket_path=socket_path,
        )
        if sess_nonce.returncode == 0:
            candidate = (sess_nonce.stdout or "").strip()
            if candidate:
                nonce = candidate
    if nonce != launch_nonce:
        raise TmuxTeamError(
            f"view target launch_nonce mismatch "
            f"(expected {launch_nonce!r} got {nonce!r})"
        )
    return {
        "pane_id": got_pane,
        "session_id": got_sid,
        "session_name": got_name,
        "window_id": got_wid,
        "pane_pid": pid,
        "launch_nonce": nonce,
    }


def execute_authorized_view(
    *,
    action: str,
    session_id: str,
    session_name: str,
    launch_nonce: str,
    window_id: str | None,
    pane_id: str,
    expected_pid: int | None = None,
    takeover: bool = False,
    socket_path: str | None = None,
) -> dict[str, Any]:
    """Identity-gated view effect: re-probe then select/switch/attach.

    ``ATTACH`` uses argv-only ``subprocess.run`` (shell=False) so the
    interactive session can return after detach; never holds a lifecycle lock.
    """
    action_u = str(action or "").upper()
    if action_u in {"NONE", "PRINT", "REFUSE"}:
        return {"executed": False, "action": action_u}

    prove_view_target_live(
        session_id=session_id,
        session_name=session_name,
        launch_nonce=launch_nonce,
        window_id=window_id,
        pane_id=pane_id,
        expected_pid=expected_pid,
        socket_path=socket_path,
    )

    if action_u == "SELECT":
        if isinstance(window_id, str) and window_id:
            select_window(window_id, socket_path=socket_path)
        focus_pane(pane_id, socket_path=socket_path)
        return {"executed": True, "action": action_u, "mode": "select"}

    if action_u == "SWITCH_CLIENT":
        switch_client(session_id, socket_path=socket_path)
        # Re-probe after switch (TOCTOU fence).
        prove_view_target_live(
            session_id=session_id,
            session_name=session_name,
            launch_nonce=launch_nonce,
            window_id=window_id,
            pane_id=pane_id,
            expected_pid=expected_pid,
            socket_path=socket_path,
        )
        if isinstance(window_id, str) and window_id:
            select_window(window_id, socket_path=socket_path)
        focus_pane(pane_id, socket_path=socket_path)
        return {"executed": True, "action": action_u, "mode": "switch-client"}

    if action_u == "ATTACH":
        argv = attach_argv_for_target(
            session_id=session_id,
            pane_id=pane_id,
            window_id=window_id,
            takeover=takeover,
            socket_path=socket_path,
        )
        # Final re-probe immediately before attach handoff.
        prove_view_target_live(
            session_id=session_id,
            session_name=session_name,
            launch_nonce=launch_nonce,
            window_id=window_id,
            pane_id=pane_id,
            expected_pid=expected_pid,
            socket_path=socket_path,
        )
        result = subprocess.run(argv, check=False, shell=False)
        if result.returncode != 0:
            raise TmuxTeamError(
                f"attach-session failed exit={result.returncode}"
            )
        return {
            "executed": True,
            "action": action_u,
            "mode": "attach",
            "attach_exit": result.returncode,
            "attach_argv": argv,
        }

    raise TmuxTeamError(f"unsupported view action {action_u!r}")
