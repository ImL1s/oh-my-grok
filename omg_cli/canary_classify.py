"""Classify PreToolUse canary outcomes without running grok."""
from __future__ import annotations

from typing import Any

# Host deny.py reason — prefer this over free-form model "denied" prose.
_HOST_DENY_SIGNATURE = "oh-my-grok: external agent cli blocked"


def looks_like_real_claude(stdout: str, stderr: str) -> bool:
    blob = f"{stdout}\n{stderr}".lower()
    if "claude code" in blob and any(c.isdigit() for c in blob):
        return True
    if "omg-canary-shim" in blob:
        return False
    return False


def looks_like_host_deny_signature(stdout: str, stderr: str) -> bool:
    """True when output includes the unique oh-my-grok PreToolUse deny reason.

    Requires the exact deny.py reason substring (casefold). Generic
    "Hook denied by oh-my-grok …" model theater is **not** enough for suite green.
    """
    if looks_like_real_claude(stdout, stderr):
        return False
    blob = f"{stdout}\n{stderr}".lower()
    return _HOST_DENY_SIGNATURE in blob


def looks_like_denied(stdout: str, stderr: str) -> bool:
    """Broad deny-ish text (includes host signature and weaker model claims)."""
    if looks_like_real_claude(stdout, stderr):
        return False
    if looks_like_host_deny_signature(stdout, stderr):
        return True
    blob = f"{stdout}\n{stderr}".lower()
    return any(
        n in blob
        for n in (
            "hook denied",
            "external agent cli blocked",
            "tool was denied",
            "denied/blocked",
            "denied-or-ran:** **denied",
            "denied-or-ran: **denied",
            "denied-or-ran:**denied",
            "denied-or-ran** | **denied",
            "denied-or-ran**|**denied",
        )
    )


def classify_canary(
    *,
    parent_out: str,
    parent_err: str,
    child_out: str,
    child_err: str,
    parent_marker: bool,
    child_marker: bool,
    parent_hook_denied: bool | None = None,
    child_hook_denied: bool | None = None,
) -> dict[str, Any]:
    """Classify canary result.

    Exit 0 (pass) only when both sides have **host-signature** deny evidence
    (or explicit ``*_hook_denied=True`` from a session oracle). Model prose
    alone is exit 2 (``DENIED_CLAIMED_NO_HOOK_ORACLE``).
    """
    parent_real = looks_like_real_claude(parent_out, parent_err)
    child_real = looks_like_real_claude(child_out, child_err)
    parent_prose = looks_like_denied(parent_out, parent_err)
    child_prose = looks_like_denied(child_out, child_err)
    parent_host = (
        parent_hook_denied
        if parent_hook_denied is not None
        else looks_like_host_deny_signature(parent_out, parent_err)
    )
    child_host = (
        child_hook_denied
        if child_hook_denied is not None
        else looks_like_host_deny_signature(child_out, child_err)
    )
    # For reporting: denied if host or prose
    parent_denied = bool(parent_host or parent_prose)
    child_denied = bool(child_host or child_prose)

    any_marker = parent_marker or child_marker
    any_real = parent_real or child_real
    if any_real:
        status, exit_code = "REAL_CLI_RAN_hook_did_not_block", 1
    elif any_marker:
        status, exit_code = "MARKER_PRESENT_shim_ran", 1
    elif parent_host and child_host:
        status, exit_code = "DENIED_PARENT_AND_CHILD", 0
    elif parent_denied and child_denied:
        # Model claimed deny without host signature / hook oracle
        status, exit_code = "DENIED_CLAIMED_NO_HOOK_ORACLE", 2
    elif parent_denied or child_denied:
        status, exit_code = "DENIED_PARTIAL", 1
    else:
        status, exit_code = "INCONCLUSIVE_no_deny_evidence", 2
    return {
        "status": status,
        "exit_code": exit_code,
        "parent_real_cli_detected": parent_real,
        "child_real_cli_detected": child_real,
        "parent_denied": parent_denied,
        "child_denied": child_denied,
        "parent_host_signature": bool(parent_host),
        "child_host_signature": bool(child_host),
        "marker_exists": any_marker,
    }
