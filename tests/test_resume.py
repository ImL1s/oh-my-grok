"""omg resume + RESUME.md routing."""
from __future__ import annotations

import pytest

from omg_cli.contracts.state_schemas import ContractValidationError

from omg_cli.resume import (
    build_resume_pack,
    clear_resume_md,
    recommend_commands,
    resolve_resume_selection,
    resume_md_path,
    route_resume,
)
from omg_cli.state import create_run, write_status


def test_resume_pack_no_active(tmp_path):
    pack = build_resume_pack(tmp_path)
    assert pack["ok"] is False
    assert pack["reason"] == "no_active_run"


def test_resume_routes_pipeline_and_writes_md(tmp_path):
    run = create_run(tmp_path, mode="pipeline", goal="ship resume")
    rid = run["run_id"]
    write_status(tmp_path, rid, "running", extra={"stage": "implement"})
    code, pack = route_resume(tmp_path, run_id=rid)
    assert code == 0
    assert pack["resumable"] is True
    assert pack["mode"] == "pipeline"
    cmds = pack["commands"]
    assert any(f"omg pipeline --resume {rid}" in c for c in cmds)
    path = resume_md_path(tmp_path)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert rid in text
    assert "pipeline --resume" in text
    assert clear_resume_md(tmp_path) is True
    assert not path.is_file()


def test_resume_terminal_not_resumable(tmp_path):
    run = create_run(tmp_path, mode="ralph", goal="done goal")
    rid = run["run_id"]
    st = write_status(tmp_path, rid, "completed")
    pack = build_resume_pack(tmp_path, rid)
    assert st.get("status") == "completed"
    assert pack.get("terminal") is True
    assert pack.get("resumable") is False


def test_recommend_ralph_includes_session(tmp_path):
    status = {
        "run_id": "r1",
        "mode": "ralph",
        "status": "running",
        "grok_session_id": "11111111-1111-1111-1111-111111111111",
    }
    cmds = recommend_commands(status)
    assert any("omg ralph --resume r1" in c for c in cmds)
    assert any("grok --resume" in c for c in cmds)


def test_cli_resume_json(tmp_path, monkeypatch):
    from omg_cli.main import main

    monkeypatch.chdir(tmp_path)
    create_run(tmp_path, mode="ulw", goal="fanout")
    rc = main(["resume", "--json", "--no-write"])
    assert rc == 0


def _candidate(**overrides):
    row = {
        "repository_id": "OMG",
        "host": "grok",
        "run_id": "run-1",
        "native_session_id": "session-1",
        "recovery_manifest_sha256": "a" * 64,
        "signed_handoff_sha256": "b" * 64,
        "cwd_hash": "c" * 64,
        "generation": 3,
        "parent_hash": "d" * 64,
        "parent_valid": True,
        "live_lease": True,
        "expires_at": "2099-01-01T00:00:00Z",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("selectors", "expected"),
    [
        ({"recovery_manifest": {"sha256": "a" * 64}}, "recovery_manifest"),
        ({"run_id": "run-1"}, "run_id"),
        ({"native_session_id": "session-1"}, "native_session_id"),
        ({"current_process_run": "run-1"}, "current_process_run"),
        ({"signed_handoff": {"sha256": "b" * 64}}, "signed_handoff"),
        ({"best_effort_cwd": True}, "best_effort_cwd"),
    ],
)
def test_exact_six_rank_resume_selector(selectors, expected) -> None:
    selected = resolve_resume_selection(
        selectors,
        [_candidate()],
        expected_repository_id="OMG",
        expected_host="grok",
        expected_cwd_hash="c" * 64,
        current_generation=3,
        best_effort=expected == "best_effort_cwd",
    )
    assert selected["selector"] == expected
    assert selected["verified"] is (expected != "best_effort_cwd")


def test_higher_selector_conflict_or_invalidity_never_falls_through() -> None:
    with pytest.raises(ContractValidationError, match="E_RESUME_SELECTOR_CONFLICT"):
        resolve_resume_selection(
            {"recovery_manifest": {"sha256": "f" * 64}, "run_id": "run-1"},
            [_candidate()],
            expected_repository_id="OMG",
            expected_host="grok",
            expected_cwd_hash="c" * 64,
            current_generation=3,
        )

    with pytest.raises(ContractValidationError, match="E_RESUME_NOT_FOUND"):
        resolve_resume_selection(
            {"run_id": "run-1"},
            [_candidate(generation=2)],
            expected_repository_id="OMG",
            expected_host="grok",
            expected_cwd_hash="c" * 64,
            current_generation=3,
        )
    with pytest.raises(ContractValidationError, match="E_RESUME_NOT_FOUND"):
        resolve_resume_selection(
            {"native_session_id": "missing"},
            [_candidate()],
            expected_repository_id="OMG",
            expected_host="grok",
            expected_cwd_hash="c" * 64,
            current_generation=3,
        )


def test_best_effort_tie_or_broken_parent_is_ambiguous() -> None:
    with pytest.raises(ContractValidationError, match="E_RESUME_AMBIGUOUS"):
        resolve_resume_selection(
            {"best_effort_cwd": True},
            [_candidate(), _candidate(run_id="run-2")],
            expected_repository_id="OMG",
            expected_host="grok",
            expected_cwd_hash="c" * 64,
            current_generation=3,
            best_effort=True,
        )
    with pytest.raises(ContractValidationError, match="E_RESUME_AMBIGUOUS"):
        resolve_resume_selection(
            {"best_effort_cwd": True},
            [_candidate(parent_valid=False)],
            expected_repository_id="OMG",
            expected_host="grok",
            expected_cwd_hash="c" * 64,
            current_generation=3,
            best_effort=True,
        )
