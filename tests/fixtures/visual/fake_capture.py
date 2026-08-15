"""Hermetic visual capture driver for tests (#75).

Writes a tiny PNG to ``OMG_VISUAL_OUTPUT``. Optional ``OMG_VISUAL_FAKE_SOURCE``
copies an existing fixture instead. Does not import Playwright or decode pixels.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# 1x1 transparent PNG.
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def main() -> int:
    dest = os.environ.get("OMG_VISUAL_OUTPUT", "").strip()
    if not dest:
        print("OMG_VISUAL_OUTPUT is required", file=sys.stderr)
        return 2
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    src = os.environ.get("OMG_VISUAL_FAKE_SOURCE", "").strip()
    if src:
        shutil.copyfile(src, path)
        return 0
    path.write_bytes(PNG_1X1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
