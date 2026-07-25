"""Deterministic goal → task board for ``omg team N[:role] "<goal>"``.

Does not invent parallel work when the goal is atomic: diagnosis /
implementation / verification lanes get dependency edges so they stay ordered.
"""

from __future__ import annotations

import re
from typing import Any

_NUMBERED_RE = re.compile(
    r"^\s*(?:\(?\d+\)?[.)]|[-*•])\s+(.+)$",
    re.MULTILINE,
)
_CLAUSE_SPLIT_RE = re.compile(
    r"\s+(?:and then|then|;|\band\b)\s+",
    re.IGNORECASE,
)


def _lane_owned_path(index: int) -> str:
    return f".omg/team-lanes/w{index}/"


def decompose_goal(
    goal: str,
    *,
    workers: int,
    role: str = "executor",
) -> list[dict[str, Any]]:
    """Return a tasks-json-compatible list of length ``workers``.

    Priority:
    1. Explicit numbered / bulleted items (capped / padded to ``workers``)
    2. Conjunction clauses when count matches workers
    3. Atomic fallback lanes with dependencies (diagnose → implement → verify)
    """
    text = (goal or "").strip()
    if not text:
        raise ValueError("goal must be a non-empty string")
    if workers < 1:
        raise ValueError("workers must be >= 1")

    items = [m.group(1).strip() for m in _NUMBERED_RE.finditer(text) if m.group(1).strip()]
    if items:
        return _pad_or_trim(items, workers=workers, role=role, goal=text)

    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(text) if c.strip()]
    # Only treat as parallel clauses when we get a clean match to N and N>1.
    if len(clauses) == workers and workers > 1 and all(len(c) > 3 for c in clauses):
        return [
            _task(i, subject=clauses[i], role=role, depends_on=[])
            for i in range(workers)
        ]

    return _atomic_lanes(text, workers=workers, role=role)


def _pad_or_trim(
    items: list[str],
    *,
    workers: int,
    role: str,
    goal: str,
) -> list[dict[str, Any]]:
    subjects = list(items[:workers])
    while len(subjects) < workers:
        subjects.append(f"support lane {len(subjects) + 1} for: {goal}")
    return [
        _task(i, subject=subjects[i], role=role, depends_on=[])
        for i in range(workers)
    ]


def _atomic_lanes(
    goal: str,
    *,
    workers: int,
    role: str,
) -> list[dict[str, Any]]:
    """Ordered diagnosis / implementation / verification style lanes."""
    templates = (
        ("diagnose", "Diagnose and map the smallest correct change for: {goal}"),
        ("implement", "Implement the assigned slice for: {goal}"),
        ("verify", "Verify with tests/evidence for: {goal}"),
    )
    tasks: list[dict[str, Any]] = []
    for i in range(workers):
        kind, template = templates[min(i, len(templates) - 1)]
        depends_on = [f"w{i}"] if i > 0 else []
        # Keep the caller-requested role on every lane (role selects prompt /
        # posture; lane label is advisory metadata only).
        tasks.append(
            _task(
                i,
                subject=template.format(goal=goal),
                role=role,
                depends_on=depends_on,
                lane=kind,
            )
        )
    return tasks


def _task(
    index: int,
    *,
    subject: str,
    role: str,
    depends_on: list[str],
    lane: str | None = None,
) -> dict[str, Any]:
    task_id = f"w{index + 1}"
    owned = [_lane_owned_path(index + 1)]
    row: dict[str, Any] = {
        "task_id": task_id,
        "owned_files": owned,
        "role": role,
        "subject": subject,
        "description": subject,
        "depends_on": list(depends_on),
        "scope_mode": "discover",
    }
    if lane is not None:
        row["lane"] = lane
    return row
