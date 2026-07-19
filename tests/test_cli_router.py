# tests/test_cli_router.py
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_OMG = REPO_ROOT / "bin" / "omg"
PYTHON = sys.executable


def _run_omg(*args, cwd=None, env=None, check=False):
    cmd = [PYTHON, str(BIN_OMG), *args]
    full_env = os.environ.copy()
    # Ensure package importable when invoked as script
    full_env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + full_env["PYTHONPATH"] if full_env.get("PYTHONPATH") else ""
    )
    if env:
        full_env.update(env)
    return subprocess.run(
        cmd,
        cwd=cwd or REPO_ROOT,
        env=full_env,
        capture_output=True,
        text=True,
        check=check,
    )


def test_help_exits_zero():
    r = _run_omg("--help")
    assert r.returncode == 0
    out = r.stdout + r.stderr
    assert "setup" in out
    assert "doctor" in out
    assert "state" in out


def test_unknown_command_fails():
    r = _run_omg("not-a-real-command")
    assert r.returncode != 0


def test_setup_on_tmp_path(tmp_path):
    r = _run_omg("setup", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / ".omg" / "state" / "runs").is_dir()
    assert (tmp_path / ".omg" / "plans").is_dir()
    assert (tmp_path / ".omg" / "research").is_dir()
    assert (tmp_path / ".omg" / "handoffs").is_dir()
    assert (tmp_path / ".omg" / "artifacts").is_dir()
    assert (tmp_path / ".omg" / "ultragoal").is_dir()
    agents = tmp_path / "AGENTS.md"
    assert agents.is_file()
    assert "oh-my-grok" in agents.read_text(encoding="utf-8")
    gi = tmp_path / ".gitignore"
    assert gi.is_file()
    assert ".omg/" in gi.read_text(encoding="utf-8") or ".omg/state" in gi.read_text(
        encoding="utf-8"
    )
    assert "plugin install" in r.stdout.lower() or "grok plugin" in r.stdout.lower()
    # isolation banner always printed after setup success
    assert "[compat.claude]" in r.stdout
    assert "skills = false" in r.stdout
    assert "hooks = false" in r.stdout


def test_setup_idempotent_agents_marker(tmp_path):
    r1 = _run_omg("setup", cwd=tmp_path)
    assert r1.returncode == 0
    text1 = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    r2 = _run_omg("setup", cwd=tmp_path)
    assert r2.returncode == 0
    text2 = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    # marker block should not duplicate
    marker = "<!-- OMG:START -->"
    assert text1.count(marker) == 1
    assert text2.count(marker) == 1


def test_doctor_runnable():
    r = _run_omg("doctor", cwd=REPO_ROOT)
    # doctor prints OK/FAIL lines; may pass or fail on grok PATH depending on env
    out = r.stdout + r.stderr
    assert "plugin.json" in out.lower() or "OK" in out or "FAIL" in out
    # process always finishes cleanly (0 or 1), not crash
    assert r.returncode in (0, 1)
    # always runs compat.claude section + isolation banner
    assert "compat.claude" in out
    assert "skills = false" in out


def test_doctor_strict_flag_accepted():
    r = _run_omg("doctor", "--strict", cwd=REPO_ROOT)
    assert r.returncode in (0, 1)
    out = r.stdout + r.stderr
    assert "compat.claude" in out or "plugin.json" in out.lower()
    # soft-gate honesty footer always present
    assert "fail-open" in out.lower() or "soft-gate" in out.lower()
    # best-effort trust section present
    assert "trust" in out.lower() or "inventory" in out.lower()


def test_state_no_active(tmp_path):
    # setup dirs then state with no run
    _run_omg("setup", cwd=tmp_path)
    r = _run_omg("state", cwd=tmp_path)
    assert r.returncode == 0
    assert "no active run" in r.stdout.lower()


def test_state_and_cancel_via_cli(tmp_path):
    from omg_cli.state import create_run, load_active_run

    run = create_run(tmp_path, mode="ralph", goal="cli cancel")
    r_state = _run_omg("state", cwd=tmp_path)
    assert r_state.returncode == 0
    assert run["run_id"] in r_state.stdout

    r_cancel = _run_omg("cancel", cwd=tmp_path)
    assert r_cancel.returncode == 0, r_cancel.stderr
    assert load_active_run(tmp_path) is None


def test_mode_launchers_dry_run(tmp_path):
    """Mode launchers: create run state without execing grok when --dry-run."""
    for mode in ("ulw", "ralph", "ralplan"):
        # ralph defaults require_acceptance → non-zero when not verified;
        # opt out for this scaffold smoke test.
        args = [mode, "do something", "--dry-run"]
        if mode == "ralph":
            args.append("--no-require-acceptance")
        r = _run_omg(*args, cwd=tmp_path)
        assert r.returncode == 0, r.stderr + r.stdout
        # active run should exist under project cwd (tmp_path)
        state = _run_omg("state", cwd=tmp_path)
        assert state.returncode == 0
        assert "do something" in state.stdout or mode in state.stdout
        # cancel so next mode can create a new active cleanly
        _run_omg("cancel", cwd=tmp_path)


def test_accept_cli_freeze_and_run(tmp_path):
    """omg accept freezes prd commands and stamps CLI acceptance result."""
    from omg_cli.state import create_run

    run = create_run(tmp_path, mode="ralph", goal="accept cli")
    rid = run["run_id"]
    prd_path = tmp_path / ".omg" / "state" / "runs" / rid / "prd.json"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        json.dumps(
            {
                "version": 1,
                "goal": "accept cli",
                "stories": [
                    {"id": "s1", "title": "ok", "commands": [["true"]]}
                ],
                "global_commands": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    r = _run_omg("accept", "--run", rid, cwd=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "verified" in r.stdout.lower() or rid in r.stdout
    result = tmp_path / ".omg" / "state" / "runs" / rid / "acceptance.result.json"
    assert result.is_file()
    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["writer"] == "omg-cli"
    assert data["passed"] is True


def test_safe_and_yolo_flags_accepted():
    r = _run_omg("--help")
    assert r.returncode == 0
    # flags documented or at least parseable
    r2 = _run_omg("doctor", "--safe")
    assert r2.returncode in (0, 1)
    r3 = _run_omg("doctor", "--yolo")
    assert r3.returncode in (0, 1)


def test_doctor_hooks_missing_plugin_root(monkeypatch, tmp_path):
    """Fail-path: monkeypatched empty plugin root → hooks scripts check fails."""
    import omg_cli.doctor as doctor

    monkeypatch.setattr(doctor, "plugin_root", lambda: tmp_path)
    name, ok, detail = doctor.check_hooks_scripts()
    assert name == "hooks scripts"
    assert ok is False
    assert "missing" in detail.lower()


def test_doctor_hooks_not_executable(monkeypatch, tmp_path):
    """Fail-path: hooks present but lacking +x → check fails."""
    import omg_cli.doctor as doctor

    for rel in doctor.HOOK_SCRIPTS:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# stub\n", encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

    monkeypatch.setattr(doctor, "plugin_root", lambda: tmp_path)
    name, ok, detail = doctor.check_hooks_scripts()
    assert name == "hooks scripts"
    assert ok is False
    assert "not executable" in detail.lower() or "+x" in detail.lower()
