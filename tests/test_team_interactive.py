"""#147 PR2: direct-exec interactive pane contract (hermetic)."""
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

import pytest

from omg_cli.team.interactive import (
    E_TEAM_IO_MODE_UNSUPPORTED,
    ECHO_PROBE_ENV,
    GROK_INTERACTIVE_RULES,
    GROK_INTERACTIVE_SEED_PROMPT,
    INTERACTIVE_NONCE_ENV,
    InteractiveTeamError,
    api_worker_inbox_basename,
    capture_contains_provider_echo,
    capture_contains_tui_ready,
    evidence_matches_worker_identity,
    grok_interactive_argv,
    interactive_inbox_basename,
    interactive_inbox_instruction,
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
    assert "--single-turn" not in argv
    assert "-p" not in argv
    assert "--single" not in argv
    assert "--prompt" not in argv
    assert "--no-alt-screen" in argv
    assert "--minimal" in argv
    assert "--no-subagents" in argv
    assert "--rules" not in argv
    assert argv[-1] == GROK_INTERACTIVE_SEED_PROMPT
    assert argv[-1] != "--prompt-file"
    echo = grok_interactive_argv(
        cwd="/tmp/wt", posture="read-write", rules=GROK_INTERACTIVE_RULES
    )
    assert "--rules" in echo
    assert echo[echo.index("--rules") + 1].startswith("When the user sends a line")
    assert echo[-1] == GROK_INTERACTIVE_SEED_PROMPT
    assert "--prompt" not in echo
    assert "bypassPermissions" not in argv
    yolo = grok_interactive_argv(cwd="/tmp/wt", posture="read-write", yolo=True)
    assert "bypassPermissions" in yolo
    assert "--always-approve" in yolo
    assert yolo[-1] == GROK_INTERACTIVE_SEED_PROMPT
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
    assert GROK_INTERACTIVE_SEED_PROMPT in text
    assert " --prompt " not in f" {text} "
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
    with pytest.raises(InteractiveTeamError, match="--prompt flag"):
        write_interactive_exec_script(
            dest=dest,
            argv=["grok", "--prompt", "x"],
            worktree=tmp_path,
        )
    with pytest.raises(InteractiveTeamError, match="-p/--single"):
        write_interactive_exec_script(
            dest=dest,
            argv=["grok", "-p", "x"],
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
    assert str(rec["inbox_path"]).endswith("t1.a1.inbox.txt")
    assert "t1.inbox.txt" not in str(rec["inbox_path"]).replace("t1.a1.inbox.txt", "")
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
    assert "--rules" not in rec["argv"]
    assert "--prompt" not in rec["argv"]
    assert rec["argv"][-1] == GROK_INTERACTIVE_SEED_PROMPT
    assert "--single-turn" not in rec["argv"]
    assert "-p" not in rec["argv"]
    assert "supervisor" not in rec["pane_command"]
    exec_path = tmp_path / ".omg" / "state" / "runs" / meta["run_id"] / "team" / "t1.interactive.sh"
    script = exec_path.read_text(encoding="utf-8")
    assert "omg_cli.team.interactive_wrapper" in script
    assert "PYTHONPATH=" in script
    assert "--prompt-file" not in script
    rules = tmp_path / ".omg" / "state" / "runs" / meta["run_id"] / "team" / "t1.interactive.rules.txt"
    assert not rules.is_file()


@_POSIX
def test_dry_run_interactive_echo_probe_attaches_rules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable(monkeypatch)
    monkeypatch.setenv(ECHO_PROBE_ENV, "1")
    _git_init(tmp_path)
    meta = start_team(
        "interactive grok",
        TASKS,
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        io_mode="interactive",
        env={EXPERIMENTAL_ENV: "1", ECHO_PROBE_ENV: "1"},
    )
    rec = meta["tasks"][0]
    assert "--rules" in rec["argv"]
    assert rec["argv"][rec["argv"].index("--rules") + 1].startswith(
        "When the user sends a line"
    )
    assert rec["argv"][-1] == GROK_INTERACTIVE_SEED_PROMPT
    assert "--prompt" not in rec["argv"]
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


def test_provider_echo_allows_optional_space_not_local_composer() -> None:
    token = "OMG147-LIVE-01548eed17fe"
    assert capture_contains_provider_echo(f"PROVIDER_ECHO:{token}\n", token)
    assert capture_contains_provider_echo(f"PROVIDER_ECHO: {token}\n", token)
    assert capture_contains_provider_echo(
        "Thought for 0.8s\nPROVIDER_ECHO: OMG147-LIVE-01548eed17fe\n❯\n",
        token,
    )
    assert not capture_contains_provider_echo(f"❯ {token}\n", token)
    assert not capture_contains_provider_echo(f"PROVIDER_ECHO:{token}EVIL\n", token)
    assert not capture_contains_provider_echo(
        f"PROVIDER_ECHO:\n{token}\n", token
    )
    assert not capture_contains_provider_echo("", token)


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


@pytest.mark.skipif(os.name != "posix" or sys.platform == "win32", reason="POSIX PTY only")
def test_fixture_sigint_prints_int_and_does_not_exit() -> None:
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
            "OMG_TEAM_INTERACTIVE_NONCE": "sigintab",
            "OMG_TEAM_PROVIDER_HOLD_S": "8",
            "OMG_TEAM_PROVIDER_LINGER_S": "8",
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

        ready_buf = _read_until(b"TUI_READY:sigintab", 5.0)
        assert b"TUI_READY:sigintab" in ready_buf, ready_buf
        proc.send_signal(signal.SIGINT)
        int_buf = ready_buf + _read_until(b"INT:", 5.0)
        assert b"INT:" in int_buf, int_buf
        assert proc.poll() is None
        os.write(master, b"after-int\r")
        echo_buf = int_buf + _read_until(b"PROVIDER_ECHO:after-int", 5.0)
        assert b"PROVIDER_ECHO:after-int" in echo_buf, echo_buf
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


def test_evidence_matches_worker_identity_refuses_pane_pid_start_flip() -> None:
    ev = {
        "pane_id": "%7",
        "provider_pid": 111,
        "pid_start": "start-a",
        "attempt": 1,
        "generation": 0,
    }
    row = {
        "pane_id": "%7",
        "pid": 111,
        "pid_start": "start-a",
        "attempt": 1,
        "generation": 0,
    }
    assert evidence_matches_worker_identity(ev, row)
    assert not evidence_matches_worker_identity(ev, {**row, "pane_id": "%8"})
    assert not evidence_matches_worker_identity(ev, {**row, "pid": 222})
    assert not evidence_matches_worker_identity(ev, {**row, "pid_start": "start-b"})
    assert not evidence_matches_worker_identity(ev, {**row, "attempt": 2})
    # Scale-down clears pid while retaining pane_id — still a mismatch.
    assert not evidence_matches_worker_identity(ev, {**row, "pid": None})
    assert not evidence_matches_worker_identity(
        ev, {k: v for k, v in row.items() if k != "pid"}
    )
    assert not evidence_matches_worker_identity(ev, {**row, "pid": 0})
    # Evidence pid_start is bound: missing/empty row start is mismatch, not skip.
    assert not evidence_matches_worker_identity(ev, {**row, "pid_start": None})
    assert not evidence_matches_worker_identity(
        ev, {k: v for k, v in row.items() if k != "pid_start"}
    )
    assert not evidence_matches_worker_identity(ev, {**row, "pid_start": ""})


def test_wait_for_tui_ready_prove_fn_refuse_is_not_ready() -> None:
    nonce = "abc123de"
    out = wait_for_interactive_tui_ready(
        [
            {
                "task_id": "t1",
                "pane_id": "%12",
                "pid": 100,
                "pid_start": "A",
                "interactive_nonce": nonce,
            }
        ],
        timeout_ms=0,
        poll_s=0.01,
        capture_fn=lambda _pane: f"TUI_READY:{nonce}\n",
        sleep_fn=lambda _s: None,
        prove_fn=lambda _row, _ev: False,
    )
    assert out["ready_workers"] == []
    assert out["missing_workers"] == ["t1"]


def test_overlay_reuses_nonce_and_attempt_inbox(tmp_path: Path) -> None:
    from omg_cli.team.interactive import overlay_interactive_launch

    if os.name != "posix":
        pytest.skip("managed inbox write requires POSIX")
    first = overlay_interactive_launch(
        team_dir=tmp_path,
        task_id="t1",
        attempt=1,
        worktree=tmp_path,
        goal="do the work",
        use_fixture=True,
    )
    nonce = first["interactive_nonce"]
    assert (tmp_path / "t1.a1.inbox.txt").is_file()
    assert "supervisor" not in first["pane_command"]
    second = overlay_interactive_launch(
        team_dir=tmp_path,
        task_id="t1",
        attempt=2,
        worktree=tmp_path,
        goal="do the work again",
        use_fixture=True,
    )
    assert second["interactive_nonce"] == nonce
    assert (tmp_path / "t1.a2.inbox.txt").is_file()
    assert (tmp_path / "t1.a1.inbox.txt").is_file()
    assert "attempt=2" in (tmp_path / "t1.a2.inbox.txt").read_text(encoding="utf-8")


def test_overlay_inbox_includes_task_assignment(tmp_path: Path) -> None:
    from omg_cli.team.interactive import overlay_interactive_launch

    if os.name != "posix":
        pytest.skip("managed inbox write requires POSIX")
    overlay_interactive_launch(
        team_dir=tmp_path,
        task_id="t1",
        attempt=1,
        worktree=tmp_path / "wt-t1",
        goal="shared team goal",
        use_fixture=True,
        owned_files=["src/a.py", "README.md"],
        role="executor",
        subject="implement slice A",
        depends_on=["t0"],
        run_id="run-1",
        team_id="team",
        api_task_id="42",
    )
    text = (tmp_path / "t1.a1.inbox.txt").read_text(encoding="utf-8")
    assert "task_id=t1" in text
    assert "attempt=1" in text
    assert "role=executor" in text
    assert "board_task_id=42" in text
    assert "## Assignment" in text
    assert "implement slice A" in text
    assert "- `src/a.py`" in text
    assert "- `README.md`" in text
    assert "## Depends on" in text
    assert "- `t0`" in text
    assert "shared team goal" in text
    assert "omg team api" in text
    assert "claim-task" in text or "Claim board task" in text


def test_inbox_basenames_include_task_id_and_attempt() -> None:
    assert interactive_inbox_basename("t1", 1) == "t1.a1.inbox.txt"
    assert interactive_inbox_basename("t1", 2) == "t1.a2.inbox.txt"
    assert api_worker_inbox_basename("w1", 3) == "w1.a3.inbox.md"
    path = "/tmp/run/team/t1.a2.inbox.txt"
    text = interactive_inbox_instruction(path)
    assert "t1.a2.inbox.txt" in text
    assert GROK_INTERACTIVE_SEED_PROMPT not in text
    with pytest.raises(InteractiveTeamError):
        interactive_inbox_basename("", 1)


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
def test_promote_refuses_when_identity_flips_between_capture_and_stamp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omg_cli.team.interactive as interactive_mod
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
    nonce = str(meta["tasks"][0]["interactive_nonce"])
    run_id = str(meta["run_id"])

    def _pane(current: dict) -> dict:
        current["tasks"][0]["pane_id"] = "%7"
        current["tasks"][0]["pid"] = 111
        current["tasks"][0]["pid_start"] = "start-a"
        return current

    mutate_team_meta(tmp_path, run_id, _pane)
    real_wait = interactive_mod.wait_for_interactive_tui_ready

    def _wait_then_flip(*args: object, **kwargs: object):
        out = real_wait(*args, **kwargs)

        def _flip(current: dict) -> dict:
            current["tasks"][0]["pane_id"] = "%99"
            current["tasks"][0]["pid"] = 999
            current["tasks"][0]["pid_start"] = "flipped"
            return current

        mutate_team_meta(tmp_path, run_id, _flip)
        return out

    monkeypatch.setattr(
        interactive_mod, "wait_for_interactive_tui_ready", _wait_then_flip
    )
    monkeypatch.setattr(
        "omg_cli.team.runtime._capture_interactive_pane",
        lambda pane_id, socket_path=None: f"TUI_READY:{nonce}\n",
    )
    out = apply_start_readiness(
        tmp_path,
        {**meta, "dry_run": False},
        dry_run=False,
        env={"OMG_TEAM_READY_TIMEOUT_MS": "2000"},
    )
    assert out["tasks"][0]["input_ready"] is False
    assert out["startup_status"] == "failed_start"
    assert "%99" == out["tasks"][0]["pane_id"]


@_POSIX
def test_scale_up_interactive_team_uses_exec_wrapper_not_supervisor(
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
    out = scale_team(tmp_path, meta["run_id"], add=1, dry_run=True)
    added = out.get("tasks_added") or []
    assert len(added) == 1
    rec = added[0]
    assert rec["io_mode"] == IO_MODE_INTERACTIVE_TTY
    assert rec["input_ready"] is False
    assert rec["operator_input_supported"] is True
    assert rec["provider_tty_owner"] == "provider"
    pane = str(rec.get("pane_command") or "")
    assert "supervisor" not in pane
    assert "omg_cli.main" not in pane
    assert "interactive.sh" in pane or pane.startswith("exec /bin/sh")
    assert rec.get("inbox_path")
    assert ".a1.inbox.txt" in str(rec["inbox_path"])
    assert rec.get("interactive_nonce")
    assert "--prompt-file" not in json.dumps(rec.get("argv") or [])


@_POSIX
def test_pending_scale_records_interactive_expects_inbox_exec_not_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
    from omg_cli.team.plane import IDENTITY_RECEIPT_SCHEMA_VERSION, team_dir
    from omg_cli.team.scaling import (
        STATUS_RUNNING,
        _build_scale_intent,
        _pending_scale_records,
    )
    from omg_cli.workers import load_ownership_manifest, worktree_dir

    _enable(monkeypatch)
    root = tmp_path.resolve()
    _git_init(root)
    meta = start_team(
        "interactive fixture",
        TASKS,
        root=root,
        dry_run=True,
        check_binary=False,
        executor="fixture",
        io_mode="interactive",
        env={EXPERIMENTAL_ENV: "1"},
    )
    rid = str(meta["run_id"])
    out = scale_team(root, rid, add=1, dry_run=True)
    added = out.get("tasks_added") or []
    assert len(added) == 1
    rec = dict(added[0])
    tid = str(rec["task_id"])
    attempt = int(rec.get("attempt") or 1)
    tdir = team_dir(root, rid)
    inbox = tdir / interactive_inbox_basename(tid, attempt)
    exec_script = tdir / f"{tid}.interactive.sh"
    argv_path = tdir / f"{tid}.argv.json"
    assert inbox.is_file()
    assert exec_script.is_file()
    assert argv_path.is_file()
    inbox_before = inbox.read_bytes()
    exec_before = exec_script.read_bytes()
    rec["status"] = STATUS_RUNNING
    rec["_artifact_paths"] = [
        str(inbox.relative_to(root)),
        str(exec_script.relative_to(root)),
        str(argv_path.relative_to(root)),
    ]
    request_sha256 = "ab" * 32
    intent = _build_scale_intent(
        root,
        rid,
        request_sha256=request_sha256,
        scale_wal_sha256="cd" * 32,
        records=[rec],
    )
    intent.pop("scale_wal_sha256", None)
    artifact_paths = {row["path"] for row in intent["artifacts"]}
    assert str(inbox.relative_to(root)) in artifact_paths
    assert str(exec_script.relative_to(root)) in artifact_paths
    assert str(argv_path.relative_to(root)) in artifact_paths
    assert not any(path.endswith(".prompt.md") for path in artifact_paths)
    assert not any(Path(path).name == "last_prompt.md" for path in artifact_paths)
    prompt_dir = worktree_dir(root, rid, tid) / ".omg" / "team-prompt"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / f"{tid}.prompt.md").write_text("unused-prompt\n", encoding="utf-8")
    (prompt_dir / "last_prompt.md").write_text("unused-last\n", encoding="utf-8")
    receipt = {
        "schema_version": IDENTITY_RECEIPT_SCHEMA_VERSION,
        "scale_intent": intent,
        "scale_intent_sha256": sha256_hex(canonical_json_bytes(intent)),
    }
    owned = load_ownership_manifest(root, rid)
    spec = next(row for row in owned["tasks"] if row["task_id"] == tid)
    recovered = _pending_scale_records(
        root,
        rid,
        receipt=receipt,
        request_sha256=request_sha256,
        task_specs=[
            {
                "task_id": tid,
                "owned_files": list(spec["owned_files"]),
                "role": spec.get("role") or "executor",
            }
        ],
    )
    assert [row["task_id"] for row in recovered] == [tid]
    assert recovered[0]["prompt_delivery"] == "interactive-tty"
    assert inbox.is_file()
    assert exec_script.is_file()
    assert inbox.read_bytes() == inbox_before
    assert exec_script.read_bytes() == exec_before


@_POSIX
def test_promote_lock_mismatch_returns_no_stamped_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omg_cli.team.runtime as runtime_mod
    from omg_cli.team.plane import load_team_meta, mutate_team_meta

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
    run_id = str(meta["run_id"])

    def _pane(current: dict) -> dict:
        current["tasks"][0]["pane_id"] = "%7"
        current["tasks"][0]["pid"] = 111
        current["tasks"][0]["pid_start"] = "start-a"
        return current

    mutate_team_meta(tmp_path, run_id, _pane)
    evidence = {
        "t1": {
            "task_id": "t1",
            "ready_marker": f"TUI_READY:{nonce}",
            "pane_id": "%7",
            "provider_pid": 111,
            "pid_start": "start-a",
            "attempt": 1,
            "generation": 0,
        }
    }
    real_filter = runtime_mod._filter_interactive_ready_evidence

    def _filter_then_flip(
        root: object, run_id_arg: str, evidence_by_id: object
    ) -> dict:
        proven = real_filter(root, run_id_arg, evidence_by_id)

        def _flip(current: dict) -> dict:
            current["tasks"][0]["pane_id"] = "%99"
            current["tasks"][0]["pid"] = 999
            current["tasks"][0]["pid_start"] = "flipped"
            return current

        mutate_team_meta(tmp_path, run_id, _flip)
        return proven

    monkeypatch.setattr(runtime_mod, "_filter_interactive_ready_evidence", _filter_then_flip)
    stamped = runtime_mod._promote_interactive_input_ready(tmp_path, run_id, evidence)
    assert stamped == []
    disk = load_team_meta(tmp_path, run_id)
    assert disk["tasks"][0]["input_ready"] is False
    assert disk["tasks"][0]["pane_id"] == "%99"


@_POSIX
def test_promote_restamps_generation_to_current_team_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omg_cli.team.runtime as runtime_mod
    from omg_cli.team.plane import load_team_meta, mutate_team_meta

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
    run_id = str(meta["run_id"])

    def _seed(current: dict) -> dict:
        current["identity_generation"] = 0
        current["tasks"][0]["pane_id"] = "%7"
        current["tasks"][0]["pid"] = 111
        current["tasks"][0]["pid_start"] = "start-a"
        current["tasks"][0]["generation"] = 0
        current["tasks"][0]["attempt"] = 1
        return current

    mutate_team_meta(tmp_path, run_id, _seed)
    evidence = {
        "t1": {
            "task_id": "t1",
            "ready_marker": f"TUI_READY:{nonce}",
            "pane_id": "%7",
            "provider_pid": 111,
            "pid_start": "start-a",
            "attempt": 1,
            "generation": 0,
        }
    }
    stamped = runtime_mod._promote_interactive_input_ready(tmp_path, run_id, evidence)
    assert stamped == ["t1"]

    def _bump(current: dict) -> dict:
        current["identity_generation"] = 1
        return current

    mutate_team_meta(tmp_path, run_id, _bump)
    stamped2 = runtime_mod._promote_interactive_input_ready(tmp_path, run_id, evidence)
    assert stamped2 == ["t1"]
    disk = load_team_meta(tmp_path, run_id)
    assert disk["tasks"][0]["generation"] == 1
    ev = disk["tasks"][0].get("interaction_evidence") or {}
    assert ev.get("generation") == 1


@_POSIX
def test_readiness_rebinds_generation_when_tui_ready_left_scrollback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omg_cli.team.runtime as runtime_mod
    from omg_cli.team.io_capability import (
        interactive_pane_io_ready,
        normalize_worker_io_capability,
        stamp_io_capability,
    )
    from omg_cli.team.plane import load_team_meta, mutate_team_meta

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
    run_id = str(meta["run_id"])

    def _seed(current: dict) -> dict:
        current["identity_generation"] = 0
        row = current["tasks"][0]
        row["pane_id"] = "%7"
        row["pid"] = 111
        row["pid_start"] = "start-a"
        row["generation"] = 0
        row["attempt"] = 1
        stamp_io_capability(
            row,
            interactive_pane_io_ready(
                ready_marker=f"TUI_READY:{nonce}",
                pane_id="%7",
                provider_pid=111,
                attempt=1,
                generation=0,
                pid_start="start-a",
            ),
        )
        return current

    mutate_team_meta(tmp_path, run_id, _seed)

    def _bump(current: dict) -> dict:
        current["identity_generation"] = 1
        return current

    mutate_team_meta(tmp_path, run_id, _bump)
    capture_calls: list[str] = []

    def _scrolled_out(pane_id: str, socket_path: str | None = None) -> str:
        capture_calls.append(pane_id)
        return ("noise line\n" * 250) + "still no marker\n"

    monkeypatch.setattr(
        "omg_cli.team.runtime._capture_interactive_pane", _scrolled_out
    )
    out = runtime_mod.apply_interactive_worker_readiness(
        tmp_path,
        run_id,
        ["t1"],
        timeout_ms=200,
        env={EXPERIMENTAL_ENV: "1", "OMG_TEAM_READY_TIMEOUT_MS": "200"},
    )
    assert capture_calls == []
    assert "t1" in (out.get("startup_ready_workers") or [])
    disk = load_team_meta(tmp_path, run_id)
    assert disk["tasks"][0]["input_ready"] is True
    ev = disk["tasks"][0].get("interaction_evidence") or {}
    assert ev.get("generation") == 1
    cap = normalize_worker_io_capability(
        disk["tasks"][0], attempt=1, generation=1
    )
    assert cap.input_ready is True
    assert (disk["tasks"][0].get("interaction_evidence") or {}).get("pid_start") == "start-a"


@_POSIX
def test_readiness_does_not_rebind_without_persisted_pid_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omg_cli.team.runtime as runtime_mod
    from omg_cli.team.io_capability import (
        interactive_pane_io_ready,
        stamp_io_capability,
    )
    from omg_cli.team.plane import load_team_meta, mutate_team_meta

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
    run_id = str(meta["run_id"])

    def _seed(current: dict) -> dict:
        current["identity_generation"] = 1
        row = current["tasks"][0]
        row["pane_id"] = "%7"
        row["pid"] = 111
        row["pid_start"] = "start-a"
        row["generation"] = 0
        row["attempt"] = 1
        stamp_io_capability(
            row,
            interactive_pane_io_ready(
                ready_marker=f"TUI_READY:{nonce}",
                pane_id="%7",
                provider_pid=111,
                attempt=1,
                generation=0,
            ),
        )
        ev = row.get("interaction_evidence") or {}
        ev.pop("pid_start", None)
        row["interaction_evidence"] = ev
        return current

    mutate_team_meta(tmp_path, run_id, _seed)
    monkeypatch.setattr(
        "omg_cli.team.runtime._capture_interactive_pane",
        lambda pane_id, socket_path=None: "noise line\n" * 250,
    )
    out = runtime_mod.apply_interactive_worker_readiness(
        tmp_path,
        run_id,
        ["t1"],
        timeout_ms=200,
        env={EXPERIMENTAL_ENV: "1", "OMG_TEAM_READY_TIMEOUT_MS": "200"},
    )
    assert "t1" not in (out.get("startup_ready_workers") or [])
    disk = load_team_meta(tmp_path, run_id)
    ev = disk["tasks"][0].get("interaction_evidence") or {}
    assert ev.get("generation") == 0


@_POSIX
def test_readiness_does_not_rebind_when_pane_identity_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omg_cli.team.runtime as runtime_mod
    from omg_cli.team.io_capability import (
        interactive_pane_io_ready,
        stamp_io_capability,
    )
    from omg_cli.team.plane import load_team_meta, mutate_team_meta

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
    run_id = str(meta["run_id"])

    def _seed(current: dict) -> dict:
        current["identity_generation"] = 1
        row = current["tasks"][0]
        row["pane_id"] = "%99"
        row["pid"] = 999
        row["pid_start"] = "flipped"
        row["generation"] = 0
        row["attempt"] = 1
        stamp_io_capability(
            row,
            interactive_pane_io_ready(
                ready_marker=f"TUI_READY:{nonce}",
                pane_id="%7",
                provider_pid=111,
                attempt=1,
                generation=0,
            ),
        )
        return current

    mutate_team_meta(tmp_path, run_id, _seed)
    monkeypatch.setattr(
        "omg_cli.team.runtime._capture_interactive_pane",
        lambda pane_id, socket_path=None: "noise line\n" * 250,
    )
    out = runtime_mod.apply_interactive_worker_readiness(
        tmp_path,
        run_id,
        ["t1"],
        timeout_ms=200,
        env={EXPERIMENTAL_ENV: "1", "OMG_TEAM_READY_TIMEOUT_MS": "200"},
    )
    assert "t1" not in (out.get("startup_ready_workers") or [])
    disk = load_team_meta(tmp_path, run_id)
    ev = disk["tasks"][0].get("interaction_evidence") or {}
    assert ev.get("generation") == 0
    assert ev.get("pane_id") == "%7"


@_POSIX
def test_inbox_instruction_skips_same_inbox_and_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omg_cli.team.runtime as runtime_mod
    from omg_cli.team.plane import mutate_team_meta

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
    run_id = str(meta["run_id"])
    inbox_rel = "team/t1.a1.inbox.txt"

    def _seed(current: dict) -> dict:
        current["dry_run"] = False
        current["tasks"][0]["inbox_path"] = inbox_rel
        current["tasks"][0]["attempt"] = 1
        current["tasks"][0]["inbox_instruction_submitted"] = True
        current["tasks"][0]["inbox_instruction_inbox"] = inbox_rel
        current["tasks"][0]["inbox_instruction_attempt"] = 1
        return current

    mutate_team_meta(tmp_path, run_id, _seed)
    out = runtime_mod._submit_interactive_inbox_instructions(tmp_path, run_id, ["t1"])
    assert out == {"t1": True}


@_POSIX
def test_promote_lock_mismatch_omits_worker_from_stamped_and_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omg_cli.team.runtime as runtime_mod
    from omg_cli.team.plane import load_team_meta, mutate_team_meta
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
    nonce = str(meta["tasks"][0]["interactive_nonce"])
    run_id = str(meta["run_id"])

    def _pane(current: dict) -> dict:
        current["tasks"][0]["pane_id"] = "%7"
        current["tasks"][0]["pid"] = 111
        current["tasks"][0]["pid_start"] = "start-a"
        return current

    mutate_team_meta(tmp_path, run_id, _pane)

    real_filter = runtime_mod._filter_interactive_ready_evidence
    filter_calls = {"n": 0}

    def _filter_then_flip_on_promote(
        root: object, run_id_arg: str, evidence_by_id: object
    ) -> dict:
        proven = real_filter(root, run_id_arg, evidence_by_id)
        filter_calls["n"] += 1
        if filter_calls["n"] >= 2:

            def _flip(current: dict) -> dict:
                current["tasks"][0]["pane_id"] = "%99"
                current["tasks"][0]["pid"] = 999
                current["tasks"][0]["pid_start"] = "flipped"
                return current

            mutate_team_meta(tmp_path, run_id, _flip)
        return proven

    captured: dict[str, list[str]] = {}
    real_promote = runtime_mod._promote_interactive_input_ready

    def _capture_promote(*args: object, **kwargs: object) -> list[str]:
        stamped = real_promote(*args, **kwargs)
        captured["stamped"] = list(stamped)
        return stamped

    monkeypatch.setattr(
        runtime_mod, "_filter_interactive_ready_evidence", _filter_then_flip_on_promote
    )
    monkeypatch.setattr(runtime_mod, "_promote_interactive_input_ready", _capture_promote)
    monkeypatch.setattr(
        "omg_cli.team.runtime._capture_interactive_pane",
        lambda pane_id, socket_path=None: f"TUI_READY:{nonce}\n",
    )
    out = apply_start_readiness(
        tmp_path,
        {**meta, "dry_run": False},
        dry_run=False,
        env={"OMG_TEAM_READY_TIMEOUT_MS": "2000"},
    )
    assert captured.get("stamped") == []
    assert "t1" not in (out.get("startup_ready_workers") or [])
    assert "t1" in (out.get("startup_missing_workers") or [])
    assert out["startup_status"] == "failed_start"
    disk = load_team_meta(tmp_path, run_id)
    assert disk["tasks"][0]["input_ready"] is False
    assert disk["tasks"][0]["pane_id"] == "%99"


@_POSIX
def test_filter_ready_evidence_rejects_scaled_down_cleared_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Stale TUI-ready evidence must not promote a scaled-down pid=None row."""
    import omg_cli.team.runtime as runtime_mod
    from omg_cli.team.plane import load_team_meta, mutate_team_meta

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
    run_id = str(meta["run_id"])

    def _scaled_down(current: dict) -> dict:
        current["tasks"][0]["pane_id"] = "%7"
        current["tasks"][0]["pid"] = None
        current["tasks"][0]["pid_start"] = "start-a"
        return current

    mutate_team_meta(tmp_path, run_id, _scaled_down)
    evidence = {
        "t1": {
            "task_id": "t1",
            "ready_marker": f"TUI_READY:{nonce}",
            "pane_id": "%7",
            "provider_pid": 111,
            "pid_start": "start-a",
            "attempt": 1,
            "generation": 0,
        }
    }
    proven = runtime_mod._filter_interactive_ready_evidence(tmp_path, run_id, evidence)
    assert proven == {}
    stamped = runtime_mod._promote_interactive_input_ready(tmp_path, run_id, evidence)
    assert stamped == []
    disk = load_team_meta(tmp_path, run_id)
    assert disk["tasks"][0]["input_ready"] is False
    assert disk["tasks"][0]["pane_id"] == "%7"
    assert disk["tasks"][0]["pid"] is None


@_POSIX
def test_scale_up_refuses_mixed_interactive_headless(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.team.io_capability import (
        stamp_io_capability,
        supervisor_pane_io_defaults,
    )
    from omg_cli.team.plane import mutate_team_meta

    _enable(monkeypatch)
    _git_init(tmp_path)
    meta = start_team(
        "interactive fixture",
        [
            {"task_id": "t1", "title": "one", "owned_files": ["README.md"]},
            {"task_id": "t2", "title": "two", "owned_files": ["LICENSE"]},
        ],
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        executor="fixture",
        io_mode="interactive",
        env={EXPERIMENTAL_ENV: "1"},
    )

    def _mix(current: dict) -> dict:
        stamp_io_capability(current["tasks"][1], supervisor_pane_io_defaults())
        return current

    mutate_team_meta(tmp_path, str(meta["run_id"]), _mix)
    with pytest.raises(TeamError, match="mixed interactive/headless"):
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


def test_resume_for_identity_runs_interactive_readiness_after_relaunch() -> None:
    import inspect

    from omg_cli.team.runtime import resume_for_identity

    src = inspect.getsource(resume_for_identity)
    relaunch_at = src.index("relaunched")
    ready_at = src.index("apply_interactive_worker_readiness")
    assert relaunch_at < ready_at
    assert "IO_MODE_INTERACTIVE_TTY" in src
    assert "persist_startup_annotations" in src
    assert "startup_status" in src


def test_scale_up_refreshes_readiness_for_all_interactive_workers() -> None:
    import inspect

    from omg_cli.team.scaling import _scale_up

    src = inspect.getsource(_scale_up)
    assert "apply_interactive_worker_readiness" in src
    assert "updated.get(\"tasks\")" in src or "updated.get('tasks')" in src
    assert "IO_MODE_INTERACTIVE_TTY" in src
    assert "persist_startup_annotations" in src
    assert "startup_status" in src


def test_resolve_routing_from_meta_accepts_persisted_by_role() -> None:
    from omg_cli.team.routing import resolve_routing
    from omg_cli.team.scaling import _resolve_routing_from_meta

    snap = resolve_routing(
        {"executor": {"provider": "codex", "model": "m1"}},
        roles_needed=["executor"],
        check_binary=False,
        available_providers={"codex", "grok"},
    )
    persisted = snap.to_dict()
    assert "by_role" in persisted
    assert "default_provider" in persisted
    out = _resolve_routing_from_meta(
        {"multi_cli": True, "routing": persisted},
        ["executor"],
    )
    assert out is not None
    route = out.for_role("executor")
    assert route.provider == "codex"
    assert route.model == "m1"


def test_canonical_scale_specs_keep_assignment_fields() -> None:
    from omg_cli.team.scaling import (
        _canonical_scale_task_specs,
        _ownership_spec_view,
    )

    specs = _canonical_scale_task_specs(
        [
            {
                "task_id": "t1",
                "owned_files": ["README.md"],
                "role": "executor",
                "subject": "slice A",
                "depends_on": ["t0"],
            }
        ]
    )
    assert specs[0]["subject"] == "slice A"
    assert specs[0]["depends_on"] == ["t0"]
    view = _ownership_spec_view(specs[0])
    assert "subject" not in view
    assert "depends_on" not in view
    assert view["task_id"] == "t1"
    assert view["owned_files"] == ["README.md"]


def test_interactive_scale_refuses_unqualified_route() -> None:
    from omg_cli.team.routing import resolve_routing
    from omg_cli.team.scaling import TeamError, _interactive_scale_route_model

    snap = resolve_routing(
        {"executor": {"provider": "agy"}},
        roles_needed=["executor"],
        check_binary=False,
        available_providers={"agy", "grok"},
    )
    with pytest.raises(TeamError, match="interactive scale refused"):
        _interactive_scale_route_model(
            resolved=snap, multi_cli=True, role="executor"
        )


def test_scale_command_uses_startup_exit_gate() -> None:
    import inspect

    from omg_cli.commands.team import cmd_team

    src = inspect.getsource(cmd_team)
    scale_at = src.index('if action == "scale"')
    emit_at = src.index("_emit_startup_human", scale_at)
    resume_at = src.index('if action == "resume"', scale_at)
    assert scale_at < emit_at < resume_at
    resume_emit = src.index('_emit_startup_human(result, command="resume"')
    assert resume_at < resume_emit


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


def test_aarch64_stdin_wait_syscalls_use_generic_epoll_pwait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.team import interactive_wrapper as wrap

    monkeypatch.setattr(wrap, "_machine", lambda: "aarch64")
    wait_set = wrap._stdin_wait_syscalls()
    assert 22 in wait_set  # epoll_pwait
    assert 441 in wait_set  # epoll_pwait2
    assert 232 not in wait_set  # x86_64 epoll_wait == aarch64 mincore
    assert 281 not in wait_set  # x86_64 epoll_pwait == aarch64 execveat
    monkeypatch.setattr(wrap, "_machine", lambda: "x86_64")
    x86 = wrap._stdin_wait_syscalls()
    assert 232 in x86 and 281 in x86 and 441 in x86
    assert 22 not in x86


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


def test_reap_child_bounded_kills_after_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.team import interactive_wrapper as wrap

    if not hasattr(os, "WNOHANG"):
        pytest.skip("WNOHANG is POSIX")
    signals: list[int] = []

    def fake_kill(_pid: int, signum: int) -> None:
        signals.append(signum)

    waits = {"n": 0}

    def fake_waitpid(_pid: int, flags: int) -> tuple[int, int]:
        waits["n"] += 1
        if flags == os.WNOHANG and signal.SIGKILL not in signals:
            return (0, 0)
        if signal.SIGKILL in signals:
            return (77, 0)
        return (0, 0)

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(os, "killpg", fake_kill)
    monkeypatch.setattr(os, "waitpid", fake_waitpid)
    wrap._reap_child_bounded(77, grace_s=0.0)
    assert signal.SIGTERM in signals
    assert signal.SIGKILL in signals
    assert waits["n"] >= 2


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


def test_child_waiting_on_stdin_read_non_fd0_is_not_stdin_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team import interactive_wrapper as wrap

    syscall = tmp_path / "syscall"
    syscall.write_text(
        "0 0x3 0x7fff0000 0x400 0 0 0 0x7fff1000 0x401000\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(wrap, "_process_is_live", lambda _pid: True)
    monkeypatch.setattr(wrap, "_proc_fd0_target", lambda _pid: "/dev/pts/3")
    monkeypatch.setattr(wrap, "_iter_task_syscall_paths", lambda _pid: [syscall])
    monkeypatch.setattr(wrap, "_proc_state_sleeping", lambda _pid: True)
    monkeypatch.setattr(wrap, "_machine", lambda: "x86_64")
    monkeypatch.setattr(wrap, "_tty_in_raw_or_noncanonical", lambda: True)
    assert wrap.child_waiting_on_stdin(99, "/dev/pts/3") is False


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
