#!/usr/bin/env python3
"""Validate the OMG parity inventory (v2 canonical; v1 fixture still supported)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omg_cli.contracts.state_schemas import ContractValidationError  # noqa: E402
from omg_cli.parity_check import check_parity_inventory  # noqa: E402


INVENTORY_PATH = ROOT / "docs/parity/omg-parity.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail closed on schema/path/overclaim drift (gaps may remain open)",
    )
    parser.add_argument(
        "--inventory",
        default=str(INVENTORY_PATH),
        help="parity inventory path (default: docs/parity/omg-parity.json)",
    )
    args = parser.parse_args(argv)

    try:
        payload = check_parity_inventory(
            inventory_path=Path(args.inventory),
            repo_root=ROOT,
            strict=bool(args.strict),
        )
    except ContractValidationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1

    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
