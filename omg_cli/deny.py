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
# Wrappers that still leave the denied bin in command position after them.
# Path-prefixed env/exec allowed: /usr/bin/env claude, /bin/exec codex.
_WRAPPER_BIN = r"(?:(?:\S*/)?(?:env|command|xargs|nice|nohup|sudo|time|exec))"
_WRAPPERS = rf"(?:{_WRAPPER_BIN}\s+(?:--\s+)*)*"
_PATH_PREFIX = r"(?:\S*/)?"

_DENY_AT_CMD_POS = re.compile(
    rf"{_CMD_POS}\s*{_ENV_ASSIGNS}{_WRAPPERS}{_PATH_PREFIX}{_DENY_BINS}\b",
    re.IGNORECASE,
)
_OMC_TEAM = re.compile(rf"{_CMD_POS}\s*omc\s+team\b", re.IGNORECASE)

# eval claude ... (command-position eval of a deny bin)
_EVAL = re.compile(
    rf"{_CMD_POS}\s*{_ENV_ASSIGNS}{_WRAPPERS}(?:\S*/)?eval\s+(?:['\"]?){_PATH_PREFIX}{_DENY_BINS}\b",
    re.IGNORECASE,
)

# sh/bash/zsh -c / -lc (login+command) with quoted OR unquoted body containing a deny bin.
# Path-prefixed shells: /bin/bash -c 'claude'
# Requires short-flag cluster that includes `c` (so bare `bash -l` is not a hit).
_SH_C = re.compile(
    rf"{_CMD_POS}\s*"
    rf"{_ENV_ASSIGNS}"
    rf"{_WRAPPERS}"
    rf"(?:\S*/)?(?:sh|bash|zsh)\s+-"
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
    if _EVAL.search(command):
        return True
    return False


# Role → required capability_mode for spawn_subagent fail-closed gate.
# Soft-gate: only effective when PreToolUse runs (still fail-open on hook crash).
_READ_ONLY_TYPES = frozenset(
    {
        "explore",
        "plan",
        "omg-critic",
        "omg-verifier",
        "oh-my-claudecode:explore",
        "oh-my-claudecode:code-reviewer",
        "oh-my-claudecode:security-reviewer",
        "oh-my-claudecode:architect",
        "oh-my-claudecode:critic",
        "oh-my-claudecode:planner",
    }
)
_READ_WRITE_TYPES = frozenset(
    {
        "omg-executor",
        "general-purpose",  # default implementer path in oh-my-grok skills
        "oh-my-claudecode:executor",
    }
)


def _tool_input(event: dict[str, Any]) -> dict[str, Any]:
    tin = event.get("toolInput") or event.get("tool_input") or {}
    return tin if isinstance(tin, dict) else {}


def _spawn_fields(tin: dict[str, Any]) -> tuple[str, str]:
    """Return (subagent_type, capability_mode) lowercased, empty if missing."""
    st = (
        tin.get("subagent_type")
        or tin.get("subagentType")
        or tin.get("agent_type")
        or tin.get("agentType")
        or ""
    )
    cm = (
        tin.get("capability_mode")
        or tin.get("capabilityMode")
        or ""
    )
    return str(st).strip().lower(), str(cm).strip().lower()


def required_capability_mode(subagent_type: str) -> str | None:
    """Return required mode for *subagent_type*, or None if unknown (still require some mode)."""
    st = (subagent_type or "").strip().lower()
    if not st:
        return None
    if st in _READ_ONLY_TYPES or "critic" in st or "verifier" in st or "explore" in st:
        if st in _READ_WRITE_TYPES:
            return "read-write"  # explicit RW type wins
        return "read-only"
    if st in _READ_WRITE_TYPES or "executor" in st:
        return "read-write"
    # Unknown types: still require an explicit mode (caller enforces presence)
    return None


def decide_spawn_subagent(tin: dict[str, Any]) -> dict[str, str]:
    """Fail-closed spawn policy when PreToolUse runs for spawn_subagent.

    - Missing capability_mode → deny
    - Mode incompatible with role table → deny
    - Unknown type with explicit mode → allow (host still applies the mode)
    """
    if os.environ.get("OMG_ALLOW_UNSAFE_SPAWN") == "1":
        return {"decision": "allow", "reason": "OMG_ALLOW_UNSAFE_SPAWN=1"}
    st, cm = _spawn_fields(tin)
    if not cm:
        return {
            "decision": "deny",
            "reason": (
                "oh-my-grok: spawn_subagent requires capability_mode "
                "(read-write for implementers; read-only for critic/verifier/explore)"
            ),
        }
    if cm not in ("read-write", "read-only", "read_write", "read_only", "execute", "all"):
        return {
            "decision": "deny",
            "reason": f"oh-my-grok: invalid capability_mode {cm!r}",
        }
    # Normalize underscores
    if cm == "read_write":
        cm = "read-write"
    if cm == "read_only":
        cm = "read-only"
    # execute/all are never allowed for default workers under oh-my-grok
    if cm in ("execute", "all"):
        return {
            "decision": "deny",
            "reason": (
                "oh-my-grok: capability_mode execute/all denied for spawn "
                "(use read-write or read-only)"
            ),
        }
    required = required_capability_mode(st)
    if required is None:
        # Unknown type but mode present and is RW or RO → allow
        return {"decision": "allow"}
    if cm != required:
        return {
            "decision": "deny",
            "reason": (
                f"oh-my-grok: subagent_type {st!r} requires capability_mode={required!r} "
                f"(got {cm!r})"
            ),
        }
    return {"decision": "allow"}


def decide_pre_tool_use(event: dict[str, Any]) -> dict[str, str]:
    """Return Grok PreToolUse decision. Fail-safe: emit explicit allow/deny always."""
    try:
        tool = (event.get("toolName") or event.get("tool_name") or "").strip()
        # Claude alias
        if tool in ("Bash", "bash"):
            tool = "run_terminal_command"
        if tool in ("Task", "task"):
            tool = "spawn_subagent"
        if tool == "spawn_subagent":
            return decide_spawn_subagent(_tool_input(event))
        if tool not in ("run_terminal_command", "Shell"):
            return {"decision": "allow"}
        tin = _tool_input(event)
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
