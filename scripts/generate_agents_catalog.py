#!/usr/bin/env python3
"""Generate / check agents/catalog.json and AG projections from YAML (#71).

``agents/catalog.yaml`` is the source of truth. Committed JSON stays for the
fail-closed loader. Antigravity ``agent.md`` files are static projections —
not live AG evidence.

Usage:
  python scripts/generate_agents_catalog.py
  python scripts/generate_agents_catalog.py --check
  python scripts/generate_agents_catalog.py --root PATH
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
    check_antigravity_agent_tools,
    check_antigravity_projections,
    check_catalog_yaml,
    plugin_root,
    write_antigravity_projections,
    write_antigravity_agent_tools,
    write_catalog_json_from_yaml,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if JSON or AG projections drift from YAML + agents/",
    )
    args = parser.parse_args(argv)
    root = (args.root or plugin_root()).resolve()
    try:
        if args.check:
            errors = (
                check_catalog_yaml(root)
                + check_antigravity_agent_tools(root)
                + check_antigravity_projections(root)
            )
            if errors:
                print("agents catalog drift:", file=sys.stderr)
                for item in errors:
                    print(f"  {item}", file=sys.stderr)
                return 1
            print("agents_catalog_ok")
            return 0
        written_json = write_catalog_json_from_yaml(root)
        print(written_json)
        agent_files = write_antigravity_agent_tools(root)
        for rel in agent_files:
            print(rel)
        written = write_antigravity_projections(root)
        for rel in written:
            print(rel)
        print(
            f"wrote {written_json}, synchronized {len(agent_files)} installable "
            f"agent file(s), and {len(written)} projection file(s)"
        )
        return 0
    except AgentsCatalogError as exc:
        print(f"generate agents catalog: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
