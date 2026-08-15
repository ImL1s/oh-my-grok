#!/usr/bin/env python3
"""Generate / check the Antigravity hook projection README + hooks.json (#72)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omg_cli.hooks_registry import (  # noqa: E402
    HooksRegistryError,
    check_antigravity_projection,
    plugin_root,
    write_antigravity_projection,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    root = (args.root or plugin_root()).resolve()
    try:
        if args.check:
            errors = check_antigravity_projection(root)
            if errors:
                print("antigravity hook projection drift:", file=sys.stderr)
                for item in errors:
                    print(f"  {item}", file=sys.stderr)
                return 1
            print("antigravity_hook_projection_ok")
            return 0
        print(write_antigravity_projection(root))
        return 0
    except HooksRegistryError as exc:
        print(f"generate hook projection: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
