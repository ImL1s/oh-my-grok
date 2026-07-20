# omg_cli/integrate.py
"""ULW clean-tree preflight + result-envelope integrator.

Child workers write envelopes under
``.omg/artifacts/ulw-results/<run_id>/<task_id>.json``.
The leader (or ``omg integrate``) applies them in ``task_id`` order via
``git cherry-pick`` of each envelope's ``head_sha`` onto the project root.

Only the omg CLI owns integration status under
``.omg/state/runs/<run_id>/integrate.result.json``.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omg_cli.evidence import sha256_bytes, validate_identifier


CLI_WRITER = "omg-cli"
ENVELOPES_REL = Path(".omg") / "artifacts" / "ulw-results"
RESULT_NAME = "integrate.result.json"
LEGACY_IMPORTS_DIR = "legacy-envelope-imports"

# Minimal envelope keys required by the ULW convergence protocol.
REQUIRED_ENVELOPE_KEYS = (
    "task_id",
    "base_sha",
    "head_sha",
    "worktree_path",
    "status",
    "changed_files",
)
VALID_ENVELOPE_STATUSES = frozenset({"ok", "failed"})

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

# Capture real subprocess entry points at import time so git helpers still work
# when tests monkeypatch ``subprocess.Popen`` / ``run`` to isolate grok launch.
_REAL_POPEN = subprocess.Popen
_REAL_RUN = subprocess.run


class IntegrateError(RuntimeError):
    """Raised for dirty trees, bad envelopes, or apply failures callers may handle."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runs_dir(root: Path) -> Path:
    return Path(root) / ".omg" / "state" / "runs"


def run_dir(root: Path, run_id: str) -> Path:
    return _runs_dir(root) / run_id


def result_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / RESULT_NAME


def default_envelopes_dir(root: Path, run_id: str | None = None) -> Path:
    """Return the ULW envelope root.

    ``run_id`` selects the authoritative run-scoped directory.  Omitting it
    retains the legacy public helper result for callers that need to inspect or
    explicitly import old global envelopes; integration never auto-reads it.
    """

    base = Path(root) / ENVELOPES_REL
    if run_id is None:
        return base
    return base / validate_identifier(run_id, label="run_id")


def legacy_import_command(
    root: Path | str,
    run_id: str,
    source_dir: Path | str | None = None,
) -> str:
    """Return the exact reviewed command for importing legacy v1 envelopes."""

    source = Path(source_dir) if source_dir is not None else default_envelopes_dir(Path(root))
    return (
        f"omg integrate --run {shlex.quote(validate_identifier(run_id, label='run_id'))} "
        f"--import-legacy-envelopes {shlex.quote(str(source))} --review --yes"
    )


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _run_git(
    args: list[str],
    *,
    cwd: Path | str,
    check: bool = False,
    timeout: float | None = 60.0,
) -> subprocess.CompletedProcess[str]:
    """Run git with the real Popen/run, immune to grok-isolation monkeypatches."""
    prev_popen = subprocess.Popen
    prev_run = subprocess.run
    subprocess.Popen = _REAL_POPEN  # type: ignore[misc, assignment]
    subprocess.run = _REAL_RUN  # type: ignore[misc, assignment]
    try:
        return _REAL_RUN(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )
    finally:
        subprocess.Popen = prev_popen  # type: ignore[misc, assignment]
        subprocess.run = prev_run  # type: ignore[misc, assignment]


def git_available(root: Path | str | None = None) -> bool:
    """True if ``git`` runs and (when root given) root is inside a work tree."""
    try:
        if root is None:
            r = subprocess.run(
                ["git", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return r.returncode == 0
        r = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=root)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False


def git_rev_parse_head(root: Path | str) -> str | None:
    """Return ``HEAD`` full sha for ``root``, or None if not a git work tree."""
    root = Path(root)
    try:
        r = _run_git(["rev-parse", "HEAD"], cwd=root)
    except Exception:
        # OSError / Timeout / broken mocks in tests — best-effort only
        return None
    if r.returncode != 0:
        return None
    sha = (r.stdout or "").strip()
    return sha if _SHA_RE.match(sha) else None


def _porcelain_is_dirty(porcelain: str) -> bool:
    """True if porcelain output has real dirt, ignoring oh-my-grok ``.omg/`` state.

    Runtime state under ``.omg/`` is expected untracked/modified during runs and
    must not block ULW integrate preflight (create_run always writes there).
    """
    for line in porcelain.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        # porcelain: XY PATH or XY ORIG -> PATH (rename)
        path_part = line[3:] if len(line) > 3 else line
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[-1]
        path_part = path_part.strip().strip('"')
        # Ignore .omg runtime tree (and nested paths)
        if path_part == ".omg" or path_part.startswith(".omg/"):
            continue
        return True
    return False


def preflight_clean_tree(root: Path | str) -> None:
    """Require clean work tree (ignoring ``.omg/``). No auto-stash.

    Raises:
        IntegrateError: dirty tree, not a git repo, or git unavailable.
    """
    root = Path(root)
    if not git_available(root):
        raise IntegrateError(
            f"preflight_clean_tree: not a git work tree or git missing: {root}"
        )
    try:
        r = _run_git(["status", "--porcelain"], cwd=root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IntegrateError(f"preflight_clean_tree: git status failed: {exc}") from exc
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        raise IntegrateError(f"preflight_clean_tree: git status failed: {err}")
    if _porcelain_is_dirty(r.stdout or ""):
        raise IntegrateError(
            "preflight_clean_tree: working tree is dirty "
            "(git status --porcelain not empty); commit/stash first — no auto-stash"
        )


def record_base_sha(root: Path | str, run_id: str | None = None) -> str | None:
    """Capture ``git rev-parse HEAD`` and optionally persist on the run.

    When ``run_id`` is provided, writes ``base_sha`` into that run's
    ``status.json`` via ``write_status`` (extra field). Returns the sha or None
    when git is unavailable.
    """
    root = Path(root)
    sha = git_rev_parse_head(root)
    if sha is None:
        return None
    if run_id is not None:
        from omg_cli.state import write_status

        # Preserve current status value while attaching base_sha
        from omg_cli.state import load_run

        current = load_run(root, run_id)
        if current is None:
            raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
        st = str(current.get("status") or "initialized")
        write_status(root, run_id, st, extra={"base_sha": sha})
    return sha


def validate_envelope(
    data: dict[str, Any],
    *,
    expected_run_id: str | None = None,
    expected_task_id: str | None = None,
    require_cli_writer: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    """Validate a child result envelope. Returns a normalized copy.

    Required keys: task_id, base_sha, head_sha, worktree_path, status,
    changed_files. ``status`` must be ``ok`` or ``failed``.

    Raises:
        ValueError: on missing/invalid fields.
    """
    if not isinstance(data, dict):
        raise ValueError("envelope must be a dict")

    missing = [k for k in REQUIRED_ENVELOPE_KEYS if k not in data]
    if missing:
        raise ValueError(f"envelope missing keys: {missing}")

    task_id = data["task_id"]
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("envelope.task_id must be a non-empty string")
    task_id = task_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", task_id):
        raise ValueError("envelope.task_id is not a safe task identifier")
    if expected_task_id is not None and task_id != expected_task_id:
        raise ValueError(
            f"envelope.task_id={task_id!r} does not match filename "
            f"task_id={expected_task_id!r}"
        )

    for sha_key in ("base_sha", "head_sha"):
        val = data[sha_key]
        if not isinstance(val, str) or not _SHA_RE.match(val.strip()):
            raise ValueError(
                f"envelope.{sha_key} must be a git object id (7–64 hex chars)"
            )

    worktree_path = data["worktree_path"]
    if not isinstance(worktree_path, str) or not worktree_path.strip():
        raise ValueError("envelope.worktree_path must be a non-empty string")

    status = data["status"]
    if not isinstance(status, str) or status not in VALID_ENVELOPE_STATUSES:
        raise ValueError(
            f"envelope.status must be one of {sorted(VALID_ENVELOPE_STATUSES)}"
        )

    changed = data["changed_files"]
    if not isinstance(changed, list):
        raise ValueError("envelope.changed_files must be a list")
    for i, item in enumerate(changed):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"envelope.changed_files[{i}] must be a non-empty string"
            )
        item_path = Path(item.strip())
        if item_path.is_absolute() or ".." in item_path.parts:
            raise ValueError(
                f"envelope.changed_files[{i}] must be a safe relative path"
            )

    normalized_changed = [str(item).strip().replace("\\", "/") for item in changed]
    if len(set(normalized_changed)) != len(normalized_changed):
        raise ValueError("envelope.changed_files contains duplicate paths")

    writer = data.get("writer")
    if require_cli_writer and writer != CLI_WRITER:
        raise ValueError(
            f"envelope.writer must be {CLI_WRITER!r}, got {writer!r}"
        )
    if writer is not None and not isinstance(writer, str):
        raise ValueError("envelope.writer must be a string when present")

    envelope_run_id = data.get("run_id")
    if expected_run_id is not None:
        if not isinstance(envelope_run_id, str) or not envelope_run_id.strip():
            raise ValueError("envelope.run_id must be a non-empty string")
        if envelope_run_id.strip() != expected_run_id:
            raise ValueError(
                f"envelope.run_id={envelope_run_id!r} does not match selected "
                f"run_id={expected_run_id!r}"
            )
    elif envelope_run_id is not None and not isinstance(envelope_run_id, str):
        raise ValueError("envelope.run_id must be a string when present")

    if strict and status == "ok":
        if data["base_sha"].strip().lower() == data["head_sha"].strip().lower():
            raise ValueError("strict ok envelope must advance head_sha beyond base_sha")
        if not normalized_changed:
            raise ValueError("strict ok envelope.changed_files must not be empty")

    evidence = data.get("evidence", "")
    if evidence is not None and not isinstance(evidence, str):
        raise ValueError("envelope.evidence must be a string when present")

    out: dict[str, Any] = {
        "task_id": task_id,
        "base_sha": data["base_sha"].strip().lower(),
        "head_sha": data["head_sha"].strip().lower(),
        "worktree_path": worktree_path.strip(),
        "status": status,
        "changed_files": normalized_changed,
    }
    if isinstance(writer, str):
        out["writer"] = writer
    if isinstance(envelope_run_id, str):
        out["run_id"] = envelope_run_id.strip()
    if isinstance(evidence, str):
        out["evidence"] = evidence
    return out


def load_envelopes(
    envelopes_dir: Path | str,
    *,
    expected_run_id: str | None = None,
    require_cli_writer: bool = False,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Load one complete envelope set; any invalid sibling fails the set."""
    d = Path(envelopes_dir)
    if not d.is_dir():
        return []

    loaded: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        children = sorted(d.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise IntegrateError(f"cannot enumerate envelopes: {d}: {exc}") from exc
    for path in children:
        if not path.is_file() or path.suffix != ".json":
            errors.append(f"{path.name}: unexpected non-envelope entry")
            continue
        try:
            body = path.read_bytes()
            raw = json.loads(body.decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        except UnicodeDecodeError as exc:
            errors.append(f"{path.name}: envelope is not UTF-8: {exc}")
            continue
        try:
            env = validate_envelope(
                raw if isinstance(raw, dict) else {},
                expected_run_id=expected_run_id,
                expected_task_id=path.stem if expected_run_id is not None else None,
                require_cli_writer=require_cli_writer,
                strict=strict,
            )
        except ValueError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        env["_source"] = str(path)
        env["_sha256"] = sha256_bytes(body)
        loaded.append(env)

    task_ids = [env["task_id"] for env in loaded]
    duplicates = sorted({task for task in task_ids if task_ids.count(task) > 1})
    if duplicates:
        errors.append(f"duplicate task_id values: {duplicates}")

    if errors:
        raise IntegrateError(
            "invalid envelope set; parse/validation errors:\n  "
            + "\n  ".join(errors)
        )
    loaded.sort(key=lambda e: e["task_id"])
    return loaded


def import_legacy_envelopes(
    root: Path | str,
    run_id: str,
    source_dir: Path | str,
    *,
    review: bool = False,
    yes: bool = False,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    """Explicitly import one reviewed legacy-v1 envelope set.

    The complete source directory is validated before any destination is
    created.  A byte-for-byte backup plus provenance manifest is committed
    before the validated snapshot is atomically renamed into the selected
    run's scoped envelope directory.  The run schema is never rewritten.
    """

    root_path = Path(root).resolve()
    run_id = validate_identifier(run_id, label="run_id")

    # Schema dispatch and operator confirmation happen before source
    # enumeration or any import mutation.
    from omg_cli.state import RunSchema, classify_run_schema, load_run

    run = load_run(root_path, run_id)
    if run is None:
        raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
    schema = classify_run_schema(run)
    if schema is RunSchema.STRICT_V2:
        raise IntegrateError("strict-v2 runs refuse legacy envelope import")
    if not review or not yes:
        raise IntegrateError(
            "legacy envelope import requires both --review and --yes; run: "
            + legacy_import_command(root_path, run_id, source_dir)
        )

    source = Path(source_dir).resolve(strict=False)
    destination = default_envelopes_dir(root_path, run_id)
    if source == destination.resolve(strict=False):
        raise IntegrateError("legacy import source and destination must differ")
    if destination.exists():
        raise IntegrateError(
            f"legacy import destination collision; refusing overwrite: {destination}"
        )

    invocation = validate_identifier(
        invocation_id or uuid.uuid4().hex,
        label="invocation_id",
    )
    import_root = run_dir(root_path, run_id) / LEGACY_IMPORTS_DIR
    backup_dir = import_root / invocation
    if backup_dir.exists():
        raise IntegrateError(f"legacy import invocation collision: {backup_dir}")

    # Serialize official import callers without treating atomic rename as a
    # concurrency primitive.  A stale lock is intentionally fail-closed.
    lock_path = run_dir(root_path, run_id) / "legacy-envelope-import.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise IntegrateError(
            f"legacy envelope import already active or stale lock exists: {lock_path}"
        ) from exc

    backup_stage = import_root / f".{invocation}.tmp"
    stage_dir = import_root / f".{invocation}.destination.tmp"
    try:
        with os.fdopen(lock_fd, "w", encoding="utf-8") as handle:
            handle.write(invocation + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        envelopes = load_envelopes(
            source,
            expected_run_id=run_id,
            require_cli_writer=True,
            strict=False,
        )
        if not envelopes:
            raise IntegrateError(f"legacy import source has no envelopes: {source}")

        snapshot: dict[str, bytes] = {}
        for envelope in envelopes:
            path = Path(envelope["_source"])
            body = path.read_bytes()
            if sha256_bytes(body) != envelope["_sha256"]:
                raise IntegrateError(
                    f"legacy import source changed during validation: {path.name}"
                )
            snapshot[path.name] = body

        source_files = [
            {
                "name": name,
                "sha256": sha256_bytes(body),
                "size": len(body),
            }
            for name, body in sorted(snapshot.items())
        ]
        manifest: dict[str, Any] = {
            "writer": CLI_WRITER,
            "schema_version": 1,
            "schema_classification": schema.value,
            "run_id": run_id,
            "invocation_id": invocation,
            "source_dir": str(source),
            "destination_dir": str(destination),
            "source_files": source_files,
            "created_at": _utc_now(),
        }
        manifest_body = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

        backup_files = backup_stage / "files"
        backup_files.mkdir(parents=True, exist_ok=False)
        stage_dir.mkdir(parents=True, exist_ok=False)
        for name, body in sorted(snapshot.items()):
            (backup_files / name).write_bytes(body)
            (stage_dir / name).write_bytes(body)
        (backup_stage / "manifest.json").write_bytes(manifest_body)

        for record in source_files:
            name = str(record["name"])
            expected_hash = str(record["sha256"])
            if sha256_bytes((backup_files / name).read_bytes()) != expected_hash:
                raise IntegrateError(f"legacy import backup verification failed: {name}")
            if sha256_bytes((stage_dir / name).read_bytes()) != expected_hash:
                raise IntegrateError(f"legacy import staging verification failed: {name}")

        current_names = sorted(path.name for path in source.iterdir())
        if current_names != sorted(snapshot):
            raise IntegrateError("legacy import source set changed before commit")
        for name, original in snapshot.items():
            if sha256_bytes((source / name).read_bytes()) != sha256_bytes(original):
                raise IntegrateError(
                    f"legacy import source changed before commit: {name}"
                )

        import_root.mkdir(parents=True, exist_ok=True)
        backup_stage.rename(backup_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise IntegrateError(
                f"legacy import destination collision; refusing overwrite: {destination}"
            )
        stage_dir.rename(destination)

        result = dict(manifest)
        result.update(
            {
                "status": "imported",
                "backup_dir": str(backup_dir),
                "manifest_path": str(backup_dir / "manifest.json"),
                "manifest_sha256": sha256_bytes(manifest_body),
                "envelope_hashes": {
                    Path(envelope["_source"]).stem: envelope["_sha256"]
                    for envelope in envelopes
                },
            }
        )
        return result
    except (OSError, ValueError) as exc:
        if isinstance(exc, IntegrateError):
            raise
        raise IntegrateError(f"legacy envelope import failed: {exc}") from exc
    finally:
        for temporary in (stage_dir, backup_stage):
            if temporary.is_dir():
                shutil.rmtree(temporary, ignore_errors=True)
            elif temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
        try:
            lock_path.unlink()
        except OSError:
            pass


def _commit_exists(root: Path, sha: str) -> bool:
    r = _run_git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=root)
    return r.returncode == 0


def assert_ancestor(root: Path | str, base_sha: str, head_sha: str) -> None:
    """Require ``base_sha`` is an ancestor of ``head_sha`` (or equal).

    Uses ``git merge-base --is-ancestor base head``. Skips the check when
    base and head normalize to the same object id.

    Raises:
        IntegrateError: not an ancestor, or git failure.
    """
    root = Path(root)
    base = (base_sha or "").strip().lower()
    head = (head_sha or "").strip().lower()
    if not base or not head:
        raise IntegrateError("assert_ancestor: base_sha and head_sha required")
    if base == head:
        return
    r = _run_git(["merge-base", "--is-ancestor", base, head], cwd=root)
    if r.returncode == 0:
        return
    # Non-zero: either not ancestor or git error
    err = (r.stderr or r.stdout or "").strip()
    if r.returncode == 1 and not err:
        raise IntegrateError(
            f"assert_ancestor: {base} is not an ancestor of {head}"
        )
    raise IntegrateError(
        f"assert_ancestor: merge-base --is-ancestor failed for "
        f"{base}..{head}: {err or f'exit {r.returncode}'}"
    )


def list_range_commits(root: Path | str, base: str, head: str) -> list[str]:
    """Return commits in ``base..head`` (exclusive base) topo-order oldest-first.

    Empty list when base == head or range is empty.
    """
    root = Path(root)
    b = (base or "").strip()
    h = (head or "").strip()
    if not b or not h:
        raise IntegrateError("list_range_commits: base and head required")
    if b.lower() == h.lower():
        return []
    r = _run_git(
        ["rev-list", "--reverse", "--topo-order", f"{b}..{h}"],
        cwd=root,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        raise IntegrateError(
            f"list_range_commits: rev-list failed for {b}..{h}: {err}"
        )
    out: list[str] = []
    for line in (r.stdout or "").splitlines():
        sha = line.strip().lower()
        if sha:
            out.append(sha)
    return out


def reject_merge_commits(root: Path | str, commits: list[str]) -> None:
    """Raise IntegrateError if any commit in *commits* is a merge (>1 parent)."""
    root = Path(root)
    for sha in commits:
        r = _run_git(["rev-list", "--parents", "-n", "1", sha], cwd=root)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            raise IntegrateError(
                f"reject_merge_commits: cannot inspect {sha}: {err}"
            )
        parts = (r.stdout or "").strip().split()
        # format: <commit> <parent1> [parent2 ...]
        if len(parts) > 2:
            raise IntegrateError(
                f"reject_merge_commits: merge commit not allowed in range: {sha} "
                f"(parents={parts[1:]})"
            )


def _normalize_path_for_compare(p: str) -> str:
    """Normalize a path string for claimed-vs-actual changed_files compare."""
    s = (p or "").strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s


def verify_changed_files(
    root: Path | str,
    base: str,
    head: str,
    claimed: list[str],
) -> None:
    """Require *claimed* paths match ``git diff --name-only base head``.

    - Actual non-empty + claimed empty → refuse (anti-forge skip).
    - Both empty → ok (true no-op range, base==head or empty diff).
    - Claimed non-empty → order-independent set equality after path normalize.

    Raises:
        IntegrateError: empty claim with non-empty diff, or set mismatch.
    """
    root = Path(root)
    b = (base or "").strip()
    h = (head or "").strip()
    if not b or not h:
        raise IntegrateError("verify_changed_files: base and head required")
    r = _run_git(["diff", "--name-only", b, h], cwd=root)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        raise IntegrateError(
            f"verify_changed_files: git diff --name-only failed: {err}"
        )
    actual = {
        _normalize_path_for_compare(line)
        for line in (r.stdout or "").splitlines()
        if line.strip()
    }
    claimed_norm = {_normalize_path_for_compare(p) for p in claimed if p}
    if actual and not claimed_norm:
        raise IntegrateError(
            "verify_changed_files: changed_files is empty but git diff "
            f"{b}..{h} has {len(actual)} path(s); refuse skip (anti-forge)"
        )
    if not claimed_norm and not actual:
        return  # true no-op range
    if claimed_norm != actual:
        missing = sorted(actual - claimed_norm)
        extra = sorted(claimed_norm - actual)
        parts = []
        if missing:
            parts.append(f"missing from claim: {missing}")
        if extra:
            parts.append(f"claimed but not in diff: {extra}")
        raise IntegrateError(
            "verify_changed_files: claimed changed_files do not match "
            f"git diff --name-only {b} {h}: " + "; ".join(parts)
        )


def preflight_envelope_range(
    root: Path | str,
    base_sha: str,
    head_sha: str,
    claimed: list[str] | None = None,
    *,
    require_squash: bool = False,
) -> list[str]:
    """Ancestry + merge reject + optional changed_files + require_squash.

    Returns the list of commits in ``base..head`` (may be empty when equal).
    Call before cherry-pick.
    """
    root = Path(root)
    base = base_sha.strip().lower()
    head = head_sha.strip().lower()
    assert_ancestor(root, base, head)
    commits = list_range_commits(root, base, head)
    if commits:
        reject_merge_commits(root, commits)
    if require_squash and len(commits) > 1:
        raise IntegrateError(
            f"require_squash: range {base}..{head} has {len(commits)} commits; "
            "squash to a single commit (or set require_squash=False)"
        )
    if claimed is not None:
        verify_changed_files(root, base, head, list(claimed))
    return commits


def worktree_path_allowed(root: Path, worktree: Path) -> bool:
    """True if *worktree* resolves under project root or ``root/.omg/worktrees``.

    Absolute paths outside these trees are rejected to prevent envelope
    path-injection (``worktree_path: /etc`` etc.).
    """
    root_r = Path(root).resolve()
    try:
        wt_r = Path(worktree).resolve()
    except (OSError, RuntimeError):
        return False
    allowed_roots = (root_r, (root_r / ".omg" / "worktrees").resolve())
    for base in allowed_roots:
        try:
            wt_r.relative_to(base)
            return True
        except ValueError:
            continue
    return False


def assert_worktree_path_allowed(root: Path, worktree: Path) -> Path:
    """Resolve *worktree* and raise ``IntegrateError`` if outside whitelist."""
    root = Path(root).resolve()
    wt = Path(worktree)
    if not wt.is_absolute():
        wt = (root / wt).resolve()
    else:
        try:
            wt = wt.resolve()
        except (OSError, RuntimeError) as exc:
            raise IntegrateError(f"worktree_path not resolvable: {worktree}: {exc}") from exc
    if not worktree_path_allowed(root, wt):
        raise IntegrateError(
            f"worktree_path outside allowlist (must be under project root or "
            f".omg/worktrees): {wt}"
        )
    return wt


def _ensure_commit_reachable(
    root: Path,
    head_sha: str,
    worktree_path: Path,
    *,
    base_sha: str | None = None,
) -> None:
    """Make ``head_sha`` (and optional ``base_sha``) available in ``root``."""
    need = [head_sha]
    if base_sha and base_sha != head_sha:
        need.append(base_sha)
    missing = [s for s in need if not _commit_exists(root, s)]
    if not missing:
        return
    if not worktree_path.is_dir():
        raise IntegrateError(
            f"worktree_path does not exist and commit(s) not in repo: "
            f"{worktree_path} missing={missing}"
        )
    # Fetch objects from the worker worktree/clone into the leader.
    # Works for linked worktrees (usually already present) and separate clones.
    r = _run_git(
        ["fetch", "--no-tags", str(worktree_path), head_sha],
        cwd=root,
        timeout=120.0,
    )
    if r.returncode != 0 or not _commit_exists(root, head_sha):
        # Fallback: fetch HEAD from that repo and hope head_sha is reachable
        r2 = _run_git(
            ["fetch", "--no-tags", str(worktree_path), "HEAD"],
            cwd=root,
            timeout=120.0,
        )
        if r2.returncode != 0 or not _commit_exists(root, head_sha):
            err = (r.stderr or r2.stderr or "").strip()
            raise IntegrateError(
                f"cannot obtain head_sha={head_sha} from worktree {worktree_path}: {err}"
            )
    if base_sha and base_sha != head_sha and not _commit_exists(root, base_sha):
        r3 = _run_git(
            ["fetch", "--no-tags", str(worktree_path), base_sha],
            cwd=root,
            timeout=120.0,
        )
        if r3.returncode != 0 or not _commit_exists(root, base_sha):
            err = (r3.stderr or "").strip()
            raise IntegrateError(
                f"cannot obtain base_sha={base_sha} from worktree {worktree_path}: {err}"
            )


def _cherry_pick(
    root: Path,
    head_sha: str,
    *,
    base_sha: str | None = None,
) -> str:
    """Cherry-pick worker commits onto leader. Abort and raise on conflict.

    When ``base_sha`` is set and differs from ``head_sha``, picks the range
    ``base_sha..head_sha`` (all commits after base up to head). Otherwise picks
    the single ``head_sha`` commit.

    Returns a label describing what was picked (for result entries).
    """
    if base_sha and base_sha.lower() != head_sha.lower():
        rev = f"{base_sha}..{head_sha}"
        label = rev
        args = ["cherry-pick", "--allow-empty", rev]
    else:
        rev = head_sha
        label = head_sha
        args = ["cherry-pick", "--allow-empty", head_sha]

    r = _run_git(args, cwd=root, timeout=120.0)
    if r.returncode == 0:
        return label
    # Conflict or other failure — leave tree resolvable but abort the pick
    _run_git(["cherry-pick", "--abort"], cwd=root, timeout=30.0)
    err = (r.stderr or r.stdout or "cherry-pick failed").strip()
    raise IntegrateError(f"cherry-pick conflict or failure for {label}: {err}")


def _reset_hard(root: Path, sha: str) -> None:
    """Hard-reset leader to ``sha`` (atomic rollback after partial apply)."""
    r = _run_git(["reset", "--hard", sha], cwd=root, timeout=60.0)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "reset --hard failed").strip()
        raise IntegrateError(f"partial_reset reset --hard {sha} failed: {err}")


def integrate_results(
    root: Path | str,
    run_id: str,
    envelopes_dir: Path | str | None = None,
    *,
    dry_run: bool = False,
    skip_preflight: bool = False,
    require_squash: bool = False,
    lease: Any = None,
) -> dict[str, Any]:
    """Load ULW envelopes, apply in task_id order, write integrate.result.json.

    - ``preflight_clean_tree`` unless ``skip_preflight`` or ``dry_run`` (dry_run
      still validates envelopes / base_sha but does not require a clean tree).
    - Envelopes default path: ``.omg/artifacts/ulw-results/<run_id>/*.json``
    - ``status != ok`` → stop, overall failed (no apply for that task)
    - If run has ``base_sha``, each envelope ``base_sha`` must match
    - Before cherry-pick: ancestry check, reject merge commits, optional
      ``changed_files`` vs ``git diff --name-only``, optional ``require_squash``
    - Apply: ensure ``head_sha`` reachable, then ``git cherry-pick`` range
    - Conflict → abort cherry-pick; if any prior pick succeeded, ``reset --hard``
      to ``start_sha`` (unless dry_run) and set ``partial_reset=true``
    - Missing envelopes → result status ``missing`` (not an exception)
    - Strict-v2 status writes require an execution ``lease`` (caller-owned).
    """
    root = Path(root).resolve()

    from omg_cli.state import (
        RunSchema,
        classify_run_schema,
        execution_lease,
        load_run,
        write_status,
    )

    run = load_run(root, run_id)
    if run is None:
        raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
    schema = classify_run_schema(run)

    # Strict-v2 status mutations require a held execution lease.  Callers may
    # pass one; otherwise we acquire a short integrate lease for the write path.
    _owned_lease_cm = None
    active_lease = lease
    if schema is RunSchema.STRICT_V2 and active_lease is None and not dry_run:
        _owned_lease_cm = execution_lease(root, run_id, intent="integrate")
        active_lease = _owned_lease_cm.__enter__()

    def _write_status(status: str, *, extra: dict[str, Any] | None = None) -> None:
        write_status(
            root,
            run_id,
            status,
            extra=extra,
            lease=active_lease,
        )
    expected_env_dir = default_envelopes_dir(root, run_id)
    env_dir = Path(envelopes_dir) if envelopes_dir is not None else expected_env_dir
    try:
        env_dir_resolved = env_dir.resolve(strict=False)
        expected_resolved = expected_env_dir.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise IntegrateError(f"cannot resolve run-scoped envelopes directory: {exc}") from exc
    if env_dir_resolved != expected_resolved:
        label = schema.value
        raise IntegrateError(
            f"{label} integration reads only selected run envelopes at "
            f"{expected_env_dir}; legacy/global/custom directory {env_dir} requires "
            "the explicit reviewed legacy import path"
        )
    env_dir = expected_env_dir

    run_base = run.get("base_sha")
    if isinstance(run_base, str):
        run_base = run_base.strip().lower() or None
    else:
        run_base = None

    # Probe envelopes before clean-tree preflight so "missing" is reportable
    # even when the tree is dirty / not a git repo (pipeline ULW gate).
    result: dict[str, Any] = {
        "writer": CLI_WRITER,
        "run_id": run_id,
        "schema_classification": schema.value,
        "status": "ok",
        "dry_run": bool(dry_run),
        "require_squash": bool(require_squash),
        "envelopes_dir": str(env_dir),
        "base_sha": run_base,
        "start_sha": None,
        "applied": [],
        "envelope_hashes": {},
        "failed_task": None,
        "error": None,
        "partial_reset": False,
        "created_at": _utc_now(),
        "note": None,
    }

    try:
        # When a CLI ownership manifest exists, require join complete first.
        from omg_cli.workers import join_worker_results, ownership_manifest_path

        own_path = ownership_manifest_path(root, run_id)
        if own_path.is_file() and not dry_run:
            joined = join_worker_results(root, run_id)
            if not joined.get("complete"):
                raise IntegrateError(
                    "ownership join incomplete; refuse integrate: "
                    f"missing={joined.get('missing')} failed={joined.get('failed')}"
                )

        try:
            envelopes = load_envelopes(
                env_dir,
                expected_run_id=run_id,
                require_cli_writer=True,
                strict=schema is RunSchema.STRICT_V2,
            )
        except IntegrateError as exc:
            result["status"] = "failed"
            result["error"] = str(exc)
            _atomic_write_json(result_path(root, run_id), result)
            if not dry_run:
                _write_status(
                    "failed",
                    extra={"integrate_status": "failed", "integrate_error": str(exc)},
                )
            return result

        if not envelopes:
            result["status"] = "missing"
            note = (
                f"no envelopes under {env_dir}; "
                "workers should write "
                f".omg/artifacts/ulw-results/{run_id}/<task_id>.json "
                "with task_id, base_sha, head_sha, worktree_path, "
                "changed_files, status"
            )
            if schema is RunSchema.LEGACY_V1:
                note += (
                    "; legacy global envelopes are never auto-read; reviewed import: `"
                    + legacy_import_command(root, run_id)
                    + "`"
                )
            result["note"] = note
            _atomic_write_json(result_path(root, run_id), result)
            return result

        result["envelope_hashes"] = {
            env["task_id"]: env["_sha256"] for env in envelopes
        }
        if schema is RunSchema.STRICT_V2 and run_base is None:
            result["status"] = "failed"
            result["error"] = "strict-v2 integration requires run.base_sha"
            _atomic_write_json(result_path(root, run_id), result)
            if not dry_run:
                _write_status(
                    "failed",
                    extra={
                        "integrate_status": "failed",
                        "integrate_error": result["error"],
                    },
                )
            return result

        if not dry_run and not skip_preflight:
            preflight_clean_tree(root)

        # Record leader HEAD before any cherry-pick so partial failure can roll back.
        start_sha = git_rev_parse_head(root) if not dry_run else None
        result["start_sha"] = start_sha
        applied_ok_count = 0

        for env in envelopes:
            task_id = env["task_id"]
            entry: dict[str, Any] = {
                "task_id": task_id,
                "head_sha": env["head_sha"],
                "status": "pending",
            }

            if env["status"] != "ok":
                entry["status"] = "skipped_failed_envelope"
                entry["error"] = f"envelope status={env['status']!r} (expected ok)"
                result["applied"].append(entry)
                result["status"] = "failed"
                result["failed_task"] = task_id
                result["error"] = entry["error"]
                break

            if run_base and env["base_sha"] != run_base:
                entry["status"] = "base_sha_mismatch"
                entry["error"] = (
                    f"envelope base_sha={env['base_sha']} != run base_sha={run_base}"
                )
                result["applied"].append(entry)
                result["status"] = "failed"
                result["failed_task"] = task_id
                result["error"] = entry["error"]
                break

            try:
                worktree = assert_worktree_path_allowed(root, env["worktree_path"])
            except IntegrateError as exc:
                entry["status"] = "worktree_path_denied"
                entry["error"] = str(exc)
                result["applied"].append(entry)
                result["status"] = "failed"
                result["failed_task"] = task_id
                result["error"] = str(exc)
                break

            entry["worktree_path"] = str(worktree)
            pick_base = env.get("base_sha")
            if isinstance(pick_base, str) and pick_base.strip():
                entry["pick"] = (
                    f"{pick_base}..{env['head_sha']}"
                    if pick_base.lower() != env["head_sha"].lower()
                    else env["head_sha"]
                )
            else:
                entry["pick"] = env["head_sha"]

            # Range preflight (ancestry / merge / changed_files / require_squash)
            # needs objects reachable in the leader object store first.
            try:
                if not dry_run:
                    _ensure_commit_reachable(
                        root,
                        env["head_sha"],
                        worktree,
                        base_sha=pick_base if isinstance(pick_base, str) else None,
                    )
                # dry_run still needs objects if present; try best-effort fetch
                elif not _commit_exists(root, env["head_sha"]):
                    try:
                        _ensure_commit_reachable(
                            root,
                            env["head_sha"],
                            worktree,
                            base_sha=pick_base if isinstance(pick_base, str) else None,
                        )
                    except IntegrateError:
                        pass  # dry_run may skip if worktree absent

                if _commit_exists(root, env["head_sha"]) and (
                    not pick_base
                    or pick_base.lower() == env["head_sha"].lower()
                    or _commit_exists(root, str(pick_base))
                ):
                    preflight_envelope_range(
                        root,
                        str(pick_base or env["head_sha"]),
                        env["head_sha"],
                        list(env.get("changed_files") or []),
                        require_squash=require_squash,
                    )
                elif not dry_run:
                    raise IntegrateError(
                        f"commits not reachable for range preflight: "
                        f"base={pick_base} head={env['head_sha']}"
                    )
            except IntegrateError as exc:
                entry["status"] = "failed"
                entry["error"] = str(exc)
                result["applied"].append(entry)
                result["status"] = "failed"
                result["failed_task"] = task_id
                result["error"] = str(exc)
                if applied_ok_count > 0 and start_sha and not dry_run:
                    try:
                        _reset_hard(root, start_sha)
                        result["partial_reset"] = True
                        result["reset_to"] = start_sha
                    except IntegrateError as reset_exc:
                        result["error"] = (
                            f"{exc}; additionally partial_reset failed: {reset_exc}"
                        )
                        result["partial_reset"] = False
                break

            if dry_run:
                entry["status"] = "dry_run_ok"
                result["applied"].append(entry)
                continue

            try:
                picked = _cherry_pick(
                    root,
                    env["head_sha"],
                    base_sha=pick_base if isinstance(pick_base, str) else None,
                )
                entry["status"] = "applied"
                entry["pick"] = picked
                result["applied"].append(entry)
                applied_ok_count += 1
            except IntegrateError as exc:
                entry["status"] = "failed"
                entry["error"] = str(exc)
                result["applied"].append(entry)
                result["status"] = "failed"
                result["failed_task"] = task_id
                result["error"] = str(exc)
                # Atomic integrate: if earlier cherry-picks succeeded, roll back
                # to start_sha so leader is not left in a partial-merge state.
                if applied_ok_count > 0 and start_sha and not dry_run:
                    try:
                        _reset_hard(root, start_sha)
                        result["partial_reset"] = True
                        result["reset_to"] = start_sha
                    except IntegrateError as reset_exc:
                        # Surface both the original conflict and the reset failure.
                        result["error"] = (
                            f"{exc}; additionally partial_reset failed: {reset_exc}"
                        )
                        result["partial_reset"] = False
                break

        # Also roll back if we failed for non-pick reasons after some applies
        # (e.g. later envelope base_sha mismatch should not leave partial state).
        # Those breaks happen before apply for that task; if applied_ok_count>0
        # and status failed without partial_reset yet, reset now.
        if (
            not dry_run
            and result["status"] == "failed"
            and applied_ok_count > 0
            and start_sha
            and not result.get("partial_reset")
            and result.get("reset_to") is None
        ):
            # Check whether failure was after successful applies without reset
            # (base_sha / skipped_failed paths break without incrementing after
            # prior applies — rare if all share same base, but be safe).
            try:
                _reset_hard(root, start_sha)
                result["partial_reset"] = True
                result["reset_to"] = start_sha
            except IntegrateError as reset_exc:
                result["error"] = (
                    f"{result.get('error')}; additionally partial_reset failed: {reset_exc}"
                )

        result["finished_at"] = _utc_now()
        _atomic_write_json(result_path(root, run_id), result)

        if not dry_run:
            if result["status"] == "ok":
                # Do not set verified — acceptance still required
                _write_status(
                    str(run.get("status") or "running"),
                    extra={"integrate_status": "ok"},
                )
            elif result["status"] == "failed":
                _write_status(
                    "failed",
                    extra={
                        "integrate_status": "failed",
                        "integrate_error": result.get("error"),
                        "partial_reset": bool(result.get("partial_reset")),
                    },
                )

        return result
    finally:
        if _owned_lease_cm is not None:
            _owned_lease_cm.__exit__(None, None, None)

