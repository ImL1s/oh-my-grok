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


def test_parse_request_changes():
    assert parse_verdict("REQUEST CHANGES: fix tests") == "REQUEST_CHANGES"
    assert parse_verdict('{"verdict": "REQUEST CHANGES"}') == "REQUEST_CHANGES"
    # REQUEST CHANGES wins over co-present soft language
    assert parse_verdict("Do not APPROVE yet. REQUEST CHANGES.") == "REQUEST_CHANGES"


def test_parse_failed():
    assert parse_verdict("FAILED: cannot proceed") == "FAILED"
    assert parse_verdict("APPROVE\nFAILED") == "FAILED"  # safer priority


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
    assert verdict == "APPROVE"  # dry_run stub
    active = load_active_run(tmp_path)
    assert active is not None
    assert active.get("verified") is False
    data = load_run(tmp_path, active["run_id"])
    assert data.get("verified") is False
    # prompts exist
    rid = active["run_id"]
    assert stage_prompt_path(tmp_path, rid, "critic", 1).is_file()
    assert stage_prompt_path(tmp_path, rid, "verifier", 1).is_file()


def test_dry_run_cli_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    from omg_cli.dual_review import run_dual_review_cli

    rc = run_dual_review_cli("x", root=tmp_path, dry_run=True)
    assert rc == 0


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
