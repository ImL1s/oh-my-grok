#!/usr/bin/env python3
"""Hermetic fake ``agy`` binary for Antigravity provider probe tests (#67-A).

Responds to ``--version`` / ``--help`` via argv only (no network). Version and
help text are overridable via env for compat-range fixtures:

- ``FAKE_AGY_VERSION`` — printed for ``--version`` (default: fixture version.txt)
- ``FAKE_AGY_HELP`` — if set, path to help text; else sibling ``help.txt``
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_VERSION = (_HERE / "version.txt").read_text(encoding="utf-8").strip()
_DEFAULT_HELP = (_HERE / "help.txt").read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    version = os.environ.get("FAKE_AGY_VERSION", _DEFAULT_VERSION).strip() or _DEFAULT_VERSION
    help_path = os.environ.get("FAKE_AGY_HELP")
    help_text = (
        Path(help_path).read_text(encoding="utf-8")
        if help_path
        else _DEFAULT_HELP
    )

    if "--version" in args or "-V" in args:
        sys.stdout.write(version + "\n")
        return 0
    if "--help" in args or "-h" in args or not args:
        sys.stdout.write(help_text if help_text.endswith("\n") else help_text + "\n")
        return 0

    # Unknown subcommands stay hermetic: no TTY / network.
    sys.stderr.write(f"fake_agy: unsupported argv {args!r} (hermetic stub)\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
