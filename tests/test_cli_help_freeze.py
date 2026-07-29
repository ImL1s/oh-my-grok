"""#29 Phase 0′: top-level help inventory freeze (lightweight).

Full nested golden fixtures can grow later; this locks root command list
against command_registry so host-launch + help cannot drift.
"""

from __future__ import annotations

from omg_cli.command_registry import KNOWN_SUBCOMMANDS, command_names
from omg_cli.main import build_parser


def test_root_help_lists_every_known_subcommand() -> None:
    help_text = build_parser().format_help()
    for name in command_names():
        assert name in help_text, f"missing from --help: {name}"


def test_root_help_subcommand_set_matches_registry() -> None:
    parser = build_parser()
    choices: set[str] = set()
    for action in parser._actions:
        raw = getattr(action, "choices", None)
        if isinstance(raw, dict) and "setup" in raw and "doctor" in raw:
            choices = set(raw)
            break
    assert choices == set(KNOWN_SUBCOMMANDS)
