#!/usr/bin/env python3
"""Print READY, then echo literal stdin lines (operator round-trip)."""

from __future__ import annotations

import os
import sys
import time

_HOLD = float(os.environ.get("OMG_TEAM_PROVIDER_HOLD_S") or "30")


def main() -> int:
    print("TEAM_PROVIDER_READY_OK", flush=True)
    sys.stdout.flush()
    deadline = time.monotonic() + max(1.0, _HOLD)
    while time.monotonic() < deadline:
        line = sys.stdin.readline()
        if not line:
            time.sleep(0.05)
            continue
        print(f"ECHO:{line.rstrip(chr(10))}", flush=True)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
