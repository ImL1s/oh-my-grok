"""Bounded, opt-in code simplifier assignment (#76).

The CLI does not call an LLM. With no hash-edit descriptors it records an
assignment for ``omg-code-simplifier`` and blocks. Independent review is
required; this command cannot approve itself. The once-per-stage marker
``.omg/state/simplify-guard.json`` is not a ``verified`` stamp.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final, Sequence

from omg_cli.edit_hygiene.artifacts import write_edit_artifact
from omg_cli.edit_hygiene.authority import assert_mutative_edit_allowed
from omg_cli.edit_hygiene.workspace import (
    WorkspacePathError,
    posix_relpath,
    read_workspace_text,
    relativize_to_root,
    resolve_workspace_file,
    write_confined_text,
)
from omg_cli.hash_edit import (
    HashEditCurrentFact,
    apply_hash_edit,
    parse_hash_edit_descriptor,
    plan_hash_edit,
)

SIMPLIFIER_ROLE: Final[str] = "omg-code-simplifier"
REVIEWER_ROLE: Final[str] = "omg-code-reviewer"
GUARD_REL: Final[str] = ".omg/state/simplify-guard.json"
DEFAULT_EXTENSIONS: Final[tuple[str, ...]] = (
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".cs",
)
DEFAULT_MAX_FILES: Final[int] = 16
DEFAULT_MAX_BYTES: Final[int] = 262_144
_SKIP_PARTS = (
    "/vendor/",
    "/node_modules/",
    "/dist/",
    "/generated/",
    "/third_party/",
)
_SKIP_NAMES = frozenset(
    {
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "cargo.lock",
        "poetry.lock",
        "composer.lock",
        "go.sum",
        "gemfile.lock",
    }
)
_SKIP_SUFFIXES = (".min.js", ".min.css", ".min.mjs", ".lock")
_GENERATED_HEAD = ("@generated", "auto-generated", "do not edit")

NEXT_ACTION: Final[str] = (
    "spawn omg-code-simplifier read-write then omg-code-reviewer read-only"
)


class SimplifyError(ValueError):
    code = "E_SIMPLIFY"


class SimplifyDisabled(SimplifyError):
    code = "E_SIMPLIFY_DISABLED"


class SimplifyRecursion(SimplifyError):
    code = "E_SIMPLIFY_RECURSION"


class SimplifyBounds(SimplifyError):
    code = "E_SIMPLIFY_BOUNDS"


class SimplifyBlocked(SimplifyError):
    """No descriptors supplied — assignment recorded, simplification not faked."""

    code = "E_SIMPLIFY_ASSIGNMENT"

    def __init__(self, message: str, assignment: dict[str, Any]) -> None:
        super().__init__(message)
        self.assignment = assignment


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_simplify_config(root: Path, path: Path | None = None) -> dict[str, Any]:
    candidate = path if path is not None else Path(root) / ".omg" / "simplify.json"
    data = _load_json(candidate)
    extensions = data.get("extensions") or list(DEFAULT_EXTENSIONS)
    if not isinstance(extensions, list) or any(not isinstance(x, str) for x in extensions):
        raise SimplifyError("simplify.json extensions must be a string list")
    max_files = data.get("max_files", DEFAULT_MAX_FILES)
    max_bytes = data.get("max_bytes", DEFAULT_MAX_BYTES)
    if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files < 1:
        raise SimplifyError("simplify.json max_files must be a positive int")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise SimplifyError("simplify.json max_bytes must be a positive int")
    return {
        "enabled": bool(data.get("enabled", False)),
        "extensions": [item.lower() if item.startswith(".") else f".{item.lower()}" for item in extensions],
        "max_files": max_files,
        "max_bytes": max_bytes,
    }


def _skip_reason(rel: str, text: str | None) -> str | None:
    lower = rel.lower().replace("\\", "/")
    wrapped = f"/{lower}"
    if any(part in wrapped for part in _SKIP_PARTS):
        return "generated_or_vendor"
    name = Path(lower).name
    if name in _SKIP_NAMES or lower.endswith(_SKIP_SUFFIXES):
        return "lock_or_minified"
    if text:
        head = "\n".join(text.splitlines()[:8]).lower()
        if any(marker in head for marker in _GENERATED_HEAD):
            return "generated_header"
    return None


def _read_guard(root: Path) -> dict[str, Any]:
    try:
        return _load_json(resolve_workspace_file(root, GUARD_REL))
    except WorkspacePathError:
        return {}


def _write_guard(root: Path, payload: dict[str, Any]) -> None:
    body = dict(payload)
    body.pop("passes", None)
    body.pop("verified", None)
    text = json.dumps(body, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        write_confined_text(root, GUARD_REL, text)
    except WorkspacePathError as exc:
        raise SimplifyError(f"cannot write simplify guard: {exc}") from exc


def _rel_of(root: Path, raw: str) -> str:
    if Path(raw).is_absolute():
        return relativize_to_root(root, raw)
    return posix_relpath(raw.replace("\\", "/"))


def _collect_targets(
    root: Path,
    paths: Sequence[str],
    cfg: dict[str, Any],
) -> tuple[list[str], list[dict[str, str]], int]:
    kept: list[str] = []
    skipped: list[dict[str, str]] = []
    total = 0
    for raw in paths:
        try:
            rel = _rel_of(root, raw)
        except WorkspacePathError:
            skipped.append({"path": str(raw), "reason": "unreadable"})
            continue
        ext = Path(rel).suffix.lower()
        if ext not in cfg["extensions"]:
            skipped.append({"path": rel, "reason": "extension"})
            continue
        try:
            text = read_workspace_text(root, rel, max_bytes=cfg["max_bytes"])
        except WorkspacePathError:
            skipped.append({"path": rel, "reason": "unreadable"})
            continue
        reason = _skip_reason(rel, text)
        if reason:
            skipped.append({"path": rel, "reason": reason})
            continue
        size = len(text.encode("utf-8"))
        kept.append(rel)
        total += size
    if len(kept) > cfg["max_files"]:
        raise SimplifyBounds(
            f"simplifier exceeds max_files={cfg['max_files']} (got {len(kept)})"
        )
    if total > cfg["max_bytes"]:
        raise SimplifyBounds(
            f"simplifier exceeds max_bytes={cfg['max_bytes']} (got {total})"
        )
    return kept, skipped, total


def _load_descriptors(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict) and isinstance(raw.get("descriptors"), list):
        items = raw["descriptors"]
    else:
        raise SimplifyError("apply-edits JSON must be a descriptor list or {descriptors: []}")
    out = []
    for item in items:
        out.append(parse_hash_edit_descriptor(item))
    return out


def run_simplify(
    root: Path,
    *,
    paths: Sequence[str],
    enable: bool = False,
    apply_edits_path: str | None = None,
    stage: str = "default",
    config_path: Path | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Return a domain payload. Raises SimplifyBlocked when assignment-only."""

    if not paths:
        raise SimplifyError("require --paths")
    cfg = load_simplify_config(root, config_path)
    if not (enable or cfg["enabled"]):
        raise SimplifyDisabled(
            "simplifier is disabled; pass --enable or set .omg/simplify.json enabled:true"
        )
    stage_id = (stage or "default").strip() or "default"
    kept, skipped, total = _collect_targets(root, paths, cfg)
    guard = _read_guard(root)
    status = str(guard.get("status") or "")
    same_stage = str(guard.get("stage") or "") == stage_id
    applying = bool(apply_edits_path)

    if applying:
        if same_stage and status == "applied":
            raise SimplifyRecursion("simplifier already applied for this stage")
        if not (same_stage and status == "assigned"):
            raise SimplifyRecursion(
                "apply-edits requires a prior assignment for this stage"
            )
    else:
        if same_stage and status in {"assigned", "applied"}:
            raise SimplifyRecursion("simplifier already ran for this stage")

    assignment = {
        "role": SIMPLIFIER_ROLE,
        "capability_mode": "read-write",
        "reviewer_role": REVIEWER_ROLE,
        "reviewer_capability_mode": "read-only",
        "self_approve": False,
        "independent_review_required": True,
        "paths": kept,
        "skipped": skipped,
        "bounds": {
            "extensions": cfg["extensions"],
            "max_files": cfg["max_files"],
            "max_bytes": cfg["max_bytes"],
            "total_bytes": total,
        },
        "stage": stage_id,
        "next_action": NEXT_ACTION,
    }

    if not applying:
        _write_guard(
            root,
            {
                "kind": "omg.simplify.guard.v1",
                "schema_version": 1,
                "stage": stage_id,
                "status": "assigned",
                "paths": kept,
            },
        )
        artifact = write_edit_artifact(
            root,
            {
                "kind": "omg.edit.simplify.assignment.v1",
                "command": "edit.simplify",
                "assignment": {
                    key: value
                    for key, value in assignment.items()
                    if key != "next_action"
                },
            },
        )
        assignment["artifact"] = artifact
        raise SimplifyBlocked(
            "simplifier does not invent edits; spawn omg-code-simplifier then independent review",
            assignment,
        )

    # Mutating path: descriptors provided.
    for rel in kept:
        assert_mutative_edit_allowed(root, rel, run_id=run_id, task_id=task_id)
        resolve_workspace_file(root, rel)

    try:
        body = Path(apply_edits_path).read_text(encoding="utf-8")
        raw = json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SimplifyError("cannot read --apply-edits JSON") from exc
    descriptors = _load_descriptors(raw)
    if not descriptors:
        raise SimplifyError("apply-edits contained no descriptors")

    kept_set = {Path(rel).as_posix() for rel in kept}
    planned: list[tuple[Any, Any]] = []
    from omg_cli.hash_edit.apply import read_confined_regular_file

    for desc in descriptors:
        desc_path = Path(str(desc.path)).as_posix()
        if desc_path not in kept_set:
            raise SimplifyError(
                f"apply-edits path {desc_path!r} is outside the bounded --paths set"
            )
        assert_mutative_edit_allowed(root, desc.path, run_id=run_id, task_id=task_id)
        current = read_confined_regular_file(root, desc.path)
        plan = plan_hash_edit(desc, HashEditCurrentFact(path=desc.path, current_bytes=current))
        planned.append((desc, plan))

    applied: list[dict[str, Any]] = []
    for desc, plan in planned:
        result = apply_hash_edit(root, desc, plan)
        applied.append(
            {
                "path": result.path,
                "descriptor_digest": result.descriptor_digest,
                "before_sha256": result.before_sha256,
                "after_sha256": result.after_sha256,
                "unified_diff_sha256": result.unified_diff_sha256,
            }
        )

    _write_guard(
        root,
        {
            "kind": "omg.simplify.guard.v1",
            "schema_version": 1,
            "stage": stage_id,
            "status": "applied",
            "paths": kept,
        },
    )
    payload = {
        "kind": "omg.edit.simplify.result.v1",
        "ok": True,
        "self_approve": False,
        "independent_review_required": True,
        "reviewer_role": REVIEWER_ROLE,
        "applied": applied,
        "skipped": skipped,
        "stage": stage_id,
    }
    payload["artifact"] = write_edit_artifact(root, payload)
    return payload
