"""Install-family CLI handlers (#29 Phase 2).

Commands: setup, install-hook, doctor, update, uninstall.
Parser construction: ``register_install_parsers`` (#29 Phase 4').
"""

from __future__ import annotations

import argparse
import os
import sys

from omg_cli.cli_util import project_root


def cmd_setup(args: argparse.Namespace) -> int:
    from omg_cli import __version__
    from omg_cli.compat import format_isolation_banner
    from omg_cli.install_manifest import (
        InstallManifestError,
        plugin_root as install_plugin_root,
        refuse_home_project,
        run_scoped_setup,
    )

    runtime = getattr(args, "setup_runtime", None) or "grok"
    scope = getattr(args, "setup_scope", None) or "project"
    here = bool(getattr(args, "setup_here", False))
    force = bool(getattr(args, "force", False))
    install_rules = not bool(getattr(args, "no_global_rules", False))
    install_hook = not bool(getattr(args, "no_global_hook", False))
    try:
        if scope == "user":
            result = run_scoped_setup(
                runtime=runtime,
                scope="user",
                force=force,
                source_version=__version__,
            )
            print(f"oh-my-grok user-scope setup (runtime={runtime})")
            print(f"  manifest: {result.get('manifest')}")
            print(f"  written: {len(result.get('written') or [])} (not live-verified)")
            print("  did not create a project .omg")
            return 0
        root = project_root()
        refuse_home_project(root, here=here)
        grok_machine = runtime in {"grok", "both"}
        want_rules = install_rules if grok_machine else False
        want_hook = install_hook if grok_machine else False
        posix_hook_skipped = False
        if want_hook and os.name != "posix":
            want_hook = False
            posix_hook_skipped = True
        result = run_scoped_setup(
            runtime=runtime,
            scope="project",
            project_root=root,
            here=here,
            force=force,
            source_version=__version__,
            install_rules=want_rules,
            install_hook=want_hook,
        )
        print(f"oh-my-grok setup complete in {root}")
        print("  .omg/ dirs: ensured")
        for line in result.get("actions") or []:
            print(f"  {line}")
        print(f"  install manifest: {result.get('manifest')} (not live-verified)")
        if result.get("skipped"):
            print(f"  preserved foreign/user-owned: {len(result['skipped'])}")
        if grok_machine and not install_hook:
            print(
                "  global hook: skipped (--no-global-hook); doctor will report it missing"
            )
        elif posix_hook_skipped:
            print(
                "  global hook: skipped (POSIX PreToolUse wrapper; not installed on this OS)"
            )
        print()
        print("Install/refresh oh-my-grok with the checksum-verified release bootstrap:")
        print("  (performs the exact Grok plugin install and atomic CLI switch)")
        print()
        print(
            "  curl -fsSL https://raw.githubusercontent.com/ImL1s/oh-my-grok/main/"
            "scripts/install.sh | bash"
        )
        print()
        print("Manual/offline (already-downloaded release bytes; no pip/npm/network):")
        print(
            "  bash install.sh --offline --archive oh-my-grok-X.Y.Z.tar.gz "
            "--checksums SHA256SUMS"
        )
        print()
        print("Maintainers developing from a clean checkout can instead run:")
        print(f"  cd {install_plugin_root()} && bash scripts/install-plugin.sh")
        print()
        if grok_machine:
            print("Global guidance (~/.grok/rules/omg.md) is installed and loads every")
            print("Grok session (skip with: omg setup --no-global-rules).")
            print()
        print("Then verify:")
        print("  omg doctor")
        print()
        print(format_isolation_banner())
        return 0
    except InstallManifestError as exc:
        print(f"omg setup: {exc}", file=sys.stderr)
        return 2


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
        p_setup.add_argument(
            "--runtime",
            dest="setup_runtime",
            choices=("grok", "antigravity", "both"),
            default="grok",
            help="install runtime projection (default: grok; #77)",
        )
        p_setup.add_argument(
            "--scope",
            dest="setup_scope",
            choices=("project", "user"),
            default="project",
            help="project writes .omg; user writes ~/.omg-user (default: project)",
        )
        p_setup.add_argument(
            "--force",
            action="store_true",
            help="replace user-owned/foreign targets (default: preserve)",
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
