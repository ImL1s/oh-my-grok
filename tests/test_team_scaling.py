"""Hermetic tests for team plane scaling + resume (D4).

Dry-run + FSM only — no live tmux/subprocess. Mirrors test_team_plane.py patterns.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from omg_cli.evidence import CLI_WRITER
from omg_cli.fanout import max_workers_cap
from omg_cli.state import create_run, load_run
from omg_cli.team import plane, scaling
from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    TeamError,
    TeamGateError,
    create_native_team,
    load_team_meta,
    prepare_native_spawn,
    start_team,
    team_meta_path,
)
from omg_cli.team.scaling import (
    STATUS_NEEDS_COLLECT,
    STATUS_RUNNING,
    STATUS_SCALED_DOWN,
    acquire_scale_lock,
    native_dispatch_plan,
    pending_identity_wal_operation,
    relaunch_dead_incomplete_workers,
    resume_team,
    scale_lock_path,
    scale_team,
)
from omg_cli.workers import WorkerError, ownership_manifest_path, worktree_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_OMG = REPO_ROOT / "bin" / "omg"
PYTHON = sys.executable

TASKS_TWO = [
    {"task_id": "t-a", "owned_files": ["a.py"]},
    {"task_id": "t-b", "owned_files": ["b.py"]},
]
TASKS_THREE = [
    {"task_id": "t-a", "owned_files": ["a.py"]},
    {"task_id": "t-b", "owned_files": ["b.py"]},
    {"task_id": "t-c", "owned_files": ["c.py"]},
]


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "omg-test@example.com")
    _git(path, "config", "user.name", "omg-test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / ".gitignore").write_text(".omg/\n", encoding="utf-8")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md", ".gitignore")
    _git(path, "commit", "-m", "initial")
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def _enable_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    for key in plane.WORKER_ENV_MARKERS:
        monkeypatch.delenv(key, raising=False)


def _boom_subprocess(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("subprocess must not be called in dry_run scale")


def _boom_tmux(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("tmux_available must not be called in dry_run scale")


def test_add_tmux_windows_records_primary_tmux_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        command = list(args)
        calls.append(command)
        if command[0] == "new-window":
            return MagicMock(returncode=0, stdout="2\t@12\t%22\n", stderr="")
        if command[0] == "display-message":
            return MagicMock(
                returncode=0,
                stdout=f"@12\t%22\tscale-2\t{'a' * 32}\n",
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _session: True)
    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(scaling.secrets, "token_hex", lambda _size: "a" * 32)

    record = {
        "task_id": "scale-2",
        "window_index": 2,
        "worktree": str(tmp_path / "scale-2"),
        "pane_command": "run-scale-2",
        "_env_pairs": [(plane.TEAM_WORKER_ID_ENV, "scale-2")],
    }
    scaling._add_tmux_windows(
        session="omg-workers",
        records=[record],
    )

    assert calls == [
        [
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{window_index}\t#{window_id}\t#{pane_id}",
            "-t",
            "omg-workers:2",
            "-n",
            f"scale-2-{'a' * 32}",
            "-c",
            str(tmp_path / "scale-2"),
            "-e",
            f"{plane.TEAM_WORKER_ID_ENV}=scale-2",
            "run-scale-2",
        ],
        [
            "if-shell",
            "-F",
            "-t",
            "@12",
            "#{&&:#{&&:#{&&:#{==:#{window_id},@12},#{==:#{pane_id},%22}},#{==:#{window_name},scale-2-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}},#{==:#{@omg_scale_nonce},}}",
            "set-window-option -t @12 @omg_scale_nonce aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ; rename-window -t @12 scale-2",
            "",
        ],
        [
            "display-message",
            "-p",
            "-t",
            "@12",
            "-F",
            "#{window_id}\t#{pane_id}\t#{window_name}\t#{@omg_scale_nonce}",
        ],
    ]
    assert record["window_index"] == 2
    assert record["window_id"] == "@12"
    assert record["pane_id"] == "%22"
    assert record["window_nonce"] == "a" * 32


@pytest.mark.parametrize(
    ("window_name", "marker", "expects_bind"),
    [
        (f"scale-2-{'a' * 32}", "", True),
        (f"scale-2-{'a' * 32}", "a" * 32, True),
        ("scale-2", "a" * 32, False),
    ],
)
def test_add_tmux_windows_adopts_each_exact_orphan_stage_without_relaunch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    window_name: str,
    marker: str,
    expects_bind: bool,
) -> None:
    calls: list[list[str]] = []

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        command = list(args)
        calls.append(command)
        if command[0] == "list-windows":
            return MagicMock(
                returncode=0,
                stdout=f"omg-workers\t$7\t@12\t%22\t{window_name}\t{marker}\t{'b' * 32}\n",
                stderr="",
            )
        if command[0] == "display-message" and command[-1] == "#{window_index}":
            return MagicMock(returncode=0, stdout="4\n", stderr="")
        if command[0] == "display-message":
            return MagicMock(
                returncode=0,
                stdout=f"@12\t%22\tscale-2\t{'a' * 32}\n",
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _session: True)
    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: ("omg-workers", "$7"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "b" * 32)
    record = {
        "task_id": "scale-2",
        "window_index": 2,
        "window_nonce": "a" * 32,
        "_launch_name": f"scale-2-{'a' * 32}",
        "_planned_window_index": 2,
        "_session_id": "$7",
        "_launch_nonce": "b" * 32,
        "worktree": str(tmp_path / "scale-2"),
        "pane_command": "run-scale-2",
    }

    scaling._add_tmux_windows(session="omg-workers", records=[record])

    assert not any(call[0] == "new-window" for call in calls)
    assert len([call for call in calls if call[0] == "if-shell"]) == int(
        expects_bind
    )
    assert record["window_id"] == "@12"
    assert record["pane_id"] == "%22"
    assert record["window_index"] == 4


def test_add_tmux_windows_rejects_duplicate_exact_orphans_without_relaunch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        calls.append(list(args))
        return MagicMock(
            returncode=0,
            stdout=(
                f"omg-workers\t$7\t@12\t%22\tscale-2-{'a' * 32}\t\t{'b' * 32}\n"
                f"omg-workers\t$7\t@13\t%23\tscale-2-{'a' * 32}\t{'a' * 32}\t{'b' * 32}\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _session: True)
    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: ("omg-workers", "$7"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "b" * 32)
    record = {
        "task_id": "scale-2",
        "window_index": 2,
        "window_nonce": "a" * 32,
        "_launch_name": f"scale-2-{'a' * 32}",
        "_session_id": "$7",
        "_launch_nonce": "b" * 32,
        "worktree": str(tmp_path / "scale-2"),
        "pane_command": "run-scale-2",
    }

    with pytest.raises(TeamError, match="ambiguous tmux orphan"):
        scaling._add_tmux_windows(session="omg-workers", records=[record])

    assert not any(call[0] == "new-window" for call in calls)


def test_add_tmux_windows_does_not_stamp_reused_foreign_window_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        command = list(args)
        calls.append(command)
        if command[0] == "list-windows" and "-a" not in command:
            return MagicMock(returncode=0, stdout="", stderr="")
        if command[0] == "if-shell" and command[command.index("-t") + 1] == "omg-workers":
            return MagicMock(returncode=0, stdout="2\t@12\t%22\n", stderr="")
        if command[0] == "display-message":
            return MagicMock(
                returncode=0,
                stdout="@12\t%99\tforeign\t\n",
                stderr="",
            )
        if command[0] == "list-windows":
            return MagicMock(returncode=0, stdout="@12\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _session: True)
    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: ("omg-workers", "$7"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "b" * 32)
    record = {
        "task_id": "scale-2",
        "window_index": 2,
        "window_nonce": "a" * 32,
        "_launch_name": f"scale-2-{'a' * 32}",
        "_session_id": "$7",
        "_launch_nonce": "b" * 32,
        "worktree": str(tmp_path / "scale-2"),
        "pane_command": "run-scale-2",
    }

    with pytest.raises(TeamError, match="ownership readback failed"):
        scaling._add_tmux_windows(session="omg-workers", records=[record])

    assert not any(call[0] == "set-window-option" for call in calls)
    binding = [
        call
        for call in calls
        if call[0] == "if-shell" and call[call.index("-t") + 1] == "@12"
    ][0]
    assert "#{==:#{window_id},@12}" in binding[4]
    assert "#{==:#{pane_id},%22}" in binding[4]
    assert f"#{{==:#{{window_name}},scale-2-{'a' * 32}}}" in binding[4]


def test_add_tmux_windows_adopts_ambiguous_create_result_without_duplicate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    state = {"created": False, "bound": False}

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        command = list(args)
        calls.append(command)
        if command[0] == "list-windows" and "-a" not in command:
            if not state["created"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            name = "scale-2" if state["bound"] else f"scale-2-{'a' * 32}"
            marker = "a" * 32 if state["bound"] else ""
            return MagicMock(
                returncode=0,
                stdout=f"omg-workers\t$7\t@12\t%22\t{name}\t{marker}\t{'b' * 32}\n",
                stderr="",
            )
        if command[0] == "if-shell" and command[command.index("-t") + 1] == "omg-workers":
            state["created"] = True
            return MagicMock(returncode=1, stdout="", stderr="lost result")
        if command[0] == "if-shell":
            state["bound"] = True
            return MagicMock(returncode=0, stdout="", stderr="")
        if command[0] == "display-message" and command[-1] == "#{window_index}":
            return MagicMock(returncode=0, stdout="2\n", stderr="")
        if command[0] == "display-message":
            return MagicMock(
                returncode=0,
                stdout=f"@12\t%22\tscale-2\t{'a' * 32}\n",
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _session: True)
    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: ("omg-workers", "$7"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "b" * 32)
    record = {
        "task_id": "scale-2",
        "window_index": 2,
        "window_nonce": "a" * 32,
        "_launch_name": f"scale-2-{'a' * 32}",
        "_session_id": "$7",
        "_launch_nonce": "b" * 32,
        "worktree": str(tmp_path / "scale-2"),
        "pane_command": "run-scale-2",
    }

    scaling._add_tmux_windows(session="omg-workers", records=[record])

    creates = [
        call
        for call in calls
        if call[0] == "if-shell" and call[call.index("-t") + 1] == "omg-workers"
    ]
    assert len(creates) == 1
    assert record["window_id"] == "@12"
    assert record["pane_id"] == "%22"


def test_add_tmux_windows_collision_fallback_records_actual_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    responses = iter(
        [
            MagicMock(returncode=1, stdout="", stderr="index in use"),
            MagicMock(returncode=0, stdout="2\n", stderr=""),
            MagicMock(returncode=0, stdout="0\n1\n2\n6\n", stderr=""),
            MagicMock(returncode=0, stdout="7\t@17\t%27\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(
                returncode=0,
                stdout=f"@17\t%27\tscale-2\t{'a' * 32}\n",
                stderr="",
            ),
        ]
    )

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        calls.append(list(args))
        return next(responses)

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _session: True)
    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(scaling.secrets, "token_hex", lambda _size: "a" * 32)
    record = {
        "task_id": "scale-2",
        "window_index": 2,
        "worktree": str(tmp_path / "scale-2"),
        "pane_command": "run-scale-2",
        "_env_pairs": [(plane.TEAM_WORKER_ID_ENV, "scale-2")],
    }

    scaling._add_tmux_windows(session="omg-workers", records=[record])

    assert calls[1] == [
        "display-message",
        "-p",
        "-t",
        "omg-workers:2",
        "#{window_index}",
    ]
    assert calls[2] == [
        "list-windows",
        "-t",
        "omg-workers",
        "-F",
        "#{window_index}",
    ]
    assert calls[3][calls[3].index("-t") + 1] == "omg-workers:7"
    assert record["window_index"] == 7
    assert record["window_id"] == "@17"
    assert record["pane_id"] == "%27"


def test_add_tmux_windows_keeps_batch_monotonic_after_collision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    responses = iter(
        [
            MagicMock(returncode=1, stdout="", stderr="index in use"),
            MagicMock(returncode=0, stdout="2\n", stderr=""),
            MagicMock(returncode=0, stdout="0\n1\n2\n6\n", stderr=""),
            MagicMock(returncode=0, stdout="7\t@17\t%27\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(
                returncode=0,
                stdout=f"@17\t%27\tscale-2\t{'a' * 32}\n",
                stderr="",
            ),
            MagicMock(returncode=0, stdout="8\t@18\t%28\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(
                returncode=0,
                stdout=f"@18\t%28\tscale-3\t{'a' * 32}\n",
                stderr="",
            ),
        ]
    )

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        calls.append(list(args))
        return next(responses)

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _session: True)
    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(scaling.secrets, "token_hex", lambda _size: "a" * 32)
    records = [
        {
            "task_id": "scale-2",
            "window_index": 2,
            "worktree": str(tmp_path / "scale-2"),
            "pane_command": "run-scale-2",
            "_env_pairs": [],
        },
        {
            "task_id": "scale-3",
            "window_index": 3,
            "worktree": str(tmp_path / "scale-3"),
            "pane_command": "run-scale-3",
            "_env_pairs": [],
        },
    ]

    scaling._add_tmux_windows(session="omg-workers", records=records)

    new_targets = [
        call[call.index("-t") + 1] for call in calls if call[0] == "new-window"
    ]
    assert new_targets == ["omg-workers:2", "omg-workers:7", "omg-workers:8"]
    assert [record["window_index"] for record in records] == [7, 8]


def test_add_tmux_windows_does_not_fallback_without_confirmed_collision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    responses = iter(
        [
            MagicMock(returncode=1, stdout="", stderr="permission denied"),
            MagicMock(returncode=1, stdout="", stderr="missing target"),
        ]
    )

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        calls.append(list(args))
        return next(responses)

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _session: True)
    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)

    with pytest.raises(TeamError, match="permission denied"):
        scaling._add_tmux_windows(
            session="omg-workers",
            records=[
                {
                    "task_id": "scale-2",
                    "window_index": 2,
                    "worktree": str(tmp_path / "scale-2"),
                    "pane_command": "run-scale-2",
                    "_env_pairs": [],
                }
            ],
        )

    assert [call[0] for call in calls] == ["new-window", "display-message"]


@pytest.mark.parametrize(
    "stdout",
    [
        "3\t@13\t%23\n",
        "2\tbad-window\t%22\n",
        "2\t@12\tbad-pane\n",
        "2\t@12\t%22\nextra\n",
    ],
)
def test_add_tmux_windows_primary_malformed_output_fails_closed(
    stdout: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        calls.append(list(args))
        return MagicMock(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _session: True)
    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)

    with pytest.raises(TeamError, match="exact window/pane identity"):
        scaling._add_tmux_windows(
            session="omg-workers",
            records=[
                {
                    "task_id": "scale-2",
                    "window_index": 2,
                    "worktree": str(tmp_path / "scale-2"),
                    "pane_command": "run-scale-2",
                    "_env_pairs": [],
                }
            ],
        )

    assert calls[0][0] == "new-window"
    assert all(call[0] != "new-window" for call in calls[1:])


def test_add_tmux_windows_malformed_recovery_refuses_foreign_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    responses = iter(
        [
            MagicMock(returncode=0, stdout="malformed\n", stderr=""),
            MagicMock(
                returncode=0,
                stdout="2\t@99\t%99\tscale-2\n",
                stderr="",
            ),
        ]
    )

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        calls.append(list(args))
        return next(responses)

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _session: True)
    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(scaling.secrets, "token_hex", lambda _size: "b" * 32)

    with pytest.raises(TeamError, match="cleanup handle unavailable"):
        scaling._add_tmux_windows(
            session="omg-workers",
            records=[
                {
                    "task_id": "scale-2",
                    "window_index": 2,
                    "worktree": str(tmp_path / "scale-2"),
                    "pane_command": "run-scale-2",
                    "_env_pairs": [],
                }
            ],
        )

    assert all(call[0] != "kill-window" for call in calls)


def test_add_tmux_windows_malformed_success_refuses_replacement_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        calls.append(list(args))
        if args[0] == "list-windows":
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[0] == "if-shell":
            return MagicMock(returncode=0, stdout="malformed\n", stderr="")
        return MagicMock(
            returncode=0,
            stdout=(
                f"2\t@12\t%22\tscale-2-{'a' * 32}\t"
                f"omg-workers\t$99\t{'a' * 32}\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _session: True)
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: ("omg-workers", "$7"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "a" * 32)
    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)

    with pytest.raises(TeamError, match="cleanup handle unavailable"):
        scaling._add_tmux_windows(
            session="omg-workers",
            records=[
                {
                    "task_id": "scale-2",
                    "window_index": 2,
                    "worktree": str(tmp_path / "scale-2"),
                    "pane_command": "run-scale-2",
                    "window_nonce": "a" * 32,
                    "_launch_name": f"scale-2-{'a' * 32}",
                    "_session_id": "$7",
                    "_launch_nonce": "a" * 32,
                    "_env_pairs": [],
                }
            ],
        )

    assert all(call[0] != "kill-window" for call in calls)


def test_add_tmux_windows_binding_failure_rolls_back_owned_window_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    responses = iter(
        [
            MagicMock(returncode=0, stdout="2\t@12\t%22\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="binding refused"),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
    )

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        calls.append(list(args))
        return next(responses)

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _session: True)
    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(scaling.secrets, "token_hex", lambda _size: "a" * 32)

    with pytest.raises(TeamError, match="binding refused"):
        scaling._add_tmux_windows(
            session="omg-workers",
            records=[
                {
                    "task_id": "scale-2",
                    "window_index": 2,
                    "worktree": str(tmp_path / "scale-2"),
                    "pane_command": "run-scale-2",
                    "_env_pairs": [],
                }
            ],
        )

    assert [call[0] for call in calls] == [
        "new-window",
        "if-shell",
        "if-shell",
        "list-windows",
    ]


def test_add_tmux_windows_rolls_back_prior_window_when_later_create_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    responses = iter(
        [
            MagicMock(returncode=0, stdout="2\t@12\t%22\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(
                returncode=0,
                stdout=f"@12\t%22\tscale-2\t{'a' * 32}\n",
                stderr="",
            ),
            MagicMock(returncode=1, stdout="", stderr="permission denied"),
            MagicMock(returncode=1, stdout="", stderr="missing target"),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
    )

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        calls.append(list(args))
        return next(responses)

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _session: True)
    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(scaling.secrets, "token_hex", lambda _size: "a" * 32)

    with pytest.raises(TeamError, match="permission denied"):
        scaling._add_tmux_windows(
            session="omg-workers",
            records=[
                {
                    "task_id": "scale-2",
                    "window_index": 2,
                    "worktree": str(tmp_path / "scale-2"),
                    "pane_command": "run-scale-2",
                    "_env_pairs": [],
                },
                {
                    "task_id": "scale-3",
                    "window_index": 3,
                    "worktree": str(tmp_path / "scale-3"),
                    "pane_command": "run-scale-3",
                    "_env_pairs": [],
                },
            ],
        )

    assert [call[:2] for call in calls[-3:]] == [
        ["display-message", "-p"],
        ["if-shell", "-F"],
        ["list-windows", "-a"],
    ]
    assert calls[-2][-2] == "kill-window -t @12"


def test_rollback_created_tmux_windows_uses_reverse_creation_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = {
        "@17": ("%27", "scale-2", "a" * 32),
        "@18": ("%28", "scale-3", "b" * 32),
    }
    killed: list[str] = []
    calls: list[list[str]] = []

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        command = list(args)
        calls.append(command)
        if command[0] == "list-windows":
            return MagicMock(
                returncode=0,
                stdout="".join(f"{window_id}\n" for window_id in live),
                stderr="",
            )
        target = command[command.index("-t") + 1]
        if command[0] == "if-shell":
            killed.append(target)
            live.pop(target)
            return MagicMock(returncode=0, stdout="", stderr="")
        if target not in live:
            return MagicMock(returncode=1, stdout="", stderr="missing target")
        if command[-1] == "#{window_id}\t#{pane_id}":
            return MagicMock(
                returncode=0,
                stdout=f"{target}\t{live[target][0]}\n",
                stderr="",
            )
        pane_id, window_name, nonce = live[target]
        return MagicMock(
            returncode=0,
            stdout=f"{target}\t{pane_id}\t{window_name}\t{nonce}\n",
            stderr="",
        )

    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)

    errors = scaling._rollback_created_tmux_windows(
        [
            {
                "task_id": "scale-2",
                "window_id": "@17",
                "pane_id": "%27",
                "window_nonce": "a" * 32,
                "_session_name": "omg-workers",
                "_session_id": "$7",
                "_launch_nonce": "c" * 32,
            },
            {
                "task_id": "scale-3",
                "window_id": "@18",
                "pane_id": "%28",
                "window_nonce": "b" * 32,
            },
        ]
    )

    assert errors == []
    assert killed == ["@18", "@17"]
    scale_2_cleanup = next(
        call for call in calls if call[0] == "if-shell" and call[3] == "@17"
    )
    assert "#{==:#{session_name},omg-workers}" in scale_2_cleanup[4]
    assert "#{==:#{session_id},$7}" in scale_2_cleanup[4]
    assert f"#{{==:#{{@omg_launch_nonce}},{'c' * 32}}}" in scale_2_cleanup[4]


def test_rollback_created_tmux_windows_preserves_unknown_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            MagicMock(returncode=1, stdout="", stderr="transport error"),
            MagicMock(returncode=1, stdout="", stderr="server unavailable"),
        ]
    )
    calls: list[list[str]] = []

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        calls.append(list(args))
        return next(responses)

    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)

    errors = scaling._rollback_created_tmux_windows(
        [
            {
                "task_id": "scale-2",
                "window_id": "@17",
                "pane_id": "%27",
                "window_nonce": "a" * 32,
            }
        ]
    )

    assert len(errors) == 1
    assert "disappearance unverified" in errors[0]
    assert all(call[0] != "kill-window" for call in calls)


def test_rollback_created_tmux_windows_refuses_reused_foreign_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        calls.append(list(args))
        return MagicMock(
            returncode=0,
            stdout=f"@17\t%27\tforeign\t{'b' * 32}\n",
            stderr="",
        )

    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)

    errors = scaling._rollback_created_tmux_windows(
        [
            {
                "task_id": "scale-2",
                "window_id": "@17",
                "pane_id": "%27",
                "window_nonce": "a" * 32,
            }
        ]
    )

    assert len(errors) == 1
    assert "tmux rollback identity mismatch task=scale-2" in errors[0]
    assert all(call[0] != "kill-window" for call in calls)


def test_rollback_created_tmux_windows_refuses_unverified_disappearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            MagicMock(
                returncode=0,
                stdout=f"@17\t%27\tscale-2\t{'a' * 32}\n",
                stderr="",
            ),
            MagicMock(returncode=1, stdout="", stderr="kill transport error"),
            MagicMock(returncode=1, stdout="", stderr="server unavailable"),
        ]
    )

    monkeypatch.setattr(
        scaling,
        "_tmux_run",
        lambda _args, **_kwargs: next(responses),
    )

    errors = scaling._rollback_created_tmux_windows(
        [
            {
                "task_id": "scale-2",
                "window_id": "@17",
                "pane_id": "%27",
                "window_nonce": "a" * 32,
            }
        ]
    )

    assert len(errors) == 1
    assert "disappearance unverified" in errors[0]


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        (f"$7\t7\t@17\t%27\t10002\t{'c' * 32}\t0\n", 0, 10002),
        (f"$8\t7\t@17\t%27\t10002\t{'c' * 32}\t0\n", 0, None),
        (f"$7\t8\t@17\t%27\t10002\t{'c' * 32}\t0\n", 0, None),
        (f"$7\t7\t@18\t%27\t10002\t{'c' * 32}\n", 0, None),
        (f"$7\t7\t@17\t%28\t10002\t{'c' * 32}\n", 0, None),
        (f"$7\t7\t@17\t%27\t10002\t{'d' * 32}\t0\n", 0, None),
        (f"$7\t7\t@17\t%27\tnot-a-pid\t{'c' * 32}\t0\n", 0, None),
        (f"$7\t7\t@17\t%27\t0\t{'c' * 32}\t0\n", 0, None),
        (f"$7\t7\t@17\t%27\t10002\t{'c' * 32}\t1\n", 0, None),
        (f"$7\t7\t@17\t%27\t10002\t{'c' * 32}\t0\nextra\n", 0, None),
        ("", 1, None),
    ],
)
def test_read_scaled_pane_pid_requires_exact_tmux_identity(
    stdout: str,
    returncode: int,
    expected: int | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        calls.append(list(args))
        return MagicMock(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)

    actual = scaling._read_scaled_pane_pid(
        session_id="$7",
        record={
            "window_index": 7,
            "window_id": "@17",
            "window_nonce": "c" * 32,
            "pane_id": "%27",
        },
    )

    assert actual == expected
    assert calls == [
        [
            "display-message",
            "-p",
            "-t",
            "%27",
            "-F",
            "#{session_id}\t#{window_index}\t#{window_id}\t#{pane_id}\t#{pane_pid}"
            "\t#{@omg_scale_nonce}\t#{pane_dead}",
        ]
    ]


def test_kill_pane_recorded_uses_atomic_receipt_bound_tmux_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = {
        "task_id": "scale-2",
        "window_index": 7,
        "window_id": "@17",
        "window_nonce": "c" * 32,
        "pane_id": "%27",
        "pid": 10002,
        "pgid": 20002,
        "pid_start": "start-10002",
    }
    authority = {"session_id": "$7", "launch_nonce": "a" * 32}
    signals: list[tuple[int, int]] = []
    calls: list[list[str]] = []

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: ("omg-workers", "$7"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "a" * 32)
    monkeypatch.setattr(
        scaling,
        "_read_recorded_tmux_pane",
        lambda _rec, **_kwargs: ("@17", "%27"),
    )
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: "start-10002")
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: 20002)
    monkeypatch.setattr(
        scaling.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, int(sig))),
    )

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        command = list(args)
        calls.append(command)
        if command[0] == "if-shell":
            # Model a server restart after the final client-side read: the
            # receipt predicate is false, so tmux must not execute kill-pane.
            return MagicMock(returncode=0, stdout="", stderr="")
        if command[0] == "list-panes":
            return MagicMock(returncode=0, stdout="%27\n", stderr="")
        raise AssertionError(f"unexpected tmux call: {command}")

    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    actions: list[str] = []
    errors: list[str] = []
    signalled: list[dict[str, Any]] = []

    scaling._kill_pane_recorded(
        record,
        session="omg-workers",
        dry=False,
        actions=actions,
        errors=errors,
        signalled=signalled,
        authority=authority,
    )

    assert signals == [(20002, int(signal.SIGTERM))]
    assert signalled == [{"task_id": "scale-2", "pgid": 20002, "pid": 10002}]
    assert errors == ["tmux kill-pane task=scale-2 pane=%27; still present: "]
    conditional = next(call for call in calls if call[0] == "if-shell")
    predicate = conditional[4]
    assert conditional[-2] == "kill-pane -t %27"
    assert "#{==:#{session_id},$7}" in predicate
    assert "#{==:#{@omg_launch_nonce}," + "a" * 32 + "}" in predicate
    assert "#{==:#{window_id},@17}" in predicate
    assert "#{==:#{pane_id},%27}" in predicate
    assert "#{==:#{pane_pid},10002}" in predicate
    assert "#{==:#{@omg_scale_nonce}," + "c" * 32 + "}" in predicate
    assert all(call[0] not in {"kill-pane", "kill-window"} for call in calls)


def test_read_recorded_tmux_pane_refuses_scale_nonce_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        calls.append(list(args))
        return MagicMock(
            returncode=0,
            stdout=f"omg-workers\t$7\t7\t0\t@17\t%27\t10002\t{'d' * 32}\t0\n",
            stderr="",
        )

    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    actual = scaling._read_recorded_tmux_pane(
        {
            "window_index": 7,
            "window_id": "@17",
            "window_nonce": "c" * 32,
            "pane_id": "%27",
            "pid": 10002,
        },
        session="omg-workers",
        session_id="$7",
    )

    assert actual is None
    assert len(calls) == 1


def test_read_recorded_tmux_pane_accepts_split_topology_pane_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        scaling,
        "_tmux_run",
        lambda *_args, **_kwargs: MagicMock(
            returncode=0,
            # window_index remains 0 while pane_index identifies task slot 1.
            # trailing fields: scale_nonce empty, pane_dead=0 (alive)
            stdout="omg-workers\t$7\t0\t1\t@0\t%1\t10002\t\t0\n",
            stderr="",
        ),
    )

    assert scaling._read_recorded_tmux_pane(
        {
            "window_index": 1,
            "window_id": None,
            "window_nonce": None,
            "pane_id": "%1",
            "pid": 10002,
        },
        session="omg-workers",
        session_id="$7",
    ) == ("@0", "%1")


def _prepare_live_scale_team(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[str, dict[str, Any]]:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team(
        "live scale identity",
        TASKS_TWO,
        root=tmp_path,
        dry_run=True,
        team_id="team-1",
        owner_token="owner-1",
    )
    rid = meta["run_id"]
    live = dict(load_team_meta(tmp_path, rid))
    live["dry_run"] = False
    live["session"] = "omg-workers"
    live["tasks"] = [
        {
            **task,
            "pane_id": f"%{index + 10}",
            "pid": 10000 + index,
            "pgid": 20000 + index,
            "pid_start": f"start-{10000 + index}",
            "status": STATUS_RUNNING,
        }
        for index, task in enumerate(live["tasks"])
    ]
    _receipt, receipt_hash = plane._persist_team_launch_receipt(
        tmp_path,
        rid,
        session=live["session"],
        session_id="$7",
        launch_nonce="a" * 32,
        tasks=live["tasks"],
    )
    live.update(
        {
            "launch_nonce": "a" * 32,
            "launch_receipt_sha256": receipt_hash,
            "identity_generation": 0,
            "identity_receipt_sha256": receipt_hash,
        }
    )
    _write_team_meta(tmp_path, rid, live)
    return rid, live


def _prepare_live_relaunch_team(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    dead_tasks: int = 1,
) -> tuple[str, dict[str, Any], dict[str, str], list[str]]:
    rid, live = _prepare_live_scale_team(monkeypatch, tmp_path)
    live["topology"] = "split"
    live["window_id"] = "@7"
    _write_team_meta(tmp_path, rid, live)
    dead_ids = {str(row["task_id"]) for row in live["tasks"][:dead_tasks]}
    pane_to_task = {str(row["pane_id"]): str(row["task_id"]) for row in live["tasks"]}
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: (live["session"], "$7"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "a" * 32)
    monkeypatch.setattr(scaling, "_session_alive", lambda _session: True)
    monkeypatch.setattr(scaling, "_resolve_relaunch_target", lambda *_args, **_kwargs: "@7")
    monkeypatch.setattr(
        scaling,
        "_worker_api_tasks_terminal",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(scaling, "_worktree_dirty", lambda _path: False)
    monkeypatch.setattr(
        "omg_cli.team.tmux.pane_alive",
        lambda pane: pane_to_task.get(pane) not in dead_ids,
    )
    monkeypatch.setattr(scaling, "_resync_window_indices", lambda *_args: None)
    launched: dict[str, str] = {}
    respawns: list[str] = []

    def discover(**kwargs: Any) -> str | None:
        return launched.get(str(kwargs["task_id"]))

    def respawn(**kwargs: Any) -> str:
        task_id = next(
            tid for tid in dead_ids if tid in str(kwargs["pane_command"])
        )
        respawns.append(task_id)
        pane_id = f"%{80 + len(respawns)}"
        launched[task_id] = pane_id
        return pane_id

    monkeypatch.setattr(scaling, "_discover_relaunch_pane", discover)
    monkeypatch.setattr(scaling, "_wait_for_relaunch_pane", discover)
    monkeypatch.setattr("omg_cli.team.tmux.respawn_worker_pane", respawn)
    monkeypatch.setattr(
        scaling,
        "_read_exact_relaunch_pane",
        lambda pane_id, **_kwargs: 50000 + int(pane_id.removeprefix("%")) - 80,
    )
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda pid: pid + 10000)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda pid: f"start-{pid}")
    return rid, live, launched, respawns


def test_scale_up_persists_actual_tmux_identity_and_scale_down_uses_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, live = _prepare_live_scale_team(monkeypatch, tmp_path)

    launched: list[dict[str, Any]] = []

    def capture_add(*, session: str, records: Any) -> None:
        assert session == "omg-workers"
        for record in records:
            record["window_index"] = 7
            record["window_id"] = "@17"
            record["window_nonce"] = "c" * 32
            record["pane_id"] = "%27"
            launched.append(dict(record))

    monkeypatch.setattr(scaling, "_add_tmux_windows", capture_add)
    monkeypatch.setattr(
        scaling,
        "_read_scaled_pane_pid",
        lambda **_kwargs: 10002,
        raising=False,
    )
    monkeypatch.setattr(
        scaling, "_list_pane_identities", lambda _session: {7: ("%27", 10002)}
    )
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: 20002)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: "start-10002")

    out = scale_team(tmp_path, rid, add=1)

    assert out["task_ids"] == ["scale-2"]
    assert out["window_indices"] == [7]
    assert out["next_worker_index"] == 8
    assert len(launched) == 1
    env = dict(launched[0]["_env_pairs"])
    assert env[plane.TEAM_RUN_ID_ENV] == rid
    assert env[plane.TEAM_ID_ENV] == "team-1"
    assert env[plane.TEAM_WORKER_ID_ENV] == "scale-2"
    assert env[plane.TEAM_LEADER_ROOT_ENV] == str(tmp_path.resolve())
    assert env[plane.TEAM_STATE_ROOT_ENV] == str(
        (tmp_path / ".omg" / "state").resolve()
    )
    assert env[plane.TEAM_OWNER_TOKEN_ENV] == "owner-1"
    disk = load_team_meta(tmp_path, rid)
    scaled = next(task for task in disk["tasks"] if task["task_id"] == "scale-2")
    assert "_env_pairs" not in scaled
    assert scaled["window_index"] == 7
    assert scaled["window_id"] == "@17"
    assert scaled["window_nonce"] == "c" * 32
    assert scaled["pane_id"] == "%27"
    receipt = json.loads(
        plane.team_identity_receipt_path(tmp_path, rid, 1).read_text(encoding="utf-8")
    )
    receipt_scaled = next(
        task for task in receipt["tasks_after"] if task["task_id"] == "scale-2"
    )
    wal_body = (plane.team_dir(tmp_path, rid) / "scale-wal" / "1.json").read_bytes()
    assert receipt["scale_intent"]["scale_wal_sha256"] == hashlib.sha256(
        wal_body
    ).hexdigest()
    assert receipt_scaled["window_index"] == 7
    assert receipt_scaled["window_id"] == "@17"
    assert receipt_scaled["window_nonce"] == "c" * 32
    assert receipt_scaled["pane_id"] == "%27"

    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: (live["session"], "$7"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "a" * 32)
    monkeypatch.setattr(
        scaling,
        "_list_pane_identities",
        lambda _session: {0: ("%10", 10000), 1: ("%11", 10001), 7: ("%27", 10002)},
    )
    monkeypatch.setattr(
        scaling,
        "_pid_start_identity",
        lambda pid: f"start-{pid}",
    )
    monkeypatch.setattr(
        scaling,
        "_pgid_for_pid",
        lambda pid: {10000: 20000, 10001: 20001, 10002: 20002}[pid],
    )
    monkeypatch.setattr(scaling.os, "killpg", lambda _pgid, _sig: None)
    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    tmux_calls: list[list[str]] = []
    pane_killed = False

    def capture_tmux(args: Any, **_kwargs: Any) -> MagicMock:
        nonlocal pane_killed
        tmux_calls.append(list(args))
        command = list(args)
        if command[0] == "display-message":
            return MagicMock(
                returncode=0,
                stdout=(f"omg-workers\t$7\t7\t0\t@17\t%27\t10002\t{'c' * 32}\t0\n"),
                stderr="",
            )
        if command[0] == "if-shell":
            pane_killed = True
            return MagicMock(returncode=0, stdout="", stderr="")
        if command[0] == "list-panes":
            panes = ["%10", "%11"] + ([] if pane_killed else ["%27"])
            return MagicMock(
                returncode=0,
                stdout="".join(f"{pane}\n" for pane in panes),
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaling, "_tmux_run", capture_tmux)

    removed = scale_team(tmp_path, rid, remove=1)

    assert removed["task_ids"] == ["scale-2"]
    conditional_kills = [call for call in tmux_calls if call[0] == "if-shell"]
    assert len(conditional_kills) == 1
    assert conditional_kills[0][-2] == "kill-pane -t %27"
    assert "#{==:#{window_id},@17}" in conditional_kills[0][4]
    assert "#{==:#{@omg_scale_nonce}," + "c" * 32 + "}" in conditional_kills[0][4]
    assert all(call[:2] != ["kill-window", "-t"] for call in tmux_calls)


def test_scale_identity_chain_refuses_team_meta_window_handle_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, live = _prepare_live_scale_team(monkeypatch, tmp_path)
    before = list(live["tasks"])
    scaled = {
        **before[-1],
        "task_id": "scale-2",
        "window_index": 7,
        "window_id": "@17",
        "window_nonce": "c" * 32,
        "pane_id": "%27",
        "pid": 10002,
        "pgid": 20002,
        "pid_start": "start-10002",
    }
    after = [*before, scaled]
    _receipt, head = plane._persist_team_identity_receipt(
        tmp_path,
        rid,
        session=live["session"],
        session_id="$7",
        launch_nonce="a" * 32,
        generation=1,
        previous_receipt_sha256=live["launch_receipt_sha256"],
        operation="add",
        tasks_before=before,
        tasks_after=after,
    )
    drifted = dict(live)
    drifted["tasks"] = [dict(task) for task in after]
    drifted["tasks"][-1]["window_nonce"] = "d" * 32
    drifted["identity_generation"] = 1
    drifted["identity_receipt_sha256"] = head
    _write_team_meta(tmp_path, rid, drifted)

    with pytest.raises(TeamError, match="active identities differ"):
        plane._load_team_identity_chain(tmp_path, rid, drifted)


def test_scale_up_refuses_pane_identity_replacement_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live = _prepare_live_scale_team(monkeypatch, tmp_path)

    def capture_add(*, session: str, records: Any) -> None:
        assert session == "omg-workers"
        records[0]["window_index"] = 7
        records[0]["window_id"] = "@17"
        records[0]["window_nonce"] = "c" * 32
        records[0]["pane_id"] = "%27"

    rollbacks: list[list[str]] = []

    def capture_rollback(records: Any) -> list[str]:
        rollbacks.append([str(record["window_id"]) for record in records])
        return []

    monkeypatch.setattr(scaling, "_add_tmux_windows", capture_add)
    monkeypatch.setattr(
        scaling,
        "_read_scaled_pane_pid",
        lambda **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        scaling, "_list_pane_identities", lambda _session: {7: ("%99", 10002)}
    )
    monkeypatch.setattr(
        scaling,
        "_rollback_created_tmux_windows",
        capture_rollback,
        raising=False,
    )
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: 20002)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: "start-10002")

    with pytest.raises(TeamError, match="complete worker identity"):
        scale_team(tmp_path, rid, add=1)

    assert rollbacks == [["@17"]]
    assert load_team_meta(tmp_path, rid)["identity_generation"] == 0


def test_scale_up_publishes_durable_wal_before_preparation_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, live = _prepare_live_scale_team(monkeypatch, tmp_path)
    observed: dict[str, Any] = {}

    def stop_at_prepare(_root: Path, _run_id: str, _task_id: str) -> None:
        wal_path = plane.team_dir(tmp_path, rid) / "scale-wal" / "1.json"
        observed["body"] = wal_path.read_bytes()
        raise WorkerError("stop after WAL")

    monkeypatch.setattr(scaling, "prepare_task", stop_at_prepare)

    with pytest.raises(TeamError, match="stop after WAL"):
        scale_team(tmp_path, rid, add=1)

    wal = json.loads(observed["body"])
    assert wal["store_kind"] == "team_scale_wal"
    assert wal["run_id"] == rid
    assert wal["generation"] == 1
    assert wal["session_name"] == live["session"]
    assert wal["session_id"] == "$7"
    assert wal["launch_nonce"] == "a" * 32
    assert wal["base_identity_generation"] == 0
    assert wal["base_receipt_sha256"] == live["identity_receipt_sha256"]
    assert len(wal["request_sha256"]) == 64
    assert wal["tasks"] == [
        {
            "launch_name": f"scale-2-{wal['tasks'][0]['window_nonce']}",
            "planned_window_index": 2,
            "scaled_in_at": wal["tasks"][0]["scaled_in_at"],
            "task_id": "scale-2",
            "window_nonce": wal["tasks"][0]["window_nonce"],
        }
    ]


def test_scale_up_wal_publish_result_loss_adopts_exact_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.contracts import path_keys

    rid, _live = _prepare_live_scale_team(monkeypatch, tmp_path)
    real_atomic_write = path_keys.atomic_write_bytes

    def publish_then_lose(path: Path, body: bytes, **kwargs: Any) -> Path:
        result = real_atomic_write(path, body, **kwargs)
        if Path(path).parent.name == "scale-wal":
            raise OSError("simulated WAL result loss")
        return result

    def stop_after_publish(*_args: Any, **_kwargs: Any) -> None:
        raise WorkerError("stop after exact WAL adoption")

    monkeypatch.setattr(path_keys, "atomic_write_bytes", publish_then_lose)
    monkeypatch.setattr(scaling, "prepare_task", stop_after_publish)

    with pytest.raises(TeamError, match="exact WAL adoption"):
        scale_team(tmp_path, rid, add=1)

    wal_path = plane.team_dir(tmp_path, rid) / "scale-wal" / "1.json"
    assert wal_path.is_file()
    assert json.loads(wal_path.read_bytes())["request"]["operation"] == "add"


def test_scale_up_changed_pre_receipt_retry_rejects_before_new_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live = _prepare_live_scale_team(monkeypatch, tmp_path)
    prepares = 0

    def stop_first_prepare(*_args: Any, **_kwargs: Any) -> None:
        nonlocal prepares
        prepares += 1
        raise WorkerError("simulated preparation crash")

    monkeypatch.setattr(scaling, "prepare_task", stop_first_prepare)
    with pytest.raises(TeamError, match="preparation crash"):
        scale_team(tmp_path, rid, add=1)

    with pytest.raises(TeamError, match="retry intent differs"):
        scale_team(tmp_path, rid, add=1, extra=["--changed"])

    assert prepares == 1


def test_pending_add_wal_blocks_scale_down_and_resume_generation_takeover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live = _prepare_live_scale_team(monkeypatch, tmp_path)

    def crash_after_wal(*_args: Any, **_kwargs: Any) -> None:
        raise WorkerError("simulated crash after WAL")

    monkeypatch.setattr(scaling, "prepare_task", crash_after_wal)
    with pytest.raises(TeamError, match="crash after WAL"):
        scale_team(tmp_path, rid, add=1)

    before = load_team_meta(tmp_path, rid)
    with pytest.raises(TeamError, match="scale-down refused while scale-up WAL"):
        scale_team(tmp_path, rid, remove=1)
    with pytest.raises(TeamError, match="resume refused while scale-up WAL"):
        resume_team(tmp_path, rid)

    after = load_team_meta(tmp_path, rid)
    assert after["identity_generation"] == before["identity_generation"] == 0
    assert after["meta_generation"] == before["meta_generation"]


def test_future_identity_receipt_blocks_scale_down_and_raw_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, live = _prepare_live_scale_team(monkeypatch, tmp_path)
    plane._persist_team_identity_receipt(
        tmp_path,
        rid,
        session=live["session"],
        session_id="$7",
        launch_nonce="a" * 32,
        generation=1,
        previous_receipt_sha256=live["identity_receipt_sha256"],
        operation="add",
        tasks_before=live["tasks"],
        tasks_after=live["tasks"],
    )

    before = load_team_meta(tmp_path, rid)
    with pytest.raises(TeamError, match="scale-down refused while identity receipt"):
        scale_team(tmp_path, rid, remove=1)
    with pytest.raises(TeamError, match="resume refused while identity receipt"):
        resume_team(tmp_path, rid)

    after = load_team_meta(tmp_path, rid)
    assert after == before


def test_pending_add_wal_blocks_dry_run_without_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live = _prepare_live_scale_team(monkeypatch, tmp_path)
    prepares = 0

    def crash_after_wal(*_args: Any, **_kwargs: Any) -> None:
        nonlocal prepares
        prepares += 1
        raise WorkerError("simulated crash after WAL")

    monkeypatch.setattr(scaling, "prepare_task", crash_after_wal)
    with pytest.raises(TeamError, match="crash after WAL"):
        scale_team(tmp_path, rid, add=1)

    state_root = tmp_path / ".omg"
    before = {
        path.relative_to(state_root): path.read_bytes()
        for path in state_root.rglob("*")
        if path.is_file()
    }

    with pytest.raises(TeamError, match="dry-run scale-up refused while scale-up WAL"):
        scale_team(tmp_path, rid, add=1, dry_run=True)

    after = {
        path.relative_to(state_root): path.read_bytes()
        for path in state_root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert prepares == 1


def test_pending_add_wal_exact_retry_ignores_later_cap_reduction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live = _prepare_live_scale_team(monkeypatch, tmp_path)
    state = {"present": False, "creates": 0, "calls": 0, "crashed": False}

    def launch_or_adopt(*, session: str, records: Any) -> None:
        assert session == "omg-workers"
        state["calls"] += 1
        if not state["present"]:
            state["present"] = True
            state["creates"] += 1
        records[0].update(
            {
                "window_index": 7,
                "window_id": "@17",
                "pane_id": "%27",
            }
        )

    def crash_once_after_window(**_kwargs: Any) -> int:
        if not state["crashed"]:
            state["crashed"] = True
            raise SystemExit("simulated leader SIGKILL after window creation")
        return 10002

    monkeypatch.setattr(scaling, "_add_tmux_windows", launch_or_adopt)
    monkeypatch.setattr(scaling, "_read_scaled_pane_pid", crash_once_after_window)
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: 20002)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: "start-10002")

    with pytest.raises(SystemExit, match="leader SIGKILL"):
        scale_team(tmp_path, rid, add=1)

    monkeypatch.setenv("OMG_MAX_WORKERS", "2")
    with pytest.raises(TeamError, match="retry intent differs"):
        scale_team(tmp_path, rid, add=1, extra=["--changed"])
    assert state["calls"] == 1

    out = scale_team(tmp_path, rid, add=1)

    assert out["task_ids"] == ["scale-2"]
    assert state == {"present": True, "creates": 1, "calls": 2, "crashed": True}
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 1
    assert [task["task_id"] for task in disk["tasks"]].count("scale-2") == 1


def test_scale_up_pane_binding_failure_exact_retry_reuses_preparation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live = _prepare_live_scale_team(monkeypatch, tmp_path)
    real_build = scaling._build_pane_record
    builds = 0
    launches = 0

    def count_build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal builds
        builds += 1
        return real_build(*args, **kwargs)

    def fail_then_launch(*, session: str, records: Any) -> None:
        nonlocal launches
        launches += 1
        if launches == 1:
            raise TeamError("simulated pane binding failure")
        records[0].update(
            {
                "window_index": 7,
                "window_id": "@17",
                "window_nonce": records[0]["window_nonce"],
                "pane_id": "%27",
            }
        )

    monkeypatch.setattr(scaling, "_build_pane_record", count_build)
    monkeypatch.setattr(scaling, "_add_tmux_windows", fail_then_launch)
    monkeypatch.setattr(scaling, "_read_scaled_pane_pid", lambda **_kwargs: 10002)
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: 20002)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: "start-10002")

    with pytest.raises(TeamError, match="pane binding failure"):
        scale_team(tmp_path, rid, add=1)
    argv_path = plane.team_dir(tmp_path, rid) / "scale-2.argv.json"
    task_prompt = (
        worktree_dir(tmp_path, rid, "scale-2")
        / ".omg"
        / "team-prompt"
        / "scale-2.prompt.md"
    )
    argv_path.unlink()
    task_prompt.unlink()
    out = scale_team(tmp_path, rid, add=1)

    assert out["task_ids"] == ["scale-2"]
    assert builds == 1
    assert launches == 2
    assert argv_path.is_file()
    assert task_prompt.is_file()
    disk = load_team_meta(tmp_path, rid)
    assert [row["task_id"] for row in disk["tasks"]].count("scale-2") == 1


def test_scale_up_partial_atomic_preparation_crash_repairs_without_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.contracts import path_keys

    rid, _live = _prepare_live_scale_team(monkeypatch, tmp_path)
    real_atomic = path_keys.atomic_write_bytes
    crashed = False

    def crash_after_first_prompt(path: Path, body: bytes, **kwargs: Any) -> Path:
        nonlocal crashed
        result = real_atomic(path, body, **kwargs)
        if str(path).endswith("scale-2.prompt.md") and not crashed:
            crashed = True
            raise SystemExit("simulated crash after atomic prompt publication")
        return result

    monkeypatch.setattr(path_keys, "atomic_write_bytes", crash_after_first_prompt)
    with pytest.raises(SystemExit, match="atomic prompt publication"):
        scale_team(tmp_path, rid, add=1)

    monkeypatch.setattr(path_keys, "atomic_write_bytes", real_atomic)
    launches = 0

    def launch(*, session: str, records: Any) -> None:
        nonlocal launches
        launches += 1
        records[0].update(
            {"window_index": 7, "window_id": "@17", "pane_id": "%27"}
        )

    monkeypatch.setattr(scaling, "_add_tmux_windows", launch)
    monkeypatch.setattr(scaling, "_read_scaled_pane_pid", lambda **_kwargs: 10002)
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: 20002)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: "start-10002")
    out = scale_team(tmp_path, rid, add=1)

    assert out["task_ids"] == ["scale-2"]
    assert launches == 1


def test_scale_up_torn_preparation_refuses_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live = _prepare_live_scale_team(monkeypatch, tmp_path)
    worktree = worktree_dir(tmp_path, rid, "scale-2")
    scaling.prepare_task(tmp_path, rid, "scale-2")
    prompt = worktree / ".omg" / "team-prompt" / "scale-2.prompt.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_bytes(b"torn")
    launches = 0

    def forbidden_launch(**_kwargs: Any) -> None:
        nonlocal launches
        launches += 1

    monkeypatch.setattr(scaling, "_add_tmux_windows", forbidden_launch)
    with pytest.raises(TeamError, match="preparation differs"):
        scale_team(tmp_path, rid, add=1)
    assert launches == 0


@pytest.mark.parametrize("damage", ["wrong-path", "missing", "symlink"])
def test_relaunch_invalid_linked_worktree_refuses_before_wal_or_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, damage: str
) -> None:
    rid, live, _launched, respawns = _prepare_live_relaunch_team(
        monkeypatch, tmp_path
    )
    expected = Path(str(live["tasks"][0]["worktree"]))
    if damage == "wrong-path":
        live["tasks"][0]["worktree"] = str(tmp_path)
    else:
        backup = expected.with_name(expected.name + "-backup")
        expected.rename(backup)
        if damage == "symlink":
            expected.symlink_to(backup, target_is_directory=True)
    _write_team_meta(tmp_path, rid, live)

    with pytest.raises(TeamError, match="relaunch worktree authority invalid"):
        relaunch_dead_incomplete_workers(tmp_path, rid)
    assert respawns == []
    assert not (plane.team_dir(tmp_path, rid) / "scale-wal" / "1.json").exists()


def test_scale_up_exact_retry_recovers_legacy_receipt_without_wal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, live = _prepare_live_scale_team(monkeypatch, tmp_path)
    launches = 0

    def capture_add(*, session: str, records: Any) -> None:
        nonlocal launches
        assert session == "omg-workers"
        launches += 1
        records[0]["window_index"] = 7
        records[0]["window_id"] = "@17"
        records[0]["window_nonce"] = "c" * 32
        records[0]["pane_id"] = "%27"

    rollbacks: list[list[str]] = []

    def capture_rollback(records: Any) -> list[str]:
        rollbacks.append([str(record["window_id"]) for record in records])
        return []

    monkeypatch.setattr(scaling, "_add_tmux_windows", capture_add)
    monkeypatch.setattr(
        scaling,
        "_read_scaled_pane_pid",
        lambda **_kwargs: 10002,
    )
    monkeypatch.setattr(scaling, "_rollback_created_tmux_windows", capture_rollback)
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: 20002)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: "start-10002")
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: (live["session"], "$7"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "a" * 32)
    real_mutate = scaling.mutate_team_meta
    commit_attempts = 0

    def fail_once_then_commit(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal commit_attempts
        commit_attempts += 1
        if commit_attempts == 1:
            raise TeamError("forced meta commit failure")
        return real_mutate(*args, **kwargs)

    monkeypatch.setattr(scaling, "mutate_team_meta", fail_once_then_commit)

    with pytest.raises(TeamError, match="same scale --add request"):
        scale_team(tmp_path, rid, add=1)

    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    receipt_path = plane.team_identity_receipt_path(tmp_path, rid, 1)
    legacy_receipt = json.loads(receipt_path.read_bytes())
    legacy_receipt["scale_intent"].pop("scale_wal_sha256")
    legacy_receipt["scale_intent_sha256"] = sha256_hex(
        canonical_json_bytes(legacy_receipt["scale_intent"])
    )
    receipt_bytes = canonical_json_bytes(legacy_receipt)
    receipt_path.chmod(0o600)
    receipt_path.write_bytes(receipt_bytes)
    receipt_path.chmod(0o400)
    (plane.team_dir(tmp_path, rid) / "scale-wal" / "1.json").unlink()
    mutable_paths = [
        ownership_manifest_path(tmp_path, rid),
        plane.team_dir(tmp_path, rid) / "scale-2.argv.json",
        worktree_dir(tmp_path, rid, "scale-2")
        / ".omg"
        / "team-prompt"
        / "last_prompt.md",
        worktree_dir(tmp_path, rid, "scale-2")
        / ".omg"
        / "team-prompt"
        / "scale-2.prompt.md",
    ]
    mutable_before = {path: path.read_bytes() for path in mutable_paths}
    first_disk = load_team_meta(tmp_path, rid)
    assert first_disk["identity_generation"] == 0
    assert launches == 1
    assert rollbacks == []

    def refuse_rebuild(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("pending retry must not rebuild mutable artifacts")

    monkeypatch.setattr(scaling, "build_ownership_manifest", refuse_rebuild)
    monkeypatch.setattr(scaling, "prepare_task", refuse_rebuild)
    monkeypatch.setattr(scaling, "_build_pane_record", refuse_rebuild)

    with pytest.raises(TeamError, match="retry intent differs"):
        scale_team(tmp_path, rid, add=1, extra=["--changed-retry"])
    assert {path: path.read_bytes() for path in mutable_paths} == mutable_before
    assert load_team_meta(tmp_path, rid)["identity_generation"] == 0

    out = scale_team(tmp_path, rid, add=1)

    assert out["task_ids"] == ["scale-2"]
    assert launches == 1
    assert rollbacks == []
    assert receipt_path.read_bytes() == receipt_bytes
    assert {path: path.read_bytes() for path in mutable_paths} == mutable_before
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 1
    assert disk["identity_receipt_sha256"] == hashlib.sha256(receipt_bytes).hexdigest()
    assert [
        task["task_id"] for task in disk["tasks"] if task["task_id"] == "scale-2"
    ] == ["scale-2"]


def test_scale_up_pending_retry_fails_closed_when_live_pane_is_rebound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, live = _prepare_live_scale_team(monkeypatch, tmp_path)
    launches = 0

    def capture_add(*, session: str, records: Any) -> None:
        nonlocal launches
        assert session == "omg-workers"
        launches += 1
        records[0].update(
            {
                "window_index": 7,
                "window_id": "@17",
                "window_nonce": "c" * 32,
                "pane_id": "%27",
            }
        )

    monkeypatch.setattr(scaling, "_add_tmux_windows", capture_add)
    monkeypatch.setattr(scaling, "_read_scaled_pane_pid", lambda **_kwargs: 10002)
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: 20002)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: "start-10002")
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: (live["session"], "$7"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "a" * 32)
    real_mutate = scaling.mutate_team_meta
    commit_attempts = 0

    def fail_once(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal commit_attempts
        commit_attempts += 1
        if commit_attempts == 1:
            raise TeamError("forced meta commit failure")
        return real_mutate(*args, **kwargs)

    monkeypatch.setattr(scaling, "mutate_team_meta", fail_once)
    with pytest.raises(TeamError, match="same scale --add request"):
        scale_team(tmp_path, rid, add=1)

    receipt_path = plane.team_identity_receipt_path(tmp_path, rid, 1)
    receipt_before = receipt_path.read_bytes()
    monkeypatch.setattr(scaling, "_read_scaled_pane_pid", lambda **_kwargs: None)

    def refuse_side_effect(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("rebound retry must not launch, rollback, or commit")

    monkeypatch.setattr(scaling, "_add_tmux_windows", refuse_side_effect)
    monkeypatch.setattr(scaling, "_rollback_created_tmux_windows", refuse_side_effect)
    monkeypatch.setattr(scaling, "mutate_team_meta", refuse_side_effect)

    with pytest.raises(TeamError, match="live worker identity mismatch"):
        scale_team(tmp_path, rid, add=1)

    assert launches == 1
    assert receipt_path.read_bytes() == receipt_before
    assert load_team_meta(tmp_path, rid)["identity_generation"] == 0


def test_scale_up_pending_remain_on_exit_dead_cleanup_then_needs_collect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Receipt-bound dead pane (pane_dead=1) cleans then commits needs_collect."""
    rid, live = _prepare_live_scale_team(monkeypatch, tmp_path)
    launches = 0

    def capture_add(*, session: str, records: Any) -> None:
        nonlocal launches
        launches += 1
        records[0].update(
            {
                "window_index": 7,
                "window_id": "@17",
                "window_nonce": "c" * 32,
                "pane_id": "%27",
            }
        )

    real_persist = scaling._persist_team_identity_receipt

    def publish_then_lose(*args: Any, **kwargs: Any) -> Any:
        real_persist(*args, **kwargs)
        raise OSError("simulated receipt publication result loss")

    monkeypatch.setattr(scaling, "_add_tmux_windows", capture_add)
    monkeypatch.setattr(scaling, "_read_scaled_pane_pid", lambda **_kwargs: 10002)
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: 20002)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: "start-10002")
    monkeypatch.setattr(
        scaling,
        "_rollback_created_tmux_windows",
        lambda _records: [],
    )
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: (live["session"], "$7"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "a" * 32)
    monkeypatch.setattr(scaling, "_persist_team_identity_receipt", publish_then_lose)

    with pytest.raises(TeamError, match="receipt-bound windows preserved"):
        scale_team(tmp_path, rid, add=1)

    assert launches == 1
    assert load_team_meta(tmp_path, rid)["identity_generation"] == 0

    # Remain-on-exit: no live PID rebind, but exact dead pane still present until
    # cleanup kills it. Must use cleaned_dead path, not pane_absent alone.
    pane_present = True
    cleanup_calls = 0

    def fake_cleanup(rec: Any, **_kwargs: Any) -> bool:
        nonlocal pane_present, cleanup_calls
        cleanup_calls += 1
        assert rec.get("pane_id") == "%27"
        assert rec.get("window_id") == "@17"
        assert rec.get("window_nonce") == "c" * 32
        pane_present = False
        return True

    monkeypatch.setattr(scaling, "_read_scaled_pane_pid", lambda **_kwargs: None)
    monkeypatch.setattr(scaling, "_cleanup_exact_dead_recorded_pane", fake_cleanup)
    monkeypatch.setattr(
        scaling, "_tmux_pane_presence", lambda _pane: pane_present
    )
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: None)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: None)

    out = scale_team(tmp_path, rid, add=1)
    assert out["task_ids"] == ["scale-2"]
    assert launches == 1
    assert cleanup_calls == 1
    assert pane_present is False
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 1
    recovered = next(row for row in disk["tasks"] if row["task_id"] == "scale-2")
    assert recovered["status"] == STATUS_NEEDS_COLLECT


def test_scale_up_receipt_result_loss_preserves_window_and_retry_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, live = _prepare_live_scale_team(monkeypatch, tmp_path)
    launches = 0
    rollbacks: list[str] = []

    def capture_add(*, session: str, records: Any) -> None:
        nonlocal launches
        assert session == "omg-workers"
        launches += 1
        records[0].update(
            {
                "window_index": 7,
                "window_id": "@17",
                "window_nonce": "c" * 32,
                "pane_id": "%27",
            }
        )

    real_persist = scaling._persist_team_identity_receipt

    def publish_then_lose_result(*args: Any, **kwargs: Any) -> Any:
        real_persist(*args, **kwargs)
        raise OSError("simulated receipt publication result loss")

    monkeypatch.setattr(scaling, "_add_tmux_windows", capture_add)
    monkeypatch.setattr(scaling, "_read_scaled_pane_pid", lambda **_kwargs: 10002)
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: 20002)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: "start-10002")
    monkeypatch.setattr(
        scaling,
        "_rollback_created_tmux_windows",
        lambda _records: rollbacks.append("rollback") or [],
    )
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: (live["session"], "$7"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "a" * 32)
    monkeypatch.setattr(
        scaling, "_persist_team_identity_receipt", publish_then_lose_result
    )

    with pytest.raises(TeamError, match="receipt-bound windows preserved"):
        scale_team(tmp_path, rid, add=1)

    receipt_path = plane.team_identity_receipt_path(tmp_path, rid, 1)
    receipt_before = receipt_path.read_bytes()
    assert launches == 1
    assert rollbacks == []
    assert load_team_meta(tmp_path, rid)["identity_generation"] == 0

    monkeypatch.setattr(scaling, "_read_scaled_pane_pid", lambda **_kwargs: None)
    monkeypatch.setattr(scaling, "_tmux_pane_presence", lambda _pane: False)
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: None)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: None)
    out = scale_team(tmp_path, rid, add=1)

    assert out["task_ids"] == ["scale-2"]
    assert launches == 1
    assert rollbacks == []
    assert receipt_path.read_bytes() == receipt_before
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 1
    recovered = next(row for row in disk["tasks"] if row["task_id"] == "scale-2")
    assert recovered["status"] == STATUS_NEEDS_COLLECT


def test_scale_up_pending_receipt_without_retry_intent_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, live = _prepare_live_scale_team(monkeypatch, tmp_path)
    pending = {
        "task_id": "scale-2",
        "window_index": 7,
        "window_id": "@17",
        "window_nonce": "c" * 32,
        "pane_id": "%27",
        "pid": 10002,
        "pgid": 20002,
        "pid_start": "start-10002",
    }
    plane._persist_team_identity_receipt(
        tmp_path,
        rid,
        session=live["session"],
        session_id="$7",
        launch_nonce="a" * 32,
        generation=1,
        previous_receipt_sha256=live["launch_receipt_sha256"],
        operation="add",
        tasks_before=live["tasks"],
        tasks_after=[*live["tasks"], pending],
    )

    def refuse_side_effect(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("unauthenticated pending receipt must be read-only")

    monkeypatch.setattr(scaling, "_add_tmux_windows", refuse_side_effect)
    monkeypatch.setattr(scaling, "build_ownership_manifest", refuse_side_effect)
    monkeypatch.setattr(scaling, "prepare_task", refuse_side_effect)
    monkeypatch.setattr(scaling, "_build_pane_record", refuse_side_effect)
    monkeypatch.setattr(scaling, "mutate_team_meta", refuse_side_effect)
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: (live["session"], "$7"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "a" * 32)

    with pytest.raises(TeamError, match="lacks authenticated retry intent"):
        scale_team(tmp_path, rid, add=1)

    assert load_team_meta(tmp_path, rid)["identity_generation"] == 0


def test_scale_up_receipt_contract_path_error_rolls_back_created_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.contracts.path_keys import ContractPathError

    rid, _live = _prepare_live_scale_team(monkeypatch, tmp_path)

    def capture_add(*, session: str, records: Any) -> None:
        assert session == "omg-workers"
        records[0]["window_index"] = 7
        records[0]["window_id"] = "@17"
        records[0]["window_nonce"] = "c" * 32
        records[0]["pane_id"] = "%27"

    rollbacks: list[list[str]] = []

    def capture_rollback(records: Any) -> list[str]:
        rollbacks.append([str(record["window_id"]) for record in records])
        return []

    monkeypatch.setattr(scaling, "_add_tmux_windows", capture_add)
    monkeypatch.setattr(scaling, "_read_scaled_pane_pid", lambda **_kwargs: 10002)
    monkeypatch.setattr(scaling, "_rollback_created_tmux_windows", capture_rollback)
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: 20002)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: "start-10002")
    monkeypatch.setattr(
        scaling,
        "_persist_team_identity_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ContractPathError("receipt parent refused")
        ),
    )

    with pytest.raises(TeamError, match="receipt parent refused"):
        scale_team(tmp_path, rid, add=1)

    assert rollbacks == [["@17"]]
    assert load_team_meta(tmp_path, rid)["identity_generation"] == 0


def test_scale_up_meta_publish_then_raise_preserves_committed_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live = _prepare_live_scale_team(monkeypatch, tmp_path)

    def capture_add(*, session: str, records: Any) -> None:
        assert session == "omg-workers"
        records[0]["window_index"] = 7
        records[0]["window_id"] = "@17"
        records[0]["window_nonce"] = "c" * 32
        records[0]["pane_id"] = "%27"

    rollbacks: list[list[str]] = []

    def capture_rollback(records: Any) -> list[str]:
        rollbacks.append([str(record["window_id"]) for record in records])
        return []

    real_mutate = scaling.mutate_team_meta

    def publish_then_raise(*args: Any, **kwargs: Any) -> None:
        real_mutate(*args, **kwargs)
        raise OSError("simulated parent fsync result loss")

    monkeypatch.setattr(scaling, "_add_tmux_windows", capture_add)
    monkeypatch.setattr(scaling, "_read_scaled_pane_pid", lambda **_kwargs: 10002)
    monkeypatch.setattr(scaling, "_rollback_created_tmux_windows", capture_rollback)
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: 20002)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: "start-10002")
    monkeypatch.setattr(scaling, "mutate_team_meta", publish_then_raise)

    out = scale_team(tmp_path, rid, add=1)

    assert out["task_ids"] == ["scale-2"]
    assert out["window_indices"] == [7]
    assert rollbacks == []
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 1
    assert disk["next_worker_index"] == 8
    assert any(task["task_id"] == "scale-2" for task in disk["tasks"])
    assert plane.team_identity_receipt_path(tmp_path, rid, 1).is_file()


def test_scale_up_unknown_meta_readback_preserves_windows_and_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live = _prepare_live_scale_team(monkeypatch, tmp_path)

    def capture_add(*, session: str, records: Any) -> None:
        assert session == "omg-workers"
        records[0]["window_index"] = 7
        records[0]["window_id"] = "@17"
        records[0]["window_nonce"] = "c" * 32
        records[0]["pane_id"] = "%27"

    rollbacks: list[list[str]] = []

    def capture_rollback(records: Any) -> list[str]:
        rollbacks.append([str(record["window_id"]) for record in records])
        return []

    real_mutate = scaling.mutate_team_meta

    def publish_drift_then_raise(*args: Any, **kwargs: Any) -> None:
        real_mutate(*args, **kwargs)

        def drift_window_id(current: dict[str, Any]) -> dict[str, Any]:
            for task in current.get("tasks") or []:
                if task.get("task_id") == "scale-2":
                    task["window_id"] = "@99"
            return current

        real_mutate(args[0], args[1], drift_window_id)
        raise OSError("simulated ambiguous readback")

    monkeypatch.setattr(scaling, "_add_tmux_windows", capture_add)
    monkeypatch.setattr(scaling, "_read_scaled_pane_pid", lambda **_kwargs: 10002)
    monkeypatch.setattr(scaling, "_rollback_created_tmux_windows", capture_rollback)
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: 20002)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: "start-10002")
    monkeypatch.setattr(scaling, "mutate_team_meta", publish_drift_then_raise)

    with pytest.raises(TeamError, match="commit outcome unknown"):
        scale_team(tmp_path, rid, add=1)

    assert rollbacks == []
    assert plane.team_identity_receipt_path(tmp_path, rid, 1).is_file()
    disk = load_team_meta(tmp_path, rid)
    scaled = next(task for task in disk["tasks"] if task["task_id"] == "scale-2")
    assert scaled["window_id"] == "@99"


@pytest.mark.parametrize("failure_kind", ["os", "contract"])
def test_scale_up_meta_durability_unknown_preserves_windows_and_receipt(
    failure_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omg_cli.contracts import path_keys
    from omg_cli.contracts.path_keys import ContractPathError

    rid, _live = _prepare_live_scale_team(monkeypatch, tmp_path)

    def capture_add(*, session: str, records: Any) -> None:
        assert session == "omg-workers"
        records[0]["window_index"] = 7
        records[0]["window_id"] = "@17"
        records[0]["window_nonce"] = "c" * 32
        records[0]["pane_id"] = "%27"

    rollbacks: list[list[str]] = []

    def capture_rollback(records: Any) -> list[str]:
        rollbacks.append([str(record["window_id"]) for record in records])
        return []

    real_mutate = scaling.mutate_team_meta

    def publish_then_raise(*args: Any, **kwargs: Any) -> None:
        real_mutate(*args, **kwargs)
        raise OSError("simulated parent fsync result loss")

    def fail_durability_readback(_path: Path) -> None:
        if failure_kind == "contract":
            raise ContractPathError("managed parent refused")
        raise OSError("directory fsync failed")

    monkeypatch.setattr(scaling, "_add_tmux_windows", capture_add)
    monkeypatch.setattr(scaling, "_read_scaled_pane_pid", lambda **_kwargs: 10002)
    monkeypatch.setattr(scaling, "_rollback_created_tmux_windows", capture_rollback)
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: 20002)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: "start-10002")
    monkeypatch.setattr(scaling, "mutate_team_meta", publish_then_raise)
    monkeypatch.setattr(
        path_keys,
        "fsync_existing_managed_dir",
        fail_durability_readback,
    )

    with pytest.raises(TeamError, match="commit outcome unknown"):
        scale_team(tmp_path, rid, add=1)

    assert rollbacks == []
    assert plane.team_identity_receipt_path(tmp_path, rid, 1).is_file()
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 1
    assert any(task["task_id"] == "scale-2" for task in disk["tasks"])


def _write_team_meta(root: Path, run_id: str, meta: dict[str, Any]) -> None:
    meta["writer"] = CLI_WRITER
    plane._atomic_write_json(team_meta_path(root, run_id), meta)


def _tasks_n(n: int) -> list[dict[str, Any]]:
    return [{"task_id": f"t{i}", "owned_files": [f"f{i}.py"]} for i in range(n)]


# ---------------------------------------------------------------------------
# scale up — cap + monotonic indices + dry-run
# ---------------------------------------------------------------------------


def test_scale_up_within_cap_appends_monotonic_indices(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _boom_subprocess)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)

    meta = start_team("scale up", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]

    out = scale_team(tmp_path, rid, add=2, dry_run=True)
    assert out["op"] == "add"
    assert out["added"] == 2
    assert out["window_indices"] == [2, 3]
    assert out["dry_run"] is True
    assert out["verified"] is False

    disk = load_team_meta(tmp_path, rid)
    indices = [int(t["window_index"]) for t in disk["tasks"]]
    assert indices == [0, 1, 2, 3]
    assert len(set(indices)) == 4
    assert disk["next_worker_index"] == 4
    for rec in out["tasks_added"]:
        assert rec["pid"] is None
        assert rec["pgid"] is None


def test_scale_up_beyond_cap_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)

    cap = max_workers_cap()
    assert cap == 8
    tasks = _tasks_n(cap - 1)
    meta = start_team("near cap", tasks, root=tmp_path, dry_run=True)
    rid = meta["run_id"]

    # Fill to cap
    scale_team(tmp_path, rid, add=1, dry_run=True)
    disk = load_team_meta(tmp_path, rid)
    assert disk["task_count"] == cap

    with pytest.raises(TeamGateError, match="exceeds hard cap"):
        scale_team(tmp_path, rid, add=1, dry_run=True)


def test_scale_up_never_reuses_window_index_after_scale_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)

    meta = start_team("monotonic", TASKS_THREE, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    scale_team(tmp_path, rid, add=1, dry_run=True)
    # indices 0,1,2,3 — remove highest idle (3)
    scale_team(tmp_path, rid, remove=1, dry_run=True)

    out = scale_team(tmp_path, rid, add=1, dry_run=True)
    assert out["window_indices"] == [4]
    disk = load_team_meta(tmp_path, rid)
    all_indices = [int(t["window_index"]) for t in disk["tasks"]]
    assert 4 in all_indices
    assert 3 in all_indices  # scaled_down record preserved
    scaled = [t for t in disk["tasks"] if t.get("status") == STATUS_SCALED_DOWN]
    assert any(int(t["window_index"]) == 3 for t in scaled)


# ---------------------------------------------------------------------------
# scale down — recorded kills only, worktrees preserved, min 1 active
# ---------------------------------------------------------------------------


def test_scale_down_kills_only_recorded_targets_preserves_worktrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    meta = start_team("scale down", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    live = dict(load_team_meta(tmp_path, rid))
    live["dry_run"] = False
    live["session"] = "omg-scale-test"
    live["tasks"] = [
        {
            **live["tasks"][0],
            "pid": 11111,
            "pgid": 424242,
            "pane_id": "%11",
            "pid_start": "start-11111",
            "status": STATUS_RUNNING,
            "window_index": 0,
        },
        {
            **live["tasks"][1],
            "pid": 22222,
            "pgid": 424243,
            "pane_id": "%22",
            "pid_start": "start-22222",
            "status": STATUS_RUNNING,
            "window_index": 1,
        },
    ]
    _receipt, receipt_hash = plane._persist_team_launch_receipt(
        tmp_path,
        rid,
        session=live["session"],
        session_id="$77",
        launch_nonce="b" * 32,
        tasks=live["tasks"],
    )
    live["launch_nonce"] = "b" * 32
    live["launch_receipt_sha256"] = receipt_hash
    live["identity_generation"] = 0
    live["identity_receipt_sha256"] = receipt_hash
    _write_team_meta(tmp_path, rid, live)
    worktrees_before = [Path(t["worktree"]) for t in live["tasks"] if t.get("worktree")]

    killpg_calls: list[tuple[int, int]] = []
    tmux_cmds: list[list[str]] = []
    pane_killed = False

    def fake_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, sig))

    def fake_tmux_run(args: Any, **kw: Any) -> MagicMock:
        nonlocal pane_killed
        tmux_cmds.append(list(args))
        command = list(args)
        if command[0] == "display-message":
            # session_name session_id window_index pane_index window_id pane_id
            # pane_pid scale_nonce pane_dead
            return MagicMock(
                returncode=0,
                stdout="omg-scale-test\t$77\t1\t0\t@22\t%22\t22222\t\t0\n",
                stderr="",
            )
        if command[0] == "if-shell":
            pane_killed = True
            return MagicMock(returncode=0, stdout="", stderr="")
        if command[0] == "list-panes":
            panes = ["%11"] + ([] if pane_killed else ["%22"])
            return MagicMock(
                returncode=0,
                stdout="".join(f"{pane}\n" for pane in panes),
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    def guard_run(cmd: Any, *a: Any, **k: Any) -> Any:
        joined = " ".join(
            str(x) for x in (cmd if isinstance(cmd, (list, tuple)) else [cmd])
        )
        if "pkill" in joined or "pgrep" in joined:
            raise AssertionError(f"forbidden broad kill: {joined}")
        raise AssertionError(f"unexpected subprocess.run: {joined}")

    monkeypatch.setattr(scaling.os, "killpg", fake_killpg)
    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: (live["session"], "$77"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "b" * 32)
    monkeypatch.setattr(
        scaling,
        "_list_pane_identities",
        lambda _session: {0: ("%11", 11111), 1: ("%22", 22222)},
    )
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda pid: f"start-{pid}")
    monkeypatch.setattr(
        scaling,
        "_pgid_for_pid",
        lambda pid: {11111: 424242, 22222: 424243}[pid],
    )
    monkeypatch.setattr(subprocess, "run", guard_run)
    monkeypatch.setattr(plane.subprocess, "run", guard_run)

    out = scale_team(tmp_path, rid, remove=1, dry_run=False)
    assert out["op"] == "remove"
    assert out["removed"] == 1
    assert killpg_calls
    assert all(pg in (424242, 424243) for pg, _ in killpg_calls)
    conditional_kills = [call for call in tmux_cmds if call[0] == "if-shell"]
    assert len(conditional_kills) == 1
    assert conditional_kills[0][-2] == "kill-pane -t %22"
    assert "#{==:#{session_id},$77}" in conditional_kills[0][4]
    assert "#{==:#{@omg_launch_nonce}," + "b" * 32 + "}" in conditional_kills[0][4]
    assert all(call[0] != "kill-window" for call in tmux_cmds)
    for wt in worktrees_before:
        assert wt.is_dir()
    disk = load_team_meta(tmp_path, rid)
    assert disk["task_count"] == 1
    assert any(t.get("status") == STATUS_SCALED_DOWN for t in disk["tasks"])


def test_scale_down_refuses_last_active_pane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    one = [{"task_id": "solo", "owned_files": ["solo.py"]}]
    meta = start_team("min one", one, root=tmp_path, dry_run=True)
    rid = meta["run_id"]

    with pytest.raises(TeamError, match="never remove below 1"):
        scale_team(tmp_path, rid, remove=1, dry_run=True)

    meta2 = start_team("two", TASKS_TWO, root=tmp_path, dry_run=True, force=True)
    rid2 = meta2["run_id"]
    with pytest.raises(TeamError, match="minimum is 1"):
        scale_team(tmp_path, rid2, remove=2, dry_run=True)


def test_scale_down_fails_closed_when_pid_pgid_drifts_before_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("pgid drift", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    live = dict(load_team_meta(tmp_path, rid))
    live["dry_run"] = False
    live["session"] = "omg-scale-drift"
    live["tasks"] = [
        {
            **task,
            "pane_id": f"%{index + 31}",
            "pid": 31000 + index,
            "pgid": 41000 + index,
            "pid_start": f"start-{31000 + index}",
            "status": STATUS_RUNNING,
        }
        for index, task in enumerate(live["tasks"])
    ]
    _receipt, receipt_hash = plane._persist_team_launch_receipt(
        tmp_path,
        rid,
        session=live["session"],
        session_id="$31",
        launch_nonce="c" * 32,
        tasks=live["tasks"],
    )
    live.update(
        {
            "launch_nonce": "c" * 32,
            "launch_receipt_sha256": receipt_hash,
            "identity_generation": 0,
            "identity_receipt_sha256": receipt_hash,
        }
    )
    _write_team_meta(tmp_path, rid, live)
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: (live["session"], "$31"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "c" * 32)
    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(
        scaling,
        "_read_recorded_tmux_pane",
        lambda rec, **_kwargs: ("@32", str(rec["pane_id"])),
    )
    monkeypatch.setattr(
        scaling,
        "_list_pane_identities",
        lambda _session: {0: ("%31", 31000), 1: ("%32", 31001)},
    )
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda pid: f"start-{pid}")
    reads = iter([41001, 99999])
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: next(reads))
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        scaling.os, "killpg", lambda pgid, sig: signals.append((pgid, int(sig)))
    )

    with pytest.raises(TeamError, match="PGID drift"):
        scale_team(tmp_path, rid, remove=1)

    assert signals == []
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 0
    assert all(task["status"] == STATUS_RUNNING for task in disk["tasks"])


# ---------------------------------------------------------------------------
# scale lock
# ---------------------------------------------------------------------------


def test_scale_lock_refuses_concurrent_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)

    meta = start_team("lock", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    # flock is process-held; a plain PID file is not exclusive.
    with acquire_scale_lock(tmp_path, rid):
        with pytest.raises(TeamError, match="scale lock held"):
            scale_team(tmp_path, rid, add=1, dry_run=True)


def test_acquire_scale_lock_exclusive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("lock ctx", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    with acquire_scale_lock(tmp_path, rid):
        assert scale_lock_path(tmp_path, rid).is_file()
        with pytest.raises(TeamError, match="scale lock held"):
            with acquire_scale_lock(tmp_path, rid):
                pass
    # File may remain as a lock node; flock is released so re-acquire works.
    with acquire_scale_lock(tmp_path, rid):
        assert scale_lock_path(tmp_path, rid).is_file()


def test_relaunch_refuses_when_scale_lock_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Live relaunch shares the scale lock (no concurrent scale/resume spawn)."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)

    meta = start_team("relaunch lock", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    live = dict(load_team_meta(tmp_path, rid))
    live["dry_run"] = False
    live["session"] = "omg-relaunch-lock"
    _write_team_meta(tmp_path, rid, live)

    with acquire_scale_lock(tmp_path, rid):
        with pytest.raises(TeamError, match="scale lock held"):
            relaunch_dead_incomplete_workers(tmp_path, rid)


def test_relaunch_wal_precedes_respawn_and_commits_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live, _launched, respawns = _prepare_live_relaunch_team(
        monkeypatch, tmp_path
    )
    real_respawn = __import__("omg_cli.team.tmux", fromlist=["respawn_worker_pane"]).respawn_worker_pane

    def assert_wal_first(**kwargs: Any) -> str:
        meta = load_team_meta(tmp_path, rid)
        assert pending_identity_wal_operation(tmp_path, rid, meta) == "relaunch"
        wal = json.loads(
            (plane.team_dir(tmp_path, rid) / "scale-wal" / "1.json").read_bytes()
        )
        assert wal["store_kind"] == "team_relaunch_wal"
        assert wal["writer_contract"] == "relaunch-wal-v1"
        assert wal["target_window_id"] == "@7"
        return real_respawn(**kwargs)

    monkeypatch.setattr("omg_cli.team.tmux.respawn_worker_pane", assert_wal_first)
    out = relaunch_dead_incomplete_workers(tmp_path, rid)

    assert respawns == ["t-a"]
    assert out["identity_generation"] == 1
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 1
    assert pending_identity_wal_operation(tmp_path, rid, disk) is None
    receipt = json.loads(plane.team_identity_receipt_path(tmp_path, rid, 1).read_bytes())
    assert receipt["operation"] == "relaunch"
    assert receipt["scale_intent"]["relaunch_wal_sha256"]


def test_legacy_future_receipt_blocks_relaunch_before_wal_or_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, live, _launched, respawns = _prepare_live_relaunch_team(
        monkeypatch, tmp_path
    )
    plane._persist_team_identity_receipt(
        tmp_path,
        rid,
        session=live["session"],
        session_id="$7",
        launch_nonce="a" * 32,
        generation=1,
        previous_receipt_sha256=live["identity_receipt_sha256"],
        operation="add",
        tasks_before=live["tasks"],
        tasks_after=live["tasks"],
    )

    wal_path = plane.team_dir(tmp_path, rid) / "scale-wal" / "1.json"
    assert not wal_path.exists()
    with pytest.raises(TeamError, match="relaunch refused while an identity receipt"):
        relaunch_dead_incomplete_workers(tmp_path, rid)

    assert not wal_path.exists()
    assert respawns == []
    assert load_team_meta(tmp_path, rid)["identity_generation"] == 0


def test_relaunch_retry_adopts_post_respawn_pre_receipt_orphan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live, _launched, respawns = _prepare_live_relaunch_team(
        monkeypatch, tmp_path
    )
    persist = scaling._persist_team_identity_receipt
    calls = 0

    def crash_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("crash before receipt")
        return persist(*args, **kwargs)

    monkeypatch.setattr(scaling, "_persist_team_identity_receipt", crash_once)
    with pytest.raises(OSError, match="crash before receipt"):
        relaunch_dead_incomplete_workers(tmp_path, rid)

    # Pending WAL owns candidate selection; mutable observations cannot wedge it.
    monkeypatch.setattr("omg_cli.team.tmux.pane_alive", lambda _pane: True)
    monkeypatch.setattr(
        scaling,
        "_worker_api_tasks_terminal",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(scaling, "_worktree_dirty", lambda _path: True)
    out = relaunch_dead_incomplete_workers(tmp_path, rid)
    assert respawns == ["t-a"]
    assert [row["task_id"] for row in out["relaunched"]] == ["t-a"]
    assert load_team_meta(tmp_path, rid)["identity_generation"] == 1


def test_relaunch_retry_recovers_pending_receipt_without_respawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live, _launched, respawns = _prepare_live_relaunch_team(
        monkeypatch, tmp_path
    )
    mutate = scaling.mutate_team_meta
    calls = 0

    def renumber(_session: str, tasks: Any) -> None:
        for row in tasks:
            if row.get("task_id") == "t-b":
                row["window_index"] = 9

    monkeypatch.setattr(scaling, "_resync_window_indices", renumber)

    def fail_before_meta(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("crash before meta")
        return mutate(*args, **kwargs)

    monkeypatch.setattr(scaling, "mutate_team_meta", fail_before_meta)
    with pytest.raises(OSError, match="crash before meta"):
        relaunch_dead_incomplete_workers(tmp_path, rid)
    assert plane.team_identity_receipt_path(tmp_path, rid, 1).is_file()

    out = relaunch_dead_incomplete_workers(tmp_path, rid)
    assert respawns == ["t-a"]
    assert out["identity_generation"] == 1
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 1
    assert next(row for row in disk["tasks"] if row["task_id"] == "t-b")[
        "window_index"
    ] == 9


def test_relaunch_receipt_recovery_rejects_tasks_before_tamper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live, _launched, respawns = _prepare_live_relaunch_team(
        monkeypatch, tmp_path
    )
    persist = scaling._persist_team_identity_receipt
    lost = False

    def publish_then_lose(*args: Any, **kwargs: Any) -> Any:
        nonlocal lost
        result = persist(*args, **kwargs)
        if not lost:
            lost = True
            raise OSError("lost receipt result")
        return result

    monkeypatch.setattr(scaling, "_persist_team_identity_receipt", publish_then_lose)
    with pytest.raises(OSError, match="lost receipt result"):
        relaunch_dead_incomplete_workers(tmp_path, rid)

    from omg_cli.contracts.writer_chain import canonical_json_bytes, parse_canonical_json_bytes

    receipt_path = plane.team_identity_receipt_path(tmp_path, rid, 1)
    receipt = parse_canonical_json_bytes(receipt_path.read_bytes())
    receipt["tasks_before"][0]["pane_id"] = "%999"
    receipt_path.chmod(0o600)
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    receipt_path.chmod(0o400)

    with pytest.raises(TeamError, match="identity continuity mismatch"):
        relaunch_dead_incomplete_workers(tmp_path, rid)
    assert respawns == ["t-a"]
    assert load_team_meta(tmp_path, rid)["identity_generation"] == 0


def test_pending_relaunch_receipt_commits_exact_absent_worker_needs_collect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live, launched, respawns = _prepare_live_relaunch_team(
        monkeypatch, tmp_path
    )
    real_mutate = scaling.mutate_team_meta
    monkeypatch.setattr(
        scaling,
        "mutate_team_meta",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("crash before meta")),
    )
    with pytest.raises(OSError, match="crash before meta"):
        relaunch_dead_incomplete_workers(tmp_path, rid)
    launched.clear()
    monkeypatch.setattr(scaling, "mutate_team_meta", real_mutate)
    monkeypatch.setattr(scaling, "_tmux_pane_presence", lambda _pane: False)
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: None)
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda _pid: None)

    out = relaunch_dead_incomplete_workers(tmp_path, rid)
    assert respawns == ["t-a"]
    assert out["identity_generation"] == 1
    disk = load_team_meta(tmp_path, rid)
    recovered = next(row for row in disk["tasks"] if row["task_id"] == "t-a")
    assert recovered["status"] == STATUS_NEEDS_COLLECT


def test_relaunch_partial_retry_launches_each_task_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live, launched, respawns = _prepare_live_relaunch_team(
        monkeypatch, tmp_path, dead_tasks=2
    )
    original = __import__("omg_cli.team.tmux", fromlist=["respawn_worker_pane"]).respawn_worker_pane
    failed = False

    def fail_second_once(**kwargs: Any) -> str:
        nonlocal failed
        if "t-b" in str(kwargs["pane_command"]) and not failed:
            failed = True
            raise __import__("omg_cli.team.tmux", fromlist=["TmuxTeamError"]).TmuxTeamError(
                "simulated second-task crash"
            )
        return original(**kwargs)

    monkeypatch.setattr("omg_cli.team.tmux.respawn_worker_pane", fail_second_once)
    with pytest.raises(TeamError, match="second-task crash"):
        relaunch_dead_incomplete_workers(tmp_path, rid)
    assert launched == {"t-a": "%81"}

    out = relaunch_dead_incomplete_workers(tmp_path, rid)
    assert respawns == ["t-a", "t-b"]
    assert {row["task_id"] for row in out["relaunched"]} == {"t-a", "t-b"}


def test_relaunch_wal_rejects_changed_retry_and_session_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live, _launched, _respawns = _prepare_live_relaunch_team(
        monkeypatch, tmp_path
    )
    monkeypatch.setattr(
        scaling,
        "_persist_team_identity_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("crash")),
    )
    with pytest.raises(OSError, match="crash"):
        relaunch_dead_incomplete_workers(tmp_path, rid)

    changed = load_team_meta(tmp_path, rid)
    changed["tasks"][0]["pane_command"] += " --changed"
    _write_team_meta(tmp_path, rid, changed)
    with pytest.raises(TeamError, match="differs from WAL"):
        relaunch_dead_incomplete_workers(tmp_path, rid)

    changed["tasks"][0]["pane_command"] = _live["tasks"][0]["pane_command"]
    _write_team_meta(tmp_path, rid, changed)
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: (changed["session"], "$999"),
    )
    with pytest.raises(TeamError, match="session identity mismatch"):
        relaunch_dead_incomplete_workers(tmp_path, rid)


@pytest.mark.parametrize(
    "rows",
    [
        [
            "omg-workers\t$7\t@7\t%81\t"
            + "a" * 32
                + "\tt-a\t"
                + "b" * 32
                + "\t0\tCMD",
            "omg-workers\t$7\t@7\t%82\t"
            + "a" * 32
                + "\tt-a\t"
                + "b" * 32
                + "\t0\tCMD",
        ],
        [
            "omg-workers\t$7\t@8\t%81\t"
            + "a" * 32
                + "\tt-a\t"
                + "b" * 32
                + "\t0\tCMD"
        ],
    ],
    ids=["duplicate", "foreign-window"],
)
def test_relaunch_discovery_rejects_ambiguous_or_foreign_marker(
    monkeypatch: pytest.MonkeyPatch, rows: list[str]
) -> None:
    command = scaling._relaunch_bootstrap_command(
        "echo worker",
        session_id="$7",
        launch_nonce="a" * 32,
        target_window_id="@7",
        task_id="t-a",
        relaunch_nonce="b" * 32,
    )
    rows = [row.replace("CMD", command) for row in rows]
    monkeypatch.setattr(
        scaling,
        "_tmux_run",
        lambda _args: MagicMock(returncode=0, stdout="\n".join(rows) + "\n", stderr=""),
    )
    with pytest.raises(TeamError, match="ambiguous/foreign"):
        scaling._discover_relaunch_pane(
            session="omg-workers",
            session_id="$7",
            launch_nonce="a" * 32,
            target_window_id="@7",
            task_id="t-a",
            relaunch_nonce="b" * 32,
            start_command=command,
        )


def test_relaunch_discovery_adopts_exact_pre_marker_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = scaling._relaunch_bootstrap_command(
        "echo worker",
        session_id="$7",
        launch_nonce="a" * 32,
        target_window_id="@7",
        task_id="t-a",
        relaunch_nonce="b" * 32,
    )
    start = f'"{command}"'
    row = (
        "omg-workers\t$7\t@7\t%81\t"
            + "a" * 32
            + "\t\t\t0\t"
        + start
        + "\n"
    )
    monkeypatch.setattr(
        scaling,
        "_tmux_run",
        lambda _args: MagicMock(returncode=0, stdout=row, stderr=""),
    )
    assert (
        scaling._discover_relaunch_pane(
            session="omg-workers",
            session_id="$7",
            launch_nonce="a" * 32,
            target_window_id="@7",
            task_id="t-a",
            relaunch_nonce="b" * 32,
            start_command=command,
        )
        == "%81"
    )


@pytest.mark.parametrize(
    ("marker_task", "marker_nonce"),
    [("", ""), ("t-a", ""), ("t-a", "b" * 32)],
    ids=["pre-marker", "partial-marker", "exact-marker"],
)
def test_relaunch_discovery_removes_exact_dead_wal_pane(
    monkeypatch: pytest.MonkeyPatch,
    marker_task: str,
    marker_nonce: str,
) -> None:
    command = scaling._relaunch_bootstrap_command(
        "echo worker",
        session_id="$7",
        launch_nonce="a" * 32,
        target_window_id="@7",
        task_id="t-a",
        relaunch_nonce="b" * 32,
    )
    present = True
    calls: list[list[str]] = []

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        nonlocal present
        call = list(args)
        calls.append(call)
        if call[0] == "if-shell":
            present = False
            return MagicMock(returncode=0, stdout="", stderr="")
        if call[-1] == scaling._TMUX_RELAUNCH_DISCOVERY_FORMAT:
            row = (
                f"omg-workers\t$7\t@7\t%81\t{'a' * 32}\t{marker_task}\t"
                f"{marker_nonce}\t1\t{command}\n"
            )
            return MagicMock(returncode=0, stdout=row if present else "", stderr="")
        return MagicMock(returncode=0, stdout="%81\n" if present else "", stderr="")

    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    assert (
        scaling._discover_relaunch_pane(
            session="omg-workers",
            session_id="$7",
            launch_nonce="a" * 32,
            target_window_id="@7",
            task_id="t-a",
            relaunch_nonce="b" * 32,
            start_command=command,
        )
        is None
    )
    cleanup = next(call for call in calls if call[0] == "if-shell")
    assert "#{==:#{pane_dead},1}" in cleanup[4]
    assert "OMG_RELAUNCH_START_SHA=" in cleanup[4]


def test_pane_alive_treats_remain_on_exit_dead_pane_as_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.team import tmux

    monkeypatch.setattr(tmux, "tmux_available", lambda: True)
    monkeypatch.setattr(
        tmux,
        "_tmux_run",
        lambda _args: MagicMock(returncode=0, stdout="%81\t1\n", stderr=""),
    )
    assert tmux.pane_alive("%81") is False


def test_relaunch_meta_publish_result_loss_is_adopted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live, _launched, respawns = _prepare_live_relaunch_team(
        monkeypatch, tmp_path
    )
    mutate = scaling.mutate_team_meta
    lost = False

    def publish_then_raise(*args: Any, **kwargs: Any) -> Any:
        nonlocal lost
        result = mutate(*args, **kwargs)
        if not lost:
            lost = True
            raise OSError("lost meta result")
        return result

    monkeypatch.setattr(scaling, "mutate_team_meta", publish_then_raise)
    out = relaunch_dead_incomplete_workers(tmp_path, rid)
    assert respawns == ["t-a"]
    assert out["identity_generation"] == 1
    assert load_team_meta(tmp_path, rid)["identity_generation"] == 1


def test_scale_lock_allows_orphaned_file_without_holder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Orphan lock files (no live flock holder) do not block scale."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)

    meta = start_team("stale lock", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    lock = scale_lock_path(tmp_path, rid)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999999\n", encoding="utf-8")

    out = scale_team(tmp_path, rid, add=1, dry_run=True)
    assert out["op"] == "add"
    assert out["added"] == 1


# ---------------------------------------------------------------------------
# resume — liveness reconciliation, idempotent, fail-closed
# ---------------------------------------------------------------------------


def test_resume_reconciles_liveness_from_tmux_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    meta = start_team("resume", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    live = dict(load_team_meta(tmp_path, rid))
    live["dry_run"] = False
    live["session"] = "omg-resume-test"
    live["tasks"] = [
        {**live["tasks"][0], "status": STATUS_RUNNING, "window_index": 0},
        {**live["tasks"][1], "status": STATUS_RUNNING, "window_index": 1},
    ]
    _write_team_meta(tmp_path, rid, live)

    def fake_window_alive(_session: str, widx: int) -> bool | None:
        return widx == 0  # pane 0 alive, pane 1 dead

    monkeypatch.setattr(scaling, "_window_alive", fake_window_alive)

    out = resume_team(tmp_path, rid)
    assert out["changes"] == 1
    assert out["verified"] is False
    disk = load_team_meta(tmp_path, rid)
    by_id = {t["task_id"]: t for t in disk["tasks"]}
    assert by_id["t-a"]["status"] == STATUS_RUNNING
    assert by_id["t-b"]["status"] == STATUS_NEEDS_COLLECT


def test_resume_idempotent_second_run_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    meta = start_team("idem", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    live = dict(load_team_meta(tmp_path, rid))
    live["dry_run"] = False
    live["session"] = "omg-idem"
    live["tasks"] = [
        {**live["tasks"][0], "status": STATUS_RUNNING, "window_index": 0},
        {**live["tasks"][1], "status": STATUS_RUNNING, "window_index": 1},
    ]
    _write_team_meta(tmp_path, rid, live)
    monkeypatch.setattr(
        scaling, "_window_alive", lambda _s, w: True if w == 0 else False
    )

    first = resume_team(tmp_path, rid)
    second = resume_team(tmp_path, rid)
    assert first["changes"] == 1
    assert second["changes"] == 0
    assert (
        load_team_meta(tmp_path, rid)["tasks"] == load_team_meta(tmp_path, rid)["tasks"]
    )
    # reconciliations stable (all unchanged on second pass)
    assert all(r.get("unchanged") for r in second["reconciliations"])


def test_resume_fail_closed_non_team_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    run = create_run(tmp_path, mode="ulw", goal="not team")
    rid = run["run_id"]
    with pytest.raises(TeamError, match="team.json missing"):
        resume_team(tmp_path, rid)


def test_resume_fail_closed_missing_team_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    run = create_run(tmp_path, mode="ulw", goal="ghost")
    rid = run["run_id"]
    # Mark as team in status but no team.json on disk
    from omg_cli.state import write_status

    write_status(tmp_path, rid, "running", extra={"team": True})
    with pytest.raises(TeamError, match="team.json missing"):
        resume_team(tmp_path, rid)


def test_resume_does_not_trust_stale_running_without_live_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """team.json says running but tmux probe says dead → needs_collect."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    meta = start_team("stale", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    live = dict(load_team_meta(tmp_path, rid))
    live["dry_run"] = False
    live["session"] = "omg-stale"
    live["tasks"] = [
        {**live["tasks"][0], "status": STATUS_RUNNING, "window_index": 0},
    ]
    _write_team_meta(tmp_path, rid, live)
    monkeypatch.setattr(scaling, "_window_alive", lambda *_a, **_k: False)

    resume_team(tmp_path, rid)
    disk = load_team_meta(tmp_path, rid)
    assert disk["tasks"][0]["status"] == STATUS_NEEDS_COLLECT


# ---------------------------------------------------------------------------
# CLI smoke (dry-run scale/resume)
# ---------------------------------------------------------------------------


def test_cli_team_scale_dry_run(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    env = os.environ.copy()
    env[EXPERIMENTAL_ENV] = "1"
    for k in plane.WORKER_ENV_MARKERS:
        env.pop(k, None)
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )

    start = subprocess.run(
        [
            PYTHON,
            str(BIN_OMG),
            "team",
            "start",
            "--dry-run",
            "--goal",
            "cli scale",
            "--tasks-json",
            json.dumps(TASKS_TWO),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert start.returncode == 0, start.stderr + start.stdout
    rid = json.loads(start.stdout)["run_id"]

    scale = subprocess.run(
        [
            PYTHON,
            str(BIN_OMG),
            "team",
            "scale",
            "--add",
            "1",
            "--dry-run",
            "--run",
            rid,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert scale.returncode == 0, scale.stderr + scale.stdout
    payload = json.loads(scale.stdout)
    assert payload["op"] == "add"
    assert payload.get("verified") is not True
    wt = worktree_dir(tmp_path, rid, payload["task_ids"][0])
    assert wt.is_dir()
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("verified") is not True


def test_native_dispatch_plan_respects_capacity_without_process_launch(
    tmp_path: Path,
) -> None:
    create_native_team(
        tmp_path,
        run_id="run-scale-native",
        team_id="team-scale-native",
        leader_id="leader",
        parent_session_id="parent-session",
        base_sha="a" * 40,
        created_at="2026-07-22T00:00:00Z",
        tasks=[
            {"task_id": task_id, "role": "verifier", "prompt": task_id}
            for task_id in ("a", "b", "c")
        ],
    )
    first = native_dispatch_plan(
        tmp_path,
        run_id="run-scale-native",
        team_id="team-scale-native",
        max_concurrency=2,
    )
    assert [item["task_id"] for item in first["ready"]] == ["a", "b"]
    assert first["slots"] == 2
    prepare_native_spawn(
        tmp_path,
        run_id="run-scale-native",
        team_id="team-scale-native",
        task_id="a",
        expected_sequence=0,
        expected_generation=0,
        lease_generation=0,
        description="task a",
        expires_at="2099-01-01T00:00:00Z",
    )
    second = native_dispatch_plan(
        tmp_path,
        run_id="run-scale-native",
        team_id="team-scale-native",
        max_concurrency=2,
    )
    assert second["active"] == 1
    assert [item["task_id"] for item in second["ready"]] == ["b"]
    assert second["blocked_by_capacity"] == 1
    with pytest.raises(TeamError, match="max_concurrency"):
        native_dispatch_plan(
            tmp_path,
            run_id="run-scale-native",
            team_id="team-scale-native",
            max_concurrency=max_workers_cap() + 1,
        )


# ---------------------------------------------------------------------------
# aborted scale intent receipts (orphan adoption)
# ---------------------------------------------------------------------------


def _prepare_scale_signal_team(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[str, dict[str, Any]]:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("orphan intent", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    live = dict(load_team_meta(tmp_path, rid))
    live["dry_run"] = False
    live["session"] = "omg-scale-orphan"
    live["tasks"] = [
        {
            **task,
            "pane_id": f"%{index + 31}",
            "pid": 31000 + index,
            "pgid": 41000 + index,
            "pid_start": f"start-{31000 + index}",
            "status": STATUS_RUNNING,
        }
        for index, task in enumerate(live["tasks"])
    ]
    _receipt, receipt_hash = plane._persist_team_launch_receipt(
        tmp_path,
        rid,
        session=live["session"],
        session_id="$31",
        launch_nonce="c" * 32,
        tasks=live["tasks"],
    )
    live.update(
        {
            "launch_nonce": "c" * 32,
            "launch_receipt_sha256": receipt_hash,
            "identity_generation": 0,
            "identity_receipt_sha256": receipt_hash,
        }
    )
    _write_team_meta(tmp_path, rid, live)
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: (live["session"], "$31"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "c" * 32)
    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(
        scaling,
        "_read_recorded_tmux_pane",
        lambda rec, **_kwargs: ("@32", str(rec["pane_id"])),
    )
    monkeypatch.setattr(scaling, "_tmux_pane_presence", lambda _pane_id: False)
    monkeypatch.setattr(
        scaling,
        "_list_pane_identities",
        lambda _session: {0: ("%31", 31000), 1: ("%32", 31001)},
    )
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda pid: f"start-{pid}")
    return rid, live


def test_scale_down_retry_after_aborted_signal_adopts_orphan_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.contracts.writer_chain import (
        canonical_json_bytes,
        parse_canonical_json_bytes,
        sha256_hex,
    )

    rid, _live = _prepare_scale_signal_team(monkeypatch, tmp_path)
    reads = iter([41001, 99999])
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: next(reads))
    monkeypatch.setattr(
        scaling.os, "killpg", lambda pgid, sig: (_ for _ in ()).throw(AssertionError)
    )

    with pytest.raises(TeamError, match="PGID drift"):
        scale_team(tmp_path, rid, remove=1)

    orphan_path = plane.team_identity_receipt_path(tmp_path, rid, 1)
    assert orphan_path.is_file()
    orphan_bytes = orphan_path.read_bytes()
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 0

    monkeypatch.setattr(
        scaling, "_pgid_for_pid", lambda pid: {31000: 41000, 31001: 41001}[pid]
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        scaling.os, "killpg", lambda pgid, sig: killed.append((pgid, int(sig)))
    )
    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    tmux_cmds: list[list[str]] = []

    def fake_tmux_run(args: Any, **kw: Any) -> Any:
        tmux_cmds.append(list(args))
        from unittest.mock import MagicMock

        m = MagicMock()
        m.returncode = 0
        return m

    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)

    out = scale_team(tmp_path, rid, remove=1)
    assert out["op"] == "remove"
    assert out["removed"] == 1
    assert killed and all(pg == 41001 for pg, _ in killed)

    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 1
    assert orphan_path.read_bytes() == orphan_bytes
    parsed = parse_canonical_json_bytes(orphan_bytes)
    assert disk["identity_receipt_sha256"] == sha256_hex(canonical_json_bytes(parsed))


def test_scale_down_retry_adopts_already_absent_victim_without_rekill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live = _prepare_scale_signal_team(monkeypatch, tmp_path)
    process_present = True
    pane_present = True
    kills = 0

    def first_kill(
        _rec: Any,
        *,
        actions: list[str],
        **_kwargs: Any,
    ) -> None:
        nonlocal process_present, pane_present, kills
        kills += 1
        process_present = False
        pane_present = False
        actions.append("simulated exact kill")

    real_mutate = scaling.mutate_team_meta
    monkeypatch.setattr(scaling, "_kill_pane_recorded", first_kill)
    monkeypatch.setattr(
        scaling,
        "mutate_team_meta",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("crash before meta")),
    )
    with pytest.raises(TeamError, match="crash before meta"):
        scale_team(tmp_path, rid, remove=1)

    monkeypatch.setattr(scaling, "mutate_team_meta", real_mutate)
    monkeypatch.setattr(
        scaling,
        "_pid_start_identity",
        lambda pid: f"start-{pid}" if process_present else None,
    )
    monkeypatch.setattr(
        scaling,
        "_pgid_for_pid",
        lambda pid: pid + 10000 if process_present else None,
    )
    monkeypatch.setattr(
        scaling,
        "_read_recorded_tmux_pane",
        lambda *_args, **_kwargs: ("@32", "%32") if pane_present else None,
    )
    monkeypatch.setattr(
        scaling,
        "_tmux_pane_presence",
        lambda _pane: pane_present,
    )

    out = scale_team(tmp_path, rid, remove=1)
    assert out["removed"] == 1
    assert kills == 1
    assert "remove retry already complete task=t-b" in out["actions"]
    assert load_team_meta(tmp_path, rid)["identity_generation"] == 1


def test_scale_down_retry_cleans_exact_dead_pane_after_signal_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live = _prepare_scale_signal_team(monkeypatch, tmp_path)
    process_present = True
    pane_present = True

    def signal_then_crash(
        _rec: Any,
        *,
        actions: list[str],
        **_kwargs: Any,
    ) -> None:
        nonlocal process_present
        process_present = False
        actions.append("simulated signal before pane cleanup")

    real_mutate = scaling.mutate_team_meta
    monkeypatch.setattr(scaling, "_kill_pane_recorded", signal_then_crash)
    monkeypatch.setattr(
        scaling,
        "mutate_team_meta",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("crash before meta")),
    )
    with pytest.raises(TeamError, match="crash before meta"):
        scale_team(tmp_path, rid, remove=1)

    monkeypatch.setattr(scaling, "mutate_team_meta", real_mutate)
    monkeypatch.setattr(
        scaling,
        "_pid_start_identity",
        lambda pid: f"start-{pid}" if process_present else None,
    )
    monkeypatch.setattr(
        scaling,
        "_pgid_for_pid",
        lambda pid: pid + 10000 if process_present else None,
    )
    monkeypatch.setattr(
        scaling,
        "_read_recorded_tmux_pane",
        lambda *_args, **_kwargs: ("@32", "%32") if pane_present else None,
    )
    monkeypatch.setattr(scaling, "_tmux_pane_presence", lambda _pane: pane_present)

    def kill_dead(*_args: Any, **_kwargs: Any) -> MagicMock:
        nonlocal pane_present
        pane_present = False
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaling, "_kill_recorded_tmux_pane_atomically", kill_dead)
    out = scale_team(tmp_path, rid, remove=1)
    assert out["removed"] == 1
    assert "remove retry killed dead pane=%32 task=t-b" in out["actions"]
    assert load_team_meta(tmp_path, rid)["identity_generation"] == 1


def test_scale_down_pending_remove_binds_receipt_victims_not_redrain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recovery must cancel receipt victims even if drain order would differ."""
    rid, live = _prepare_scale_signal_team(monkeypatch, tmp_path)
    # Prefer a non-default victim: force first attempt to fail after publishing
    # a receipt for the drain-preferred task, then retry with wrong n and right n.
    reads = iter([41001, 99999])
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: next(reads))
    monkeypatch.setattr(
        scaling.os, "killpg", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError)
    )
    with pytest.raises(TeamError, match="PGID drift"):
        scale_team(tmp_path, rid, remove=1)

    orphan = plane.team_identity_receipt_path(tmp_path, rid, 1)
    assert orphan.is_file()
    receipt = json.loads(orphan.read_text(encoding="utf-8"))
    before_ids = {row["task_id"] for row in receipt["tasks_before"]}
    after_ids = {row["task_id"] for row in receipt["tasks_after"]}
    receipt_victims = sorted(before_ids - after_ids)
    assert receipt_victims == ["t-b"]  # highest window_index drain

    with pytest.raises(TeamError, match=r"receipt_victims=\[.t-b.\].*scale --remove 1"):
        scale_team(tmp_path, rid, remove=2)

    monkeypatch.setattr(
        scaling, "_pgid_for_pid", lambda pid: {31000: 41000, 31001: 41001}[pid]
    )
    monkeypatch.setattr(scaling.os, "killpg", lambda *_a, **_k: None)
    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_tmux_pane_presence", lambda _pane: False)
    monkeypatch.setattr(
        scaling,
        "_read_recorded_tmux_pane",
        lambda rec, **_kwargs: ("@32", str(rec["pane_id"])),
    )
    monkeypatch.setattr(
        scaling,
        "_tmux_run",
        lambda *_a, **_k: MagicMock(returncode=0, stdout="", stderr=""),
    )
    out = scale_team(tmp_path, rid, remove=1)
    assert out["removed"] == 1
    assert out["task_ids"] == receipt_victims
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 1
    by_id = {t["task_id"]: t["status"] for t in disk["tasks"]}
    assert by_id["t-b"] == STATUS_SCALED_DOWN
    assert by_id["t-a"] != STATUS_SCALED_DOWN


def test_scale_down_meta_result_loss_is_adopted_on_identical_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If meta committed victims then raised, retry must adopt not refuse."""
    rid, _live = _prepare_scale_signal_team(monkeypatch, tmp_path)
    monkeypatch.setattr(
        scaling, "_pgid_for_pid", lambda pid: {31000: 41000, 31001: 41001}[pid]
    )
    monkeypatch.setattr(scaling.os, "killpg", lambda *_a, **_k: None)
    monkeypatch.setattr(scaling, "tmux_available", lambda: True)

    def fake_tmux_run(args: Any, **_kw: Any) -> Any:
        from unittest.mock import MagicMock

        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        # list-panes presence: empty => gone after kill
        if args and args[0] == "list-panes":
            m.stdout = ""
        return m

    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(scaling, "_tmux_pane_presence", lambda _pane: False)
    monkeypatch.setattr(
        scaling,
        "_read_recorded_tmux_pane",
        lambda rec, **_kwargs: ("@32", str(rec["pane_id"])),
    )

    real_mutate = scaling.mutate_team_meta
    calls = {"n": 0}

    def mutate_then_lose(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        real_mutate(*args, **kwargs)
        raise OSError("simulated scale-down meta result loss")

    monkeypatch.setattr(scaling, "mutate_team_meta", mutate_then_lose)
    # First call: commit succeeds on disk, caller observes OSError, then readback adopts.
    out = scale_team(tmp_path, rid, remove=1)
    assert out["op"] == "remove"
    assert out["removed"] == 1
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 1
    statuses = {t["task_id"]: t["status"] for t in disk["tasks"]}
    assert statuses["t-b"] == scaling.STATUS_SCALED_DOWN
    assert statuses["t-a"] != scaling.STATUS_SCALED_DOWN

    # Second call with committed state must not refuse for "only one active pane"
    # as if the first attempt never landed — identity gen already advanced.
    monkeypatch.setattr(scaling, "mutate_team_meta", real_mutate)
    with pytest.raises(TeamError, match="never remove below 1|scale --remove"):
        # only one active left; further remove of 1 is correctly refused
        scale_team(tmp_path, rid, remove=1)


def test_scale_down_dry_run_refuses_pending_remove_receipt_without_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rid, _live = _prepare_scale_signal_team(monkeypatch, tmp_path)
    reads = iter([41001, 99999])
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: next(reads))
    with pytest.raises(TeamError, match="PGID drift"):
        scale_team(tmp_path, rid, remove=1)

    before = load_team_meta(tmp_path, rid)
    with pytest.raises(TeamError, match="dry-run scale-down refused while identity receipt"):
        scale_team(tmp_path, rid, remove=1, dry_run=True)
    assert load_team_meta(tmp_path, rid) == before


def test_scale_down_tampered_orphan_receipt_stays_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.contracts.writer_chain import (
        canonical_json_bytes,
        parse_canonical_json_bytes,
    )

    rid, _live = _prepare_scale_signal_team(monkeypatch, tmp_path)
    reads = iter([41001, 99999])
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: next(reads))
    monkeypatch.setattr(
        scaling.os, "killpg", lambda pgid, sig: (_ for _ in ()).throw(AssertionError)
    )
    with pytest.raises(TeamError, match="PGID drift"):
        scale_team(tmp_path, rid, remove=1)

    orphan_path = plane.team_identity_receipt_path(tmp_path, rid, 1)
    tampered = parse_canonical_json_bytes(orphan_path.read_bytes())
    tampered["tasks_after"] = []
    orphan_path.chmod(0o600)
    orphan_path.write_bytes(canonical_json_bytes(tampered))
    orphan_path.chmod(0o400)

    monkeypatch.setattr(
        scaling, "_pgid_for_pid", lambda pid: {31000: 41000, 31001: 41001}[pid]
    )
    monkeypatch.setattr(scaling.os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(scaling, "tmux_available", lambda: True)

    with pytest.raises(TeamError, match="receipt intent/authority mismatch"):
        scale_team(tmp_path, rid, remove=1)
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("writer", "foreign-writer"),
        ("schema_version", 999),
        ("extra", "unexpected"),
        ("scale_intent", {"unexpected": True}),
    ],
)
def test_scale_down_pending_receipt_requires_exact_schema_before_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    from omg_cli.contracts.writer_chain import (
        canonical_json_bytes,
        parse_canonical_json_bytes,
    )

    rid, _live = _prepare_scale_signal_team(monkeypatch, tmp_path)
    reads = iter([41001, 99999])
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: next(reads))
    with pytest.raises(TeamError, match="PGID drift"):
        scale_team(tmp_path, rid, remove=1)

    receipt_path = plane.team_identity_receipt_path(tmp_path, rid, 1)
    receipt = parse_canonical_json_bytes(receipt_path.read_bytes())
    receipt[field] = value
    receipt_path.chmod(0o600)
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    receipt_path.chmod(0o400)

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda pid: pid + 10000)
    monkeypatch.setattr(
        scaling.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, int(sig))),
    )
    with pytest.raises(TeamError, match="receipt intent/authority mismatch"):
        scale_team(tmp_path, rid, remove=1)
    assert signals == []
    assert load_team_meta(tmp_path, rid)["identity_generation"] == 0
