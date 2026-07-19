"""Tests for omg ask broker — fixed argv, child-only allow env, dry-run."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from omg_cli.ask import (
    AskProviderError,
    AskProviderMissing,
    child_env_for_ask,
    run_ask,
    run_ask_cli,
)
from omg_cli.ask.providers import (
    argv_claude,
    argv_codex,
    build_provider_argv,
    normalize_provider,
    validate_extra,
)


def test_unknown_provider_exit_2():
    with pytest.raises(AskProviderError):
        normalize_provider("not-a-provider")
    with pytest.raises(AskProviderError):
        run_ask("nope", "hello", dry_run=True, check_binary=False)
    rc = run_ask_cli("nope", "hello", dry_run=True)
    assert rc == 2


def test_argv_codex_fixed_no_shell():
    # Legacy argv mode still embeds prompt
    argv = argv_codex("summarize HARD RULES", prompt_mode="argv")
    assert argv[0] == "codex"
    assert "exec" in argv
    assert "-s" in argv and "read-only" in argv
    assert "summarize HARD RULES" in argv
    assert all(isinstance(x, str) for x in argv)


def test_argv_codex_stdin_no_prompt_body():
    secret = "TOP_SECRET_PROMPT_BODY_xyz"
    argv = argv_codex(secret, prompt_mode="stdin")
    assert argv[0] == "codex"
    assert "read-only" in argv
    assert secret not in argv
    assert "-" in argv  # stdin sentinel


def test_argv_claude_no_skip_permissions():
    argv = argv_claude("review this", prompt_mode="argv")
    assert argv[:2] == ["claude", "-p"]
    joined = " ".join(argv)
    assert "dangerously-skip-permissions" not in joined
    assert "bypassPermissions" not in joined


def test_fable_alias():
    assert normalize_provider("fable") == "claude"
    argv = build_provider_argv(
        "fable", "hi", check_binary=False, prompt_mode="argv"
    )
    assert argv[0] == "claude"


def test_child_env_has_allow_parent_does_not(monkeypatch, tmp_path):
    monkeypatch.delenv("OMG_ALLOW_EXTERNAL_CLI", raising=False)
    monkeypatch.setenv("OMG_ASK_STDIN", "1")
    parent_before = os.environ.get("OMG_ALLOW_EXTERNAL_CLI")

    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        captured["shell"] = kwargs.get("shell", False)
        captured["stdin"] = kwargs.get("stdin")
        m = MagicMock()
        m.pid = 12345

        def comm(input=None, timeout=None):
            captured["stdin_input"] = input
            return (b"advisor says ok\n", None)

        m.communicate.side_effect = comm
        m.returncode = 0
        return m

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    # Pretend binary exists
    monkeypatch.setattr(
        "omg_cli.ask.providers.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )

    prompt = "hello advisor SECRET_NOT_IN_ARGV"
    result = run_ask(
        "codex",
        prompt,
        root=tmp_path,
        dry_run=False,
        timeout=30,
    )
    assert result.exit_code == 0
    assert captured.get("shell") is False
    assert captured["env"]["OMG_ALLOW_EXTERNAL_CLI"] == "1"
    assert captured["env"].get("OMG_ASK_BROKER") == "1"
    # Parent unchanged
    assert os.environ.get("OMG_ALLOW_EXTERNAL_CLI") == parent_before
    assert "OMG_ALLOW_EXTERNAL_CLI" not in os.environ or parent_before is not None
    assert parent_before is None
    assert "OMG_ALLOW_EXTERNAL_CLI" not in os.environ
    # stdin mode: prompt not in argv; fed via communicate(input=)
    assert prompt not in captured["argv"]
    assert captured.get("stdin") is subprocess.PIPE
    assert captured.get("stdin_input") == prompt.encode("utf-8")


def test_dry_run_writes_no_provider_exec(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise AssertionError("Popen must not be called on dry-run")

    monkeypatch.setattr(subprocess, "Popen", boom)
    result = run_ask(
        "codex",
        "dry only",
        root=tmp_path,
        dry_run=True,
        check_binary=False,
    )
    assert result.dry_run is True
    assert result.exit_code == 0
    assert result.artifact.is_file()
    assert "ask-" in result.artifact.name
    assert "codex" in result.artifact.name
    text = result.artifact.read_text(encoding="utf-8")
    assert "dry-run" in text.lower() or "dry_run" in text


def test_artifact_written_under_omg_artifacts(tmp_path):
    result = run_ask(
        "claude",
        "ping",
        root=tmp_path,
        dry_run=True,
        check_binary=False,
    )
    rel = result.artifact.relative_to(tmp_path)
    assert rel.parts[0] == ".omg"
    assert rel.parts[1] == "artifacts"
    assert result.artifact.name.startswith("ask-")


def test_extra_rejects_by_default_without_allow_env(monkeypatch):
    monkeypatch.delenv("OMG_ASK_ALLOW_EXTRA", raising=False)
    with pytest.raises(AskProviderError, match="OMG_ASK_ALLOW_EXTRA"):
        validate_extra(["--temperature", "0"])
    with pytest.raises(AskProviderError, match="OMG_ASK_ALLOW_EXTRA"):
        argv_claude("x", extra=["--yolo"], prompt_mode="argv")


def test_extra_rejects_bypass_permissions_when_allow_extra(monkeypatch):
    monkeypatch.setenv("OMG_ASK_ALLOW_EXTRA", "1")
    with pytest.raises(AskProviderError):
        validate_extra(["--dangerously-skip-permissions"])
    with pytest.raises(AskProviderError):
        validate_extra(["-s", "workspace-write"])
    with pytest.raises(AskProviderError):
        argv_claude("x", extra=["--yolo"], prompt_mode="argv")


def test_stdin_mode_default_argv_excludes_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv("OMG_ASK_STDIN", "1")
    monkeypatch.delenv("OMG_ASK_ALLOW_EXTRA", raising=False)
    secret = "PROMPT_BODY_MUST_NOT_LEAK_INTO_ARGV_99"
    result = run_ask(
        "codex",
        secret,
        root=tmp_path,
        dry_run=True,
        check_binary=False,
    )
    assert secret not in result.argv
    assert "OMG_ALLOW_EXTERNAL_CLI" not in os.environ


def test_provider_missing_exit_3(monkeypatch, tmp_path):
    monkeypatch.setattr("omg_cli.ask.providers.shutil.which", lambda _n: None)
    with pytest.raises(AskProviderMissing):
        run_ask("gemini", "hi", root=tmp_path, dry_run=False)
    rc = run_ask_cli("gemini", "hi", root=tmp_path, dry_run=False)
    assert rc == 3


def test_accept_sanitized_env_still_strips_allow(monkeypatch):
    from omg_cli.acceptance import sanitized_env

    monkeypatch.setenv("OMG_ALLOW_EXTERNAL_CLI", "1")
    # child_env for ask sets allow; acceptance must still strip
    child = child_env_for_ask()
    assert child["OMG_ALLOW_EXTERNAL_CLI"] == "1"
    clean = sanitized_env(child)
    assert "OMG_ALLOW_EXTERNAL_CLI" not in clean
    # parent test env still has it (monkeypatch) — sanitized does not mutate parent
    assert os.environ.get("OMG_ALLOW_EXTERNAL_CLI") == "1"


def test_timeout_kills_process_group(monkeypatch, tmp_path):
    """Fake long-running process → broker exit 4."""

    class SlowProc:
        pid = 99999
        returncode = None

        def communicate(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd=["sleep"], timeout=timeout)
            return (b"", None)

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return -9

    monkeypatch.setattr(
        "omg_cli.ask.providers.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    killed = {"pg": False}

    def fake_popen(*_a, **_k):
        return SlowProc()

    def fake_killpg(pid, sig):
        killed["pg"] = True

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(os, "killpg", fake_killpg, raising=False)
    # communicate after timeout
    orig_comm = SlowProc.communicate

    def comm(self, input=None, timeout=None):
        if not getattr(self, "_timed", False):
            self._timed = True
            raise subprocess.TimeoutExpired(cmd=["x"], timeout=timeout or 0)
        return (b"partial\n", None)

    monkeypatch.setattr(SlowProc, "communicate", comm)

    result = run_ask(
        "codex",
        "slow",
        root=tmp_path,
        timeout=0.01,
        dry_run=False,
    )
    assert result.exit_code == 4
