#!/usr/bin/env python3
"""Generate / check static Antigravity skill SKILL.md projections (#70).

These files are parity projections, not an installed AG plugin and not live
AG evidence. Do not claim ``agy`` install works.

Usage:
  python scripts/generate_antigravity_skill_projections.py
  python scripts/generate_antigravity_skill_projections.py --check
  python scripts/generate_antigravity_skill_projections.py --root PATH
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omg_cli.skills_catalog import (  # noqa: E402
    SkillsCatalogError,
    check_antigravity_projections,
    check_catalog_markdown,
    plugin_root,
    write_antigravity_projections,
    write_catalog_markdown,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if committed projections or catalog markdown drift",
    )
    args = parser.parse_args(argv)
    root = (args.root or plugin_root()).resolve()
    try:
        if args.check:
            errors = check_antigravity_projections(root) + check_catalog_markdown(root)
            if errors:
                print("antigravity skill projections / catalog docs drift:", file=sys.stderr)
                for item in errors:
                    print(f"  {item}", file=sys.stderr)
                return 1
            print("antigravity_skill_projections_ok")
            return 0
        written = write_antigravity_projections(root)
        docs = write_catalog_markdown(root)
        for rel in written:
            print(rel)
        for rel in docs:
            print(rel)
        print(f"wrote {len(written)} projection file(s) + {len(docs)} catalog markdown")
        return 0
    except SkillsCatalogError as exc:
        print(f"generate antigravity skill projections: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
