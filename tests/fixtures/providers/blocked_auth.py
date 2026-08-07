#!/usr/bin/env python3
"""Emit authentication-required prompt and never become READY."""

from __future__ import annotations

import os
import sys
import time

_HOLD = float(os.environ.get("OMG_TEAM_PROVIDER_HOLD_S") or "30")


def main() -> int:
    print("authentication required — please log in", flush=True)
    sys.stdout.flush()
    if _HOLD > 0:
        time.sleep(_HOLD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
