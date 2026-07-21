"""Anti-drift guard: command lists in docs must match real omg argparse choices.

Root cause of the v0.3.x `omg goal start`/`omg goal complete` doc drift: no CI
check cross-validated documented subcommands against the actual parser. This
test closes that permanently by introspecting build_parser().
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from omg_cli.main import build_parser

ROOT = Path(__file__).resolve().parents[1]
DOCS = (ROOT / "docs" / "skills.md", ROOT / "docs" / "skills.zh-Hant.md")


def _subparser_choices(parser: argparse.ArgumentParser, dest_cmd: str) -> set[str]:
    """Return the set of sub-actions registered under top-level *dest_cmd*."""
    for act in parser._actions:
        if isinstance(act, argparse._SubParsersAction):
            top = act.choices
            if dest_cmd in top:
                for a2 in top[dest_cmd]._actions:
                    if isinstance(a2, argparse._SubParsersAction):
                        return set(a2.choices.keys())
    return set()


def test_goal_subcommands_exist() -> None:
    choices = _subparser_choices(build_parser(), "goal")
    assert "start-story" in choices
    assert "complete-story" in choices
    # the historical doc drift referenced these non-existent actions:
    assert "start" not in choices
    assert "complete" not in choices


def _documented_actions(text: str, cmd: str) -> set[str]:
    """Extract every documented `omg <cmd> a|b|c` subcommand (pipes may be
    markdown-escaped as ``\\|``)."""
    out: set[str] = set()
    for m in re.finditer(rf"omg {re.escape(cmd)} ([a-z0-9|\\\-]+)", text):
        raw = m.group(1).replace("\\", "")
        for tok in raw.split("|"):
            tok = tok.strip()
            if tok and tok != "*":
                out.add(tok)
    return out


def test_docs_goal_actions_are_real() -> None:
    choices = _subparser_choices(build_parser(), "goal")
    for doc in DOCS:
        documented = _documented_actions(doc.read_text(encoding="utf-8"), "goal")
        unknown = documented - choices
        assert not unknown, (
            f"{doc.name} documents non-existent `omg goal` subcommands: "
            f"{sorted(unknown)} (real choices: {sorted(choices)})"
        )
