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


def test_version_flag_matches_plugin_json():
    plugin = json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))
    expected = plugin["version"]
    r = _run_omg("--version")
    assert r.returncode == 0, r.stderr
    out = (r.stdout + r.stderr).strip()
    assert expected in out
    assert "omg" in out.lower() or expected in out


def test_unknown_command_fails():
    r = _run_omg("not-a-real-command")
    assert r.returncode != 0


def test_setup_on_tmp_path(tmp_path):
    grok_home = tmp_path / ".grokhome"
    env = {"GROK_HOME": str(grok_home)}
    r = _run_omg("setup", cwd=tmp_path, env=env)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / ".omg" / "state" / "runs").is_dir()
    assert (tmp_path / ".omg" / "plans").is_dir()
    assert (tmp_path / ".omg" / "research").is_dir()
    assert (tmp_path / ".omg" / "handoffs").is_dir()
    assert (tmp_path / ".omg" / "artifacts").is_dir()
    assert (tmp_path / ".omg" / "ultragoal").is_dir()
    assert (tmp_path / ".omg" / "wiki").is_dir()
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
    # global rules installed under GROK_HOME (never real ~/.grok)
    rules = grok_home / "rules" / "omg.md"
    assert rules.is_file(), f"expected global rules at {rules}"
    assert "<!-- OMG:START -->" in rules.read_text(encoding="utf-8")


def test_setup_idempotent_agents_marker(tmp_path):
    env = {"GROK_HOME": str(tmp_path / ".grokhome")}
    r1 = _run_omg("setup", cwd=tmp_path, env=env)
    assert r1.returncode == 0
    text1 = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    r2 = _run_omg("setup", cwd=tmp_path, env=env)
    assert r2.returncode == 0
    text2 = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    # marker block should not duplicate
    marker = "<!-- OMG:START -->"
    assert text1.count(marker) == 1
    assert text2.count(marker) == 1
    # second setup with same GROK_HOME still succeeds (idempotent rules)
    rules = tmp_path / ".grokhome" / "rules" / "omg.md"
    assert rules.is_file()
    assert marker in rules.read_text(encoding="utf-8")


def test_setup_no_global_rules_skips_install(tmp_path):
    """setup --no-global-rules returns 0 and does not create rules file."""
    grok_home = tmp_path / ".grokhome"
    env = {"GROK_HOME": str(grok_home)}
    r = _run_omg("setup", "--no-global-rules", cwd=tmp_path, env=env)
    assert r.returncode == 0, r.stderr
    rules = grok_home / "rules" / "omg.md"
    assert not rules.is_file(), f"rules must not be created with --no-global-rules: {rules}"


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
    # setup dirs then state with no run (hermetic GROK_HOME)
    env = {"GROK_HOME": str(tmp_path / ".grokhome")}
    _run_omg("setup", cwd=tmp_path, env=env)
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
        # ralplan FSM dry_run without verifier APPROVE → failed (exit 1).
        args = [mode, "do something", "--dry-run"]
        if mode == "ralph":
            args.append("--no-require-acceptance")
        r = _run_omg(*args, cwd=tmp_path)
        if mode == "ralplan":
            assert r.returncode == 1, r.stderr + r.stdout
        else:
            assert r.returncode == 0, r.stderr + r.stdout
        # active run should exist under project cwd (tmp_path)
        state = _run_omg("state", cwd=tmp_path)
        assert state.returncode == 0
        assert "do something" in state.stdout or mode in state.stdout
        if mode == "ralplan":
            assert "ralplan" in state.stdout.lower() or "failed" in state.stdout.lower()
            runs = list((tmp_path / ".omg" / "state" / "runs").glob("*/ralplan.json"))
            assert runs, "ralplan.json missing"
            data = json.loads(runs[0].read_text(encoding="utf-8"))
            assert data["status"] == "failed"
            assert data["accepted"] is False
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
    # non-tty subprocess requires --yes; --review prints sha/cwd/commands first
    r = _run_omg("accept", "--run", rid, "--review", "--yes", cwd=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "verified" in r.stdout.lower() or rid in r.stdout
    out = r.stdout.lower()
    assert "manifest_sha256" in out or "manifest_sha" in out
    assert "acceptance commands" in out or "true" in r.stdout
    assert rid in r.stdout or "run_id" in out
    result = tmp_path / ".omg" / "state" / "runs" / rid / "acceptance.result.json"
    assert result.is_file()
    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["writer"] == "omg-cli"
    assert data["passed"] is True


def test_accept_cli_strict_v2_sets_verified(tmp_path):
    """strict-v2 accept must auto-lease and set verified (default ralph path)."""
    from omg_cli.state import create_run, load_run

    run = create_run(
        tmp_path,
        mode="ralph",
        goal="strict accept",
        force=True,
        extra={"schema_version": 2, "lifecycle_version": 2},
    )
    rid = run["run_id"]
    assert run.get("schema_version") == 2
    prd_path = tmp_path / ".omg" / "state" / "runs" / rid / "prd.json"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        json.dumps(
            {
                "version": 1,
                "goal": "strict accept",
                "stories": [
                    {"id": "s1", "title": "ok", "commands": [["true"]]}
                ],
                "global_commands": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    r = _run_omg("accept", "--run", rid, "--yes", cwd=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "verified" in r.stdout.lower() or rid in r.stdout
    assert "set_verified failed" not in (r.stderr + r.stdout).lower()
    assert "fencing" not in (r.stderr + r.stdout).lower()
    status = load_run(tmp_path, rid)
    assert status is not None
    assert status["verified"] is True
    assert status["status"] == "verified"
    result = tmp_path / ".omg" / "state" / "runs" / rid / "acceptance.result.json"
    assert result.is_file()
    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["writer"] == "omg-cli"
    assert data["passed"] is True


def test_accept_cli_review_requires_yes(tmp_path):
    from omg_cli.state import create_run

    run = create_run(tmp_path, mode="ralph", goal="review gate")
    rid = run["run_id"]
    prd_path = tmp_path / ".omg" / "state" / "runs" / rid / "prd.json"
    prd_path.write_text(
        json.dumps(
            {
                "version": 1,
                "goal": "review gate",
                "stories": [
                    {"id": "s1", "title": "ok", "commands": [["true"]]}
                ],
                "global_commands": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    r = _run_omg("accept", "--run", rid, "--review", cwd=tmp_path)
    assert r.returncode == 2, r.stderr + r.stdout
    assert "yes" in (r.stderr + r.stdout).lower()


def test_accept_cli_yes_cannot_bypass_policy(tmp_path):
    """--yes skips confirmation only; python -c still rejected."""
    from omg_cli.state import create_run

    run = create_run(tmp_path, mode="ralph", goal="policy floor")
    rid = run["run_id"]
    prd_path = tmp_path / ".omg" / "state" / "runs" / rid / "prd.json"
    prd_path.write_text(
        json.dumps(
            {
                "version": 1,
                "goal": "policy floor",
                "stories": [
                    {
                        "id": "s1",
                        "title": "bad",
                        "commands": [["python3", "-c", "pass"]],
                    }
                ],
                "global_commands": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    r = _run_omg("accept", "--run", rid, "--yes", cwd=tmp_path)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "-c" in (r.stderr + r.stdout) or "policy" in (r.stderr + r.stdout).lower()


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
