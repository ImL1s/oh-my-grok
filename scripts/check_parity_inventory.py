#!/usr/bin/env python3
"""Validate the OMG parity inventory (v2 canonical; v1 fixture still supported)."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omg_cli.contracts.parity_schema import (  # noqa: E402
    NORMATIVE_ARTIFACT_HASHES,
    inventory_completion_claims_allowed,
    load_json_object,
    validate_parity_inventory,
)
from omg_cli.contracts.state_schemas import ContractValidationError  # noqa: E402


ARTIFACT_PATHS = {
    "requirements": ROOT / ".omx/plans/omg-oma-full-parity-requirements.md",
    "prd": ROOT / ".omx/plans/prd-omg-oma-full-parity-20260722.md",
    "test_spec": ROOT / ".omx/plans/test-spec-omg-oma-full-parity-20260722.md",
    "plan": ROOT / ".omx/plans/plan-omg-oma-full-parity-20260722.md",
}
INVENTORY_PATH = ROOT / "docs/parity/omg-parity.json"


def _check_v1_normative_artifacts() -> bool:
    missing = [name for name, path in ARTIFACT_PATHS.items() if not path.is_file()]
    if missing:
        return False
    observed = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in ARTIFACT_PATHS.items()
    }
    if observed != NORMATIVE_ARTIFACT_HASHES:
        raise SystemExit(
            "normative artifact hash drift: "
            + json.dumps(
                {"expected": NORMATIVE_ARTIFACT_HASHES, "observed": observed},
                sort_keys=True,
            )
        )
    return True


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

    path = Path(args.inventory)
    try:
        raw = load_json_object(path)
        inventory = validate_parity_inventory(
            raw,
            repo_root=ROOT if args.strict or raw.get("schema_version") == 2 else None,
        )
    except ContractValidationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1

    artifacts_checked = False
    if inventory.get("schema_version") == 1:
        artifacts_checked = _check_v1_normative_artifacts()

    open_gaps = [
        gap
        for gap in inventory.get("gaps", [])
        if isinstance(gap, dict) and gap.get("status") == "open"
    ]
    payload = {
        "ok": True,
        "schema_version": inventory["schema_version"],
        "repository_id": inventory["repository_id"],
        "inventory_status": inventory.get("inventory_status"),
        "capabilities": len(inventory.get("capabilities", inventory.get("rows", []))),
        "open_gaps": len(open_gaps),
        "completion_claims_allowed": inventory_completion_claims_allowed(inventory)
        if inventory.get("schema_version") == 2
        else False,
        "strict": bool(args.strict),
        "normative_artifacts_verified": artifacts_checked,
    }
    if inventory.get("schema_version") == 1:
        payload["requirements"] = len(inventory["requirement_ids"])
        payload["mcp_operations"] = len(inventory["mcp_operations"])
        payload["semantic_lsp_proxy_count"] = inventory["semantic_lsp_proxy_count"]

    if args.strict and inventory.get("schema_version") == 2:
        if inventory.get("inventory_status") not in {"bootstrapping", "complete"}:
            print(json.dumps({"ok": False, "error": "inventory_status invalid"}, sort_keys=True))
            return 1
        if inventory_completion_claims_allowed(inventory) and any(
            gap.get("status") == "open" for gap in inventory.get("gaps", [])
        ):
            # complete inventory may still track deferred gaps; open P0s with
            # complete status would be contradictory — reject.
            open_p0 = [
                gap
                for gap in inventory.get("gaps", [])
                if gap.get("status") == "open" and gap.get("priority") == "P0"
            ]
            if open_p0:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": "complete inventory cannot leave open P0 gaps",
                            "open_p0": [gap["id"] for gap in open_p0],
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                return 1

    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
