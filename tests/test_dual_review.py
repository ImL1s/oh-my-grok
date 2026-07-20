"""Tests for Grok-native dual-review — verdict parse, stage order, no verified."""
from __future__ import annotations

import subprocess

from omg_cli.dual_review import (
    build_dual_prompt,
    parse_verdict,
    run_dual_review,
    stage_artifact_path,
    stage_prompt_path,
)
from omg_cli.state import load_active_run, load_run


def test_parse_approve_whole_word():
    assert parse_verdict("## Verdict\nAPPROVE\n") == "APPROVE"
    assert parse_verdict('{"verdict": "APPROVE"}') == "APPROVE"
    assert parse_verdict("we approve this") == "UNKNOWN"  # case-sensitive word
    # free-floating body mention is not terminal APPROVE
    assert parse_verdict("mention APPROVE in prose only\n") == "UNKNOWN"
    assert parse_verdict("Do not APPROVE this yet.\n") == "UNKNOWN"


def test_parse_request_changes():
    assert parse_verdict("REQUEST CHANGES: fix tests") == "REQUEST_CHANGES"
    assert parse_verdict('{"verdict": "REQUEST CHANGES"}') == "REQUEST_CHANGES"
    # REQUEST CHANGES wins over co-present soft language
    assert parse_verdict("Do not APPROVE yet. REQUEST CHANGES.") == "REQUEST_CHANGES"


def test_parse_failed():
    assert parse_verdict("FAILED: cannot proceed") == "FAILED"
    assert parse_verdict("APPROVE\nFAILED") == "FAILED"  # safer priority


def test_rc_fail_closed_blocks_approve(monkeypatch, tmp_path):
    """Non-zero verifier exit must not leave APPROVE (Codex P0)."""
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )

    def exec_stage(role, **kwargs):
        root = kwargs["root"]
        rid = kwargs["run_id"]
        rn = kwargs["round_n"]
        path = stage_artifact_path(root, rid, role, rn)
        path.parent.mkdir(parents=True, exist_ok=True)
        if role == "verifier":
            path.write_text("## Verdict\nAPPROVE\n", encoding="utf-8")
            return 127  # missing binary / launch failure
        path.write_text("findings: none\n", encoding="utf-8")
        return 0

    verdict = run_dual_review(
        "review",
        root=tmp_path,
        dry_run=True,
        stage_executor=exec_stage,
    )
    assert verdict != "APPROVE"
    assert verdict == "FAILED"


def test_stage_order_critic_before_verifier(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    order: list[str] = []

    def exec_stage(role, **kwargs):
        order.append(role)
        # write minimal artifacts
        root = kwargs["root"]
        rid = kwargs["run_id"]
        rn = kwargs["round_n"]
        path = stage_artifact_path(root, rid, role, rn)
        path.parent.mkdir(parents=True, exist_ok=True)
        if role == "verifier":
            path.write_text("APPROVE\n", encoding="utf-8")
        else:
            path.write_text("findings: none major\n", encoding="utf-8")
        # also write prompt via default path check
        return 0

    verdict = run_dual_review(
        "review README",
        root=tmp_path,
        dry_run=True,
        stage_executor=exec_stage,
    )
    assert verdict == "APPROVE"
    assert order == ["critic", "verifier"]


def test_read_only_flags_in_prompt():
    from pathlib import Path

    text = build_dual_prompt(
        "critic", "goal X", run_id="r1", round_n=1
    )
    assert "read-only" in text.lower() or "READ-ONLY" in text
    assert "goal X" in text
    assert "verified" in text.lower()

    v = build_dual_prompt(
        "verifier",
        "goal Y",
        run_id="r1",
        round_n=1,
        critic_artifact=Path("/tmp/critic.md"),
    )
    assert "APPROVE" in v
    assert "goal Y" in v


def test_does_not_set_verified(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    verdict = run_dual_review(
        "independent review",
        root=tmp_path,
        dry_run=True,
    )
    # dry_run stub must NOT contain APPROVE — honest NEEDS_REVIEW → UNKNOWN
    assert verdict in ("UNKNOWN", "NEEDS_REVIEW", "REQUEST_CHANGES")
    assert verdict != "APPROVE"
    active = load_active_run(tmp_path)
    assert active is not None
    assert active.get("verified") is False
    data = load_run(tmp_path, active["run_id"])
    assert data.get("verified") is False
    # prompts exist
    rid = active["run_id"]
    assert stage_prompt_path(tmp_path, rid, "critic", 1).is_file()
    assert stage_prompt_path(tmp_path, rid, "verifier", 1).is_file()
    # stub text must not contain whole-word APPROVE
    art = stage_artifact_path(tmp_path, rid, "verifier", 1)
    assert art.is_file()
    text = art.read_text(encoding="utf-8")
    assert "APPROVE" not in text
    assert "NEEDS_REVIEW" in text or "UNKNOWN" in text


def test_dry_run_cli_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    from omg_cli.dual_review import run_dual_review_cli

    # dry_run without APPROVE → non-zero (same honesty as ralplan dry_run)
    rc = run_dual_review_cli("x", root=tmp_path, dry_run=True)
    assert rc == 1


def test_dual_review_argv_disallows_shell(monkeypatch, tmp_path):
    """Critic/verifier launches inject --disallowed-tools run_terminal_command."""
    import json

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    run_dual_review("shell clamp", root=tmp_path, dry_run=True)
    active = load_active_run(tmp_path)
    assert active is not None
    rid = active["run_id"]
    stages = tmp_path / ".omg" / "state" / "runs" / rid / "stages"
    for role in ("critic", "verifier"):
        argv_path = stages / f"dual-{role}-01.argv.json"
        assert argv_path.is_file(), role
        argv = json.loads(argv_path.read_text(encoding="utf-8"))
        assert "--disallowed-tools" in argv
        assert "run_terminal_command" in argv[argv.index("--disallowed-tools") + 1]


def test_dual_review_ro_stages_ignore_yolo(monkeypatch, tmp_path):
    """yolo=True must not inject bypassPermissions on critic/verifier argv."""
    import json

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    run_dual_review("yolo clamp", root=tmp_path, dry_run=True, yolo=True)
    active = load_active_run(tmp_path)
    assert active is not None
    rid = active["run_id"]
    stages = tmp_path / ".omg" / "state" / "runs" / rid / "stages"
    for role in ("critic", "verifier"):
        argv_path = stages / f"dual-{role}-01.argv.json"
        argv = json.loads(argv_path.read_text(encoding="utf-8"))
        joined = " ".join(argv)
        assert "bypassPermissions" not in joined, role
        assert "--always-approve" not in argv, role
        assert "--permission-mode" in argv
        assert argv[argv.index("--permission-mode") + 1] == "plan"


def test_require_native_gate_exits_2(monkeypatch, tmp_path):
    """OMG_DUAL_REVIEW_REQUIRE_NATIVE=1 refuses sequential headless path."""
    monkeypatch.setenv("OMG_DUAL_REVIEW_REQUIRE_NATIVE", "1")
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    from omg_cli.dual_review import run_dual_review_cli

    import pytest

    with pytest.raises(RuntimeError, match="OMG_DUAL_REVIEW_REQUIRE_NATIVE"):
        run_dual_review("native only", root=tmp_path, dry_run=True)
    rc = run_dual_review_cli("native only", root=tmp_path, dry_run=True)
    assert rc == 2
