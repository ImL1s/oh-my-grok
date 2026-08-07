"""Team view_mode / topology contract (#96 + #102).

Canonical launch modes:
- ``same_window`` — inside tmux default; workers split beside the leader
- ``dedicated_window`` — inside tmux ``--dedicated-window`` / legacy inside
- ``detached_session`` — outside tmux or ``--detach``

Persisted-only mode (never returned by :func:`resolve_launch_view_mode`):
- ``legacy_windows`` — one worker per tmux window (pre-split adapter)

Legacy metadata without ``view_mode`` must not invent ``same_window`` from
pane counts, and must not invent ``dedicated_window`` for shapes that also
match same-window (shared session + leader ``window_id``). Prefer explicit
mode; legacy dedicated only when window naming evidence is unambiguous.

#102 adds a canonical :class:`TopologySnapshot` so scale / resume / relaunch
consume the persisted topology as authority rather than inferring from
``window_index`` or live pane arrangement.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

VIEW_MODE_SAME_WINDOW = "same_window"
VIEW_MODE_DEDICATED_WINDOW = "dedicated_window"
VIEW_MODE_DETACHED_SESSION = "detached_session"
VIEW_MODE_LEGACY_WINDOWS = "legacy_windows"

LAUNCH_VIEW_MODES = frozenset(
    {
        VIEW_MODE_SAME_WINDOW,
        VIEW_MODE_DEDICATED_WINDOW,
        VIEW_MODE_DETACHED_SESSION,
    }
)
# Back-compat alias used by launch receipt / intent writers (#96).
VIEW_MODES = LAUNCH_VIEW_MODES

PERSISTED_VIEW_MODES = LAUNCH_VIEW_MODES | {VIEW_MODE_LEGACY_WINDOWS}

LAYOUT_MAIN_VERTICAL = "main-vertical"
LAYOUT_TILED = "tiled"

TMUX_TOPOLOGY_SCHEMA_VERSION = 1
PLACEMENT_RIGHT_STACK = "right_stack"
PLACEMENT_LEGACY_WINDOW = "legacy_window"
LAYOUT_STATUS_CLEAN = "clean"
LAYOUT_STATUS_PENDING = "pending"
LAYOUT_STATUS_REPAIR_NEEDED = "repair_needed"

# Dedicated inside windows are named ``omg-team-<nonce>``; same_window WAL
# uses a synthetic ``omg-same-<nonce>`` key that must never imply dedicated.
_DEDICATED_WINDOW_NAME_PREFIX = "omg-team-"

_SESSION_ID_RE = re.compile(r"^\$[0-9]{1,16}$")
_PANE_ID_RE = re.compile(r"^%[0-9]{1,16}$")
_WINDOW_ID_RE = re.compile(r"^@[0-9]{1,16}$")


class TopologyError(ValueError):
    """Invalid view_mode / attach / topology combination."""


@dataclass(frozen=True)
class TopologyAnchor:
    """Immutable launch-bound placement authority (#102)."""

    mode: str
    session_name: str
    session_id: str
    launch_nonce: str
    session_owned: bool
    team_window_id: str | None
    leader_pane_id: str | None
    leader_pane_pid: int | None
    owner_token_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "session_name": self.session_name,
            "session_id": self.session_id,
            "launch_nonce": self.launch_nonce,
            "session_owned": self.session_owned,
            "team_window_id": self.team_window_id,
            "leader_pane_id": self.leader_pane_id,
            "leader_pane_pid": self.leader_pane_pid,
            "owner_token_sha256": self.owner_token_sha256,
        }


@dataclass(frozen=True)
class ActiveWorkerRef:
    """Projection of one active worker from the identity chain head."""

    task_id: str
    logical_worker_index: int
    attempt: int
    window_id: str | None
    pane_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "logical_worker_index": self.logical_worker_index,
            "attempt": self.attempt,
            "window_id": self.window_id,
            "pane_id": self.pane_id,
        }


@dataclass(frozen=True)
class PlacementTarget:
    """Exact tmux placement target for add / relaunch."""

    session_id: str
    window_id: str | None
    leader_pane_id: str | None
    split_target_pane_id: str | None
    strategy: str
    horizontal_first: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "window_id": self.window_id,
            "leader_pane_id": self.leader_pane_id,
            "split_target_pane_id": self.split_target_pane_id,
            "strategy": self.strategy,
            "horizontal_first": self.horizontal_first,
        }


@dataclass(frozen=True)
class TopologySnapshot:
    """Canonical persisted topology for lifecycle mutations (#102)."""

    mode: str
    topology_string: str  # top-level team.json "split" | "windows"
    anchor: TopologyAnchor
    active_workers: tuple[ActiveWorkerRef, ...] = ()
    identity_generation: int = 0
    identity_receipt_sha256: str | None = None
    placement_strategy: str = PLACEMENT_RIGHT_STACK
    right_stack_root_pane_id: str | None = None
    layout_name: str = LAYOUT_MAIN_VERTICAL
    leader_width_policy: str = "clamped_half"
    layout_status: str = LAYOUT_STATUS_CLEAN
    layout_last_error_code: str | None = None
    schema_version: int = TMUX_TOPOLOGY_SCHEMA_VERSION
    extras: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_legacy(self) -> bool:
        return self.mode == VIEW_MODE_LEGACY_WINDOWS

    @property
    def is_split(self) -> bool:
        return self.mode in {
            VIEW_MODE_SAME_WINDOW,
            VIEW_MODE_DEDICATED_WINDOW,
            VIEW_MODE_DETACHED_SESSION,
        }

    def to_tmux_topology_dict(self) -> dict[str, Any]:
        """Serialize the nested ``tmux_topology`` object for team.json."""
        return {
            "schema_version": self.schema_version,
            "anchor": self.anchor.to_dict(),
            "identity_generation": self.identity_generation,
            "identity_receipt_sha256": self.identity_receipt_sha256,
            "active_workers": [w.to_dict() for w in self.active_workers],
            "placement": {
                "strategy": self.placement_strategy,
                "right_stack_root_pane_id": self.right_stack_root_pane_id,
            },
            "layout": {
                "name": self.layout_name,
                "leader_width_policy": self.leader_width_policy,
                "status": self.layout_status,
                "last_error_code": self.layout_last_error_code,
            },
        }


def resolve_launch_view_mode(
    *,
    inside_tmux: bool,
    dedicated_window: bool = False,
    detach: bool = False,
) -> str:
    """Pure resolver shared by plan-only, dry-run, and live launch.

    Fail closed when ``--dedicated-window`` is combined with detached launch
    (outside tmux or explicit ``--detach``) — never silently degrade.
    Never returns ``legacy_windows``.
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
    - top-level ``topology == "windows"`` → ``legacy_windows`` (#102)
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
        if isinstance(raw, str) and raw in PERSISTED_VIEW_MODES:
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
    topology = merged.get("topology")

    # Explicit legacy windows topology string — never promote to same_window.
    if topology == "windows":
        return VIEW_MODE_LEGACY_WINDOWS

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
    if view_mode == VIEW_MODE_LEGACY_WINDOWS:
        return LAYOUT_TILED
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


def _require_session_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SESSION_ID_RE.fullmatch(value) is None:
        raise TopologyError(f"{label} must be an exact tmux session id")
    return value


def _optional_window_id(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _WINDOW_ID_RE.fullmatch(value) is None:
        raise TopologyError(f"{label} must be an exact tmux window id")
    return value


def _optional_pane_id(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _PANE_ID_RE.fullmatch(value) is None:
        raise TopologyError(f"{label} must be an exact tmux pane id")
    return value


def validate_topology_anchor(anchor: TopologyAnchor | Mapping[str, Any]) -> TopologyAnchor:
    """Fail-closed validation of an immutable topology anchor."""
    if isinstance(anchor, TopologyAnchor):
        data = anchor.to_dict()
    elif isinstance(anchor, Mapping):
        data = dict(anchor)
    else:
        raise TopologyError("topology anchor must be a mapping")

    mode = data.get("mode")
    if not isinstance(mode, str) or mode not in PERSISTED_VIEW_MODES:
        raise TopologyError(f"topology anchor mode unsupported: {mode!r}")

    session_name = data.get("session_name")
    if not isinstance(session_name, str) or not session_name:
        raise TopologyError("topology anchor session_name required")

    session_id = _require_session_id(data.get("session_id"), label="topology anchor session_id")

    launch_nonce = data.get("launch_nonce")
    if not isinstance(launch_nonce, str) or not launch_nonce:
        raise TopologyError("topology anchor launch_nonce required")

    session_owned = data.get("session_owned")
    if not isinstance(session_owned, bool):
        raise TopologyError("topology anchor session_owned must be bool")

    team_window_id = _optional_window_id(
        data.get("team_window_id"), label="topology anchor team_window_id"
    )
    leader_pane_id = _optional_pane_id(
        data.get("leader_pane_id"), label="topology anchor leader_pane_id"
    )

    leader_pane_pid = data.get("leader_pane_pid")
    if leader_pane_pid is not None and (
        isinstance(leader_pane_pid, bool)
        or not isinstance(leader_pane_pid, int)
        or leader_pane_pid <= 0
    ):
        raise TopologyError("topology anchor leader_pane_pid must be a positive int")

    owner_token_sha256 = data.get("owner_token_sha256")
    if owner_token_sha256 is not None and (
        not isinstance(owner_token_sha256, str) or not owner_token_sha256
    ):
        raise TopologyError("topology anchor owner_token_sha256 must be a string")

    # Split modes (except legacy) require an exact Team window.
    if mode != VIEW_MODE_LEGACY_WINDOWS and team_window_id is None:
        raise TopologyError(
            f"topology anchor for {mode} requires exact team_window_id"
        )
    if mode == VIEW_MODE_SAME_WINDOW and leader_pane_id is None:
        raise TopologyError(
            "same_window topology anchor requires exact leader_pane_id"
        )

    return TopologyAnchor(
        mode=mode,
        session_name=session_name,
        session_id=session_id,
        launch_nonce=launch_nonce,
        session_owned=session_owned,
        team_window_id=team_window_id,
        leader_pane_id=leader_pane_id,
        leader_pane_pid=leader_pane_pid if isinstance(leader_pane_pid, int) else None,
        owner_token_sha256=owner_token_sha256
        if isinstance(owner_token_sha256, str)
        else None,
    )


def derive_worker_stack(
    workers: Sequence[ActiveWorkerRef | Mapping[str, Any]],
    *,
    leader_pane_id: str | None = None,
) -> tuple[tuple[ActiveWorkerRef, ...], str | None]:
    """Order active workers by logical index; derive right-stack root pane."""
    normalized: list[ActiveWorkerRef] = []
    for raw in workers:
        if isinstance(raw, ActiveWorkerRef):
            ref = raw
        elif isinstance(raw, Mapping):
            tid = raw.get("task_id")
            idx = raw.get("logical_worker_index", raw.get("window_index"))
            attempt = raw.get("attempt", 1)
            if not isinstance(tid, str) or not tid:
                raise TopologyError("active worker task_id required")
            if (
                isinstance(idx, bool)
                or not isinstance(idx, int)
                or idx < 0
            ):
                raise TopologyError(
                    f"active worker logical_worker_index invalid for {tid!r}"
                )
            if (
                isinstance(attempt, bool)
                or not isinstance(attempt, int)
                or attempt < 1
            ):
                raise TopologyError(f"active worker attempt invalid for {tid!r}")
            window_id = _optional_window_id(
                raw.get("window_id"), label=f"worker {tid} window_id"
            )
            pane_id = _optional_pane_id(
                raw.get("pane_id"), label=f"worker {tid} pane_id"
            )
            ref = ActiveWorkerRef(
                task_id=tid,
                logical_worker_index=idx,
                attempt=attempt,
                window_id=window_id,
                pane_id=pane_id,
            )
        else:
            raise TopologyError("active worker must be a mapping")
        if leader_pane_id is not None and ref.pane_id == leader_pane_id:
            continue
        normalized.append(ref)
    normalized.sort(key=lambda w: (w.logical_worker_index, w.task_id))
    # Right-stack root is the first ordered worker pane (derived state).
    # Scale-add splits against the *last* ordered pane (placement_target_for_add).
    root: str | None = None
    if normalized and isinstance(normalized[0].pane_id, str):
        if _PANE_ID_RE.fullmatch(normalized[0].pane_id):
            root = normalized[0].pane_id
    return tuple(normalized), root


def _active_workers_from_tasks(
    tasks: Sequence[Mapping[str, Any]],
    *,
    leader_pane_id: str | None = None,
) -> tuple[ActiveWorkerRef, ...]:
    rows: list[dict[str, Any]] = []
    for raw in tasks:
        if not isinstance(raw, Mapping):
            continue
        if raw.get("status") == "scaled_down":
            continue
        tid = raw.get("task_id")
        if not isinstance(tid, str) or not tid:
            continue
        idx = raw.get("logical_worker_index", raw.get("window_index"))
        attempt = raw.get("attempt", 1)
        rows.append(
            {
                "task_id": tid,
                "logical_worker_index": idx,
                "attempt": attempt,
                "window_id": raw.get("window_id"),
                "pane_id": raw.get("pane_id"),
            }
        )
    ordered, _root = derive_worker_stack(rows, leader_pane_id=leader_pane_id)
    return ordered


def _owner_token_sha256(meta: Mapping[str, Any] | None) -> str | None:
    if not isinstance(meta, Mapping):
        return None
    token = meta.get("owner_token")
    if not isinstance(token, str) or not token:
        # Prefer pre-hashed field when present.
        hashed = meta.get("owner_token_sha256")
        return hashed if isinstance(hashed, str) and hashed else None
    from omg_cli.contracts.writer_chain import sha256_hex

    return sha256_hex(token.encode("utf-8"))


def normalize_persisted_topology(
    meta: Mapping[str, Any] | None,
    *,
    receipt: Mapping[str, Any] | None = None,
) -> TopologySnapshot:
    """Build a canonical topology snapshot; receipt fields win over team.json.

    Pure: does not execute tmux. Fail closed on missing / ambiguous authority.
    ``topology == "windows"`` always classifies as ``legacy_windows``.
    """
    if not isinstance(meta, Mapping) and not isinstance(receipt, Mapping):
        raise TopologyError("topology normalize requires team meta or launch receipt")

    mode = resolve_persisted_view_mode(meta, receipt=receipt)

    # Merge with receipt preference for identity fields.
    merged: dict[str, Any] = {}
    if isinstance(meta, Mapping):
        merged.update(dict(meta))
    if isinstance(receipt, Mapping):
        for key in (
            "session_name",
            "session_id",
            "launch_nonce",
            "view_mode",
            "window_id",
            "window_name",
            "leader_pane_id",
            "leader_pane_pid",
            "session_owned",
            "attach_mode",
            "layout",
        ):
            if receipt.get(key) is not None:
                merged[key] = receipt[key]
        if receipt.get("session_name") is not None:
            merged["session"] = receipt["session_name"]

    topology_string = merged.get("topology")
    if mode == VIEW_MODE_LEGACY_WINDOWS:
        topology_string = "windows"
    elif topology_string not in {"split", "windows"}:
        # New split modes always use the string "split" at top level.
        topology_string = "split" if mode != VIEW_MODE_LEGACY_WINDOWS else "windows"

    if topology_string == "windows" and mode != VIEW_MODE_LEGACY_WINDOWS:
        raise TopologyError(
            "topology string 'windows' cannot combine with non-legacy view_mode"
        )

    session_name = merged.get("session") or merged.get("session_name")
    if not isinstance(session_name, str) or not session_name:
        raise TopologyError("persisted topology missing session name")

    session_id = merged.get("session_id")
    if not isinstance(session_id, str) or _SESSION_ID_RE.fullmatch(session_id) is None:
        # Dry-run / incomplete meta: fail closed for lifecycle, not status.
        raise TopologyError("persisted topology missing exact session_id")

    launch_nonce = merged.get("launch_nonce")
    if not isinstance(launch_nonce, str) or not launch_nonce:
        raise TopologyError("persisted topology missing launch_nonce")

    session_owned = merged.get("session_owned")
    if not isinstance(session_owned, bool):
        # Infer from mode when absent.
        session_owned = mode == VIEW_MODE_DETACHED_SESSION

    team_window_id = merged.get("window_id")
    if isinstance(team_window_id, str) and team_window_id == "":
        team_window_id = None

    leader_pane_id = merged.get("leader_pane_id")
    if isinstance(leader_pane_id, str) and leader_pane_id == "":
        leader_pane_id = None

    leader_pane_pid = merged.get("leader_pane_pid")
    if isinstance(leader_pane_pid, bool):
        leader_pane_pid = None

    # Nested tmux_topology.anchor overrides when present and consistent.
    nested = None
    if isinstance(meta, Mapping):
        nested = meta.get("tmux_topology")
    if isinstance(nested, Mapping):
        nested_anchor = nested.get("anchor")
        if isinstance(nested_anchor, Mapping):
            # Prefer nested exact fields but keep mode from resolve.
            for key, dest in (
                ("session_name", "session"),
                ("session_id", "session_id"),
                ("launch_nonce", "launch_nonce"),
                ("session_owned", "session_owned"),
                ("team_window_id", "window_id"),
                ("leader_pane_id", "leader_pane_id"),
                ("leader_pane_pid", "leader_pane_pid"),
            ):
                if nested_anchor.get(key) is not None:
                    if dest == "session":
                        session_name = nested_anchor[key]
                    elif dest == "session_id":
                        session_id = nested_anchor[key]
                    elif dest == "launch_nonce":
                        launch_nonce = nested_anchor[key]
                    elif dest == "session_owned":
                        session_owned = nested_anchor[key]
                    elif dest == "window_id":
                        team_window_id = nested_anchor[key]
                    elif dest == "leader_pane_id":
                        leader_pane_id = nested_anchor[key]
                    elif dest == "leader_pane_pid":
                        leader_pane_pid = nested_anchor[key]
            nested_mode = nested_anchor.get("mode")
            if isinstance(nested_mode, str) and nested_mode in PERSISTED_VIEW_MODES:
                if nested_mode != mode:
                    raise TopologyError(
                        f"tmux_topology.anchor.mode {nested_mode!r} disagrees "
                        f"with resolved view_mode {mode!r}"
                    )

    anchor = validate_topology_anchor(
        {
            "mode": mode,
            "session_name": session_name,
            "session_id": session_id,
            "launch_nonce": launch_nonce,
            "session_owned": bool(session_owned),
            "team_window_id": team_window_id,
            "leader_pane_id": leader_pane_id,
            "leader_pane_pid": leader_pane_pid,
            "owner_token_sha256": _owner_token_sha256(
                meta if isinstance(meta, Mapping) else None
            ),
        }
    )

    tasks: Sequence[Mapping[str, Any]] = ()
    if isinstance(meta, Mapping) and isinstance(meta.get("tasks"), list):
        tasks = [t for t in meta["tasks"] if isinstance(t, Mapping)]
    elif isinstance(receipt, Mapping) and isinstance(receipt.get("tasks"), list):
        tasks = [t for t in receipt["tasks"] if isinstance(t, Mapping)]

    # Prefer nested active_workers projection when present.
    active: tuple[ActiveWorkerRef, ...]
    right_stack_root: str | None = None
    if isinstance(nested, Mapping) and isinstance(nested.get("active_workers"), list):
        active, right_stack_root = derive_worker_stack(
            [w for w in nested["active_workers"] if isinstance(w, Mapping)],
            leader_pane_id=anchor.leader_pane_id,
        )
    else:
        active = _active_workers_from_tasks(
            tasks, leader_pane_id=anchor.leader_pane_id
        )
        _, right_stack_root = derive_worker_stack(
            active, leader_pane_id=anchor.leader_pane_id
        )

    identity_generation = 0
    if isinstance(meta, Mapping):
        gen = meta.get("identity_generation", 0)
        if isinstance(gen, int) and not isinstance(gen, bool) and gen >= 0:
            identity_generation = gen
        if isinstance(nested, Mapping):
            nested_gen = nested.get("identity_generation")
            if (
                isinstance(nested_gen, int)
                and not isinstance(nested_gen, bool)
                and nested_gen >= 0
            ):
                identity_generation = nested_gen

    identity_receipt_sha256 = None
    if isinstance(meta, Mapping):
        identity_receipt_sha256 = meta.get("identity_receipt_sha256") or meta.get(
            "launch_receipt_sha256"
        )
        if isinstance(nested, Mapping) and nested.get("identity_receipt_sha256"):
            identity_receipt_sha256 = nested.get("identity_receipt_sha256")
    if identity_receipt_sha256 is not None and not isinstance(
        identity_receipt_sha256, str
    ):
        identity_receipt_sha256 = None

    placement_strategy = (
        PLACEMENT_LEGACY_WINDOW if mode == VIEW_MODE_LEGACY_WINDOWS else PLACEMENT_RIGHT_STACK
    )
    layout_name = layout_for_view_mode(mode)
    layout_status = LAYOUT_STATUS_CLEAN
    layout_error: str | None = None
    leader_width_policy = "clamped_half"
    if isinstance(nested, Mapping):
        placement = nested.get("placement")
        if isinstance(placement, Mapping):
            strategy = placement.get("strategy")
            if isinstance(strategy, str) and strategy:
                placement_strategy = strategy
            root = placement.get("right_stack_root_pane_id")
            if isinstance(root, str) and _PANE_ID_RE.fullmatch(root):
                right_stack_root = root
        layout = nested.get("layout")
        if isinstance(layout, Mapping):
            name = layout.get("name")
            if isinstance(name, str) and name:
                layout_name = name
            status = layout.get("status")
            if status in {
                LAYOUT_STATUS_CLEAN,
                LAYOUT_STATUS_PENDING,
                LAYOUT_STATUS_REPAIR_NEEDED,
            }:
                layout_status = status
            err = layout.get("last_error_code")
            layout_error = err if isinstance(err, str) else None
            policy = layout.get("leader_width_policy")
            if isinstance(policy, str) and policy:
                leader_width_policy = policy
    elif isinstance(merged.get("layout"), str) and merged["layout"]:
        layout_name = str(merged["layout"])

    return TopologySnapshot(
        mode=mode,
        topology_string=str(topology_string),
        anchor=anchor,
        active_workers=active,
        identity_generation=identity_generation,
        identity_receipt_sha256=identity_receipt_sha256,
        placement_strategy=placement_strategy,
        right_stack_root_pane_id=right_stack_root,
        layout_name=layout_name,
        leader_width_policy=leader_width_policy,
        layout_status=layout_status,
        layout_last_error_code=layout_error,
    )


def build_topology_snapshot(
    meta: Mapping[str, Any] | None,
    *,
    receipt: Mapping[str, Any] | None = None,
    active_workers: Sequence[ActiveWorkerRef | Mapping[str, Any]] | None = None,
    layout_status: str | None = None,
    layout_last_error_code: str | None = None,
) -> TopologySnapshot:
    """Normalize then optionally override active workers / layout status."""
    snap = normalize_persisted_topology(meta, receipt=receipt)
    workers = snap.active_workers
    root = snap.right_stack_root_pane_id
    if active_workers is not None:
        workers, root = derive_worker_stack(
            active_workers, leader_pane_id=snap.anchor.leader_pane_id
        )
    status = snap.layout_status if layout_status is None else layout_status
    if status not in {
        LAYOUT_STATUS_CLEAN,
        LAYOUT_STATUS_PENDING,
        LAYOUT_STATUS_REPAIR_NEEDED,
    }:
        raise TopologyError(f"unsupported layout status {status!r}")
    err = (
        snap.layout_last_error_code
        if layout_last_error_code is None
        else layout_last_error_code
    )
    return TopologySnapshot(
        mode=snap.mode,
        topology_string=snap.topology_string,
        anchor=snap.anchor,
        active_workers=workers,
        identity_generation=snap.identity_generation,
        identity_receipt_sha256=snap.identity_receipt_sha256,
        placement_strategy=snap.placement_strategy,
        right_stack_root_pane_id=root,
        layout_name=snap.layout_name,
        leader_width_policy=snap.leader_width_policy,
        layout_status=status,
        layout_last_error_code=err,
        schema_version=snap.schema_version,
    )


def topology_sha256(snapshot: TopologySnapshot | Mapping[str, Any]) -> str:
    """Canonical fingerprint of topology authority (anchor + mode + window)."""
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    if isinstance(snapshot, TopologySnapshot):
        payload = {
            "mode": snapshot.mode,
            "topology_string": snapshot.topology_string,
            "anchor": snapshot.anchor.to_dict(),
            "placement_strategy": snapshot.placement_strategy,
            "layout_name": snapshot.layout_name,
        }
    elif isinstance(snapshot, Mapping):
        payload = dict(snapshot)
    else:
        raise TopologyError("topology_sha256 requires TopologySnapshot or mapping")
    return sha256_hex(canonical_json_bytes(payload))


def placement_target_for_add(snapshot: TopologySnapshot) -> PlacementTarget:
    """Exact placement for scale-up under the persisted topology mode."""
    anchor = snapshot.anchor
    if snapshot.mode == VIEW_MODE_LEGACY_WINDOWS:
        return PlacementTarget(
            session_id=anchor.session_id,
            window_id=None,
            leader_pane_id=None,
            split_target_pane_id=None,
            strategy=PLACEMENT_LEGACY_WINDOW,
            horizontal_first=False,
        )

    window_id = anchor.team_window_id
    if window_id is None:
        raise TopologyError(
            f"{snapshot.mode} scale-up requires exact team_window_id"
        )

    # Prefer last ordered active worker pane as vertical split target.
    split_target: str | None = None
    for worker in reversed(snapshot.active_workers):
        if (
            isinstance(worker.pane_id, str)
            and _PANE_ID_RE.fullmatch(worker.pane_id)
            and worker.pane_id != anchor.leader_pane_id
        ):
            split_target = worker.pane_id
            break

    horizontal_first = False
    if split_target is None:
        if snapshot.mode == VIEW_MODE_SAME_WINDOW:
            if anchor.leader_pane_id is None:
                raise TopologyError(
                    "same_window scale-up with empty stack requires leader pane"
                )
            split_target = anchor.leader_pane_id
            horizontal_first = True
        else:
            # dedicated / detached: first worker splits from the Team window
            # itself (tiled). Caller may pass window id as split target.
            split_target = None
            horizontal_first = False

    return PlacementTarget(
        session_id=anchor.session_id,
        window_id=window_id,
        leader_pane_id=anchor.leader_pane_id,
        split_target_pane_id=split_target,
        strategy=PLACEMENT_RIGHT_STACK,
        horizontal_first=horizontal_first,
    )


def placement_target_for_relaunch(
    snapshot: TopologySnapshot,
    *,
    task_id: str | None = None,
) -> PlacementTarget:
    """Exact placement for relaunch — same Team window / mode as launch."""
    _ = task_id  # reserved for per-worker legacy window targeting
    anchor = snapshot.anchor
    if snapshot.mode == VIEW_MODE_LEGACY_WINDOWS:
        return PlacementTarget(
            session_id=anchor.session_id,
            window_id=None,
            leader_pane_id=None,
            split_target_pane_id=None,
            strategy=PLACEMENT_LEGACY_WINDOW,
            horizontal_first=False,
        )
    window_id = anchor.team_window_id
    if window_id is None:
        raise TopologyError(
            f"{snapshot.mode} relaunch requires exact team_window_id"
        )
    # Relaunch splits into the Team window; prefer last stack pane when present.
    split_target: str | None = None
    for worker in reversed(snapshot.active_workers):
        if (
            isinstance(worker.pane_id, str)
            and _PANE_ID_RE.fullmatch(worker.pane_id)
            and worker.pane_id != anchor.leader_pane_id
        ):
            split_target = worker.pane_id
            break
    if split_target is None and snapshot.mode == VIEW_MODE_SAME_WINDOW:
        split_target = anchor.leader_pane_id
    return PlacementTarget(
        session_id=anchor.session_id,
        window_id=window_id,
        leader_pane_id=anchor.leader_pane_id,
        split_target_pane_id=split_target,
        strategy=PLACEMENT_RIGHT_STACK,
        horizontal_first=False,
    )


__all__ = [
    "ActiveWorkerRef",
    "LAYOUT_MAIN_VERTICAL",
    "LAYOUT_STATUS_CLEAN",
    "LAYOUT_STATUS_PENDING",
    "LAYOUT_STATUS_REPAIR_NEEDED",
    "LAYOUT_TILED",
    "LAUNCH_VIEW_MODES",
    "PERSISTED_VIEW_MODES",
    "PLACEMENT_LEGACY_WINDOW",
    "PLACEMENT_RIGHT_STACK",
    "PlacementTarget",
    "TMUX_TOPOLOGY_SCHEMA_VERSION",
    "TopologyAnchor",
    "TopologyError",
    "TopologySnapshot",
    "VIEW_MODE_DEDICATED_WINDOW",
    "VIEW_MODE_DETACHED_SESSION",
    "VIEW_MODE_LEGACY_WINDOWS",
    "VIEW_MODE_SAME_WINDOW",
    "VIEW_MODES",
    "build_topology_snapshot",
    "clamp_main_vertical_leader_width",
    "derive_worker_stack",
    "layout_for_view_mode",
    "normalize_persisted_topology",
    "placement_target_for_add",
    "placement_target_for_relaunch",
    "resolve_launch_view_mode",
    "resolve_persisted_view_mode",
    "topology_sha256",
    "validate_topology_anchor",
]
