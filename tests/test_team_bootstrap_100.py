"""#100 silent Team worker bootstrap — no pane JSON / nested-.omg warnings."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from omg_cli.main import main
from omg_cli.project_root import (
    clear_resolved_project_root,
    resolve_project_root,
)
from omg_cli.team.bootstrap import (
    BootstrapError,
    BootstrapErrorCode,
    append_bootstrap_log,
    pane_failure_line,
    read_bootstrap_summary,
    resolve_supervisor_project_root,
    validate_canonical_leader_root,
    worker_bootstrap_path,
    worker_ready_internal,
)
from omg_cli.team.plane import _pane_env_pairs
from omg_cli.team.supervisor import write_provider_descriptor


def _leader_root(tmp_path: Path) -> Path:
    (tmp_path / ".omg" / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _bind_supervisor_env(
    monkeypatch: pytest.MonkeyPatch,
    leader: Path,
    *,
    worker_id: str = "w1",
    run_id: str = "run-100",
    team_id: str = "team",
) -> None:
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", worker_id)
    monkeypatch.setenv("OMG_TEAM_RUN_ID", run_id)
    monkeypatch.setenv("OMG_TEAM_ID", team_id)
    monkeypatch.setenv("OMG_TEAM_LEADER_ROOT", str(leader))
    monkeypatch.setenv("OMG_TEAM_STATE_ROOT", str(leader / ".omg" / "state"))
    monkeypatch.setenv("OMG_TEAM_OWNER_TOKEN", "owner-token-test")
    monkeypatch.setenv("OMG_EXPERIMENTAL_TMUX_TEAM", "1")
    monkeypatch.setenv("OMG_TEAM_SUPERVISOR_READY_S", "5")


def test_pane_failure_line_has_no_paths() -> None:
    line = pane_failure_line(worker_id="w2", run_id="run-abc")
    assert "w2" in line
    assert "run-abc" in line
    assert "--full" in line
    assert "/" not in line.split(";")[0]  # no absolute path in primary clause
    assert "Traceback" not in line
    assert "HOME" not in line


def test_validate_leader_root_rejects_missing_omg(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(BootstrapError) as ei:
        validate_canonical_leader_root(
            {"OMG_TEAM_LEADER_ROOT": str(bare)}
        )
    assert ei.value.code == BootstrapErrorCode.ROOT_INVALID


def test_validate_leader_root_rejects_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = _leader_root(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(BootstrapError) as ei:
        validate_canonical_leader_root(
            {"OMG_TEAM_LEADER_ROOT": str(link)}
        )
    assert ei.value.code == BootstrapErrorCode.ROOT_INVALID


def test_resolve_supervisor_skips_discovery_no_shadow_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leader = _leader_root(tmp_path)
    worktree = leader / "worktrees" / "w1"
    worktree.mkdir(parents=True)
    (worktree / ".omg").mkdir()
    monkeypatch.chdir(worktree)
    monkeypatch.setenv("OMG_TEAM_LEADER_ROOT", str(leader))
    monkeypatch.setenv(
        "OMG_TEAM_STATE_ROOT", str(leader / ".omg" / "state")
    )
    clear_resolved_project_root()
    res = resolve_supervisor_project_root()
    assert res.root == leader.resolve()
    assert res.source == "team_leader"
    assert res.shadowed_omg_ancestors == ()
    # Generic discovery from the same cwd WOULD warn — prove contrast.
    generic = resolve_project_root(cwd=worktree, env={})
    assert generic.shadowed_omg_ancestors  # leader is an ancestor .omg
    assert generic.root == worktree.resolve()


def test_pane_env_pairs_pins_project_root(tmp_path: Path) -> None:
    leader = _leader_root(tmp_path)
    pairs = dict(
        _pane_env_pairs(
            run_id="run-x",
            team_id="team",
            worker_id="w1",
            leader_root=leader,
            state_root=leader / ".omg" / "state",
            owner_token="tok",
        )
    )
    assert pairs["OMG_TEAM_LEADER_ROOT"] == str(leader.resolve())
    assert pairs["OMG_PROJECT_ROOT"] == str(leader.resolve())
    assert pairs["OMG_TEAM_STATE_ROOT"] == str(
        (leader / ".omg" / "state").resolve()
    )


def test_worker_ready_internal_silent_no_emit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    leader = _leader_root(tmp_path)
    result = worker_ready_internal(
        leader, run_id="run-wr", team_id="team", worker_id="w1"
    )
    assert result.ok is True
    assert result.ready_path is not None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    # Public dict must not leak absolute ready_path.
    assert "ready_path" not in result.to_dict()
    assert result.to_dict().get("ready_written") is True


def test_public_worker_ready_still_emits_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    leader = _leader_root(tmp_path)
    monkeypatch.chdir(leader)
    _bind_supervisor_env(monkeypatch, leader, run_id="run-pub")
    clear_resolved_project_root()
    rc = main(["team", "worker-ready", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload.get("ok") is True or (
        isinstance(payload.get("data"), dict) and payload["data"].get("ok") is True
    )
    assert "ready_path" not in out
    assert str(leader) not in out or "ready_written" in out


def test_supervisor_cli_silent_success_no_warning_no_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    leader = _leader_root(tmp_path / "leader")
    worktree = tmp_path / "worktrees" / "w1"
    worktree.mkdir(parents=True)
    (worktree / ".omg").mkdir()
    script = worktree / "provider.py"
    script.write_text(
        "import time\n"
        "print('PROVIDER_TUI_FIRST_LINE', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    desc = write_provider_descriptor(
        leader / "w1.provider.json",
        provider="fixture",
        argv=[sys.executable, str(script)],
        cwd=worktree,
    )
    _publish_test_authority(
        leader,
        run_id="run-silent",
        worker_id="w1",
        descriptor=desc,
    )
    monkeypatch.chdir(worktree)
    _bind_supervisor_env(monkeypatch, leader, run_id="run-silent")
    monkeypatch.setenv("OMG_TEAM_PROVIDER_STRATEGY", "fake-ready")
    clear_resolved_project_root()

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omg_cli.main",
            "team",
            "supervisor",
            "--descriptor",
            str(desc),
            "--ready-timeout",
            "5",
        ],
        cwd=str(worktree),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Let provider print and supervisor reach ready.
        deadline = time.monotonic() + 6.0
        stdout_buf = ""
        stderr_buf = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
            # Non-blocking-ish read via communicate would wait; use short poll.
            # Capture at end.
            from omg_cli.team.startup import read_startup_record

            rec = read_startup_record(
                leader, run_id="run-silent", team_id="team", worker_id="w1"
            )
            if rec and rec.get("phase") in (
                "provider_ready",
                "task_dispatched",
            ):
                break
        proc.send_signal(signal.SIGTERM)
        try:
            stdout_buf, stderr_buf = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_buf, stderr_buf = proc.communicate(timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()
            stdout_buf, stderr_buf = proc.communicate(timeout=2)

    combined = (stdout_buf or "") + (stderr_buf or "")
    assert "shadows ancestor" not in combined
    assert "nearest .omg" not in combined
    assert "ready_path" not in combined
    assert "team.worker-ready" not in combined
    # No OMG CLI JSON envelope on success (provider TUI may print freely).
    assert '"schema_version"' not in combined
    assert '"command"' not in combined
    assert '"ready_written"' not in combined
    # Bootstrap diagnostics stay out of pane streams.
    assert "BOOTSTRAP_BEGIN" not in combined
    assert "ROOT_VALIDATED" not in combined
    boot = worker_bootstrap_path(
        leader, run_id="run-silent", team_id="team", worker_id="w1"
    )
    assert boot.is_file()
    text = boot.read_text(encoding="utf-8")
    assert "ROOT_VALIDATED" in text or "BOOTSTRAP_BEGIN" in text
    assert "OPENAI" not in text
    assert "HOME=" not in text


def test_supervisor_missing_leader_one_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".omg").mkdir()
    desc = write_provider_descriptor(
        tmp_path / "d.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print(1)"],
    )
    monkeypatch.chdir(worktree)
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "w2")
    monkeypatch.setenv("OMG_TEAM_RUN_ID", "run-fail")
    monkeypatch.setenv("OMG_TEAM_ID", "team")
    monkeypatch.delenv("OMG_TEAM_LEADER_ROOT", raising=False)
    monkeypatch.delenv("OMG_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("OMG_EXPERIMENTAL_TMUX_TEAM", "1")
    clear_resolved_project_root()
    rc = main(["team", "supervisor", "--descriptor", str(desc)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "failed to initialize" in err
    assert "w2" in err
    assert "Traceback" not in err
    assert "shadows ancestor" not in err
    # At most one actionable line (ignore blank trailing).
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1


def _minimal_team_meta(
    leader: Path,
    *,
    run_id: str,
    team_id: str = "team",
    owner_token: str = "owner-token-test",
    schema_version: int = 1,
    writer: str | None = None,
    tasks: list | None = None,
    extra: dict | None = None,
) -> Path:
    """Write a CLI-style 0600 team.json for supervisor authority tests."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team.plane import _atomic_write_json, team_meta_path

    path = team_meta_path(leader, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    body: dict = {
        "writer": CLI_WRITER if writer is None else writer,
        "run_id": run_id,
        "team_id": team_id,
        "owner_token": owner_token,
        "schema_version": schema_version,
        "tasks": [] if tasks is None else tasks,
    }
    if extra:
        body.update(extra)
    _atomic_write_json(path, body)
    return path


def _publish_test_authority(
    leader: Path,
    *,
    run_id: str,
    worker_id: str,
    descriptor: Path,
    team_id: str = "team",
    owner_token: str = "owner-token-test",
) -> Path:
    """CLI-style prepublish for tests that admit before team.json exists."""
    from omg_cli.team.supervisor import publish_supervisor_authority

    return publish_supervisor_authority(
        leader_root=leader,
        run_id=run_id,
        team_id=team_id,
        worker_id=worker_id,
        owner_token=owner_token,
        descriptor_path=descriptor,
    )


def _assert_no_supervisor_side_effects(
    leader: Path, *, run_id: str, team_id: str = "team", worker_id: str = "w1"
) -> None:
    boot = worker_bootstrap_path(
        leader, run_id=run_id, team_id=team_id, worker_id=worker_id
    )
    assert not boot.is_file(), "admission failure must not write bootstrap.log"
    from omg_cli.team.startup import worker_startup_path

    startup = worker_startup_path(
        leader, run_id=run_id, team_id=team_id, worker_id=worker_id
    )
    assert not startup.is_file(), "admission failure must not write startup phase"


def test_supervisor_legal_worker_context_not_nested_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """P0: OMG_TEAM_WORKER=1 + full identity must reach supervisor (not E_TEAM_NESTED)."""
    leader = _leader_root(tmp_path / "leader")
    worktree = tmp_path / "worktrees" / "w1"
    worktree.mkdir(parents=True)
    (worktree / ".omg").mkdir()
    script = worktree / "provider.py"
    script.write_text(
        "import time\nprint('ok', flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    desc = write_provider_descriptor(
        leader / "w1.provider.json",
        provider="fixture",
        argv=[sys.executable, str(script)],
        cwd=worktree,
    )
    # Prepublish required when team.json is absent (PR #156 fail-closed).
    _publish_test_authority(
        leader,
        run_id="run-legal",
        worker_id="w1",
        descriptor=desc,
    )
    monkeypatch.chdir(worktree)
    _bind_supervisor_env(monkeypatch, leader, run_id="run-legal")
    monkeypatch.setenv("OMG_TEAM_PROVIDER_STRATEGY", "fake-ready")
    clear_resolved_project_root()

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omg_cli.main",
            "team",
            "supervisor",
            "--descriptor",
            str(desc),
            "--ready-timeout",
            "5",
        ],
        cwd=str(worktree),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.15)
            from omg_cli.team.startup import read_startup_record

            rec = read_startup_record(
                leader, run_id="run-legal", team_id="team", worker_id="w1"
            )
            if rec and rec.get("phase") in ("provider_ready", "task_dispatched"):
                break
        proc.send_signal(signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate(timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()
            out, err = proc.communicate(timeout=2)

    combined = (out or "") + (err or "")
    assert "E_TEAM_NESTED_LAUNCH" not in combined
    boot = worker_bootstrap_path(
        leader, run_id="run-legal", team_id="team", worker_id="w1"
    )
    assert boot.is_file()
    assert "ROOT_VALIDATED" in boot.read_text(encoding="utf-8") or (
        "BOOTSTRAP_BEGIN" in boot.read_text(encoding="utf-8")
    )


def test_supervisor_missing_identity_zero_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Forged/incomplete supervisor env: refuse before bootstrap/startup writes."""
    leader = _leader_root(tmp_path / "leader")
    desc = write_provider_descriptor(
        tmp_path / "d.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print(1)"],
    )
    monkeypatch.chdir(tmp_path)
    # Worker marker set (as a nested attacker would have) but identity incomplete.
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.delenv("OMG_TEAM_WORKER_ID", raising=False)
    monkeypatch.delenv("OMG_TEAM_RUN_ID", raising=False)
    monkeypatch.setenv("OMG_TEAM_LEADER_ROOT", str(leader))
    monkeypatch.setenv("OMG_EXPERIMENTAL_TMUX_TEAM", "1")
    clear_resolved_project_root()
    rc = main(["team", "supervisor", "--descriptor", str(desc)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "E_TEAM_NESTED_LAUNCH" not in err  # not the wrong gate
    assert "failed to initialize" in err
    assert "Traceback" not in err
    _assert_no_supervisor_side_effects(
        leader, run_id="missing", worker_id="missing"
    )


def test_supervisor_forged_leader_root_zero_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Leader root without real .omg control plane fails closed, no side effects."""
    bare = tmp_path / "bare"
    bare.mkdir()
    desc = write_provider_descriptor(
        tmp_path / "d.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print(1)"],
    )
    monkeypatch.chdir(bare)
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "w1")
    monkeypatch.setenv("OMG_TEAM_RUN_ID", "run-forged-root")
    monkeypatch.setenv("OMG_TEAM_ID", "team")
    monkeypatch.setenv("OMG_TEAM_LEADER_ROOT", str(bare))
    monkeypatch.setenv("OMG_EXPERIMENTAL_TMUX_TEAM", "1")
    clear_resolved_project_root()
    rc = main(["team", "supervisor", "--descriptor", str(desc)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "failed to initialize" in err
    assert "w1" in err
    assert "Traceback" not in err
    # No control-plane writes under the forged root.
    assert not (bare / ".omg").exists() or not any(
        (bare / ".omg").rglob("bootstrap.log")
    )


def test_supervisor_stale_owner_token_zero_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Published team.json owner_token must match env; mismatch is zero-side-effect."""
    leader = _leader_root(tmp_path / "leader")
    run_id = "run-stale-tok"
    _minimal_team_meta(
        leader, run_id=run_id, owner_token="published-token-abc"
    )
    desc = write_provider_descriptor(
        tmp_path / "d.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print(1)"],
    )
    monkeypatch.chdir(tmp_path)
    _bind_supervisor_env(monkeypatch, leader, run_id=run_id)
    monkeypatch.setenv("OMG_TEAM_OWNER_TOKEN", "stale-or-forged-token")
    clear_resolved_project_root()
    rc = main(["team", "supervisor", "--descriptor", str(desc)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "failed to initialize" in err
    assert "Traceback" not in err
    _assert_no_supervisor_side_effects(leader, run_id=run_id)


def test_supervisor_missing_descriptor_zero_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Argparse requires --descriptor; ensure no control-plane writes on misuse."""
    from omg_cli.team.supervisor import admit_pane_supervisor, SupervisorError

    leader = _leader_root(tmp_path)
    monkeypatch.chdir(tmp_path)
    _bind_supervisor_env(monkeypatch, leader, run_id="run-nodesc")
    clear_resolved_project_root()
    with pytest.raises(SupervisorError, match="descriptor"):
        admit_pane_supervisor(None)
    _assert_no_supervisor_side_effects(leader, run_id="run-nodesc")


def test_worker_launch_still_nested_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nested launch remains refused; only supervisor is identity-admitted."""
    leader = _leader_root(tmp_path)
    monkeypatch.chdir(leader)
    _bind_supervisor_env(monkeypatch, leader, run_id="run-nolaunch")
    clear_resolved_project_root()
    rc = main(["team", "launch", "--workers", "1", "--goal", "x", "--plan-only"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "E_TEAM_NESTED_LAUNCH" in err


def test_generic_cli_still_warns_on_nested_omg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    leader = _leader_root(tmp_path / "leader")
    nested = leader / "worktrees" / "w1"
    nested.mkdir(parents=True)
    (nested / ".omg").mkdir()
    monkeypatch.chdir(nested)
    clear_resolved_project_root()
    # doctor is project-scoped and should still surface the shadow warning.
    rc = main(["doctor"])
    _ = rc
    err = capsys.readouterr().err
    assert "shadows ancestor" in err


def test_bootstrap_log_redacts_and_bounds(tmp_path: Path) -> None:
    leader = _leader_root(tmp_path)
    for i in range(80):
        append_bootstrap_log(
            leader,
            run_id="run-b",
            team_id="team",
            worker_id="w1",
            phase="PHASE",
            code="TEST",
            summary=f"api_key=SUPERSECRET{i} " + ("x" * 400),
        )
    path = worker_bootstrap_path(
        leader, run_id="run-b", team_id="team", worker_id="w1"
    )
    text = path.read_text(encoding="utf-8")
    assert "SUPERSECRET" not in text
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) <= 64
    summary = read_bootstrap_summary(
        leader, run_id="run-b", team_id="team", worker_id="w1"
    )
    assert summary is not None
    assert summary["artifact"] == "bootstrap.log"
    assert "present" in summary
    # No absolute path in descriptor.
    assert str(leader) not in json.dumps(summary)


def test_bootstrap_summary_redacts_absolute_home_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team.bootstrap import sanitize_bootstrap_summary

    home = tmp_path / "homeuser"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    leader = _leader_root(tmp_path / "proj")
    leaked = (
        f"PermissionError: [Errno 13] denied: '{home}/.omg/state/runs/x/ready.json'"
    )
    append_bootstrap_log(
        leader,
        run_id="run-home",
        team_id="team",
        worker_id="w1",
        phase="READY_WRITE_FAIL",
        code="PERMISSION",
        summary=leaked,
    )
    text = worker_bootstrap_path(
        leader, run_id="run-home", team_id="team", worker_id="w1"
    ).read_text(encoding="utf-8")
    assert str(home) not in text
    assert "<home>" in text
    # Generic /Users and /home shapes.
    cleaned = sanitize_bootstrap_summary(
        "failed under /Users/alice/secret/path and /home/bob/x"
    )
    assert cleaned is not None
    assert "/Users/alice" not in cleaned
    assert "/home/bob" not in cleaned
    assert "/Users/<user>" in cleaned
    assert "/home/<user>" in cleaned


def test_early_identity_fail_emits_one_pane_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spawn/identity fail-closed paths must not leave a blank pane (#100)."""
    from omg_cli.team import supervisor as sup
    from omg_cli.team.bootstrap import pane_failure_line
    from omg_cli.team.supervisor import run_supervisor

    leader = _leader_root(tmp_path)
    _bind_supervisor_env(monkeypatch, leader, worker_id="w9", run_id="run-idfail")
    desc = write_provider_descriptor(
        leader / "bad.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )

    def _unresolved(*_a: object, **_k: object) -> tuple[None, str]:
        return None, "needs_pty: provider child identity unresolved"

    monkeypatch.setattr(sup, "resolve_provider_child_pid", _unresolved)
    rc = run_supervisor(descriptor_path=desc, ready_timeout_s=2.0)
    assert rc == 1
    err = capsys.readouterr().err
    expected = pane_failure_line(worker_id="w9", run_id="run-idfail")
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert lines == [expected]
    assert "Traceback" not in err
    assert str(leader) not in err
    boot = worker_bootstrap_path(
        leader, run_id="run-idfail", team_id="team", worker_id="w9"
    )
    assert boot.is_file()
    assert "BOOTSTRAP_FAIL" in boot.read_text(encoding="utf-8")


def test_symlink_state_root_rejected(tmp_path: Path) -> None:
    leader = _leader_root(tmp_path / "leader")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "state").mkdir()
    with pytest.raises(BootstrapError):
        validate_canonical_leader_root(
            {
                "OMG_TEAM_LEADER_ROOT": str(leader),
                "OMG_TEAM_STATE_ROOT": str(foreign / "state"),
            }
        )


# ---------------------------------------------------------------------------
# PR #156: fail-closed prepublish authority (team.json missing ≠ allow)
# ---------------------------------------------------------------------------


def test_supervisor_forged_env_no_prepublish_zero_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Attacker env + schema-valid descriptor without prepublish cannot spawn."""
    leader = _leader_root(tmp_path / "leader")
    run_id = "run-forged-auth"
    desc = write_provider_descriptor(
        tmp_path / "evil.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print('pwned')"],
    )
    monkeypatch.chdir(tmp_path)
    _bind_supervisor_env(monkeypatch, leader, run_id=run_id)
    clear_resolved_project_root()
    # No team.json, no prepublish — must refuse before provider/bootstrap writes.
    rc = main(["team", "supervisor", "--descriptor", str(desc)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "failed to initialize" in err
    assert "Traceback" not in err
    _assert_no_supervisor_side_effects(leader, run_id=run_id)
    # No authority dir invented under a forged launch either.
    from omg_cli.team.supervisor import supervisor_prepublish_dir

    auth_dir = supervisor_prepublish_dir(leader, run_id)
    assert not auth_dir.exists() or not any(auth_dir.glob("*.json"))


def test_supervisor_valid_prepublish_admits_without_team_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid CLI prepublish binds descriptor digest + owner token before team.json."""
    from omg_cli.team.supervisor import admit_pane_supervisor

    leader = _leader_root(tmp_path / "leader")
    run_id = "run-prepub-ok"
    desc = write_provider_descriptor(
        leader / "w1.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print(1)"],
    )
    _publish_test_authority(
        leader, run_id=run_id, worker_id="w1", descriptor=desc
    )
    monkeypatch.chdir(tmp_path)
    _bind_supervisor_env(monkeypatch, leader, run_id=run_id)
    clear_resolved_project_root()
    rid, tid, wid, root = admit_pane_supervisor(desc)
    assert rid == run_id
    assert tid == "team"
    assert wid == "w1"
    assert root == leader.resolve()


def test_supervisor_tampered_descriptor_digest_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutating descriptor after prepublish fails closed (zero side effects)."""
    leader = _leader_root(tmp_path / "leader")
    run_id = "run-tamper-desc"
    desc = write_provider_descriptor(
        leader / "w1.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print(1)"],
    )
    _publish_test_authority(
        leader, run_id=run_id, worker_id="w1", descriptor=desc
    )
    # Tamper descriptor bytes after authority was bound to the digest.
    desc.write_text(
        desc.read_text(encoding="utf-8").replace("print(1)", "print(2)"),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    _bind_supervisor_env(monkeypatch, leader, run_id=run_id)
    clear_resolved_project_root()
    rc = main(["team", "supervisor", "--descriptor", str(desc)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "failed to initialize" in err
    _assert_no_supervisor_side_effects(leader, run_id=run_id)


def test_supervisor_tampered_prepublish_token_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    leader = _leader_root(tmp_path / "leader")
    run_id = "run-tamper-tok"
    desc = write_provider_descriptor(
        leader / "w1.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print(1)"],
    )
    _publish_test_authority(
        leader,
        run_id=run_id,
        worker_id="w1",
        descriptor=desc,
        owner_token="published-token",
    )
    monkeypatch.chdir(tmp_path)
    _bind_supervisor_env(monkeypatch, leader, run_id=run_id)
    monkeypatch.setenv("OMG_TEAM_OWNER_TOKEN", "attacker-token")
    clear_resolved_project_root()
    rc = main(["team", "supervisor", "--descriptor", str(desc)])
    assert rc != 0
    _assert_no_supervisor_side_effects(leader, run_id=run_id)


def test_supervisor_wrong_worker_prepublish_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.team.supervisor import SupervisorError, admit_pane_supervisor

    leader = _leader_root(tmp_path / "leader")
    run_id = "run-wrong-worker"
    desc = write_provider_descriptor(
        leader / "w1.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print(1)"],
    )
    # Authority published for w1, env claims w2.
    _publish_test_authority(
        leader, run_id=run_id, worker_id="w1", descriptor=desc
    )
    monkeypatch.chdir(tmp_path)
    _bind_supervisor_env(monkeypatch, leader, run_id=run_id, worker_id="w2")
    clear_resolved_project_root()
    with pytest.raises(SupervisorError, match="prepublish|authority|missing"):
        admit_pane_supervisor(desc)


def test_start_team_live_publishes_then_clears_prepublish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live start materializes prepublish before panes; team.json clears it."""
    import subprocess as sp

    from omg_cli.team import plane
    from omg_cli.team.plane import EXPERIMENTAL_ENV, start_team, team_meta_path
    from omg_cli.team.supervisor import supervisor_prepublish_dir

    def _git(cwd: Path, *args: str) -> None:
        sp.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )

    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "omg-test@example.com")
    _git(tmp_path, "config", "user.name", "omg-test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "initial")

    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    monkeypatch.delenv("OMG_DISABLE_TMUX_TEAM", raising=False)
    # dry_run: no prepublish (supervisor never runs)
    meta = start_team(
        "prepub dry",
        [{"task_id": "w1", "owned_files": ["a.py"]}],
        root=tmp_path,
        dry_run=True,
        owner_token="tok-dry",
    )
    rid = str(meta["run_id"])
    auth_dir = supervisor_prepublish_dir(tmp_path, rid)
    assert not auth_dir.exists() or not any(auth_dir.glob("*.json"))
    assert team_meta_path(tmp_path, rid).is_file()

    # materialize + publish without live tmux: exercise publish path directly
    from omg_cli.team.plane import materialize_supervisor_pane_command
    from omg_cli.team.supervisor import (
        clear_supervisor_prepublish_authorities,
        supervisor_prepublish_path,
    )

    tdir = plane.team_dir(tmp_path, rid)
    desc_path = tdir / "w2.provider.json"
    materialize_supervisor_pane_command(
        descriptor_path=desc_path,
        provider="fixture",
        argv=[sys.executable, "-c", "print('x')"],
        leader_root=tmp_path,
        run_id=rid,
        team_id="team",
        worker_id="w2",
        owner_token="tok-live",
        publish_authority=True,
    )
    pre = supervisor_prepublish_path(tmp_path, rid, "w2")
    assert pre.is_file()
    clear_supervisor_prepublish_authorities(tmp_path, rid)
    assert not pre.is_file()


def test_supervisor_team_json_token_alone_cannot_use_foreign_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PR #156: after team.json, shared token + arbitrary descriptor is refused."""
    from omg_cli.team.plane import team_dir

    leader = _leader_root(tmp_path / "leader")
    run_id = "run-tok-only"
    _minimal_team_meta(
        leader,
        run_id=run_id,
        owner_token="owner-token-test",
    )
    # Publish a legitimate worker descriptor under the team tree.
    tdir = team_dir(leader, run_id)
    tdir.mkdir(parents=True, exist_ok=True)
    write_provider_descriptor(
        tdir / "w1.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print('legit')"],
    )
    # Attacker points at a different schema-valid descriptor.
    evil = write_provider_descriptor(
        tmp_path / "evil.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print('pwn')"],
    )
    monkeypatch.chdir(tmp_path)
    _bind_supervisor_env(monkeypatch, leader, run_id=run_id, worker_id="w1")
    clear_resolved_project_root()
    rc = main(["team", "supervisor", "--descriptor", str(evil)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "failed to initialize" in err
    _assert_no_supervisor_side_effects(leader, run_id=run_id)


def test_supervisor_team_json_unknown_worker_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Published task list must exclude unknown worker_ids even with token."""
    from omg_cli.team.plane import team_dir
    from omg_cli.team.supervisor import SupervisorError, admit_pane_supervisor

    leader = _leader_root(tmp_path / "leader")
    run_id = "run-unknown-w"
    tdir = team_dir(leader, run_id)
    tdir.mkdir(parents=True, exist_ok=True)
    _minimal_team_meta(
        leader,
        run_id=run_id,
        tasks=[{"task_id": "w1", "status": "running"}],
    )
    # Attacker forges w2 descriptor path under team dir.
    desc = write_provider_descriptor(
        tdir / "w2.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print(1)"],
    )
    monkeypatch.chdir(tmp_path)
    _bind_supervisor_env(monkeypatch, leader, run_id=run_id, worker_id="w2")
    clear_resolved_project_root()
    with pytest.raises(SupervisorError, match="not a published team task"):
        admit_pane_supervisor(desc)


def test_supervisor_team_json_published_descriptor_admits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matching token + CLI path for a published worker admits without prepublish."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team.plane import team_dir, team_meta_path
    from omg_cli.team.supervisor import admit_pane_supervisor

    leader = _leader_root(tmp_path / "leader")
    run_id = "run-pub-desc"
    tdir = team_dir(leader, run_id)
    tdir.mkdir(parents=True, exist_ok=True)
    desc = write_provider_descriptor(
        tdir / "w1.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print(1)"],
    )
    path = team_meta_path(leader, run_id)
    path.write_text(
        json.dumps(
            {
                "writer": CLI_WRITER,
                "run_id": run_id,
                "team_id": "team",
                "owner_token": "owner-token-test",
                "schema_version": 1,
                "tasks": [{"task_id": "w1"}],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    # Mode may not be 0600 if write_text — load_team_meta requires 0600!
    # Use atomic path via plane helper.
    from omg_cli.team.plane import _atomic_write_json

    from omg_cli.team.supervisor import descriptor_content_digest

    digest = descriptor_content_digest(desc)
    _atomic_write_json(
        path,
        {
            "writer": CLI_WRITER,
            "run_id": run_id,
            "team_id": "team",
            "owner_token": "owner-token-test",
            "schema_version": 1,
            "tasks": [{"task_id": "w1", "descriptor_sha256": digest}],
        },
    )
    monkeypatch.chdir(tmp_path)
    _bind_supervisor_env(monkeypatch, leader, run_id=run_id, worker_id="w1")
    clear_resolved_project_root()
    rid, tid, wid, root = admit_pane_supervisor(desc)
    assert rid == run_id and wid == "w1" and tid == "team"
    assert root == leader.resolve()


def test_supervisor_team_json_missing_digest_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-publication path+task bind without digest is fail-closed."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team.plane import team_dir, team_meta_path, _atomic_write_json
    from omg_cli.team.supervisor import SupervisorError, admit_pane_supervisor

    leader = _leader_root(tmp_path / "leader")
    run_id = "run-no-digest"
    tdir = team_dir(leader, run_id)
    tdir.mkdir(parents=True, exist_ok=True)
    desc = write_provider_descriptor(
        tdir / "w1.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print(1)"],
    )
    _atomic_write_json(
        team_meta_path(leader, run_id),
        {
            "writer": CLI_WRITER,
            "run_id": run_id,
            "team_id": "team",
            "owner_token": "owner-token-test",
            "schema_version": 1,
            "tasks": [{"task_id": "w1"}],
        },
    )
    monkeypatch.chdir(tmp_path)
    _bind_supervisor_env(monkeypatch, leader, run_id=run_id, worker_id="w1")
    clear_resolved_project_root()
    with pytest.raises(SupervisorError, match="descriptor_sha256"):
        admit_pane_supervisor(desc)


def test_supervisor_postpublish_replacement_cannot_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Altered descriptor bytes after team.json digest bind must not spawn."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team.plane import team_dir, team_meta_path, _atomic_write_json
    from omg_cli.team.supervisor import descriptor_content_digest

    leader = _leader_root(tmp_path / "leader")
    run_id = "run-replace-race"
    tdir = team_dir(leader, run_id)
    tdir.mkdir(parents=True, exist_ok=True)
    desc = write_provider_descriptor(
        tdir / "w1.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print('good')"],
    )
    digest = descriptor_content_digest(desc)
    _atomic_write_json(
        team_meta_path(leader, run_id),
        {
            "writer": CLI_WRITER,
            "run_id": run_id,
            "team_id": "team",
            "owner_token": "owner-token-test",
            "schema_version": 1,
            "tasks": [{"task_id": "w1", "descriptor_sha256": digest}],
        },
    )
    # Deterministic replacement: same path, different argv bytes.
    write_provider_descriptor(
        desc,
        provider="fixture",
        argv=[sys.executable, "-c", "print('evil')"],
    )
    monkeypatch.chdir(tmp_path)
    _bind_supervisor_env(monkeypatch, leader, run_id=run_id, worker_id="w1")
    clear_resolved_project_root()
    rc = main(["team", "supervisor", "--descriptor", str(desc)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "failed to initialize" in err
    _assert_no_supervisor_side_effects(leader, run_id=run_id)


def test_supervisor_admitted_bytes_survive_post_admit_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spawn uses the admitted mapping; a replaced file is never reopened."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team import supervisor as sup
    from omg_cli.team.plane import team_dir, team_meta_path, _atomic_write_json
    from omg_cli.team.supervisor import (
        admit_pane_supervisor_binding,
        descriptor_content_digest,
        run_supervisor,
    )

    leader = _leader_root(tmp_path / "leader")
    run_id = "run-carry"
    tdir = team_dir(leader, run_id)
    tdir.mkdir(parents=True, exist_ok=True)
    desc = write_provider_descriptor(
        tdir / "w1.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print('good')"],
    )
    digest = descriptor_content_digest(desc)
    _atomic_write_json(
        team_meta_path(leader, run_id),
        {
            "writer": CLI_WRITER,
            "run_id": run_id,
            "team_id": "team",
            "owner_token": "owner-token-test",
            "schema_version": 1,
            "tasks": [{"task_id": "w1", "descriptor_sha256": digest}],
        },
    )
    monkeypatch.chdir(tmp_path)
    _bind_supervisor_env(monkeypatch, leader, run_id=run_id, worker_id="w1")
    clear_resolved_project_root()
    binding = admit_pane_supervisor_binding(desc)
    assert binding.descriptor["argv"][-1] == "print('good')"
    write_provider_descriptor(
        desc,
        provider="fixture",
        argv=[sys.executable, "-c", "print('evil')"],
    )
    reads: list[str] = []
    original = sup._read_provider_descriptor_bytes

    def _spy(path: object) -> tuple[bytes, str]:
        reads.append(str(path))
        return original(path)

    monkeypatch.setattr(sup, "_read_provider_descriptor_bytes", _spy)
    # Identity-only spawn: admitted mapping is used; file must not be reopened.
    # Fake an immediate identity failure so we never exec either argv.
    monkeypatch.setattr(
        sup,
        "resolve_provider_child_pid",
        lambda *_a, **_k: (None, "needs_pty: provider child identity unresolved"),
    )
    rc = run_supervisor(
        descriptor_path=desc,
        ready_timeout_s=0.2,
        admitted_descriptor=binding.descriptor,
        expected_digest=binding.descriptor_sha256,
    )
    assert rc == 1
    assert reads == []
    assert binding.descriptor["argv"][-1] == "print('good')"


def _admit_with_team_meta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str,
    worker_id: str = "w1",
    team_id: str = "team",
    meta_kwargs: dict | None = None,
    chmod: int | None = None,
    symlink_to: Path | None = None,
    publish_prepublish: bool = False,
):
    """Build a published worker descriptor + team.json, then admit."""
    from omg_cli.team.plane import team_dir, team_meta_path
    from omg_cli.team.supervisor import (
        SupervisorError,
        admit_pane_supervisor,
        descriptor_content_digest,
    )

    leader = _leader_root(tmp_path / "leader")
    tdir = team_dir(leader, run_id)
    tdir.mkdir(parents=True, exist_ok=True)
    desc = write_provider_descriptor(
        tdir / f"{worker_id}.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print(1)"],
    )
    digest = descriptor_content_digest(desc)
    tasks = [{"task_id": worker_id, "descriptor_sha256": digest, "status": "running"}]
    kwargs: dict = {"tasks": tasks, "team_id": team_id}
    if meta_kwargs:
        kwargs.update(meta_kwargs)
    if symlink_to is not None:
        path = team_meta_path(leader, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            path.unlink()
        path.symlink_to(symlink_to)
    else:
        path = _minimal_team_meta(leader, run_id=run_id, **kwargs)
        if chmod is not None:
            os.chmod(path, chmod)
    if publish_prepublish:
        _publish_test_authority(
            leader, run_id=run_id, worker_id=worker_id, descriptor=desc, team_id=team_id
        )
    monkeypatch.chdir(tmp_path)
    _bind_supervisor_env(
        monkeypatch, leader, run_id=run_id, worker_id=worker_id, team_id=team_id
    )
    clear_resolved_project_root()
    try:
        return admit_pane_supervisor(desc), desc, leader
    except SupervisorError:
        raise


def test_supervisor_team_json_forged_writer_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team.supervisor import SupervisorError

    with pytest.raises(SupervisorError, match="CLI writer|refused"):
        _admit_with_team_meta(
            tmp_path,
            monkeypatch,
            run_id="run-forged-writer",
            meta_kwargs={"writer": "agent"},
        )


def test_supervisor_team_json_symlink_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team.supervisor import SupervisorError

    target = tmp_path / "forged-team.json"
    target.write_text('{"writer":"omg-cli","run_id":"run-symlink"}\n', encoding="utf-8")
    with pytest.raises(SupervisorError, match="symlink|secure open|refused"):
        _admit_with_team_meta(
            tmp_path,
            monkeypatch,
            run_id="run-symlink",
            symlink_to=target,
            publish_prepublish=True,
        )


def test_supervisor_team_json_wrong_mode_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team.supervisor import SupervisorError

    with pytest.raises(SupervisorError, match="mode must be 0600|refused"):
        _admit_with_team_meta(
            tmp_path,
            monkeypatch,
            run_id="run-bad-mode",
            chmod=0o644,
        )


def test_supervisor_team_json_wrong_schema_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team.supervisor import SupervisorError

    with pytest.raises(SupervisorError, match="schema_version|refused"):
        _admit_with_team_meta(
            tmp_path,
            monkeypatch,
            run_id="run-bad-schema",
            meta_kwargs={"schema_version": 99},
        )


def test_supervisor_team_json_wrong_run_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team.supervisor import SupervisorError

    with pytest.raises(SupervisorError, match="run_id mismatch|refused"):
        _admit_with_team_meta(
            tmp_path,
            monkeypatch,
            run_id="run-wrong-run",
            meta_kwargs={"extra": {"run_id": "other-run"}},
        )


def test_supervisor_team_json_wrong_team_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team.supervisor import SupervisorError

    with pytest.raises(SupervisorError, match="team_id mismatch|refused"):
        _admit_with_team_meta(
            tmp_path,
            monkeypatch,
            run_id="run-wrong-team",
            meta_kwargs={"team_id": "other-team"},
        )


def test_supervisor_team_json_missing_owner_token_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team.supervisor import SupervisorError

    with pytest.raises(SupervisorError, match="owner_token missing|refused"):
        _admit_with_team_meta(
            tmp_path,
            monkeypatch,
            run_id="run-no-token",
            meta_kwargs={"owner_token": ""},
            publish_prepublish=True,
        )


def test_supervisor_team_json_inactive_worker_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team.supervisor import SupervisorError, descriptor_content_digest
    from omg_cli.team.plane import team_dir

    leader = _leader_root(tmp_path / "leader")
    run_id = "run-inactive"
    tdir = team_dir(leader, run_id)
    tdir.mkdir(parents=True, exist_ok=True)
    desc = write_provider_descriptor(
        tdir / "w1.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print(1)"],
    )
    digest = descriptor_content_digest(desc)
    _minimal_team_meta(
        leader,
        run_id=run_id,
        tasks=[
            {
                "task_id": "w1",
                "descriptor_sha256": digest,
                "status": "scaled_down",
            }
        ],
    )
    _publish_test_authority(
        leader, run_id=run_id, worker_id="w1", descriptor=desc
    )
    monkeypatch.chdir(tmp_path)
    _bind_supervisor_env(monkeypatch, leader, run_id=run_id)
    clear_resolved_project_root()
    with pytest.raises(SupervisorError, match="not an active published team task"):
        from omg_cli.team.supervisor import admit_pane_supervisor

        admit_pane_supervisor(desc)


def test_supervisor_team_json_missing_tasks_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team.supervisor import SupervisorError

    with pytest.raises(SupervisorError, match="tasks missing|not a published"):
        _admit_with_team_meta(
            tmp_path,
            monkeypatch,
            run_id="run-no-tasks",
            meta_kwargs={"tasks": None, "extra": {"tasks": "nope"}},
        )


def test_start_team_dry_run_stamps_task_descriptor_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authoritative team.json task rows bind SHA-256 of the published file."""
    import subprocess as sp

    from omg_cli.team.plane import EXPERIMENTAL_ENV, start_team, team_dir
    from omg_cli.team.supervisor import descriptor_content_digest

    def _git(cwd: Path, *args: str) -> None:
        sp.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )

    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "omg-test@example.com")
    _git(tmp_path, "config", "user.name", "omg-test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "initial")

    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    monkeypatch.delenv("OMG_DISABLE_TMUX_TEAM", raising=False)
    meta = start_team(
        "digest stamp",
        [{"task_id": "w1", "owned_files": ["a.py"]}],
        root=tmp_path,
        dry_run=True,
        owner_token="tok-digest",
    )
    tasks = list(meta.get("tasks") or [])
    assert len(tasks) == 1
    digest = str(tasks[0].get("descriptor_sha256") or "")
    assert len(digest) == 64
    desc = team_dir(tmp_path, str(meta["run_id"])) / "w1.provider.json"
    assert descriptor_content_digest(desc) == digest
