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
