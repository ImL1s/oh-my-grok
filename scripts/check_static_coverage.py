#!/usr/bin/env python3
"""Prove every tracked production/test/script .py is under static roots (#24).

Fails if a tracked ``*.py`` under included roots is neither covered by the
default static entrypoint roots nor listed in the explicit exclusion table
with a reason.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Paths relative to repo root that static_checks.sh always lints/compiles.
STATIC_ROOTS = (
    "omg_cli",
    "tests",
    "scripts",
    "hooks",
)

# Tracked .py files allowed outside STATIC_ROOTS or intentionally skipped.
# Values are short reasons (must be non-empty).
EXPLICIT_EXCLUSIONS: dict[str, str] = {
    # Installer bootstrap lives outside package trees; not type-checked yet.
    # (none currently — keep map for future documented skips)
}


def _git_tracked_py_files(root: Path) -> list[Path]:
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=str(root),
        capture_output=True,
        check=True,
    )
    out: list[Path] = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        out.append(Path(raw.decode("utf-8")))
    return out


def _under_static_root(rel: Path) -> bool:
    parts = rel.parts
    if not parts:
        return False
    return parts[0] in STATIC_ROOTS


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check", action="store_true", required=True)
    p.add_argument("--root", type=Path, default=ROOT)
    args = p.parse_args(argv)
    root = args.root.resolve()

    for path, reason in EXPLICIT_EXCLUSIONS.items():
        if not str(reason).strip():
            print(f"empty exclusion reason for {path}", file=sys.stderr)
            return 1

    tracked = _git_tracked_py_files(root)
    uncovered: list[str] = []
    for rel in tracked:
        key = rel.as_posix()
        if key in EXPLICIT_EXCLUSIONS:
            continue
        if _under_static_root(rel):
            continue
        # e.g. top-level only; report
        uncovered.append(key)

    if uncovered:
        print(
            "static coverage FAILED — tracked .py outside static roots "
            f"{STATIC_ROOTS} without exclusion:",
            file=sys.stderr,
        )
        for u in uncovered:
            print(f"  {u}", file=sys.stderr)
        return 1

    n = len(tracked)
    print(f"static_coverage_ok tracked_py={n} roots={list(STATIC_ROOTS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
