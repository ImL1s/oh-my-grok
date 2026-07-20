from __future__ import annotations

import uuid

import pytest

from omg_cli.host_session import (
    HostSessionError,
    allocate_host_session,
    load_host_session,
    session_flag_argv,
)


def test_preallocated_session_uses_session_id_once_then_resume() -> None:
    binding = allocate_host_session()
    assert str(uuid.UUID(binding.session_id)) == binding.session_id
    assert binding.launch_argv() == ["--session-id", binding.session_id]

    attempted = binding.attempted()
    assert attempted.launch_argv() == ["--resume", binding.session_id]
    assert attempted.status_fields()["grok_session_attempts"] == 1


def test_process_resume_requires_persisted_binding() -> None:
    with pytest.raises(HostSessionError, match="refusing silent new session"):
        load_host_session({}, required=True)


def test_persisted_binding_round_trips_and_never_emits_both_flags() -> None:
    session_id = str(uuid.uuid4())
    loaded = load_host_session(
        {
            "grok_session_id": session_id,
            "grok_session_attempts": 2,
            "grok_session_state": "resumable",
        }
    )
    assert loaded is not None
    assert loaded.launch_argv() == ["--resume", session_id]
    with pytest.raises(HostSessionError, match="both"):
        session_flag_argv(
            new_session_id=session_id,
            resume_session_id=session_id,
        )


@pytest.mark.parametrize(
    "run",
    [
        {"grok_session_id": "not-a-uuid"},
        {"grok_session_id": str(uuid.uuid4()), "grok_session_attempts": -1},
        {"grok_session_id": str(uuid.uuid4()), "grok_session_attempts": True},
        {"grok_session_id": str(uuid.uuid4()), "grok_session_state": "unknown"},
    ],
)
def test_malformed_binding_fails_closed(run: dict) -> None:
    with pytest.raises(HostSessionError):
        load_host_session(run)
