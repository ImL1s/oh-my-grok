"""Inventory checks for plugin skills (session playbooks)."""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"


def _skill(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def test_required_skills_exist():
    for name in (
        "omg-autopilot",
        "omg-using",
        "omg-ralph",
        "omg-ultrawork",
        "omg-ralplan",
        "omg-ultragoal",
        "omg-deep-interview",
        "omg-ultraqa",
        "omg-wiki",
        "omg-hud",
        "omg-lsp",
    ):
        assert (SKILLS / name / "SKILL.md").is_file(), name


def test_omg_autopilot_is_session_playbook_not_stub():
    text = _skill("omg-autopilot")
    assert "name: omg-autopilot" in text
    assert "description:" in text
    # body must be substantial (was ~33 lines)
    assert text.count("\n") >= 120, "autopilot skill still too thin for in-session use"
    for needle in (
        "HARD RULES",
        "Use when",
        "Do not use when",
        "interview",
        "ralplan",
        "implement",
        "review",
        "qa",
        "acceptance",
        "spawn_subagent",
        "capability_mode",
        "omg autopilot start",
        "omg autopilot transition",
        "omg accept",
        "omg autopilot complete",
        "verified",
        "Stop",
    ):
        assert needle in text, f"missing {needle!r}"


def test_omg_using_routes_autopilot():
    text = _skill("omg-using")
    assert "omg-autopilot" in text
    low = text.lower()
    assert "autopilot" in low
    # at least one common power-user trigger
    assert any(
        t in low for t in ("build me", "full auto", "autonomous", "handle it all")
    )


def test_omg_ultragoal_is_session_playbook_not_stub():
    text = _skill("omg-ultragoal")
    assert "name: omg-ultragoal" in text
    assert text.count("\n") >= 100
    for needle in (
        "HARD RULES",
        "Use when",
        "Do not use when",
        "omg goal init",
        "checkpoint",
        "link-run",
        "verify",
        "spawn_subagent",
        "no host",
        "/goal",
    ):
        assert needle.lower() in text.lower() or needle in text, f"missing {needle!r}"


def test_omg_using_routes_ultragoal():
    text = _skill("omg-using")
    assert "omg-ultragoal" in text
    low = text.lower()
    assert "ultragoal" in low or "omg goal" in low


def test_omg_using_routes_resume_and_lifestyle():
    text = _skill("omg-using")
    low = text.lower()
    assert "resume.md" in low
    assert "omg resume" in low
    assert "omg-wiki" in text or "omg wiki" in low
    assert "omg-ultraqa" in text or "ultraqa" in low
    assert "omg-deep-interview" in text or "deep interview" in low


def test_omg_ultraqa_and_interview_not_stubs():
    uq = _skill("omg-ultraqa")
    di = _skill("omg-deep-interview")
    assert uq.count("\n") >= 80
    assert di.count("\n") >= 80
    assert "QA clean" in uq or "verified" in uq.lower()
    assert "pressure-pass" in di or "pressure pass" in di.lower()
