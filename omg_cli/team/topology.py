"""Team view_mode / topology contract (#96).

Canonical modes:
- ``same_window`` — inside tmux default; workers split beside the leader
- ``dedicated_window`` — inside tmux ``--dedicated-window`` / legacy inside
- ``detached_session`` — outside tmux or ``--detach``

Legacy metadata without ``view_mode`` resolves read-only to dedicated/detached
behavior — never guess ``same_window`` from pane counts.
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
    - inside shared session (``session_owned=False``) → ``dedicated_window``
    - owned / detached session → ``detached_session``
    Fail closed when evidence is insufficient.
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

    # Legacy inference — never invent same_window.
    src = sources[0] if sources else {}
    attach = src.get("attach_mode")
    session_owned = src.get("session_owned")
    window_id = src.get("window_id")
    if attach == "inside" or session_owned is False:
        if isinstance(window_id, str) and window_id:
            return VIEW_MODE_DEDICATED_WINDOW
        raise TopologyError(
            "legacy team metadata missing view_mode and insufficient "
            "inside/window evidence — refuse cleanup guess"
        )
    if attach == "detached" or session_owned is True:
        return VIEW_MODE_DETACHED_SESSION
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
