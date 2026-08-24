"""Visual command family lives under omg_cli.commands.visual (#75)."""

from __future__ import annotations

import argparse

from omg_cli.commands import visual as visual_cmds
from omg_cli.main import build_parser


def _subparser_choices(parser: argparse.ArgumentParser, dest_cmd: str) -> set[str]:
    for act in parser._actions:
        if isinstance(act, argparse._SubParsersAction):
            top = act.choices
            if dest_cmd in top:
                for a2 in top[dest_cmd]._actions:
                    if isinstance(a2, argparse._SubParsersAction):
                        return set(a2.choices.keys())
    return set()


def test_parser_wires_visual_handlers() -> None:
    parser = build_parser()
    samples = {
        "compare": ["visual", "compare", "--input", "x.json"],
        "capture": ["visual", "capture", "--config", "visual.yaml"],
        "verdict": ["visual", "verdict", "--reference", "a.png", "--actual", "b.png"],
        "ralph": ["visual", "ralph", "--config", "visual.yaml"],
        "overlay": ["visual", "overlay", "--reference", "a.png", "--candidate", "b.png"],
    }
    for name, argv in samples.items():
        ns = parser.parse_args(argv)
        assert callable(getattr(ns, "func", None)), name
        assert ns.func.__module__ == "omg_cli.commands.visual", name
        assert ns.visual_action == name


def test_visual_actions_include_overlay() -> None:
    choices = _subparser_choices(build_parser(), "visual")
    assert choices == {"compare", "capture", "verdict", "ralph", "overlay"}
    assert visual_cmds.CMD_OVERLAY == "visual.overlay"
