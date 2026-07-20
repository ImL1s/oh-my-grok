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
