"""Fail-closed worker I/O capability contract — pure unit tests (#147 PR1)."""

from __future__ import annotations

from pathlib import Path

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
    assert stale.input_ready is False

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


def test_demote_interactive_readiness_clears_stale_ready() -> None:
    from omg_cli.team.io_capability import (
        demote_interactive_readiness,
        interactive_pane_io_ready,
        stamp_io_capability,
    )

    row: dict = {"task_id": "w1", "attempt": 2, "pane_id": "%11"}
    stamp_io_capability(
        row,
        interactive_pane_io_ready(
            ready_marker="TUI_READY:abc",
            pane_id="%10",
            provider_pid=99,
            attempt=1,
            generation=0,
        ),
    )
    assert row["input_ready"] is True
    assert demote_interactive_readiness(row) is True
    assert row["input_ready"] is False
    assert row["operator_input_supported"] is True
    assert row["io_mode"] == IO_MODE_INTERACTIVE_TTY
    assert row["interaction_evidence"] is None

    headless: dict = {"task_id": "w2"}
    stamp_io_capability(headless, supervisor_pane_io_defaults())
    assert demote_interactive_readiness(headless) is False
    assert headless["io_mode"] == IO_MODE_HEADLESS_STREAM


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


def test_stamp_io_capability_and_topology_defaults() -> None:
    from omg_cli.team.io_capability import (
        io_defaults_for_worker_topology,
        stamp_io_capability,
    )

    row: dict = {"task_id": "w1"}
    stamp_io_capability(row)  # default supervisor
    assert row["io_mode"] == IO_MODE_HEADLESS_STREAM
    assert row["provider_tty_owner"] == TTY_OWNER_SUPERVISOR
    assert row["input_ready"] is False
    assert row["operator_input_supported"] is False
    assert row["interaction_evidence"] is None

    job: dict = {}
    stamp_io_capability(job, io_defaults_for_worker_topology("job"))
    assert job["io_mode"] == IO_MODE_BACKGROUND_JOB
    assert job["provider_tty_owner"] == TTY_OWNER_NONE
    assert job["operator_input_supported"] is False

    assert io_defaults_for_worker_topology("pane").io_mode == IO_MODE_HEADLESS_STREAM
    assert io_defaults_for_worker_topology(None).io_mode == IO_MODE_UNPROVEN


def test_write_provider_descriptor_stamps_headless_io(tmp_path: Path) -> None:
    """New descriptors are CLI-stamped headless; needs_pty does not promote."""
    from omg_cli.team.io_capability import normalize_worker_io_capability
    from omg_cli.team.supervisor import (
        DESCRIPTOR_SCHEMA_VERSION,
        load_provider_descriptor,
        write_provider_descriptor,
    )

    path = tmp_path / "w1.provider.json"
    write_provider_descriptor(
        path,
        provider="agy",
        argv=["agy", "-p", "hi"],
        needs_pty=True,
    )
    data = load_provider_descriptor(path)
    assert data["schema_version"] == DESCRIPTOR_SCHEMA_VERSION
    assert data["needs_pty"] is True
    assert data["io_mode"] == IO_MODE_HEADLESS_STREAM
    assert data["provider_tty_owner"] == TTY_OWNER_SUPERVISOR
    assert data["input_ready"] is False
    assert data["operator_input_supported"] is False
    assert data["interaction_evidence"] is None
    cap = normalize_worker_io_capability(data)
    assert cap.operator_input_supported is False


def test_legacy_descriptor_without_io_keys_still_loads(tmp_path: Path) -> None:
    """Backcompat: old descriptors load; normalize → unsupported."""
    import json

    from omg_cli.team.io_capability import normalize_worker_io_capability
    from omg_cli.team.supervisor import DESCRIPTOR_KIND, load_provider_descriptor

    path = tmp_path / "legacy.provider.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": DESCRIPTOR_KIND,
                "provider": "grok",
                "argv": ["grok", "--prompt-file", "x"],
                "prompt_delivery": "prompt-file",
                "needs_pty": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    data = load_provider_descriptor(path)
    assert "io_mode" not in data
    cap = normalize_worker_io_capability(data)
    assert cap.io_mode == IO_MODE_UNPROVEN
    assert cap.operator_input_supported is False


def test_legacy_task_row_io_projects_unproven_in_status_view() -> None:
    """Missing I/O on a task still projects unproven via worker_status_view."""
    from omg_cli.team.launch import worker_status_view

    view = worker_status_view(
        {
            "task_id": "legacy",
            "status": "running",
            "pane_id": "%10",
            "provider": "grok",
            "needs_pty": True,
        }
    )
    assert view["topology"] == "pane"
    assert view["io"]["io_mode"] == IO_MODE_UNPROVEN
    assert view["io"]["operator_input_supported"] is False
    assert view["io"]["input_ready"] is False


def test_start_team_dry_run_stamps_task_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launch writers stamp headless I/O on pane task rows."""
    import subprocess

    from omg_cli.team.plane import EXPERIMENTAL_ENV, start_team

    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    monkeypatch.delenv("OMG_DISABLE_TMUX_TEAM", raising=False)
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "i"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    meta = start_team(
        "io stamp",
        [{"task_id": "t1", "owned_files": ["README.md"]}],
        root=tmp_path,
        dry_run=True,
    )
    task = meta["tasks"][0]
    assert task["io_mode"] == IO_MODE_HEADLESS_STREAM
    assert task["provider_tty_owner"] == TTY_OWNER_SUPERVISOR
    assert task["input_ready"] is False
    assert task["operator_input_supported"] is False
    assert task["interaction_evidence"] is None
    # Descriptor written for dry-run path also carries I/O stamp.
    from omg_cli.team.plane import team_dir
    from omg_cli.team.supervisor import load_provider_descriptor

    desc = team_dir(tmp_path, meta["run_id"]) / "t1.provider.json"
    assert desc.is_file()
    d = load_provider_descriptor(desc)
    assert d["io_mode"] == IO_MODE_HEADLESS_STREAM
    assert d["operator_input_supported"] is False
