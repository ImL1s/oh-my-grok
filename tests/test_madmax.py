"""Tests for omg --madmax host launcher (Fable contract + residual cleanup)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from omg_cli import madmax
from omg_cli.madmax import (
    GROK_OPEN_FLAGS,
    MadmaxUsageError,
    build_pane_command,
    cwd_digest,
    has_madmax_flag,
    is_print_mode,
    normalize_grok_args,
    run_madmax,
    session_name_for_cwd,
    strip_madmax_flags,
    tmux_env_args,
)
from omg_cli.main import KNOWN_SUBCOMMANDS, main


def test_has_madmax_flag():
    assert has_madmax_flag(["--madmax"])
    assert has_madmax_flag(["--madmax", "hi"])
    assert not has_madmax_flag(["--yolo", "hi"])
    assert not has_madmax_flag(["--safe", "ulw"])


def test_strip_madmax_flags():
    assert strip_madmax_flags(["--madmax", "fix it"]) == ["fix it"]


def test_normalize_injects_open_flags_once():
    out = normalize_grok_args(["--madmax", "ship it"])
    assert out.count("--always-approve") == 1
    assert out.count("bypassPermissions") == 1
    assert "--madmax" not in out
    assert "ship it" in out


def test_normalize_idempotent_if_already_open():
    base = list(GROK_OPEN_FLAGS) + ["hello"]
    out = normalize_grok_args(["--madmax", *base])
    assert out.count("--always-approve") == 1
    assert out.count("bypassPermissions") == 1


def test_normalize_dedupes_repeated_always_approve():
    out = normalize_grok_args(
        ["--madmax", "--always-approve", "--always-approve", "x"]
    )
    assert out.count("--always-approve") == 1
    assert "x" in out


def test_normalize_rejects_conflicting_permission_mode():
    with pytest.raises(MadmaxUsageError, match="bypassPermissions"):
        normalize_grok_args(["--madmax", "--permission-mode", "plan"])
    with pytest.raises(MadmaxUsageError, match="bypassPermissions"):
        normalize_grok_args(["--madmax", "--permission-mode=plan"])


def test_normalize_rejects_safe():
    with pytest.raises(MadmaxUsageError, match="--safe"):
        normalize_grok_args(["--madmax", "--safe"])


def test_normalize_strips_yolo():
    out = normalize_grok_args(["--madmax", "--yolo", "x"])
    assert "--yolo" not in out
    assert "x" in out


def test_session_name_unique_across_launches(tmp_path: Path):
    t1 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    a = session_name_for_cwd(tmp_path, now=t1, nonce="aaaa")
    b = session_name_for_cwd(tmp_path, now=t1, nonce="bbbb")
    assert a != b  # same second, different nonce
    assert a.startswith("omg-")
    assert cwd_digest(tmp_path) in a
    assert a.endswith("-aaaa")


def test_build_pane_command_login_shell_no_secret_export():
    cmd = build_pane_command(
        ["--always-approve", "hi"],
        shell="/bin/zsh",
        da1_drain=True,
    )
    assert "zsh" in cmd
    assert "-lc" in cmd
    assert "exec" in cmd
    assert "grok" in cmd
    assert "tcflush" in cmd
    # Secrets must NOT be in pane start command text.
    assert "XAI_API_KEY" not in cmd
    assert "export " not in cmd


def test_tmux_env_args_for_secrets():
    args = tmux_env_args([("XAI_API_KEY", "sekrit"), ("GROK_SANDBOX", "off")])
    assert args == [
        "-e",
        "XAI_API_KEY=sekrit",
        "-e",
        "GROK_SANDBOX=off",
    ]


def test_tmux_env_args_skips_bad_keys():
    assert tmux_env_args([("BAD=KEY", "v"), ("OK", "v")]) == ["-e", "OK=v"]


def test_is_print_mode_prompt_json_eq_variant():
    assert is_print_mode(["--prompt-json={}", "x"])
    assert is_print_mode(['--prompt-json={"a":1}'])
    assert is_print_mode(["--version"])
    assert not is_print_mode(["interactive prompt"])


def test_dispatch_print_uses_direct(monkeypatch, tmp_path: Path):
    calls: list[str] = []

    def fake_direct(cwd, args):
        calls.append("direct")
        return 0

    def fake_tmux(cwd, args):
        calls.append("tmux")
        return 0

    monkeypatch.setattr(madmax, "_run_grok_direct", fake_direct)
    monkeypatch.setattr(madmax, "_run_grok_in_tmux", fake_tmux)
    monkeypatch.setattr(madmax, "grok_available", lambda: True)
    monkeypatch.delenv("TMUX", raising=False)
    rc = run_madmax(tmp_path, ["--madmax", "-p", "hi"])
    assert rc == 0
    assert calls == ["direct"]


def test_dispatch_inside_tmux_uses_direct(monkeypatch, tmp_path: Path):
    calls: list[str] = []
    monkeypatch.setattr(
        madmax, "_run_grok_direct", lambda c, a: calls.append("direct") or 0
    )
    monkeypatch.setattr(
        madmax, "_run_grok_in_tmux", lambda c, a: calls.append("tmux") or 0
    )
    monkeypatch.setattr(madmax, "grok_available", lambda: True)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1")
    rc = run_madmax(tmp_path, ["--madmax"])
    assert rc == 0
    assert calls == ["direct"]


def test_dispatch_outside_uses_tmux(monkeypatch, tmp_path: Path):
    calls: list[str] = []
    monkeypatch.setattr(
        madmax, "_run_grok_direct", lambda c, a: calls.append("direct") or 0
    )
    monkeypatch.setattr(
        madmax, "_run_grok_in_tmux", lambda c, a: calls.append("tmux") or 0
    )
    monkeypatch.setattr(madmax, "grok_available", lambda: True)
    monkeypatch.delenv("TMUX", raising=False)
    rc = run_madmax(tmp_path, ["--madmax"])
    assert rc == 0
    assert calls == ["tmux"]


def test_dispatch_missing_grok(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(madmax, "grok_available", lambda: False)
    rc = run_madmax(tmp_path, ["--madmax"])
    assert rc == 127


def test_dispatch_usage_error(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(madmax, "grok_available", lambda: True)
    rc = run_madmax(tmp_path, ["--madmax", "--safe"])
    assert rc == 2


def test_madmax_module_never_imports_state_or_acceptance():
    import omg_cli.madmax as m

    src = Path(m.__file__).read_text(encoding="utf-8")
    assert "omg_cli.state" not in src
    assert "omg_cli.acceptance" not in src
    assert "write_status" not in src


def test_known_subcommands_cover_router_set():
    assert "ulw" in KNOWN_SUBCOMMANDS
    assert "ask" in KNOWN_SUBCOMMANDS
    assert "ralph" in KNOWN_SUBCOMMANDS


def test_main_madmax_intercept_dispatches(monkeypatch, tmp_path: Path):
    seen: list[list[str]] = []

    def fake_run(cwd, argv):
        seen.append(list(argv))
        return 0

    monkeypatch.setattr("omg_cli.madmax.run_madmax", fake_run)
    monkeypatch.chdir(tmp_path)
    rc = main(["--madmax", "hello"])
    assert rc == 0
    assert seen and "--madmax" in seen[0]


def test_main_bare_interactive_intercept(monkeypatch, tmp_path: Path):
    seen: list[list[str]] = []

    def fake_run(cwd, argv):
        seen.append(list(argv))
        return 0

    monkeypatch.setattr("omg_cli.madmax.run_interactive", fake_run)
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0
    assert seen == [[]]
    seen.clear()
    assert main(["fix the bug"]) == 0
    assert seen == [["fix the bug"]]


def test_should_host_launch_matrix():
    from omg_cli.madmax import should_host_launch

    assert should_host_launch([], KNOWN_SUBCOMMANDS) is True
    assert should_host_launch(["fix it"], KNOWN_SUBCOMMANDS) is True
    assert should_host_launch(["doctor"], KNOWN_SUBCOMMANDS) is False
    assert should_host_launch(["--help"], KNOWN_SUBCOMMANDS) is False
    assert should_host_launch(["--madmax"], KNOWN_SUBCOMMANDS) is False
    assert should_host_launch(["--safe"], KNOWN_SUBCOMMANDS) is False


def test_subcommand_before_madmax_exits_2(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    for sub in ("ulw", "ask", "ralph"):
        called = []

        def boom(*a, **k):
            called.append(1)
            return 0

        monkeypatch.setattr("omg_cli.madmax.run_madmax", boom)
        rc = main([sub, "goal", "--madmax"])
        assert rc == 2, sub
        assert called == []


def test_madmax_before_prompt_token_intercepts(monkeypatch, tmp_path: Path):
    """omg --madmax ralph is allowed: ralph is prompt text, not subcommand first."""
    seen = []

    def fake_run(cwd, argv):
        seen.append(argv)
        return 0

    monkeypatch.setattr("omg_cli.madmax.run_madmax", fake_run)
    monkeypatch.chdir(tmp_path)
    rc = main(["--madmax", "ralph"])
    assert rc == 0
    assert seen


def test_run_grok_in_tmux_passes_e_flags(monkeypatch, tmp_path: Path):
    """Integration: new-session argv includes -e and no secret in pane string."""
    captured: list[list[str]] = []

    def fake_run(cmd, check=False, capture_output=False, text=False):
        captured.append(list(cmd))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(madmax, "tmux_available", lambda: True)
    monkeypatch.setattr(madmax, "_list_previous_sessions", lambda d: [])
    monkeypatch.setattr(madmax, "session_name_for_cwd", lambda c: "omg-test-sess")
    monkeypatch.setattr(
        madmax,
        "forwarded_env",
        lambda: [("XAI_API_KEY", "sekrit"), ("PATH", "/bin")],
    )
    monkeypatch.setattr(madmax.subprocess, "run", fake_run)

    rc = madmax._run_grok_in_tmux(tmp_path, ["--always-approve", "hi"])
    assert rc == 0
    create = next(c for c in captured if c[:2] == ["tmux", "new-session"])
    assert "-e" in create
    assert "XAI_API_KEY=sekrit" in create
    # pane command is last arg and must not contain secret
    assert "sekrit" not in create[-1]
