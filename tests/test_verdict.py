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


def test_cant_cannot_unable_negation_blocks_terminal_approve():
    # Negated language in body must neutralize a later terminal APPROVE line
    # (research R3: can't / unable / cannot)
    assert parse_verdict("I can't APPROVE this plan.\n\nAPPROVE\n") == "UNKNOWN"
    assert parse_verdict("Cannot APPROVE.\n\nVerdict: APPROVE\n") == "UNKNOWN"
    assert parse_verdict("Unable to APPROVE this.\n\nAPPROVE\n") == "UNKNOWN"
    assert (
        parse_verdict("We refuse to APPROVE.\n\n## Verdict\nAPPROVE\n")
        == "UNKNOWN"
    )
    assert parse_verdict("I decline to APPROVE.\n\nAPPROVE\n") == "UNKNOWN"
    assert parse_verdict("I won't APPROVE this.\n\nAPPROVE\n") == "UNKNOWN"
    # Smart apostrophe (U+2019) must still fail-closed
    assert (
        parse_verdict("I can\u2019t APPROVE this plan.\n\nAPPROVE\n") == "UNKNOWN"
    )


def test_fenced_approve_alone_is_not_acceptance():
    assert parse_verdict("```\nAPPROVE\n```\n") == "UNKNOWN"
    assert (
        parse_verdict(
            "Example stub:\n```md\n## Verdict\nAPPROVE\n```\nNeeds work.\n"
        )
        == "UNKNOWN"
    )
    # Unclosed fence (LLM often omits closer) must not false-green
    assert parse_verdict("```\nAPPROVE\n") == "UNKNOWN"
    assert parse_verdict("~~~\nAPPROVE\n~~~\n") == "UNKNOWN"
    # Real terminal outside fence still works
    assert (
        parse_verdict("See example:\n```\nAPPROVE\n```\n\nVerdict: APPROVE\n")
        == "APPROVE"
    )


def test_json_approve_survives_negation_notes():
    # JSON path is preferred escape hatch when notes mention negation
    assert (
        parse_verdict(
            '{"verdict": "APPROVE", "notes": "I can\'t APPROVE lightly"}'
        )
        == "APPROVE"
    )


def test_exit_code_override_law_documented():
    # Regression lock for dual_review apply path (research Exit Code Override Law)
    assert apply_stage_exit_codes("APPROVE", critic_rc=0, verifier_rc=1) == "FAILED"
    assert apply_stage_exit_codes("APPROVE", critic_rc=2, verifier_rc=0) == "FAILED"


def test_schema_v2_run_id_match_and_mismatch():
    good = (
        '{"schema_version": 2, "run_id": "run-abc", "stage": "verifier", '
        '"verdict": "APPROVE", "is_stub": false, "evidence": "tests green"}'
    )
    assert parse_verdict(good, expected_run_id="run-abc") == "APPROVE"
    # Research spelling APPROVED normalizes
    approved = (
        '{"schema_version": 2, "run_id": "run-abc", "verdict": "APPROVED", '
        '"is_stub": false}'
    )
    assert parse_verdict(approved, expected_run_id="run-abc") == "APPROVE"
    # Mismatch must not false-green
    bad = (
        '{"schema_version": 2, "run_id": "other", "verdict": "APPROVE", '
        '"is_stub": false}'
    )
    assert parse_verdict(bad, expected_run_id="run-abc") != "APPROVE"
    # is_stub blocks
    stub = (
        '{"schema_version": 2, "run_id": "run-abc", "verdict": "APPROVE", '
        '"is_stub": true}'
    )
    assert parse_verdict(stub, expected_run_id="run-abc") != "APPROVE"


def test_schema_v2_in_fenced_json():
    text = (
        "Here is my decision:\n"
        "```json\n"
        '{"schema_version": 2, "run_id": "r1", "verdict": "REQUEST_CHANGES"}\n'
        "```\n"
    )
    assert parse_verdict(text, expected_run_id="r1") == "REQUEST_CHANGES"


def test_failed_case_insensitive_blocks_terminal_approve():
    """Prose Failed/failed/FAILED must beat terminal APPROVE (fail-closed)."""
    assert parse_verdict("Failed\n\nAPPROVE\n") == "FAILED"
    assert parse_verdict("failed\n\nAPPROVE\n") == "FAILED"
    assert parse_verdict("FAILED\n\nAPPROVE\n") == "FAILED"
    assert parse_verdict("Verdict: Failed\n\n## Verdict\nAPPROVE\n") == "FAILED"


def test_schema_v2_present_no_prose_approve_fallback():
    """schema_version=2 docs must not fall through to prose APPROVE."""
    # ITERATE is not an acceptance signal; trailing terminal APPROVE ignored
    iterate = '{"schema_version": 2, "verdict": "ITERATE"}\n\nAPPROVE\n'
    assert parse_verdict(iterate) != "APPROVE"
    assert parse_verdict(iterate) == "UNKNOWN"
    # Missing usable verdict field → UNKNOWN, not prose APPROVE
    missing = '{"schema_version": 2, "run_id": "r1"}\n\nVerdict: APPROVE\n'
    assert parse_verdict(missing) != "APPROVE"
    # schema_v2 APPROVE still works
    approve = '{"schema_version": 2, "verdict": "APPROVE"}'
    assert parse_verdict(approve) == "APPROVE"
    # prose-only terminal APPROVE (no schema_v2) still works
    assert parse_verdict("## Verdict\nAPPROVE\n") == "APPROVE"


def test_run_id_binding_poisons_stale_document():
    """A present-but-mismatched run_id makes the whole artifact stale/wrong-run:
    a stray unbound `{"verdict":"APPROVE"}` snippet elsewhere must NOT win."""
    # Real, correctly-bound verdict is a mismatch (stale run); a stray example
    # APPROVE snippet with no run_id must NOT override the mismatched binding.
    text = (
        '{"run_id": "WRONG-STALE-RUN", "verdict": "FAILED"}\n\n'
        "The expected format looks like:\n"
        "```json\n"
        '{"verdict": "APPROVE"}\n'
        "```\n"
    )
    assert parse_verdict(text, expected_run_id="REAL-RUN-123") != "APPROVE"

    # A wrong-run schema-v2 document cannot approve even with an APPROVE verdict.
    wrong = '{"schema_version": 2, "run_id": "WRONG", "verdict": "APPROVE"}'
    assert parse_verdict(wrong, expected_run_id="REAL-RUN-123") != "APPROVE"


def test_run_id_binding_preserves_unbound_artifacts():
    """ralplan/dual-review write path-bound verifier artifacts that legitimately
    carry NO run_id — those must still be accepted under a run_id-bound gate."""
    # bare unbound JSON approve (the real dual-review/ralplan shape)
    assert (
        parse_verdict('{"verdict": "APPROVE", "notes": "ok"}', expected_run_id="REAL-RUN-123")
        == "APPROVE"
    )
    # prose terminal approve (the other real shape)
    assert parse_verdict("## Verdict\nAPPROVE\n", expected_run_id="REAL-RUN-123") == "APPROVE"
    # with NO run_id requirement the legacy behavior is unchanged
    assert parse_verdict('{"verdict": "APPROVE"}') == "APPROVE"
    # a correctly bound run_id still approves, including nested result objects
    assert (
        parse_verdict(
            '{"run_id": "REAL-RUN-123", "verdict": "APPROVE"}',
            expected_run_id="REAL-RUN-123",
        )
        == "APPROVE"
    )
    nested = '{"run_id": "REAL-RUN-123", "result": {"verdict": "APPROVE"}}'
    assert parse_verdict(nested, expected_run_id="REAL-RUN-123") == "APPROVE"


def test_poison_flipped_order_two_raw_objects():
    """Stray APPROVE first + stale-run FAILED second must be FAILED (not APPROVE)."""
    text = '{"verdict": "APPROVE"}\n{"run_id": "WRONG", "verdict": "FAILED"}'
    assert parse_verdict(text, expected_run_id="REAL") == "FAILED"


def test_poison_fenced_stray_before_stale():
    """Fenced stray APPROVE before a stale-run FAILED object must be FAILED."""
    text = (
        "The expected format:\n"
        "```json\n"
        '{"verdict": "APPROVE"}\n'
        "```\n\n"
        '{"run_id": "WRONG-STALE-RUN", "verdict": "FAILED"}\n'
    )
    assert parse_verdict(text, expected_run_id="REAL-RUN-123") == "FAILED"


def test_bound_rc_beats_earlier_fenced_stray_approve():
    """Matching-run REQUEST_CHANGES after fenced stray APPROVE must win."""
    text = (
        "Format e.g.:\n"
        "```json\n"
        '{"verdict":"APPROVE"}\n'
        "```\n\n"
        '{"run_id":"REAL-RUN-123","verdict":"REQUEST_CHANGES"}\n'
    )
    assert parse_verdict(text, expected_run_id="REAL-RUN-123") == "REQUEST_CHANGES"


def test_bound_schema_v2_failed_beats_earlier_fenced_stray_approve():
    """Bound schema_version=2 FAILED after fenced stray APPROVE must be FAILED."""
    text = (
        "Example:\n"
        "```json\n"
        '{"verdict": "APPROVE"}\n'
        "```\n\n"
        '{"schema_version": 2, "run_id": "REAL-RUN-123", "verdict": "FAILED", '
        '"is_stub": false}\n'
    )
    assert parse_verdict(text, expected_run_id="REAL-RUN-123") == "FAILED"


def test_legit_unbound_approve_regression():
    """Unbound artifacts must still APPROVE under a run_id-bound gate."""
    assert (
        parse_verdict('{"verdict":"APPROVE","notes":"ok"}', expected_run_id="R")
        == "APPROVE"
    )
    assert parse_verdict("## Verdict\nAPPROVE\n", expected_run_id="R") == "APPROVE"
    assert parse_verdict('{"verdict":"APPROVE"}') == "APPROVE"


def test_scanner_ignores_unbalanced_brace_in_string():
    """Lone `}` inside a JSON string must not truncate the stale-run object.

    Pre-fix brace scanner treated the `}` in `"note": "} closer"` as a
    structural closer, truncated the first object to invalid JSON, dropped it,
    and let the stray APPROVE win. Must stay FAILED under run_id binding.
    """
    b2 = (
        '{"run_id": "WRONG", "verdict": "FAILED", "note": "} closer"}\n'
        '{"verdict": "APPROVE"}'
    )
    assert parse_verdict(b2, expected_run_id="REAL-RUN-123") == "FAILED"


def test_scanner_lone_open_brace_in_string_stays_failed():
    """Regression pin: lone `{` inside a string must not hide the stale FAILED."""
    text = (
        '{"verdict": "APPROVE", "note": "open { brace"}\n'
        '{"run_id": "WRONG", "verdict": "FAILED"}'
    )
    assert parse_verdict(text, expected_run_id="REAL-RUN-123") == "FAILED"


def test_scanner_balanced_braces_in_string_still_extracts_stale():
    """Balanced braces inside a string must still extract the stale FAILED object."""
    text = (
        '{"run_id":"WRONG","verdict":"FAILED","note":"cfg {\\"debug\\": true}"}\n'
        '{"verdict":"APPROVE"}'
    )
    assert parse_verdict(text, expected_run_id="REAL-RUN-123") == "FAILED"


def test_scanner_union_prose_odd_quote_before_stale():
    """Odd prose double-quote must not hide a following raw JSON stale object.

    Quote-aware-only scan stays in_string=True after the unmatched prose quote,
    skips every following brace, misses the WRONG-run FAILED object, and lets
    fenced APPROVE win. Union with quote-agnostic brace scan must yield FAILED.
    """
    t = (
        'He said "beware of stale runs\n'
        '{"run_id": "WRONG", "verdict": "FAILED"}\n'
        "Example:\n"
        "```json\n"
        '{"verdict": "APPROVE"}\n'
        "```\n"
    )
    assert parse_verdict(t, expected_run_id="REAL-RUN-123") == "FAILED"


def test_scanner_union_prose_odd_quote_legit_unbound_approve():
    """Odd prose quote + real unbound APPROVE (no stale object) still approves."""
    t = 'He said "ok"\n{"verdict":"APPROVE"}'
    assert parse_verdict(t, expected_run_id="R") == "APPROVE"
