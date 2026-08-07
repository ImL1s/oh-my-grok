"""Team view_mode / topology contract (#96).

Canonical modes:
- ``same_window`` — inside tmux default; workers split beside the leader
- ``dedicated_window`` — inside tmux ``--dedicated-window`` / legacy inside
- ``detached_session`` — outside tmux or ``--detach``

Legacy metadata without ``view_mode`` must not invent ``same_window`` from
pane counts, and must not invent ``dedicated_window`` for shapes that also
match same-window (shared session + leader ``window_id``). Prefer explicit
mode; legacy dedicated only when window naming evidence is unambiguous.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

VIEW_MODE_SAME_WINDOW = "same_window"
VIEW_MODE_DEDICATED_WINDOW = "dedicated_window"
VIEW_MODE_DETACHED_SESSION = "detached_session"

VIEW_MODES = frozenset(
    {
        VIEW_MODE_SAME_WINDOW,
        VIEW_MODE_DEDICATED_WINDOW,
        VIEW_MODE_DETACHED_SESSION,
    }
)

LAYOUT_MAIN_VERTICAL = "main-vertical"
LAYOUT_TILED = "tiled"

# Dedicated inside windows are named ``omg-team-<nonce>``; same_window WAL
# uses a synthetic ``omg-same-<nonce>`` key that must never imply dedicated.
_DEDICATED_WINDOW_NAME_PREFIX = "omg-team-"


class TopologyError(ValueError):
    """Invalid view_mode / attach combination."""


def resolve_launch_view_mode(
    *,
    inside_tmux: bool,
    dedicated_window: bool = False,
    detach: bool = False,
) -> str:
    """Pure resolver shared by plan-only, dry-run, and live launch.

    Fail closed when ``--dedicated-window`` is combined with detached launch
    (outside tmux or explicit ``--detach``) — never silently degrade.
    """
    if dedicated_window and (detach or not inside_tmux):
        raise TopologyError(
            "--dedicated-window requires an interactive inside-tmux launch "
            "(refuse outside tmux / --detach)"
        )
    if detach or not inside_tmux:
        return VIEW_MODE_DETACHED_SESSION
    if dedicated_window:
        return VIEW_MODE_DEDICATED_WINDOW
    return VIEW_MODE_SAME_WINDOW


def resolve_persisted_view_mode(
    meta: Mapping[str, Any] | None,
    *,
    receipt: Mapping[str, Any] | None = None,
) -> str:
    """Resolve view_mode from receipt-bound / team.json metadata.

    Prefer receipt when present; otherwise team.json. Legacy missing mode:
    - owned / detached session → ``detached_session``
    - inside + dedicated window name (``omg-team-*``) → ``dedicated_window``
    - inside + ``window_id`` alone (leader-window shape) → fail closed
    Never invent ``same_window`` from counts or a bare window id.
    """
    sources: list[Mapping[str, Any]] = []
    if isinstance(receipt, Mapping):
        sources.append(receipt)
    if isinstance(meta, Mapping):
        sources.append(meta)

    for src in sources:
        raw = src.get("view_mode")
        if isinstance(raw, str) and raw in VIEW_MODES:
            return raw
        if raw is not None and raw != "":
            raise TopologyError(f"unsupported persisted view_mode {raw!r}")

    # Legacy inference across receipt then meta (first source wins fields,
    # but scan all sources for unambiguous dedicated window_name).
    merged: dict[str, Any] = {}
    for src in reversed(sources):
        merged.update(dict(src))
    attach = merged.get("attach_mode")
    session_owned = merged.get("session_owned")
    window_id = merged.get("window_id")
    window_name = merged.get("window_name")

    if attach == "detached" or session_owned is True:
        return VIEW_MODE_DETACHED_SESSION

    if attach == "inside" or session_owned is False:
        # Unambiguous pre-#96 / opt-in dedicated: named omg-team-* window.
        if (
            isinstance(window_name, str)
            and window_name.startswith(_DEDICATED_WINDOW_NAME_PREFIX)
            and isinstance(window_id, str)
            and window_id
        ):
            return VIEW_MODE_DEDICATED_WINDOW
        raise TopologyError(
            "legacy team metadata missing view_mode — refuse inside "
            "dedicated/same_window guess (ambiguous leader-window shape)"
        )
    raise TopologyError(
        "team metadata missing view_mode — refuse topology guess"
    )


def layout_for_view_mode(view_mode: str) -> str:
    if view_mode == VIEW_MODE_SAME_WINDOW:
        return LAYOUT_MAIN_VERTICAL
    return LAYOUT_TILED


def clamp_main_vertical_leader_width(
    window_width: int,
    *,
    worker_count: int = 1,
    min_leader: int = 20,
    min_worker_col: int = 20,
) -> int:
    """Compute main-pane width for main-vertical without starving workers."""
    if window_width < 2:
        return max(1, window_width)
    workers = max(1, int(worker_count))
    # Keep at least one worker column; shrink leader first on narrow terminals.
    reserve = min_worker_col if workers >= 1 else 0
    max_leader = max(1, window_width - reserve)
    preferred = max(min_leader, window_width // 2)
    return max(1, min(preferred, max_leader))


__all__ = [
    "LAYOUT_MAIN_VERTICAL",
    "LAYOUT_TILED",
    "TopologyError",
    "VIEW_MODE_DEDICATED_WINDOW",
    "VIEW_MODE_DETACHED_SESSION",
    "VIEW_MODE_SAME_WINDOW",
    "VIEW_MODES",
    "clamp_main_vertical_leader_width",
    "layout_for_view_mode",
    "resolve_launch_view_mode",
    "resolve_persisted_view_mode",
]
