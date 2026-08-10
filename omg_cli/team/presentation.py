"""Team Presentation State V1 — pure read-only projection (#69 PR6).

``build_team_presentation_v1`` maps canonical persisted Team facts into one
versioned object shared by CLI, catalog API, and MCP. No tmux / Jobs /
provider / network probes and no state writes. Never sets ``verified``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from omg_cli.state import load_active_run
from omg_cli.team.launch import WorkerLaunchError, validate_execution_record
from omg_cli.team.roles import UnknownRoleError, normalize_role, role_posture

PRESENTATION_KIND = "omg.team.presentation_state"
PRESENTATION_SCHEMA_VERSION = 1
ROUTE_SCHEMA = 1
ROUTE_KIND_EXTERNAL = "external_executor"
ROUTE_KIND_UNKNOWN = "unknown"
ROUTE_KIND_NATIVE_RECEIPT = "native_host_receipt"
MCP_PROJECTION_V1 = "presentation.v1"

_SUMMARY_MAX = 200
_SECRET_KEYS = frozenset(
    {
        "token",
        "owner_token",
        "claim_token",
        "idempotency_key",
        "argv",
        "prompt",
        "environment",
        "env",
        "credentials",
        "password",
        "secret",
        "authorization",
    }
)
_ABS_PATH_RE = re.compile(r"(^|[\s\"'])(/Users/|/home/|/private/|/var/folders/|\\\\)")


class PresentationError(ValueError):
    """Fail-closed presentation projection error."""

    def __init__(self, message: str, *, code: str = "E_TEAM_PRESENTATION") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_external_route(
    *,
    executor: str | None,
    provider: str | None,
    role: str | None,
    posture: str | None,
) -> dict[str, Any]:
    """Additive route descriptor stamped on new start/scale records."""
    return {
        "schema": ROUTE_SCHEMA,
        "kind": ROUTE_KIND_EXTERNAL,
        "executor": _optional_str(executor),
        "provider": _optional_str(provider),
        "role": _optional_str(role),
        "posture": _optional_str(posture),
    }


def stamp_route_on_task(
    task: MutableMapping[str, Any],
    *,
    executor: str | None = None,
    provider: str | None = None,
    role: str | None = None,
    posture: str | None = None,
) -> dict[str, Any]:
    """Stamp ``route`` from canonical launch facts (additive; never inferred)."""
    prov = provider if provider is not None else task.get("provider")
    role_v = role if role is not None else task.get("role")
    posture_v = posture if posture is not None else task.get("posture")
    if posture_v is None and isinstance(role_v, str) and role_v.strip():
        try:
            posture_v = role_posture(str(role_v))
        except UnknownRoleError:
            posture_v = None
    exec_v = executor
    if exec_v is None:
        exec_v = prov
    route = build_external_route(
        executor=str(exec_v) if exec_v is not None else None,
        provider=str(prov) if prov is not None else None,
        role=str(role_v) if role_v is not None else None,
        posture=str(posture_v) if posture_v is not None else None,
    )
    task["route"] = route
    return route


def validate_route_descriptor(raw: Any) -> dict[str, Any]:
    """Validate a persisted route; refuse malformed shapes fail-closed."""
    if not isinstance(raw, Mapping):
        raise PresentationError(
            "route must be an object",
            code="E_TEAM_PRESENTATION_ROUTE",
        )
    schema = raw.get("schema")
    if schema != ROUTE_SCHEMA or isinstance(schema, bool):
        raise PresentationError(
            "route.schema must be 1",
            code="E_TEAM_PRESENTATION_ROUTE",
        )
    kind = raw.get("kind")
    if kind not in (
        ROUTE_KIND_EXTERNAL,
        ROUTE_KIND_UNKNOWN,
        ROUTE_KIND_NATIVE_RECEIPT,
    ):
        raise PresentationError(
            f"unsupported route.kind {kind!r}",
            code="E_TEAM_PRESENTATION_ROUTE",
        )
    if kind == ROUTE_KIND_NATIVE_RECEIPT:
        ref = raw.get("receipt_ref")
        digest = raw.get("receipt_digest")
        if not isinstance(ref, str) or not ref.strip():
            raise PresentationError(
                "native route requires receipt_ref",
                code="E_TEAM_PRESENTATION_ROUTE",
            )
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PresentationError(
                "native route requires sha256 receipt_digest",
                code="E_TEAM_PRESENTATION_ROUTE",
            )
        _assert_relative_safe(ref, label="receipt_ref")
        return {
            "schema": ROUTE_SCHEMA,
            "kind": ROUTE_KIND_NATIVE_RECEIPT,
            "receipt_ref": ref.strip().replace("\\", "/"),
            "receipt_digest": digest,
            "executor": None,
            "provider": None,
            "role": None,
            "posture": None,
        }
    out: dict[str, Any] = {
        "schema": ROUTE_SCHEMA,
        "kind": kind,
        "executor": _optional_str(raw.get("executor")),
        "provider": _optional_str(raw.get("provider")),
        "role": _optional_str(raw.get("role")),
        "posture": _optional_str(raw.get("posture")),
    }
    return out


def unknown_route() -> dict[str, Any]:
    """Legacy records without a stamped route render honest unknown."""
    return {
        "schema": ROUTE_SCHEMA,
        "kind": ROUTE_KIND_UNKNOWN,
        "executor": None,
        "provider": None,
        "role": None,
        "posture": None,
    }


def build_team_presentation_v1(
    root: Path | str,
    run_id: str | None = None,
    *,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Generation-fenced read-only presentation snapshot (schema v1)."""
    root_path = Path(root).resolve()
    last_err: PresentationError | None = None
    for _attempt in range(2):
        try:
            return _build_once(root_path, run_id=run_id, team_id=team_id)
        except PresentationError as exc:
            if exc.code != "E_TEAM_PRESENTATION_RACE":
                raise
            last_err = exc
    assert last_err is not None
    raise last_err


def _build_once(
    root: Path,
    *,
    run_id: str | None,
    team_id: str | None,
) -> dict[str, Any]:
    from omg_cli.team.plane import load_team_meta
    from omg_cli.workers import WorkerError, load_ownership_manifest

    rid = run_id
    if not rid:
        active = load_active_run(root)
        if active is None:
            raise PresentationError(
                "no active run (pass run_id)",
                code="E_TEAM_PRESENTATION_NO_RUN",
            )
        rid = str(active["run_id"])

    meta = load_team_meta(root, rid)
    gen0 = _read_meta_generation(meta)
    tid = team_id or meta.get("team_id")
    if not isinstance(tid, str) or not tid.strip():
        raise PresentationError(
            "team_id missing from team.json",
            code="E_TEAM_PRESENTATION_TEAM",
        )
    tid = tid.strip()
    if team_id is not None and tid != team_id.strip():
        raise PresentationError(
            "team_id mismatch",
            code="E_TEAM_PRESENTATION_TEAM",
        )

    ownership: dict[str, Any] | None
    try:
        ownership = load_ownership_manifest(root, rid)
    except WorkerError:
        ownership = None

    api_versions = _snapshot_api_task_versions(root, rid, tid, meta)

    # Sidecars: startup facts from meta only (no pane/Jobs probes).
    startup_status = meta.get("startup_status")

    # Re-fence generations.
    meta2 = load_team_meta(root, rid)
    gen1 = _read_meta_generation(meta2)
    if gen1 != gen0:
        raise PresentationError(
            "team meta_generation changed during presentation read",
            code="E_TEAM_PRESENTATION_RACE",
        )
    api_versions2 = _snapshot_api_task_versions(root, rid, tid, meta2)
    if api_versions2 != api_versions:
        raise PresentationError(
            "api task versions changed during presentation read",
            code="E_TEAM_PRESENTATION_RACE",
        )

    members = _build_members(
        root,
        meta=meta2,
        ownership=ownership,
        run_id=rid,
        team_id=tid,
    )
    team_name = meta2.get("team_name") or tid
    if not isinstance(team_name, str) or not team_name.strip():
        team_name = tid

    payload = {
        "kind": PRESENTATION_KIND,
        "schema_version": PRESENTATION_SCHEMA_VERSION,
        "run_id": rid,
        "team_id": tid,
        "team_name": str(team_name).strip(),
        "state_generation": gen1,
        "lifecycle": {
            "dry_run": bool(meta2.get("dry_run")),
            "workspace_mode": str(meta2.get("workspace_mode") or "worktree"),
            "startup_status": (
                str(startup_status) if isinstance(startup_status, str) else None
            ),
            "worker_topology": (
                str(meta2["worker_topology"])
                if isinstance(meta2.get("worker_topology"), str)
                else None
            ),
        },
        "workspace": {
            "mode": str(meta2.get("workspace_mode") or "worktree"),
        },
        "members": members,
    }
    _assert_no_secret_sentinels(payload)
    return payload


def _build_members(
    root: Path,
    *,
    meta: Mapping[str, Any],
    ownership: Mapping[str, Any] | None,
    run_id: str,
    team_id: str,
) -> list[dict[str, Any]]:
    own_by_id: dict[str, Mapping[str, Any]] = {}
    if ownership is not None:
        for row in ownership.get("tasks") or []:
            if isinstance(row, Mapping) and row.get("task_id"):
                own_by_id[str(row["task_id"])] = row

    meta_executor = meta.get("executor")
    members: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in meta.get("tasks") or []:
        if not isinstance(raw, Mapping):
            continue
        task = dict(raw)
        tid = str(task.get("task_id") or "").strip()
        if not tid:
            raise PresentationError(
                "task missing task_id",
                code="E_TEAM_PRESENTATION_TASK",
            )
        if tid in seen_ids:
            raise PresentationError(
                f"duplicate member task_id {tid!r}",
                code="E_TEAM_PRESENTATION_DUP_MEMBER",
            )
        seen_ids.add(tid)
        binding = task.get("binding")
        api_task_id = None
        logical = tid
        if isinstance(binding, Mapping):
            logical = str(binding.get("logical_worker_id") or tid)
            api_raw = binding.get("api_task_id")
            if api_raw is not None:
                if not isinstance(api_raw, str) or not api_raw.strip():
                    raise PresentationError(
                        "binding.api_task_id malformed",
                        code="E_TEAM_PRESENTATION_BINDING",
                    )
                api_task_id = api_raw.strip()

        role = str(task.get("role") or "executor")
        capability_floor = _capability_floor(role)
        route = _project_route(task, root=root, meta_executor=meta_executor)
        worktree = _project_worktree(
            root,
            run_id=run_id,
            task_id=tid,
            raw_worktree=task.get("worktree"),
            ownership_row=own_by_id.get(tid),
        )
        attempts = _project_attempts(task, member_id=logical)
        current = attempts[-1] if attempts else None
        if current is None:
            raise PresentationError(
                f"member {tid!r} has no attempt lineage",
                code="E_TEAM_PRESENTATION_ATTEMPT",
            )
        members.append(
            {
                "logical_worker_id": logical,
                "member_id": logical,
                "task_id": tid,
                "api_task_id": api_task_id,
                "role": normalize_role(role) if role else "executor",
                "capability_floor": capability_floor,
                "route": route,
                "worktree": worktree,
                "current_attempt": current,
                "attempts": attempts,
            }
        )
    members.sort(key=lambda m: (m["logical_worker_id"], m["task_id"]))
    return members


def _project_attempts(
    task: Mapping[str, Any], *, member_id: str
) -> list[dict[str, Any]]:
    identities: set[tuple[str, int, int]] = set()
    attempts: list[dict[str, Any]] = []

    priors = task.get("prior_attempts") or []
    if priors is not None and not isinstance(priors, list):
        raise PresentationError(
            "prior_attempts must be a list",
            code="E_TEAM_PRESENTATION_HISTORY",
        )
    for raw in priors:
        if not isinstance(raw, Mapping):
            raise PresentationError(
                "prior_attempts entry must be an object",
                code="E_TEAM_PRESENTATION_HISTORY",
            )
        row = _attempt_from_prior(raw, member_id=member_id)
        key = (member_id, row["attempt"], row["launch_generation"])
        if key in identities:
            raise PresentationError(
                f"duplicate attempt identity {key!r}",
                code="E_TEAM_PRESENTATION_DUP_ATTEMPT",
            )
        identities.add(key)
        attempts.append(row)

    current = _attempt_from_live(task, member_id=member_id)
    key = (member_id, current["attempt"], current["launch_generation"])
    if key in identities:
        raise PresentationError(
            f"duplicate attempt identity {key!r}",
            code="E_TEAM_PRESENTATION_DUP_ATTEMPT",
        )
    identities.add(key)
    attempts.append(current)
    attempts.sort(key=lambda a: (a["attempt"], a["launch_generation"]))
    return attempts


def _attempt_from_prior(raw: Mapping[str, Any], *, member_id: str) -> dict[str, Any]:
    attempt = raw.get("attempt", 1)
    launch_generation = raw.get("launch_generation", 0)
    if (
        isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
        or isinstance(launch_generation, bool)
        or not isinstance(launch_generation, int)
        or launch_generation < 0
    ):
        raise PresentationError(
            "prior attempt/launch_generation corrupt",
            code="E_TEAM_PRESENTATION_CORRUPT",
        )
    execution = _project_execution(raw.get("execution"))
    start = "committed" if execution.get("topology") else "unknown"
    status = raw.get("status")
    reason = raw.get("reason")
    terminal = None
    if isinstance(status, str) and status.lower() in {
        "failed",
        "cancelled",
        "unproven",
    }:
        terminal = _terminal_failure(
            code=str(reason) if isinstance(reason, str) else status,
            summary=str(reason) if isinstance(reason, str) else status,
        )
    elif isinstance(reason, str) and reason.strip():
        terminal = _terminal_failure(code="prior_attempt", summary=reason)
    return {
        "attempt": attempt,
        "launch_generation": launch_generation,
        "start": start,
        "status": str(status) if isinstance(status, str) else None,
        "startup": None,
        "execution": execution,
        "prior_evidence": _prior_evidence(raw),
        "terminal_failure": terminal,
        "member_id": member_id,
    }


def _attempt_from_live(task: Mapping[str, Any], *, member_id: str) -> dict[str, Any]:
    attempt = task.get("attempt", 1)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise PresentationError(
            "task.attempt corrupt",
            code="E_TEAM_PRESENTATION_CORRUPT",
        )
    execution_raw = task.get("execution")
    execution = _project_execution(execution_raw)
    launch_generation = execution.get("launch_generation")
    if launch_generation is None:
        binding = task.get("binding")
        if isinstance(binding, Mapping):
            launch_generation = binding.get("launch_generation", 0)
        else:
            launch_generation = 0
    if (
        isinstance(launch_generation, bool)
        or not isinstance(launch_generation, int)
        or launch_generation < 0
    ):
        raise PresentationError(
            "launch_generation corrupt",
            code="E_TEAM_PRESENTATION_CORRUPT",
        )
    start = "committed" if execution.get("topology") is not None else "unknown"
    status = task.get("status")
    terminal = None
    if isinstance(status, str) and status.lower() in {
        "failed",
        "cancelled",
        "unproven",
    }:
        err = task.get("replacement_error") or status
        terminal = _terminal_failure(
            code="terminal",
            summary=str(err),
        )
    return {
        "attempt": attempt,
        "launch_generation": int(launch_generation),
        "start": start,
        "status": str(status) if isinstance(status, str) else None,
        "startup": None,
        "execution": execution,
        "prior_evidence": None,
        "terminal_failure": terminal,
        "member_id": member_id,
    }


def _project_execution(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {
            "topology": None,
            "launch_generation": None,
            "job_id": None,
            "pane_id": None,
        }
    if not isinstance(raw, Mapping):
        raise PresentationError(
            "execution must be an object",
            code="E_TEAM_PRESENTATION_EXEC",
        )
    if raw.get("job_id") is not None and raw.get("pane_id") is not None:
        raise PresentationError(
            "dual execution handle refused",
            code="E_TEAM_PRESENTATION_DUAL_HANDLE",
        )
    try:
        rec = validate_execution_record(raw)
    except WorkerLaunchError as exc:
        raise PresentationError(
            f"invalid execution: {exc}",
            code=getattr(exc, "code", None) or "E_TEAM_PRESENTATION_EXEC",
        ) from exc
    return {
        "topology": rec.get("topology"),
        "launch_generation": rec.get("launch_generation"),
        "job_id": rec.get("job_id"),
        "pane_id": rec.get("pane_id"),
    }


def _project_route(
    task: Mapping[str, Any],
    *,
    root: Path,
    meta_executor: Any,
) -> dict[str, Any]:
    raw = task.get("route")
    if raw is None:
        return unknown_route()
    route = validate_route_descriptor(raw)
    if route["kind"] == ROUTE_KIND_NATIVE_RECEIPT:
        _verify_native_receipt(root, route)
    # Never invent executor from meta when route already stamped unknown/native.
    if route["kind"] == ROUTE_KIND_EXTERNAL and route.get("executor") is None:
        if isinstance(meta_executor, str) and meta_executor.strip():
            route = dict(route)
            route["executor"] = meta_executor.strip()
    return route


def _verify_native_receipt(root: Path, route: Mapping[str, Any]) -> None:
    rel = str(route["receipt_ref"])
    path = root / rel
    if not path.is_file() or path.is_symlink():
        raise PresentationError(
            "native receipt missing or not a regular file",
            code="E_TEAM_PRESENTATION_NATIVE_RECEIPT",
        )
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise PresentationError(
            f"native receipt unreadable: {exc}",
            code="E_TEAM_PRESENTATION_NATIVE_RECEIPT",
        ) from exc
    digest = hashlib.sha256(body).hexdigest()
    if digest != route["receipt_digest"]:
        raise PresentationError(
            "native receipt digest mismatch",
            code="E_TEAM_PRESENTATION_NATIVE_RECEIPT",
        )


def _project_worktree(
    root: Path,
    *,
    run_id: str,
    task_id: str,
    raw_worktree: Any,
    ownership_row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    from omg_cli.workers import worktree_dir

    expected = worktree_dir(root, run_id, task_id)
    expected_rel = str(expected.relative_to(root)).replace("\\", "/")
    if raw_worktree is None or raw_worktree == "":
        rel = expected_rel
    elif isinstance(raw_worktree, str):
        rel = _relativize_worktree(root, raw_worktree, expected_rel=expected_rel)
    else:
        raise PresentationError(
            "worktree must be a string path",
            code="E_TEAM_PRESENTATION_PATH",
        )
    ownership_state = "missing"
    if ownership_row is not None:
        st = ownership_row.get("status")
        ownership_state = str(st) if isinstance(st, str) and st.strip() else "present"
    return {
        "relative_path": rel,
        "ownership_state": ownership_state,
    }


def _relativize_worktree(root: Path, raw: str, *, expected_rel: str) -> str:
    text = raw.strip()
    if not text:
        raise PresentationError(
            "empty worktree path",
            code="E_TEAM_PRESENTATION_PATH",
        )
    candidate = Path(text)
    if candidate.is_absolute():
        try:
            rel = candidate.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise PresentationError(
                "worktree path escapes project root",
                code="E_TEAM_PRESENTATION_PATH",
            ) from exc
        out = str(rel).replace("\\", "/")
    else:
        _assert_relative_safe(text, label="worktree")
        out = text.replace("\\", "/")
    # Prefer canonical expected relative when it matches the same leaf.
    if out == expected_rel or out.endswith("/" + Path(expected_rel).name):
        return expected_rel
    if ".." in Path(out).parts:
        raise PresentationError(
            "worktree path unsafe",
            code="E_TEAM_PRESENTATION_PATH",
        )
    return out


def _prior_evidence(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for key in ("provider", "role", "worktree", "reason"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            if key == "worktree":
                # Relative only — drop absolute machine paths from evidence.
                if Path(val).is_absolute() or _ABS_PATH_RE.search(val):
                    continue
                _assert_relative_safe(val, label="prior worktree")
            evidence.append({"kind": key, "value": _bound_summary(val)})
    return evidence


def _terminal_failure(*, code: str, summary: str) -> dict[str, Any]:
    return {
        "code": _bound_summary(code, limit=64),
        "summary": _bound_summary(summary),
    }


def _capability_floor(role: str) -> str:
    try:
        return role_posture(role)
    except UnknownRoleError:
        return "unknown"


def _snapshot_api_task_versions(
    root: Path,
    run_id: str,
    team_id: str,
    meta: Mapping[str, Any],
) -> dict[str, int]:
    from omg_cli.team import api as team_api

    versions: dict[str, int] = {}
    for raw in meta.get("tasks") or []:
        if not isinstance(raw, Mapping):
            continue
        binding = raw.get("binding")
        if not isinstance(binding, Mapping):
            continue
        api_id = binding.get("api_task_id")
        if not isinstance(api_id, str) or not api_id.strip():
            continue
        try:
            task = team_api._read_task(root, run_id, team_id, api_id.strip())
        except team_api.TeamApiError as exc:
            raise PresentationError(
                f"api task read failed: {exc}",
                code="E_TEAM_PRESENTATION_API_TASK",
            ) from exc
        if task is None:
            continue
        ver = task.get("version")
        if isinstance(ver, bool) or not isinstance(ver, int) or ver < 0:
            raise PresentationError(
                "api task version corrupt",
                code="E_TEAM_PRESENTATION_CORRUPT",
            )
        versions[api_id.strip()] = ver
    return versions


def _read_meta_generation(meta: Mapping[str, Any]) -> int:
    raw = meta.get("meta_generation", 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise PresentationError(
            f"meta_generation corrupt: {raw!r}",
            code="E_TEAM_PRESENTATION_CORRUPT",
        )
    return raw


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PresentationError(
            "route string field must be a string",
            code="E_TEAM_PRESENTATION_ROUTE",
        )
    text = value.strip()
    return text or None


def _assert_relative_safe(value: str, *, label: str) -> None:
    text = value.strip().replace("\\", "/")
    if not text or text.startswith("/") or text.startswith("~"):
        raise PresentationError(
            f"{label} must be a relative path",
            code="E_TEAM_PRESENTATION_PATH",
        )
    parts = Path(text).parts
    if ".." in parts:
        raise PresentationError(
            f"{label} must not contain ..",
            code="E_TEAM_PRESENTATION_PATH",
        )


def _bound_summary(text: str, *, limit: int = _SUMMARY_MAX) -> str:
    cleaned = " ".join(str(text).split())
    for key in _SECRET_KEYS:
        cleaned = re.sub(
            rf"(?i)\b{re.escape(key)}\b\s*[:=]\s*\S+",
            f"{key}=[REDACTED]",
            cleaned,
        )
    cleaned = _ABS_PATH_RE.sub(r"\1[PATH]", cleaned)
    if len(cleaned) > limit:
        return cleaned[: limit - 3] + "..."
    return cleaned


def _assert_no_secret_sentinels(payload: Mapping[str, Any]) -> None:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    lowered = blob.lower()
    for needle in (
        "owner_token",
        "claim_token",
        "idempotency_key",
        "/users/",
        "/home/",
        "/private/",
        "/var/folders/",
    ):
        if needle in lowered:
            raise PresentationError(
                f"presentation leaked sentinel {needle!r}",
                code="E_TEAM_PRESENTATION_LEAK",
            )


__all__ = [
    "MCP_PROJECTION_V1",
    "PRESENTATION_KIND",
    "PRESENTATION_SCHEMA_VERSION",
    "ROUTE_KIND_EXTERNAL",
    "ROUTE_KIND_NATIVE_RECEIPT",
    "ROUTE_KIND_UNKNOWN",
    "ROUTE_SCHEMA",
    "PresentationError",
    "build_external_route",
    "build_team_presentation_v1",
    "stamp_route_on_task",
    "unknown_route",
    "validate_route_descriptor",
]
