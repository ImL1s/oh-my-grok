"""Install-family CLI handlers (#29 Phase 2).

Commands: setup, install-hook, doctor, update, uninstall.
Parser construction: ``register_install_parsers`` (#29 Phase 4').
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
    from omg_cli.cli_envelope import wants_json
    from omg_cli.doctor import run_doctor

    return run_doctor(
        strict=bool(getattr(args, "strict", False)),
        project_root=project_root(),
        json_output=wants_json(args),
    )


def cmd_update(args: argparse.Namespace) -> int:
    from omg_cli.update_cmd import run_update

    return run_update()


def cmd_uninstall(args: argparse.Namespace) -> int:
    from omg_cli.uninstall_cmd import run_uninstall

    return run_uninstall(yes=bool(getattr(args, "yes", False)))



def register_install_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
    *,
    phase: str = "all",
) -> None:
    """Register install-family argparse parsers (#29 Phase 4').

    ``phase``:
      - ``early``: setup, install-hook, doctor (before note)
      - ``late``: update, uninstall (after note)
      - ``all``: both
    """
    if phase not in {"early", "late", "all"}:
        raise ValueError(f"unknown install register phase: {phase!r}")
    if phase in ("early", "all"):
        p_setup = sub.add_parser(
            "setup",
            parents=[common],
            help="ensure .omg dirs, merge AGENTS + gitignore",
        )
        p_setup.add_argument(
            "--no-global-rules",
            action="store_true",
            help="do not install ~/.grok/rules/omg.md global guidance",
        )
        p_setup.add_argument(
            "--no-global-hook",
            action="store_true",
            help="do not install the global PreToolUse soft-gate ($GROK_HOME/hooks/); "
            "doctor will still report it missing",
        )
        p_setup.add_argument(
            "--here",
            dest="setup_here",
            action="store_true",
            help=(
                "initialize .omg in the exact current directory (skip git/.omg "
                "discovery; #22)"
            ),
        )
        p_setup.set_defaults(func=cmd_setup)

        p_install_hook = sub.add_parser(
            "install-hook",
            parents=[common],
            help="install/repair the global PreToolUse soft-gate ($GROK_HOME/hooks/)",
        )
        p_install_hook.add_argument(
            "--remove",
            action="store_true",
            help="uninstall the global hook instead of installing it",
        )
        p_install_hook.set_defaults(func=cmd_install_hook)

        p_doctor = sub.add_parser(
            "doctor",
            parents=[common],
            help="check plugin + environment health",
        )
        p_doctor.add_argument(
            "--strict",
            action="store_true",
            help="treat compat.claude isolation risks as FAIL (exit 1)",
        )
        p_doctor.set_defaults(func=cmd_doctor)

    if phase in ("late", "all"):
        p_update = sub.add_parser(
            "update",
            parents=[common],
            help="upgrade a verified managed install or safely refresh its proven source checkout",
        )
        p_update.set_defaults(func=cmd_update)

        p_uninstall = sub.add_parser(
            "uninstall",
            parents=[common],
            help="remove plugin, global hook, and OMG rules block",
        )
        p_uninstall.add_argument(
            "--yes",
            action="store_true",
            help="actually perform removal",
        )
        p_uninstall.set_defaults(func=cmd_uninstall)


__all__ = [
    "register_install_parsers",
    "cmd_doctor",
    "cmd_install_hook",
    "cmd_setup",
    "cmd_uninstall",
    "cmd_update",
]
