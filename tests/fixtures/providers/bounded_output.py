#!/usr/bin/env python3
"""Emit READY plus bounded secret-like noise for capture redaction tests."""

from __future__ import annotations

import os
import sys
import time

_HOLD = float(os.environ.get("OMG_TEAM_PROVIDER_HOLD_S") or "30")


def main() -> int:
    print("TEAM_PROVIDER_READY_OK", flush=True)
    print("token=sk-secret-should-redact", flush=True)
    print(f"home_path={os.path.expanduser('~')}/.omg/secret", flush=True)
    print("api_key=AKIAIOSFODNN7EXAMPLE", flush=True)
    sys.stdout.flush()
    if _HOLD > 0:
        time.sleep(_HOLD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
