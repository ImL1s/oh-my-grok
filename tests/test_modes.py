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


def _stub_process_starttime(monkeypatch, starttime: str = "fake-start") -> None:
    """Hermetic pid.json publish for tests that use non-live mock PIDs."""
    monkeypatch.setattr(
        "omg_cli.state.process_starttime", lambda _pid: starttime
    )


def _install_mock_popen_lifecycle(
    monkeypatch, mock_proc, *, starttime: str = "fake-start"
):
    """Stub Popen + identity so the mock is live only until wait() returns.

    Other PIDs keep real liveness/starttime (execution-owner probes must
    still work). A real OS process at ``mock_proc.pid`` must not look like
    a matching leader after the mocked grok has exited.
    """
    import omg_cli.state as state_mod

    live = {"on": False}
    real_alive = state_mod._pid_alive
    real_start = state_mod.process_starttime
    real_popen = subprocess.Popen
    mock_pid = int(mock_proc.pid)

    def wait_and_exit(*_a, **_k):
        live["on"] = False
        return 0

    mock_proc.wait.side_effect = wait_and_exit

    def fake_popen(*_a, **_k):
        live["on"] = True
        return mock_proc

    popen = MagicMock(side_effect=_selective_popen(real_popen, fake_popen))
    monkeypatch.setattr(subprocess, "Popen", popen)

    def pid_alive(pid: int):
        if int(pid) == mock_pid:
            return bool(live["on"])
        return real_alive(pid)

    def process_starttime(pid: int):
        if int(pid) == mock_pid:
            return starttime
        return real_start(pid)

    monkeypatch.setattr(state_mod, "_pid_alive", pid_alive)
    monkeypatch.setattr(state_mod, "process_starttime", process_starttime)
    return popen


def test_build_launch_argv_no_yolo_by_default():
    argv = build_grok_argv(mode="ulw", goal="fix tests", yolo=False, cwd="/tmp/proj")
    assert argv[0] == "grok"
    assert "-p" in argv
    assert "--yolo" not in argv and "bypassPermissions" not in " ".join(argv)
    assert any(
        "spawn_subagent" in a or "HARD RULE" in a or "omg-ultrawork" in a for a in argv
    )
    # headless defaults: --cwd when known + --output-format plain
    assert "--cwd" in argv
    assert "/tmp/proj" in argv
    assert "--output-format" in argv
    of_idx = argv.index("--output-format")
    assert argv[of_idx + 1] == "plain"


def test_build_argv_includes_cwd_and_goal():
    argv = build_grok_argv(mode="ralph", goal="ship feature X", yolo=False, cwd="/tmp/p")
    assert "--cwd" in argv
    assert "/tmp/p" in argv
    p_idx = argv.index("-p")
    prompt = argv[p_idx + 1]
    assert "ship feature X" in prompt
    assert "omg-ralph" in prompt or "ONE" in prompt or "HARD RULE" in prompt


def test_build_argv_always_cwd_when_path_known():
    argv = build_grok_argv(mode="ulw", goal="x", cwd="/known/path")
    assert argv.index("--cwd") >= 0
    assert argv[argv.index("--cwd") + 1] == "/known/path"
    # without cwd, flag absent
    argv2 = build_grok_argv(mode="ulw", goal="x", cwd=None)
    assert "--cwd" not in argv2


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
    assert argv[pm_idx + 1] == "plan"


def test_yolo_ignored_when_safe_also_set():
    # safe wins over yolo for elevation (safer default when both present)
    argv = build_grok_argv(mode="ulw", goal="go", yolo=True, safe=True, cwd="/tmp")
    joined = " ".join(argv)
    assert "bypassPermissions" not in joined
    assert "--always-approve" not in argv
    assert "--permission-mode" in argv
    pm_idx = argv.index("--permission-mode")
    assert argv[pm_idx + 1] == "plan"


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        build_grok_argv(mode="not-a-mode", goal="x")


def test_disallow_shell_injects_disallowed_tools():
    argv = build_grok_argv(
        mode="ralplan", goal="review", cwd="/tmp", disallow_shell=True
    )
    assert "--disallowed-tools" in argv
    idx = argv.index("--disallowed-tools")
    assert "run_terminal_command" in argv[idx + 1]


def test_disallow_shell_false_by_default_for_leaders():
    """ulw/ralph leaders must keep shell (do not strip by default)."""
    for mode in ("ulw", "ralph"):
        argv = build_grok_argv(mode=mode, goal="go", cwd="/tmp", disallow_shell=False)
        assert "--disallowed-tools" not in argv


def test_disallow_shell_via_env(monkeypatch):
    monkeypatch.setenv("OMG_DISALLOW_SHELL", "1")
    argv = build_grok_argv(mode="ulw", goal="go", cwd="/tmp", disallow_shell=False)
    assert "--disallowed-tools" in argv
    monkeypatch.delenv("OMG_DISALLOW_SHELL", raising=False)
    argv2 = build_grok_argv(mode="ulw", goal="go", cwd="/tmp", disallow_shell=False)
    assert "--disallowed-tools" not in argv2


def test_disallow_shell_skips_when_already_in_extra():
    argv = build_grok_argv(
        mode="ralplan",
        goal="x",
        cwd="/tmp",
        disallow_shell=True,
        extra=["--disallowed-tools", "run_terminal_command,Bash"],
    )
    # only one occurrence (from extra, not double-injected)
    count = sum(1 for a in argv if a == "--disallowed-tools")
    assert count == 1



def test_skill_files_exist():
    root = plugin_root()
    for mode, rel in MODE_SKILL_REL.items():
        assert (root / rel).is_file(), f"missing skill for {mode}: {rel}"


def test_build_prompt_contains_hard_rules():
    text = build_prompt("ralplan", "consensus on schema")
    assert "HARD RULE" in text or "spawn_subagent" in text
    assert "consensus on schema" in text


def test_ralph_prompt_context_pack_when_iteration_set(tmp_path):
    """When ralph iteration is set, prompt includes context pack fields."""
    from omg_cli.modes import ralph_context_pack
    from omg_cli.state import create_run

    run = create_run(tmp_path, mode="ralph", goal="ctx pack")
    rid = run["run_id"]
    # seed prd with a story + commands for frozen summary
    prd_path = tmp_path / ".omg" / "state" / "runs" / rid / "prd.json"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        json.dumps(
            {
                "version": 1,
                "goal": "ctx pack",
                "current_story": "s1: wire acceptance",
                "stories": [
                    {
                        "id": "s1",
                        "title": "wire acceptance",
                        "commands": [["pytest", "tests/test_foo.py", "-q"]],
                    }
                ],
                "global_commands": [["true"]],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    text = build_prompt(
        "ralph",
        "ctx pack goal",
        iteration=2,
        max_iter=5,
        run_id=rid,
        project_root=tmp_path,
    )
    assert "Ralph context pack" in text
    assert f"run_id: {rid}" in text or rid in text
    assert "iteration: 2/5" in text
    assert "story:" in text
    assert "s1" in text or "wire acceptance" in text
    assert "frozen_commands_summary:" in text
    assert "pytest" in text or "true" in text
    assert "acceptance.result.json" in text
    assert rid in text

    pack = ralph_context_pack(
        run_id=rid,
        iteration=2,
        max_iter=5,
        project_root=tmp_path,
    )
    assert "run_id:" in pack
    assert "iteration: 2/5" in pack
    assert "story:" in pack
    assert "frozen_commands_summary:" in pack
    assert "acceptance.result.json" in pack


def test_ralph_prompt_context_pack_without_prd():
    """Context pack still emits required fields with placeholders when no prd."""
    text = build_prompt(
        "ralph",
        "bare",
        iteration=1,
        max_iter=3,
        run_id="run-abc",
    )
    assert "Ralph context pack" in text
    assert "run_id: run-abc" in text
    assert "iteration: 1/3" in text
    assert "story:" in text
    assert "frozen_commands_summary:" in text
    assert "acceptance.result.json" in text


def test_resolve_launch_timeout_defaults():
    from omg_cli.modes import DEFAULT_TIMEOUT, resolve_launch_timeout

    assert DEFAULT_TIMEOUT == 3600.0
    assert resolve_launch_timeout(None, dry_run=False) == 3600.0
    assert resolve_launch_timeout(120.0, dry_run=False) == 120.0
    assert resolve_launch_timeout(0, dry_run=False) is None  # unlimited
    # dry_run leaves None alone (no process)
    assert resolve_launch_timeout(None, dry_run=True) is None


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
    # _launch_grok rewrites -p skill bodies to --prompt-file (YAML --- frontmatter)
    assert "--prompt-file" in argv or "-p" in argv
    if "--prompt-file" in argv:
        pf = Path(argv[argv.index("--prompt-file") + 1])
        assert pf.is_file()
        assert (run_dir / "last_prompt.md").is_file()
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
    assert run["status"] == "blocked"
    assert run["next_action"].startswith(f"omg ralph --resume {run['run_id']}")
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
    _install_mock_popen_lifecycle(monkeypatch, mock_proc)

    rc = run_mode("ralph", "no accept yet", root=tmp_path, max_iter=2, dry_run=False)
    # require_acceptance default → non-zero exit when never verified
    assert rc == 1
    run = load_active_run(tmp_path)
    assert run is not None
    assert run["verified"] is False
    assert run["status"] == "blocked"
    assert run["blocker"]["code"] == "not_verified"
    # loop should have called grok max_iter times (Popen may also be used by
    # process_starttime via subprocess.run → ignore non-grok argv)
    grok_calls = [
        c
        for c in subprocess.Popen.call_args_list
        if c.args and c.args[0] and c.args[0][0] == "grok"
    ]
    assert len(grok_calls) == 2
    # pid file written
    pid_path = tmp_path / ".omg" / "state" / "runs" / run["run_id"] / "pid"
    assert pid_path.is_file()
    assert pid_path.read_text(encoding="utf-8").strip() == "4242"


def test_ralph_unrelated_live_pid_does_not_block_second_iteration(
    monkeypatch, tmp_path
):
    """Occupant of the old hardcoded 4242 must not fake a live leader."""
    import omg_cli.state as state_mod

    mock_proc = MagicMock()
    mock_proc.pid = 9_000_001
    _install_mock_popen_lifecycle(monkeypatch, mock_proc)

    inner_alive = state_mod._pid_alive
    inner_start = state_mod.process_starttime

    def pid_alive(pid: int):
        if int(pid) == 4242:
            return True
        return inner_alive(pid)

    def process_starttime(pid: int):
        if int(pid) == 4242:
            return "fake-start"
        return inner_start(pid)

    monkeypatch.setattr(state_mod, "_pid_alive", pid_alive)
    monkeypatch.setattr(state_mod, "process_starttime", process_starttime)

    rc = run_mode(
        "ralph", "collision-proof mock pid", root=tmp_path, max_iter=2, dry_run=False
    )
    assert rc == 1
    run = load_active_run(tmp_path)
    assert run is not None
    assert run["verified"] is False
    assert run["status"] == "blocked"
    assert run["blocker"]["code"] == "not_verified"
    grok_calls = [
        c
        for c in subprocess.Popen.call_args_list
        if c.args and c.args[0] and c.args[0][0] == "grok"
    ]
    assert len(grok_calls) == 2
    pid_path = tmp_path / ".omg" / "state" / "runs" / run["run_id"] / "pid"
    assert pid_path.is_file()
    assert pid_path.read_text(encoding="utf-8").strip() == "9000001"


def test_existing_v1_ralph_keeps_legacy_completed_terminal_semantics(
    monkeypatch, tmp_path
):
    """Strict-v2 blocked semantics must not rewrite an existing v1 run."""
    from omg_cli.state import create_run

    run = create_run(tmp_path, mode="ralph", goal="legacy existing run")
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )

    rc = run_mode(
        "ralph",
        "legacy existing run",
        root=tmp_path,
        existing_run_id=run["run_id"],
        max_iter=1,
        dry_run=True,
        require_acceptance=False,
    )

    assert rc == 0
    final = load_run(tmp_path, run["run_id"])
    assert final is not None
    assert "schema_version" not in final
    assert final["status"] == "completed"
    assert final["verified"] is False


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

    _stub_process_starttime(monkeypatch)
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

    _stub_process_starttime(monkeypatch)
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
    _stub_process_starttime(monkeypatch)
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


def test_ulw_auto_integrate_missing_ok(monkeypatch, tmp_path):
    """ULW completes with no envelopes → still exit 0 (solo smoke)."""
    mock_proc = MagicMock()
    mock_proc.pid = 11
    mock_proc.wait.return_value = 0
    real = subprocess.Popen
    _stub_process_starttime(monkeypatch)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        _selective_popen(real, lambda *_a, **_k: mock_proc),
    )
    rc = run_mode("ulw", "solo smoke", root=tmp_path, dry_run=False)
    assert rc == 0


def test_ulw_auto_integrate_helper_statuses(monkeypatch, tmp_path):
    """_ulw_auto_integrate: missing→0, ok→0, failed→1."""
    from omg_cli.modes import _ulw_auto_integrate
    from omg_cli.state import create_run

    run = create_run(tmp_path, mode="ulw", goal="x", force=True)
    rid = run["run_id"]

    monkeypatch.setattr(
        "omg_cli.integrate.integrate_results",
        lambda *a, **k: {"status": "missing"},
    )
    assert _ulw_auto_integrate(tmp_path, rid) == 0

    monkeypatch.setattr(
        "omg_cli.integrate.integrate_results",
        lambda *a, **k: {"status": "ok"},
    )
    assert _ulw_auto_integrate(tmp_path, rid) == 0

    monkeypatch.setattr(
        "omg_cli.integrate.integrate_results",
        lambda *a, **k: {"status": "failed", "error": "base_sha_mismatch"},
    )
    assert _ulw_auto_integrate(tmp_path, rid) == 1


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
    _stub_process_starttime(monkeypatch)
    monkeypatch.setattr(subprocess, "Popen", _selective_popen(real, fake_grok))

    rc = run_mode("ulw", "session leader", root=tmp_path, dry_run=False)
    assert rc == 0
    if os.name == "posix":
        assert captured.get("start_new_session") is True
    else:
        assert "start_new_session" not in captured


def test_spawn_grok_process_kills_child_when_pid_metadata_fails(
    monkeypatch, tmp_path
):
    """R13-1: pid.json publish failure must kill the child (no orphan) and raise."""
    from omg_cli.modes import _spawn_grok_process

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    mock_proc = MagicMock()
    mock_proc.pid = 7777
    killed: list[int] = []

    def fake_popen(argv, **kwargs):
        return mock_proc

    def boom_write(*_a, **_k):
        raise OSError("disk full")

    def fake_killpg(pid, _sig):
        killed.append(pid)

    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", _selective_popen(real, fake_popen))
    monkeypatch.setattr("omg_cli.state.write_pid_metadata", boom_write)
    monkeypatch.setattr("os.killpg", fake_killpg)

    with pytest.raises(OSError, match="disk full"):
        _spawn_grok_process(["grok", "-p", "hi"], cwd=tmp_path, run_dir=run_dir)

    assert killed == [7777] or mock_proc.kill.called
    assert not (run_dir / "pid.json").is_file()


def test_spawn_grok_process_kills_child_when_starttime_unavailable(
    monkeypatch, tmp_path
):
    """R14-4: missing process starttime aborts pid publish and kills the child."""
    from omg_cli.modes import _spawn_grok_process

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    mock_proc = MagicMock()
    mock_proc.pid = 7788
    killed: list[int] = []

    def fake_popen(argv, **kwargs):
        return mock_proc

    def fake_killpg(pid, _sig):
        killed.append(pid)

    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", _selective_popen(real, fake_popen))
    monkeypatch.setattr("omg_cli.state.process_starttime", lambda _pid: None)
    monkeypatch.setattr("os.killpg", fake_killpg)

    with pytest.raises(RuntimeError, match="starttime"):
        _spawn_grok_process(["grok", "-p", "hi"], cwd=tmp_path, run_dir=run_dir)

    assert killed == [7788] or mock_proc.kill.called
    assert not (run_dir / "pid.json").is_file()
    assert not (run_dir / "pid").is_file()


def test_launch_grok_still_spawn_then_wait(monkeypatch, tmp_path):
    """R13-1: ``_launch_grok`` remains spawn+wait for ralplan/dual_review callers."""
    from omg_cli.modes import _launch_grok

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    mock_proc = MagicMock()
    mock_proc.pid = 8888
    mock_proc.wait.return_value = 0
    real = subprocess.Popen
    _stub_process_starttime(monkeypatch)
    monkeypatch.setattr(
        subprocess, "Popen", _selective_popen(real, lambda *_a, **_k: mock_proc)
    )

    rc = _launch_grok(
        ["grok", "-p", "hello"],
        cwd=tmp_path,
        run_dir=run_dir,
        timeout=None,
        dry_run=False,
    )
    assert rc == 0
    mock_proc.wait.assert_called()
    assert (run_dir / "pid.json").is_file()
    assert (run_dir / "last_argv.json").is_file()


def test_run_mode_mutex_blocks_second_active(monkeypatch, tmp_path):
    """Second run_mode while first is non-terminal returns non-zero (mutex)."""
    from omg_cli.state import create_run, write_status

    # R17: terminal statuses are absorbing via write_status — seed a live
    # non-terminal run directly instead of reopening completed → running.
    first = create_run(tmp_path, mode="ulw", goal="first")
    write_status(tmp_path, first["run_id"], "running")
    active = load_active_run(tmp_path)
    assert active is not None
    assert active["run_id"] == first["run_id"]
    assert active["status"] == "running"

    real = subprocess.Popen

    def boom_grok(*_a, **_k):
        raise AssertionError("no popen")

    monkeypatch.setattr(subprocess, "Popen", _selective_popen(real, boom_grok))

    rc2 = run_mode("ralph", "second", root=tmp_path, dry_run=True)
    assert rc2 != 0
    # first run still active
    still = load_active_run(tmp_path)
    assert still is not None
    assert still["run_id"] == active["run_id"]


def test_launch_grok_dry_run_under_held_transition_guard(tmp_path):
    """R15 hotfix: nested transition_guard must not deadlock (autopilot dry-run)."""
    from omg_cli.modes import _launch_grok, _run_dir
    from omg_cli.state import create_run, transition_guard

    run = create_run(tmp_path, mode="autopilot", goal="nested dry-run")
    rid = run["run_id"]
    run_dir = _run_dir(tmp_path, rid)
    with transition_guard(tmp_path, rid):
        rc = _launch_grok(
            ["grok", "-p", "hello"],
            cwd=tmp_path,
            run_dir=run_dir,
            timeout=None,
            dry_run=True,
        )
    assert rc == 0
    assert (run_dir / "dry_run").is_file()


def test_launch_grok_refuses_cancelled_status(monkeypatch, tmp_path, capsys):
    """R16-1: terminal cancelled status must refuse `_launch_grok` (no Popen)."""
    from omg_cli.modes import _launch_grok, _run_dir
    from omg_cli.state import cancel_run, create_run

    run = create_run(tmp_path, mode="ralph", goal="r16 cancelled refuse")
    rid = run["run_id"]
    run_dir = _run_dir(tmp_path, rid)
    cancel_run(tmp_path, rid, kill_grace_s=0)

    launched: list[bool] = []

    def fake_popen(*_a, **_k):
        launched.append(True)
        raise AssertionError("must not spawn when cancelled")

    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", _selective_popen(real, fake_popen))

    rc = _launch_grok(
        ["grok", "-p", "hello"],
        cwd=tmp_path,
        run_dir=run_dir,
        timeout=None,
        dry_run=False,
    )
    assert rc != 0
    assert not launched
    err = capsys.readouterr().err
    assert "terminal" in err.lower() or "cancelled" in err.lower()


def test_launch_grok_refuses_pending_cancel_request(monkeypatch, tmp_path, capsys):
    """R16-1: pending cancel.request.json must refuse `_launch_grok` (no Popen)."""
    from omg_cli.modes import _launch_grok, _run_dir
    from omg_cli.state import create_run

    run = create_run(tmp_path, mode="ralph", goal="r16 pending cancel refuse")
    rid = run["run_id"]
    run_dir = _run_dir(tmp_path, rid)
    (run_dir / "cancel.request.json").write_text(
        json.dumps(
            {
                "writer": "omg-cli",
                "run_id": rid,
                "request_id": "pending-request",
                "requested_at": "2026-08-05T00:00:00+00:00",
                "observed_generation": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    launched: list[bool] = []

    def fake_popen(*_a, **_k):
        launched.append(True)
        raise AssertionError("must not spawn when cancel pending")

    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", _selective_popen(real, fake_popen))

    rc = _launch_grok(
        ["grok", "-p", "hello"],
        cwd=tmp_path,
        run_dir=run_dir,
        timeout=None,
        dry_run=False,
    )
    assert rc != 0
    assert not launched
    err = capsys.readouterr().err
    assert "cancel" in err.lower()


def test_launch_grok_refuses_live_leader_pid_match(
    monkeypatch, tmp_path, capsys
):
    """R15-3: live MATCH pid.json must refuse `_launch_grok` spawn (no Popen)."""
    import os

    from omg_cli.modes import _launch_grok, _run_dir
    from omg_cli.state import create_run, process_starttime, write_pid_metadata

    run = create_run(tmp_path, mode="ralph", goal="r15 live match")
    rid = run["run_id"]
    run_dir = _run_dir(tmp_path, rid)
    our_pid = os.getpid()
    start = process_starttime(our_pid)
    assert start is not None and start.strip()
    write_pid_metadata(run_dir / "pid.json", pid=our_pid, pgid=our_pid, starttime=start)

    launched: list[bool] = []

    def fake_popen(*_a, **_k):
        launched.append(True)
        raise AssertionError("must not spawn")

    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", _selective_popen(real, fake_popen))

    rc = _launch_grok(
        ["grok", "-p", "hello"],
        cwd=tmp_path,
        run_dir=run_dir,
        timeout=None,
        dry_run=False,
    )
    assert rc != 0
    assert not launched
    assert (run_dir / "pid.json").is_file()
    err = capsys.readouterr().err
    assert "live leader" in err.lower() or "pid" in err.lower()


def test_launch_grok_refuses_live_leader_missing_starttime(
    monkeypatch, tmp_path, capsys
):
    """R15-3: live PID without recorded starttime must refuse (do not clear)."""
    import json
    import os

    from omg_cli.modes import _launch_grok, _run_dir
    from omg_cli.state import create_run

    run = create_run(tmp_path, mode="ralph", goal="r15 missing starttime")
    rid = run["run_id"]
    run_dir = _run_dir(tmp_path, rid)
    our_pid = os.getpid()
    (run_dir / "pid.json").write_text(
        json.dumps({"pid": our_pid, "starttime": None, "pgid": our_pid}) + "\n",
        encoding="utf-8",
    )

    launched: list[bool] = []

    def fake_popen(*_a, **_k):
        launched.append(True)
        raise AssertionError("must not spawn")

    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", _selective_popen(real, fake_popen))

    rc = _launch_grok(
        ["grok", "-p", "hello"],
        cwd=tmp_path,
        run_dir=run_dir,
        timeout=None,
        dry_run=False,
    )
    assert rc != 0
    assert not launched
    assert (run_dir / "pid.json").is_file()
    err = capsys.readouterr().err
    assert "starttime" in err.lower() or "live leader" in err.lower()


def test_launch_grok_refuses_unknown_starttime_probe(
    monkeypatch, tmp_path, capsys
):
    """R15-3: alive + recorded starttime + process_starttime=None → refuse."""
    import os

    from omg_cli import state as state_mod
    from omg_cli.modes import _launch_grok, _run_dir
    from omg_cli.state import create_run, write_pid_metadata

    run = create_run(tmp_path, mode="ralph", goal="r15 unknown probe")
    rid = run["run_id"]
    run_dir = _run_dir(tmp_path, rid)
    our_pid = os.getpid()
    write_pid_metadata(
        run_dir / "pid.json",
        pid=our_pid,
        pgid=our_pid,
        starttime="Mon Jan  1 00:00:00 2000",
    )
    monkeypatch.setattr(state_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(state_mod, "process_starttime", lambda _pid: None)

    launched: list[bool] = []

    def fake_popen(*_a, **_k):
        launched.append(True)
        raise AssertionError("must not spawn")

    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", _selective_popen(real, fake_popen))

    rc = _launch_grok(
        ["grok", "-p", "hello"],
        cwd=tmp_path,
        run_dir=run_dir,
        timeout=None,
        dry_run=False,
    )
    assert rc != 0
    assert not launched
    assert (run_dir / "pid.json").is_file()
    err = capsys.readouterr().err
    assert "unknown" in err.lower() or "live leader" in err.lower()


def test_launch_grok_clears_stale_dead_leader_then_spawns(monkeypatch, tmp_path):
    """R15-3: dead PID in pid.json is stale — clear then allow spawn."""
    from omg_cli.modes import _launch_grok, _run_dir
    from omg_cli.state import create_run, write_pid_metadata

    run = create_run(tmp_path, mode="ralph", goal="r15 stale dead")
    rid = run["run_id"]
    run_dir = _run_dir(tmp_path, rid)
    dead_pid = 2_000_000_001
    write_pid_metadata(
        run_dir / "pid.json",
        pid=dead_pid,
        pgid=dead_pid,
        starttime="Mon Jan  1 00:00:00 1970",
    )

    mock_proc = MagicMock()
    mock_proc.pid = 9991
    mock_proc.wait.return_value = 0
    cleared_before_spawn: list[bool] = []

    def fake_popen(*_a, **_k):
        cleared_before_spawn.append(not (run_dir / "pid.json").exists())
        return mock_proc

    real = subprocess.Popen
    _stub_process_starttime(monkeypatch)
    monkeypatch.setattr(subprocess, "Popen", _selective_popen(real, fake_popen))

    rc = _launch_grok(
        ["grok", "-p", "hello"],
        cwd=tmp_path,
        run_dir=run_dir,
        timeout=None,
        dry_run=False,
    )
    assert rc == 0
    assert cleared_before_spawn == [True]


def test_ralph_resume_refuses_live_leader_pid(monkeypatch, tmp_path, capsys):
    """R15-3: ralph/run_mode launch refuses when live leader pid still matches."""
    import os

    from omg_cli.host_session import allocate_host_session
    from omg_cli.modes import _run_dir
    from omg_cli.state import (
        create_run,
        execution_lease,
        process_starttime,
        write_pid_metadata,
        write_status,
    )

    run = create_run(
        tmp_path,
        mode="ralph",
        goal="r15 ralph resume live",
        extra={"schema_version": 2, "lifecycle_version": 2, "max_iter": 3},
    )
    rid = run["run_id"]
    binding = allocate_host_session()
    with execution_lease(tmp_path, rid, intent="test-seed", timeout_s=5.0) as lease:
        write_status(
            tmp_path,
            rid,
            "running",
            extra={
                **binding.status_fields(),
                "iterations_completed": 0,
                "max_iter": 3,
            },
            lease=lease,
        )
    run_dir = _run_dir(tmp_path, rid)
    our_pid = os.getpid()
    start = process_starttime(our_pid)
    assert start is not None
    write_pid_metadata(run_dir / "pid.json", pid=our_pid, pgid=our_pid, starttime=start)

    launched: list[bool] = []

    def fake_popen(*_a, **_k):
        launched.append(True)
        raise AssertionError("must not spawn on live leader")

    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", _selective_popen(real, fake_popen))

    rc = run_mode(
        "ralph",
        "",
        root=tmp_path,
        resume_run_id=rid,
        max_iter=3,
        dry_run=False,
        require_acceptance=False,
    )
    assert rc != 0
    assert not launched
    assert (run_dir / "pid.json").is_file()
    err = capsys.readouterr().err
    assert "live leader" in err.lower() or "pid" in err.lower()


def test_legacy_cancel_then_launch_refuses_spawn(monkeypatch, tmp_path, capsys):
    """R17-1: legacy cancel-first linearization → `_launch_grok` refuses Popen."""
    from omg_cli.modes import _launch_grok, _run_dir
    from omg_cli.state import cancel_run, create_run, load_run

    run = create_run(tmp_path, mode="ulw", goal="r17 cancel-first")
    rid = run["run_id"]
    run_dir = _run_dir(tmp_path, rid)
    cancel_run(tmp_path, rid, kill_grace_s=0)
    assert load_run(tmp_path, rid)["status"] == "cancelled"

    launched: list[bool] = []

    def fake_popen(*_a, **_k):
        launched.append(True)
        raise AssertionError("must not spawn after legacy cancel")

    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", _selective_popen(real, fake_popen))

    rc = _launch_grok(
        ["grok", "-p", "hello"],
        cwd=tmp_path,
        run_dir=run_dir,
        timeout=None,
        dry_run=False,
    )
    assert rc != 0
    assert not launched
    assert "terminal" in capsys.readouterr().err.lower()


def test_legacy_cancel_vs_launch_launch_holds_guard_then_cancel_sees_pid(
    monkeypatch, tmp_path
):
    """R17-1: launch holds transition_guard through cancel-check; cancel waits,
    then sees published PID and signals after launch releases."""
    import os
    import threading

    import omg_cli.state as state_mod
    from omg_cli.modes import _launch_grok, _run_dir
    from omg_cli.state import (
        create_run,
        launch_refused_for_cancel,
        load_run,
        transition_guard_held,
    )

    run = create_run(tmp_path, mode="ulw", goal="r17 launch-holds-guard")
    rid = run["run_id"]
    run_dir = _run_dir(tmp_path, rid)

    barrier = threading.Barrier(2)
    events: list[str] = []
    killpgs: list[int] = []
    starttime = "Wed Aug  5 12:00:00 2026"

    original_refused = launch_refused_for_cancel

    def refused_then_barrier(root, run_id):
        result = original_refused(root, run_id)
        if result is None:
            assert transition_guard_held()
            events.append("launch_checked")
            barrier.wait(timeout=5)
            # Cancel thread is now blocked on transition_guard; proceed to Popen.
        return result

    mock_proc = MagicMock()
    mock_proc.pid = 424242
    mock_proc.wait.return_value = 0

    def fake_popen(*_a, **_k):
        events.append("popen")
        return mock_proc

    def fake_killpg(pgid, _sig):
        killpgs.append(pgid)

    def fake_kill(pid, sig):
        if sig == 0:
            return  # pretend alive for cancel identity check
        killpgs.append(pid)

    monkeypatch.setattr(state_mod, "launch_refused_for_cancel", refused_then_barrier)
    _stub_process_starttime(monkeypatch, starttime)
    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", _selective_popen(real, fake_popen))
    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(state_mod, "process_starttime", lambda _pid: starttime)

    cancel_result: dict = {}

    def cancel_worker() -> None:
        barrier.wait(timeout=5)
        from omg_cli.state import cancel_run

        cancel_result.update(cancel_run(tmp_path, rid, kill_grace_s=0))
        events.append("cancel_done")

    t = threading.Thread(target=cancel_worker)
    t.start()
    rc = _launch_grok(
        ["grok", "-p", "hello"],
        cwd=tmp_path,
        run_dir=run_dir,
        timeout=None,
        dry_run=False,
    )
    t.join(timeout=10)
    assert not t.is_alive()

    assert rc == 0
    assert "popen" in events
    assert cancel_result.get("status") == "cancelled"
    assert load_run(tmp_path, rid)["status"] == "cancelled"
    # Cancel must have observed the published leader pid (kill attempted).
    assert killpgs, cancel_result.get("kill_actions")
    assert (run_dir / "pid.json").is_file()
