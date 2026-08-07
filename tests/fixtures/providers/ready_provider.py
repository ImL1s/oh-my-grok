#!/usr/bin/env python3
"""Immediately print TEAM_PROVIDER_READY_OK and hold alive."""

from __future__ import annotations

import os
import sys
import time

_HOLD = float(os.environ.get("OMG_TEAM_PROVIDER_HOLD_S") or "30")


def main() -> int:
    print("TEAM_PROVIDER_READY_OK", flush=True)
    sys.stdout.flush()
    if _HOLD > 0:
        time.sleep(_HOLD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
