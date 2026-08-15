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
    assert "visual" in KNOWN_SUBCOMMANDS


def test_edit_is_registered_with_plan_apply() -> None:
    import argparse

    assert "edit" in KNOWN_SUBCOMMANDS
    spec = next(s for s in COMMAND_SPECS if s.name == "edit")
    assert spec.family == "inspect"
    parser = build_parser()
    choices: set[str] = set()
    for act in parser._actions:
        if isinstance(act, argparse._SubParsersAction) and "edit" in act.choices:
            for nested in act.choices["edit"]._actions:
                if isinstance(nested, argparse._SubParsersAction):
                    choices = set(nested.choices)
                    break
    assert choices == {"plan", "apply", "comments", "simplify"}


def test_job_help_mentions_auto_retry_pr5() -> None:
    from omg_cli.command_registry import COMMAND_SPECS

    job = next(s for s in COMMAND_SPECS if s.name == "job")
    assert "auto-retry" in job.help
    assert "PR5" in job.help or "PR1–PR5" in job.help
