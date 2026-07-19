# omg_cli/deny.py
from __future__ import annotations

import os
import re
from typing import Any

# Executable names that default workers must not invoke as external agent CLIs
_DENY_BINS = r"(?:claude|codex|omx|agy|cursor-agent|kimi)"

# Command-position only: start of string or after shell operators (not bare whitespace).
# Allows optional ENV=val prefixes, wrappers, and path prefixes.
# Bare "echo claude is a word" must NOT match (claude is an argument, not a command head).
_CMD_POS = r"(?:^|[;&|(`]|\|\||&&)"
_ENV_ASSIGNS = r"(?:(?:[A-Za-z_][\w]*=\S*\s+)*)"
# Wrappers that still leave the denied bin in command position after them
_WRAPPERS = r"(?:(?:env|command|xargs|nice|nohup|sudo|time)\s+(?:--\s+)*)*"
_PATH_PREFIX = r"(?:\S*/)?"

_DENY_AT_CMD_POS = re.compile(
    rf"{_CMD_POS}\s*{_ENV_ASSIGNS}{_WRAPPERS}{_PATH_PREFIX}{_DENY_BINS}\b",
    re.IGNORECASE,
)
_OMC_TEAM = re.compile(rf"{_CMD_POS}\s*omc\s+team\b", re.IGNORECASE)

# sh/bash/zsh -c / -lc (login+command) with quoted OR unquoted body containing a deny bin.
# Requires short-flag cluster that includes `c` (so bare `bash -l` is not a hit).
# Examples:
#   sh -c 'claude -p x'
#   bash -lc "claude ..."
#   zsh -c claude
#   bash -c claude -p x
_SH_C = re.compile(
    rf"{_CMD_POS}\s*"
    rf"{_ENV_ASSIGNS}"
    rf"(?:(?:env|command|nice|nohup|sudo|time)\s+(?:--\s+)*)*"
    rf"(?:sh|bash|zsh)\s+-"
    rf"[A-Za-z]*c[A-Za-z]*"  # -c, -lc, -cl, any short-flag soup that includes c
    rf"\s+"
    rf"(?:"
    rf"['\"].*\b{_DENY_BINS}\b"  # quoted body
    rf"|"
    rf"{_PATH_PREFIX}{_DENY_BINS}\b"  # unquoted: sh -c claude ...
    rf")",
    re.IGNORECASE,
)


def should_deny_command(command: str) -> bool:
    if not command or not isinstance(command, str):
        return False
    # Deny when a blocked bin appears in command position (not as a free word/arg)
    if _DENY_AT_CMD_POS.search(command):
        return True
    if _OMC_TEAM.search(command):
        return True
    # sh/bash/zsh -c/-lc '...claude...' (quoted or unquoted) and similar wrappers
    if _SH_C.search(command):
        return True
    return False


def decide_pre_tool_use(event: dict[str, Any]) -> dict[str, str]:
    """Return Grok PreToolUse decision. Fail-safe: emit explicit allow/deny always."""
    try:
        tool = (event.get("toolName") or event.get("tool_name") or "").strip()
        # Claude alias
        if tool in ("Bash", "bash"):
            tool = "run_terminal_command"
        if tool not in ("run_terminal_command", "Shell"):
            return {"decision": "allow"}
        tin = event.get("toolInput") or event.get("tool_input") or {}
        cmd = tin.get("command") if isinstance(tin, dict) else None
        if not isinstance(cmd, str):
            return {"decision": "allow"}
        # ONLY process environment — never parse env from command string
        if os.environ.get("OMG_ALLOW_EXTERNAL_CLI") == "1":
            return {"decision": "allow"}
        if should_deny_command(cmd):
            return {
                "decision": "deny",
                "reason": (
                    "oh-my-grok: external agent CLI blocked "
                    "(use omg ask for advisors; set OMG_ALLOW_EXTERNAL_CLI only in omg ask child)"
                ),
            }
        return {"decision": "allow"}
    except Exception as e:
        # Explicit allow with reason logged — caller may still choose deny; fail-open is host policy
        return {"decision": "allow", "reason": f"omg-guard-error:{type(e).__name__}"}
