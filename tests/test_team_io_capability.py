"""Fail-closed worker I/O capability contract — pure unit tests (#147 PR1)."""

from __future__ import annotations

import pytest

from omg_cli.team.io_capability import (
    E_OPERATOR_INPUT_NOT_READY,
    E_OPERATOR_INPUT_UNSUPPORTED,
    E_OPERATOR_KEY_UNSUPPORTED,
    INTERACTION_EVIDENCE_SCHEMA,
    IO_MODE_BACKGROUND_JOB,
    IO_MODE_HEADLESS_STREAM,
    IO_MODE_INTERACTIVE_TTY,
    IO_MODE_UNPROVEN,
    IoCapabilityRefuseError,
    TTY_OWNER_NONE,
    TTY_OWNER_SUPERVISOR,
    TTY_OWNER_UNKNOWN,
    assert_operator_input_allowed,
    background_job_io_defaults,
    normalize_worker_io_capability,
    operator_input_refusal,
    supervisor_pane_io_defaults,
    unproven_io_defaults,
)


def test_legacy_missing_fields_normalize_unproven() -> None:
    cap = normalize_worker_io_capability(None)
    assert cap == unproven_io_defaults()
    assert cap.io_mode == IO_MODE_UNPROVEN
    assert cap.provider_tty_owner == TTY_OWNER_UNKNOWN
    assert cap.input_ready is False
    assert cap.operator_input_supported is False
    assert cap.interaction_evidence is None

    cap2 = normalize_worker_io_capability({"task_id": "w1", "status": "running"})
    assert cap2.io_mode == IO_MODE_UNPROVEN
    assert cap2.operator_input_supported is False


def test_supervisor_and_background_defaults() -> None:
    s = supervisor_pane_io_defaults()
    assert s.io_mode == IO_MODE_HEADLESS_STREAM
    assert s.provider_tty_owner == TTY_OWNER_SUPERVISOR
    assert s.input_ready is False
    assert s.operator_input_supported is False

    b = background_job_io_defaults()
    assert b.io_mode == IO_MODE_BACKGROUND_JOB
    assert b.provider_tty_owner == TTY_OWNER_NONE
    assert b.operator_input_supported is False


def test_supported_requires_interactive_tty() -> None:
    # Self-inconsistent claim: supported without interactive_tty → demote.
    cap = normalize_worker_io_capability(
        {
            "io_mode": IO_MODE_HEADLESS_STREAM,
            "operator_input_supported": True,
            "input_ready": True,
            "provider_tty_owner": TTY_OWNER_SUPERVISOR,
        }
    )
    assert cap.operator_input_supported is False
    assert cap.input_ready is False
    assert cap.io_mode == IO_MODE_HEADLESS_STREAM


def test_never_infer_from_needs_pty_or_provider_name() -> None:
    cap = normalize_worker_io_capability(
        {
            "provider": "agy",
            "needs_pty": True,
            "status": "running",
            "pane_id": "%10",
        }
    )
    assert cap.io_mode == IO_MODE_UNPROVEN
    assert cap.operator_input_supported is False
    assert cap.input_ready is False


def test_invalid_enum_strings_fail_closed() -> None:
    cap = normalize_worker_io_capability(
        {
            "io_mode": "totally_fake",
            "provider_tty_owner": "nope",
            "operator_input_supported": "yes",  # not bool → false
            "input_ready": 1,  # not bool → false
        }
    )
    assert cap.io_mode == IO_MODE_UNPROVEN
    assert cap.provider_tty_owner == TTY_OWNER_UNKNOWN
    assert cap.operator_input_supported is False
    assert cap.input_ready is False


def test_nested_io_capability_block() -> None:
    cap = normalize_worker_io_capability(
        {
            "task_id": "w1",
            "io_capability": {
                "io_mode": IO_MODE_HEADLESS_STREAM,
                "provider_tty_owner": TTY_OWNER_SUPERVISOR,
                "operator_input_supported": False,
                "input_ready": False,
            },
        }
    )
    assert cap.io_mode == IO_MODE_HEADLESS_STREAM
    assert cap.provider_tty_owner == TTY_OWNER_SUPERVISOR


def test_stale_interaction_evidence_invalidated() -> None:
    raw = {
        "io_mode": IO_MODE_INTERACTIVE_TTY,
        "provider_tty_owner": "provider",
        "operator_input_supported": True,
        "input_ready": True,
        "interaction_evidence": {
            "schema": INTERACTION_EVIDENCE_SCHEMA,
            "attempt": 1,
            "generation": 0,
            "ready_marker": "TUI_READY:x",
            "proven_at": "2026-01-01T00:00:00Z",
            "pane_id": "%10",
            "provider_pid": 99,
        },
    }
    ok = normalize_worker_io_capability(raw, attempt=1, generation=0)
    assert ok.interaction_evidence is not None
    assert ok.interaction_evidence["attempt"] == 1

    stale = normalize_worker_io_capability(raw, attempt=2, generation=0)
    assert stale.interaction_evidence is None

    bad_schema = normalize_worker_io_capability(
        {
            **raw,
            "interaction_evidence": {"schema": "other", "attempt": 1, "generation": 0},
        },
        attempt=1,
        generation=0,
    )
    assert bad_schema.interaction_evidence is None


def test_operator_input_refusal_codes() -> None:
    legacy = unproven_io_defaults()
    r = operator_input_refusal(legacy, action="input")
    assert r is not None
    assert r.code == E_OPERATOR_INPUT_UNSUPPORTED
    r_key = operator_input_refusal(legacy, action="key")
    assert r_key is not None
    assert r_key.code == E_OPERATOR_KEY_UNSUPPORTED

    headless = supervisor_pane_io_defaults()
    assert operator_input_refusal(headless, action="input") is not None

    supported_not_ready = normalize_worker_io_capability(
        {
            "io_mode": IO_MODE_INTERACTIVE_TTY,
            "provider_tty_owner": "provider",
            "operator_input_supported": True,
            "input_ready": False,
        }
    )
    nr = operator_input_refusal(supported_not_ready, action="input")
    assert nr is not None
    assert nr.code == E_OPERATOR_INPUT_NOT_READY

    ready = normalize_worker_io_capability(
        {
            "io_mode": IO_MODE_INTERACTIVE_TTY,
            "provider_tty_owner": "provider",
            "operator_input_supported": True,
            "input_ready": True,
        }
    )
    assert operator_input_refusal(ready, action="input") is None
    assert_operator_input_allowed(ready, action="key")


def test_assert_raises_typed_error() -> None:
    with pytest.raises(IoCapabilityRefuseError) as exc:
        assert_operator_input_allowed(unproven_io_defaults(), action="input")
    assert exc.value.code == E_OPERATOR_INPUT_UNSUPPORTED


def test_as_public_dict_bounded() -> None:
    cap = supervisor_pane_io_defaults()
    d = cap.as_public_dict()
    assert set(d) == {
        "io_mode",
        "provider_tty_owner",
        "input_ready",
        "operator_input_supported",
        "interaction_evidence",
    }
