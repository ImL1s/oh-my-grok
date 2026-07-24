"""Anti-drift guard: command lists in docs must match real omg argparse choices.

Root cause of the v0.3.x `omg goal start`/`omg goal complete` doc drift: no CI
check cross-validated documented subcommands against the actual parser. This
test closes that permanently by introspecting build_parser().

Coverage: every top-level command that registers a subparser (sub-actions), not
only `goal`. Flag-only commands (ralph, ulw, ask, …) are skipped.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from omg_cli.main import build_parser

ROOT = Path(__file__).resolve().parents[1]
DOCS = (
    ROOT / "docs" / "skills.md",
    ROOT / "docs" / "skills.zh.md",
    ROOT / "docs" / "skills.zh-TW.md",
)


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


def _commands_with_subactions(
    parser: argparse.ArgumentParser,
) -> dict[str, set[str]]:
    """Map every top-level command that has a nested subparser to its choices.

    Flag-only commands (no nested subparser) are omitted.
    """
    out: dict[str, set[str]] = {}
    for act in parser._actions:
        if isinstance(act, argparse._SubParsersAction):
            for cmd in act.choices:
                choices = _subparser_choices(parser, cmd)
                if choices:
                    out[cmd] = choices
    return out


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


def test_docs_all_subcommands_are_real() -> None:
    """Every documented `omg <cmd> a|b|c` token must be a real sub-action.

    Applies to all top-level commands that expose a nested subparser via
    build_parser() (interview, goal, worker, wiki, lsp, autopilot, qa, …).
    Commands without sub-actions are skipped.
    """
    parser = build_parser()
    cmds = _commands_with_subactions(parser)
    assert cmds, "expected at least one top-level command with sub-actions"
    # sanity: the known nested-subparser commands from build_parser()
    for expected in (
        "interview",
        "goal",
        "worker",
        "wiki",
        "lsp",
        "autopilot",
        "qa",
        "session",
        "memory",
        "tracker",
        "compact",
        "notify",
        "workflow",
        "parity",
    ):
        assert expected in cmds, f"missing expected sub-actioned command: {expected}"

    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for cmd, choices in sorted(cmds.items()):
            documented = _documented_actions(text, cmd)
            if not documented:
                continue
            unknown = documented - choices
            assert not unknown, (
                f"{doc.name} documents non-existent `omg {cmd}` subcommands: "
                f"{sorted(unknown)} (real choices: {sorted(choices)})"
            )


def test_docs_describe_host_owned_lsp_without_semantic_proxy() -> None:
    """The LSP guide must not revive the removed AST/pyright proxy contract."""
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        assert "semantic_proxy_count: 0" in text
        assert "semantic_proxy_unsupported" in text
        assert "exit code 1" in text
        assert "host-owned" in text
        assert "omg_lsp_symbols" not in text
        assert "omg_lsp_diagnostics" not in text
        assert "stdlib `ast`" not in text
