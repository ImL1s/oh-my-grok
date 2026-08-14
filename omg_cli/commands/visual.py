"""omg visual — Visual Contract V1 CLI (#75).

Commands: ``omg visual compare``. Parser construction: ``register_visual_parsers``.

Wraps :func:`omg_cli.contracts.visual_contract.compare` only. Does not decode
images, talk to agents or providers, write ``passes`` / ``verified``, or
decide pass/fail from ``aggregate`` vs ``threshold``.
"""

from __future__ import annotations

import argparse
import sys

from omg_cli.cli_envelope import emit_json, failure, success
from omg_cli.cli_util import read_json_path
from omg_cli.contracts.visual_contract import VisualContractError, compare


CMD_COMPARE = "visual.compare"


def cmd_visual(args: argparse.Namespace) -> int:
    """Dispatch ``visual <action>``."""
    action = getattr(args, "visual_action", None)
    if action == "compare":
        return _cmd_visual_compare(args)
    print("usage: omg visual {compare}", file=sys.stderr)
    return 2


def _cmd_visual_compare(args: argparse.Namespace) -> int:
    input_path = getattr(args, "input", None)
    if not input_path:
        emit_json(
            failure(
                CMD_COMPARE,
                "E_USAGE",
                "require --input PATH",
                next_action="Pass --input PATH to a Visual Contract V1 JSON document",
            )
        )
        return 2

    try:
        document = read_json_path(input_path, label="visual compare input")
    except ValueError as exc:
        emit_json(
            failure(
                CMD_COMPARE,
                "E_VISUAL_INPUT",
                str(exc),
                next_action="Provide a readable JSON object at --input",
            )
        )
        return 2

    try:
        result = compare(document)
    except VisualContractError as exc:
        emit_json(
            failure(
                CMD_COMPARE,
                "E_VISUAL_CONTRACT",
                str(exc),
                next_action="Fix the comparison document per docs/visual-contract-v1.md",
            )
        )
        return 2

    emit_json(success(CMD_COMPARE, result=result))
    return 0


def register_visual_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    """Register ``visual`` inspect-family parsers (#75)."""
    p_visual = sub.add_parser(
        "visual",
        parents=[common],
        help="visual contract compare (scored/blocked; #75)",
    )
    vis_sub = p_visual.add_subparsers(dest="visual_action")
    p_compare = vis_sub.add_parser(
        "compare",
        parents=[common],
        help=(
            "wrap compare() on a Visual Contract V1 JSON document; "
            "does not decode images or write passes/verified"
        ),
    )
    p_compare.add_argument(
        "--input",
        default=None,
        help="path to omg.visual.comparison JSON document",
    )
    p_compare.set_defaults(func=cmd_visual, visual_action="compare")
    p_visual.set_defaults(func=cmd_visual)


__all__ = [
    "cmd_visual",
    "register_visual_parsers",
]
