"""omg uninstall — remove plugin, global hook, OMG rules block (never project .omg/)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _checkout_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _grok_home(home: Path | None) -> Path:
    if home is not None:
        return Path(home)
    raw = os.environ.get("GROK_HOME")
    if raw is not None and raw.strip() != "":
        return Path(raw)
    return Path.home() / ".grok"


def run_uninstall(
    *,
    yes: bool = False,
    runner=subprocess.run,
    home: Path | None = None,
) -> int:
    """Remove OMG install surfaces. Requires --yes to mutate.

    Never removes project ``.omg/`` state. Never deletes USER:OMG:POLICY blocks
    (guidance.uninstall_global_rules preserves non-OMG content).
    """
    gh = _grok_home(home)
    hook = gh / "hooks" / "omg-pretool-deny.json"
    rules = gh / "rules" / "omg.md"
    link = Path.home() / ".local" / "bin" / "omg"
    checkout = _checkout_root()

    if not yes:
        print("omg uninstall: dry run (no changes). Would remove:")
        print(f"  - grok plugin uninstall oh-my-grok --confirm")
        print(f"  - global hook json (if present): {hook}")
        print(f"  - global hook standalone (if present): {hook.with_name('omg_pretool_deny_standalone.py')}")
        print(f"  - OMG managed block in rules (if present): {rules}")
        print(
            f"  - ~/.local/bin/omg only if it is a symlink into this checkout "
            f"({checkout})"
        )
        print("  - project .omg/ state: NOT removed (intentionally left untouched)")
        print("re-run with --yes to actually perform removal")
        return 0

    # 1. grok plugin uninstall (best-effort)
    try:
        result = runner(
            ["grok", "plugin", "uninstall", "oh-my-grok", "--confirm"],
            capture_output=True,
            text=True,
        )
        print(
            "omg uninstall: grok plugin uninstall oh-my-grok "
            f"(rc={getattr(result, 'returncode', '?')})"
        )
    except OSError as exc:
        print(f"omg uninstall: grok plugin uninstall skipped: {exc}")

    # 2. remove global hook (json FIRST, then standalone .py — never leave an
    #    active json pointing at a missing script). Shared with the installer.
    try:
        from omg_cli.hook_install import remove_global_hook

        removed = remove_global_hook(home=gh)
        if removed:
            for r in removed:
                print(f"omg uninstall: removed {r}")
        else:
            print(f"omg uninstall: global hook absent ({hook})")
    except Exception as exc:  # noqa: BLE001 — best-effort, never crash uninstall
        print(f"omg uninstall: could not remove global hook: {exc}", file=sys.stderr)

    # 3. strip OMG managed rules block (preserve USER policy / foreign content)
    try:
        from omg_cli.guidance import GuidanceCorruptionError, uninstall_global_rules

        path, action = uninstall_global_rules(home=gh)
        print(f"omg uninstall: rules {path} -> {action}")
    except GuidanceCorruptionError as exc:
        print(
            f"omg uninstall: rules file corrupt, left untouched: {exc}",
            file=sys.stderr,
        )

    # 4. remove ~/.local/bin/omg only if symlink points into this checkout
    if link.is_symlink():
        try:
            target = link.resolve()
            checkout_resolved = checkout.resolve()
            try:
                target.relative_to(checkout_resolved)
                in_checkout = True
            except ValueError:
                in_checkout = False
            if in_checkout:
                link.unlink()
                print(f"omg uninstall: removed symlink {link} -> {target}")
            else:
                print(
                    f"omg uninstall: left {link} (symlink target not in this checkout)"
                )
        except OSError as exc:
            print(f"omg uninstall: could not inspect/remove {link}: {exc}", file=sys.stderr)
    elif link.exists():
        print(f"omg uninstall: left {link} (not a symlink)")
    else:
        print(f"omg uninstall: CLI link absent ({link})")

    # 5. never touch project .omg/
    print(
        "omg uninstall: project `.omg/` state was intentionally left untouched"
    )
    return 0
