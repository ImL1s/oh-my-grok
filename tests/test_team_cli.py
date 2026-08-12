"""CLI grammar for OMX-like ``omg team N[:role] "<goal>"``."""

from __future__ import annotations

import pytest

from omg_cli.team.cli import (
    TeamCliError,
    normalize_team_argv,
    parse_worker_spec,
)


def test_parse_worker_spec_defaults_role() -> None:
    assert parse_worker_spec("3") == (3, "executor")
    assert parse_worker_spec("2:critic") == (2, "critic")


def test_parse_worker_spec_rejects_bad() -> None:
    with pytest.raises(TeamCliError):
        parse_worker_spec("0")
    with pytest.raises(TeamCliError):
        parse_worker_spec("3:not-a-role")


def test_normalize_shorthand_with_role() -> None:
    out = normalize_team_argv(["team", "3:executor", "fix flaky tests"])
    assert out == [
        "team",
        "launch",
        "--workers",
        "3",
        "--role",
        "executor",
        "--goal",
        "fix flaky tests",
    ]


def test_normalize_goal_only_defaults_three() -> None:
    out = normalize_team_argv(["team", "ship it"])
    assert out[:8] == [
        "team",
        "launch",
        "--workers",
        "3",
        "--role",
        "executor",
        "--goal",
        "ship it",
    ]


def test_normalize_preserves_legacy_and_shutdown_alias() -> None:
    assert normalize_team_argv(["team", "start", "--goal", "x"])[1] == "start"
    assert normalize_team_argv(["team", "worker-ready"])[1] == "worker-ready"
    assert normalize_team_argv(["team", "shutdown", "alpha"]) == [
        "team",
        "stop",
        "alpha",
    ]


def test_normalize_requires_goal_after_spec() -> None:
    with pytest.raises(TeamCliError):
        normalize_team_argv(["team", "3:executor"])


def test_normalize_after_supported_leading_globals() -> None:
    """Form A/B after --json/--safe/--yolo/--project-root PATH (arity-aware)."""
    form_a = [
        "team",
        "launch",
        "--workers",
        "3",
        "--role",
        "executor",
        "--goal",
        "fix flaky tests",
    ]
    assert (
        normalize_team_argv(["--json", "team", "3:executor", "fix flaky tests"])
        == ["--json", *form_a]
    )
    assert (
        normalize_team_argv(["--safe", "team", "3:executor", "fix flaky tests"])
        == ["--safe", *form_a]
    )
    assert (
        normalize_team_argv(["--yolo", "team", "3:executor", "fix flaky tests"])
        == ["--yolo", *form_a]
    )
    assert normalize_team_argv(
        ["--project-root", "/missing", "team", "3:executor", "fix flaky tests"]
    ) == ["--project-root", "/missing", *form_a]
    assert normalize_team_argv(
        ["--project-root=/missing", "team", "fix the flaky tests"]
    ) == [
        "--project-root=/missing",
        "team",
        "launch",
        "--workers",
        "3",
        "--role",
        "executor",
        "--goal",
        "fix the flaky tests",
    ]
    assert normalize_team_argv(
        ["--json", "--project-root", "/missing", "--yolo", "team", "fix it"]
    ) == [
        "--json",
        "--project-root",
        "/missing",
        "--yolo",
        "team",
        "launch",
        "--workers",
        "3",
        "--role",
        "executor",
        "--goal",
        "fix it",
    ]


def test_normalize_does_not_scan_payloads_or_unknown_flags() -> None:
    # --project-root consumes the next token even if it is the word "team".
    assert normalize_team_argv(
        ["--project-root", "team", "3:executor", "fix"]
    ) == ["--project-root", "team", "3:executor", "fix"]
    # Incomplete value option stays raw for argparse.
    assert normalize_team_argv(["--project-root"]) == ["--project-root"]
    # Unknown leading flags are not peeled.
    assert normalize_team_argv(
        ["--unknown", "team", "3:executor", "fix"]
    ) == ["--unknown", "team", "3:executor", "fix"]
    # Reserved action after globals is not rewritten (except shutdown).
    assert normalize_team_argv(["--json", "team", "start", "--goal", "x"]) == [
        "--json",
        "team",
        "start",
        "--goal",
        "x",
    ]
    assert normalize_team_argv(["--safe", "team", "shutdown", "alpha"]) == [
        "--safe",
        "team",
        "stop",
        "alpha",
    ]
