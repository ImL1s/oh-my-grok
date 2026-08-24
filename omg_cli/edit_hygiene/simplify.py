"""Bounded, opt-in code simplifier assignment (#76).

Default (no ``--provider``) does not call an LLM. With no hash-edit
descriptors it records an assignment for ``omg-code-simplifier`` and
blocks. Optional ``--provider grok`` still records that assignment, then
starts a Jobs grok job that may only emit hash-edit descriptor JSON. The
CLI never applies those descriptors, never self-approves, and never writes
``verified``. Independent review is required before ``--apply-edits``.
The once-per-stage marker ``.omg/state/simplify-guard.json`` is not a
``verified`` stamp.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

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
    HASH_EDIT_KIND,
    HashEditApplyError,
    HashEditConcurrencyError,
    HashEditCurrentFact,
    HashEditDescriptorError,
    HashEditError,
    apply_hash_edit,
    content_sha256,
    parse_hash_edit_descriptor,
    plan_hash_edit,
    read_confined_regular_file,
    write_confined_regular_file,
)
from omg_cli.jobs.models import JobState, JobStoreError
from omg_cli.jobs.ownership import (
    IdentityProbeOutcome,
    ProcessIdentity,
    become_child_subreaper,
    child_identities,
    merge_identity,
    pgid_member_identities,
    probe_identity_liveness,
    refresh_identity,
)
from omg_cli.jobs.runtime import (
    cancel_job,
    collect_job,
    identities_from_start_record,
    prove_job_processes_gone,
    start_job,
    wait_job,
)
from omg_cli.jobs.store import job_dir, make_job_id

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
    ".md",
)
DEFAULT_MAX_FILES: Final[int] = 16
DEFAULT_MAX_BYTES: Final[int] = 262_144
GROK_PROVIDER: Final[str] = "grok"
PROVIDER_TIMEOUT_S: Final[float] = 90.0
PROPOSAL_KIND: Final[str] = "omg.edit.simplify.proposal.v1"
SANDBOX_REL: Final[str] = ".omg/artifacts/simplify-sandbox"
_BEGIN_FILE: Final[str] = "-----BEGIN FILE-----"
_END_FILE: Final[str] = "-----END FILE-----"
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


class SimplifyProviderError(SimplifyError):
    """Grok Jobs proposal failed. Assignment is still recorded when present."""

    code = "E_SIMPLIFY_PROVIDER"

    def __init__(
        self,
        message: str,
        assignment: dict[str, Any] | None = None,
        *,
        job_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.assignment = assignment
        self.job_id = job_id


class SimplifyRollback(SimplifyError):
    """Partial apply could not be fully restored. Not a verified stamp."""

    code = "E_SIMPLIFY_ROLLBACK"

    def __init__(
        self,
        message: str,
        dirty_paths: list[str],
        *,
        artifact: str | None = None,
    ) -> None:
        super().__init__(message)
        self.dirty_paths = list(dirty_paths)
        self.artifact = artifact


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


def _sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _restore_originals(
    root: Path, applied: dict[str, tuple[bytes, bytes]]
) -> list[str]:
    """Restore files this invocation published. Concurrent edits are not clobbered."""

    dirty: list[str] = []
    for rel, (original, after) in applied.items():
        try:
            current = read_confined_regular_file(root, rel)
            if current == original:
                continue
            if current != after:
                dirty.append(rel)
                continue
            write_confined_regular_file(root, rel, original, expected=after)
            restored = read_confined_regular_file(root, rel)
            if restored != original:
                dirty.append(rel)
        except HashEditConcurrencyError:
            dirty.append(rel)
        except Exception:
            dirty.append(rel)
    return dirty


def _record_simplify_dirty(
    root: Path,
    *,
    stage_id: str,
    kept: list[str],
    dirty: list[str],
) -> str:
    _write_guard(
        root,
        {
            "kind": "omg.simplify.guard.v1",
            "schema_version": 1,
            "stage": stage_id,
            "status": "dirty",
            "failed": True,
            "paths": kept,
            "dirty_paths": dirty,
        },
    )
    return write_edit_artifact(
        root,
        {
            "kind": "omg.edit.simplify.dirty.v1",
            "command": "edit.simplify",
            "stage": stage_id,
            "status": "dirty",
            "failed": True,
            "paths": kept,
            "dirty_paths": dirty,
        },
    )


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


def _normalize_provider(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _job_state_value(state: Any) -> str:
    if isinstance(state, JobState):
        return state.value
    if isinstance(state, str):
        return state.strip().lower()
    value = getattr(state, "value", None)
    if isinstance(value, str):
        return value.strip().lower()
    return str(state or "").strip().lower()


def _extract_json_value(text: str) -> Any:
    if not isinstance(text, str) or not text.strip():
        raise SimplifyProviderError("grok output is empty")
    stripped = text.strip()
    if stripped.startswith("\ufeff"):
        stripped = stripped.lstrip("\ufeff").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        while lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char not in "{[":
            continue
        try:
            obj, _end = decoder.raw_decode(stripped[index:])
            return obj
        except json.JSONDecodeError:
            continue
    raise SimplifyProviderError("grok output is not hash-edit descriptor JSON")


def _descriptors_from_provider_json(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        nested = raw.get("descriptors")
        if isinstance(nested, list):
            items = nested
        elif raw.get("kind") == HASH_EDIT_KIND:
            items = [raw]
        else:
            raise SimplifyProviderError("grok output is not hash-edit descriptor JSON")
    else:
        raise SimplifyProviderError("grok output is not hash-edit descriptor JSON")
    out = []
    try:
        for item in items:
            out.append(parse_hash_edit_descriptor(item))
    except HashEditDescriptorError as exc:
        raise SimplifyProviderError(
            f"grok output is not hash-edit descriptors: {exc}"
        ) from exc
    return out


def _read_job_result_text(root: Path, summary: Mapping[str, Any]) -> str:
    job_id = str(summary.get("job_id") or "").strip()
    rel = summary.get("result")
    if not job_id or not isinstance(rel, str) or not rel.strip():
        raise SimplifyProviderError(
            "grok job produced no result artifact",
            job_id=job_id or None,
        )
    raw = rel.strip()
    candidate = Path(raw)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise SimplifyProviderError(
            "grok job result path escapes job dir",
            job_id=job_id,
        )
    try:
        jdir = job_dir(root, job_id).resolve()
    except JobStoreError as exc:
        raise SimplifyProviderError(
            f"cannot resolve grok job dir: {exc}",
            job_id=job_id,
        ) from exc
    target = (jdir / candidate).resolve()
    if not target.is_relative_to(jdir):
        raise SimplifyProviderError(
            "grok job result path escapes job dir",
            job_id=job_id,
        )
    try:
        return target.read_text(encoding="utf-8")
    except OSError as exc:
        raise SimplifyProviderError(
            "cannot read grok job result",
            job_id=job_id,
        ) from exc


def _snapshot_targets(root: Path, kept: Sequence[str], cfg: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    max_bytes = int(cfg["max_bytes"])
    for rel in kept:
        out[rel] = read_workspace_text(root, rel, max_bytes=max_bytes)
    return out


def _sandbox_rel(stage_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in stage_id)[:64]
    return f"{SANDBOX_REL}/{safe or 'stage'}"


def _write_proposal_sandbox(
    root: Path, snapshots: Mapping[str, str], stage_id: str
) -> Path:
    """Copy kept files into an artifacts sandbox; grok cwd must stay here."""
    rel_base = _sandbox_rel(stage_id)
    dest_root = Path(root) / rel_base
    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    for rel, text in snapshots.items():
        nested = f"{rel_base}/{posix_relpath(rel)}"
        write_confined_text(root, nested, text)
    return dest_root.resolve()


def _git_porcelain(root: Path) -> str:
    """Whole-tree mutation detector (tracked + untracked). Empty if not a git repo."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "-uall"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _file_sha256(path: Path) -> str | None:
    """Stream SHA-256 of *path*; ``None`` if unreadable."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


_FINGERPRINT_SKIP_PREFIXES: Final[tuple[str, ...]] = (
    ".git/",
    ".omg/artifacts/simplify-sandbox/",
    ".omg/state/events/",
    ".omg/state/event-cursors/",
)


def _skip_fingerprint_rel(rel: str, extra: Sequence[str] = ()) -> bool:
    posix = rel.replace("\\", "/")
    while posix.startswith("./"):
        posix = posix[2:]
    if posix == ".git":
        return True
    prefixes = _FINGERPRINT_SKIP_PREFIXES + tuple(extra)
    for prefix in prefixes:
        base = prefix.rstrip("/")
        if posix == base or posix.startswith(prefix):
            return True
    return False


def _workspace_content_fingerprint(
    root: Path, extra_skip: Sequence[str] = ()
) -> dict[str, str]:
    """SHA-256 of regular files except git metadata and expected CLI outputs.

    Includes ``.omg/state`` (except the wrapper event journal) so forged
    ``passes``/``verified`` cannot hide. Skips only the current simplify
    sandbox, current job dir (via *extra_skip*), and CLI event journals.
    Jobs is not an OS sandbox; this is comprehensive mutation detection.
    """
    root_path = Path(root).resolve()
    extra = tuple(extra_skip)
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        try:
            rel_dir = Path(dirpath).resolve().relative_to(root_path).as_posix()
        except ValueError:
            dirnames[:] = []
            continue
        if rel_dir == ".":
            rel_dir = ""
        keep: list[str] = []
        for name in dirnames:
            child = f"{rel_dir}/{name}" if rel_dir else name
            if name == ".git" or _skip_fingerprint_rel(child, extra):
                continue
            child_path = Path(dirpath) / name
            try:
                child_info = child_path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(child_info.st_mode):
                try:
                    target = os.readlink(child_path)
                except OSError:
                    target = ""
                out[child] = "lnk:" + hashlib.sha256(
                    str(target).encode("utf-8", "replace")
                ).hexdigest()
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if _skip_fingerprint_rel(rel, extra):
                continue
            path = Path(dirpath) / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                try:
                    target = os.readlink(path)
                except OSError:
                    target = ""
                out[rel] = "lnk:" + hashlib.sha256(
                    str(target).encode("utf-8", "replace")
                ).hexdigest()
                continue
            if stat.S_ISFIFO(info.st_mode) or stat.S_ISSOCK(info.st_mode):
                out[rel] = f"mode:{info.st_mode}"
                continue
            if not stat.S_ISREG(info.st_mode):
                out[rel] = f"mode:{info.st_mode}"
                continue
            digest = _file_sha256(path)
            if digest is None:
                continue
            out[rel] = digest
    return out


def _reap_start_identities(identities: Sequence[Any]) -> None:
    """Reap start-time PIDs after exact-identity probe (never forged job.json)."""
    import signal

    from omg_cli.jobs.ownership import kill_pgid, wait_until_gone

    for ident in identities:
        pid = getattr(ident, "pid", None)
        pgid = getattr(ident, "pgid", None)
        if not isinstance(pid, int) or pid <= 1:
            continue
        if not isinstance(pgid, int) or pgid <= 1:
            continue
        if not isinstance(ident, ProcessIdentity):
            ident = ProcessIdentity(pid=pid, pgid=pgid, pid_starttime=getattr(ident, "pid_starttime", None))
        outcome = probe_identity_liveness(ident)
        if outcome in {IdentityProbeOutcome.GONE, IdentityProbeOutcome.REUSED}:
            continue
        if outcome is IdentityProbeOutcome.UNPROVEN:
            raise JobStoreError(
                f"start identity pid={pid} unproven before signal",
                code="E_JOB_CANCEL_UNPROVEN",
            )
        kill_pgid(pgid, signal.SIGTERM)
        wait_until_gone(pid, timeout_s=1.0)
        outcome = probe_identity_liveness(ident)
        if outcome is IdentityProbeOutcome.UNPROVEN:
            raise JobStoreError(
                f"start identity pid={pid} unproven after SIGTERM",
                code="E_JOB_CANCEL_UNPROVEN",
            )
        if outcome in {IdentityProbeOutcome.GONE, IdentityProbeOutcome.REUSED}:
            continue
        kill_pgid(pgid, signal.SIGKILL)
        wait_until_gone(pid, timeout_s=2.0)
        outcome = probe_identity_liveness(ident)
        if outcome not in {IdentityProbeOutcome.GONE, IdentityProbeOutcome.REUSED}:
            raise JobStoreError(
                f"start identity pid={pid} still live after SIGKILL",
                code="E_JOB_CANCEL_UNPROVEN",
            )


def _absorb_runner_children(
    identities: dict[int, ProcessIdentity],
    runner_pids: set[int],
) -> None:
    """Snapshot OS children and live process-group members (not job.json)."""
    del runner_pids
    scanned: set[int] = set()
    pending = True
    while pending:
        pending = False
        for ident in list(identities.values()):
            if ident.pid in scanned:
                continue
            scanned.add(ident.pid)
            refreshed = refresh_identity(ident)
            if refreshed is not None:
                merge_identity(identities, refreshed)
                ident = identities[ident.pid]
            extras: list[ProcessIdentity] = []
            if probe_identity_liveness(ident) is IdentityProbeOutcome.LIVE:
                extras.extend(pgid_member_identities(ident.pgid))
                extras.extend(child_identities(ident.pid))
            for extra in extras:
                if merge_identity(identities, extra):
                    pending = True


def _cancel_simplify_job(
    root: Path,
    job_id: str,
    *,
    assignment: dict[str, Any],
    reason: str,
    start_identities: Sequence[Any] = (),
) -> None:
    """Cancel a live simplify job. Unproven cancel is fail-closed."""
    try:
        cancel_job(root, job_id, reason=reason)
    except Exception as exc:
        _reap_start_identities(start_identities)
        raise SimplifyProviderError(
            f"grok simplify job {reason} and cancel failed: {exc}",
            assignment,
            job_id=job_id,
        ) from exc
    _reap_start_identities(start_identities)


_AUTHORITY_NEEDLES: Final[tuple[str, ...]] = (
    '"verified": true',
    '"verified":true',
    '"passes": true',
    '"passes":true',
)


def _authority_stamp_map(root: Path) -> dict[str, bool]:
    """Map of ``.omg/state`` relative paths that currently contain authority stamps."""
    state = Path(root) / ".omg" / "state"
    out: dict[str, bool] = {}
    if not state.is_dir():
        return out
    for path in state.rglob("*"):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(state).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        out[rel] = any(needle in text for needle in _AUTHORITY_NEEDLES)
    return out


def _assert_no_new_authority_stamps(
    before: Mapping[str, bool],
    after: Mapping[str, bool],
    *,
    assignment: dict[str, Any],
    job_id: str | None,
) -> None:
    """Fail closed if ``.omg/state`` gained verified/passes stamps during the job."""
    for rel, stamped in after.items():
        if stamped and not before.get(rel):
            raise SimplifyProviderError(
                "omg state gained verified/passes during grok proposal; originals were not overwritten",
                assignment,
                job_id=job_id,
            )


def _assert_real_tree_untouched(
    root: Path,
    snapshots: Mapping[str, str],
    cfg: Mapping[str, Any],
    *,
    assignment: dict[str, Any],
    job_id: str | None,
) -> None:
    """Detect mutation of the real --paths set. Never overwrite user bytes."""
    mutated: list[str] = []
    max_bytes = int(cfg["max_bytes"])
    for rel, original in snapshots.items():
        try:
            current = read_workspace_text(root, rel, max_bytes=max_bytes)
        except WorkspacePathError:
            mutated.append(rel)
            continue
        if current != original:
            mutated.append(rel)
    if mutated:
        raise SimplifyProviderError(
            "workspace files changed during grok proposal; originals were not overwritten",
            assignment,
            job_id=job_id,
        )


def _build_grok_simplify_prompt(
    *,
    kept: Sequence[str],
    skipped: Sequence[Mapping[str, str]],
    snapshots: Mapping[str, str],
    stage_id: str,
) -> str:
    lines = [
        "You are omg-code-simplifier. Propose bounded hash-anchored simplifications.",
        "Reply with ONLY JSON. No prose. No markdown fences.",
        'Shape: {"descriptors": [HashEditDescriptorV1, ...]} or a JSON array of descriptors.',
        'If no safe simplification exists, reply {"descriptors": []}.',
        "Required descriptor keys: schema_version (integer 1), kind "
        f'"{HASH_EDIT_KIND}", edit_id, producer, path, base_sha256, old_text, '
        "replacement, before_context, after_context, old_text_sha256, "
        "replacement_sha256, before_context_sha256, after_context_sha256.",
        f'producer must be "{SIMPLIFIER_ROLE}".',
        "path must be a workspace-relative POSIX path from FILES below.",
        "Do not write, edit, or apply any files. Do not set verified or passes.",
        f"stage: {stage_id}",
    ]
    if skipped:
        lines.append("SKIPPED (do not emit descriptors for these):")
        for item in skipped:
            lines.append(f"- {item.get('path')}: {item.get('reason')}")
    lines.append("FILES:")
    if not kept:
        lines.append("(none)")
    for rel in kept:
        body = snapshots.get(rel, "")
        lines.append(f"FILE path={rel} sha256={content_sha256(body)}")
        lines.append(_BEGIN_FILE)
        lines.append(body)
        lines.append(_END_FILE)
    return "\n".join(lines) + "\n"


def _write_proposal_artifact(root: Path, payload: dict[str, Any]) -> str:
    """Persist a proposal including descriptors. Never writes verified/passes."""

    body = dict(payload)
    body.pop("passes", None)
    body.pop("verified", None)
    body["kind"] = str(body.get("kind") or PROPOSAL_KIND)
    body["schema_version"] = int(body.get("schema_version") or 1)
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    body["digest"] = digest
    rendered = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    rel = f".omg/artifacts/edit/{digest}.json"
    write_confined_text(root, rel, rendered)
    try:
        from omg_cli.hooks_registry import emit_wrapper_event

        emit_wrapper_event(
            "artifact.created",
            {
                "root": str(Path(root).resolve()),
                "kind": PROPOSAL_KIND,
                "artifact_kind": PROPOSAL_KIND,
                "path": rel,
                "digest": digest,
                "schema_version": 1,
            },
        )
    except Exception:
        pass
    return rel


def _propose_with_grok(
    root: Path,
    *,
    assignment: dict[str, Any],
    kept: Sequence[str],
    skipped: Sequence[Mapping[str, str]],
    cfg: Mapping[str, Any],
    stage_id: str,
    run_id: str | None,
    timeout_s: float,
) -> dict[str, Any]:
    snapshots = _snapshot_targets(root, kept, cfg)
    porcelain_before = _git_porcelain(root)
    fingerprint_before = _workspace_content_fingerprint(root)
    authority_before = _authority_stamp_map(root)
    prompt = _build_grok_simplify_prompt(
        kept=kept,
        skipped=skipped,
        snapshots=snapshots,
        stage_id=stage_id,
    )
    sandbox = _write_proposal_sandbox(root, snapshots, stage_id)
    job_id: str | None = make_job_id()
    pending: SimplifyProviderError | None = None
    descriptors: list[Any] | None = None
    job_succeeded = False
    cancelled = False
    wait_observed_terminal = False
    start_identities: tuple[Any, ...] = ()
    try:
        try:
            # Subreaper must be this supervisor, not the job runner: grok can
            # kill the runner. Linux only; Darwin has no PR_SET_CHILD_SUBREAPER.
            become_child_subreaper()
            supervisor_pid = os.getpid()
            preexisting_children = {child.pid for child in child_identities(supervisor_pid)}
            started = start_job(
                root,
                provider=GROK_PROVIDER,
                role=SIMPLIFIER_ROLE,
                prompt_text=prompt,
                run_id=run_id,
                provider_timeout_s=float(timeout_s),
                cwd=sandbox,
                job_id=job_id,
            )
            record = getattr(started, "record", None)
            started_id = str(getattr(record, "job_id", "") or "").strip() or None
            if started_id:
                job_id = started_id
            if not job_id:
                raise SimplifyProviderError(
                    "grok job start did not return a job_id",
                    assignment,
                )
            start_identities = identities_from_start_record(record)
            captured: dict[int, ProcessIdentity] = {
                ident.pid: ident
                for ident in start_identities
                if isinstance(ident, ProcessIdentity)
            }
            runner_pids = set(captured)
            _absorb_runner_children(captured, runner_pids)

            def _sync_start_identities() -> None:
                nonlocal start_identities
                start_identities = tuple(captured.values())

            def _absorb_supervisor_children() -> None:
                if not runner_pids:
                    return
                for child in child_identities(supervisor_pid):
                    if child.pid in preexisting_children:
                        continue
                    merge_identity(captured, child)

            def _on_poll(_wait_record: object) -> None:
                _absorb_runner_children(captured, runner_pids)
                _absorb_supervisor_children()
                _sync_start_identities()

            _absorb_supervisor_children()
            _sync_start_identities()
            try:
                waited, timed_out = wait_job(
                    root,
                    job_id,
                    timeout_s=float(timeout_s),
                    poll_s=0.02,
                    stop_on_recovery_required=True,
                    on_poll=_on_poll,
                )
            finally:
                _absorb_runner_children(captured, runner_pids)
                _absorb_supervisor_children()
                _sync_start_identities()
            if timed_out:
                _cancel_simplify_job(
                    root,
                    job_id,
                    assignment=assignment,
                    reason="simplify-provider-timeout",
                    start_identities=start_identities,
                )
                cancelled = True
                raise SimplifyProviderError(
                    f"grok simplify job timed out after {timeout_s}s",
                    assignment,
                    job_id=job_id,
                )
            # Terminal job.json is not process-exit proof. A forged SUCCEEDED
            # stamp used to skip cancel and let a still-live grok mutate the
            # tree after the one-shot fingerprint. Inner grok is a new session
            # (not in the runner pgid) and is missing from start_job; capture
            # it from OS children of the still-live runner during wait.
            if runner_pids and not (set(captured) - runner_pids):
                _cancel_simplify_job(
                    root,
                    job_id,
                    assignment=assignment,
                    reason="simplify-provider-inner-unproven",
                    start_identities=start_identities,
                )
                cancelled = True
                raise SimplifyProviderError(
                    "grok simplify inner provider identity was never "
                    "captured before terminal state",
                    assignment,
                    job_id=job_id,
                )
            try:
                prove_job_processes_gone(
                    root, job_id, extra_identities=start_identities
                )
            except JobStoreError as exc:
                _cancel_simplify_job(
                    root,
                    job_id,
                    assignment=assignment,
                    reason="simplify-provider-terminal-live",
                    start_identities=start_identities,
                )
                cancelled = True
                try:
                    prove_job_processes_gone(
                        root, job_id, extra_identities=start_identities
                    )
                except JobStoreError as prove_exc:
                    raise SimplifyProviderError(
                        f"grok simplify job terminal but process still live: {prove_exc}",
                        assignment,
                        job_id=job_id,
                    ) from prove_exc
                raise SimplifyProviderError(
                    f"grok simplify job claimed terminal while process was live: {exc}",
                    assignment,
                    job_id=job_id,
                ) from exc
            wait_observed_terminal = True
            if _job_state_value(getattr(waited, "state", None)) != JobState.SUCCEEDED.value:
                raise SimplifyProviderError(
                    "grok simplify job did not succeed",
                    assignment,
                    job_id=job_id,
                )
            summary = collect_job(root, job_id)
            if _job_state_value(summary.get("state")) != JobState.SUCCEEDED.value:
                raise SimplifyProviderError(
                    "grok simplify job did not succeed",
                    assignment,
                    job_id=job_id,
                )
            job_succeeded = True
            raw_text = _read_job_result_text(root, summary)
            descriptors = _descriptors_from_provider_json(_extract_json_value(raw_text))
        except SimplifyProviderError as exc:
            if exc.assignment is None:
                exc.assignment = assignment
            if exc.job_id is None:
                exc.job_id = job_id
            pending = exc
        except JobStoreError as exc:
            reason = (
                "simplify-provider-recovery"
                if getattr(exc, "code", "") == "E_JOB_RECOVERY_REQUIRED"
                else "simplify-provider-wait"
            )
            if job_id and not cancelled:
                try:
                    _cancel_simplify_job(
                        root,
                        job_id,
                        assignment=assignment,
                        reason=reason,
                        start_identities=start_identities,
                    )
                    cancelled = True
                except SimplifyProviderError as cancel_exc:
                    pending = cancel_exc
            if pending is None:
                pending = SimplifyProviderError(
                    str(exc),
                    assignment,
                    job_id=job_id,
                )
                pending.__cause__ = exc
        except HashEditDescriptorError as exc:
            pending = SimplifyProviderError(
                f"grok output is not hash-edit descriptors: {exc}",
                assignment,
                job_id=job_id,
            )
            pending.__cause__ = exc
        except OSError as exc:
            pending = SimplifyProviderError(
                f"grok simplify job I/O failed: {exc}",
                assignment,
                job_id=job_id,
            )
            pending.__cause__ = exc
        if (
            pending is not None
            and job_id
            and not cancelled
            and not job_succeeded
            and not wait_observed_terminal
        ):
            try:
                _cancel_simplify_job(
                    root,
                    job_id,
                    assignment=assignment,
                    reason="simplify-provider-abort",
                    start_identities=start_identities,
                )
                cancelled = True
            except SimplifyProviderError as cancel_exc:
                pending = cancel_exc
    finally:
        dirty: SimplifyProviderError | None = None
        try:
            _assert_real_tree_untouched(
                root,
                snapshots,
                cfg,
                assignment=assignment,
                job_id=job_id,
            )
            _assert_no_new_authority_stamps(
                authority_before,
                _authority_stamp_map(root),
                assignment=assignment,
                job_id=job_id,
            )
            porcelain_after = _git_porcelain(root)
            extra_skip = (
                (
                    f".omg/jobs/{job_id}/",
                    f".omg/jobs/.locks/{job_id}.lock",
                )
                if job_id
                else ()
            )
            fingerprint_after = _workspace_content_fingerprint(
                root, extra_skip=extra_skip
            )
            if (
                porcelain_after != porcelain_before
                or fingerprint_after != fingerprint_before
            ):
                raise SimplifyProviderError(
                    "git worktree changed during grok proposal; originals were not overwritten",
                    assignment,
                    job_id=job_id,
                )
        except SimplifyProviderError as exc:
            dirty = exc
        shutil.rmtree(sandbox, ignore_errors=True)
    if dirty is not None:
        raise dirty
    if pending is not None:
        raise pending
    if descriptors is None:
        raise SimplifyProviderError(
            "grok simplify job produced no descriptors",
            assignment,
            job_id=job_id,
        )
    kept_set = {Path(rel).as_posix() for rel in kept}
    max_bytes = int(cfg["max_bytes"])
    for desc in descriptors:
        desc_path = Path(str(desc.path)).as_posix()
        if desc_path not in kept_set:
            raise SimplifyProviderError(
                f"grok descriptor path {desc_path!r} is outside the bounded --paths set",
                assignment,
                job_id=job_id,
            )
        if str(desc.producer) != SIMPLIFIER_ROLE:
            raise SimplifyProviderError(
                f"grok descriptor producer {desc.producer!r} is not {SIMPLIFIER_ROLE}",
                assignment,
                job_id=job_id,
            )
        try:
            current = snapshots[desc_path].encode("utf-8")
            if len(current) > max_bytes:
                raise SimplifyProviderError(
                    f"grok descriptor path {desc_path!r} exceeds max_bytes",
                    assignment,
                    job_id=job_id,
                )
            plan_hash_edit(
                desc,
                HashEditCurrentFact(path=desc.path, current_bytes=current),
            )
        except HashEditError as exc:
            raise SimplifyProviderError(
                f"grok descriptor does not bind to the captured snapshot: {exc}",
                assignment,
                job_id=job_id,
            ) from exc

    descriptor_maps = [desc.to_canonical_mapping() for desc in descriptors]
    empty = not descriptor_maps
    status = "no_changes" if empty else "proposed"
    proposal_body = {
        "kind": PROPOSAL_KIND,
        "schema_version": 1,
        "command": "edit.simplify",
        "status": status,
        "provider": GROK_PROVIDER,
        "job_id": job_id,
        "self_approve": False,
        "independent_review_required": True,
        "reviewer_role": REVIEWER_ROLE,
        "assignment": {
            key: value for key, value in assignment.items() if key != "next_action"
        },
        "descriptors": descriptor_maps,
        "descriptor_count": len(descriptor_maps),
        "stage": stage_id,
    }
    proposal = _write_proposal_artifact(root, proposal_body)
    next_action = (
        "no hash-edit descriptors; nothing to apply"
        if empty
        else "independent review then omg edit simplify --apply-edits " + proposal
    )
    return {
        "kind": PROPOSAL_KIND,
        "ok": True,
        "verified": False,
        "status": status,
        "provider": GROK_PROVIDER,
        "job_id": job_id,
        "self_approve": False,
        "independent_review_required": True,
        "reviewer_role": REVIEWER_ROLE,
        "assignment": assignment,
        "proposal": proposal,
        "artifact": proposal,
        "descriptor_count": len(descriptor_maps),
        "stage": stage_id,
        "next_action": next_action,
    }


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
    provider: str | None = None,
    provider_timeout_s: float | None = None,
) -> dict[str, Any]:
    """Return a domain payload. Raises SimplifyBlocked when assignment-only."""

    if not paths:
        raise SimplifyError("require --paths")
    cfg = load_simplify_config(root, config_path)
    if not (enable or cfg["enabled"]):
        raise SimplifyDisabled(
            "simplifier is disabled; pass --enable or set .omg/simplify.json enabled:true"
        )
    provider_name = _normalize_provider(provider)
    applying = bool(apply_edits_path)
    if applying and provider_name:
        raise SimplifyProviderError("cannot combine --provider with --apply-edits")
    if provider_name is not None and provider_name != GROK_PROVIDER:
        raise SimplifyProviderError(
            f"unsupported simplify provider {provider_name!r}; only grok is admitted"
        )
    stage_id = (stage or "default").strip() or "default"
    kept, skipped, total = _collect_targets(root, paths, cfg)
    guard = _read_guard(root)
    status = str(guard.get("status") or "")
    same_stage = str(guard.get("stage") or "") == stage_id

    if applying:
        if same_stage and status == "applied":
            raise SimplifyRecursion("simplifier already applied for this stage")
        if not (same_stage and status == "assigned"):
            raise SimplifyRecursion(
                "apply-edits requires a prior assignment for this stage"
            )
        recorded = guard.get("paths")
        if not isinstance(recorded, list):
            raise SimplifyRecursion(
                "apply-edits paths must match the recorded assignment"
            )
        try:
            assigned_paths = [posix_relpath(str(item)) for item in recorded]
        except WorkspacePathError as exc:
            raise SimplifyRecursion(
                "apply-edits paths must match the recorded assignment"
            ) from exc
        if assigned_paths != kept:
            raise SimplifyRecursion(
                "apply-edits paths must match the recorded assignment"
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
        if not provider_name:
            raise SimplifyBlocked(
                "simplifier does not invent edits; spawn omg-code-simplifier then independent review",
                assignment,
            )
        timeout_s = PROVIDER_TIMEOUT_S if provider_timeout_s is None else float(provider_timeout_s)
        if timeout_s <= 0:
            raise SimplifyProviderError(
                "provider timeout must be positive",
                assignment,
            )
        return _propose_with_grok(
            root,
            assignment=assignment,
            kept=kept,
            skipped=skipped,
            cfg=cfg,
            stage_id=stage_id,
            run_id=run_id,
            timeout_s=timeout_s,
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
    originals: dict[str, bytes] = {}

    for desc in descriptors:
        desc_path = Path(str(desc.path)).as_posix()
        if desc_path not in kept_set:
            raise SimplifyError(
                f"apply-edits path {desc_path!r} is outside the bounded --paths set"
            )
        assert_mutative_edit_allowed(root, desc.path, run_id=run_id, task_id=task_id)
        current = read_confined_regular_file(root, desc.path)
        if desc.path not in originals:
            originals[desc.path] = current
        plan = plan_hash_edit(desc, HashEditCurrentFact(path=desc.path, current_bytes=current))
        planned.append((desc, plan))

    applied: list[dict[str, Any]] = []
    published_by_path: dict[str, tuple[bytes, bytes]] = {}
    try:
        for desc, plan in planned:
            result = apply_hash_edit(root, desc, plan)
            published = read_confined_regular_file(root, result.path)
            if _sha256_bytes(published) != result.after_sha256:
                raise HashEditApplyError("post-apply digest mismatch")
            published_by_path[result.path] = (
                originals.get(result.path, b""),
                published,
            )
            applied.append(
                {
                    "path": result.path,
                    "descriptor_digest": result.descriptor_digest,
                    "before_sha256": result.before_sha256,
                    "after_sha256": result.after_sha256,
                    "unified_diff_sha256": result.unified_diff_sha256,
                }
            )
    except Exception as exc:
        dirty = _restore_originals(root, published_by_path)
        if dirty:
            artifact = _record_simplify_dirty(
                root, stage_id=stage_id, kept=kept, dirty=dirty
            )
            raise SimplifyRollback(
                "simplifier apply rolled back incompletely; files may be dirty",
                dirty,
                artifact=artifact,
            ) from exc
        raise

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
