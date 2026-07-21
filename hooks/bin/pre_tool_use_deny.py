#!/usr/bin/env python3
import json
import sys
from pathlib import Path

# Import deny from package if installed; else load sibling path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import hook_disabled
from omg_cli.deny import decide_pre_tool_use


def main() -> None:
    if hook_disabled("pre_tool_use"):
        # Fail-open: disabled deny hook must ALLOW, never block.
        sys.stdout.write(
            json.dumps({"decision": "allow", "reason": "OMG hooks disabled"}) + "\n"
        )
        sys.exit(0)
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        event = {}
    decision = decide_pre_tool_use(event)
    # Always print JSON decision
    sys.stdout.write(json.dumps(decision) + "\n")
    if decision.get("decision") == "deny":
        sys.exit(2)
    sys.exit(0)



if __name__ == "__main__":
    main()
