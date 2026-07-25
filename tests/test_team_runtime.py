"""Hermetic dry-run coverage for shorthand ``launch_team``."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omg_cli.team.plane import EXPERIMENTAL_ENV, WORKER_ENV_MARKERS
from omg_cli.team.runtime import launch_team, resolve_team_ref, team_ref_path


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "omg-test@example.com")
    _git(path, "config", "user.name", "omg-test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def test_launch_team_dry_run_seeds_ref_and_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    for key in WORKER_ENV_MARKERS:
        monkeypatch.delenv(key, raising=False)
    _init_repo(tmp_path)

    meta = launch_team(
        "1. lane one\n2. lane two",
        workers=2,
        role="executor",
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        env={EXPERIMENTAL_ENV: "1"},
    )
    assert meta["dry_run"] is True
    assert meta["launch_mode"] == "shorthand"
    assert meta["topology"] == "split"
    assert meta["task_count"] == 2
    team_name = meta["team_name"]
    assert team_ref_path(tmp_path, team_name).is_file()
    assert resolve_team_ref(tmp_path, team_name) == meta["run_id"]
    lane = tmp_path / ".omg" / "team-lanes" / "w1" / ".gitkeep"
    assert lane.is_file()
    # API board inboxes
    inboxes = list(tmp_path.joinpath(".omg", "state", "runs", meta["run_id"]).rglob(
        "inbox.md"
    ))
    assert len(inboxes) >= 2
    # Control plane still CLI-stamped
    team_json = json.loads(
        (
            tmp_path
            / ".omg"
            / "state"
            / "runs"
            / meta["run_id"]
            / "team"
            / "team.json"
        ).read_text(encoding="utf-8")
    )
    assert team_json["writer"] == "omg-cli"
    assert team_json["topology"] == "split"
    assert team_json.get("startup_acks") is None
    assert team_json.get("startup_status") is None
    assert meta.get("startup_acks") is None
    assert meta.get("startup_status") is None
    assert "dry_run skipped ACK wait" in str(meta.get("startup_note") or "")


def test_wait_for_startup_acks_full_partial_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team.mailbox import send_message
    from omg_cli.team.runtime import wait_for_startup_acks

    run_id = "run-ack-wait"
    team_id = "team"
    # Seed empty mailbox via first send is enough once control plane exists —
    # wait_for_startup_acks only reads mailbox; inject ACKs directly.
    monkeypatch.setenv("OMG_TEAM_READY_TIMEOUT_MS", "200")

    # Zero ACKs → failed_start
    zero = wait_for_startup_acks(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        expected_workers=["w1", "w2"],
        timeout_ms=50,
        poll_s=0.01,
    )
    assert zero["startup_status"] == "failed_start"
    assert zero["startup_acks"] == 0

    send_message(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        sender_id="w1",
        recipient_id="leader-fixed",
        body="ACK",
        generation=0,
        kind="ack",
        dedupe_key="ack-w1",
    )
    partial = wait_for_startup_acks(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        expected_workers=["w1", "w2"],
        timeout_ms=50,
        poll_s=0.01,
    )
    assert partial["startup_status"] == "degraded"
    assert partial["startup_acks"] == 1
    assert partial["startup_ack_workers"] == ["w1"]

    send_message(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        sender_id="w2",
        recipient_id="leader-fixed",
        body="ACK",
        generation=0,
        kind="ack",
        dedupe_key="ack-w2",
    )
    full = wait_for_startup_acks(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        expected_workers=["w1", "w2"],
        timeout_ms=200,
        poll_s=0.01,
    )
    assert full["startup_status"] == "running"
    assert full["startup_acks"] == 2
    assert full["startup_ack_workers"] == ["w1", "w2"]


def test_ready_timeout_ms_rejects_junk(monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.team.plane import TeamGateError
    from omg_cli.team.runtime import ready_timeout_ms

    monkeypatch.delenv("OMG_TEAM_READY_TIMEOUT_MS", raising=False)
    assert ready_timeout_ms() == 45_000
    assert ready_timeout_ms({"OMG_TEAM_READY_TIMEOUT_MS": "1000"}) == 1000
    with pytest.raises(TeamGateError):
        ready_timeout_ms({"OMG_TEAM_READY_TIMEOUT_MS": "nope"})

