#!/usr/bin/env python3
"""Validate the frozen OMG parity inventory and plan hashes."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omg_cli.contracts.parity_schema import (  # noqa: E402
    NORMATIVE_ARTIFACT_HASHES,
    load_json_object,
    validate_parity_inventory,
)


ARTIFACT_PATHS = {
    "requirements": ROOT / ".omx/plans/omg-oma-full-parity-requirements.md",
    "prd": ROOT / ".omx/plans/prd-omg-oma-full-parity-20260722.md",
    "test_spec": ROOT / ".omx/plans/test-spec-omg-oma-full-parity-20260722.md",
    "plan": ROOT / ".omx/plans/plan-omg-oma-full-parity-20260722.md",
}


def main() -> int:
    inventory = validate_parity_inventory(
        load_json_object(ROOT / "docs/parity/omg-parity.json")
    )
    # The normative parity artifacts are untracked OMX development inputs under
    # `.omx/plans/`; their hashes are frozen into NORMATIVE_ARTIFACT_HASHES and
    # bound into the release manifest. Re-verify them only when they are present
    # (local parity dev). On CI and fresh clones they are absent, so this check
    # is skipped rather than failing — the tracked inventory above is the gate.
    missing = [name for name, path in ARTIFACT_PATHS.items() if not path.is_file()]
    artifacts_checked = False
    if not missing:
        observed = {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in ARTIFACT_PATHS.items()
        }
        if observed != NORMATIVE_ARTIFACT_HASHES:
            raise SystemExit(
                "normative artifact hash drift: "
                + json.dumps({"expected": NORMATIVE_ARTIFACT_HASHES, "observed": observed}, sort_keys=True)
            )
        artifacts_checked = True
    print(
        json.dumps(
            {
                "ok": True,
                "repository_id": inventory["repository_id"],
                "requirements": len(inventory["requirement_ids"]),
                "mcp_operations": len(inventory["mcp_operations"]),
                "semantic_lsp_proxy_count": inventory["semantic_lsp_proxy_count"],
                "normative_artifacts_verified": artifacts_checked,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
