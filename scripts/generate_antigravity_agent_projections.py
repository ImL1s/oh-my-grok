#!/usr/bin/env python3
"""Generate / check static Antigravity agent.md projections (#71).

These files are parity projections, not an installed AG plugin and not live
AG evidence. Do not claim ``agy`` install works.

Usage:
  python scripts/generate_antigravity_agent_projections.py
  python scripts/generate_antigravity_agent_projections.py --check
  python scripts/generate_antigravity_agent_projections.py --root PATH
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omg_cli.agents_catalog import (  # noqa: E402
    AgentsCatalogError,
    check_antigravity_projections,
    plugin_root,
    write_antigravity_projections,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if committed projections drift from catalog + agents/",
    )
    args = parser.parse_args(argv)
    root = (args.root or plugin_root()).resolve()
    try:
        if args.check:
            errors = check_antigravity_projections(root)
            if errors:
                print("antigravity agent.md projections drift:", file=sys.stderr)
                for item in errors:
                    print(f"  {item}", file=sys.stderr)
                return 1
            print("antigravity_agent_projections_ok")
            return 0
        written = write_antigravity_projections(root)
        for rel in written:
            print(rel)
        print(f"wrote {len(written)} projection file(s)")
        return 0
    except AgentsCatalogError as exc:
        print(f"generate antigravity projections: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
