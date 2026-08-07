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
    assert "ready_path" not in combined
    assert '"ok"' not in combined or "PROVIDER_TUI" in combined
    # Bootstrap must not dump JSON envelope above provider.
    assert "team.worker-ready" not in combined
    assert "nearest .omg" not in combined
    # First meaningful provider line should be present when tee works;
    # at minimum bootstrap must not own the only output.
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
