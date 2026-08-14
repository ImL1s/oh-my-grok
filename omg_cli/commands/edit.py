"""omg edit — public hash-anchored edit CLI (#76).

Commands: ``omg edit {plan,apply} --input <descriptor.json>``.

``plan`` is read-only. ``apply`` calls :func:`omg_cli.hash_edit.apply_hash_edit`
(re-read, re-plan, atomic same-dir replace). Neither command writes
``passes`` / ``verified`` or any ``.omg/state`` stamp that claims OMG accepted
the edit. This does not claim ``omo.edit.hash_anchored`` host parity.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Final

from omg_cli.cli_envelope import emit_json, failure, success
from omg_cli.cli_util import project_root
from omg_cli.contracts.path_keys import ContractPathError, confined_path
from omg_cli.hash_edit import (
    APPLY_RESULT_KIND,
    HashEditAmbiguousError,
    HashEditApplyError,
    HashEditApplyResultV1,
    HashEditBindError,
    HashEditConcurrencyError,
    HashEditCurrentFact,
    HashEditDescriptorError,
    HashEditError,
    HashEditInputError,
    HashEditPathError,
    HashEditPlanV1,
    HashEditPlannerError,
    HashEditStaleError,
    apply_hash_edit,
    parse_hash_edit_descriptor,
    plan_hash_edit,
)
from omg_cli.hash_edit.descriptor import require_workspace_relpath
from omg_cli.redaction import redact_text

PLAN_RESULT_KIND: Final[str] = "omg.hash_edit.plan.v1"
COMMAND_PLAN: Final[str] = "edit.plan"
COMMAND_APPLY: Final[str] = "edit.apply"

# Copy-safe apply JSON keys (domain body). Must never include raw source,
# replacement, unified-diff text, or local absolute paths.
APPLY_RESULT_JSON_KEYS: Final[frozenset[str]] = frozenset(
    {
        "kind",
        "schema_version",
        "ok",
        "path",
        "descriptor_digest",
        "before_sha256",
        "after_sha256",
        "start_offset",
        "end_offset",
        "start_line",
        "end_line",
        "rebased",
        "unified_diff_sha256",
        "preserved_mode",
    }
)


class HashEditCliUsageError(ValueError):
    """Usage error before the library runs (missing/unreadable --input)."""

    code = "E_HASH_EDIT_USAGE"


def _root(args: argparse.Namespace) -> Path:
    ctx = getattr(args, "omg_ctx", None)
    if ctx is not None and getattr(ctx, "root", None) is not None:
        return Path(ctx.root)
    return project_root()


def _error_for(exc: BaseException) -> tuple[str, str]:
    """Map library exceptions to stable CLI codes (most-specific first)."""

    if isinstance(exc, HashEditCliUsageError):
        return "E_HASH_EDIT_USAGE", "Pass --input PATH to a V1 descriptor JSON file"
    if isinstance(exc, HashEditDescriptorError):
        return (
            "E_HASH_EDIT_DESCRIPTOR",
            "Fix the V1 descriptor (allowlisted keys, content hashes, path)",
        )
    if isinstance(exc, HashEditInputError):
        return (
            "E_HASH_EDIT_INPUT",
            "Fix current-file UTF-8/size or descriptor path bind",
        )
    if isinstance(exc, HashEditBindError):
        return (
            "E_HASH_EDIT_BIND",
            "Adjust old_text/context or the line hint; hint cannot pick duplicates",
        )
    if isinstance(exc, HashEditStaleError):
        return (
            "E_HASH_EDIT_STALE",
            "Re-read the file and rebuild the descriptor against current bytes",
        )
    if isinstance(exc, HashEditAmbiguousError):
        return (
            "E_HASH_EDIT_AMBIGUOUS",
            "Narrow before/after context until exactly one exact match remains",
        )
    if isinstance(exc, HashEditPathError):
        return (
            "E_HASH_EDIT_PATH",
            "Use a confined workspace-relative regular file (no symlink/fifo)",
        )
    if isinstance(exc, HashEditConcurrencyError):
        return (
            "E_HASH_EDIT_CONCURRENCY",
            "Re-plan against the current file; concurrent digest change is not unique_shift",
        )
    if isinstance(exc, HashEditApplyError):
        return "E_HASH_EDIT_APPLY", "Re-plan; apply left the target bytes unchanged"
    if isinstance(exc, HashEditPlannerError):
        return "E_HASH_EDIT_PLAN", "Fix the descriptor or current file and re-plan"
    if isinstance(exc, HashEditError):
        return "E_HASH_EDIT", "Inspect the error and rebuild the descriptor"
    return "E_HASH_EDIT", "Inspect the error and rebuild the descriptor"


def _emit_failure(command: str, exc: BaseException, *, usage: bool = False) -> int:
    code, next_action = _error_for(exc)
    emit_json(
        failure(
            command,
            code,
            redact_text(str(exc)),
            next_action=next_action,
        )
    )
    return 2 if usage else 1


def _load_descriptor(input_path: str | None) -> Any:
    if not input_path:
        raise HashEditCliUsageError("require --input PATH")
    path = Path(input_path)
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise HashEditCliUsageError(f"cannot read --input: {exc}") from exc
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HashEditDescriptorError(f"descriptor is not UTF-8 JSON: {exc}") from exc
    return parse_hash_edit_descriptor(data)


def _read_current_bytes(workspace_root: Path, relative: str) -> bytes:
    """Read target bytes for planning. Apply still re-reads under confinement."""

    rel = require_workspace_relpath(relative, label="edit path")
    parts = rel.split("/")
    root = Path(workspace_root)
    if os.name == "posix":
        try:
            target = confined_path(root, *parts)
        except ContractPathError as exc:
            raise HashEditPathError(str(exc)) from exc
    else:
        if root.is_symlink():
            raise HashEditPathError("workspace root may not be a symlink")
        target = root.joinpath(*parts)
        try:
            target.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise HashEditPathError("path escapes workspace") from exc
        if target.is_symlink():
            raise HashEditPathError(f"target may not be a symlink: {rel}")
    try:
        if target.is_symlink():
            raise HashEditPathError(f"target may not be a symlink: {rel}")
        if not target.is_file():
            raise HashEditPathError(f"target must be a regular file: {rel}")
        return target.read_bytes()
    except HashEditPathError:
        raise
    except FileNotFoundError as exc:
        raise HashEditPathError(f"target does not exist: {rel}") from exc
    except OSError as exc:
        raise HashEditPathError(f"cannot read target: {rel}") from exc


def _plan_json(plan: HashEditPlanV1) -> dict[str, Any]:
    return {
        "kind": PLAN_RESULT_KIND,
        "schema_version": 1,
        "path": plan.path,
        "descriptor_digest": plan.descriptor_digest,
        "before_sha256": plan.before_sha256,
        "after_sha256": plan.after_sha256,
        "start_offset": plan.start_offset,
        "end_offset": plan.end_offset,
        "start_line": plan.start_line,
        "end_line": plan.end_line,
        "rebased": plan.rebased,
        "unified_diff": plan.unified_diff,
        "unified_diff_sha256": plan.unified_diff_sha256,
    }


def _apply_json(result: HashEditApplyResultV1) -> dict[str, Any]:
    payload = {
        "kind": result.kind,
        "schema_version": result.schema_version,
        "ok": result.ok,
        "path": result.path,
        "descriptor_digest": result.descriptor_digest,
        "before_sha256": result.before_sha256,
        "after_sha256": result.after_sha256,
        "start_offset": result.start_offset,
        "end_offset": result.end_offset,
        "start_line": result.start_line,
        "end_line": result.end_line,
        "rebased": result.rebased,
        "unified_diff_sha256": result.unified_diff_sha256,
        "preserved_mode": result.preserved_mode,
    }
    extra = set(payload) - APPLY_RESULT_JSON_KEYS
    if extra:
        raise HashEditApplyError(f"apply JSON is not copy-safe: extra keys {sorted(extra)}")
    if payload["kind"] != APPLY_RESULT_KIND:
        raise HashEditApplyError("apply JSON kind is not HashEditApplyResultV1")
    return payload


def _plan_from_input(args: argparse.Namespace) -> tuple[Any, HashEditPlanV1]:
    desc = _load_descriptor(getattr(args, "input_path", None))
    current = _read_current_bytes(_root(args), desc.path)
    plan = plan_hash_edit(
        desc,
        HashEditCurrentFact(path=desc.path, current_bytes=current),
    )
    return desc, plan


def cmd_edit(args: argparse.Namespace) -> int:
    """Dispatch ``edit {plan,apply}``."""

    action = getattr(args, "edit_action", None)
    if action == "plan":
        return _cmd_plan(args)
    if action == "apply":
        return _cmd_apply(args)
    print("usage: omg edit {plan,apply} --input PATH", file=sys.stderr)
    return 2


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        _desc, plan = _plan_from_input(args)
        emit_json(success(COMMAND_PLAN, plan=_plan_json(plan)))
        return 0
    except HashEditCliUsageError as exc:
        return _emit_failure(COMMAND_PLAN, exc, usage=True)
    except HashEditError as exc:
        return _emit_failure(COMMAND_PLAN, exc)


def _cmd_apply(args: argparse.Namespace) -> int:
    try:
        desc, plan = _plan_from_input(args)
        result = apply_hash_edit(_root(args), desc, plan)
        emit_json(success(COMMAND_APPLY, result=_apply_json(result)))
        return 0
    except HashEditCliUsageError as exc:
        return _emit_failure(COMMAND_APPLY, exc, usage=True)
    except HashEditError as exc:
        return _emit_failure(COMMAND_APPLY, exc)


def register_edit_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    """Register ``edit`` inspect-family parsers (#76)."""

    p_edit = sub.add_parser(
        "edit",
        parents=[common],
        help="hash-anchored edit plan/apply (#76; no verified stamp)",
    )
    edit_sub = p_edit.add_subparsers(dest="edit_action")

    p_plan = edit_sub.add_parser(
        "plan",
        parents=[common],
        help="plan a hash-anchored edit (read-only; no file write)",
    )
    p_plan.add_argument(
        "--input",
        dest="input_path",
        required=True,
        metavar="PATH",
        help="V1 hash-edit descriptor JSON",
    )
    p_plan.set_defaults(func=cmd_edit, edit_action="plan")

    p_apply = edit_sub.add_parser(
        "apply",
        parents=[common],
        help="apply via apply_hash_edit (re-read, re-plan, atomic replace)",
    )
    p_apply.add_argument(
        "--input",
        dest="input_path",
        required=True,
        metavar="PATH",
        help="V1 hash-edit descriptor JSON",
    )
    p_apply.set_defaults(func=cmd_edit, edit_action="apply")

    p_edit.set_defaults(func=cmd_edit)


__all__ = [
    "APPLY_RESULT_JSON_KEYS",
    "COMMAND_APPLY",
    "COMMAND_PLAN",
    "PLAN_RESULT_KIND",
    "cmd_edit",
    "register_edit_parsers",
]
