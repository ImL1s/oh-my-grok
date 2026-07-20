# tests/test_canary_classify.py
"""Unit tests for PreToolUse canary classification (no grok)."""
from __future__ import annotations

from omg_cli.canary_classify import (
    classify_canary,
    looks_like_denied,
    looks_like_host_deny_signature,
)


def test_real_cli_status():
    r = classify_canary(
        parent_out="2.1.215 (Claude Code)\n",
        parent_err="",
        child_out="2.1.215 (Claude Code)\n",
        child_err="",
        parent_marker=False,
        child_marker=False,
    )
    assert r["status"] == "REAL_CLI_RAN_hook_did_not_block"
    assert r["exit_code"] == 1
    assert r["parent_real_cli_detected"] is True
    assert r["child_real_cli_detected"] is True


def test_both_host_signature_denied_ok():
    msg = "Hook denied: oh-my-grok: external agent CLI blocked\n"
    r = classify_canary(
        parent_out=msg,
        parent_err="",
        child_out="**denied-or-ran:** **Denied**\n" + msg,
        child_err="",
        parent_marker=False,
        child_marker=False,
    )
    assert r["status"] == "DENIED_PARENT_AND_CHILD"
    assert r["exit_code"] == 0
    assert r["parent_host_signature"] is True
    assert r["child_host_signature"] is True


def test_prose_only_deny_not_suite_green():
    """Model theater without host signature must not exit 0."""
    r = classify_canary(
        parent_out="**Result: tool was denied**\n",
        parent_err="",
        child_out="| **denied-or-ran** | **denied** |\n",
        child_err="",
        parent_marker=False,
        child_marker=False,
    )
    assert r["status"] == "DENIED_CLAIMED_NO_HOOK_ORACLE"
    assert r["exit_code"] == 2
    assert looks_like_host_deny_signature(
        "**Result: tool was denied**\n", ""
    ) is False


def test_loose_hook_denied_oh_my_grok_not_host_signature():
    """'Hook denied by oh-my-grok…' without exact reason is not suite green."""
    fake = "Hook denied by oh-my-grok because I refuse\n"
    assert looks_like_host_deny_signature(fake, "") is False
    r = classify_canary(
        parent_out=fake,
        parent_err="",
        child_out=fake,
        child_err="",
        parent_marker=False,
        child_marker=False,
    )
    assert r["status"] == "DENIED_CLAIMED_NO_HOOK_ORACLE"
    assert r["exit_code"] == 2


def test_explicit_hook_oracle_flags_pass():
    r = classify_canary(
        parent_out="whatever model said",
        parent_err="",
        child_out="whatever",
        child_err="",
        parent_marker=False,
        child_marker=False,
        parent_hook_denied=True,
        child_hook_denied=True,
    )
    assert r["status"] == "DENIED_PARENT_AND_CHILD"
    assert r["exit_code"] == 0


def test_silent_is_inconclusive():
    r = classify_canary(
        parent_out="I chose not to run any tools.\n",
        parent_err="",
        child_out="Nothing to do.\n",
        child_err="",
        parent_marker=False,
        child_marker=False,
    )
    assert r["status"] == "INCONCLUSIVE_no_deny_evidence"
    assert r["exit_code"] == 2
    assert r["parent_denied"] is False
    assert r["child_denied"] is False


def test_tool_was_denied_phrase_is_prose_not_host():
    assert looks_like_denied("**Result: tool was denied**\n", "") is True
    assert looks_like_host_deny_signature("**Result: tool was denied**\n", "") is False


def test_report_table_denied_is_prose():
    blob = (
        "| Item | Value |\n"
        "|------|--------|\n"
        "| **denied-or-ran** | **denied** |\n"
    )
    assert looks_like_denied(blob, "") is True
    assert looks_like_host_deny_signature(blob, "") is False


def test_marker_present_shim_ran():
    r = classify_canary(
        parent_out="omg-canary-shim: marker written; exit 99\n",
        parent_err="",
        child_out="",
        child_err="",
        parent_marker=True,
        child_marker=False,
    )
    assert r["status"] == "MARKER_PRESENT_shim_ran"
    assert r["exit_code"] == 1
    assert r["marker_exists"] is True


def test_partial_denied():
    msg = "Hook denied: oh-my-grok: external agent CLI blocked\n"
    r = classify_canary(
        parent_out=msg,
        parent_err="",
        child_out="I chose not to run any tools.\n",
        child_err="",
        parent_marker=False,
        child_marker=False,
    )
    assert r["status"] == "DENIED_PARTIAL"
    assert r["exit_code"] == 1


def test_parent_host_child_capability_isolation_pass():
    """Parent PreToolUse host deny + child no-shell capability = suite green."""
    from omg_cli.canary_classify import looks_like_capability_isolation

    parent = "Hook denied: oh-my-grok: external agent CLI blocked\n"
    child = (
        "spawn capability_mode=read-write\n"
        "run_terminal_command is not in the tool list for this worker. "
        "Available tools are limited to file ops. status: denied/blocked.\n"
    )
    assert looks_like_capability_isolation(child, "") is True
    r = classify_canary(
        parent_out=parent,
        parent_err="",
        child_out=child,
        child_err="",
        parent_marker=False,
        child_marker=False,
    )
    assert r["status"] == "DENIED_PARENT_HOST_CHILD_CAPABILITY"
    assert r["exit_code"] == 0
    assert r["parent_host_signature"] is True
    assert r["child_capability_isolation"] is True
