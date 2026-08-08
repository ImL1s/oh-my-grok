#!/usr/bin/env python3
"""Maintainer plan/check for parity completeness proofs (#78-D).

Never mutates inventory status or maturity. ``--plan`` emits a candidate
proof; ``--check`` verifies a proof (optionally reproducing against
``--upstream-root``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omg_cli.contracts.parity_schema import load_json_object  # noqa: E402
from omg_cli.contracts.state_schemas import ContractValidationError  # noqa: E402
from omg_cli.parity_completeness import (  # noqa: E402
    plan_completeness_proof,
    verify_completeness_proof,
)

INVENTORY_PATH = ROOT / "docs/parity/omg-parity.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--plan",
        action="store_true",
        help="emit a candidate proof (never writes inventory or proof files)",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify an existing proof against policy/inventory/(optional) checkout",
    )
    parser.add_argument(
        "--inventory",
        default=str(INVENTORY_PATH),
        help="parity inventory path",
    )
    parser.add_argument("--source", required=True, help="parity source id")
    parser.add_argument("--policy", required=True, help="completeness policy JSON")
    parser.add_argument(
        "--proof",
        default=None,
        help="completeness proof JSON (required for --check)",
    )
    parser.add_argument(
        "--upstream-root",
        default=None,
        help="pinned upstream checkout for reproduction",
    )
    parser.add_argument(
        "--seed",
        default=None,
        help="optional upstream-snapshot seed JSON (default: docs seed if present)",
    )
    parser.add_argument(
        "--mappings",
        default=None,
        help="JSON object mapping surface_id → [capability_id, ...] for --plan",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print JSON payload on stdout",
    )
    args = parser.parse_args(argv)

    inventory = load_json_object(Path(args.inventory))
    policy = load_json_object(Path(args.policy))
    if policy.get("source") != args.source:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"--source {args.source!r} != policy.source",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    seed = load_json_object(Path(args.seed)) if args.seed else None
    if seed is None:
        default_seed = (
            ROOT / "docs" / "parity" / "upstream-snapshots" / f"{args.source}.json"
        )
        if default_seed.is_file():
            seed = load_json_object(default_seed)

    mappings = None
    if args.mappings:
        mappings = json.loads(Path(args.mappings).read_text(encoding="utf-8"))

    try:
        if args.plan:
            if not args.upstream_root:
                raise ContractValidationError("--plan requires --upstream-root")
            payload = plan_completeness_proof(
                policy=policy,
                inventory=inventory,
                upstream_root=args.upstream_root,
                seed=seed,
                surface_mappings=mappings,
            )
        else:
            if not args.proof:
                raise ContractValidationError("--check requires --proof")
            proof = load_json_object(Path(args.proof))
            verified = verify_completeness_proof(
                proof,
                policy=policy,
                inventory=inventory,
                seed=seed,
                upstream_root=args.upstream_root,
                require_no_unresolved=True,
            )
            payload = {
                "ok": True,
                "mode": "check",
                "mutates_inventory": False,
                "mutates_proof_artifact": False,
                **verified,
            }
    except ContractValidationError as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
