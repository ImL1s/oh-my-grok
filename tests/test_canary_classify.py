# tests/test_canary_classify.py
"""Unit tests for PreToolUse canary classification (no grok)."""
from __future__ import annotations

from omg_cli.canary_classify import classify_canary, looks_like_denied


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


def test_both_denied_ok():
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
    assert r["parent_denied"] is True
    assert r["child_denied"] is True


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


def test_tool_was_denied_phrase():
    assert looks_like_denied("**Result: tool was denied**\n", "") is True


def test_report_table_denied():
    blob = (
        "| Item | Value |\n"
        "|------|--------|\n"
        "| **denied-or-ran** | **denied** |\n"
    )
    assert looks_like_denied(blob, "") is True


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
