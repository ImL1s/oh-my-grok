"""Install-family CLI handlers (#29 Phase 2).

Commands: setup, install-hook, doctor, update, uninstall.
Parser construction remains in ``main.build_parser``.
"""

from __future__ import annotations

import argparse

from omg_cli.cli_util import project_root


def cmd_setup(args: argparse.Namespace) -> int:
    from omg_cli.setup_cmd import run_setup

    return run_setup(
        project_root(),
        install_rules=not getattr(args, "no_global_rules", False),
        install_hook=not getattr(args, "no_global_hook", False),
    )


def cmd_install_hook(args: argparse.Namespace) -> int:
    from omg_cli.hook_install import main as hook_install_main

    return hook_install_main(["--remove"] if getattr(args, "remove", False) else [])


def cmd_doctor(args: argparse.Namespace) -> int:
    from omg_cli.doctor import run_doctor

    return run_doctor(
        strict=bool(getattr(args, "strict", False)),
        project_root=project_root(),
    )


def cmd_update(args: argparse.Namespace) -> int:
    from omg_cli.update_cmd import run_update

    return run_update()


def cmd_uninstall(args: argparse.Namespace) -> int:
    from omg_cli.uninstall_cmd import run_uninstall

    return run_uninstall(yes=bool(getattr(args, "yes", False)))


__all__ = [
    "cmd_doctor",
    "cmd_install_hook",
    "cmd_setup",
    "cmd_uninstall",
    "cmd_update",
]
