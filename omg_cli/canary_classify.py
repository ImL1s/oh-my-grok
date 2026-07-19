"""Classify PreToolUse canary outcomes without running grok."""
from __future__ import annotations

from typing import Any


def looks_like_real_claude(stdout: str, stderr: str) -> bool:
    blob = f"{stdout}\n{stderr}".lower()
    if "claude code" in blob and any(c.isdigit() for c in blob):
        return True
    if "omg-canary-shim" in blob:
        return False
    return False


def looks_like_denied(stdout: str, stderr: str) -> bool:
    if looks_like_real_claude(stdout, stderr):
        return False
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
            # markdown report table: | **denied-or-ran** | **denied** |
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
    parent_real = looks_like_real_claude(parent_out, parent_err)
    child_real = looks_like_real_claude(child_out, child_err)
    parent_denied = (
        parent_hook_denied
        if parent_hook_denied is not None
        else looks_like_denied(parent_out, parent_err)
    )
    child_denied = (
        child_hook_denied
        if child_hook_denied is not None
        else looks_like_denied(child_out, child_err)
    )
    any_marker = parent_marker or child_marker
    any_real = parent_real or child_real
    if any_real:
        status, exit_code = "REAL_CLI_RAN_hook_did_not_block", 1
    elif any_marker:
        status, exit_code = "MARKER_PRESENT_shim_ran", 1
    elif parent_denied and child_denied:
        status, exit_code = "DENIED_PARENT_AND_CHILD", 0
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
        "marker_exists": any_marker,
    }
