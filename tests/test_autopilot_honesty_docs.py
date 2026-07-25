"""Docs honesty: Stop pin supersession and autopilot skill/rules alignment."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_skill_no_longer_claims_stop_nonblocking():
    body = (ROOT / "skills/omg-autopilot/SKILL.md").read_text(encoding="utf-8")
    assert "No Stop hard-pin" not in body
    assert "8" in body and "cap" in body.lower()
    assert "ask_user_question" in body


def test_adr_is_superseded():
    adr = (
        ROOT / "docs/research/stop-continuation/stop-continuation-decision.md"
    ).read_text(encoding="utf-8")
    assert "SUPERSEDED" in adr and "0.2.107" in adr


def test_rules_forbid_midphase_questions():
    rules = (ROOT / "templates/omg-rules.md").read_text(encoding="utf-8")
    assert "do not ask" in rules.lower()


def test_claude_md_stop_honesty():
    body = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Stop is passive" not in body
    assert "No OMC-style Stop hard-pin" not in body
    assert "stop.py is passive" not in body
    assert "≥0.2.107" in body


def test_skills_hard_rule_stop_honesty():
    for rel in ("docs/skills.md", "docs/skills.zh.md", "docs/skills.zh-TW.md"):
        body = (ROOT / rel).read_text(encoding="utf-8")
        assert "No OMC Stop hard-pin" not in body
        assert "OMC 式 Stop hard-pin" not in body
