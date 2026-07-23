#!/usr/bin/env python3
"""Validate exact requirement coverage and single-writer ownership."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omg_cli.contracts.parity_schema import (  # noqa: E402
    OMG_OWNER_PATTERNS,
    load_json_object,
    validate_traceability,
)
from omg_cli.contracts.writer_chain import owner_for_path  # noqa: E402


def main() -> int:
    trace = validate_traceability(
        load_json_object(ROOT / "docs/parity/omg-traceability.json")
    )
    checked_paths = 0
    for entry in trace["entries"]:
        allowed_waves = set(entry["waves"])
        for path in [*entry["code_paths"], *entry["test_paths"]]:
            owner = owner_for_path(path, OMG_OWNER_PATTERNS)
            if owner not in allowed_waves:
                raise SystemExit(
                    f"traceability owner mismatch: {entry['requirement_id']} {path} -> {owner}, "
                    f"allowed={sorted(allowed_waves)}"
                )
            checked_paths += 1
    print(
        json.dumps(
            {
                "ok": True,
                "requirements": len(trace["entries"]),
                "owned_path_references": checked_paths,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
