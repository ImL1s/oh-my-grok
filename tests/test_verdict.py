"""Strict shared verdict parsing — negation, terminal APPROVE, stubs, rc fail-closed."""
from __future__ import annotations

from omg_cli.verdict import (
    apply_stage_exit_codes,
    parse_verdict,
    prose_has_terminal_approve,
)


def test_terminal_approve():
    assert parse_verdict("## Verdict\nAPPROVE\n") == "APPROVE"
    assert parse_verdict("APPROVE\n") == "APPROVE"
    assert parse_verdict("Verdict: APPROVE\n") == "APPROVE"
    assert parse_verdict('{"verdict": "APPROVE"}') == "APPROVE"


def test_negated_approve_not_acceptance():
    assert parse_verdict("Do not APPROVE this plan yet.\n") == "UNKNOWN"
    assert parse_verdict("Do not APPROVE to be helpful.\n") == "UNKNOWN"
    assert parse_verdict("Never APPROVE a bad plan.\n") == "UNKNOWN"
    assert parse_verdict("Please APPROVE yet if unsure.\n") == "UNKNOWN"
    # free-floating mention in body without terminal line
    assert (
        parse_verdict(
            "The word APPROVE appears in the instructions but is not our verdict.\n"
        )
        == "UNKNOWN"
    )


def test_request_changes_beats_negated_or_mention():
    assert (
        parse_verdict("Do not APPROVE yet. REQUEST CHANGES.") == "REQUEST_CHANGES"
    )
    assert parse_verdict("APPROVE\nFAILED") == "FAILED"


def test_stub_markers_block_approve():
    stub = (
        "# dual-review verifier (dry_run stub)\n"
        "dry_run: no Grok exec. Verdict placeholder: NEEDS_REVIEW\n"
        "APPROVE\n"  # even if someone stuffed APPROVE into a stub
    )
    assert parse_verdict(stub) != "APPROVE"
    assert prose_has_terminal_approve(stub) is False


def test_apply_stage_exit_codes_fail_closed():
    assert apply_stage_exit_codes("APPROVE", critic_rc=0, verifier_rc=0) == "APPROVE"
    assert apply_stage_exit_codes("APPROVE", critic_rc=0, verifier_rc=127) == "FAILED"
    assert apply_stage_exit_codes("APPROVE", critic_rc=1, verifier_rc=0) == "FAILED"
    assert apply_stage_exit_codes("UNKNOWN", critic_rc=0, verifier_rc=1) == "FAILED"
    assert (
        apply_stage_exit_codes("REQUEST_CHANGES", critic_rc=0, verifier_rc=1)
        == "REQUEST_CHANGES"
    )
