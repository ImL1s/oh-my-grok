"""#29 Phase 1: registry is source of truth for top-level inventory."""
from __future__ import annotations

from omg_cli.command_registry import COMMAND_SPECS, KNOWN_SUBCOMMANDS, command_names
from omg_cli.main import KNOWN_SUBCOMMANDS as MAIN_KNOWN, build_parser


def test_known_subcommands_match_parser_choices() -> None:
    parser = build_parser()
    choices: set[str] = set()
    for action in parser._actions:
        raw = getattr(action, "choices", None)
        if isinstance(raw, dict) and "setup" in raw and "doctor" in raw:
            choices = set(raw)
            break
    assert choices == set(KNOWN_SUBCOMMANDS)
    assert MAIN_KNOWN is KNOWN_SUBCOMMANDS


def test_command_specs_unique_and_cover_known() -> None:
    names = [s.name for s in COMMAND_SPECS]
    assert len(names) == len(set(names))
    assert set(names) == set(KNOWN_SUBCOMMANDS)
    assert command_names() == tuple(names)
    assert "install-hook" in KNOWN_SUBCOMMANDS
    assert "autopilot" in KNOWN_SUBCOMMANDS
