#!/usr/bin/env python3
"""Maintainer plan/check for parity completeness proofs (#78-D / #78-F).

Never mutates inventory status or maturity. ``--plan`` emits a candidate
proof; ``--check`` verifies a proof (optionally reproducing against
``--upstream-root``). With ``--source``, policy/mapping/proof/seed default to
the committed docs/parity/completeness paths.
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
    DEFAULT_MAPPING_DIR_RELATIVE,
    DEFAULT_POLICY_DIR_RELATIVE,
    DEFAULT_PROOF_DIR_RELATIVE,
    plan_completeness_proof,
    validate_completeness_mapping,
    validate_completeness_policy,
    verify_completeness_proof,
)

INVENTORY_PATH = ROOT / "docs/parity/omg-parity.json"


def _default_policy_path(source: str) -> Path:
    return ROOT / DEFAULT_POLICY_DIR_RELATIVE / f"{source}.json"


def _default_mapping_path(source: str) -> Path:
    return ROOT / DEFAULT_MAPPING_DIR_RELATIVE / f"{source}.json"


def _default_proof_path(source: str) -> Path:
    return ROOT / DEFAULT_PROOF_DIR_RELATIVE / f"{source}.json"


def _default_seed_path(source: str) -> Path:
    return ROOT / "docs" / "parity" / "upstream-snapshots" / f"{source}.json"


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
    parser.add_argument(
        "--policy",
        default=None,
        help="completeness policy JSON "
        f"(default: {DEFAULT_POLICY_DIR_RELATIVE}/{{SOURCE}}.json)",
    )
    parser.add_argument(
        "--proof",
        default=None,
        help="completeness proof JSON "
        f"(default for --check: {DEFAULT_PROOF_DIR_RELATIVE}/{{SOURCE}}.json)",
    )
    parser.add_argument(
        "--upstream-root",
        default=None,
        help="pinned upstream checkout for reproduction",
    )
    parser.add_argument(
        "--seed",
        default=None,
        help="optional upstream-snapshot seed JSON "
        "(default: docs/parity/upstream-snapshots/{SOURCE}.json if present)",
    )
    parser.add_argument(
        "--mappings",
        default=None,
        help="mapping JSON (legacy dict or completeness mapping store); "
        f"default: {DEFAULT_MAPPING_DIR_RELATIVE}/{{SOURCE}}.json if present",
    )
    parser.add_argument(
        "--proof-only",
        action="store_true",
        help="with --plan: print only canonical indented proof JSON to stdout",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print JSON payload on stdout",
    )
    args = parser.parse_args(argv)

    if args.proof_only and not args.plan:
        print(
            json.dumps(
                {"ok": False, "error": "--proof-only requires --plan"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    source = args.source
    policy_path = Path(args.policy) if args.policy else _default_policy_path(source)
    proof_path = Path(args.proof) if args.proof else _default_proof_path(source)
    mapping_path = (
        Path(args.mappings) if args.mappings else _default_mapping_path(source)
    )
    seed_path = Path(args.seed) if args.seed else _default_seed_path(source)

    inventory = load_json_object(Path(args.inventory))
    if not policy_path.is_file():
        print(
            json.dumps(
                {"ok": False, "error": f"policy not found: {policy_path}"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    policy = load_json_object(policy_path)
    try:
        validated_policy = validate_completeness_policy(policy)
    except ContractValidationError as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    if validated_policy.get("source") != source:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"--source {source!r} != policy.source",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    seed = None
    if seed_path.is_file():
        seed = load_json_object(seed_path)

    mapping = None
    if mapping_path.is_file():
        mapping = load_json_object(mapping_path)
        if isinstance(mapping, dict) and mapping.get("store_kind"):
            try:
                mapping = validate_completeness_mapping(mapping)
            except ContractValidationError as exc:
                print(
                    json.dumps({"ok": False, "error": str(exc)}, sort_keys=True),
                    file=sys.stderr,
                )
                return 1

    try:
        if args.plan:
            if not args.upstream_root:
                raise ContractValidationError("--plan requires --upstream-root")
            payload = plan_completeness_proof(
                policy=policy,
                inventory=inventory,
                upstream_root=args.upstream_root,
                seed=seed,
                mapping=mapping,
            )
            if args.proof_only:
                proof = payload["candidate_proof"]
                print(json.dumps(proof, indent=2, sort_keys=True, ensure_ascii=False))
                return 0
        else:
            if not proof_path.is_file():
                raise ContractValidationError(f"--check requires proof at {proof_path}")
            proof = load_json_object(proof_path)
            verified = verify_completeness_proof(
                proof,
                policy=policy,
                inventory=inventory,
                seed=seed,
                upstream_root=args.upstream_root,
                require_no_unresolved=True,
                mapping=mapping,
            )
            source_reproduced = args.upstream_root is not None
            payload = {
                "ok": True,
                "mode": "check",
                "artifact_consistency_verified": True,
                "source_reproduced": source_reproduced,
                "promotion_performed": False,
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
