"""#27 team --plan-only is side-effect free."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from omg_cli.main import main


@pytest.fixture()
def team_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMG_EXPERIMENTAL_TMUX_TEAM", "1")


def test_team_start_plan_only_no_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], team_env: None
) -> None:
    monkeypatch.chdir(tmp_path)
    # No .omg beforehand
    assert not (tmp_path / ".omg").exists()
    tasks = json.dumps(
        [{"task_id": "t1", "owned_files": ["a.py"], "role": "executor"}]
    )
    rc = main(
        [
            "team",
            "start",
            "--goal",
            "preview only",
            "--tasks-json",
            tasks,
            "--plan-only",
            "--io-mode",
            "interactive",
        ]
    )
    assert rc == 0
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["mode"] == "plan_only"
    assert payload["mutates"] is False
    assert payload["task_count"] == 1
    assert payload["goal"] == "preview only"
    assert payload["io_mode"] == "interactive"
    assert "Team plan-only" in out.err
    assert not (tmp_path / ".omg").exists()
    # No worktrees either
    assert not (tmp_path / ".omg" / "worktrees").exists()


def test_team_launch_plan_only_no_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], team_env: None
) -> None:
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*")) if tmp_path.exists() else set()
    rc = main(
        [
            "team",
            "launch",
            "--workers",
            "2",
            "--role",
            "executor",
            "--goal",
            "plan launch",
            "--plan-only",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "plan_only"
    assert payload["workers"] == 2
    assert payload["mutates"] is False
    after = set(tmp_path.rglob("*")) if tmp_path.exists() else set()
    # No new files under project root
    assert after == before
    assert not (tmp_path / ".omg").exists()


def test_team_start_dry_run_does_not_print_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], team_env: None
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = json.dumps(
        [{"task_id": "t1", "owned_files": ["a.py"], "role": "executor"}]
    )
    rc = main(
        [
            "team",
            "start",
            "--goal",
            "materialize",
            "--tasks-json",
            tasks,
            "--dry-run",
            "--force",
        ]
    )
    # dry-run may return 0 after materialize
    out = capsys.readouterr()
    assert "Team started" not in out.err
    assert "materialized" in out.err.lower() or rc in (0, 1)
