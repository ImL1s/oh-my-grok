"""Strict OMG-owned worktree create, seal, deliver, integrate, and cleanup."""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
    safe_path_key,
)
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_git_oid,
    require_integer,
    require_iso8601,
    require_safe_id,
    require_sha256,
)
from omg_cli.contracts.team_envelope import _safe_write_path, _validate_argv
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
    sha256_hex,
)
from omg_cli.evidence import safe_supervised_child_env
from omg_cli.redaction import redact_text
from omg_cli.integrate import (
    IntegrateError,
    _run_git,
    assert_ancestor,
    git_rev_parse_head,
    preflight_clean_tree,
    preflight_envelope_range,
    verify_changed_files,
)


CLI_WRITER = "omg-cli"
WORKTREE_STATES = frozenset(
    {"created", "sealed", "integrated", "conflict", "cancelled", "cleaned"}
)


class TeamWorktreeError(RuntimeError):
    """Owned worktree identity, contents, or lifecycle failed closed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def _repository_integration_lock(root: Path) -> Iterator[str]:
    """Serialize the complete leader mutation transaction repository-wide."""
    lock_dir = root / ".omg" / "state" / "team-integration"
    ensure_managed_dir(lock_dir)
    lock_path = lock_dir / "repository.lock"
    owner_path = lock_dir / "owner.json"
    token = uuid.uuid4().hex
    with exclusive_lock(lock_path):
        owner = {
            "writer": CLI_WRITER,
            "pid": os.getpid(),
            "token": token,
            "acquired_at": _utc_now(),
        }
        atomic_write_bytes(
            owner_path,
            canonical_json_bytes(owner),
            mode=DATA_FILE_MODE,
            replace=True,
        )
        try:
            observed = parse_canonical_json_bytes(owner_path.read_bytes())
            if not isinstance(observed, dict) or observed.get("token") != token:
                raise TeamWorktreeError("repository integration lock owner drift")
            yield token
            observed = parse_canonical_json_bytes(owner_path.read_bytes())
            if not isinstance(observed, dict) or observed.get("token") != token:
                raise TeamWorktreeError("repository integration lock token changed")
        finally:
            try:
                observed = parse_canonical_json_bytes(owner_path.read_bytes())
            except (OSError, ValueError):
                observed = None
            if isinstance(observed, dict) and observed.get("token") == token:
                owner_path.unlink(missing_ok=True)


def _normalize_owned_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise TeamWorktreeError("owned path must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise TeamWorktreeError("owned path must be normalized repository-relative")
    try:
        return _safe_write_path(value)
    except ContractValidationError as exc:
        raise TeamWorktreeError(str(exc)) from exc


def _team_root(root: Path | str, run_id: str, team_id: str) -> Path:
    require_safe_id(run_id, label="run_id")
    require_safe_id(team_id, label="team_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "team"
        / safe_path_key(team_id, namespace="team")
    )


def owned_worktree_path(
    root: Path | str, run_id: str, team_id: str, task_id: str
) -> Path:
    require_safe_id(task_id, label="task_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "worktrees"
        / safe_path_key(run_id, namespace="run")
        / safe_path_key(team_id, namespace="team")
        / safe_path_key(task_id, namespace="task")
    )


def worktree_receipt_path(
    root: Path | str, run_id: str, team_id: str, task_id: str
) -> Path:
    require_safe_id(task_id, label="task_id")
    return (
        _team_root(root, run_id, team_id)
        / "worktrees"
        / (safe_path_key(task_id, namespace="task") + ".json")
    )


def _worktree_receipt_history_path(
    root: Path | str,
    run_id: str,
    team_id: str,
    task_id: str,
    generation: int,
) -> Path:
    require_integer(generation, label="generation", minimum=0)
    return (
        _team_root(root, run_id, team_id)
        / "worktrees"
        / "history"
        / (
            safe_path_key(f"{task_id}-g{generation}", namespace="worktree-history")
            + ".json"
        )
    )


def delivery_path(
    root: Path | str, run_id: str, team_id: str, task_id: str, generation: int
) -> Path:
    require_integer(generation, label="generation", minimum=0)
    return (
        _team_root(root, run_id, team_id)
        / "deliveries"
        / (safe_path_key(f"{task_id}-g{generation}", namespace="delivery") + ".json")
    )


def _validate_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    required = {
        "store_kind",
        "schema_version",
        "writer",
        "run_id",
        "team_id",
        "task_id",
        "generation",
        "base_sha",
        "worktree_path",
        "worktree_path_hash",
        "branch",
        "owned_paths",
        "state",
        "delivery_path",
        "delivery_hash",
        "integrated_head",
        "created_at",
        "updated_at",
        "error",
    }
    if set(row) != required:
        raise ContractValidationError("owned worktree receipt keys mismatch")
    if (
        row["store_kind"] != "owned_team_worktree"
        or row["schema_version"] != 1
        or row["writer"] != CLI_WRITER
    ):
        raise ContractValidationError("owned worktree receipt header mismatch")
    for field in ("run_id", "team_id", "task_id", "branch"):
        require_safe_id(row[field], label=field)
    require_integer(row["generation"], label="generation", minimum=0)
    require_git_oid(row["base_sha"], label="base_sha")
    if not isinstance(row["worktree_path"], str) or not row["worktree_path"]:
        raise ContractValidationError("owned worktree path must be a non-empty string")
    require_sha256(row["worktree_path_hash"], label="worktree_path_hash")
    if (
        sha256_hex(str(Path(row["worktree_path"]).resolve()).encode("utf-8"))
        != row["worktree_path_hash"]
    ):
        raise ContractValidationError("owned worktree realpath hash mismatch")
    if not isinstance(row["owned_paths"], list) or not row["owned_paths"]:
        raise ContractValidationError("owned worktree paths must be non-empty")
    normalized = [_normalize_owned_path(item) for item in row["owned_paths"]]
    if normalized != sorted(set(normalized), key=lambda item: item.encode("utf-8")):
        raise ContractValidationError("owned worktree paths must be unique sorted")
    if row["state"] not in WORKTREE_STATES:
        raise ContractValidationError("owned worktree state mismatch")
    if row["delivery_hash"] is not None:
        require_sha256(row["delivery_hash"], label="delivery_hash")
    if row["delivery_path"] is not None and not isinstance(row["delivery_path"], str):
        raise ContractValidationError(
            "owned worktree delivery_path must be string or null"
        )
    if row["integrated_head"] is not None:
        require_git_oid(row["integrated_head"], label="integrated_head")
    if row["error"] is not None and not isinstance(row["error"], str):
        raise ContractValidationError("owned worktree error must be string or null")
    require_iso8601(row["created_at"], label="created_at")
    require_iso8601(row["updated_at"], label="updated_at")
    if row["state"] == "created" and (
        row["delivery_path"] is not None or row["delivery_hash"] is not None
    ):
        raise ContractValidationError("created worktree may not claim a delivery")
    if (row["delivery_path"] is None) != (row["delivery_hash"] is None):
        raise ContractValidationError("owned worktree delivery identity is partial")
    if row["state"] == "sealed" and row["delivery_hash"] is None:
        raise ContractValidationError("sealed worktree must bind a delivery")
    if row["state"] == "integrated" and (
        row["delivery_hash"] is None or row["integrated_head"] is None
    ):
        raise ContractValidationError("integrated worktree identity is incomplete")
    if (
        row["state"] in {"created", "sealed", "conflict", "cancelled"}
        and row["integrated_head"] is not None
    ):
        raise ContractValidationError("non-integrated worktree claims integrated HEAD")
    return row


def load_worktree_receipt(
    root: Path | str, *, run_id: str, team_id: str, task_id: str
) -> dict[str, Any]:
    path = worktree_receipt_path(root, run_id, team_id, task_id)
    if not path.exists():
        raise TeamWorktreeError("owned worktree receipt is missing")
    parsed = parse_canonical_json_bytes(path.read_bytes())
    if not isinstance(parsed, dict):
        raise ContractValidationError("owned worktree receipt must be an object")
    receipt = _validate_receipt(parsed)
    expected_worktree = owned_worktree_path(root, run_id, team_id, task_id).resolve()
    if Path(receipt["worktree_path"]).resolve() != expected_worktree:
        raise TeamWorktreeError(
            "owned worktree receipt path escapes its exact allocation"
        )
    if receipt["delivery_path"] is not None:
        expected_delivery = delivery_path(
            root, run_id, team_id, task_id, receipt["generation"]
        ).resolve()
        if Path(receipt["delivery_path"]).resolve() != expected_delivery:
            raise TeamWorktreeError("owned delivery path escapes its exact allocation")
    return receipt


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    ensure_managed_dir(path.parent)
    atomic_write_bytes(
        path,
        canonical_json_bytes(_validate_receipt(receipt)),
        mode=DATA_FILE_MODE,
        replace=True,
    )


def create_owned_worktree(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    generation: int,
    base_sha: str,
    owned_paths: Sequence[str],
) -> dict[str, Any]:
    """Create an exact-base linked worktree; no mkdir/clone fallback exists."""

    root_path = Path(root).resolve()
    require_integer(generation, label="generation", minimum=0)
    require_git_oid(base_sha, label="base_sha")
    owned = sorted(
        {_normalize_owned_path(item) for item in owned_paths},
        key=lambda item: item.encode("utf-8"),
    )
    if not owned:
        raise TeamWorktreeError("at least one owned path is required")
    path = worktree_receipt_path(root_path, run_id, team_id, task_id)
    wt = owned_worktree_path(root_path, run_id, team_id, task_id)
    branch = safe_path_key(
        f"omg-{run_id}-{team_id}-{task_id}-g{generation}", namespace="branch"
    )[:63]
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        previous_receipt: dict[str, Any] | None = None
        if path.exists():
            current = load_worktree_receipt(
                root_path, run_id=run_id, team_id=team_id, task_id=task_id
            )
            identity = {
                "run_id": run_id,
                "team_id": team_id,
                "task_id": task_id,
                "generation": generation,
                "base_sha": base_sha,
                "worktree_path": str(wt),
                "worktree_path_hash": sha256_hex(str(wt.resolve()).encode("utf-8")),
                "branch": branch,
                "owned_paths": owned,
            }
            if not any(
                current[field] != expected for field, expected in identity.items()
            ):
                return current
            if not (
                current["state"] in {"cancelled", "cleaned"}
                and generation == current["generation"] + 1
                and not wt.exists()
            ):
                raise TeamWorktreeError(
                    "owned worktree identity already exists with different bytes"
                )
            previous_receipt = current
        try:
            preflight_clean_tree(root_path)
        except IntegrateError as exc:
            raise TeamWorktreeError(str(exc)) from exc
        head = git_rev_parse_head(root_path)
        if head != base_sha:
            raise TeamWorktreeError(
                f"leader HEAD {head!r} differs from requested base {base_sha!r}"
            )
        if wt.exists():
            raise TeamWorktreeError("unreceipted worktree path already exists")
        timestamp = _utc_now()
        candidate = _validate_receipt(
            {
                "store_kind": "owned_team_worktree",
                "schema_version": 1,
                "writer": CLI_WRITER,
                "run_id": run_id,
                "team_id": team_id,
                "task_id": task_id,
                "generation": generation,
                "base_sha": base_sha,
                "worktree_path": str(wt),
                "worktree_path_hash": sha256_hex(str(wt.resolve()).encode("utf-8")),
                "branch": branch,
                "owned_paths": owned,
                "state": "created",
                "delivery_path": None,
                "delivery_hash": None,
                "integrated_head": None,
                "created_at": timestamp,
                "updated_at": timestamp,
                "error": None,
            }
        )
        ensure_managed_dir(wt.parent)
        result = _run_git(
            ["worktree", "add", "-b", branch, str(wt), base_sha],
            cwd=root_path,
            timeout=120.0,
        )
        if result.returncode != 0:
            raise TeamWorktreeError(
                "git worktree add failed: "
                + (result.stderr or result.stdout or "").strip()
            )
        if git_rev_parse_head(wt) != base_sha:
            _run_git(["worktree", "remove", "--force", str(wt)], cwd=root_path)
            raise TeamWorktreeError("created worktree HEAD does not match exact base")
        try:
            if previous_receipt is not None:
                history = _worktree_receipt_history_path(
                    root_path,
                    run_id,
                    team_id,
                    task_id,
                    previous_receipt["generation"],
                )
                history_body = canonical_json_bytes(previous_receipt)
                ensure_managed_dir(history.parent)
                if history.exists() and history.read_bytes() != history_body:
                    raise TeamWorktreeError(
                        "immutable worktree generation history already differs"
                    )
                if not history.exists():
                    atomic_write_bytes(
                        history,
                        history_body,
                        mode=DATA_FILE_MODE,
                        replace=False,
                    )
            _write_receipt(path, candidate)
        except Exception:
            _run_git(["worktree", "remove", "--force", str(wt)], cwd=root_path)
            try:
                _delete_owned_branch(root_path, branch)
            except TeamWorktreeError:
                pass
            raise
    return candidate


def _changed_paths(cwd: Path, base_sha: str, head_sha: str) -> list[str]:
    result = _run_git(["diff", "--name-only", base_sha, head_sha], cwd=cwd)
    if result.returncode != 0:
        raise TeamWorktreeError("git diff failed while sealing worktree")
    return sorted(
        {line.strip() for line in (result.stdout or "").splitlines() if line.strip()},
        key=lambda item: item.encode("utf-8"),
    )


def _delete_owned_branch(root: Path, branch: str) -> None:
    ref = f"refs/heads/{branch}"
    exists = _run_git(["show-ref", "--verify", "--quiet", ref], cwd=root)
    if exists.returncode == 1:
        return
    if exists.returncode != 0:
        raise TeamWorktreeError("git could not inspect the owned worktree branch")
    deleted = _run_git(["branch", "-D", branch], cwd=root)
    if deleted.returncode != 0:
        raise TeamWorktreeError("git could not delete the owned worktree branch")


def _run_verification(
    cwd: Path, commands: Sequence[Sequence[str]], *, timeout: int = 300
) -> list[dict[str, Any]]:
    if len(commands) > 32:
        raise TeamWorktreeError("verification command count exceeds hard cap")
    evidence: list[dict[str, Any]] = []
    for raw in commands:
        argv = list(raw)
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise TeamWorktreeError("verification command must be non-empty argv")
        _validate_argv(argv)
        try:
            result = subprocess.run(
                argv,
                cwd=str(cwd),
                env=safe_supervised_child_env(os.environ),
                capture_output=True,
                timeout=timeout,
            )
            rc = int(result.returncode)
            stdout = bytes(result.stdout or b"")
            stderr = bytes(result.stderr or b"")
        except subprocess.TimeoutExpired as exc:
            rc = 124
            stdout = bytes(exc.stdout or b"")
            stderr = bytes(exc.stderr or b"")
        row = {
            "argv": argv,
            "rc": rc,
            "stdout_sha256": sha256_hex(stdout),
            "stderr_sha256": sha256_hex(stderr),
        }
        evidence.append(row)
        if rc != 0:
            raise TeamWorktreeError(f"verification failed rc={rc}: {argv!r}")
    return evidence


def _validate_delivery(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    required = {
        "store_kind",
        "schema_version",
        "writer",
        "run_id",
        "team_id",
        "task_id",
        "generation",
        "base_sha",
        "head_sha",
        "branch",
        "worktree_path",
        "owned_paths",
        "changed_files",
        "verification",
        "sealed_at",
    }
    if set(row) != required:
        raise ContractValidationError("owned worktree delivery keys mismatch")
    if (
        row["store_kind"] != "owned_worktree_delivery"
        or row["schema_version"] != 1
        or row["writer"] != CLI_WRITER
    ):
        raise ContractValidationError("owned worktree delivery header mismatch")
    for field in ("run_id", "team_id", "task_id", "branch"):
        require_safe_id(row[field], label=field)
    require_integer(row["generation"], label="generation", minimum=0)
    require_git_oid(row["base_sha"], label="base_sha")
    require_git_oid(row["head_sha"], label="head_sha")
    if row["base_sha"] == row["head_sha"]:
        raise ContractValidationError("owned worktree delivery must advance HEAD")
    if not isinstance(row["worktree_path"], str) or not row["worktree_path"]:
        raise ContractValidationError("owned worktree delivery path is missing")
    owned = [_normalize_owned_path(item) for item in row.get("owned_paths") or []]
    changed = [_normalize_owned_path(item) for item in row.get("changed_files") or []]
    if not owned or owned != sorted(set(owned), key=lambda item: item.encode("utf-8")):
        raise ContractValidationError("delivery owned_paths must be unique sorted")
    if not changed or changed != sorted(
        set(changed), key=lambda item: item.encode("utf-8")
    ):
        raise ContractValidationError(
            "delivery changed_files must be non-empty unique sorted"
        )
    if set(changed) - set(owned):
        raise ContractValidationError("delivery contains unowned changed files")
    verification = row["verification"]
    if not isinstance(verification, list) or len(verification) > 32:
        raise ContractValidationError("delivery verification evidence is unbounded")
    for raw in verification:
        if not isinstance(raw, Mapping):
            raise ContractValidationError("delivery verification row must be an object")
        item = dict(raw)
        if set(item) != {
            "argv",
            "rc",
            "stdout_sha256",
            "stderr_sha256",
        }:
            raise ContractValidationError("delivery verification row keys mismatch")
        if (
            not isinstance(item["argv"], list)
            or not item["argv"]
            or not all(
                isinstance(argument, str) and argument for argument in item["argv"]
            )
        ):
            raise ContractValidationError("delivery verification argv is missing")
        _validate_argv(item["argv"])
        require_integer(item["rc"], label="verification.rc", minimum=0)
        if item["rc"] != 0:
            raise ContractValidationError(
                "sealed delivery contains failed verification"
            )
        require_sha256(item["stdout_sha256"], label="verification.stdout_sha256")
        require_sha256(item["stderr_sha256"], label="verification.stderr_sha256")
    require_iso8601(row["sealed_at"], label="sealed_at")
    return row


def seal_owned_worktree(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    generation: int,
    verification_commands: Sequence[Sequence[str]] = (),
    message: str = "omg native team delivery",
) -> dict[str, Any]:
    """Commit only owned paths and write an immutable delivery envelope."""

    root_path = Path(root).resolve()
    receipt_path = worktree_receipt_path(root_path, run_id, team_id, task_id)
    with exclusive_lock(receipt_path.with_suffix(".lock")):
        receipt = load_worktree_receipt(
            root_path, run_id=run_id, team_id=team_id, task_id=task_id
        )
        if receipt["generation"] != generation:
            raise TeamWorktreeError("owned worktree seal generation/state mismatch")
        if receipt["state"] == "sealed":
            delivery, digest = _load_delivery(receipt)
            return {
                "delivery": delivery,
                "delivery_hash": digest,
                "receipt": receipt,
                "duplicate": True,
            }
        if receipt["state"] != "created":
            raise TeamWorktreeError("owned worktree seal generation/state mismatch")
        wt = Path(receipt["worktree_path"]).resolve()
        if not wt.is_dir() or git_rev_parse_head(wt) is None:
            raise TeamWorktreeError("owned worktree checkout is missing")
        status = _run_git(["status", "--porcelain"], cwd=wt)
        if status.returncode != 0:
            raise TeamWorktreeError("owned worktree status failed")
        if not (status.stdout or "").strip():
            raise TeamWorktreeError("owned worktree has no changes to seal")
        add = _run_git(["add", "-A"], cwd=wt)
        if add.returncode != 0:
            raise TeamWorktreeError("git add failed while sealing worktree")
        staged = _run_git(["diff", "--cached", "--name-only"], cwd=wt)
        changed = sorted(
            {
                line.strip()
                for line in (staged.stdout or "").splitlines()
                if line.strip()
            },
            key=lambda item: item.encode("utf-8"),
        )
        if not changed:
            raise TeamWorktreeError("owned worktree staged no deliverable paths")
        foreign = sorted(set(changed) - set(receipt["owned_paths"]))
        if foreign:
            _run_git(["reset"], cwd=wt)
            raise TeamWorktreeError(f"worktree changed unowned paths: {foreign!r}")
        commit = _run_git(["commit", "-m", message], cwd=wt, timeout=120.0)
        if commit.returncode != 0:
            raise TeamWorktreeError(
                "git commit failed while sealing: "
                + (commit.stderr or commit.stdout or "").strip()
            )
        if (_run_git(["status", "--porcelain"], cwd=wt).stdout or "").strip():
            raise TeamWorktreeError("owned worktree remained dirty after seal")
        head_sha = git_rev_parse_head(wt)
        if head_sha is None or head_sha == receipt["base_sha"]:
            raise TeamWorktreeError("sealed worktree did not advance HEAD")
        try:
            assert_ancestor(wt, receipt["base_sha"], head_sha)
            changed = _changed_paths(wt, receipt["base_sha"], head_sha)
            if set(changed) - set(receipt["owned_paths"]):
                raise TeamWorktreeError("sealed commit range contains unowned paths")
            preflight_envelope_range(
                wt, receipt["base_sha"], head_sha, changed, require_squash=True
            )
        except IntegrateError as exc:
            raise TeamWorktreeError(str(exc)) from exc
        try:
            verification = _run_verification(wt, verification_commands)
        except TeamWorktreeError as exc:
            failed = {
                **receipt,
                "state": "conflict",
                "updated_at": _utc_now(),
                "error": redact_text(str(exc)),
            }
            _write_receipt(receipt_path, failed)
            raise
        delivery = _validate_delivery(
            {
                "store_kind": "owned_worktree_delivery",
                "schema_version": 1,
                "writer": CLI_WRITER,
                "run_id": run_id,
                "team_id": team_id,
                "task_id": task_id,
                "generation": generation,
                "base_sha": receipt["base_sha"],
                "head_sha": head_sha,
                "branch": receipt["branch"],
                "worktree_path": str(wt),
                "owned_paths": receipt["owned_paths"],
                "changed_files": changed,
                "verification": verification,
                "sealed_at": _utc_now(),
            }
        )
        body = canonical_json_bytes(delivery)
        digest = sha256_hex(body)
        out = delivery_path(root_path, run_id, team_id, task_id, generation)
        ensure_managed_dir(out.parent)
        if out.exists() and out.read_bytes() != body:
            raise TeamWorktreeError(
                "immutable delivery path already has different bytes"
            )
        if not out.exists():
            atomic_write_bytes(out, body, mode=DATA_FILE_MODE, replace=False)
        updated = {
            **receipt,
            "state": "sealed",
            "delivery_path": str(out),
            "delivery_hash": digest,
            "updated_at": _utc_now(),
        }
        _write_receipt(receipt_path, updated)
    return {
        "delivery": delivery,
        "delivery_hash": digest,
        "receipt": updated,
        "duplicate": False,
    }


def _load_delivery(receipt: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    path = Path(str(receipt["delivery_path"]))
    if not path.is_file():
        raise TeamWorktreeError("owned delivery file is missing")
    body = path.read_bytes()
    digest = sha256_hex(body)
    if digest != receipt["delivery_hash"]:
        raise TeamWorktreeError("owned delivery hash drift")
    parsed = parse_canonical_json_bytes(body)
    if not isinstance(parsed, dict):
        raise TeamWorktreeError("owned delivery must be an object")
    return _validate_delivery(parsed), digest


def integrate_owned_delivery(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    generation: int,
    delivery_hash: str,
    post_integration_commands: Sequence[Sequence[str]] = (),
) -> dict[str, Any]:
    """Leader integrates the exact sealed commit and runs fresh tests."""

    root_path = Path(root).resolve()
    require_sha256(delivery_hash, label="delivery_hash")
    receipt_path = worktree_receipt_path(root_path, run_id, team_id, task_id)
    with _repository_integration_lock(root_path):
        with exclusive_lock(receipt_path.with_suffix(".lock")):
            receipt = load_worktree_receipt(
                root_path, run_id=run_id, team_id=team_id, task_id=task_id
            )
            if (
                receipt["generation"] == generation
                and receipt["state"] == "integrated"
                and receipt["delivery_hash"] == delivery_hash
            ):
                return {
                    "status": "integrated",
                    "delivery_hash": delivery_hash,
                    "integrated_head": receipt["integrated_head"],
                    "verification": [],
                    "receipt": receipt,
                    "duplicate": True,
                }
            if (
                receipt["generation"] != generation
                or receipt["state"] != "sealed"
                or receipt["delivery_hash"] != delivery_hash
            ):
                raise TeamWorktreeError(
                    "delivery integration generation/state/hash mismatch"
                )
            delivery, observed_hash = _load_delivery(receipt)
            if observed_hash != delivery_hash:
                raise TeamWorktreeError("delivery hash mismatch")
            expected = {
                "run_id": run_id,
                "team_id": team_id,
                "task_id": task_id,
                "generation": generation,
                "base_sha": receipt["base_sha"],
                "branch": receipt["branch"],
                "worktree_path": receipt["worktree_path"],
                "owned_paths": receipt["owned_paths"],
            }
            if any(delivery.get(field) != value for field, value in expected.items()):
                raise TeamWorktreeError(
                    "delivery identity differs from worktree receipt"
                )
            try:
                preflight_clean_tree(root_path)
            except IntegrateError as exc:
                raise TeamWorktreeError(str(exc)) from exc
            start_sha = git_rev_parse_head(root_path)
            if start_sha is None:
                raise TeamWorktreeError("leader HEAD unavailable")
            try:
                assert_ancestor(root_path, receipt["base_sha"], delivery["head_sha"])
                verify_changed_files(
                    root_path,
                    receipt["base_sha"],
                    delivery["head_sha"],
                    list(delivery["changed_files"]),
                )
            except IntegrateError as exc:
                raise TeamWorktreeError(str(exc)) from exc
            pick = _run_git(
                ["cherry-pick", delivery["head_sha"]], cwd=root_path, timeout=120.0
            )
            if pick.returncode != 0:
                _run_git(["cherry-pick", "--abort"], cwd=root_path, timeout=30.0)
                updated = {
                    **receipt,
                    "state": "conflict",
                    "updated_at": _utc_now(),
                    "error": redact_text(
                        (pick.stderr or pick.stdout or "cherry-pick conflict").strip()
                    ),
                }
                _write_receipt(receipt_path, updated)
                return {"status": "conflict", "receipt": updated}
            try:
                verification = _run_verification(root_path, post_integration_commands)
            except TeamWorktreeError:
                reset = _run_git(["reset", "--hard", start_sha], cwd=root_path)
                if reset.returncode != 0:
                    raise TeamWorktreeError(
                        "post-integration verification and rollback both failed"
                    )
                try:
                    preflight_clean_tree(root_path)
                except Exception as exc:
                    raise TeamWorktreeError(
                        "post-integration verification rollback left a dirty leader tree"
                    ) from exc
                raise
            integrated_head = git_rev_parse_head(root_path)
            updated = {
                **receipt,
                "state": "integrated",
                "integrated_head": integrated_head,
                "updated_at": _utc_now(),
                "error": None,
            }
            _write_receipt(receipt_path, updated)
            return {
                "status": "integrated",
                "delivery_hash": delivery_hash,
                "integrated_head": integrated_head,
                "verification": verification,
                "receipt": updated,
                "duplicate": False,
            }


def cancel_owned_worktree(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    generation: int,
) -> dict[str, Any]:
    """Cancel only the exact receipted worktree; never scan processes/paths."""

    root_path = Path(root).resolve()
    receipt_path = worktree_receipt_path(root_path, run_id, team_id, task_id)
    with exclusive_lock(receipt_path.with_suffix(".lock")):
        receipt = load_worktree_receipt(
            root_path, run_id=run_id, team_id=team_id, task_id=task_id
        )
        if receipt["generation"] != generation:
            raise TeamWorktreeError("worktree cancel generation mismatch")
        if receipt["state"] == "cancelled":
            return receipt
        if receipt["state"] == "integrated":
            raise TeamWorktreeError("integrated worktree cannot be cancelled")
        if receipt["state"] not in {"created", "sealed", "conflict"}:
            raise TeamWorktreeError("worktree state cannot be cancelled")
        wt = Path(receipt["worktree_path"]).resolve()
        expected = owned_worktree_path(root_path, run_id, team_id, task_id).resolve()
        if wt != expected:
            raise TeamWorktreeError("worktree cancel path is not the exact owned path")
        if wt.exists():
            result = _run_git(["worktree", "remove", "--force", str(wt)], cwd=root_path)
            if result.returncode != 0:
                raise TeamWorktreeError("git worktree remove failed during cancel")
        _delete_owned_branch(root_path, receipt["branch"])
        updated = {
            **receipt,
            "state": "cancelled",
            "updated_at": _utc_now(),
            "error": "cancelled",
        }
        _write_receipt(receipt_path, updated)
        return updated


def cleanup_owned_worktree(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    generation: int,
) -> dict[str, Any]:
    """Idempotently clean only integrated/cancelled owned worktrees."""

    root_path = Path(root).resolve()
    receipt_path = worktree_receipt_path(root_path, run_id, team_id, task_id)
    with exclusive_lock(receipt_path.with_suffix(".lock")):
        receipt = load_worktree_receipt(
            root_path, run_id=run_id, team_id=team_id, task_id=task_id
        )
        if receipt["generation"] != generation:
            raise TeamWorktreeError("worktree cleanup generation mismatch")
        if receipt["state"] == "cleaned":
            return receipt
        if receipt["state"] not in {"integrated", "cancelled"}:
            raise TeamWorktreeError(
                "only integrated/cancelled worktrees may be cleaned"
            )
        wt = Path(receipt["worktree_path"]).resolve()
        expected = owned_worktree_path(root_path, run_id, team_id, task_id).resolve()
        if wt != expected:
            raise TeamWorktreeError("worktree cleanup path is not the exact owned path")
        if wt.exists():
            result = _run_git(["worktree", "remove", "--force", str(wt)], cwd=root_path)
            if result.returncode != 0:
                raise TeamWorktreeError("git worktree remove failed during cleanup")
        _delete_owned_branch(root_path, receipt["branch"])
        updated = {
            **receipt,
            "state": "cleaned",
            "updated_at": _utc_now(),
            "error": receipt["error"],
        }
        _write_receipt(receipt_path, updated)
        return updated


__all__ = [
    "TeamWorktreeError",
    "cancel_owned_worktree",
    "cleanup_owned_worktree",
    "create_owned_worktree",
    "delivery_path",
    "integrate_owned_delivery",
    "load_worktree_receipt",
    "owned_worktree_path",
    "seal_owned_worktree",
    "worktree_receipt_path",
]
