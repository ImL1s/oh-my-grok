"""#29 Phase 2: modes + mcp families under commands/."""

from __future__ import annotations

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
