#!/usr/bin/env python3
"""Exit immediately with configurable code (default 0)."""

from __future__ import annotations

import os


def main() -> int:
    code = int(os.environ.get("OMG_TEAM_PROVIDER_EXIT_CODE") or "0")
    msg = (os.environ.get("OMG_TEAM_PROVIDER_EXIT_MSG") or "").strip()
    if msg:
        print(msg, flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
