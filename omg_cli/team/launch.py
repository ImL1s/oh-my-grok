"""Unified Team worker launch: pane vs durable job (#69 PR4).

One execution abstraction:

    Team Task → Worker Launch Request → tmux pane | durable job

Task lifecycle stays on Team; process lifecycle for job-backed workers is
owned by the #68 Jobs plane. Team persists only durable execution references
(``topology`` + exactly one of ``job_id`` / ``pane_id`` +
``launch_generation``) — never PID/PGID/subprocess handles.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from omg_cli.jobs.models import JobState, JobStoreError
from omg_cli.jobs.runtime import cancel_job, job_status, start_job
from omg_cli.jobs.store import read_job_record

WORKER_TOPOLOGY_PANE = "pane"
WORKER_TOPOLOGY_JOB = "job"
WORKER_TOPOLOGIES: frozenset[str] = frozenset(
    {WORKER_TOPOLOGY_PANE, WORKER_TOPOLOGY_JOB}
)

EXECUTION_SCHEMA = 1

# Jobs-plane providers Team may launch for worker-topology=job.
JOB_ADMITTED_PROVIDERS: frozenset[str] = frozenset({"fake", "antigravity", "grok"})

# Task / worker status vocabulary for the launch abstraction (team.json).
STATUS_READY = "ready"
STATUS_CLAIMED = "claimed"
STATUS_WORKER_LAUNCHED = "worker_launched"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_UNPROVEN = "unproven"


class WorkerLaunchError(ValueError):
    """Fail-closed worker launch / execution-binding error."""

    def __init__(self, message: str, *, code: str = "E_TEAM_WORKER_LAUNCH") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class WorkerExecutionHandle:
    """Versioned execution descriptor returned by :func:`launch_worker`.

    Exactly one of ``job_id`` / ``pane_id`` may be set (never both, never
    neither for a live handle). Dry-run descriptors omit both ids.
    """

    topology: str
    worker_id: str
    provider: str
    launch_generation: int
    job_id: str | None = None
    pane_id: str | None = None
    attempt: int = 1
    run_id: str | None = None
    team_id: str | None = None
    task_id: str | None = None
    dry_run: bool = False

    def __post_init__(self) -> None:
        topo = normalize_worker_topology(self.topology)
        object.__setattr__(self, "topology", topo)
        if self.dry_run:
            if self.job_id is not None or self.pane_id is not None:
                raise WorkerLaunchError(
                    "dry-run execution handle must not carry job_id or pane_id",
                    code="E_TEAM_EXEC_DRY_RUN",
                )
            return
        _assert_xor_ids(self.topology, self.job_id, self.pane_id)

    def to_execution_record(self) -> dict[str, Any]:
        """Durable Team-state fragment (no PID / claim token / Jobs metadata)."""
        row: dict[str, Any] = {
            "schema": EXECUTION_SCHEMA,
            "topology": self.topology,
            "launch_generation": int(self.launch_generation),
        }
        if self.job_id is not None:
            row["job_id"] = self.job_id
        if self.pane_id is not None:
            row["pane_id"] = self.pane_id
        return row

    def to_status_view(self) -> dict[str, Any]:
        """Operator-facing worker status slice."""
        view: dict[str, Any] = {"topology": self.topology}
        if self.topology == WORKER_TOPOLOGY_JOB and self.job_id is not None:
            view["job_id"] = self.job_id
        if self.topology == WORKER_TOPOLOGY_PANE and self.pane_id is not None:
            view["pane_id"] = self.pane_id
        return view

    def to_dict(self) -> dict[str, Any]:
        return {
            "topology": self.topology,
            "worker_id": self.worker_id,
            "provider": self.provider,
            "launch_generation": int(self.launch_generation),
            "job_id": self.job_id,
            "pane_id": self.pane_id,
            "attempt": int(self.attempt),
            "run_id": self.run_id,
            "team_id": self.team_id,
            "task_id": self.task_id,
            "dry_run": bool(self.dry_run),
        }


def normalize_worker_topology(value: Any) -> str:
    """Normalize ``pane`` / ``job``; fail closed on anything else."""
    if not isinstance(value, str) or not value.strip():
        raise WorkerLaunchError(
            f"worker topology required (pane|job); got {value!r}",
            code="E_TEAM_TOPOLOGY",
        )
    topo = value.strip().lower()
    if topo not in WORKER_TOPOLOGIES:
        raise WorkerLaunchError(
            f"unsupported worker topology {value!r} (supported: pane|job)",
            code="E_TEAM_TOPOLOGY",
        )
    return topo


def _assert_xor_ids(
    topology: str, job_id: str | None, pane_id: str | None
) -> None:
    has_job = job_id is not None
    has_pane = pane_id is not None
    if has_job and has_pane:
        raise WorkerLaunchError(
            "execution handle must not carry both job_id and pane_id",
            code="E_TEAM_EXEC_XOR",
        )
    if topology == WORKER_TOPOLOGY_JOB:
        if not has_job or has_pane:
            raise WorkerLaunchError(
                "job topology requires job_id and forbids pane_id",
                code="E_TEAM_EXEC_XOR",
            )
        if not isinstance(job_id, str) or not job_id.strip():
            raise WorkerLaunchError(
                "job_id must be a non-empty string",
                code="E_TEAM_EXEC_JOB_ID",
            )
    elif topology == WORKER_TOPOLOGY_PANE:
        if not has_pane or has_job:
            raise WorkerLaunchError(
                "pane topology requires pane_id and forbids job_id",
                code="E_TEAM_EXEC_XOR",
            )
        if not isinstance(pane_id, str) or not pane_id.strip():
            raise WorkerLaunchError(
                "pane_id must be a non-empty string",
                code="E_TEAM_EXEC_PANE_ID",
            )


def validate_execution_record(raw: Any) -> dict[str, Any]:
    """Fail-closed validation of a persisted ``execution`` object."""
    if not isinstance(raw, Mapping):
        raise WorkerLaunchError(
            "execution record must be an object",
            code="E_TEAM_EXEC_SHAPE",
        )
    data = dict(raw)
    topology = normalize_worker_topology(data.get("topology"))
    job_id = data.get("job_id")
    pane_id = data.get("pane_id")
    if job_id is not None and not isinstance(job_id, str):
        raise WorkerLaunchError(
            "execution.job_id must be a string when present",
            code="E_TEAM_EXEC_JOB_ID",
        )
    if pane_id is not None and not isinstance(pane_id, str):
        raise WorkerLaunchError(
            "execution.pane_id must be a string when present",
            code="E_TEAM_EXEC_PANE_ID",
        )
    # Dry-run / pre-launch materialization may omit both ids.
    if job_id is None and pane_id is None:
        gen = data.get("launch_generation", 0)
        if not isinstance(gen, int) or isinstance(gen, bool) or gen < 0:
            raise WorkerLaunchError(
                "execution.launch_generation must be a non-negative int",
                code="E_TEAM_EXEC_GENERATION",
            )
        out: dict[str, Any] = {
            "schema": int(data.get("schema") or EXECUTION_SCHEMA),
            "topology": topology,
            "launch_generation": int(gen),
        }
        return out
    _assert_xor_ids(topology, job_id, pane_id)
    gen = data.get("launch_generation")
    if not isinstance(gen, int) or isinstance(gen, bool) or gen < 1:
        raise WorkerLaunchError(
            "execution.launch_generation must be a positive int",
            code="E_TEAM_EXEC_GENERATION",
        )
    out = {
        "schema": int(data.get("schema") or EXECUTION_SCHEMA),
        "topology": topology,
        "launch_generation": int(gen),
    }
    if job_id is not None:
        out["job_id"] = job_id
    if pane_id is not None:
        out["pane_id"] = pane_id
    return out


def resolve_job_provider(provider: str, *, executor: str | None = None) -> str:
    """Map Team provider/executor labels onto Jobs-admitted providers."""
    if (executor or "").strip().lower() == "fixture":
        return "fake"
    raw = (provider or "").strip().lower()
    if raw in ("fixture",):
        return "fake"
    if raw in ("agy", "antigravity"):
        return "antigravity"
    if raw in JOB_ADMITTED_PROVIDERS:
        return raw
    raise WorkerLaunchError(
        "worker-topology=job requires a jobs-admitted provider "
        f"(fake|antigravity|grok); got {provider!r}",
        code="E_TEAM_JOB_PROVIDER",
    )


PaneLauncher = Callable[..., str]


def launch_worker(
    root: Path | str,
    *,
    worker_id: str,
    topology: str,
    provider: str,
    role: str = "executor",
    run_id: str | None = None,
    team_id: str | None = None,
    task_id: str | None = None,
    attempt: int = 1,
    launch_generation: int = 1,
    prompt_text: str | None = None,
    prompt_file: Path | str | None = None,
    pane_id: str | None = None,
    pane_launcher: PaneLauncher | None = None,
    dry_run: bool = False,
    executor: str | None = None,
    sleep_s: float | None = None,
    job_request_stamps: Mapping[str, Any] | None = None,
) -> WorkerExecutionHandle:
    """Launch one worker as a pane or durable job; return an execution handle.

    Dry-run returns a descriptor without creating a pane, subprocess, or job.
    Live job topology requires the Jobs record to exist after ``start_job``
    (missing metadata → fail closed, never fabricate Team state).
    """
    root_path = Path(root).resolve()
    topo = normalize_worker_topology(topology)
    wid = (worker_id or "").strip()
    if not wid:
        raise WorkerLaunchError("worker_id is required", code="E_TEAM_WORKER_ID")
    if not isinstance(launch_generation, int) or isinstance(launch_generation, bool):
        raise WorkerLaunchError(
            "launch_generation must be an int",
            code="E_TEAM_EXEC_GENERATION",
        )
    if launch_generation < 1 and not dry_run:
        raise WorkerLaunchError(
            "launch_generation must be >= 1 for live launch",
            code="E_TEAM_EXEC_GENERATION",
        )
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise WorkerLaunchError("attempt must be a positive int", code="E_TEAM_ATTEMPT")

    if dry_run:
        # Materialize launch descriptor only — no pane / job / subprocess.
        resolved_provider = provider
        if topo == WORKER_TOPOLOGY_JOB:
            try:
                resolved_provider = resolve_job_provider(provider, executor=executor)
            except WorkerLaunchError:
                # Dry-run still records the requested provider label when the
                # live path would refuse; callers may preview then fix routing.
                resolved_provider = (provider or "").strip() or "unknown"
        return WorkerExecutionHandle(
            topology=topo,
            worker_id=wid,
            provider=resolved_provider,
            launch_generation=max(int(launch_generation), 0),
            job_id=None,
            pane_id=None,
            attempt=int(attempt),
            run_id=run_id,
            team_id=team_id,
            task_id=task_id or wid,
            dry_run=True,
        )

    if topo == WORKER_TOPOLOGY_PANE:
        resolved_pane = pane_id
        if resolved_pane is None:
            if pane_launcher is None:
                raise WorkerLaunchError(
                    "pane topology requires pane_id or pane_launcher",
                    code="E_TEAM_PANE_LAUNCH",
                )
            resolved_pane = pane_launcher(
                root=root_path,
                worker_id=wid,
                run_id=run_id,
                team_id=team_id,
                task_id=task_id or wid,
                provider=provider,
                role=role,
                attempt=attempt,
                launch_generation=launch_generation,
            )
        if not isinstance(resolved_pane, str) or not resolved_pane.strip():
            raise WorkerLaunchError(
                "pane launch produced no pane_id",
                code="E_TEAM_PANE_LAUNCH",
            )
        return WorkerExecutionHandle(
            topology=WORKER_TOPOLOGY_PANE,
            worker_id=wid,
            provider=(provider or "").strip() or "grok",
            launch_generation=int(launch_generation),
            job_id=None,
            pane_id=resolved_pane.strip(),
            attempt=int(attempt),
            run_id=run_id,
            team_id=team_id,
            task_id=task_id or wid,
            dry_run=False,
        )

    # --- job topology ---
    job_provider = resolve_job_provider(provider, executor=executor)
    if prompt_text is None and prompt_file is None:
        prompt_text = (
            f"Team worker {wid}"
            + (f" task={task_id}" if task_id else "")
            + (f" run={run_id}" if run_id else "")
        )
    stamps: dict[str, Any] = {}
    if job_request_stamps:
        for key, val in job_request_stamps.items():
            if not isinstance(key, str) or not key.startswith("team_"):
                continue
            if isinstance(val, bool) or not isinstance(val, (str, int)):
                continue
            if isinstance(val, str) and not val.strip():
                continue
            stamps[key] = val.strip() if isinstance(val, str) else int(val)
    # Always stamp attempt/generation/worker for replacement orphan adoption.
    stamps.setdefault("team_worker_id", wid)
    stamps.setdefault("team_task_id", str(task_id or wid))
    stamps.setdefault("team_attempt", int(attempt))
    stamps.setdefault("team_launch_generation", int(launch_generation))

    try:
        started = start_job(
            root_path,
            provider=job_provider,
            role=(role or "executor").strip() or "executor",
            prompt_text=prompt_text,
            prompt_file=prompt_file,
            run_id=run_id,
            sleep_s=sleep_s,
            launch=True,
            team_id=team_id,
            request_overrides=stamps or None,
        )
    except JobStoreError as exc:
        raise WorkerLaunchError(
            f"job creation failed: {exc}",
            code=getattr(exc, "code", None) or "E_TEAM_JOB_CREATE",
        ) from exc

    record = started.record
    job_id = str(record.job_id)
    # Fail closed: missing job record after start → never fabricate Team state.
    try:
        verified = read_job_record(root_path, job_id)
    except JobStoreError as exc:
        raise WorkerLaunchError(
            f"missing job metadata after start for {job_id}: {exc}",
            code="E_TEAM_JOB_MISSING",
        ) from exc
    if verified is None or str(verified.job_id) != job_id:
        raise WorkerLaunchError(
            f"missing job metadata after start for {job_id}",
            code="E_TEAM_JOB_MISSING",
        )

    return WorkerExecutionHandle(
        topology=WORKER_TOPOLOGY_JOB,
        worker_id=wid,
        provider=job_provider,
        launch_generation=int(launch_generation),
        job_id=job_id,
        pane_id=None,
        attempt=int(attempt),
        run_id=run_id,
        team_id=team_id,
        task_id=task_id or wid,
        dry_run=False,
    )


def stamp_execution_on_task(
    task: MutableMapping[str, Any],
    handle: WorkerExecutionHandle,
) -> dict[str, Any]:
    """Attach a validated execution record; refuse topology drift in place.

    Fail closed on corrupt prior handles (dual ``job_id``+``pane_id``, invalid
    shape): never heal by overwrite.
    """
    existing = task.get("execution")
    if isinstance(existing, Mapping) and existing:
        # Dual-id refuse before validate (validate also rejects; keep explicit).
        if existing.get("job_id") is not None and existing.get("pane_id") is not None:
            raise WorkerLaunchError(
                "duplicate execution handle (both job_id and pane_id)",
                code="E_TEAM_EXEC_XOR",
            )
        try:
            prev = validate_execution_record(existing)
        except WorkerLaunchError as exc:
            raise WorkerLaunchError(
                f"refusing stamp over invalid prior execution: {exc}",
                code=getattr(exc, "code", None) or "E_TEAM_EXEC_SHAPE",
            ) from exc
        prev_topo = prev.get("topology")
        if prev_topo and prev_topo != handle.topology:
            raise WorkerLaunchError(
                "refusing in-place topology mutation "
                f"({prev_topo} → {handle.topology}); launch a new generation",
                code="E_TEAM_TOPOLOGY_DRIFT",
            )
        if (
            not handle.dry_run
            and prev.get("job_id")
            and handle.job_id
            and prev.get("job_id") != handle.job_id
            and int(prev.get("launch_generation") or 0)
            == int(handle.launch_generation)
        ):
            raise WorkerLaunchError(
                "duplicate execution handle for same launch_generation",
                code="E_TEAM_EXEC_DUP",
            )
    record = handle.to_execution_record()
    validate_execution_record(record)
    task["execution"] = record
    task["worker_topology"] = handle.topology
    if handle.job_id is not None:
        task["job_id"] = handle.job_id
    if handle.pane_id is not None and not task.get("pane_id"):
        task["pane_id"] = handle.pane_id
    return dict(task)


def worker_status_view(task: Mapping[str, Any]) -> dict[str, Any]:
    """Build the status ``worker:`` slice from a team.json task row.

    Includes fail-closed I/O capability projection (#147). Topology / pane
    presence never implies operator interactivity; missing I/O fields normalize
    to unproven/unsupported.
    """
    from omg_cli.team.io_capability import normalize_worker_io_capability

    execution = task.get("execution")
    if isinstance(execution, Mapping) and execution.get("topology"):
        try:
            rec = validate_execution_record(execution)
        except WorkerLaunchError:
            rec = {"topology": str(execution.get("topology"))}
        view: dict[str, Any] = {"topology": rec.get("topology")}
        if rec.get("topology") == WORKER_TOPOLOGY_JOB and rec.get("job_id"):
            view["job_id"] = rec["job_id"]
        if rec.get("topology") == WORKER_TOPOLOGY_PANE and rec.get("pane_id"):
            view["pane_id"] = rec["pane_id"]
        elif rec.get("topology") == WORKER_TOPOLOGY_PANE and task.get("pane_id"):
            view["pane_id"] = task["pane_id"]
    elif task.get("pane_id"):
        # Legacy pane tasks without an execution stamp.
        view = {"topology": WORKER_TOPOLOGY_PANE, "pane_id": task["pane_id"]}
    elif task.get("job_id"):
        view = {"topology": WORKER_TOPOLOGY_JOB, "job_id": task["job_id"]}
    else:
        topo = task.get("worker_topology") or WORKER_TOPOLOGY_PANE
        try:
            topo_n = normalize_worker_topology(topo)
        except WorkerLaunchError:
            topo_n = WORKER_TOPOLOGY_PANE
        view = {"topology": topo_n}
    # Additive I/O block (not part of status_locked_view task keys).
    view["io"] = normalize_worker_io_capability(task).as_public_dict()
    return view


@dataclass(frozen=True, slots=True)
class CompletionDecision:
    accepted: bool
    reason: str
    task_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "task_status": self.task_status,
        }


def apply_job_completion(
    task: Mapping[str, Any],
    *,
    job_id: str,
    job_attempt: int,
    job_state: str,
    claim_token: str | None,
    expected_claim_token: str | None,
    expected_attempt: int,
    expected_worker_id: str | None,
    worker_id: str | None,
    team_id: str | None = None,
    foreign_team_id: str | None = None,
    expected_launch_generation: int | None = None,
) -> CompletionDecision:
    """Promote a Jobs terminal state onto a Team task — fail closed.

    Rejects missing claim tokens, claim-token mismatches, stale attempts,
    stale launch generations, foreign workers/teams, and unknown/non-terminal
    job states (never synthesize success). Both ``claim_token`` and
    ``expected_claim_token`` must be non-empty and equal — ``None``/``None``
    is not a soft success.
    """
    if foreign_team_id is not None and team_id is not None:
        if foreign_team_id != team_id:
            return CompletionDecision(False, "foreign_team")
    if expected_worker_id is not None and worker_id is not None:
        if expected_worker_id != worker_id:
            return CompletionDecision(False, "foreign_worker")

    execution = task.get("execution")
    if not isinstance(execution, Mapping):
        return CompletionDecision(False, "missing_execution")
    try:
        rec = validate_execution_record(execution)
    except WorkerLaunchError:
        return CompletionDecision(False, "invalid_execution")
    if rec.get("topology") != WORKER_TOPOLOGY_JOB:
        return CompletionDecision(False, "not_job_topology")
    if rec.get("job_id") != job_id:
        return CompletionDecision(False, "job_id_mismatch")

    # Fail closed: claim tokens are required for any terminal promotion.
    if (
        not isinstance(expected_claim_token, str)
        or not expected_claim_token.strip()
        or not isinstance(claim_token, str)
        or not claim_token.strip()
    ):
        return CompletionDecision(False, "claim_token_required")
    if claim_token != expected_claim_token:
        return CompletionDecision(False, "claim_token_mismatch")
    if int(job_attempt) != int(expected_attempt):
        return CompletionDecision(False, "stale_attempt")
    if expected_launch_generation is not None:
        if int(rec.get("launch_generation") or 0) != int(expected_launch_generation):
            return CompletionDecision(False, "stale_launch_generation")
    # Binding attempt (post-replacement) must match when present.
    binding = task.get("binding")
    if isinstance(binding, Mapping) and binding.get("attempt") is not None:
        try:
            bound_attempt = int(binding["attempt"])
        except (TypeError, ValueError):
            return CompletionDecision(False, "invalid_binding_attempt")
        if bound_attempt != int(expected_attempt):
            return CompletionDecision(False, "stale_attempt")

    state = (job_state or "").strip().lower()
    if state == JobState.SUCCEEDED.value:
        return CompletionDecision(True, "succeeded", STATUS_SUCCEEDED)
    if state == JobState.FAILED.value:
        return CompletionDecision(True, "failed", STATUS_FAILED)
    if state == JobState.CANCELLED.value:
        return CompletionDecision(True, "cancelled", STATUS_CANCELLED)
    if state == JobState.LOST.value:
        return CompletionDecision(True, "unproven", STATUS_UNPROVEN)
    return CompletionDecision(False, "nonterminal_or_unknown")


def observe_job_for_task(
    root: Path | str,
    task: Mapping[str, Any],
) -> dict[str, Any]:
    """Observe Jobs metadata for a job-backed task; never synthesize success."""
    execution = task.get("execution")
    if not isinstance(execution, Mapping):
        return {"health": STATUS_UNPROVEN, "reason": "missing_execution"}
    try:
        rec = validate_execution_record(execution)
    except WorkerLaunchError as exc:
        return {"health": STATUS_UNPROVEN, "reason": str(exc)}
    if rec.get("topology") != WORKER_TOPOLOGY_JOB:
        return {"health": "n/a", "reason": "not_job_topology"}
    job_id = rec.get("job_id")
    if not job_id:
        return {"health": STATUS_UNPROVEN, "reason": "missing_job_id"}
    try:
        record = job_status(Path(root).resolve(), str(job_id))
    except JobStoreError:
        return {
            "health": STATUS_UNPROVEN,
            "reason": "unknown_job",
            "job_id": job_id,
        }
    return {
        "health": record.state.value
        if isinstance(record.state, JobState)
        else str(record.state),
        "job_id": record.job_id,
        "attempt": record.attempt,
        "state": (
            record.state.value
            if isinstance(record.state, JobState)
            else str(record.state)
        ),
    }


def cancel_job_backed_worker(
    root: Path | str,
    task: Mapping[str, Any],
    *,
    reason: str = "team_cancel",
) -> dict[str, Any]:
    """Cancel via Jobs plane; Team never signals OS processes directly."""
    execution = task.get("execution")
    if not isinstance(execution, Mapping):
        return {"ok": False, "reason": "missing_execution"}
    try:
        rec = validate_execution_record(execution)
    except WorkerLaunchError as exc:
        return {"ok": False, "reason": str(exc)}
    if rec.get("topology") != WORKER_TOPOLOGY_JOB or not rec.get("job_id"):
        return {"ok": False, "reason": "not_job_backed"}
    job_id = str(rec["job_id"])
    try:
        result = cancel_job(Path(root).resolve(), job_id)
    except JobStoreError as exc:
        return {
            "ok": False,
            "reason": str(exc),
            "job_id": job_id,
            "code": getattr(exc, "code", None),
        }
    state = getattr(result, "state", None)
    state_s = state.value if isinstance(state, JobState) else str(state or "")
    return {
        "ok": True,
        "job_id": job_id,
        "state": state_s,
        "reason": reason,
        "task_status": STATUS_CANCELLED,
    }


def resume_bind_job_workers(
    root: Path | str,
    tasks: Sequence[Mapping[str, Any]],
    *,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Leader restart: bind existing Jobs without relaunching.

    Reconstructs worker state from Team execution refs + Jobs metadata.
    Unknown jobs → UNPROVEN (never synthesize success). Never launches.
    """
    root_path = Path(root).resolve()
    bound: list[dict[str, Any]] = []
    unproven: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for raw in tasks:
        if not isinstance(raw, Mapping):
            continue
        tid = str(raw.get("task_id") or raw.get("worker_id") or "")
        execution = raw.get("execution")
        if not isinstance(execution, Mapping):
            skipped.append({"task_id": tid, "reason": "no_execution"})
            continue
        try:
            rec = validate_execution_record(execution)
        except WorkerLaunchError as exc:
            unproven.append({"task_id": tid, "reason": str(exc)})
            continue
        if rec.get("topology") != WORKER_TOPOLOGY_JOB:
            skipped.append({"task_id": tid, "reason": "not_job_topology"})
            continue
        job_id = rec.get("job_id")
        if not job_id:
            # Dry-run / pre-launch descriptor — nothing to bind.
            skipped.append({"task_id": tid, "reason": "no_job_id"})
            continue
        try:
            record = read_job_record(root_path, str(job_id))
        except JobStoreError:
            record = None
        if record is None:
            unproven.append(
                {
                    "task_id": tid,
                    "job_id": job_id,
                    "reason": "unknown_job",
                    "health": STATUS_UNPROVEN,
                }
            )
            continue
        # Isolation: when binder supplies team_id, Jobs request.team_id must
        # match (missing ownership stamp → foreign / unproven, fail closed).
        if team_id is not None:
            req = getattr(record, "request", None) or {}
            owned = (
                req.get("team_id") if isinstance(req, Mapping) else None
            )
            if owned is None or str(owned) != str(team_id):
                unproven.append(
                    {
                        "task_id": tid,
                        "job_id": job_id,
                        "reason": "foreign_team_job",
                        "health": STATUS_UNPROVEN,
                    }
                )
                continue
        bound.append(
            {
                "task_id": tid,
                "job_id": record.job_id,
                "attempt": record.attempt,
                "state": (
                    record.state.value
                    if isinstance(record.state, JobState)
                    else str(record.state)
                ),
                "launch_generation": rec.get("launch_generation"),
                "relaunched": False,
            }
        )
    return {
        "bound": bound,
        "unproven": unproven,
        "skipped": skipped,
        "relaunched": [],
    }


def launch_descriptors_for_tasks(
    root: Path | str,
    tasks: Sequence[Mapping[str, Any]],
    *,
    topology: str,
    run_id: str,
    team_id: str,
    dry_run: bool,
    executor: str | None = None,
    launch_generation: int = 1,
    pane_ids: Mapping[str, str] | None = None,
) -> list[WorkerExecutionHandle]:
    """Launch (or dry-run describe) workers for a start_team task set."""
    topo = normalize_worker_topology(topology)
    handles: list[WorkerExecutionHandle] = []
    for raw in tasks:
        if not isinstance(raw, Mapping):
            continue
        tid = str(raw.get("task_id") or "")
        if not tid:
            continue
        provider = str(raw.get("provider") or "grok")
        role = str(raw.get("role") or "executor")
        pane = None
        if pane_ids and tid in pane_ids:
            pane = pane_ids[tid]
        elif isinstance(raw.get("pane_id"), str):
            pane = str(raw["pane_id"])
        handle = launch_worker(
            root,
            worker_id=tid,
            topology=topo,
            provider=provider,
            role=role,
            run_id=run_id,
            team_id=team_id,
            task_id=tid,
            attempt=int(raw.get("attempt") or 1),
            launch_generation=launch_generation,
            pane_id=pane if topo == WORKER_TOPOLOGY_PANE else None,
            dry_run=dry_run,
            executor=executor,
            prompt_text=(
                None
                if dry_run
                else f"Team worker {tid} run={run_id} team={team_id}"
            ),
        )
        handles.append(handle)
    return handles


def claim_launch_or_release(
    *,
    launch: Callable[[], WorkerExecutionHandle],
    release_claim: Callable[[], None],
) -> WorkerExecutionHandle:
    """CLAIMED → launch; on failure release claim back to READY."""
    try:
        return launch()
    except Exception:
        release_claim()
        raise


__all__ = [
    "CompletionDecision",
    "EXECUTION_SCHEMA",
    "JOB_ADMITTED_PROVIDERS",
    "STATUS_CANCELLED",
    "STATUS_CLAIMED",
    "STATUS_FAILED",
    "STATUS_READY",
    "STATUS_RUNNING",
    "STATUS_SUCCEEDED",
    "STATUS_UNPROVEN",
    "STATUS_WORKER_LAUNCHED",
    "WORKER_TOPOLOGIES",
    "WORKER_TOPOLOGY_JOB",
    "WORKER_TOPOLOGY_PANE",
    "WorkerExecutionHandle",
    "WorkerLaunchError",
    "apply_job_completion",
    "cancel_job_backed_worker",
    "claim_launch_or_release",
    "launch_descriptors_for_tasks",
    "launch_worker",
    "normalize_worker_topology",
    "observe_job_for_task",
    "resolve_job_provider",
    "resume_bind_job_workers",
    "stamp_execution_on_task",
    "validate_execution_record",
    "worker_status_view",
]
