"""#29 Phase 2: install family handlers live under omg_cli.commands.install."""

from __future__ import annotations

from omg_cli.commands import install as install_cmds
from omg_cli.main import (
    build_parser,
    cmd_doctor,
    cmd_install_hook,
    cmd_setup,
    cmd_setup_import,
    cmd_setup_migrate,
    cmd_uninstall,
    cmd_update,
)


INSTALL_CMDS = (
    "setup",
    "install-hook",
    "doctor",
    "update",
    "uninstall",
)


def test_main_reexports_install_handlers() -> None:
    assert cmd_setup is install_cmds.cmd_setup
    assert cmd_setup_import is install_cmds.cmd_setup_import
    assert cmd_setup_migrate is install_cmds.cmd_setup_migrate
    assert cmd_install_hook is install_cmds.cmd_install_hook
    assert cmd_doctor is install_cmds.cmd_doctor
    assert cmd_update is install_cmds.cmd_update
    assert cmd_uninstall is install_cmds.cmd_uninstall
    assert callable(install_cmds.register_install_parsers)


def test_parser_wires_install_handlers() -> None:
    parser = build_parser()
    samples = {
        "setup": ["setup"],
        "install-hook": ["install-hook"],
        "doctor": ["doctor"],
        "update": ["update"],
        "uninstall": ["uninstall", "--yes"],
    }
    for name in INSTALL_CMDS:
        ns = parser.parse_args(samples[name])
        assert callable(getattr(ns, "func", None))
        assert ns.func.__module__ == "omg_cli.commands.install", name
    imported = parser.parse_args(["setup", "import", "--from", "SKILL.md"])
    assert imported.func is install_cmds.cmd_setup_import
    migrated = parser.parse_args(["setup", "migrate", "--from", "."])
    assert migrated.func is install_cmds.cmd_setup_migrate


def test_install_help_lists_commands() -> None:
    help_text = build_parser().format_help()
    for name in INSTALL_CMDS:
        assert name in help_text


def test_install_hook_still_in_known_subcommands() -> None:
    """#18 regression: install-hook must never leave host-launch recognition."""
    from omg_cli.command_registry import KNOWN_SUBCOMMANDS

    assert "install-hook" in KNOWN_SUBCOMMANDS
