"""#29 Phase 2: memory family handlers under omg_cli.commands.memory."""

from __future__ import annotations

from omg_cli.commands import memory as memory_cmds
from omg_cli.main import (
    build_parser,
    cmd_compact,
    cmd_memory,
    cmd_note,
    cmd_tracker,
)


MEMORY_CMDS = ("note", "memory", "tracker", "compact")


def test_main_reexports_memory_handlers() -> None:
    assert cmd_note is memory_cmds.cmd_note
    assert cmd_memory is memory_cmds.cmd_memory
    assert cmd_tracker is memory_cmds.cmd_tracker
    assert cmd_compact is memory_cmds.cmd_compact
    assert callable(memory_cmds.register_memory_parsers)


def test_parser_wires_memory_handlers() -> None:
    parser = build_parser()
    samples = {
        "note": ["note", "hello"],
        "memory": ["memory", "show"],
        "tracker": ["tracker", "status", "--run", "r1"],
        "compact": ["compact", "show", "x.json"],
    }
    for name in MEMORY_CMDS:
        ns = parser.parse_args(samples[name])
        assert callable(getattr(ns, "func", None))
        assert ns.func.__module__ == "omg_cli.commands.memory", name


def test_memory_help_lists_commands() -> None:
    help_text = build_parser().format_help()
    for name in MEMORY_CMDS:
        assert name in help_text
