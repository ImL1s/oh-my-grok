"""Host launcher grammar/policy unit tests (Sol GRAM/POL)."""
from __future__ import annotations

import pytest

from omg_cli.host_launcher import (
    HostLaunchUsageError,
    resolve_launch_policy,
    should_host_launch,
    split_at_end_of_options,
)
from omg_cli.main import KNOWN_SUBCOMMANDS, main
from omg_cli.madmax import normalize_grok_args


def test_split_at_end_of_options_keeps_suffix_opaque():
    head, suffix = split_at_end_of_options(["--madmax", "--", "--safe", "x"])
    assert head == ["--madmax"]
    assert suffix == ["--", "--safe", "x"]


def test_resolve_policy_last_flag_and_env(monkeypatch):
    monkeypatch.delenv("OMG_LAUNCH_POLICY", raising=False)
    assert resolve_launch_policy(["--tmux", "--direct", "hi"])[0] == "direct"
    assert resolve_launch_policy(["hi"], env={"OMG_LAUNCH_POLICY": "tmux"})[0] == "tmux"
    assert resolve_launch_policy(["--direct"], env={"OMG_LAUNCH_POLICY": "tmux"})[0] == "direct"
    with pytest.raises(HostLaunchUsageError):
        resolve_launch_policy([], env={"OMG_LAUNCH_POLICY": "nope"})


def test_should_host_launch_matrix():
    assert should_host_launch([], KNOWN_SUBCOMMANDS) is True
    assert should_host_launch(["fix it"], KNOWN_SUBCOMMANDS) is True
    assert should_host_launch(["doctor"], KNOWN_SUBCOMMANDS) is False
    assert should_host_launch(["--help"], KNOWN_SUBCOMMANDS) is False
    assert should_host_launch(["--madmax"], KNOWN_SUBCOMMANDS) is False
    assert should_host_launch(["--direct"], KNOWN_SUBCOMMANDS) is True


def test_madmax_suffix_opacity_does_not_scan_safe():
    # `--safe` after `--` must not trip madmax normalizer.
    out = normalize_grok_args(["--madmax"])
    assert "--always-approve" in out
    head, suffix = split_at_end_of_options(["--madmax", "--", "--safe"])
    assert "--safe" in suffix
    normalized = normalize_grok_args(head) + suffix
    assert "--safe" in normalized


def test_reject_launcher_flags_after_subcommand():
    from omg_cli.host_launcher import reject_launcher_flags_after_subcommand

    with pytest.raises(HostLaunchUsageError, match="E_LAUNCH_USAGE"):
        reject_launcher_flags_after_subcommand(["doctor", "--direct"], KNOWN_SUBCOMMANDS)
    with pytest.raises(HostLaunchUsageError, match="E_LAUNCH_USAGE"):
        reject_launcher_flags_after_subcommand(["ralph", "--tmux"], KNOWN_SUBCOMMANDS)
    # Suffix opacity: launcher flag after `--` is not a grammar error here.
    reject_launcher_flags_after_subcommand(["doctor", "--", "--direct"], KNOWN_SUBCOMMANDS)


def test_suffix_only_madmax_does_not_hijack_subcommand(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    def fake_i(cwd, argv):
        seen.append(list(argv))
        return 0

    monkeypatch.setattr("omg_cli.host_launcher.run_interactive", fake_i)
    monkeypatch.setattr("omg_cli.host_launcher.run_madmax_host", lambda *a, **k: 99)
    monkeypatch.chdir(tmp_path)
    from omg_cli.madmax import has_madmax_flag

    assert has_madmax_flag(["doctor", "--", "--madmax"]) is False
    assert has_madmax_flag(["--madmax", "--", "x"]) is True


def test_main_dispatches_interactive_and_madmax(monkeypatch, tmp_path):
    seen_i: list[list[str]] = []
    seen_m: list[list[str]] = []

    def fake_i(cwd, argv):
        seen_i.append(list(argv))
        return 0

    def fake_m(cwd, argv):
        seen_m.append(list(argv))
        return 0

    monkeypatch.setattr("omg_cli.host_launcher.run_interactive", fake_i)
    monkeypatch.setattr("omg_cli.host_launcher.run_madmax_host", fake_m)
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0
    assert seen_i == [[]]
    assert main(["--madmax", "hi"]) == 0
    assert seen_m and "--madmax" in seen_m[0]

