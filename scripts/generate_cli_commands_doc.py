#!/usr/bin/env python3
"""Generate or check docs/cli-commands.md from command_registry (#29 Phase 4)."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "cli-commands.md"

sys.path.insert(0, str(ROOT))
from omg_cli.command_registry import (  # noqa: E402
    INVENTORY_END,
    INVENTORY_START,
    inventory_fragment,
    render_inventory_markdown,
)

HEADER = """# CLI command inventory

English. **Source of truth:** `omg_cli/command_registry.py` (`COMMAND_SPECS`).

This table is regenerated from the registry (#29 Phase 4). Do not hand-edit
between the markers — update `COMMAND_SPECS` and re-run:

```bash
python3 scripts/generate_cli_commands_doc.py
# or check only:
python3 scripts/generate_cli_commands_doc.py --check
```

Related: [cli-contract.md](./cli-contract.md) (exit codes + JSON envelopes).

"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if docs/cli-commands.md is stale",
    )
    args = parser.parse_args(argv)
    expected_body = render_inventory_markdown().strip()
    if args.check:
        if not DOC.is_file():
            print(f"missing {DOC}", file=sys.stderr)
            return 1
        current = DOC.read_text(encoding="utf-8")
        m = re.search(
            re.escape(INVENTORY_START) + r"(.*?)" + re.escape(INVENTORY_END),
            current,
            re.DOTALL,
        )
        if not m:
            print(f"missing markers in {DOC}", file=sys.stderr)
            return 1
        if m.group(1).strip() != expected_body:
            print("stale docs/cli-commands.md — run:", file=sys.stderr)
            print("  python3 scripts/generate_cli_commands_doc.py", file=sys.stderr)
            return 1
        print("ok: docs/cli-commands.md matches COMMAND_SPECS")
        return 0

    DOC.write_text(HEADER + inventory_fragment() + "\n", encoding="utf-8")
    print(f"wrote {DOC.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
