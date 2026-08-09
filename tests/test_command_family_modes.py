"""#29 Phase 2: modes + mcp families under commands/."""

from __future__ import annotations

from pathlib import Path

import pytest

from omg_cli.commands import mcp as mcp_cmds
from omg_cli.commands import modes as modes_cmds
from omg_cli.main import (
    build_parser,
    cmd_ask,
    cmd_autopilot,
    cmd_dual_review,
    cmd_mcp_install,
    cmd_mcp_server,
    cmd_mode,
    cmd_pipeline,
    cmd_qa,
    cmd_review,
)

pytest_plugins = ["tests.jobs_testutil"]


def test_main_reexports_modes_and_mcp() -> None:
    assert cmd_mode is modes_cmds.cmd_mode
    assert cmd_review is modes_cmds.cmd_review
    assert cmd_qa is modes_cmds.cmd_qa
    assert cmd_autopilot is modes_cmds.cmd_autopilot
    assert cmd_ask is modes_cmds.cmd_ask
    assert cmd_pipeline is modes_cmds.cmd_pipeline
    assert cmd_dual_review is modes_cmds.cmd_dual_review
    assert cmd_mcp_server is mcp_cmds.cmd_mcp_server
    assert cmd_mcp_install is mcp_cmds.cmd_mcp_install
    assert callable(modes_cmds.register_modes_parsers)
    assert callable(mcp_cmds.register_mcp_parsers)


def test_parser_wires_modes_and_mcp() -> None:
    parser = build_parser()
    samples = {
        "ulw": (["ulw", "goal text"], modes_cmds.cmd_mode),
        "ralph": (["ralph", "goal text"], modes_cmds.cmd_mode),
        "ralplan": (["ralplan", "goal text"], modes_cmds.cmd_mode),
        "review": (
            [
                "review",
                "--run",
                "r1",
                "--diff-text",
                "d",
                "--code-reviewer-json",
                "{}",
                "--architect-json",
                "{}",
            ],
            modes_cmds.cmd_review,
        ),
        "qa": (["qa", "status", "--run", "r1"], modes_cmds.cmd_qa),
        "autopilot": (["autopilot", "status", "--run", "r1"], modes_cmds.cmd_autopilot),
        "ask": (["ask", "codex", "hello"], modes_cmds.cmd_ask),
        "pipeline": (["pipeline", "goal"], modes_cmds.cmd_pipeline),
        "dual-review": (["dual-review", "goal"], modes_cmds.cmd_dual_review),
        "mcp-server": (["mcp-server"], mcp_cmds.cmd_mcp_server),
        "mcp-install": (["mcp-install", "--print-only"], mcp_cmds.cmd_mcp_install),
    }
    for name, (argv, expected) in samples.items():
        ns = parser.parse_args(argv)
        assert ns.func is expected, name
        assert ns.func.__module__.startswith("omg_cli.commands."), name


def test_modes_in_root_help() -> None:
    help_text = build_parser().format_help()
    for name in (
        "ulw",
        "ralph",
        "ralplan",
        "review",
        "qa",
        "autopilot",
        "ask",
        "pipeline",
        "dual-review",
        "mcp-server",
        "mcp-install",
    ):
        assert name in help_text


def test_ask_background_returns_job_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from omg_cli.main import main

    (tmp_path / ".omg").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path))
    rc = main(["--json", "ask", "fake", "background hello", "--background"])
    assert rc == 0
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is True
    assert body["command"] == "ask.background"
    assert body["job_id"]
    assert body["background"] is True
    assert body["provider"] == "fake"
    assert (tmp_path / ".omg" / "jobs" / body["job_id"] / "job.json").is_file()


def test_ask_background_preserves_sync_default(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    def _fake_run_ask_cli(provider: str, prompt: str, **kwargs: object) -> int:
        called.append(provider)
        return 0

    monkeypatch.setattr("omg_cli.ask.run_ask_cli", _fake_run_ask_cli)
    ns = build_parser().parse_args(["ask", "codex", "hello"])
    assert getattr(ns, "background", False) is False
    rc = ns.func(ns)
    assert rc == 0
    assert called == ["codex"]


def test_ask_background_rejects_non_job_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from omg_cli.main import main

    (tmp_path / ".omg").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path))
    rc = main(["--json", "ask", "codex", "nope", "--background"])
    assert rc == 2
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False
    assert body["error"]["code"] == "E_JOB_PROVIDER"
