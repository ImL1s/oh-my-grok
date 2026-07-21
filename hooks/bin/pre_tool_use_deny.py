#!/usr/bin/env python3
"""Import-based PreToolUse deny shim (tested reference implementation).

DO NOT use this as a GLOBAL ($GROK_HOME/hooks) hook target. It ``import``s
``omg_cli`` from the checkout, so it is only usable where that checkout is on
``sys.path`` and readable — and it exits 2 on deny, which collides with the exit
code grok emits when python cannot even open a script. If a global hook pointed
here and the file were unreadable (another workspace, TCC-protected ~/Documents),
python would exit 2 and grok would read that as an explicit deny → every tool call
blocked.

The global soft-gate uses the self-contained, always-exit-0, JSON-only-deny
``omg_pretool_deny_standalone.py`` installed under ``$GROK_HOME/hooks`` via
``omg install-hook`` / ``omg setup`` (see ``omg_cli/hook_install.py``). This shim
remains as the canonical, unit-tested deny path (``omg_cli.deny``).
"""
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
