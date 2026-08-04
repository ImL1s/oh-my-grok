"""U-11 strict Autopilot v2 transitions."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omg_cli.acceptance import clear_cli_acceptance_tokens
from omg_cli.autopilot import (
    AutopilotError,
    COMMIT_ONLY_TRANSITIONS,
    LEGAL_TRANSITIONS,
    MANUAL_TRANSITIONS,
    assert_legal_transition,
    autopilot_context_pack,
    build_phase_prompt,
    complete_with_acceptance,
    run_autopilot,
    set_awaiting_confirmation,
    start_autopilot,
    status_autopilot,
    transition,
)
from omg_cli.main import main
from omg_cli.state import create_run, load_active_run, load_run, merge_status_fields
from omg_cli.stop_gate import decide_stop
from omg_cli.qa import freeze_scenarios, run_qa_cycle
from omg_cli.review import run_structured_review

ROOT = Path(__file__).resolve().parents[1]


def _ev_interview_bg() -> dict:
    """Test-only break-glass interview gate (not a CLI stamp)."""
    return {"interview_complete": True, "break_glass": True}


def _ev_consensus_bg() -> dict:
    """Test-only break-glass consensus gate (not a CLI stamp)."""
    return {"consensus": True, "break_glass": True}


def _ev_no_change_bg(reason: str = "no product change") -> dict:
    """Test-only break-glass no-change gate for implement→review."""
    return {"no_change_reason": reason, "break_glass": True}



def _goal_bound_prd(tmp_path: Path, goal: str) -> dict:
    test_file = tmp_path / "tests" / "test_ok.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return {
        "version": 1,
        "goal": goal,
        "stories": [
            {
                "id": "s1",
                "title": "ok",
                "commands": [
                    [sys.executable, "-m", "pytest", str(test_file), "-q"]
                ],
            }
        ],
        "global_commands": [],
    }


def _stamp_review_clean(root: Path, run_id: str, diff: str = "diff body") -> None:
    run_structured_review(
        root,
        run_id,
        diff_text=diff,
        code_reviewer_payload={"verdict": "APPROVE", "findings": []},
        architect_payload={"verdict": "CLEAR", "findings": []},
    )


def _stamp_qa_clean(root: Path, run_id: str, *, tmp_path: Path | None = None) -> None:
    if tmp_path is not None:
        test_file = tmp_path / "tests" / "test_ok.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        freeze_scenarios(
            root,
            run_id,
            [
                {
                    "id": "s1",
                    "check": "command",
                    "command": [
                        sys.executable,
                        "-m",
                        "pytest",
                        str(test_file),
                        "-q",
                    ],
                }
            ],
        )
    else:
        freeze_scenarios(
            root,
            run_id,
            [{"id": "s1", "check": "always_pass"}],
            allow_always_pass=True,
        )
    out = run_qa_cycle(root, run_id)
    assert out["clean"] is True


def _walk_to_acceptance(root: Path, rid: str, *, tmp_path: Path | None = None) -> None:
    transition(root, rid, "implement", evidence=_ev_consensus_bg())
    transition(root, rid, "review", evidence=_ev_no_change_bg())
    _stamp_review_clean(root, rid)
    transition(root, rid, "qa")
    _stamp_qa_clean(root, rid, tmp_path=tmp_path)
    transition(root, rid, "acceptance")


def test_legal_transition_table() -> None:
    assert_legal_transition("interview", "ralplan")
    with pytest.raises(AutopilotError):
        assert_legal_transition("interview", "qa")
    with pytest.raises(AutopilotError, match="commit-only|illegal"):
        assert_legal_transition("init", "verified")
    with pytest.raises(AutopilotError, match="commit-only"):
        assert_legal_transition("acceptance", "verified")
    assert "acceptance" in LEGAL_TRANSITIONS["qa"]
    # Conceptual graph still includes verified; manual table does not.
    assert "verified" in LEGAL_TRANSITIONS["acceptance"]
    assert "verified" not in MANUAL_TRANSITIONS["acceptance"]
    assert COMMIT_ONLY_TRANSITIONS["acceptance"] == frozenset({"verified"})


def test_consensus_stamp_unlocks_implement_without_boolean(tmp_path: Path) -> None:
    """CLI ralplan accepted stamp is enough — no evidence.consensus boolean."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.ralplan import ralplan_state_path

    st = start_autopilot(tmp_path, "stamp consensus", skip_interview=True)
    rid = st["run_id"]
    path = ralplan_state_path(tmp_path, rid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        __import__("json").dumps(
            {
                "writer": CLI_WRITER,
                "run_id": rid,
                "accepted": True,
                "status": "accepted",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    out = transition(tmp_path, rid, "implement")
    assert out["phase"] == "implement"
    hist = __import__("omg_cli.autopilot", fromlist=["load_autopilot"]).load_autopilot(
        tmp_path, rid
    )["history"]
    assert not any(h.get("gate_audit") for h in hist if h.get("phase") == "implement")


def test_status_legal_next_excludes_verified(tmp_path: Path) -> None:
    st = start_autopilot(tmp_path, "legal next contract", skip_interview=True)
    rid = st["run_id"]
    _walk_to_acceptance(tmp_path, rid, tmp_path=tmp_path)
    st2 = status_autopilot(tmp_path, rid)
    assert st2["phase"] == "acceptance"
    assert "verified" not in st2["legal_next"]
    assert st2.get("commit_only_next") == ["verified"]
    assert st2.get("terminal_action") == "omg autopilot complete"
    with pytest.raises(AutopilotError, match="commit-only"):
        transition(tmp_path, rid, "verified")


def test_start_and_gated_transitions(tmp_path: Path) -> None:
    st = start_autopilot(tmp_path, "ship parity core")
    rid = st["run_id"]
    assert st["phase"] == "interview"
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("schema_version") == 2

    with pytest.raises(AutopilotError, match="interview"):
        transition(tmp_path, rid, "ralplan")

    # Bare boolean without break_glass is no longer trusted.
    with pytest.raises(AutopilotError, match="break_glass"):
        transition(
            tmp_path,
            rid,
            "ralplan",
            evidence={"interview_complete": True},
        )

    transition(
        tmp_path,
        rid,
        "ralplan",
        evidence=_ev_interview_bg(),
    )
    with pytest.raises(AutopilotError, match="consensus"):
        transition(tmp_path, rid, "implement")

    with pytest.raises(AutopilotError, match="break_glass"):
        transition(
            tmp_path,
            rid,
            "implement",
            evidence={"consensus": True},
        )

    transition(
        tmp_path,
        rid,
        "implement",
        evidence=_ev_consensus_bg(),
    )
    transition(tmp_path, rid, "review", evidence=_ev_no_change_bg())

    # evidence_json alone cannot open QA — needs staged structured_review
    with pytest.raises(AutopilotError, match="structured_review"):
        transition(
            tmp_path,
            rid,
            "qa",
            evidence={"review_clean": True},
        )

    _stamp_review_clean(tmp_path, rid)
    transition(tmp_path, rid, "qa")

    with pytest.raises(AutopilotError, match="ultraqa"):
        transition(
            tmp_path,
            rid,
            "acceptance",
            evidence={"qa_clean": True},
        )

    _stamp_qa_clean(tmp_path, rid)
    transition(tmp_path, rid, "acceptance")
    st2 = status_autopilot(tmp_path, rid)
    assert st2["phase"] == "acceptance"
    assert st2["verified"] is False


def test_complete_without_prd_materializes_from_ultraqa(tmp_path: Path) -> None:
    """Clean ultraqa always_pass scenarios materialize to prd (true) then verify."""
    clear_cli_acceptance_tokens()
    st = start_autopilot(tmp_path, "verify path", skip_interview=True)
    rid = st["run_id"]
    _walk_to_acceptance(tmp_path, rid, tmp_path=tmp_path)
    out = complete_with_acceptance(tmp_path, rid)
    assert out["phase"] == "verified"
    assert out["verified"] is True
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("verified") is True
    assert run.get("autopilot_phase") == "verified"
    assert (tmp_path / ".omg" / "state" / "runs" / rid / "prd.json").is_file()


def test_complete_without_prd_or_ultraqa_refuses(tmp_path: Path) -> None:
    """No prd and no materializable ultraqa → AutopilotError."""
    clear_cli_acceptance_tokens()
    st = start_autopilot(tmp_path, "no prd no qa", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "review", evidence=_ev_no_change_bg())
    _stamp_review_clean(tmp_path, rid)
    transition(tmp_path, rid, "qa")
    # Frozen but never run → not clean; transition to acceptance requires clean
    # so stamp clean then wipe ultraqa file after entering acceptance.
    _stamp_qa_clean(tmp_path, rid)
    transition(tmp_path, rid, "acceptance")
    qa_path = (
        tmp_path / ".omg" / "state" / "runs" / rid / "stages" / "ultraqa.json"
    )
    qa_path.unlink()
    with pytest.raises(AutopilotError, match="prd|ultraqa"):
        complete_with_acceptance(tmp_path, rid)


def test_try_advance_records_gate_failure(tmp_path: Path) -> None:
    from omg_cli.autopilot import _try_advance_after_launch

    clear_cli_acceptance_tokens()
    st = start_autopilot(tmp_path, "gate failure surface", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "review", evidence=_ev_no_change_bg())
    _stamp_review_clean(tmp_path, rid)
    transition(tmp_path, rid, "qa")
    _stamp_qa_clean(tmp_path, rid)
    transition(tmp_path, rid, "acceptance")
    (
        tmp_path / ".omg" / "state" / "runs" / rid / "stages" / "ultraqa.json"
    ).unlink()
    assert _try_advance_after_launch(tmp_path, rid, "acceptance") == "acceptance"
    st2 = status_autopilot(tmp_path, rid)
    assert st2["phase"] == "acceptance"
    gate = st2.get("gate_failure")
    assert isinstance(gate, dict)
    assert gate.get("phase") == "acceptance"
    assert gate.get("message")


def test_complete_happy_path_same_process_acceptance(tmp_path: Path) -> None:
    """Happy path: freeze_and_run in-process then set_verified → verified."""
    clear_cli_acceptance_tokens()
    st = start_autopilot(tmp_path, "happy accept", skip_interview=True)
    rid = st["run_id"]
    _walk_to_acceptance(tmp_path, rid, tmp_path=tmp_path)

    prd = _goal_bound_prd(tmp_path, "happy accept")
    out = complete_with_acceptance(tmp_path, rid, prd=prd)
    assert out["phase"] == "verified"
    assert out["verified"] is True
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("verified") is True
    assert run.get("status") == "verified"
    assert run.get("autopilot_phase") == "verified"


def test_complete_short_circuit_when_already_verified(tmp_path: Path) -> None:
    """If omg accept already verified, complete syncs phase without re-accept."""
    clear_cli_acceptance_tokens()
    from omg_cli.acceptance import freeze_and_run
    from omg_cli.state import set_verified

    st = start_autopilot(tmp_path, "short circuit", skip_interview=True)
    rid = st["run_id"]
    _walk_to_acceptance(tmp_path, rid, tmp_path=tmp_path)
    prd = _goal_bound_prd(tmp_path, "short circuit")
    assert freeze_and_run(tmp_path, rid, prd) is True
    set_verified(tmp_path, rid, force=False)
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("verified") is True
    # Autopilot still on acceptance until complete
    assert status_autopilot(tmp_path, rid)["phase"] == "acceptance"

    out = complete_with_acceptance(tmp_path, rid, prd=prd)
    assert out["phase"] == "verified"
    assert out["verified"] is True
    run2 = load_run(tmp_path, rid)
    assert run2 is not None
    assert run2.get("autopilot_phase") == "verified"
    # Second complete is idempotent
    out2 = complete_with_acceptance(tmp_path, rid)
    assert out2["phase"] == "verified"


def test_autopilot_complete_rejects_analyze_only_acceptance(tmp_path: Path) -> None:
    clear_cli_acceptance_tokens()
    st = start_autopilot(tmp_path, "analyze only", skip_interview=True)
    rid = st["run_id"]
    _walk_to_acceptance(tmp_path, rid)
    prd = {
        "version": 1,
        "goal": "analyze only",
        "stories": [
            {
                "id": "s1",
                "title": "lint",
                "commands": [["flutter", "analyze", "lib"]],
            }
        ],
        "global_commands": [],
    }
    with pytest.raises(AutopilotError, match="analyze-only|goal-bound"):
        complete_with_acceptance(tmp_path, rid, prd=prd)


def test_blocked_to_qa_still_requires_review(tmp_path: Path) -> None:
    """Destination gates apply even when recovering from blocked."""
    st = start_autopilot(tmp_path, "blocked qa", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "review", evidence=_ev_no_change_bg())
    transition(tmp_path, rid, "blocked", reason="ops")
    with pytest.raises(AutopilotError, match="structured_review"):
        transition(tmp_path, rid, "qa")


def test_blocked_to_implement_requires_consensus(tmp_path: Path) -> None:
    st = start_autopilot(tmp_path, "blocked impl", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "blocked", reason="wait")
    with pytest.raises(AutopilotError, match="consensus"):
        transition(tmp_path, rid, "implement")


def test_implement_to_review_requires_evidence_of_work(tmp_path: Path) -> None:
    """implement→review must not silently pass with zero product change."""
    st = start_autopilot(tmp_path, "impl gate", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    with pytest.raises(AutopilotError, match="implementation|no_change"):
        transition(tmp_path, rid, "review")  # no receipt / no_change_reason
    transition(
        tmp_path,
        rid,
        "review",
        evidence={"no_change_reason": "dry-run scaffold only", "break_glass": True},
    )
    assert status_autopilot(tmp_path, rid)["phase"] == "review"


def test_implement_to_review_allows_workspace_fingerprint_change(
    tmp_path: Path,
) -> None:
    """A real workspace change since entering implement is sufficient on its
    own — no receipt or break_glass no_change needed."""
    st = start_autopilot(tmp_path, "impl gate fp", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    (tmp_path / "changed.py").write_text("# product change\n", encoding="utf-8")
    transition(tmp_path, rid, "review")
    assert status_autopilot(tmp_path, rid)["phase"] == "review"


def test_implement_to_review_allows_implementation_receipt_with_break_glass(
    tmp_path: Path,
) -> None:
    """An inline implementation_receipt is unauthenticated caller JSON, not a
    verified CLI stamp — it only substitutes for a fp change when audited via
    break_glass=true (same as no_change_reason)."""
    from omg_cli.evidence import CLI_WRITER

    st = start_autopilot(tmp_path, "impl gate receipt", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(
        tmp_path,
        rid,
        "review",
        evidence={
            "implementation_receipt": {"writer": CLI_WRITER, "note": "work done"},
            "break_glass": True,
        },
    )
    assert status_autopilot(tmp_path, rid)["phase"] == "review"


def test_implement_to_review_rejects_inline_receipt_without_break_glass(
    tmp_path: Path,
) -> None:
    """Inline evidence.implementation_receipt alone (no break_glass, no real
    fp change) must not bypass the implement→review work gate."""
    from omg_cli.evidence import CLI_WRITER

    st = start_autopilot(tmp_path, "impl gate receipt no bg", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    with pytest.raises(AutopilotError, match="implementation|no_change"):
        transition(
            tmp_path,
            rid,
            "review",
            evidence={
                "implementation_receipt": {"writer": CLI_WRITER, "note": "work done"}
            },
        )
    assert status_autopilot(tmp_path, rid)["phase"] == "implement"


def test_implement_fingerprint_covers_non_python_product_surfaces(
    tmp_path: Path,
) -> None:
    """P1 regression: qa.product_hash only hashes omg_cli/**/*.py, so a
    change confined to plugin.json / hooks/ / skills/ / agents/ / templates/
    (or a non-.py file under omg_cli/) is invisible to it. The implement-gate
    fingerprint must still detect such a change."""
    from omg_cli.autopilot import _implement_workspace_fingerprint
    from omg_cli.qa import product_hash

    (tmp_path / "omg_cli").mkdir()
    (tmp_path / "omg_cli" / "core.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "hooks.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "plugin.json").write_text('{"v": 1}\n', encoding="utf-8")

    fp_before = _implement_workspace_fingerprint(tmp_path)
    qa_before = product_hash(tmp_path)

    (tmp_path / "plugin.json").write_text('{"v": 2}\n', encoding="utf-8")
    (tmp_path / "hooks" / "hooks.json").write_text('{"changed": true}\n', encoding="utf-8")

    fp_after = _implement_workspace_fingerprint(tmp_path)
    qa_after = product_hash(tmp_path)

    assert fp_after != fp_before
    # Confirms the narrower QA hash really is blind to this change — proof
    # the implement gate needed its own helper rather than reusing it.
    assert qa_after == qa_before


def test_implement_to_review_allows_non_python_product_change(
    tmp_path: Path,
) -> None:
    """Real gate path: a change confined to a curated non-.py product
    surface (hooks/) must register as implementation work (P1)."""
    st = start_autopilot(tmp_path, "non-py product change", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text('{"changed": true}\n', encoding="utf-8")
    transition(tmp_path, rid, "review")
    assert status_autopilot(tmp_path, rid)["phase"] == "review"


def test_stamp_and_read_implementation_receipt_roundtrip(tmp_path: Path) -> None:
    """P2: a real on-disk CLI implementation receipt round-trips and is
    recognized as CLI-writer authoritative."""
    from omg_cli.implementation import (
        read_implementation_receipt,
        stamp_implementation_receipt,
    )

    digest = "0" * 64
    stamped = stamp_implementation_receipt(
        tmp_path, "run-x", content_sha256=digest, note="worker finished"
    )
    assert stamped["writer"] == "omg-cli"
    receipt = read_implementation_receipt(tmp_path, "run-x")
    assert receipt is not None
    assert receipt["writer"] == "omg-cli"
    assert receipt["run_id"] == "run-x"
    assert receipt["content_sha256"] == digest
    assert receipt["note"] == "worker finished"


def test_stamp_implementation_receipt_rejects_malformed_hash(tmp_path: Path) -> None:
    from omg_cli.implementation import stamp_implementation_receipt

    with pytest.raises(ValueError):
        stamp_implementation_receipt(tmp_path, "run-x", content_sha256="not-a-hash")


def test_read_implementation_receipt_rejects_forged_writer(tmp_path: Path) -> None:
    """Fail-closed: a hand-written file claiming to be the receipt but with
    the wrong writer must never be trusted."""
    import json

    from omg_cli.implementation import (
        implementation_receipt_path,
        read_implementation_receipt,
    )

    path = implementation_receipt_path(tmp_path, "run-x")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"writer": "not-omg-cli", "run_id": "run-x", "content_sha256": "0" * 64}
        ),
        encoding="utf-8",
    )
    assert read_implementation_receipt(tmp_path, "run-x") is None


def test_read_implementation_receipt_rejects_run_id_mismatch(tmp_path: Path) -> None:
    """A receipt stamped for one run must not be readable under another."""
    from omg_cli.implementation import (
        read_implementation_receipt,
        stamp_implementation_receipt,
    )

    stamp_implementation_receipt(tmp_path, "run-a", content_sha256="0" * 64)
    assert read_implementation_receipt(tmp_path, "run-b") is None


def test_implement_to_review_accepts_on_disk_cli_receipt_without_break_glass(
    tmp_path: Path,
) -> None:
    """P2: a trusted on-disk CLI implementation receipt satisfies the
    implement→review work gate on its own — no break_glass required."""
    from omg_cli.autopilot import _implement_workspace_fingerprint, load_autopilot
    from omg_cli.implementation import stamp_implementation_receipt

    st = start_autopilot(tmp_path, "cli receipt gate", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    fp = _implement_workspace_fingerprint(tmp_path)
    stamp_implementation_receipt(tmp_path, rid, content_sha256=fp, note="worker finished")
    transition(tmp_path, rid, "review")  # no evidence, no break_glass
    assert status_autopilot(tmp_path, rid)["phase"] == "review"
    history = load_autopilot(tmp_path, rid)["history"]
    assert history[-1].get("gate_audit") == "cli_receipt:implementation.json"


def test_implement_to_review_rejects_stale_on_disk_receipt(tmp_path: Path) -> None:
    """A receipt whose content_sha256 no longer matches the current
    workspace must not satisfy the gate (stale stamp, not proof of work)."""
    from omg_cli.implementation import stamp_implementation_receipt

    st = start_autopilot(tmp_path, "stale cli receipt", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    stamp_implementation_receipt(tmp_path, rid, content_sha256="0" * 64)
    with pytest.raises(AutopilotError, match="implementation|no_change"):
        transition(tmp_path, rid, "review")
    assert status_autopilot(tmp_path, rid)["phase"] == "implement"


def test_implement_reentry_invalidates_stale_receipt_with_unchanged_fingerprint(
    tmp_path: Path,
) -> None:
    """P2 (Codex): a receipt stamped in one implement cycle must not unlock
    review in a later cycle whose fingerprint still matches — e.g.
    review → ralplan → implement with no new product work. The stale
    receipt must be invalidated on the new implement entry, not silently
    accepted just because content_sha256 still equals the (unchanged)
    current fingerprint."""
    from omg_cli.autopilot import _implement_workspace_fingerprint
    from omg_cli.implementation import (
        read_implementation_receipt,
        stamp_implementation_receipt,
    )

    st = start_autopilot(tmp_path, "stale receipt cycle bind", skip_interview=True)
    rid = st["run_id"]

    # Cycle 1: enter implement, do real product work, stamp a matching receipt.
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    (tmp_path / "changed.py").write_text("# product change\n", encoding="utf-8")
    fp1 = _implement_workspace_fingerprint(tmp_path)
    stamp_implementation_receipt(tmp_path, rid, content_sha256=fp1, note="cycle 1 work")
    transition(tmp_path, rid, "review")
    assert status_autopilot(tmp_path, rid)["phase"] == "review"

    # Replan back to ralplan, then re-enter implement with NO new product
    # work — the workspace fingerprint is unchanged from cycle 1.
    transition(tmp_path, rid, "ralplan", reason="replan")
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())

    # The old receipt from cycle 1 must be invalidated by the new implement
    # entry, even though its content_sha256 still equals the current fp.
    assert read_implementation_receipt(tmp_path, rid) is None

    with pytest.raises(AutopilotError, match="implementation|no_change"):
        transition(tmp_path, rid, "review")
    assert status_autopilot(tmp_path, rid)["phase"] == "implement"


def test_implement_workspace_fingerprint_excludes_generated_caches(
    tmp_path: Path,
) -> None:
    """P2 (Codex): __pycache__/*.pyc, .pytest_cache/, .ruff_cache/,
    .mypy_cache/, and *.egg-info/ must never affect the implement-gate
    fingerprint — running tests or importing a module during implement
    writes these with zero durable product change."""
    from omg_cli.autopilot import _implement_workspace_fingerprint

    omg_cli_dir = tmp_path / "omg_cli"
    omg_cli_dir.mkdir()
    (omg_cli_dir / "core.py").write_text("x = 1\n", encoding="utf-8")

    fp_before = _implement_workspace_fingerprint(tmp_path)

    (omg_cli_dir / "__pycache__").mkdir()
    (omg_cli_dir / "__pycache__" / "core.cpython-312.pyc").write_bytes(b"\x00\x01")
    (omg_cli_dir / "core.pyc").write_bytes(b"\x00\x01")
    pytest_cache = omg_cli_dir / ".pytest_cache" / "v" / "cache"
    pytest_cache.mkdir(parents=True)
    (pytest_cache / "lastfailed").write_text("{}", encoding="utf-8")
    (omg_cli_dir / ".ruff_cache").mkdir()
    (omg_cli_dir / ".ruff_cache" / "content").write_text("cache", encoding="utf-8")
    mypy_cache = omg_cli_dir / ".mypy_cache" / "3.12"
    mypy_cache.mkdir(parents=True)
    (mypy_cache / "core.data.json").write_text("{}", encoding="utf-8")
    egg_info = omg_cli_dir / "omg_cli.egg-info"
    egg_info.mkdir()
    (egg_info / "PKG-INFO").write_text("meta", encoding="utf-8")

    fp_after = _implement_workspace_fingerprint(tmp_path)
    assert fp_after == fp_before

    # Sanity: a real source edit is still detected (caches aren't masking
    # everything).
    (omg_cli_dir / "core.py").write_text("x = 2\n", encoding="utf-8")
    fp_changed = _implement_workspace_fingerprint(tmp_path)
    assert fp_changed != fp_before


def test_implement_to_review_blocked_by_only_pycache_change(tmp_path: Path) -> None:
    """Real gate path: writing/updating __pycache__ bytecode alone (e.g. from
    running tests during implement) must not register as implementation
    work (P2)."""
    st = start_autopilot(tmp_path, "pycache noise", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    cache_dir = tmp_path / "omg_cli" / "__pycache__"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "core.cpython-312.pyc").write_bytes(b"\x00\x01\x02")
    with pytest.raises(AutopilotError, match="implementation|no_change"):
        transition(tmp_path, rid, "review")
    assert status_autopilot(tmp_path, rid)["phase"] == "implement"


def test_try_advance_after_launch_stalls_implement_without_work_evidence(
    tmp_path: Path,
) -> None:
    """No silent advance: implement→review gate failure must stay on implement
    and surface a gate_failure, not fall through to review."""
    from omg_cli.autopilot import _try_advance_after_launch

    st = start_autopilot(tmp_path, "impl gate stall", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    out = _try_advance_after_launch(tmp_path, rid, "implement")
    assert out == "implement"
    st2 = status_autopilot(tmp_path, rid)
    assert st2["phase"] == "implement"
    gate = st2.get("gate_failure")
    assert isinstance(gate, dict)
    assert gate.get("phase") == "implement"
    assert gate.get("message")


def test_rework_invalidates_review_stamp(tmp_path: Path) -> None:
    """After rework, a previous clean structured_review must not open QA."""
    from omg_cli.autopilot import stage_review_is_clean

    st = start_autopilot(tmp_path, "rework stamp", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "review", evidence=_ev_no_change_bg())
    _stamp_review_clean(tmp_path, rid)
    assert stage_review_is_clean(tmp_path, rid) is True
    transition(tmp_path, rid, "rework", reason="findings")
    assert stage_review_is_clean(tmp_path, rid) is False
    transition(tmp_path, rid, "review")
    with pytest.raises(AutopilotError, match="structured_review"):
        transition(tmp_path, rid, "qa")
    # Fresh stamp required
    _stamp_review_clean(tmp_path, rid, diff="new-diff-after-rework")
    transition(tmp_path, rid, "qa")


def test_autopilot_save_and_invalidate_use_atomic_write_helper(tmp_path: Path) -> None:
    """``_save`` and ``invalidate_quality_stages`` must reuse
    ``state._atomic_write_json`` (temp file + fsync + os.replace) instead of
    a bare ``path.write_text``, so a crash mid-write can never leave a torn
    autopilot.json or stage stamp — and never leaks its temp file."""
    import json

    import omg_cli.autopilot as autopilot_mod
    from omg_cli.autopilot import autopilot_state_path
    from omg_cli.review import review_state_path
    from omg_cli.state import _atomic_write_json

    assert autopilot_mod._atomic_write_json is _atomic_write_json

    st = start_autopilot(tmp_path, "atomic write", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "review", evidence=_ev_no_change_bg())
    _stamp_review_clean(tmp_path, rid)

    path = autopilot_state_path(tmp_path, rid)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["run_id"] == rid
    assert data["writer"] == "omg-cli"
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []

    transition(tmp_path, rid, "rework", reason="findings")
    review_path = review_state_path(tmp_path, rid)
    review_data = json.loads(review_path.read_text(encoding="utf-8"))
    assert review_data["invalidated"] is True
    assert review_data["writer"] == "omg-cli"
    assert list(review_path.parent.glob(f".{review_path.name}.*.tmp")) == []


def test_stale_review_stamp_rejected_when_diff_hash_drifts(tmp_path: Path) -> None:
    """A structured_review.json whose declared diff_hash no longer matches
    the diff_hash actually approved by its own CLI-stamped lanes must be
    treated as stale, not clean — closes an on-disk tamper/drift gap."""
    st = start_autopilot(tmp_path, "stale review", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "review", evidence=_ev_no_change_bg())
    _stamp_review_clean(tmp_path, rid, diff="original diff")
    # Mutate the stamp's recorded top-level diff_hash so it no longer matches
    # what the nested code_reviewer_stamp/architect_stamp lanes approved.
    from omg_cli.review import review_state_path
    import json

    path = review_state_path(tmp_path, rid)
    data = json.loads(path.read_text())
    data["diff_hash"] = "0" * 64  # force mismatch vs recomputed
    path.write_text(json.dumps(data, indent=2) + "\n")
    with pytest.raises(AutopilotError, match="stale|fingerprint|diff_hash"):
        transition(tmp_path, rid, "qa")


def test_stale_qa_stamp_rejected_when_product_hash_drifts(tmp_path: Path) -> None:
    """An ultraqa.json clean stamp whose recorded product_hash no longer
    matches the current workspace must be treated as stale, not clean."""
    st = start_autopilot(tmp_path, "stale qa", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "review", evidence=_ev_no_change_bg())
    _stamp_review_clean(tmp_path, rid)
    transition(tmp_path, rid, "qa")
    _stamp_qa_clean(tmp_path, rid)
    from omg_cli.qa import qa_state_path
    import json

    path = qa_state_path(tmp_path, rid)
    data = json.loads(path.read_text())
    data["cycles"][-1]["product_hash"] = "0" * 64  # force mismatch vs recomputed
    path.write_text(json.dumps(data, indent=2) + "\n")
    with pytest.raises(AutopilotError, match="stale|fingerprint|product_hash"):
        transition(tmp_path, rid, "acceptance")


def test_review_stamp_rejected_when_diff_hash_present_but_lane_stamps_stripped(
    tmp_path: Path,
) -> None:
    """diff_hash present + clean=true but code_reviewer_stamp/architect_stamp
    stripped/malformed must fail closed, not fall back to legacy
    clean-flag-only trust — closes a downgrade gap where an attacker (or a
    buggy writer) drops the lane stamps while keeping the top-level fields
    that used to be sufficient on their own."""
    from omg_cli.autopilot import stage_review_is_clean
    from omg_cli.review import review_state_path
    import json

    st = start_autopilot(tmp_path, "stripped lane stamps", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "review", evidence=_ev_no_change_bg())
    _stamp_review_clean(tmp_path, rid)
    assert stage_review_is_clean(tmp_path, rid) is True

    path = review_state_path(tmp_path, rid)
    data = json.loads(path.read_text())
    assert data.get("diff_hash")
    data["code_reviewer_stamp"] = None
    data["architect_stamp"] = None
    path.write_text(json.dumps(data, indent=2) + "\n")

    assert stage_review_is_clean(tmp_path, rid) is False
    with pytest.raises(AutopilotError, match="structured_review"):
        transition(tmp_path, rid, "qa")


def test_legacy_v1_refused(tmp_path: Path) -> None:
    run = create_run(tmp_path, mode="autopilot", goal="legacy")
    with pytest.raises(AutopilotError):
        transition(tmp_path, run["run_id"], "interview")


def test_blocked_implement_roundtrip_invalidates_stale_stamps(tmp_path: Path) -> None:
    """qa→blocked→implement→blocked→qa must NOT reuse the stale clean review
    stamp — re-entering implement produces new, unreviewed code."""
    st = start_autopilot(tmp_path, "roundtrip", skip_interview=True)
    rid = st["run_id"]
    # Reach a clean qa the legitimate way.
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "review", evidence=_ev_no_change_bg())
    _stamp_review_clean(tmp_path, rid)
    transition(tmp_path, rid, "qa")
    _stamp_qa_clean(tmp_path, rid)
    # Detour that used to smuggle new code past review/QA:
    transition(tmp_path, rid, "blocked", reason="infra hiccup")
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "blocked", reason="another hiccup")
    # The qa gate must now reject: the review stamp was invalidated on implement.
    with pytest.raises(AutopilotError, match="review"):
        transition(tmp_path, rid, "qa")


def test_set_awaiting_mirrors_flag_into_status(tmp_path: Path) -> None:
    st = start_autopilot(tmp_path, "vague", skip_interview=False)
    set_awaiting_confirmation(
        tmp_path, st["run_id"], True, reason="interview:waiting_input"
    )
    run = load_run(tmp_path, st["run_id"])
    assert run is not None
    assert run["autopilot_awaiting"] is True
    assert run["autopilot_awaiting_reason"] == "interview:waiting_input"


def test_clear_awaiting(tmp_path: Path) -> None:
    st = start_autopilot(tmp_path, "vague", skip_interview=False)
    rid = st["run_id"]
    set_awaiting_confirmation(tmp_path, rid, True, reason="interview:waiting_input")
    set_awaiting_confirmation(tmp_path, rid, False)
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("autopilot_awaiting") is False
    assert run.get("autopilot_awaiting_reason") == ""


def test_set_awaiting_never_touches_verified(tmp_path: Path) -> None:
    st = start_autopilot(tmp_path, "vague", skip_interview=False)
    rid = st["run_id"]
    set_awaiting_confirmation(tmp_path, rid, True, reason="permission:destructive")
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("verified") is not True
    assert run.get("status") not in ("verified", "cancelled", "completed")
    assert run.get("autopilot_phase") == "interview"


def test_cli_autopilot_await_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st = start_autopilot(tmp_path, "vague", skip_interview=False)
    rid = st["run_id"]
    monkeypatch.chdir(tmp_path)
    rc = main(["autopilot", "await", "--run", rid, "--reason", "cli:pause"])
    assert rc == 0
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("autopilot_awaiting") is True
    assert run.get("autopilot_awaiting_reason") == "cli:pause"
    rc_clear = main(
        ["autopilot", "await", "--run", rid, "--clear", "--reason", "should-ignore"]
    )
    assert rc_clear == 0
    run2 = load_run(tmp_path, rid)
    assert run2 is not None
    assert run2.get("autopilot_awaiting") is False
    assert run2.get("autopilot_awaiting_reason") == ""


def test_set_awaiting_allows_stop_gate(tmp_path: Path) -> None:
    st = start_autopilot(tmp_path, "vague", skip_interview=False)
    rid = st["run_id"]
    event = {"reason": "end_turn", "stopHookActive": False, "backgroundTasks": []}
    assert decide_stop(tmp_path, event) is not None
    set_awaiting_confirmation(tmp_path, rid, True, reason="interview:waiting_input")
    assert decide_stop(tmp_path, event) is None


def test_qa_blocked_review_roundtrip_invalidates_review_stamp(tmp_path: Path) -> None:
    """qa→blocked→review must invalidate the prior clean review stamp so a
    later qa entry cannot reuse it without a fresh structured_review."""
    from omg_cli.autopilot import stage_review_is_clean

    st = start_autopilot(tmp_path, "qa-blocked-review", skip_interview=True)
    rid = st["run_id"]
    # Reach a clean qa the legitimate way.
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "review", evidence=_ev_no_change_bg())
    _stamp_review_clean(tmp_path, rid)
    assert stage_review_is_clean(tmp_path, rid) is True
    transition(tmp_path, rid, "qa")
    # Detour that re-enters review without new product code, but still must
    # not reopen qa on a pre-block stamp.
    transition(tmp_path, rid, "blocked", reason="ops hiccup")
    transition(tmp_path, rid, "review")
    assert stage_review_is_clean(tmp_path, rid) is False
    with pytest.raises(AutopilotError, match="review"):
        transition(tmp_path, rid, "qa")
    # Fresh stamp required after invalidation.
    _stamp_review_clean(tmp_path, rid, diff="new-diff-after-blocked-review")
    transition(tmp_path, rid, "qa")


def _rid(root: Path) -> str:
    run = load_active_run(root)
    assert run is not None
    return str(run["run_id"])


def _stamp_gate_for(root: Path, kw: dict) -> int:
    """Simulate grok completing the current phase gate (test helper)."""
    run_dir = kw["run_dir"]
    run_id = run_dir.name
    phase = status_autopilot(root, run_id)["phase"]
    if phase == "ralplan":
        merge_status_fields(root, run_id, {"ralplan_consensus": True})
    elif phase == "implement":
        # Simulate grok producing a real product change so implement→review
        # has workspace-fingerprint evidence of work.
        (root / f"_autopilot_work_{run_id}.py").write_text(
            "# simulated implementation work\n", encoding="utf-8"
        )
    elif phase == "review":
        _stamp_review_clean(root, run_id)
    elif phase == "qa":
        _stamp_qa_clean(root, run_id, tmp_path=root)
    return 0


def test_autopilot_context_pack_names_phase_and_gate() -> None:
    pack = autopilot_context_pack(
        run_id="r1",
        phase="review",
        goal="g",
        next_gate="CLI stages/structured_review.json clean",
    )
    assert "phase=review" in pack and "structured_review.json" in pack


def test_build_phase_prompt_maps_skill_and_forbids_questions(tmp_path: Path) -> None:
    text = build_phase_prompt("implement", root=tmp_path, goal="g", run_id="r1")
    assert "ultrawork" in text.lower() or "implement" in text.lower()
    assert "do not ask" in text.lower()


def test_build_phase_prompt_ralplan_binds_to_autopilot_run(tmp_path: Path) -> None:
    text = build_phase_prompt("ralplan", root=tmp_path, goal="g", run_id="ap-run-9")
    assert "Autopilot-bound ralplan" in text
    assert "--run ap-run-9" in text
    assert "Do **not** edit `.omg/state/`" in text
    assert "accepted: true" in text  # forbidden forge called out
    assert "ralplan-consensus-ap-run-9.json" not in text
    assert "Do **not** start a standalone" in text or "do **not** start a standalone" in text.lower()


def test_consensus_ready_ignores_artifact_marker(tmp_path: Path) -> None:
    from omg_cli.autopilot import _consensus_ready

    st = start_autopilot(tmp_path, "artifact alone", skip_interview=True)
    rid = st["run_id"]
    marker = tmp_path / ".omg" / "artifacts" / f"ralplan-consensus-{rid}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    assert _consensus_ready(tmp_path, rid) is False
    merge_status_fields(tmp_path, rid, {"ralplan_consensus": True})
    assert _consensus_ready(tmp_path, rid) is True


def test_try_advance_after_launch_skips_when_implement_became_blocked(
    tmp_path: Path,
) -> None:
    """Stale phase=implement must not force review after launch left blocked."""
    from omg_cli.autopilot import _try_advance_after_launch

    st = start_autopilot(tmp_path, "block mid implement", skip_interview=True)
    rid = st["run_id"]
    merge_status_fields(tmp_path, rid, {"ralplan_consensus": True})
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "blocked", reason="ops")
    assert status_autopilot(tmp_path, rid)["phase"] == "blocked"
    out = _try_advance_after_launch(tmp_path, rid, "implement")
    assert out == "blocked"
    assert status_autopilot(tmp_path, rid)["phase"] == "blocked"


def test_run_autopilot_walks_to_verified_with_mocked_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_cli_acceptance_tokens()
    launches: list[dict] = []

    def _fake_launch(argv, **kw):
        launches.append({**kw, "argv": argv})
        _stamp_gate_for(tmp_path, kw)
        return 0

    monkeypatch.setattr("omg_cli.modes._launch_grok", _fake_launch)
    rc = run_autopilot(
        tmp_path, "add pure add(a,b) with test", skip_interview=True
    )
    assert rc == 0
    assert status_autopilot(tmp_path, _rid(tmp_path))["phase"] == "verified"
    assert launches


def test_run_autopilot_pauses_at_interview(tmp_path: Path) -> None:
    rc = run_autopilot(tmp_path, "vague idea")
    assert rc == 0
    assert status_autopilot(tmp_path, _rid(tmp_path))["phase"] == "interview"


def test_run_autopilot_pauses_when_awaiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    st = start_autopilot(tmp_path, "ship it", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "review", evidence=_ev_no_change_bg())
    set_awaiting_confirmation(tmp_path, rid, True, reason="cli:pause")
    launched: list[bool] = []

    def _fake_launch(argv, **kw):
        launched.append(True)
        return 0

    monkeypatch.setattr("omg_cli.modes._launch_grok", _fake_launch)
    rc = run_autopilot(tmp_path, "", resume_run_id=rid)
    assert rc == 0
    assert not launched
    captured = capsys.readouterr()
    err = captured.err
    assert f"omg autopilot await --clear --run {rid}" in err
    assert f"omg autopilot run --resume {rid}" in err
    assert err.index(f"omg autopilot await --clear --run {rid}") < err.index(
        f"omg autopilot run --resume {rid}"
    )
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["pause"] == "awaiting"
    assert payload["run_id"] == rid
    assert f"--resume {rid}" in payload["resume_command"]
    assert (tmp_path / ".omg" / "state" / "RESUME.md").is_file()


def test_run_resume_reenters_current_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st = start_autopilot(tmp_path, "resume goal", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "review", evidence=_ev_no_change_bg())
    phases_seen: list[str] = []

    def _fake_launch(argv, **kw):
        run_id = kw["run_dir"].name
        phases_seen.append(status_autopilot(tmp_path, run_id)["phase"])
        return 0

    monkeypatch.setattr("omg_cli.modes._launch_grok", _fake_launch)
    rc = run_autopilot(tmp_path, "", resume_run_id=rid)
    assert rc == 0
    assert phases_seen == ["review"]
    assert status_autopilot(tmp_path, rid)["phase"] == "review"

    phases_seen.clear()
    rc2 = run_autopilot(tmp_path, "", resume_run_id=rid)
    assert rc2 == 0
    assert phases_seen == ["review"]
    assert status_autopilot(tmp_path, rid)["phase"] == "review"


def test_cli_autopilot_run_listed_in_skills_md() -> None:
    assert "omg autopilot run" in (ROOT / "docs" / "skills.md").read_text()


def test_run_autopilot_unattended_relaunches_on_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#40: unattended re-launches same phase without human go."""
    st = start_autopilot(tmp_path, "keep going", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "review", evidence=_ev_no_change_bg())
    launches = 0

    def _fake_launch(argv, **kw):
        nonlocal launches
        launches += 1
        # Never advance phase → stall path.
        return 0

    monkeypatch.setattr("omg_cli.modes._launch_grok", _fake_launch)
    rc = run_autopilot(
        tmp_path,
        "",
        resume_run_id=rid,
        unattended=True,
        max_stall_relaunches=3,
    )
    assert rc == 1
    assert launches == 4  # initial + 3 stall re-launches then block
    assert status_autopilot(tmp_path, rid)["phase"] == "blocked"


def test_run_autopilot_unattended_still_pauses_on_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    st = start_autopilot(tmp_path, "pause me", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "review", evidence=_ev_no_change_bg())
    set_awaiting_confirmation(tmp_path, rid, True, reason="need human")
    launches = 0

    def _fake_launch(argv, **kw):
        nonlocal launches
        launches += 1
        return 0

    monkeypatch.setattr("omg_cli.modes._launch_grok", _fake_launch)
    rc = run_autopilot(
        tmp_path, "", resume_run_id=rid, unattended=True, max_stall_relaunches=5
    )
    assert rc == 0
    assert launches == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pause"] == "awaiting"
    assert "--unattended" in payload["resume_command"]


def test_cli_autopilot_run_unattended_flag() -> None:
    """Argparse surfaces --unattended for outer loop (#40)."""
    from omg_cli.main import build_parser

    p = build_parser()
    ns = p.parse_args(
        ["autopilot", "run", "--resume", "r1", "--unattended", "--max-stall-relaunches", "4"]
    )
    assert ns.unattended is True
    assert ns.max_stall_relaunches == 4
