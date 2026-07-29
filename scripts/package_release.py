#!/usr/bin/env python3
"""CLI entry for deterministic release packaging (#26).

Example::

    python scripts/package_release.py --out dist/release-bundle
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omg_cli.package_release import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
