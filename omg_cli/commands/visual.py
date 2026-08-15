"""omg visual — Visual Contract V1 CLI (#75).

Commands: ``omg visual compare``. Parser construction: ``register_visual_parsers``.

Wraps :func:`omg_cli.contracts.visual_contract.compare` only. Does not decode
images, talk to agents or providers, write ``passes`` / ``verified``, or
decide pass/fail from ``aggregate`` vs ``threshold``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Final

from omg_cli.cli_envelope import emit_json, failure, success
from omg_cli.contracts.visual_contract import VisualContractError, compare


CMD_COMPARE = "visual.compare"
MAX_COMPARE_DOCUMENT_BYTES: Final[int] = 1 * 1024 * 1024
CONTRACT_VALIDATION_MESSAGE = (
    "comparison document failed Visual Contract V1 validation"
)


def cmd_visual(args: argparse.Namespace) -> int:
    """Dispatch ``visual <action>``."""
    action = getattr(args, "visual_action", None)
    if action == "compare":
        return _cmd_visual_compare(args)
    print("usage: omg visual {compare}", file=sys.stderr)
    return 2


def _load_compare_document(input_path: str) -> Any:
    path = Path(input_path)
    try:
        with path.open("rb") as handle:
            body = handle.read(MAX_COMPARE_DOCUMENT_BYTES + 1)
    except OSError as exc:
        raise ValueError("visual compare input is not readable JSON") from exc
    if len(body) > MAX_COMPARE_DOCUMENT_BYTES:
        raise ValueError("visual compare input exceeds size limit")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("visual compare input is not readable JSON") from exc


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
        document = _load_compare_document(input_path)
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
    except VisualContractError:
        emit_json(
            failure(
                CMD_COMPARE,
                "E_VISUAL_CONTRACT",
                CONTRACT_VALIDATION_MESSAGE,
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
    "CMD_COMPARE",
    "CONTRACT_VALIDATION_MESSAGE",
    "MAX_COMPARE_DOCUMENT_BYTES",
    "cmd_visual",
    "register_visual_parsers",
]
