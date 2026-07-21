# omg_cli/deny.py
from __future__ import annotations

import os
import re
from typing import Any

# Executable names that default workers must not invoke as external agent CLIs
_DENY_BINS = r"(?:claude|codex|omx|agy|cursor-agent|kimi)"

# Command-position only: start of string, a NEWLINE, or after shell operators
# (not bare whitespace). A denied bin on its own line (multi-line scripts,
# heredocs, sequential setup+run) is command-position too — the newline class
# member closes the "no semicolon needed" bypass.
# Bare "echo claude is a word" must NOT match (claude is an argument, not a command head).
_CMD_POS = r"(?:^|[;&|(`\n\r]|\|\||&&)"
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


def suggested_capability_mode(subagent_type: str) -> str:
    """Best-effort mode for retry messaging when mode is missing or wrong."""
    req = required_capability_mode(subagent_type)
    if req:
        return req
    # Unknown type: default implementer-ish unless name looks read-only
    st = (subagent_type or "").strip().lower()
    if any(k in st for k in ("critic", "verif", "explore", "plan", "review", "architect")):
        return "read-only"
    return "read-write"


def spawn_deny_reason(*, st: str, cm: str, kind: str) -> str:
    """Human+model-facing deny text that **forces retry**, not abandon multi-agent.

    Models often stop spawning after a bare deny. The reason must say RETRY NOW
    with the exact field to set, and forbid solo-fallback as the default response.
    """
    suggested = suggested_capability_mode(st)
    type_label = st or "(missing subagent_type)"
    if kind == "missing":
        head = (
            f"oh-my-grok: spawn_subagent DENIED — missing capability_mode "
            f"(subagent_type={type_label!r})."
        )
    elif kind == "invalid":
        head = (
            f"oh-my-grok: spawn_subagent DENIED — invalid capability_mode={cm!r} "
            f"(subagent_type={type_label!r})."
        )
    elif kind == "execute_all":
        head = (
            f"oh-my-grok: spawn_subagent DENIED — capability_mode execute/all not allowed "
            f"(subagent_type={type_label!r})."
        )
    elif kind == "mismatch":
        head = (
            f"oh-my-grok: spawn_subagent DENIED — subagent_type={type_label!r} "
            f"requires capability_mode={suggested!r} (got {cm!r})."
        )
    else:
        head = "oh-my-grok: spawn_subagent DENIED."

    return (
        f"{head} "
        "RETRY IMMEDIATELY in this same turn: call spawn_subagent again with the SAME prompt "
        f"and capability_mode={suggested!r} "
        "(read-only for explore/plan/critic/verifier; read-write for general-purpose/"
        "omg-executor). "
        "Do NOT abandon multi-agent work. Do NOT switch to solo-only just because spawn was "
        "denied. Do NOT use capability_mode=execute or all. "
        f"Minimal fix: add parameter capability_mode={suggested!r}."
    )


def _depth_or_nested_spawn_denied(tin: dict[str, Any], st: str) -> str | None:
    """Executor/implementer children must not re-spawn (depth=1)."""
    depth = tin.get("depth") or tin.get("child_depth") or tin.get("spawn_depth")
    try:
        if depth is not None and int(depth) > 1:
            return (
                "oh-my-grok: spawn_subagent DENIED — child depth>1 forbidden "
                f"(subagent_type={st or '?'}). Workers must not re-spawn."
            )
    except (TypeError, ValueError):
        pass
    # Nested tool lists that include spawn are denied for executors
    tools = tin.get("tools") or tin.get("allowed_tools") or tin.get("allowedTools")
    if isinstance(tools, (list, tuple)):
        lowered = {str(t).strip().lower() for t in tools}
        if "spawn_subagent" in lowered or "task" in lowered:
            if st in _READ_WRITE_TYPES or "executor" in (st or ""):
                return (
                    "oh-my-grok: spawn_subagent DENIED — executor role may not "
                    "include spawn_subagent/Task in tools (depth=1)."
                )
    return None


def decide_spawn_subagent(tin: dict[str, Any]) -> dict[str, str]:
    """Fail-closed spawn policy when PreToolUse runs for spawn_subagent.

    - Missing capability_mode → deny (reason mandates immediate retry)
    - Mode incompatible with role table → deny (reason mandates retry with required mode)
    - execute/all denied; executor nested spawn denied
    - Unknown type with explicit mode → allow (host still applies the mode)
    """
    if os.environ.get("OMG_ALLOW_UNSAFE_SPAWN") == "1":
        return {"decision": "allow", "reason": "OMG_ALLOW_UNSAFE_SPAWN=1"}
    st, cm = _spawn_fields(tin)
    depth_deny = _depth_or_nested_spawn_denied(tin, st)
    if depth_deny:
        return {"decision": "deny", "reason": depth_deny}
    if not cm:
        return {
            "decision": "deny",
            "reason": spawn_deny_reason(st=st, cm=cm, kind="missing"),
        }
    if cm not in ("read-write", "read-only", "read_write", "read_only", "execute", "all"):
        return {
            "decision": "deny",
            "reason": spawn_deny_reason(st=st, cm=cm, kind="invalid"),
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
            "reason": spawn_deny_reason(st=st, cm=cm, kind="execute_all"),
        }
    required = required_capability_mode(st)
    if required is None:
        # Unknown type but mode present and is RW or RO → allow
        return {"decision": "allow"}
    if cm != required:
        return {
            "decision": "deny",
            "reason": spawn_deny_reason(st=st, cm=cm, kind="mismatch"),
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
