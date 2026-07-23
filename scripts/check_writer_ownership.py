#!/usr/bin/env python3
"""Run the frozen inclusive dirty or final-tree ownership oracle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omg_cli.contracts.parity_schema import FROZEN_PINS, OMG_OWNER_PATTERNS  # noqa: E402
from omg_cli.contracts.writer_chain import (  # noqa: E402
    verify_dirty_ownership,
    verify_final_candidate,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--base", default=FROZEN_PINS["OMG"])
    parser.add_argument("--candidate")
    parser.add_argument("--remote")
    parser.add_argument("--approved-branch")
    parser.add_argument("--approved-remote-old-oid")
    parser.add_argument("--expected-wave")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.candidate:
        rows = verify_final_candidate(
            args.root,
            base_commit=args.base,
            candidate_commit=args.candidate,
            ownership=OMG_OWNER_PATTERNS,
            remote=args.remote,
            approved_branch=args.approved_branch,
            approved_remote_old_oid=args.approved_remote_old_oid,
        )
        mode = "final_tree"
    else:
        rows = verify_dirty_ownership(args.root, args.base, OMG_OWNER_PATTERNS)
        mode = "inclusive_dirty"
    if args.expected_wave:
        foreign = sorted({str(row["owner"]) for row in rows if row["owner"] != args.expected_wave})
        if foreign:
            raise SystemExit(
                f"changed paths escape expected wave {args.expected_wave}: owners={foreign}"
            )
    print(
        json.dumps(
            {
                "ok": True,
                "mode": mode,
                "base": args.base,
                "records": len(rows),
                "owners": sorted({str(row["owner"]) for row in rows}),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
