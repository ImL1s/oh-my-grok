"""omg visual — Visual Contract V1 CLI (#75).

Commands: ``compare``, ``capture``, ``verdict``, ``ralph``, ``overlay``.
Parser construction: ``register_visual_parsers``.

``compare`` wraps :func:`omg_cli.contracts.visual_contract.compare` only
and stays pixel-agnostic. ``overlay`` may decode PNG pixels via stdlib
(no Pillow). ``capture`` / ``verdict`` / ``ralph`` never write ``passes`` /
``verified``. Overlay JSON never inlines image bytes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Final

from omg_cli.cli_envelope import emit_json, failure, success
from omg_cli.contracts.visual_contract import VisualContractError, compare
from omg_cli.visual_pixels import VisualPixelError
from omg_cli.visual_runtime import (
    VisualMetadataError,
    VisualPathError,
    VisualReviewerError,
    VisualRuntimeError,
    load_visual_config,
    run_capture,
    run_overlay,
    run_ralph,
    run_verdict,
)

CMD_COMPARE = "visual.compare"
CMD_CAPTURE = "visual.capture"
CMD_VERDICT = "visual.verdict"
CMD_RALPH = "visual.ralph"
CMD_OVERLAY = "visual.overlay"
MAX_COMPARE_DOCUMENT_BYTES: Final[int] = 1 * 1024 * 1024
CONTRACT_VALIDATION_MESSAGE = (
    "comparison document failed Visual Contract V1 validation"
)
USAGE = "usage: omg visual {compare,capture,verdict,ralph,overlay}"


def cmd_visual(args: argparse.Namespace) -> int:
    """Dispatch ``visual <action>``."""
    action = getattr(args, "visual_action", None)
    if action == "compare":
        return _cmd_visual_compare(args)
    if action == "capture":
        return _cmd_visual_capture(args)
    if action == "verdict":
        return _cmd_visual_verdict(args)
    if action == "ralph":
        return _cmd_visual_ralph(args)
    if action == "overlay":
        return _cmd_visual_overlay(args)
    print(USAGE, file=sys.stderr)
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


def _root(args: argparse.Namespace) -> Path:
    ctx = getattr(args, "omg_ctx", None)
    if ctx is not None and getattr(ctx, "root", None) is not None:
        return Path(ctx.root)
    explicit = getattr(args, "project_root", None)
    if explicit:
        return Path(str(explicit))
    from omg_cli.cli_util import project_root

    return project_root()


def _load_optional_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    return load_visual_config(Path(path))


def _emit_runtime_failure(command: str, exc: BaseException) -> int:
    code = getattr(exc, "code", "E_VISUAL_RUNTIME")
    message = str(exc)
    next_action = "Inspect the visual config, paths, and reviewer roles"
    if isinstance(exc, VisualReviewerError):
        next_action = (
            "Use a read-only reviewer (omg-vision) distinct from the editor role"
        )
    elif isinstance(exc, VisualPathError):
        next_action = "Use a workspace-relative image path (no traversal)"
    elif isinstance(exc, VisualMetadataError):
        next_action = "Declare width/height in config or flags; images are not decoded"
    elif isinstance(exc, VisualPixelError):
        next_action = (
            "Provide two workspace-relative PNG files; overlay is evidence only"
        )
    emit_json(failure(command, code, message, next_action=next_action))
    return 2


def _cmd_visual_capture(args: argparse.Namespace) -> int:
    config_path = getattr(args, "config", None)
    if not config_path:
        emit_json(
            failure(
                CMD_CAPTURE,
                "E_USAGE",
                "require --config PATH",
                next_action="Pass --config visual.yaml (JSON or restricted YAML)",
            )
        )
        return 2
    try:
        root = _root(args)
        config = load_visual_config(Path(config_path))
        result = run_capture(
            root=root,
            config=config,
            run_id=getattr(args, "run_id", None),
        )
    except VisualRuntimeError as exc:
        return _emit_runtime_failure(CMD_CAPTURE, exc)
    emit_json(success(CMD_CAPTURE, result=result))
    return 0


def _cmd_visual_verdict(args: argparse.Namespace) -> int:
    try:
        root = _root(args)
        config = _load_optional_config(getattr(args, "config", None))
        reference = getattr(args, "reference", None)
        actual = getattr(args, "actual", None)
        if not reference and not config.get("reference"):
            emit_json(
                failure(
                    CMD_VERDICT,
                    "E_USAGE",
                    "require --reference PATH",
                    next_action="Pass --reference and --actual image paths",
                )
            )
            return 2
        if not actual and not (config.get("actual") or config.get("candidate")):
            emit_json(
                failure(
                    CMD_VERDICT,
                    "E_USAGE",
                    "require --actual PATH",
                    next_action="Pass --reference and --actual image paths",
                )
            )
            return 2
        result = run_verdict(
            root=root,
            config=config,
            reference_path=reference,
            actual_path=actual,
            threshold_percent=getattr(args, "threshold", None),
            width=getattr(args, "width", None),
            height=getattr(args, "height", None),
            run_id=getattr(args, "run_id", None),
            editor_role=getattr(args, "editor_role", None),
            reviewer_role=getattr(args, "reviewer_role", None),
            descriptor_only=bool(getattr(args, "descriptor_only", False)),
        )
    except (VisualRuntimeError, VisualPixelError) as exc:
        return _emit_runtime_failure(CMD_VERDICT, exc)
    emit_json(success(CMD_VERDICT, result=result))
    return 0


def _cmd_visual_ralph(args: argparse.Namespace) -> int:
    config_path = getattr(args, "config", None)
    if not config_path:
        emit_json(
            failure(
                CMD_RALPH,
                "E_USAGE",
                "require --config PATH",
                next_action="Pass --config visual.yaml (JSON or restricted YAML)",
            )
        )
        return 2
    try:
        root = _root(args)
        config = load_visual_config(Path(config_path))
        result = run_ralph(
            root=root,
            config=config,
            max_iter=getattr(args, "max_iter", None),
            threshold_percent=getattr(args, "threshold", None),
            run_id=getattr(args, "run_id", None),
        )
    except (VisualRuntimeError, VisualPixelError) as exc:
        return _emit_runtime_failure(CMD_RALPH, exc)
    emit_json(success(CMD_RALPH, result=result))
    return 0


def _cmd_visual_overlay(args: argparse.Namespace) -> int:
    reference = getattr(args, "reference", None)
    candidate = getattr(args, "candidate", None)
    if not reference:
        emit_json(
            failure(
                CMD_OVERLAY,
                "E_USAGE",
                "require --reference PATH",
                next_action="Pass --reference and --candidate PNG paths",
            )
        )
        return 2
    if not candidate:
        emit_json(
            failure(
                CMD_OVERLAY,
                "E_USAGE",
                "require --candidate PATH",
                next_action="Pass --reference and --candidate PNG paths",
            )
        )
        return 2
    try:
        root = _root(args)
        result = run_overlay(
            root=root,
            reference_path=reference,
            candidate_path=candidate,
            run_id=getattr(args, "run_id", None),
            descriptor_only=bool(getattr(args, "descriptor_only", False)),
        )
    except (VisualRuntimeError, VisualPixelError) as exc:
        return _emit_runtime_failure(CMD_OVERLAY, exc)
    emit_json(success(CMD_OVERLAY, result=result))
    return 0


def register_visual_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    """Register ``visual`` inspect-family parsers (#75)."""
    p_visual = sub.add_parser(
        "visual",
        parents=[common],
        help="visual compare/capture/verdict/ralph/overlay (scored/blocked; #75)",
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

    p_capture = vis_sub.add_parser(
        "capture",
        parents=[common],
        help=(
            "run a provider-neutral capture command (config/env/PATH screencapture); "
            "blocked if none; never fakes a pass"
        ),
    )
    p_capture.add_argument(
        "--config",
        default=None,
        help="visual.yaml / JSON config with optional capture.command argv",
    )
    p_capture.add_argument("--run-id", default=None, dest="run_id")
    p_capture.set_defaults(func=cmd_visual, visual_action="capture")

    p_verdict = vis_sub.add_parser(
        "verdict",
        parents=[common],
        help=(
            "compare reference/actual via compare(); PNG overlay sidecar "
            "unless --descriptor-only; reviewer_status only — never verified"
        ),
    )
    p_verdict.add_argument("--config", default=None)
    p_verdict.add_argument("--reference", default=None)
    p_verdict.add_argument("--actual", default=None)
    p_verdict.add_argument(
        "--threshold",
        type=int,
        default=None,
        help="percent 0..100 (90 → contract score 9000)",
    )
    p_verdict.add_argument("--width", type=int, default=None)
    p_verdict.add_argument("--height", type=int, default=None)
    p_verdict.add_argument("--run-id", default=None, dest="run_id")
    p_verdict.add_argument("--editor-role", default=None, dest="editor_role")
    p_verdict.add_argument("--reviewer-role", default=None, dest="reviewer_role")
    p_verdict.add_argument(
        "--descriptor-only",
        action="store_true",
        dest="descriptor_only",
        help="skip PNG pixel decode (sha/byte identity only)",
    )
    p_verdict.set_defaults(func=cmd_visual, visual_action="verdict")

    p_ralph = vis_sub.add_parser(
        "ralph",
        parents=[common],
        help=(
            "bounded capture/verdict/repair-prompt loop; evidence only; "
            "does not spawn agents or set verified"
        ),
    )
    p_ralph.add_argument(
        "--config",
        default=None,
        help="visual.yaml / JSON config",
    )
    p_ralph.add_argument(
        "--max-iter",
        type=int,
        default=None,
        dest="max_iter",
        help="iteration budget (default 5, max 20)",
    )
    p_ralph.add_argument(
        "--threshold",
        type=int,
        default=None,
        help="percent 0..100",
    )
    p_ralph.add_argument("--run-id", default=None, dest="run_id")
    p_ralph.set_defaults(func=cmd_visual, visual_action="ralph")

    p_overlay = vis_sub.add_parser(
        "overlay",
        parents=[common],
        help=(
            "PNG pixel overlay evidence (changed_pixels + overlay.png path); "
            "never verified; no vision model"
        ),
    )
    p_overlay.add_argument("--reference", default=None)
    p_overlay.add_argument("--candidate", default=None)
    p_overlay.add_argument("--run-id", default=None, dest="run_id")
    p_overlay.add_argument(
        "--descriptor-only",
        action="store_true",
        dest="descriptor_only",
        help="skip PNG pixel decode (sha/byte identity only)",
    )
    p_overlay.set_defaults(func=cmd_visual, visual_action="overlay")

    p_visual.set_defaults(func=cmd_visual)


__all__ = [
    "CMD_CAPTURE",
    "CMD_COMPARE",
    "CMD_OVERLAY",
    "CMD_RALPH",
    "CMD_VERDICT",
    "CONTRACT_VALIDATION_MESSAGE",
    "MAX_COMPARE_DOCUMENT_BYTES",
    "cmd_visual",
    "register_visual_parsers",
]
