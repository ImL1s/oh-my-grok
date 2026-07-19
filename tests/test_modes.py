"""Tests for omg_cli.modes — argv builder + run_mode skeleton."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from omg_cli.modes import (
    MODE_SKILL_REL,
    build_grok_argv,
    build_prompt,
    plugin_root,
    run_mode,
)
from omg_cli.state import load_active_run, load_run


def test_build_launch_argv_no_yolo_by_default():
    argv = build_grok_argv(mode="ulw", goal="fix tests", yolo=False, cwd="/tmp/proj")
    assert argv[0] == "grok"
    assert "-p" in argv
    assert "--yolo" not in argv and "bypassPermissions" not in " ".join(argv)
    assert any(
        "spawn_subagent" in a or "HARD RULE" in a or "omg-ultrawork" in a for a in argv
    )


def test_build_argv_includes_cwd_and_goal():
    argv = build_grok_argv(mode="ralph", goal="ship feature X", yolo=False, cwd="/tmp/p")
    assert "--cwd" in argv
    assert "/tmp/p" in argv
    p_idx = argv.index("-p")
    prompt = argv[p_idx + 1]
    assert "ship feature X" in prompt
    assert "omg-ralph" in prompt or "ONE" in prompt or "HARD RULE" in prompt


def test_yolo_maps_to_permission_mode_not_bare_yolo():
    """Grok has no --yolo; yolo=True -> --permission-mode bypassPermissions."""
    argv = build_grok_argv(mode="ulw", goal="go", yolo=True, cwd="/tmp")
    joined = " ".join(argv)
    assert "--yolo" not in argv  # flag does not exist on grok
    assert "bypassPermissions" in joined
    assert "--permission-mode" in argv
    assert "--always-approve" in argv


def test_safe_without_yolo_not_elevated():
    argv = build_grok_argv(mode="ulw", goal="go", yolo=False, safe=True, cwd="/tmp")
    joined = " ".join(argv)
    assert "bypassPermissions" not in joined
    assert "--always-approve" not in argv
    assert "--permission-mode" in argv
    pm_idx = argv.index("--permission-mode")
    assert argv[pm_idx + 1] == "default"


def test_yolo_ignored_when_safe_also_set():
    # safe wins over yolo for elevation (safer default when both present)
    argv = build_grok_argv(mode="ulw", goal="go", yolo=True, safe=True, cwd="/tmp")
    joined = " ".join(argv)
    assert "bypassPermissions" not in joined
    assert "--always-approve" not in argv
    assert "--permission-mode" in argv
    pm_idx = argv.index("--permission-mode")
    assert argv[pm_idx + 1] == "default"


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        build_grok_argv(mode="not-a-mode", goal="x")


def test_skill_files_exist():
    root = plugin_root()
    for mode, rel in MODE_SKILL_REL.items():
        assert (root / rel).is_file(), f"missing skill for {mode}: {rel}"


def test_build_prompt_contains_hard_rules():
    text = build_prompt("ralplan", "consensus on schema")
    assert "HARD RULE" in text or "spawn_subagent" in text
    assert "consensus on schema" in text


def test_dry_run_does_not_call_subprocess(monkeypatch, tmp_path):
    """dry_run must not launch grok (Popen). git rev-parse for ulw base_sha is OK."""
    real_popen = subprocess.Popen
    grok_launches: list[object] = []

    def selective_popen(argv, *a, **k):
        if argv and argv[0] == "git":
            return real_popen(argv, *a, **k)
        grok_launches.append(argv)
        raise AssertionError("grok Popen should not be used in dry_run")

    monkeypatch.setattr(subprocess, "Popen", selective_popen)

    rc = run_mode("ulw", "demo dry", root=tmp_path, dry_run=True)
    assert rc == 0
    assert grok_launches == []

    active = load_active_run(tmp_path)
    assert active is not None
    assert active["mode"] == "ulw"
    assert active["status"] in ("completed", "running", "verified")
    # no acceptance -> not verified
    assert active.get("verified") is False

    run_dir = tmp_path / ".omg" / "state" / "runs" / active["run_id"]
    assert (run_dir / "last_argv.json").is_file()
    argv = json.loads((run_dir / "last_argv.json").read_text(encoding="utf-8"))
    assert argv[0] == "grok"
    assert "-p" in argv
    assert "bypassPermissions" not in " ".join(argv)


def test_ralph_dry_run_writes_prd_and_no_verified(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    # require_acceptance default True for ralph → non-zero when not verified
    rc = run_mode("ralph", "persist until done", root=tmp_path, max_iter=3, dry_run=True)
    assert rc == 1
    run = load_active_run(tmp_path)
    assert run is not None
    assert run["verified"] is False
    assert run["status"] == "completed"
    rid = run["run_id"]
    assert (tmp_path / ".omg" / "state" / "runs" / rid / "prd.json").is_file()
    art = tmp_path / ".omg" / "artifacts" / f"prd-{rid}.json"
    assert art.is_file()
    prd = json.loads(art.read_text(encoding="utf-8"))
    assert prd["goal"] == "persist until done"
    assert prd["status"] == "scaffold"
    assert prd.get("version") == 1


def test_ralph_doesnt_set_verified_without_acceptance(monkeypatch, tmp_path):
    mock_proc = MagicMock()
    mock_proc.pid = 4242
    mock_proc.wait.return_value = 0

    monkeypatch.setattr(subprocess, "Popen", MagicMock(return_value=mock_proc))

    rc = run_mode("ralph", "no accept yet", root=tmp_path, max_iter=2, dry_run=False)
    # require_acceptance default → non-zero exit when never verified
    assert rc == 1
    run = load_active_run(tmp_path)
    assert run is not None
    assert run["verified"] is False
    assert run["status"] == "completed"
    # loop should have called grok max_iter times
    assert subprocess.Popen.call_count == 2
    # pid file written
    pid_path = tmp_path / ".omg" / "state" / "runs" / run["run_id"] / "pid"
    assert pid_path.is_file()
    assert pid_path.read_text(encoding="utf-8").strip() == "4242"


def test_forged_acceptance_does_not_set_verified(monkeypatch, tmp_path):
    """Agent-forged {passed:true} without omg-cli writer stamp is ignored."""
    mock_proc = MagicMock()
    mock_proc.pid = 7
    mock_proc.wait.return_value = 0
    real_popen = subprocess.Popen

    def selective_popen(argv, **kwargs):
        if argv and argv[0] == "grok":
            return mock_proc
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", selective_popen)

    from omg_cli import modes as modes_mod

    original_launch = modes_mod._launch_grok

    def launch_and_forge(argv, *, cwd, run_dir, timeout, dry_run):
        rid = run_dir.name
        acc = Path(cwd) / ".omg" / "artifacts" / f"{rid}-acceptance.json"
        acc.parent.mkdir(parents=True, exist_ok=True)
        acc.write_text(
            json.dumps({"passed": True, "note": "forged"}) + "\n",
            encoding="utf-8",
        )
        return original_launch(
            argv, cwd=cwd, run_dir=run_dir, timeout=timeout, dry_run=dry_run
        )

    monkeypatch.setattr(modes_mod, "_launch_grok", launch_and_forge)

    rc = run_mode(
        "ulw",
        "with forge",
        root=tmp_path,
        dry_run=False,
        require_acceptance=False,
    )
    assert rc == 0
    active = load_active_run(tmp_path)
    assert active is not None
    run = load_run(tmp_path, active["run_id"])
    assert run is not None
    assert run.get("verified") is False
    assert run.get("status") == "completed"


def test_set_verified_when_cli_acceptance_present(monkeypatch, tmp_path):
    """CLI freeze+run acceptance during launch path → verified."""
    mock_proc = MagicMock()
    mock_proc.pid = 7
    mock_proc.wait.return_value = 0
    real_popen = subprocess.Popen

    def selective_popen(argv, **kwargs):
        if argv and argv[0] == "grok":
            return mock_proc
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", selective_popen)

    from omg_cli import modes as modes_mod
    from omg_cli.acceptance import freeze_and_run

    original_launch = modes_mod._launch_grok

    def launch_and_accept(argv, *, cwd, run_dir, timeout, dry_run):
        rid = run_dir.name
        prd = {
            "version": 1,
            "goal": "with accept",
            "stories": [
                {"id": "s1", "title": "ok", "commands": [["true"]]}
            ],
            "global_commands": [],
        }
        freeze_and_run(Path(cwd), rid, prd)
        return original_launch(
            argv, cwd=cwd, run_dir=run_dir, timeout=timeout, dry_run=dry_run
        )

    monkeypatch.setattr(modes_mod, "_launch_grok", launch_and_accept)

    rc = run_mode("ulw", "with accept", root=tmp_path, dry_run=False)
    assert rc == 0
    active = load_active_run(tmp_path)
    assert active is not None
    run = load_run(tmp_path, active["run_id"])
    assert run is not None
    assert run.get("verified") is True
    assert run.get("status") == "verified"


def _selective_popen(real_popen, grok_handler):
    """Allow real git Popen (ulw base_sha probe); route other argv to handler."""

    def popen(argv, *a, **k):
        if argv and argv[0] == "git":
            return real_popen(argv, *a, **k)
        return grok_handler(argv, *a, **k)

    return popen


def test_failed_subprocess_marks_failed(monkeypatch, tmp_path):
    mock_proc = MagicMock()
    mock_proc.pid = 9
    mock_proc.wait.return_value = 1
    real = subprocess.Popen
    monkeypatch.setattr(
        subprocess,
        "Popen",
        _selective_popen(real, lambda *_a, **_k: mock_proc),
    )

    rc = run_mode("ulw", "fail me", root=tmp_path, dry_run=False)
    assert rc == 1
    run = load_active_run(tmp_path)
    assert run is not None
    assert run["status"] == "failed"
    assert run["verified"] is False


def test_popen_oserror_marks_failed_not_stuck_running(monkeypatch, tmp_path):
    """FileNotFoundError/OSError from Popen → failed status, non-zero rc, launch_error."""

    def raise_not_found(*_a, **_k):
        raise FileNotFoundError("No such file or directory: 'grok'")

    real = subprocess.Popen
    monkeypatch.setattr(
        subprocess, "Popen", _selective_popen(real, raise_not_found)
    )

    rc = run_mode("ulw", "missing binary", root=tmp_path, dry_run=False)
    assert rc != 0
    assert rc == 127

    run = load_active_run(tmp_path)
    assert run is not None
    assert run["status"] == "failed"
    assert run["verified"] is False
    assert run.get("exit_code") == 127

    run_dir = tmp_path / ".omg" / "state" / "runs" / run["run_id"]
    launch_err = run_dir / "launch_error"
    assert launch_err.is_file()
    assert "No such file" in launch_err.read_text(encoding="utf-8") or "grok" in launch_err.read_text(
        encoding="utf-8"
    )


def test_launch_grok_uses_start_new_session_on_posix(monkeypatch, tmp_path):
    """_launch_grok passes start_new_session=True on POSIX for process-group cancel."""
    import os

    captured: dict = {}

    mock_proc = MagicMock()
    mock_proc.pid = 1111
    mock_proc.wait.return_value = 0

    def fake_grok(argv, **kwargs):
        captured.update(kwargs)
        return mock_proc

    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", _selective_popen(real, fake_grok))

    rc = run_mode("ulw", "session leader", root=tmp_path, dry_run=False)
    assert rc == 0
    if os.name == "posix":
        assert captured.get("start_new_session") is True
    else:
        assert "start_new_session" not in captured


def test_run_mode_mutex_blocks_second_active(monkeypatch, tmp_path):
    """Second run_mode while first is non-terminal returns non-zero (mutex)."""
    real = subprocess.Popen

    def boom_grok(*_a, **_k):
        raise AssertionError("no popen")

    monkeypatch.setattr(subprocess, "Popen", _selective_popen(real, boom_grok))
    rc = run_mode("ulw", "first", root=tmp_path, dry_run=True)
    assert rc == 0
    # dry_run ends as completed (terminal) — re-open as running to simulate active
    active = load_active_run(tmp_path)
    assert active is not None
    from omg_cli.state import write_status

    write_status(tmp_path, active["run_id"], "running")

    rc2 = run_mode("ralph", "second", root=tmp_path, dry_run=True)
    assert rc2 != 0
    # first run still active
    still = load_active_run(tmp_path)
    assert still is not None
    assert still["run_id"] == active["run_id"]
