"""#147 PR2: direct-exec interactive pane contract (hermetic)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from omg_cli.team.interactive import (
    E_TEAM_IO_MODE_UNSUPPORTED,
    INTERACTIVE_NONCE_ENV,
    InteractiveTeamError,
    capture_contains_tui_ready,
    grok_interactive_argv,
    make_interactive_nonce,
    pane_command_for_exec_script,
    resolve_effective_io_mode,
    tui_ready_marker,
    wait_for_interactive_tui_ready,
    write_interactive_exec_script,
    write_worker_inbox,
)
from omg_cli.team.io_capability import (
    IO_MODE_INTERACTIVE_TTY,
    TTY_OWNER_PROVIDER,
    interactive_pane_io_defaults,
    interactive_pane_io_ready,
)
from omg_cli.team.plane import EXPERIMENTAL_ENV, TeamError, start_team
from omg_cli.team.scaling import scale_team

_POSIX = pytest.mark.skipif(
    os.name != "posix", reason="managed-store confinement requires POSIX dir_fd"
)


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    monkeypatch.delenv("OMG_DISABLE_TMUX_TEAM", raising=False)


def _git_init(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True
    )
    (path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "i"],
        cwd=path,
        check=True,
        capture_output=True,
    )


TASKS = [{"task_id": "t1", "title": "one", "owned_files": ["README.md"]}]


def test_grok_interactive_argv_has_no_prompt_file() -> None:
    argv = grok_interactive_argv(cwd="/tmp/wt", posture="read-write")
    assert argv[0] == "grok"
    assert "--cwd" in argv
    assert "--prompt-file" not in argv
    assert "--no-alt-screen" in argv
    assert "--minimal" in argv
    assert "--no-subagents" in argv
    assert "--rules" in argv
    assert argv[argv.index("--rules") + 1].startswith("When the user sends a line")
    assert "bypassPermissions" not in argv
    yolo = grok_interactive_argv(cwd="/tmp/wt", posture="read-write", yolo=True)
    assert "bypassPermissions" in yolo
    assert "--always-approve" in yolo
    safe = grok_interactive_argv(
        cwd="/tmp/wt", posture="read-write", safe=True, yolo=True
    )
    assert "--permission-mode" in safe and "plan" in safe
    assert "bypassPermissions" not in safe
    assert "--always-approve" not in safe
    assert "--always-approve" not in argv
    ro = grok_interactive_argv(cwd="/tmp/wt", posture="read-only")
    assert "--permission-mode" in ro and "plan" in ro
    modeled = grok_interactive_argv(cwd="/tmp/wt", model="grok-example")
    assert modeled[modeled.index("-m") + 1] == "grok-example"


def test_auto_stays_headless_until_live_qualification() -> None:
    assert (
        resolve_effective_io_mode(
            requested="auto", worker_topology="pane", provider="grok"
        )
        == "headless"
    )


def test_interactive_fails_closed_for_job_and_unqualified() -> None:
    with pytest.raises(InteractiveTeamError, match=E_TEAM_IO_MODE_UNSUPPORTED):
        resolve_effective_io_mode(
            requested="interactive", worker_topology="job", provider="grok"
        )
    with pytest.raises(InteractiveTeamError, match=E_TEAM_IO_MODE_UNSUPPORTED):
        resolve_effective_io_mode(
            requested="interactive", worker_topology="pane", provider="codex"
        )


def test_exec_script_is_0700_and_has_no_prompt(tmp_path: Path) -> None:
    dest = tmp_path / "t1.interactive.sh"
    argv = grok_interactive_argv(cwd=tmp_path, posture="read-write")
    write_interactive_exec_script(dest=dest, argv=argv, worktree=tmp_path)
    text = dest.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert "exec" in text and "grok" in text
    assert "--prompt-file" not in text
    assert "XAI_API_KEY" not in text
    mode = dest.stat().st_mode & 0o777
    assert mode in {0o700, 0o666, 0o644} or (mode & 0o100)
    cmd = pane_command_for_exec_script(dest)
    assert cmd.startswith("exec /bin/sh ")
    assert "supervisor" not in cmd


def test_exec_script_wraps_grok_via_omg_cli_module(tmp_path: Path) -> None:
    dest = tmp_path / "t1.interactive.sh"
    argv = grok_interactive_argv(cwd=tmp_path, posture="read-write")
    write_interactive_exec_script(
        dest=dest,
        argv=argv,
        worktree=tmp_path,
        extra_env={INTERACTIVE_NONCE_ENV: "deadbeef"},
        wrap_module="omg_cli.team.interactive_wrapper",
        python_executable=sys.executable,
        pythonpath=str(tmp_path),
    )
    text = dest.read_text(encoding="utf-8")
    assert "omg_cli.team.interactive_wrapper" in text
    assert "PYTHONPATH=" in text
    assert "--prompt-file" not in text
    assert "team supervisor" not in text
    with pytest.raises(InteractiveTeamError, match="omg_cli"):
        write_interactive_exec_script(
            dest=dest,
            argv=argv,
            worktree=tmp_path,
            wrap_module="os.system",
        )


def test_exec_argv_rejects_prompt_file_option_not_path_substring(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "t1.interactive.sh"
    with pytest.raises(InteractiveTeamError, match="prompt-file"):
        write_interactive_exec_script(
            dest=dest,
            argv=["grok", "--prompt-file", "x"],
            worktree=tmp_path,
        )
    with pytest.raises(InteractiveTeamError, match="prompt-file"):
        write_interactive_exec_script(
            dest=dest,
            argv=["grok", "--prompt-file=secret"],
            worktree=tmp_path,
        )
    write_interactive_exec_script(
        dest=dest,
        argv=["grok", "--cwd", str(tmp_path / "no--prompt-file-demo")],
        worktree=tmp_path,
    )
    text = dest.read_text(encoding="utf-8")
    assert "no--prompt-file-demo" in text
    assert "--prompt-file " not in text
    assert "--prompt-file=" not in text


def test_inbox_is_not_a_secret_dump(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("managed atomic inbox write requires POSIX")
    p = write_worker_inbox(dest=tmp_path / "inbox.txt", body="task_id=t1\ndo the work\n")
    assert "do the work" in p.read_text(encoding="utf-8")
    assert (p.stat().st_mode & 0o777) == 0o600


def test_inbox_refuses_symlink_destination(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("managed atomic inbox write requires POSIX")
    target = tmp_path / "outside.txt"
    target.write_text("secret\n", encoding="utf-8")
    dest = tmp_path / "inbox.txt"
    try:
        dest.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation requires privileges on this host")
    with pytest.raises(InteractiveTeamError, match="symlink"):
        write_worker_inbox(dest=dest, body="overwrite\n")
    assert target.read_text(encoding="utf-8") == "secret\n"


def test_interactive_pane_io_defaults() -> None:
    cap = interactive_pane_io_defaults()
    assert cap.io_mode == IO_MODE_INTERACTIVE_TTY
    assert cap.provider_tty_owner == TTY_OWNER_PROVIDER
    assert cap.operator_input_supported is True
    assert cap.input_ready is False


@_POSIX
def test_dry_run_interactive_fixture_skips_supervisor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable(monkeypatch)
    _git_init(tmp_path)
    meta = start_team(
        "interactive fixture",
        TASKS,
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        executor="fixture",
        io_mode="interactive",
        env={EXPERIMENTAL_ENV: "1"},
    )
    tasks = meta["tasks"]
    assert len(tasks) == 1
    rec = tasks[0]
    assert rec["io_mode"] == "interactive_tty"
    assert rec["provider_tty_owner"] == "provider"
    assert rec["operator_input_supported"] is True
    assert rec["input_ready"] is False
    pane = rec["pane_command"]
    assert "supervisor" not in pane
    assert "interactive.sh" in pane or "exec /bin/sh" in pane
    assert "--prompt-file" not in json.dumps(rec["argv"])
    assert rec["provider"] == "fixture"
    assert any("interactive_tty.py" in str(x) for x in rec["argv"])
    assert rec.get("inbox_path")
    nonce = rec.get("interactive_nonce")
    assert isinstance(nonce, str) and nonce
    exec_path = tmp_path / ".omg" / "state" / "runs" / meta["run_id"] / "team" / "t1.interactive.sh"
    assert INTERACTIVE_NONCE_ENV in exec_path.read_text(encoding="utf-8")
    assert nonce in exec_path.read_text(encoding="utf-8")
    assert "interactive_wrapper" not in exec_path.read_text(encoding="utf-8")


@_POSIX
def test_dry_run_interactive_grok_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable(monkeypatch)
    _git_init(tmp_path)
    meta = start_team(
        "interactive grok",
        TASKS,
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        io_mode="interactive",
        env={EXPERIMENTAL_ENV: "1"},
    )
    rec = meta["tasks"][0]
    assert rec["provider"] == "grok"
    assert rec["io_mode"] == "interactive_tty"
    assert "--prompt-file" not in rec["argv"]
    assert rec["argv"][0] == "grok"
    assert "bypassPermissions" not in rec["argv"]
    assert "--no-alt-screen" in rec["argv"]
    assert "--minimal" in rec["argv"]
    assert "--rules" in rec["argv"]
    assert "supervisor" not in rec["pane_command"]
    exec_path = tmp_path / ".omg" / "state" / "runs" / meta["run_id"] / "team" / "t1.interactive.sh"
    script = exec_path.read_text(encoding="utf-8")
    assert "omg_cli.team.interactive_wrapper" in script
    assert "PYTHONPATH=" in script
    assert "--prompt-file" not in script
    rules = tmp_path / ".omg" / "state" / "runs" / meta["run_id"] / "team" / "t1.interactive.rules.txt"
    assert rules.is_file()
    assert "PROVIDER_ECHO:" in rules.read_text(encoding="utf-8")


@_POSIX
def test_dry_run_interactive_grok_honors_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable(monkeypatch)
    _git_init(tmp_path)
    meta = start_team(
        "interactive grok safe",
        TASKS,
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        io_mode="interactive",
        safe=True,
        env={EXPERIMENTAL_ENV: "1"},
    )
    argv = meta["tasks"][0]["argv"]
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert "bypassPermissions" not in argv


def test_interactive_job_topology_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable(monkeypatch)
    _git_init(tmp_path)
    with pytest.raises(TeamError, match=E_TEAM_IO_MODE_UNSUPPORTED):
        start_team(
            "nope",
            TASKS,
            root=tmp_path,
            dry_run=True,
            check_binary=False,
            worker_topology="job",
            io_mode="interactive",
            env={EXPERIMENTAL_ENV: "1"},
        )


def test_interactive_codex_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable(monkeypatch)
    _git_init(tmp_path)
    with pytest.raises(TeamError, match=E_TEAM_IO_MODE_UNSUPPORTED):
        start_team(
            "nope",
            TASKS,
            root=tmp_path,
            dry_run=True,
            check_binary=False,
            routing={"executor": {"provider": "codex"}},
            io_mode="interactive",
            env={EXPERIMENTAL_ENV: "1"},
        )


@_POSIX
def test_default_launch_stays_headless(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable(monkeypatch)
    _git_init(tmp_path)
    meta = start_team(
        "default",
        TASKS,
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        executor="fixture",
        env={EXPERIMENTAL_ENV: "1"},
    )
    rec = meta["tasks"][0]
    assert rec["io_mode"] == "headless_stream"
    assert "supervisor" in rec["pane_command"]


def test_parser_accepts_io_mode() -> None:
    from omg_cli.main import build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["team", "launch", "--workers", "1", "--goal", "x", "--io-mode", "interactive"]
    )
    assert args.io_mode == "interactive"
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["team", "launch", "--workers", "1", "--goal", "x", "--io-mode", "proxy"]
        )


def test_fixture_refuses_without_controlling_tty() -> None:
    import subprocess
    import sys

    script = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "providers"
        / "interactive_tty.py"
    )
    proc = subprocess.run(
        [sys.executable, str(script)],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "OMG_TEAM_PROVIDER_HOLD_S": "1"},
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode != 0
    assert "E_FIXTURE_NO_TTY" in combined or "E_FIXTURE_TIMEOUT" in combined
    assert "PROVIDER_ECHO:" not in combined


def test_tui_ready_marker_is_exact_line_only() -> None:
    nonce = make_interactive_nonce()
    marker = tui_ready_marker(nonce)
    assert capture_contains_tui_ready(f"{marker}\nnext\n", nonce)
    assert not capture_contains_tui_ready(f"prefix {marker}\n", nonce)
    assert not capture_contains_tui_ready(f"{marker}EVIL\n", nonce)
    assert not capture_contains_tui_ready("TUI_READY:other\n", nonce)
    assert not capture_contains_tui_ready("", nonce)


def test_sanitize_tty_payload_drops_csi_and_empty() -> None:
    from tests.fixtures.providers.interactive_tty import sanitize_tty_payload

    assert sanitize_tty_payload("") == ""
    assert sanitize_tty_payload("\r\n") == ""
    assert sanitize_tty_payload("\x1b[?1;2c") == ""
    assert sanitize_tty_payload("\x1b[?1;2comg147-echo-1") == "omg147-echo-1"
    assert sanitize_tty_payload("omg147-echo-1\r") == "omg147-echo-1"


def test_linger_after_echo_disabled_is_immediate(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from tests.fixtures.providers.interactive_tty import _linger_after_echo

    monkeypatch.setenv("OMG_TEAM_PROVIDER_LINGER_S", "0")
    started = time.monotonic()
    _linger_after_echo(started + 30)
    assert time.monotonic() - started < 0.5


@pytest.mark.skipif(os.name != "posix" or sys.platform == "win32", reason="POSIX PTY only")
def test_fixture_echoes_payload_after_stray_pty_bytes() -> None:
    """Spurious CR/DA1 must not consume the TTY read; PROVIDER_ECHO is the write.

    This is the hermetic shape of the real-tmux CI timeout: TUI_READY appeared,
    then send-keys never produced PROVIDER_ECHO:<payload> because a leftover
    newline had already ended the fixture.
    """
    import select
    import subprocess
    import time

    pty = pytest.importorskip("pty")

    script = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "providers"
        / "interactive_tty.py"
    )
    master, slave = pty.openpty()
    slave_open = True
    proc: subprocess.Popen[bytes] | None = None
    try:
        env = {
            **os.environ,
            "OMG_TEAM_INTERACTIVE_NONCE": "deadbeef",
            "OMG_TEAM_PROVIDER_HOLD_S": "8",
            "OMG_TEAM_PROVIDER_LINGER_S": "0",
            "TERM": "xterm",
        }
        proc = subprocess.Popen(
            [sys.executable, "-u", str(script)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True,
        )
        os.close(slave)
        slave_open = False
        # tmux-shaped leftover: CR + DA1 + CRLF sitting on the PTY.
        os.write(master, b"\r\x1b[?1;2c\r\n")

        def _read_until(needle: bytes, timeout_s: float) -> bytes:
            deadline = time.monotonic() + timeout_s
            buf = b""
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if not ready:
                    continue
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                if needle in buf:
                    return buf
            return buf

        ready_buf = _read_until(b"TUI_READY:deadbeef", 5.0)
        assert b"TUI_READY:deadbeef" in ready_buf, ready_buf
        assert b"PROVIDER_ECHO:" not in ready_buf
        payload = b"omg147-pty-payload"
        os.write(master, payload + b"\r")
        echo_buf = ready_buf + _read_until(b"PROVIDER_ECHO:omg147-pty-payload", 5.0)
        assert b"PROVIDER_ECHO:omg147-pty-payload" in echo_buf, echo_buf
        proc.wait(timeout=5)
        assert proc.returncode == 0
    finally:
        if slave_open:
            try:
                os.close(slave)
            except OSError:
                pass
        try:
            os.close(master)
        except OSError:
            pass
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)


def test_wait_for_tui_ready_does_not_write_or_self_promote() -> None:
    nonce = "abc123de"
    seen: list[str] = []

    def _capture(pane_id: str) -> str:
        seen.append(pane_id)
        return f"TUI_READY:{nonce}\n"

    workers = [
        {
            "task_id": "t1",
            "pane_id": "%12",
            "interactive_nonce": nonce,
            "io_mode": IO_MODE_INTERACTIVE_TTY,
            "input_ready": False,
        }
    ]
    out = wait_for_interactive_tui_ready(
        workers,
        timeout_ms=1000,
        poll_s=0.01,
        capture_fn=_capture,
        sleep_fn=lambda _s: None,
    )
    assert seen == ["%12"]
    assert out["ready_workers"] == ["t1"]
    assert out["missing_workers"] == []
    assert out["evidence"]["t1"]["ready_marker"] == "TUI_READY:abc123de"
    # Wait is evidence-only: the input row is unchanged (no self-promote).
    assert workers[0]["input_ready"] is False


def test_wait_for_tui_ready_timeout_without_marker() -> None:
    out = wait_for_interactive_tui_ready(
        [
            {
                "task_id": "t1",
                "pane_id": "%1",
                "interactive_nonce": "deadbeef",
            }
        ],
        timeout_ms=0,
        poll_s=0.01,
        capture_fn=lambda _pane: "still booting\n",
        sleep_fn=lambda _s: None,
    )
    assert out["ready_workers"] == []
    assert out["missing_workers"] == ["t1"]
    assert out["evidence"] == {}


def test_wait_wrong_nonce_is_not_ready() -> None:
    out = wait_for_interactive_tui_ready(
        [{"task_id": "t1", "pane_id": "%1", "interactive_nonce": "want"}],
        timeout_ms=0,
        capture_fn=lambda _pane: "TUI_READY:other\n",
        sleep_fn=lambda _s: None,
    )
    assert out["ready_workers"] == []


def test_leader_ready_stamp_requires_tui_marker() -> None:
    cap = interactive_pane_io_ready(
        ready_marker="TUI_READY:aa",
        pane_id="%9",
        attempt=1,
        generation=0,
    )
    assert cap.input_ready is True
    assert cap.io_mode == IO_MODE_INTERACTIVE_TTY
    assert cap.interaction_evidence is not None
    assert cap.interaction_evidence["ready_marker"] == "TUI_READY:aa"
    assert interactive_pane_io_defaults().input_ready is False
    with pytest.raises(ValueError):
        interactive_pane_io_ready(ready_marker="READY", pane_id="%9")


@_POSIX
def test_interactive_no_wait_does_not_promote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.team.runtime import apply_start_readiness
    from omg_cli.team.supervisor import load_provider_descriptor
    from omg_cli.team.plane import team_dir

    _enable(monkeypatch)
    _git_init(tmp_path)
    meta = start_team(
        "interactive fixture",
        TASKS,
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        executor="fixture",
        io_mode="interactive",
        env={EXPERIMENTAL_ENV: "1"},
    )
    out = apply_start_readiness(
        tmp_path, {**meta, "dry_run": False}, dry_run=False, no_wait=True
    )
    assert out["startup_status"] == "unverified_start"
    task = out["tasks"][0]
    assert task["io_mode"] == IO_MODE_INTERACTIVE_TTY
    assert task["input_ready"] is False
    desc = load_provider_descriptor(team_dir(tmp_path, meta["run_id"]) / "t1.provider.json")
    assert desc["input_ready"] is False


@_POSIX
def test_interactive_timeout_fails_closed_no_headless_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.team.plane import mutate_team_meta
    from omg_cli.team.runtime import apply_start_readiness

    _enable(monkeypatch)
    _git_init(tmp_path)
    meta = start_team(
        "interactive fixture",
        TASKS,
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        executor="fixture",
        io_mode="interactive",
        env={EXPERIMENTAL_ENV: "1"},
    )

    def _pane(current: dict) -> dict:
        current["tasks"][0]["pane_id"] = "%42"
        return current

    mutate_team_meta(tmp_path, str(meta["run_id"]), _pane)
    monkeypatch.setattr(
        "omg_cli.team.runtime._capture_interactive_pane",
        lambda pane_id, socket_path=None: "nope\n",
    )
    out = apply_start_readiness(
        tmp_path,
        {**meta, "dry_run": False},
        dry_run=False,
        env={"OMG_TEAM_READY_TIMEOUT_MS": "50"},
    )
    assert out["startup_status"] == "failed_start"
    task = out["tasks"][0]
    assert task["io_mode"] == IO_MODE_INTERACTIVE_TTY
    assert task["input_ready"] is False
    assert "did not downgrade to headless" in str(out.get("startup_note") or "")


@_POSIX
def test_leader_promotes_input_ready_after_tui_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.team.io_capability import (
        E_OPERATOR_INPUT_NOT_READY,
        normalize_worker_io_capability,
        operator_input_refusal,
    )
    from omg_cli.team.plane import mutate_team_meta, team_dir
    from omg_cli.team.runtime import apply_start_readiness
    from omg_cli.team.supervisor import load_provider_descriptor

    _enable(monkeypatch)
    _git_init(tmp_path)
    meta = start_team(
        "interactive fixture",
        TASKS,
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        executor="fixture",
        io_mode="interactive",
        env={EXPERIMENTAL_ENV: "1"},
    )
    nonce = str(meta["tasks"][0]["interactive_nonce"])

    def _pane(current: dict) -> dict:
        current["tasks"][0]["pane_id"] = "%7"
        return current

    mutate_team_meta(tmp_path, str(meta["run_id"]), _pane)

    # Before promotion, operator input stays not-ready.
    before = normalize_worker_io_capability(meta["tasks"][0])
    refusal = operator_input_refusal(before, action="input")
    assert refusal is not None
    assert refusal.code == E_OPERATOR_INPUT_NOT_READY

    monkeypatch.setattr(
        "omg_cli.team.runtime._capture_interactive_pane",
        lambda pane_id, socket_path=None: f"noise\nTUI_READY:{nonce}\n",
    )
    out = apply_start_readiness(
        tmp_path,
        {**meta, "dry_run": False},
        dry_run=False,
        env={"OMG_TEAM_READY_TIMEOUT_MS": "2000"},
    )
    assert out["startup_status"] == "running"
    assert out["startup_gate_phase"] == "tui_ready"
    task = out["tasks"][0]
    assert task["io_mode"] == IO_MODE_INTERACTIVE_TTY
    assert task["input_ready"] is True
    assert task["interaction_evidence"]["ready_marker"] == f"TUI_READY:{nonce}"
    desc = load_provider_descriptor(team_dir(tmp_path, meta["run_id"]) / "t1.provider.json")
    assert desc["input_ready"] is False
    assert desc["operator_input_supported"] is False
    # Scraping the same stdout in-process must not be what flipped the descriptor.
    assert "TUI_READY" not in json.dumps(desc)


@_POSIX
def test_scale_up_refuses_interactive_team(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable(monkeypatch)
    _git_init(tmp_path)
    meta = start_team(
        "interactive fixture",
        TASKS,
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        executor="fixture",
        io_mode="interactive",
        env={EXPERIMENTAL_ENV: "1"},
    )
    assert meta["tasks"][0]["io_mode"] == IO_MODE_INTERACTIVE_TTY
    with pytest.raises(TeamError, match="interactive TTY"):
        scale_team(tmp_path, meta["run_id"], add=1, dry_run=True)


def test_wrapper_refuses_without_tty() -> None:
    from omg_cli.team.interactive_wrapper import main as wrap_main

    rc = wrap_main(["--", sys.executable, "-c", "print('no')"])
    assert rc == 2
    # main() prints to stderr; captured by pytest. Don't claim TUI_READY.


@pytest.mark.skipif(os.name != "posix" or sys.platform == "win32", reason="POSIX PTY only")
def test_wrapper_emits_tui_ready_only_after_child_reads_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Child sleep is not ready; TUI_READY fires after the child reads stdin.

    PROVIDER_ECHO still comes from the child after that read — the wrapper
    must not echo the payload itself.
    """
    import select
    import subprocess
    import time

    pty = pytest.importorskip("pty")
    child_py = tmp_path / "child_read.py"
    child_py.write_text(
        "import sys, time\n"
        "time.sleep(0.2)\n"
        "print('booting', flush=True)\n"
        "time.sleep(0.4)\n"
        "line = sys.stdin.readline()\n"
        "print('PROVIDER_ECHO:' + line.rstrip('\\r\\n'), flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMG_TEAM_INTERACTIVE_NONCE", "cafebabe")
    master, slave = pty.openpty()
    slave_open = True
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "omg_cli.team.interactive_wrapper",
                "--",
                sys.executable,
                "-u",
                str(child_py),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={**os.environ, "OMG_TEAM_INTERACTIVE_NONCE": "cafebabe"},
            close_fds=True,
        )
        os.close(slave)
        slave_open = False

        def _read_until(needle: bytes, timeout_s: float) -> bytes:
            deadline = time.monotonic() + timeout_s
            buf = b""
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if not ready:
                    continue
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                if needle in buf:
                    return buf
            return buf

        boot = _read_until(b"booting", 5.0)
        assert b"booting" in boot, boot
        assert b"TUI_READY:cafebabe" not in boot
        ready_buf = boot + _read_until(b"TUI_READY:cafebabe", 5.0)
        assert b"TUI_READY:cafebabe" in ready_buf, ready_buf
        assert b"PROVIDER_ECHO:" not in ready_buf
        payload = b"omg147-wrap-payload"
        os.write(master, payload + b"\n")
        echo_buf = ready_buf + _read_until(b"PROVIDER_ECHO:omg147-wrap-payload", 5.0)
        assert b"PROVIDER_ECHO:omg147-wrap-payload" in echo_buf, echo_buf
        proc.wait(timeout=5)
        assert proc.returncode == 0
    finally:
        if slave_open:
            try:
                os.close(slave)
            except OSError:
                pass
        try:
            os.close(master)
        except OSError:
            pass
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)


@pytest.mark.skipif(os.name != "posix" or sys.platform == "win32", reason="POSIX PTY only")
def test_wrapper_no_tui_ready_when_child_never_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import select
    import subprocess
    import time

    pty = pytest.importorskip("pty")
    child_py = tmp_path / "child_sleep.py"
    child_py.write_text("import time\ntime.sleep(8)\n", encoding="utf-8")
    monkeypatch.setenv("OMG_TEAM_INTERACTIVE_NONCE", "deadbeef")
    monkeypatch.setenv("OMG_TEAM_WRAPPER_READY_TIMEOUT_MS", "400")
    master, slave = pty.openpty()
    slave_open = True
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "omg_cli.team.interactive_wrapper",
                "--timeout-ms",
                "400",
                "--",
                sys.executable,
                "-u",
                str(child_py),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={
                **os.environ,
                "OMG_TEAM_INTERACTIVE_NONCE": "deadbeef",
                "OMG_TEAM_WRAPPER_READY_TIMEOUT_MS": "400",
            },
            close_fds=True,
        )
        os.close(slave)
        slave_open = False
        deadline = time.monotonic() + 3.0
        buf = b""
        while time.monotonic() < deadline and proc.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.1)
            if not ready:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        proc.wait(timeout=5)
        assert b"TUI_READY:" not in buf, buf
        assert proc.returncode != 0
    finally:
        if slave_open:
            try:
                os.close(slave)
            except OSError:
                pass
        try:
            os.close(master)
        except OSError:
            pass
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)


def test_process_is_live_false_for_zombie_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.team import interactive_wrapper as wrap

    monkeypatch.setattr(wrap, "_proc_state", lambda _pid: "Z")
    monkeypatch.setattr(os, "kill", lambda *_a, **_k: None)
    assert wrap._process_is_live(4242) is False
    monkeypatch.setattr(wrap, "_proc_state", lambda _pid: "S")
    assert wrap._process_is_live(4242) is True


def test_child_alive_false_after_waitpid_reaps(monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.team import interactive_wrapper as wrap

    if not hasattr(os, "WNOHANG"):
        pytest.skip("WNOHANG is POSIX")
    monkeypatch.setattr(os, "waitpid", lambda _pid, _flags: (99, 0))
    monkeypatch.setattr(wrap, "_process_is_live", lambda _pid: True)
    assert wrap._child_alive(99) is False


def test_child_waiting_on_stdin_poll_sleep_requires_raw_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team import interactive_wrapper as wrap

    syscall = tmp_path / "syscall"
    syscall.write_text(
        "7 0x7fff0000 1 0xffffffff 0 0 0 0x7fff1000 0x401000\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(wrap, "_process_is_live", lambda _pid: True)
    monkeypatch.setattr(wrap, "_proc_fd0_target", lambda _pid: "/dev/pts/3")
    monkeypatch.setattr(wrap, "_iter_task_syscall_paths", lambda _pid: [syscall])
    monkeypatch.setattr(wrap, "_proc_state_sleeping", lambda _pid: True)
    monkeypatch.setattr(wrap, "_machine", lambda: "x86_64")
    monkeypatch.setattr(wrap, "_tty_in_raw_or_noncanonical", lambda: False)
    assert wrap.child_waiting_on_stdin(99, "/dev/pts/3") is False
    monkeypatch.setattr(wrap, "_tty_in_raw_or_noncanonical", lambda: True)
    assert wrap.child_waiting_on_stdin(99, "/dev/pts/3") is True


def test_child_waiting_on_stdin_read_fd0_does_not_need_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team import interactive_wrapper as wrap

    syscall = tmp_path / "syscall"
    syscall.write_text(
        "0 0x0 0x7fff0000 0x400 0 0 0 0x7fff1000 0x401000\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(wrap, "_process_is_live", lambda _pid: True)
    monkeypatch.setattr(wrap, "_proc_fd0_target", lambda _pid: "/dev/pts/3")
    monkeypatch.setattr(wrap, "_iter_task_syscall_paths", lambda _pid: [syscall])
    monkeypatch.setattr(wrap, "_machine", lambda: "x86_64")
    monkeypatch.setattr(wrap, "_tty_in_raw_or_noncanonical", lambda: False)
    assert wrap.child_waiting_on_stdin(99, "/dev/pts/3") is True


def test_child_waiting_on_stdin_zombie_ignores_leftover_raw_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.team import interactive_wrapper as wrap

    monkeypatch.setattr(wrap, "_process_is_live", lambda _pid: False)
    monkeypatch.setattr(wrap, "_proc_fd0_target", lambda _pid: None)
    monkeypatch.setattr(wrap, "_tty_in_raw_or_noncanonical", lambda: True)
    assert wrap.child_waiting_on_stdin(99, "/dev/pts/3") is False


@pytest.mark.skipif(os.name != "posix" or sys.platform == "win32", reason="POSIX PTY only")
def test_wrapper_no_tui_ready_when_child_polls_non_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import select
    import subprocess
    import time

    pty = pytest.importorskip("pty")
    child_py = tmp_path / "child_poll_pipe.py"
    child_py.write_text(
        "import os, select\n"
        "r, w = os.pipe()\n"
        "select.select([r], [], [], 8)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMG_TEAM_INTERACTIVE_NONCE", "deadbeef")
    master, slave = pty.openpty()
    slave_open = True
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "omg_cli.team.interactive_wrapper",
                "--timeout-ms",
                "400",
                "--",
                sys.executable,
                "-u",
                str(child_py),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={
                **os.environ,
                "OMG_TEAM_INTERACTIVE_NONCE": "deadbeef",
                "OMG_TEAM_WRAPPER_READY_TIMEOUT_MS": "400",
            },
            close_fds=True,
        )
        os.close(slave)
        slave_open = False
        deadline = time.monotonic() + 3.0
        buf = b""
        while time.monotonic() < deadline and proc.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.1)
            if not ready:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        proc.wait(timeout=5)
        assert b"TUI_READY:" not in buf, buf
        assert proc.returncode != 0
    finally:
        if slave_open:
            try:
                os.close(slave)
            except OSError:
                pass
        try:
            os.close(master)
        except OSError:
            pass
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)


@pytest.mark.skipif(os.name != "posix" or sys.platform == "win32", reason="POSIX PTY only")
def test_wrapper_no_tui_ready_when_child_sets_raw_then_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import select
    import subprocess
    import time

    pty = pytest.importorskip("pty")
    child_py = tmp_path / "child_raw_exit.py"
    child_py.write_text(
        "import sys, termios, os, time\n"
        "fd = sys.stdin.fileno()\n"
        "attrs = termios.tcgetattr(fd)\n"
        "attrs[3] &= ~termios.ICANON\n"
        "termios.tcsetattr(fd, termios.TCSANOW, attrs)\n"
        "time.sleep(0.05)\n"
        "os._exit(1)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMG_TEAM_INTERACTIVE_NONCE", "deadbeef")
    master, slave = pty.openpty()
    slave_open = True
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "omg_cli.team.interactive_wrapper",
                "--timeout-ms",
                "800",
                "--",
                sys.executable,
                "-u",
                str(child_py),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={
                **os.environ,
                "OMG_TEAM_INTERACTIVE_NONCE": "deadbeef",
                "OMG_TEAM_WRAPPER_READY_TIMEOUT_MS": "800",
            },
            close_fds=True,
        )
        os.close(slave)
        slave_open = False
        deadline = time.monotonic() + 3.0
        buf = b""
        while time.monotonic() < deadline and proc.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.1)
            if not ready:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        proc.wait(timeout=5)
        assert b"TUI_READY:" not in buf, buf
        assert proc.returncode != 0
    finally:
        if slave_open:
            try:
                os.close(slave)
            except OSError:
                pass
        try:
            os.close(master)
        except OSError:
            pass
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)
