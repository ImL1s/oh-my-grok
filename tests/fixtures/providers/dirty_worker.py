#!/usr/bin/env python3
"""Become READY, then crash after a short delay (relaunch victim)."""

from __future__ import annotations

import os
import sys
import time

_ALIVE = float(os.environ.get("OMG_TEAM_PROVIDER_ALIVE_S") or "0.6")
_CODE = int(os.environ.get("OMG_TEAM_PROVIDER_EXIT_CODE") or "1")


def main() -> int:
    print("TEAM_PROVIDER_READY_OK", flush=True)
    sys.stdout.flush()
    time.sleep(max(0.05, _ALIVE))
    print("dirty_worker: simulating crash", flush=True)
    return _CODE


if __name__ == "__main__":
    raise SystemExit(main())
