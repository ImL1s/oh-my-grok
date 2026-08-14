"""#131: agents family handlers live under omg_cli.commands.agents."""

from __future__ import annotations

from omg_cli.commands import agents as agents_cmds
from omg_cli.main import build_parser, cmd_agents


def test_main_reexports_agents_handler() -> None:
    assert cmd_agents is agents_cmds.cmd_agents
    assert agents_cmds.register_agents_parsers is not None


def test_parser_wires_agents_handler() -> None:
    parser = build_parser()
    ns = parser.parse_args(["agents", "list"])
    assert ns.func.__module__ == "omg_cli.commands.agents"
    ns = parser.parse_args(["agents", "explain", "omg-verifier"])
    assert ns.func.__module__ == "omg_cli.commands.agents"
