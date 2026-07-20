# omg_cli/review.py
"""Identity/hash-bound structured code-reviewer + architect gate.

Legacy ``dual_review.py`` remains the host-launch adapter. This module owns
the pure gate: clean only when both lanes approve the current diff hash via
CLI-stamped proposals (never forged disk writer fields alone).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from omg_cli.evidence import CLI_WRITER, sha256_bytes, validate_identifier


class ReviewError(ValueError):
    """Invalid review gate input or state."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def review_state_path(root: Path | str, run_id: str) -> Path:
    run_id = validate_identifier(run_id, label="run_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "stages"
        / "structured_review.json"
    )


def compute_diff_hash(diff_text: str) -> str:
    return sha256_bytes((diff_text or "").encode("utf-8"))


def _stamp_proposal(
    root: Path,
    run_id: str,
    *,
    role: str,
    payload: Mapping[str, Any],
    diff_hash: str,
) -> dict[str, Any]:
    """Write a proposal under proposals root and return CLI stamp record."""
    inv = uuid.uuid4().hex
    pdir = (
        Path(root).resolve()
        / ".omg"
        / "artifacts"
        / "proposals"
        / run_id
        / inv
    )
    pdir.mkdir(parents=True, exist_ok=True)
    body = {
        "role": role,
        "diff_hash": diff_hash,
        "payload": dict(payload),
        "proposed_at": _utc_now(),
    }
    raw = _canonical(body)
    prop_path = pdir / f"{role}.json"
    prop_path.write_bytes(raw)
    stamp = {
        "writer": CLI_WRITER,
        "schema_version": 2,
        "run_id": run_id,
        "role": role,
        "diff_hash": diff_hash,
        "proposal_path": str(prop_path.relative_to(Path(root).resolve())),
        "proposal_sha256": sha256_bytes(raw),
        "payload": dict(payload),
        "stamped_at": _utc_now(),
        "invocation_id": inv,
    }
    return stamp


def evaluate_lane(
    *,
    role: str,
    expected_diff_hash: str,
    proposal: Mapping[str, Any] | None,
    stamped: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Fail-closed evaluation of one review lane."""
    if stamped is None or not isinstance(stamped, Mapping):
        return {
            "role": role,
            "clean": False,
            "reason": "missing_cli_stamp",
            "verdict": None,
        }
    if stamped.get("writer") != CLI_WRITER:
        return {
            "role": role,
            "clean": False,
            "reason": "forged_or_untrusted_writer",
            "verdict": stamped.get("verdict") or stamped.get("payload", {}).get("verdict"),
        }
    if stamped.get("role") != role:
        return {
            "role": role,
            "clean": False,
            "reason": "wrong_role",
            "verdict": None,
        }
    if stamped.get("invalidated") is True:
        return {
            "role": role,
            "clean": False,
            "reason": "invalidated_stamp",
            "verdict": None,
        }
    if stamped.get("diff_hash") != expected_diff_hash:
        return {
            "role": role,
            "clean": False,
            "reason": "stale_or_wrong_diff_hash",
            "verdict": stamped.get("payload", {}).get("verdict")
            if isinstance(stamped.get("payload"), dict)
            else None,
        }
    payload = stamped.get("payload") if isinstance(stamped.get("payload"), dict) else {}
    # Reject bare proposal file used as stamp
    if proposal is not None and proposal.get("writer") == CLI_WRITER:
        return {
            "role": role,
            "clean": False,
            "reason": "proposal_cannot_self_stamp",
            "verdict": None,
        }
    verdict = str(payload.get("verdict") or "").strip().upper()
    if role == "code-reviewer":
        ok = verdict == "APPROVE"
    elif role == "architect":
        ok = verdict == "CLEAR"
    else:
        ok = False
    findings = payload.get("findings") or []
    if not ok:
        return {
            "role": role,
            "clean": False,
            "reason": "non_clean_verdict",
            "verdict": verdict or None,
            "findings": findings,
        }
    # major findings block even if mislabeled APPROVE
    if any(
        isinstance(f, dict) and str(f.get("severity", "")).lower() in {"blocker", "major"}
        for f in findings
    ):
        return {
            "role": role,
            "clean": False,
            "reason": "major_or_blocker_finding",
            "verdict": verdict,
            "findings": findings,
        }
    return {
        "role": role,
        "clean": True,
        "reason": None,
        "verdict": verdict,
        "findings": findings,
    }


def classify_rework(
    code_reviewer: Mapping[str, Any],
    architect: Mapping[str, Any],
) -> str:
    """Return rework | replan | clean | blocked."""
    if code_reviewer.get("clean") and architect.get("clean"):
        return "clean"
    findings = list(code_reviewer.get("findings") or []) + list(
        architect.get("findings") or []
    )
    for f in findings:
        if not isinstance(f, dict):
            continue
        kind = str(f.get("kind") or f.get("category") or "").lower()
        if kind in {"requirement", "architecture", "plan", "spec"}:
            return "replan"
    if code_reviewer.get("reason") in {
        "missing_cli_stamp",
        "forged_or_untrusted_writer",
        "wrong_role",
        "stale_or_wrong_diff_hash",
        "proposal_cannot_self_stamp",
    } or architect.get("reason") in {
        "missing_cli_stamp",
        "forged_or_untrusted_writer",
        "wrong_role",
        "stale_or_wrong_diff_hash",
        "proposal_cannot_self_stamp",
    }:
        return "blocked"
    return "rework"


def run_structured_review(
    root: Path | str,
    run_id: str,
    *,
    diff_text: str,
    code_reviewer_payload: Mapping[str, Any] | None = None,
    architect_payload: Mapping[str, Any] | None = None,
    # Optional pre-built stamps for tests / adapters
    code_reviewer_stamp: Mapping[str, Any] | None = None,
    architect_stamp: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """CLI-owned structured review gate bound to current diff hash."""
    root = Path(root).resolve()
    run_id = validate_identifier(run_id, label="run_id")
    diff_hash = compute_diff_hash(diff_text)

    if code_reviewer_stamp is None:
        if code_reviewer_payload is None:
            raise ReviewError("code_reviewer payload or stamp required")
        code_reviewer_stamp = _stamp_proposal(
            root,
            run_id,
            role="code-reviewer",
            payload=code_reviewer_payload,
            diff_hash=diff_hash,
        )
    if architect_stamp is None:
        if architect_payload is None:
            raise ReviewError("architect payload or stamp required")
        architect_stamp = _stamp_proposal(
            root,
            run_id,
            role="architect",
            payload=architect_payload,
            diff_hash=diff_hash,
        )

    cr = evaluate_lane(
        role="code-reviewer",
        expected_diff_hash=diff_hash,
        proposal=None,
        stamped=code_reviewer_stamp,
    )
    ar = evaluate_lane(
        role="architect",
        expected_diff_hash=diff_hash,
        proposal=None,
        stamped=architect_stamp,
    )
    disposition = classify_rework(cr, ar)
    clean = disposition == "clean"
    state = {
        "writer": CLI_WRITER,
        "schema_version": 2,
        "run_id": run_id,
        "diff_hash": diff_hash,
        "clean": clean,
        "disposition": disposition,
        "code_reviewer": cr,
        "architect": ar,
        "code_reviewer_stamp": dict(code_reviewer_stamp),
        "architect_stamp": dict(architect_stamp),
        "updated_at": _utc_now(),
    }
    path = review_state_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return state


__all__ = [
    "ReviewError",
    "classify_rework",
    "compute_diff_hash",
    "evaluate_lane",
    "review_state_path",
    "run_structured_review",
]
