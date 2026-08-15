"""omg edit — hash-anchored apply plus comment/simplifier hygiene (#76).

Commands: ``omg edit {plan,apply,comments,simplify}``.

``plan`` is read-only. ``apply`` calls :func:`omg_cli.hash_edit.apply_hash_edit`
(re-read, re-plan, atomic same-dir replace) after Team / read-only gates.
``comments`` is report-only unless ``--fix``. ``simplify`` is disabled unless
``--enable`` or project config; the CLI never calls an LLM.

None of these commands write ``passes`` / ``verified``. This does not claim
``omo.edit.hash_anchored`` host parity.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Final

from omg_cli.cli_envelope import emit_json, failure, success
from omg_cli.cli_util import project_root
from omg_cli.edit_hygiene import (
    OwnershipEditError,
    ReadOnlyEditError,
    SimplifyBlocked,
    assert_mutative_edit_allowed,
    resolve_run_task_ids,
    write_edit_artifact,
)
from omg_cli.edit_hygiene.authority import EditHygieneError
from omg_cli.edit_hygiene.comments import (
    CommentCheckerError,
    apply_comment_fixes,
    check_comments,
    git_unified_diff,
    load_comment_config,
    looks_like_unified_diff,
)
from omg_cli.edit_hygiene.simplify import (
    SimplifyError,
    run_simplify,
)
from omg_cli.edit_hygiene.workspace import WorkspacePathError
from omg_cli.hash_edit import (
    APPLY_RESULT_KIND,
    HashEditAmbiguousError,
    HashEditApplyError,
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
    read_confined_regular_file,
)
from omg_cli.redaction import redact_text

PLAN_RESULT_KIND: Final[str] = "omg.hash_edit.plan.v1"
COMMAND_PLAN: Final[str] = "edit.plan"
COMMAND_APPLY: Final[str] = "edit.apply"
COMMAND_COMMENTS: Final[str] = "edit.comments"
COMMAND_SIMPLIFY: Final[str] = "edit.simplify"

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
    if isinstance(exc, ReadOnlyEditError):
        return "E_READ_ONLY", "Use a read-write capability_mode for mutating edit tools"
    if isinstance(exc, OwnershipEditError):
        return "E_OWNERSHIP", "Edit only files owned by the calling ULW/Team task"
    if isinstance(exc, WorkspacePathError):
        return "E_EDIT_PATH", "Use a confined workspace-relative regular file"
    if isinstance(exc, CommentCheckerError):
        return "E_COMMENTS", "Fix --input/--git-diff/--paths or edit-comments.json"
    if isinstance(exc, SimplifyBlocked):
        return (
            "E_SIMPLIFY_ASSIGNMENT",
            "spawn omg-code-simplifier read-write then omg-code-reviewer read-only",
        )
    if isinstance(exc, SimplifyError):
        return getattr(exc, "code", "E_SIMPLIFY"), "See simplifier bounds, --enable, or --apply-edits"
    if isinstance(exc, EditHygieneError):
        return getattr(exc, "code", "E_EDIT"), "Inspect the edit-hygiene error"
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


def _emit_failure(
    command: str,
    exc: BaseException,
    *,
    usage: bool = False,
    extra: dict[str, Any] | None = None,
) -> int:
    code, next_action = _error_for(exc)
    payload = failure(
        command,
        code,
        redact_text(str(exc)),
        next_action=next_action,
    )
    if extra:
        payload.update(extra)
    emit_json(payload)
    return 2 if usage else 1


def _load_descriptor(input_path: str | None) -> Any:
    if not input_path:
        raise HashEditCliUsageError("require --input PATH")
    path = Path(input_path)
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise HashEditCliUsageError("cannot read --input") from exc
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HashEditDescriptorError(f"descriptor is not UTF-8 JSON: {exc}") from exc
    return parse_hash_edit_descriptor(data)


def _read_current_bytes(workspace_root: Path, relative: str) -> bytes:
    """Read target bytes for planning via the same O_NOFOLLOW walk as apply."""

    return read_confined_regular_file(workspace_root, relative)


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


def _apply_json(result: Any) -> dict[str, Any]:
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


def _ids(args: argparse.Namespace) -> tuple[str | None, str | None]:
    return resolve_run_task_ids(
        run_id=getattr(args, "run_id", None),
        task_id=getattr(args, "task_id", None),
    )


def cmd_edit(args: argparse.Namespace) -> int:
    """Dispatch ``edit {plan,apply,comments,simplify}``."""

    action = getattr(args, "edit_action", None)
    if action == "plan":
        return _cmd_plan(args)
    if action == "apply":
        return _cmd_apply(args)
    if action == "comments":
        return _cmd_comments(args)
    if action == "simplify":
        return _cmd_simplify(args)
    print("usage: omg edit {plan,apply,comments,simplify}", file=sys.stderr)
    return 2


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        desc = _load_descriptor(getattr(args, "input_path", None))
        current = _read_current_bytes(_root(args), desc.path)
        plan = plan_hash_edit(desc, HashEditCurrentFact(path=desc.path, current_bytes=current))
        emit_json(success(COMMAND_PLAN, plan=_plan_json(plan)))
        return 0
    except HashEditCliUsageError as exc:
        return _emit_failure(COMMAND_PLAN, exc, usage=True)
    except HashEditError as exc:
        return _emit_failure(COMMAND_PLAN, exc)


def _cmd_apply(args: argparse.Namespace) -> int:
    try:
        root = _root(args)
        desc = _load_descriptor(getattr(args, "input_path", None))
        run_id, task_id = _ids(args)
        assert_mutative_edit_allowed(root, desc.path, run_id=run_id, task_id=task_id)
        artifact_dir = Path(root) / ".omg" / "artifacts" / "edit"
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            probe = artifact_dir / ".write-probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            raise HashEditError("cannot write edit artifacts before apply") from exc
        current = _read_current_bytes(root, desc.path)
        plan = plan_hash_edit(
            desc,
            HashEditCurrentFact(path=desc.path, current_bytes=current),
        )
        result = apply_hash_edit(root, desc, plan)
        body = _apply_json(result)
        artifact = write_edit_artifact(
            root,
            {
                "kind": "omg.hash_edit.apply_result.v1",
                "surface": COMMAND_APPLY,
                "result": body,
            },
        )
        emit_json(success(COMMAND_APPLY, result=body, artifact=artifact))
        return 0
    except HashEditCliUsageError as exc:
        return _emit_failure(COMMAND_APPLY, exc, usage=True)
    except (HashEditError, EditHygieneError, WorkspacePathError) as exc:
        return _emit_failure(COMMAND_APPLY, exc)


def _read_input_text(input_path: str) -> str:
    path = Path(input_path)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CommentCheckerError("cannot read --input") from exc


def _cmd_comments(args: argparse.Namespace) -> int:
    try:
        root = _root(args)
        input_path = getattr(args, "input_path", None)
        git_diff = bool(getattr(args, "git_diff", False))
        paths = list(getattr(args, "paths", None) or [])
        if not input_path and not git_diff and not paths:
            raise CommentCheckerError("require --input PATH, --git-diff, or --paths")
        config_path = getattr(args, "config_path", None)
        config = load_comment_config(
            root, Path(config_path) if config_path else None
        )
        diff_text = None
        scan_paths: list[str] | None = paths or None
        if git_diff:
            diff_text = git_unified_diff(root, paths or None)
            scan_paths = paths or None
        elif input_path:
            text = _read_input_text(input_path)
            if looks_like_unified_diff(text):
                diff_text = text
            else:
                scan_paths = [input_path]
        report = check_comments(
            root, paths=scan_paths, diff_text=diff_text, config=config
        )
        fixed: list[dict[str, Any]] = []
        if bool(getattr(args, "fix", False)):
            run_id, task_id = _ids(args)
            for rel in {item.path for item in report.findings if item.auto_fixable}:
                assert_mutative_edit_allowed(root, rel, run_id=run_id, task_id=task_id)
            fixed = apply_comment_fixes(root, report)
        findings = [item.to_public() for item in report.findings]
        artifact = write_edit_artifact(
            root,
            {
                "kind": "omg.edit.comments.v1",
                "surface": COMMAND_COMMENTS,
                "mode": "fix" if getattr(args, "fix", False) else "report",
                "finding_count": len(findings),
                "findings": [item.to_artifact() for item in report.findings],
                "scanned_paths": report.scanned_paths,
                "skipped": report.skipped,
                "fixed": fixed,
            },
        )
        emit_json(
            success(
                COMMAND_COMMENTS,
                kind="omg.edit.comments.v1",
                mode="fix" if getattr(args, "fix", False) else "report",
                findings=findings,
                finding_count=len(findings),
                scanned_paths=report.scanned_paths,
                skipped=report.skipped,
                fixed=fixed,
                artifact=artifact,
            )
        )
        return 0
    except CommentCheckerError as exc:
        usage = "require --input" in str(exc)
        return _emit_failure(COMMAND_COMMENTS, exc, usage=usage)
    except (EditHygieneError, WorkspacePathError) as exc:
        return _emit_failure(COMMAND_COMMENTS, exc)


def _cmd_simplify(args: argparse.Namespace) -> int:
    try:
        root = _root(args)
        paths = list(getattr(args, "paths", None) or [])
        run_id, task_id = _ids(args)
        payload = run_simplify(
            root,
            paths=paths,
            enable=bool(getattr(args, "enable", False)),
            apply_edits_path=getattr(args, "apply_edits", None),
            stage=str(getattr(args, "stage", None) or "default"),
            config_path=Path(args.config_path) if getattr(args, "config_path", None) else None,
            run_id=run_id,
            task_id=task_id,
        )
        emit_json(success(COMMAND_SIMPLIFY, **payload))
        return 0
    except SimplifyBlocked as exc:
        extra = {"assignment": exc.assignment, "blocked": True}
        art = exc.assignment.get("artifact")
        if art:
            extra["artifact"] = art
        return _emit_failure(COMMAND_SIMPLIFY, exc, extra=extra)
    except (SimplifyError, EditHygieneError, HashEditError, WorkspacePathError) as exc:
        return _emit_failure(COMMAND_SIMPLIFY, exc)


def _add_identity_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", dest="run_id", default=None, help="ULW/Team run id")
    parser.add_argument("--task-id", dest="task_id", default=None, help="calling task id")


def register_edit_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    """Register ``edit`` inspect-family parsers (#76)."""

    p_edit = sub.add_parser(
        "edit",
        parents=[common],
        help="hash-anchored edit plan/apply plus comment/simplifier hygiene (#76)",
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
        default=None,
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
        default=None,
        metavar="PATH",
        help="V1 hash-edit descriptor JSON",
    )
    _add_identity_flags(p_apply)
    p_apply.set_defaults(func=cmd_edit, edit_action="apply")

    p_comments = edit_sub.add_parser(
        "comments",
        parents=[common],
        help="AI-slop / comment checker (report-only unless --fix)",
    )
    p_comments.add_argument("--input", dest="input_path", default=None, metavar="PATH")
    p_comments.add_argument(
        "--git-diff",
        dest="git_diff",
        action="store_true",
        help="scope to added lines from git diff HEAD",
    )
    p_comments.add_argument("--paths", nargs="+", default=None, metavar="PATH")
    p_comments.add_argument(
        "--fix",
        action="store_true",
        help="conservatively delete auto-fixable banner/AI-meta comments",
    )
    p_comments.add_argument("--config", dest="config_path", default=None, metavar="PATH")
    _add_identity_flags(p_comments)
    p_comments.set_defaults(func=cmd_edit, edit_action="comments")

    p_simplify = edit_sub.add_parser(
        "simplify",
        parents=[common],
        help="bounded simplifier assignment (disabled unless --enable/config)",
    )
    p_simplify.add_argument("--paths", nargs="+", default=None, metavar="PATH")
    p_simplify.add_argument(
        "--enable",
        action="store_true",
        help="enable this invocation even if .omg/simplify.json is disabled",
    )
    p_simplify.add_argument(
        "--apply-edits",
        dest="apply_edits",
        default=None,
        metavar="PATH",
        help="hash-edit descriptor JSON produced by omg-code-simplifier",
    )
    p_simplify.add_argument("--stage", default="default")
    p_simplify.add_argument("--config", dest="config_path", default=None, metavar="PATH")
    _add_identity_flags(p_simplify)
    p_simplify.set_defaults(func=cmd_edit, edit_action="simplify")

    p_edit.set_defaults(func=cmd_edit)


__all__ = [
    "APPLY_RESULT_JSON_KEYS",
    "COMMAND_APPLY",
    "COMMAND_COMMENTS",
    "COMMAND_PLAN",
    "COMMAND_SIMPLIFY",
    "PLAN_RESULT_KIND",
    "cmd_edit",
    "register_edit_parsers",
]
