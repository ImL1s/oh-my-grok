"""#29 Phase 2: run family handlers live under omg_cli.commands.run."""

from __future__ import annotations

from omg_cli.commands import run as run_cmds
from omg_cli.main import (
    _print_state_human,
    build_parser,
    cmd_cancel,
    cmd_recover,
    cmd_resume,
    cmd_session,
    cmd_state,
)


RUN_CMDS = ("state", "cancel", "resume", "session", "recover")


def test_main_reexports_run_handlers() -> None:
    assert cmd_state is run_cmds.cmd_state
    assert cmd_cancel is run_cmds.cmd_cancel
    assert cmd_resume is run_cmds.cmd_resume
    assert cmd_session is run_cmds.cmd_session
    assert cmd_recover is run_cmds.cmd_recover
    assert _print_state_human is run_cmds._print_state_human


def test_parser_wires_run_handlers() -> None:
    parser = build_parser()
    samples = {
        "state": ["state"],
        "cancel": ["cancel"],
        "resume": ["resume", "--no-write"],
        "session": ["session", "allocate"],
        "recover": ["recover", "/tmp/x"],
    }
    for name in RUN_CMDS:
        ns = parser.parse_args(samples[name])
        assert callable(getattr(ns, "func", None))
        assert ns.func.__module__ == "omg_cli.commands.run", name


def test_run_help_lists_commands() -> None:
    help_text = build_parser().format_help()
    for name in RUN_CMDS:
        assert name in help_text
