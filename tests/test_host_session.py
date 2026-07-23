from __future__ import annotations

import uuid

import pytest

from omg_cli.host_session import (
    HostSessionError,
    allocate_host_session,
    bind_session_lineage,
    load_host_session,
    session_route_argv,
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


def test_exact_create_resume_continue_and_named_fork_routes() -> None:
    created = str(uuid.uuid4())
    parent = str(uuid.uuid4())
    forked = str(uuid.uuid4())
    assert session_route_argv(create_session_id=created) == ["--session-id", created]
    assert session_route_argv(resume_session_id=parent) == ["--resume", parent]
    assert session_route_argv(continue_best_effort=True) == ["--continue"]
    assert session_route_argv(
        resume_session_id=parent,
        fork_session=True,
        new_session_id=forked,
    ) == ["--resume", parent, "--fork-session", "--session-id", forked]
    assert session_route_argv(
        continue_best_effort=True,
        fork_session=True,
        new_session_id=forked,
    ) == ["--continue", "--fork-session", "--session-id", forked]


def test_session_routes_reject_collision_and_invalid_combinations() -> None:
    session_id = str(uuid.uuid4())
    with pytest.raises(HostSessionError, match="already exists"):
        session_route_argv(create_session_id=session_id, existing_session_ids={session_id})
    with pytest.raises(HostSessionError):
        session_route_argv(continue_best_effort=False)
    with pytest.raises(HostSessionError):
        session_route_argv(resume_session_id=session_id, fork_session=True)


def test_lineage_binding_fences_run_cwd_generation_and_receipts() -> None:
    parent = str(uuid.uuid4())
    child = str(uuid.uuid4())
    binding = bind_session_lineage(
        session_id=child,
        parent_session_id=parent,
        run_id="run-1",
        cwd_hash="a" * 64,
        generation=4,
        spawn_receipt_hash="b" * 64,
        role_receipt_hash="c" * 64,
        observed_session_id=child,
    )
    assert binding["parent_session_id"] == parent
    assert binding["generation"] == 4
    with pytest.raises(HostSessionError, match="observed"):
        bind_session_lineage(
            session_id=child,
            parent_session_id=parent,
            run_id="run-1",
            cwd_hash="a" * 64,
            generation=4,
            spawn_receipt_hash="b" * 64,
            role_receipt_hash="c" * 64,
            observed_session_id=parent,
        )
