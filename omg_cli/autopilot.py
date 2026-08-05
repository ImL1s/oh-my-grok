# omg_cli/autopilot.py
"""Strict Autopilot v2 coordinator — legal phase transitions only.

Composes interview → ralplan → ultragoal/impl → review → ultraqa → acceptance.
Does not write verified except via same-process set_verified after acceptance.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from omg_cli.evidence import CLI_WRITER, assert_safe_supervised_parent, validate_identifier
from omg_cli.state import (
    RunSchema,
    _atomic_write_json,
    classify_run_schema,
    create_run,
    execution_lease,
    load_run,
    write_status,
)


# Edges ``transition()`` may take (machine-callable). ``verified`` is NOT here —
# it is commit-only via ``complete_with_acceptance`` (same-process acceptance).
MANUAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "init": frozenset({"interview", "ralplan"}),  # interview skip only if forced clear
    "interview": frozenset({"ralplan", "blocked"}),
    "ralplan": frozenset({"implement", "blocked"}),
    "implement": frozenset({"review", "blocked"}),
    "review": frozenset({"qa", "rework", "ralplan", "blocked"}),
    "rework": frozenset({"review", "blocked"}),
    "qa": frozenset({"acceptance", "ralplan", "rework", "blocked"}),
    "acceptance": frozenset({"blocked"}),
    "verified": frozenset(),
    "blocked": frozenset({"interview", "ralplan", "implement", "review", "qa"}),
    # "cancelled" is a terminal node reachable only via ``omg cancel`` /
    # ``cancel_run`` (status.json), never as a generic transition() edge —
    # kept here so LEGAL_TRANSITIONS / introspection still document it.
    "cancelled": frozenset(),
}

# Conceptual commit-only edges (status may advertise; ``transition()`` never takes them).
COMMIT_ONLY_TRANSITIONS: dict[str, frozenset[str]] = {
    "acceptance": frozenset({"verified"}),
}

# Full phase graph for docs / introspection (= manual ∪ commit-only).
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    phase: MANUAL_TRANSITIONS.get(phase, frozenset())
    | COMMIT_ONLY_TRANSITIONS.get(phase, frozenset())
    for phase in MANUAL_TRANSITIONS
}


logger = logging.getLogger(__name__)


class AutopilotError(ValueError):
    """Illegal transition or corrupt autopilot state."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_stage_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def stage_review_is_clean(root: Path | str, run_id: str) -> bool:
    """True only when CLI-stamped structured_review.json is clean for this run.

    Also rechecks the declared top-level ``diff_hash`` against the diff_hash
    actually approved by each nested lane stamp (``code_reviewer_stamp`` /
    ``architect_stamp``) via the same ``evaluate_lane`` comparison the review
    writer uses. A drift (e.g. on-disk tampering) between the two makes the
    stamp stale, not clean.
    """
    from omg_cli.review import evaluate_lane, review_state_path

    data = _read_stage_json(review_state_path(root, run_id))
    if not data:
        return False
    if data.get("writer") != CLI_WRITER:
        return False
    if data.get("run_id") != run_id:
        return False
    if data.get("invalidated") is True:
        return False
    if data.get("clean") is not True:
        return False
    # Bind to the current workspace fingerprint: run_structured_review has
    # written this on every stamp since schema_version 2, so its absence on
    # a schema_version>=2 stamp is itself stale/tampered, not legacy — fail
    # closed rather than trusting a clean flag with no workspace binding.
    workspace_fp = data.get("workspace_fp")
    if not workspace_fp:
        return False
    try:
        current_workspace_fp = _implement_workspace_fingerprint(root)
    except OSError:
        return False
    if current_workspace_fp != workspace_fp:
        return False
    diff_hash = data.get("diff_hash")
    cr_stamp = data.get("code_reviewer_stamp")
    ar_stamp = data.get("architect_stamp")
    if not diff_hash and cr_stamp is None and ar_stamp is None:
        # Legacy stamp without any hash/lane fields: keep prior clean-flag-only behavior.
        return True
    if not diff_hash or not isinstance(cr_stamp, dict) or not isinstance(ar_stamp, dict):
        # diff_hash present but lane stamps missing/malformed (or vice versa):
        # fail closed rather than falling back to legacy clean-flag-only trust.
        return False
    cr = evaluate_lane(
        role="code-reviewer", expected_diff_hash=diff_hash, proposal=None, stamped=cr_stamp
    )
    ar = evaluate_lane(
        role="architect", expected_diff_hash=diff_hash, proposal=None, stamped=ar_stamp
    )
    return cr.get("clean") is True and ar.get("clean") is True


def stage_qa_is_clean(root: Path | str, run_id: str) -> bool:
    """True only when CLI-stamped ultraqa.json is clean (never implies verified).

    Also rechecks the ``product_hash`` recorded on the last clean cycle
    against a fresh recompute of the current workspace product hash — a
    drift means the on-disk stamp is stale for the workspace it now claims
    to describe.
    """
    from omg_cli.qa import product_hash, qa_state_path

    data = _read_stage_json(qa_state_path(root, run_id))
    if not data:
        return False
    if data.get("writer") != CLI_WRITER:
        return False
    if data.get("run_id") != run_id:
        return False
    if data.get("invalidated") is True:
        return False
    if not (data.get("clean") is True and data.get("status") == "clean"):
        return False
    cycles = data.get("cycles") or []
    if cycles and isinstance(cycles[-1], dict):
        last_cycle = cycles[-1]
        recorded_hash = last_cycle.get("product_hash")
        if recorded_hash:
            try:
                current_hash = product_hash(root)
            except OSError:
                return False
            if current_hash != recorded_hash:
                return False
        # Broader implement-gate fingerprint snapshot (curated non-Python
        # product surfaces too) recorded at clean-cycle time — drift here
        # means product/config changed since QA went clean, even when
        # product_hash's narrower omg_cli/**/*.py view is unaffected.
        recorded_fp = last_cycle.get("implement_workspace_fp")
        if recorded_fp:
            try:
                current_fp = _implement_workspace_fingerprint(root)
            except OSError:
                return False
            if current_fp != recorded_fp:
                return False
    return True


# Curated product surfaces for the implement→review work gate. Deliberately
# broader than ``qa.product_hash`` (which only covers ``omg_cli/**/*.py`` and
# also backs UltraQA acceptance repair-cycle semantics — this helper must
# never change that hash's behavior, or ultraqa's "unchanged hash" repair
# check would silently break). ``omg_cli`` is hashed in full (not just
# ``*.py``) since it is small enough to be cheap (~200 files).
_IMPLEMENT_FINGERPRINT_ROOTS: tuple[str, ...] = (
    "omg_cli",
    "plugin.json",
    "hooks",
    "skills",
    "agents",
    "templates",
    "scripts",
    "bin",
)

# Generated/ignored directory names that must never affect the implement-gate
# fingerprint — mirrors this repo's own ``.gitignore`` cache entries
# (``__pycache__/``, ``.pytest_cache/``, ``.ruff_cache/``, ``.mypy_cache/``,
# ``*.egg-info/``). Without this, merely running tests or importing an
# ``omg_cli`` module during ``implement`` (which writes/updates ``.pyc``
# bytecode) would register as "product work" with zero source edits.
_IMPLEMENT_FINGERPRINT_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
)


def _is_fingerprint_excluded(rel_parts: tuple[str, ...]) -> bool:
    """True when a path (relative to root, as ``.parts``) is a generated
    cache artifact that must be excluded from the implement-gate fingerprint."""
    for part in rel_parts:
        if part in _IMPLEMENT_FINGERPRINT_EXCLUDED_DIRS:
            return True
        if part.endswith(".egg-info"):
            return True
    return bool(rel_parts) and rel_parts[-1].endswith(".pyc")


def _implement_workspace_fingerprint(root: Path | str) -> str:
    """Stable hash of curated product surfaces for the implement→review gate.

    A separate helper from ``qa.product_hash`` on purpose: implementation
    work confined to non-Python product surfaces (``plugin.json``, ``hooks/``,
    ``skills/``, ``agents/``, ``templates/``, ``scripts/``, ``bin/``, or non-``.py`` files under
    ``omg_cli/``) must still register as work here, without touching
    ``qa.product_hash``'s narrower semantics used for QA/acceptance. Generated
    caches (``__pycache__/``, ``.pytest_cache/``, ``.ruff_cache/``,
    ``.mypy_cache/``, ``*.egg-info/``, ``*.pyc``) are excluded — see
    ``_is_fingerprint_excluded`` — since they mutate from merely running
    code/tests, not from durable product edits.
    """
    root = Path(root).resolve()
    files: set[Path] = set()
    any_root_present = False
    for name in _IMPLEMENT_FINGERPRINT_ROOTS:
        target = root / name
        if not target.exists():
            continue
        any_root_present = True
        if target.is_file():
            files.add(target)
        elif target.is_dir():
            for fp in target.rglob("*"):
                if not fp.is_file():
                    continue
                if _is_fingerprint_excluded(fp.relative_to(root).parts):
                    continue
                files.add(fp)
    if not any_root_present:
        # Fixture/non-product root (e.g. unit-test tmp_path with none of the
        # curated roots present): fall back to hashing every file so
        # fingerprint drift is still detected, mirroring qa.product_hash's
        # own temp-fixture fallback.
        skip = {".omg", ".git"}
        for fp in root.rglob("*"):
            if fp.is_file() and not fp.is_symlink():
                rel_parts = fp.relative_to(root).parts
                if any(part in skip for part in rel_parts):
                    continue
                if _is_fingerprint_excluded(rel_parts):
                    continue
                files.add(fp)
    h = hashlib.sha256()
    for fp in sorted(files):
        if fp.is_symlink():
            continue
        try:
            rel = str(fp.relative_to(root))
        except ValueError:
            rel = str(fp)
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(fp.read_bytes())
        except OSError:
            continue
        h.update(b"\0")
    return h.hexdigest()


def _implementation_work_evidence(
    root: Path,
    run_id: str,
    state: Mapping[str, Any],
    ev: Mapping[str, Any],
    *,
    break_glass: bool,
) -> tuple[bool, str | None]:
    """True + optional gate_audit label when implement→review has proof of work."""
    stored_fp = state.get("implement_workspace_fp")
    current_fp = _implement_workspace_fingerprint(root)
    if stored_fp is not None and current_fp != stored_fp:
        return True, None
    from omg_cli.implementation import read_implementation_receipt

    on_disk_receipt = read_implementation_receipt(root, run_id)
    if isinstance(on_disk_receipt, dict):
        # Trusted CLI-side stamp (writer verified by the reader itself, not
        # caller-supplied evidence) — accepted without break_glass, but only
        # while it still describes the current workspace.
        if on_disk_receipt.get("content_sha256") == current_fp:
            return True, "cli_receipt:implementation.json"
    receipt = ev.get("implementation_receipt")
    if isinstance(receipt, dict) and receipt.get("writer") == CLI_WRITER:
        if break_glass:
            # Inline evidence is caller-supplied JSON, not a verified on-disk
            # CLI stamp — writer=="omg-cli" here is unauthenticated and
            # trivially forgeable, so it is audited the same as no_change.
            return True, "break_glass:implementation_receipt"
    if ev.get("no_change_reason") and break_glass:
        return True, "break_glass:no_change"
    return False, None


def invalidate_quality_stages(root: Path | str, run_id: str, *, reason: str) -> None:
    """Mark review/QA stage stamps stale after rework or replan (CLI write)."""
    from omg_cli.qa import qa_state_path
    from omg_cli.review import review_state_path

    root = Path(root).resolve()
    for path in (review_state_path(root, run_id), qa_state_path(root, run_id)):
        data = _read_stage_json(path)
        if not data:
            continue
        data["clean"] = False
        data["invalidated"] = True
        data["invalidated_reason"] = reason
        data["invalidated_at"] = _utc_now()
        data["writer"] = CLI_WRITER
        if "status" in data and data.get("status") == "clean":
            data["status"] = "invalidated"
        _atomic_write_json(path, data)


def _normalize_ralplan_epoch(
    state: Mapping[str, Any], root: Path | str, run_id: str
) -> int:
    """Return a trustworthy ``ralplan_epoch``, migrating pre-R9 state that
    predates the field (added in da0fc52) without blindly treating a
    missing value as ``0``.

    ``ralplan_epoch`` is the re-entry gate that decides whether the next
    ``next_phase == "ralplan"`` transition is the harmless first
    interview→ralplan handoff (epoch==0 → no-op) or a real replan that must
    invalidate stale review/QA/consensus stamps (epoch>=1). A run created
    before this field existed but that already advanced past interview —
    e.g. it already has an accepted CLI-owned ``ralplan.json`` stamp, or
    ``cycles.ralplan`` is already nonzero — must never be silently treated
    as epoch==0 on load; that would resurrect the no-op path and let a
    stale accepted stamp unlock implement again without invalidation. Only
    a run genuinely still at ``interview``, with no CLI-owned stamp and no
    ralplan cycles yet, may migrate to 0; every other missing-epoch run
    migrates to at least 1 so the very next ralplan entry is treated as a
    re-entry, not the first handoff.
    """
    raw = state.get("ralplan_epoch")
    if raw is not None:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise AutopilotError(
                f"corrupt autopilot state: ralplan_epoch must be int >= 0, "
                f"got {raw!r}"
            )
        return raw

    from omg_cli.ralplan import ralplan_state_path

    root = Path(root).resolve()
    cycles = state.get("cycles") or {}
    ralplan_cycles = int(cycles.get("ralplan") or 0)
    stamp = _read_stage_json(ralplan_state_path(root, run_id))
    has_cli_stamp = bool(
        stamp and stamp.get("writer") == CLI_WRITER and stamp.get("run_id") == run_id
    )
    if (
        str(state.get("phase") or "init") == "interview"
        and not has_cli_stamp
        and ralplan_cycles == 0
    ):
        return 0
    return max(1, ralplan_cycles or 1)


def autopilot_state_path(root: Path | str, run_id: str) -> Path:
    run_id = validate_identifier(run_id, label="run_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "stages"
        / "autopilot.json"
    )


def _save(root: Path, run_id: str, state: dict[str, Any], lease: Any) -> None:
    lease.assert_current()
    path = autopilot_state_path(root, run_id)
    state = dict(state)
    state["writer"] = CLI_WRITER
    state["updated_at"] = _utc_now()
    state["execution_generation"] = getattr(lease, "generation", None)
    state["execution_owner_invocation_id"] = getattr(lease, "invocation_id", None)
    _atomic_write_json(path, state)


def load_autopilot(root: Path | str, run_id: str) -> dict[str, Any]:
    path = autopilot_state_path(root, run_id)
    if not path.is_file():
        raise AutopilotError(f"autopilot state missing: {run_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("writer") != CLI_WRITER:
        raise AutopilotError("autopilot state lacks CLI writer")
    return data


def assert_legal_transition(src: str, dst: str) -> None:
    """Raise unless ``src → dst`` is a manual ``transition()`` edge.

    Commit-only destinations (``verified``) are rejected here — callers that
    need the conceptual graph should read ``LEGAL_TRANSITIONS`` /
    ``COMMIT_ONLY_TRANSITIONS``, not this assert.
    """
    if dst in COMMIT_ONLY_TRANSITIONS.get(src, frozenset()):
        raise AutopilotError(
            f"commit-only transition {src!r} -> {dst!r} "
            "(use complete_with_acceptance / omg autopilot complete)"
        )
    allowed = MANUAL_TRANSITIONS.get(src)
    if allowed is None:
        raise AutopilotError(f"unknown phase {src!r}")
    if dst not in allowed:
        raise AutopilotError(f"illegal transition {src!r} -> {dst!r}")


def start_autopilot(
    root: Path | str,
    goal: str,
    *,
    force: bool = False,
    skip_interview: bool = False,
) -> dict[str, Any]:
    """Create strict-v2 autopilot run at interview or ralplan phase."""
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    goal = (goal or "").strip()
    if not goal:
        raise AutopilotError("goal text required")
    run = create_run(
        root,
        mode="autopilot",
        goal=goal,
        force=force,
        extra={
            "schema_version": 2,
            "lifecycle_version": 2,
            "stage": "autopilot",
        },
    )
    run_id = run["run_id"]
    phase = "ralplan" if skip_interview else "interview"
    with execution_lease(root, run_id, intent="autopilot-start") as lease:
        state = {
            "writer": CLI_WRITER,
            "schema_version": 2,
            "lifecycle_version": 2,
            "run_id": run_id,
            "goal": goal,
            "phase": phase,
            # Monotonic re-entry counter for the ralplan phase: 0 means the
            # interview→ralplan handoff hasn't happened yet (skip_interview
            # starts already past it, at 1). Gates invalidation on
            # transition() re-entry, not on stamp *existence*.
            "ralplan_epoch": 1 if skip_interview else 0,
            "cycles": {"review": 0, "qa": 0, "ralplan": 0},
            "history": [{"phase": phase, "at": _utc_now(), "event": "start"}],
            "blocker": None,
            "verified": False,
            "created_at": _utc_now(),
        }
        _save(root, run_id, state, lease)
        write_status(
            root,
            run_id,
            "running",
            extra={
                "stage": "autopilot",
                "autopilot_phase": phase,
            },
            lease=lease,
        )
    return status_autopilot(root, run_id)


def transition(
    root: Path | str,
    run_id: str,
    next_phase: str,
    *,
    reason: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Advance phase when legal; requires execution lease."""
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    run_id = validate_identifier(run_id, label="run_id")
    run = load_run(root, run_id)
    if run is None:
        raise AutopilotError(f"run not found: {run_id}")
    try:
        schema = classify_run_schema(run)
    except (TypeError, ValueError) as exc:
        raise AutopilotError(f"refusing malformed/unknown schema: {exc}") from exc
    if schema is not RunSchema.STRICT_V2:
        raise AutopilotError(
            f"autopilot v2 requires strict-v2 run (got {schema})"
        )
    if run.get("mode") != "autopilot":
        raise AutopilotError(f"wrong mode: {run.get('mode')!r}")

    with execution_lease(root, run_id, intent=f"autopilot-{next_phase}") as lease:
        # Re-check terminal/cancellation freshly under the lease — the
        # ``run`` loaded above (pre-lease) can be stale if a concurrent
        # ``omg cancel`` commits status.json / the cancellation request
        # between that load and lease acquisition. Fail closed before any
        # state mutation or sidecar write: a run that has gone terminal
        # (``omg_cli.state.TERMINAL_STATUSES`` — cancelled/completed/
        # failed/verified) or has a pending cancellation request must
        # never accept a further phase transition.
        from omg_cli.state import TERMINAL_STATUSES, load_cancellation_request

        fresh_run = load_run(root, run_id) or {}
        fresh_status = str(fresh_run.get("status") or "")
        if fresh_status in TERMINAL_STATUSES:
            raise AutopilotError(
                "autopilot run is terminal under lease; refusing transition: "
                f"status={fresh_status!r}"
            )
        if load_cancellation_request(root, run_id) is not None:
            raise AutopilotError(
                "autopilot run has a pending cancellation request; "
                "refusing transition"
            )

        state = load_autopilot(root, run_id)
        src = str(state.get("phase") or "init")
        assert_legal_transition(src, next_phase)

        # Gate by DESTINATION phase (not only specific src) so blocked→qa
        # / blocked→implement cannot skip quality or consensus gates.
        # Interview/consensus: prefer CLI-owned stamps; bare booleans are
        # break-glass only (must set evidence.break_glass=true + audit).
        ev = dict(evidence or {})
        break_glass = ev.get("break_glass") is True
        gate_audit: str | None = None

        if next_phase == "ralplan":
            # First entry from interview needs a trusted gate; recovery from
            # later phases may re-enter ralplan with replan reason.
            if src == "interview":
                if _interview_complete(root, run_id):
                    pass
                elif ev.get("interview_complete") and break_glass:
                    gate_audit = "break_glass:interview_complete"
                else:
                    raise AutopilotError(
                        "no interview gate → no ralplan handoff "
                        "(need CLI interview status=complete, or "
                        "evidence.interview_complete + break_glass=true)"
                    )
        if next_phase == "implement":
            if _consensus_ready(root, run_id):
                pass
            elif ev.get("consensus") and break_glass:
                gate_audit = "break_glass:consensus"
            else:
                raise AutopilotError(
                    "no consensus → no implementation "
                    "(need CLI ralplan accepted / ralplan_consensus, or "
                    "evidence.consensus + break_glass=true)"
                )
        if next_phase == "qa":
            if not stage_review_is_clean(root, run_id):
                raise AutopilotError(
                    "no clean review → no QA "
                    "(requires CLI-stamped stages/structured_review.json "
                    "clean=true with a diff_hash fingerprint matching its "
                    "stamped lanes; stale/drifted diff_hash is rejected)"
                )
        if next_phase == "acceptance":
            if not stage_qa_is_clean(root, run_id):
                raise AutopilotError(
                    "no clean QA → no acceptance "
                    "(requires CLI-stamped stages/ultraqa.json status=clean "
                    "with a product_hash fingerprint matching the current "
                    "workspace; stale/drifted product_hash is rejected)"
                )
        if next_phase == "review" and src in {"implement", "blocked"}:
            # implement→review (and blocked→review recovering from a paused
            # implement cycle) must not silently pass with zero product
            # change: require a workspace fingerprint drift since entering
            # implement, a CLI-writer implementation_receipt, or an audited
            # break_glass no_change_reason. rework→review is intentionally
            # excluded — rework already forces a fresh review stamp via the
            # invalidation below without demanding new workspace evidence.
            has_work, work_audit = _implementation_work_evidence(
                root, run_id, state, ev, break_glass=break_glass
            )
            if not has_work:
                raise AutopilotError(
                    "no evidence of implementation work → no review "
                    "(need a workspace fingerprint change since entering "
                    "implement, evidence.implementation_receipt with "
                    "writer=omg-cli, or evidence.no_change_reason + "
                    "break_glass=true)"
                )
            if work_audit:
                gate_audit = work_audit
        if next_phase == "implement":
            # Any (re-)entry into implement produces new, unreviewed product
            # code. Prior clean review/QA stamps must never remain authoritative
            # for a later qa/acceptance gate — closes the
            # qa→blocked→implement→blocked→qa false-green round-trip.
            invalidate_quality_stages(
                root, run_id, reason=f"(re)implement from {src}"
            )
            # A receipt stamped during a prior implement cycle must not
            # unlock review for this new cycle just because the workspace
            # fingerprint happens to still match (e.g. review→ralplan→
            # implement with no new product changes) — closes the stale-
            # receipt round-trip. Bind receipts to the current cycle by
            # invalidating any leftover one on entry.
            from omg_cli.implementation import invalidate_implementation_receipt

            invalidate_implementation_receipt(
                root, run_id, reason=f"(re)implement from {src}"
            )
            state["implement_workspace_fp"] = _implement_workspace_fingerprint(root)
        if next_phase == "ralplan":
            # ralplan_epoch is a monotonic re-entry counter, not a stamp
            # *existence* check — gating on stamp existence (the old
            # ``_ralplan_stamp_exists`` rule) let a break-glass consensus
            # path that never wrote a ralplan.json stamp skip invalidation
            # entirely on replan. epoch==0 means the interview→ralplan
            # handoff hasn't happened yet (skip_interview starts at 1, past
            # it already) — that first handoff is a no-op. Every entry after
            # that (epoch>=1), regardless of ``src`` or stamp existence,
            # invalidates stale quality/consensus stamps before bumping.
            # A missing field (pre-R9 state) is migrated conservatively by
            # ``_normalize_ralplan_epoch`` rather than defaulting to 0 —
            # blindly treating "missing" as "0" would let a run that
            # already has an accepted stamp / prior replan cycle re-enter
            # the no-op branch and skip invalidation.
            epoch = _normalize_ralplan_epoch(state, root, run_id)
            if epoch == 0:
                state["ralplan_epoch"] = 1
            else:
                state["cycles"]["ralplan"] = (
                    int(state["cycles"].get("ralplan") or 0) + 1
                )
                # Stale clean stamps must not open QA/acceptance after replan
                invalidate_quality_stages(
                    root, run_id, reason=f"replan from {src}"
                )
                # A prior accepted ralplan.json stamp must not silently
                # unlock implement again for this new replan cycle — no-op
                # when no stamp exists yet (e.g. break-glass consensus path).
                from omg_cli.ralplan import invalidate_ralplan_consensus

                invalidate_ralplan_consensus(
                    root, run_id, reason=f"replan from {src}"
                )
                state["ralplan_epoch"] = epoch + 1
        if next_phase == "rework":
            state["cycles"]["review"] = int(state["cycles"].get("review") or 0) + 1
            invalidate_quality_stages(
                root, run_id, reason="rework invalidates review/qa stamps"
            )
        if next_phase == "review" and src in {"rework", "implement", "blocked"}:
            # Re-entering review (after leaving the linear implement→review
            # edge) requires a fresh structured_review stamp — includes
            # qa→blocked→review so a pre-block clean stamp cannot reopen qa.
            invalidate_quality_stages(
                root, run_id, reason=f"re-enter review from {src}"
            )
        if next_phase == "qa" and src == "review":
            pass
        if src == "qa" and next_phase == "ralplan":
            state["cycles"]["qa"] = int(state["cycles"].get("qa") or 0) + 1

        hist_entry: dict[str, Any] = {
            "from": src,
            "phase": next_phase,
            "reason": reason,
            "at": _utc_now(),
        }
        if gate_audit:
            hist_entry["gate_audit"] = gate_audit
        state["phase"] = next_phase
        state["history"] = list(state.get("history") or []) + [hist_entry]
        if next_phase == "blocked":
            state["blocker"] = {"reason": reason or "blocked", "from": src}
            status = "blocked"
        elif next_phase == "cancelled":
            status = "cancelled"
        else:
            state["blocker"] = None
            status = "running"
        _save(root, run_id, state, lease)
        write_status(
            root,
            run_id,
            status,
            extra={
                "stage": "autopilot",
                "autopilot_phase": next_phase,
                "blocker": state.get("blocker"),
            },
            lease=lease,
        )
    return status_autopilot(root, run_id)


def _sync_autopilot_verified(
    root: Path,
    run_id: str,
    *,
    lease: Any,
    event: str,
) -> dict[str, Any]:
    """Mark autopilot phase verified + align status.autopilot_phase (lease held).

    Does not re-commit verified status (use set_verified first when needed).
    """
    from omg_cli.state import merge_status_fields

    state = load_autopilot(root, run_id)
    state["phase"] = "verified"
    state["verified"] = True
    state["history"] = list(state.get("history") or []) + [
        {
            "phase": "verified",
            "at": _utc_now(),
            "event": event,
        }
    ]
    _save(root, run_id, state, lease)
    merge_status_fields(
        root,
        run_id,
        {
            "stage": "autopilot",
            "autopilot_phase": "verified",
            "blocker": None,
        },
        lease=lease,
    )
    return status_autopilot(root, run_id)


def complete_with_acceptance(
    root: Path | str,
    run_id: str,
    *,
    prd: Mapping[str, Any] | None = None,
    allow_soft_accept: bool = False,
) -> dict[str, Any]:
    """Terminal path: freeze+run acceptance in this process, then set_verified.

    Acceptance runs under the execution lease owner (no transition guard during
    freeze/run). ``set_verified`` then linearizes the terminal status. Disk-only
    stamps from other processes cannot promote.

    Short-circuit: if the run is already ``verified`` (e.g. prior ``omg accept``)
    and autopilot is in ``acceptance`` or ``verified``, sync phase without
    re-running freeze_and_run.
    """
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    run_id = validate_identifier(run_id, label="run_id")
    from omg_cli.acceptance import (
        freeze_and_run,
        is_trusted_acceptance,
        materialize_prd_from_ultraqa,
    )
    from omg_cli.state import set_verified

    pre = load_autopilot(root, run_id)
    phase = str(pre.get("phase") or "")
    run_pre = load_run(root, run_id) or {}
    already_verified = run_pre.get("verified") is True or run_pre.get("status") == "verified"

    # Terminal short-circuit: already verified (idempotent complete).
    if phase == "verified" and already_verified:
        return status_autopilot(root, run_id)

    if phase not in ("acceptance", "verified"):
        raise AutopilotError(
            f"acceptance only from acceptance phase (got {phase!r})"
        )

    with execution_lease(root, run_id, intent="autopilot-accept") as lease:
        state = load_autopilot(root, run_id)
        phase2 = str(state.get("phase") or "")
        run_now = load_run(root, run_id) or {}
        already = run_now.get("verified") is True or run_now.get("status") == "verified"

        if phase2 == "verified" and already:
            return status_autopilot(root, run_id)

        if already and phase2 in ("acceptance", "verified"):
            # omg accept already verified; do not re-run freeze_and_run.
            return _sync_autopilot_verified(
                root,
                run_id,
                lease=lease,
                event="short_circuit_already_verified",
            )

        if phase2 != "acceptance":
            raise AutopilotError(
                f"acceptance only from acceptance phase (got {phase2!r})"
            )

        prd_obj: dict[str, Any] | None = dict(prd) if prd is not None else None
        if prd_obj is None:
            prd_path = (
                Path(root)
                / ".omg"
                / "state"
                / "runs"
                / run_id
                / "prd.json"
            )
            if prd_path.is_file():
                try:
                    loaded = json.loads(prd_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        prd_obj = loaded
                except (OSError, json.JSONDecodeError) as exc:
                    raise AutopilotError(f"prd.json unreadable: {exc}") from exc
        if prd_obj is None:
            # Auto-build from clean UltraQA scenarios when present.
            try:
                prd_obj = materialize_prd_from_ultraqa(
                    root,
                    run_id,
                    goal=str(state.get("goal") or "") or None,
                    overwrite=False,
                )
            except ValueError as exc:
                raise AutopilotError(
                    "complete_with_acceptance requires prd.json or prd= "
                    f"(or clean ultraqa to materialize): {exc}"
                ) from exc

        # Same-process freeze + run (registers process-local acceptance token)
        try:
            passed = freeze_and_run(
                root,
                run_id,
                prd_obj,
                allow_soft_accept=allow_soft_accept,
            )
        except Exception as exc:
            raise AutopilotError(
                f"same-process freeze_and_run failed: {exc}"
            ) from exc
        if not passed:
            raise AutopilotError(
                "verified requires same-process acceptance pass "
                "(freeze_and_run returned false)"
            )
        if not is_trusted_acceptance(root, run_id):
            raise AutopilotError(
                "verified requires same-process acceptance pass "
                "(disk/cross-process stamps cannot promote)"
            )

        try:
            set_verified(root, run_id, force=False, lease=lease)
        except PermissionError as exc:
            raise AutopilotError(
                "set_verified refused; re-run freeze/run acceptance in this process"
            ) from exc
        run = load_run(root, run_id)
        if not run or not (
            run.get("verified") is True or run.get("status") == "verified"
        ):
            raise AutopilotError(
                "set_verified refused; re-run freeze/run acceptance in this process"
            )
        return _sync_autopilot_verified(
            root,
            run_id,
            lease=lease,
            event="same_process_acceptance",
        )


def set_awaiting_confirmation(
    root: Path | str,
    run_id: str,
    value: bool,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Mirror flag into status via merge_status_fields. Never touch verified."""
    from omg_cli.state import merge_status_fields

    root = Path(root).resolve()
    run_id = validate_identifier(run_id, label="run_id")
    run = load_run(root, run_id)
    if run is None:
        raise AutopilotError(f"run not found: {run_id}")
    try:
        schema = classify_run_schema(run)
    except (TypeError, ValueError) as exc:
        raise AutopilotError(f"refusing malformed/unknown schema: {exc}") from exc
    if schema is not RunSchema.STRICT_V2:
        raise AutopilotError(
            f"autopilot v2 requires strict-v2 run (got {schema})"
        )
    if run.get("mode") != "autopilot":
        raise AutopilotError(f"wrong mode: {run.get('mode')!r}")

    awaiting = bool(value)
    merge_status_fields(
        root,
        run_id,
        {
            "autopilot_awaiting": awaiting,
            # Clearing always wipes reason so --clear --reason cannot leave a stale note.
            "autopilot_awaiting_reason": (reason or "") if awaiting else "",
        },
    )
    return status_autopilot(root, run_id)


def status_autopilot(root: Path | str, run_id: str) -> dict[str, Any]:
    from omg_cli.state import TERMINAL_STATUSES

    state = load_autopilot(root, run_id)
    run = load_run(root, run_id) or {}
    phase = str(state.get("phase") or "")
    run_status = str(run.get("status") or "")
    # A terminal run (cancelled/completed/failed/verified) has no legal next
    # phase — the sidecar's ``phase`` field alone must never advertise a
    # transition that ``transition()`` will now refuse under lease.
    if run_status in TERMINAL_STATUSES:
        legal_next: list[str] = []
        commit_only_next: list[str] = []
    else:
        legal_next = sorted(MANUAL_TRANSITIONS.get(phase, frozenset()))
        commit_only_next = sorted(COMMIT_ONLY_TRANSITIONS.get(phase, frozenset()))
    out: dict[str, Any] = {
        "run_id": run_id,
        "phase": state.get("phase"),
        "goal": state.get("goal"),
        "cycles": state.get("cycles"),
        "blocker": state.get("blocker"),
        "verified": bool(run.get("verified") or state.get("verified")),
        "run_status": run.get("status"),
        # Only edges ``transition()`` can take — never advertise commit-only
        # ``verified`` as a manual next phase (use terminal_action instead).
        "legal_next": legal_next,
    }
    if commit_only_next:
        out["commit_only_next"] = commit_only_next
        out["terminal_action"] = "omg autopilot complete"
    gate = run.get("autopilot_gate_failure")
    if isinstance(gate, dict) and gate:
        out["gate_failure"] = gate
    return out


# Phase → skill body for outer-driver prompt injection
_PHASE_SKILL_REL: dict[str, str] = {
    "interview": "skills/omg-deep-interview/SKILL.md",
    "ralplan": "skills/omg-ralplan/SKILL.md",
    "implement": "skills/omg-ultrawork/SKILL.md",
    "rework": "skills/omg-ultrawork/SKILL.md",
    "review": "skills/omg-dual-review/SKILL.md",
    "qa": "skills/omg-ultraqa/SKILL.md",
    "acceptance": "skills/omg-autopilot/SKILL.md",
    "blocked": "skills/omg-autopilot/SKILL.md",
}


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_phase_skill(phase: str, *, root: Path | None = None) -> str:
    rel = _PHASE_SKILL_REL.get(phase) or _PHASE_SKILL_REL["acceptance"]
    base = root if root is not None else _plugin_root()
    path = base / rel
    if not path.is_file():
        path = _plugin_root() / rel
    if not path.is_file():
        return f"(skill missing for phase {phase!r}: {rel})"
    return path.read_text(encoding="utf-8")


def autopilot_context_pack(
    *,
    run_id: str,
    phase: str,
    goal: str,
    next_gate: str,
) -> str:
    """Build autopilot phase context block for prompt injection."""
    lines = [
        "## Autopilot context pack (CLI injection — fresh each phase)",
        f"- run_id: {run_id}",
        f"- phase={phase}",
        f"- goal: {(goal or '').strip() or '(unspecified)'}",
        f"- next_gate: {next_gate}",
        "- Do **not** ask the user mid-phase; record uncertainty under "
        "`.omg/artifacts/` or `omg autopilot transition --phase blocked`.",
        "- Only the omg CLI sets verified; use `omg autopilot complete` at acceptance.",
    ]
    return "\n".join(lines)


def build_phase_prompt(
    phase: str,
    *,
    root: Path | str,
    goal: str,
    run_id: str,
) -> str:
    """Compose grok prompt for one autopilot phase (skill + pack + no-ask rule)."""
    from omg_cli.modes import HARD_RULES_REMINDER
    from omg_cli.stop_gate import continuation_reason

    root_path = Path(root).resolve()
    phase_key = (phase or "").strip() or "unknown"
    reason = continuation_reason(phase_key, goal=goal, run_id=run_id)
    # Extract next gate clause from continuation_reason for the pack header.
    next_gate = "see continuation block"
    if "Next gate:" in reason:
        next_gate = reason.split("Next gate:", 1)[1].split(".", 1)[0].strip()

    skill = _load_phase_skill(phase_key, root=root_path)
    parts = [
        skill,
        "",
        HARD_RULES_REMINDER,
        "",
        autopilot_context_pack(
            run_id=run_id,
            phase=phase_key,
            goal=goal,
            next_gate=next_gate,
        ),
        "",
        "## Phase continuation (hard)",
        reason,
        "",
        "## Goal",
        (goal or "").strip() or "(no goal provided)",
        "",
        "Follow the phase skill above. Do not ask the user mid-phase.",
    ]
    if phase_key == "ralplan":
        parts.extend(
            [
                "",
                "## Autopilot-bound ralplan (hard — overrides Launch via CLI)",
                f"- Stay on this autopilot run_id=`{run_id}`. Do **not** start a "
                "standalone `omg ralplan` that creates a new active run / wrong "
                "active pointer.",
                f"- Run: `omg ralplan \"…\" --run {run_id}` — the CLI owns "
                "consensus on this run (`ralplan.json` / `ralplan_consensus`).",
                "- Do **not** edit `.omg/state/` yourself (including forging "
                "`accepted: true` in `ralplan.json` or `ralplan_consensus`).",
                "- Proposal drafts may go under `.omg/artifacts/` but do **not** "
                "unlock implement by themselves.",
                "- `_consensus_ready` only inspects **this** run_id — a sibling "
                "ralplan run will never unblock implement.",
            ]
        )
    return "\n".join(parts)


def _interview_complete(root: Path, run_id: str) -> bool:
    """True only when a CLI-owned interview.json for *this* run_id records a
    genuine complete envelope — not a bare ``status`` string, which is
    trivially forgeable by writing an untrusted ``interview.json``.

    Mirrors ``interview.py``'s own envelope validation for status=="complete"
    (CLI writer, run_id binding, and a matching CLI-stamped interview-spec
    artifact whose content hash/writer/run_id all check out) without
    requiring the *autopilot* run's own mode to be ``interview`` — autopilot
    embeds interview state under its own run_id rather than reusing a
    separate ``omg interview start`` run.
    """
    from omg_cli.interview import interview_spec_path, interview_state_path

    root_path = Path(root).resolve()
    data = _read_stage_json(interview_state_path(root_path, run_id))
    if not data:
        return False
    if data.get("writer") != CLI_WRITER:
        return False
    if data.get("run_id") != run_id:
        return False
    if data.get("status") != "complete":
        return False
    spec_rel = data.get("spec_path")
    if not spec_rel:
        return False
    expected_path = interview_spec_path(root_path, run_id)
    if str(spec_rel) != str(expected_path.relative_to(root_path)):
        return False
    artifact = _read_stage_json(expected_path)
    if not artifact:
        return False
    content = artifact.get("content")
    stamp = artifact.get("stamp")
    if not isinstance(content, dict) or not isinstance(stamp, dict):
        return False
    if stamp.get("writer") != CLI_WRITER or not stamp.get("invocation_id"):
        return False
    canonical = (
        json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    from omg_cli.evidence import sha256_bytes

    if sha256_bytes(canonical) != stamp.get("content_sha256"):
        return False
    if content.get("run_id") != run_id or stamp.get("run_id") != run_id:
        return False
    return True


def _consensus_ready(root: Path, run_id: str) -> bool:
    """True only when the CLI-owned ``ralplan.json`` stamp for *this*
    run_id records genuine accepted consensus.

    Workers may write proposal artifacts under ``.omg/artifacts/``; those do
    **not** unlock implement. ``status.ralplan_consensus`` alone is also
    insufficient — it is a plain status.json field that can go stale (e.g.
    a prior accepted round's flag surviving a later replan) without a
    matching on-disk stamp; the ``ralplan.json`` stamp is the source of
    truth and must show ``writer``, ``run_id``, and ``accepted`` together.

    Defense in depth (R5-4/R7-3): the stamp must carry a non-empty string
    ``goal`` matching this run's frozen goal — closes a foreign/mistargeted
    stamp even when writer/run_id/accepted otherwise look valid. A missing,
    null, or empty ``goal`` is fail-closed (rejected), not treated as a
    legacy/unaffected shape.

    Defense in depth (R10-1/P2-R8-D): the stamp must also carry
    ``schema_version == 2`` and ``lifecycle_version == 2`` — autopilot only
    ever embeds strict-v2 RALPLAN (see ``_run_ralplan_v2``), so a legacy-v1
    or otherwise malformed/missing-schema stamp is fail-closed (rejected),
    not treated as a legacy/unaffected shape.
    """
    from omg_cli.ralplan import ralplan_state_path

    data = _read_stage_json(ralplan_state_path(root, run_id))
    if not data:
        return False
    if data.get("writer") != CLI_WRITER:
        return False
    if data.get("run_id") != run_id:
        return False
    if data.get("schema_version") != 2:
        return False
    if data.get("lifecycle_version") != 2:
        return False
    if data.get("invalidated") is True:
        return False
    if data.get("accepted") is not True:
        return False
    goal = data.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return False
    run = load_run(root, run_id)
    frozen_goal = str((run or {}).get("goal") or "").strip()
    if goal.strip() != frozen_goal:
        return False
    return True


def _record_gate_failure(
    root: Path,
    run_id: str,
    phase: str,
    message: str,
) -> None:
    """Persist last advance-gate failure for status / stall payloads (best-effort)."""
    from omg_cli.state import merge_status_fields

    try:
        merge_status_fields(
            root,
            run_id,
            {
                "autopilot_gate_failure": {
                    "phase": phase,
                    "message": message,
                    "at": _utc_now(),
                }
            },
        )
    except Exception:
        # Never block the outer driver on status merge failures.
        logger.warning(
            "autopilot: failed to record gate_failure for run_id=%s phase=%s",
            run_id,
            phase,
            exc_info=True,
        )
        return


def _clear_gate_failure(root: Path, run_id: str) -> None:
    from omg_cli.state import merge_status_fields

    try:
        merge_status_fields(root, run_id, {"autopilot_gate_failure": {}})
    except Exception:
        logger.debug(
            "autopilot: failed to clear gate_failure for run_id=%s", run_id, exc_info=True
        )
        return


def _try_advance_after_launch(root: Path, run_id: str, phase: str) -> str:
    """Inspect stamps after a grok launch; transition when gates are satisfied."""
    phase = str(phase)
    try:
        if phase == "interview" and _interview_complete(root, run_id):
            transition(
                root,
                run_id,
                "ralplan",
                reason="interview complete",
            )
            _clear_gate_failure(root, run_id)
            return "ralplan"
        if phase == "ralplan" and _consensus_ready(root, run_id):
            transition(
                root,
                run_id,
                "implement",
                reason="ralplan consensus",
            )
            _clear_gate_failure(root, run_id)
            return "implement"
        if phase == "implement":
            # Recheck live phase: launch/side-effects may have moved to
            # blocked (or elsewhere). Never advance from a stale phase arg.
            current = load_run(root, run_id) or {}
            live = str(current.get("autopilot_phase") or "")
            if live and live != "implement":
                return live
            transition(
                root,
                run_id,
                "review",
                reason="implementation ready for review",
            )
            _clear_gate_failure(root, run_id)
            return "review"
        if phase == "review" and stage_review_is_clean(root, run_id):
            transition(root, run_id, "qa", reason="structured review clean")
            _clear_gate_failure(root, run_id)
            return "qa"
        if phase == "qa" and stage_qa_is_clean(root, run_id):
            transition(
                root,
                run_id,
                "acceptance",
                reason="ultraqa clean",
            )
            _clear_gate_failure(root, run_id)
            return "acceptance"
        if phase == "acceptance":
            out = complete_with_acceptance(root, run_id)
            if out.get("phase") == "verified":
                _clear_gate_failure(root, run_id)
                return "verified"
            msg = (
                "acceptance complete did not reach verified "
                f"(phase={out.get('phase')!r})"
            )
            _record_gate_failure(root, run_id, phase, msg)
            return phase
    except AutopilotError as exc:
        _record_gate_failure(root, run_id, phase, str(exc))
        return phase
    return phase


def run_autopilot(
    root: Path | str,
    goal: str,
    *,
    skip_interview: bool = False,
    resume_run_id: str | None = None,
    max_phase_cycles: int = 5,
    dry_run: bool = False,
    timeout: float | None = None,
    yolo: bool = False,
    safe: bool = False,
    force: bool = False,
    unattended: bool = False,
    max_stall_relaunches: int = 32,
    **launch_kw: Any,
) -> int:
    """Outer CLI driver: launch grok per phase until verified or pause/terminal.

    Tertiary cross-turn persistence (beyond in-session Stop pin). Writes RESUME.md
    each phase; pauses at incomplete interview with resume hint.

    With ``unattended=True`` (#40), host-turn stalls (no phase advance after a
    successful grok launch) re-launch in-process instead of returning a human
    ``go`` prompt — until terminal, await/interview pause, launch failure, or
    ``max_stall_relaunches`` exhausted. Does **not** claim infinite Stop pin.
    """
    import json
    import sys

    from omg_cli.modes import (
        _launch_grok,
        _run_dir,
        build_grok_argv,
        resolve_launch_timeout,
    )
    from omg_cli.resume import write_resume_md
    from omg_cli.state import load_active_run

    root_path = Path(root).resolve()
    assert_safe_supervised_parent()
    requested_goal = (goal or "").strip()
    run_id: str

    if resume_run_id is not None:
        if resume_run_id == "__active__":
            run = load_active_run(root_path)
            if run is None:
                print("omg autopilot run: no active run to resume", file=sys.stderr)
                return 1
            resume_run_id = str(run["run_id"])
        else:
            run = load_run(root_path, str(resume_run_id))
        if run is None:
            print(
                f"omg autopilot run: no run found: {resume_run_id!r}",
                file=sys.stderr,
            )
            return 1
        if str(run.get("mode") or "") != "autopilot":
            print(
                f"omg autopilot run: run {resume_run_id!r} is mode="
                f"{run.get('mode')!r}",
                file=sys.stderr,
            )
            return 1
        from omg_cli.state import TERMINAL_STATUSES

        run_status = str(run.get("status") or "")
        if run_status in TERMINAL_STATUSES:
            # status.json is authoritative over the autopilot sidecar's
            # ``phase`` field — a terminal run (e.g. cancelled) must never
            # launch grok again just because the sidecar still parks at a
            # non-terminal phase.
            print(
                f"omg autopilot run: run {resume_run_id!r} is terminal "
                f"(status={run_status!r}); refusing to resume",
                file=sys.stderr,
            )
            return 1
        run_id = str(run["run_id"])
        frozen_goal = str(run.get("goal") or "").strip()
        if requested_goal and requested_goal != frozen_goal:
            print(
                "omg autopilot run: conflicting goal on resume; omit goal text",
                file=sys.stderr,
            )
            return 2
        goal = frozen_goal or requested_goal
    else:
        if not requested_goal:
            print("omg autopilot run: goal text required", file=sys.stderr)
            return 2
        st = start_autopilot(
            root_path,
            requested_goal,
            force=force,
            skip_interview=skip_interview,
        )
        run_id = str(st["run_id"])
        goal = requested_goal

    launch_timeout = resolve_launch_timeout(timeout, dry_run=dry_run)
    run_dir = _run_dir(root_path, run_id)
    phase_cycles: dict[str, int] = {}
    stall_relaunches = 0
    max_stall = max(1, int(max_stall_relaunches))
    resume_cmd = (
        f"omg autopilot run --resume {run_id}"
        + (" --unattended" if unattended else "")
    )

    while True:
        st = status_autopilot(root_path, run_id)
        phase = str(st.get("phase") or "")
        run_row = load_run(root_path, run_id) or {}
        if run_row.get("autopilot_awaiting"):
            write_resume_md(root_path, run_id)
            clear_cmd = f"omg autopilot await --clear --run {run_id}"
            reason = run_row.get("autopilot_awaiting_reason")
            if isinstance(reason, str) and reason.strip():
                print(f"{clear_cmd}  # reason: {reason.strip()}", file=sys.stderr)
            else:
                print(clear_cmd, file=sys.stderr)
            print(resume_cmd, file=sys.stderr)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "pause": "awaiting",
                        "run_id": run_id,
                        "resume_command": resume_cmd,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if phase == "verified":
            write_resume_md(root_path, run_id)
            print(
                json.dumps(
                    {"ok": True, "phase": "verified", "run_id": run_id},
                    ensure_ascii=False,
                )
            )
            return 0
        if phase in ("blocked", "cancelled"):
            write_resume_md(root_path, run_id)
            print(
                json.dumps(
                    {"ok": False, "phase": phase, "run_id": run_id},
                    ensure_ascii=False,
                )
            )
            return 1
        if phase == "interview" and not _interview_complete(root_path, run_id):
            write_resume_md(root_path, run_id)
            print(resume_cmd, file=sys.stderr)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "pause": "interview",
                        "run_id": run_id,
                        "resume_command": resume_cmd,
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        # Preflight: completed interview must advance before any launch so
        # resume does not spawn an unnecessary interview Grok session.
        if phase == "interview" and _interview_complete(root_path, run_id):
            new_phase = _try_advance_after_launch(root_path, run_id, phase)
            if new_phase != phase:
                continue
            write_resume_md(root_path, run_id)
            print(resume_cmd, file=sys.stderr)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "pause": "stall",
                        "phase": phase,
                        "run_id": run_id,
                        "resume_command": resume_cmd,
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        phase_cycles[phase] = int(phase_cycles.get(phase, 0)) + 1
        if phase_cycles[phase] > max(1, int(max_phase_cycles)):
            try:
                transition(
                    root_path,
                    run_id,
                    "blocked",
                    reason=f"max_phase_cycles={max_phase_cycles}",
                )
            except AutopilotError:
                pass
            write_resume_md(root_path, run_id)
            return 1

        write_resume_md(root_path, run_id)

        prompt = build_phase_prompt(phase, root=root_path, goal=goal, run_id=run_id)
        argv = build_grok_argv(
            "ralplan",
            goal,
            yolo=yolo,
            safe=safe,
            cwd=root_path,
            project_root=root_path,
            run_id=run_id,
            prompt=prompt,
            skill_root=_plugin_root(),
            **{
                k: v
                for k, v in launch_kw.items()
                if k
                in (
                    "extra",
                    "output_format",
                    "disallow_shell",
                    "new_session_id",
                    "resume_session_id",
                )
            },
        )
        rc = _launch_grok(
            argv,
            cwd=root_path,
            run_dir=run_dir,
            timeout=launch_timeout,
            dry_run=dry_run,
        )
        if rc != 0:
            write_resume_md(root_path, run_id)
            return int(rc)

        new_phase = _try_advance_after_launch(root_path, run_id, phase)
        if dry_run:
            return 0
        if new_phase == phase:
            # No gate progress this launch — host turn likely ended (Stop cap)
            # or a destination gate failed (see gate_failure on status).
            run_now = load_run(root_path, run_id) or {}
            gate = run_now.get("autopilot_gate_failure")
            gate_payload = gate if isinstance(gate, dict) and gate else None
            if unattended:
                stall_relaunches += 1
                if stall_relaunches > max_stall:
                    try:
                        transition(
                            root_path,
                            run_id,
                            "blocked",
                            reason=f"max_stall_relaunches={max_stall}",
                        )
                    except AutopilotError:
                        pass
                    write_resume_md(root_path, run_id)
                    stall_body: dict[str, Any] = {
                        "ok": False,
                        "phase": phase,
                        "run_id": run_id,
                        "error": "max_stall_relaunches",
                        "resume_command": resume_cmd,
                    }
                    if gate_payload:
                        stall_body["gate_failure"] = gate_payload
                    print(json.dumps(stall_body, ensure_ascii=False))
                    return 1
                # Re-launch same phase without requiring human "go" (#40).
                continue
            write_resume_md(root_path, run_id)
            print(resume_cmd, file=sys.stderr)
            stall_ok: dict[str, Any] = {
                "ok": True,
                "pause": "stall",
                "phase": phase,
                "run_id": run_id,
                "resume_command": resume_cmd,
            }
            if gate_payload:
                stall_ok["gate_failure"] = gate_payload
            print(json.dumps(stall_ok, ensure_ascii=False))
            return 0
        stall_relaunches = 0


__all__ = [
    "COMMIT_ONLY_TRANSITIONS",
    "LEGAL_TRANSITIONS",
    "MANUAL_TRANSITIONS",
    "AutopilotError",
    "assert_legal_transition",
    "autopilot_context_pack",
    "autopilot_state_path",
    "build_phase_prompt",
    "complete_with_acceptance",
    "invalidate_quality_stages",
    "load_autopilot",
    "run_autopilot",
    "set_awaiting_confirmation",
    "stage_qa_is_clean",
    "stage_review_is_clean",
    "start_autopilot",
    "status_autopilot",
    "transition",
]
