"""Pure Team view planner (#103).

Maps operator context + exact topology target → a typed :class:`ViewPlan`
without tmux, subprocess, or mutation. Effects live in ``tmux`` / ``operator``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


ACTION_NONE = "NONE"
ACTION_PRINT = "PRINT"
ACTION_SELECT = "SELECT"
ACTION_SWITCH_CLIENT = "SWITCH_CLIENT"
ACTION_ATTACH = "ATTACH"
ACTION_REFUSE = "REFUSE"

MODE_NONE = "none"
MODE_PRINT = "print"
MODE_VIEW = "view"
MODE_TAKEOVER = "takeover"

_VALID_MODES = frozenset({MODE_NONE, MODE_PRINT, MODE_VIEW, MODE_TAKEOVER})
_VALID_ACTIONS = frozenset(
    {
        ACTION_NONE,
        ACTION_PRINT,
        ACTION_SELECT,
        ACTION_SWITCH_CLIENT,
        ACTION_ATTACH,
        ACTION_REFUSE,
    }
)


@dataclass(frozen=True)
class ViewRequest:
    """Inputs for the pure view planner (no I/O)."""

    mode: str
    inside_tmux: bool
    is_tty: bool
    current_session_id: str | None
    target_session_id: str
    target_session_name: str
    target_window_id: str | None
    target_pane_id: str
    as_json: bool = False
    takeover: bool = False


@dataclass(frozen=True)
class ViewPlan:
    """Typed client-navigation plan. Never mutates state by itself."""

    action: str
    reason: str | None = None
    argv: tuple[str, ...] = ()
    hint: str | None = None
    session_id: str | None = None
    session_name: str | None = None
    window_id: str | None = None
    pane_id: str | None = None
    detach_others: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "argv": list(self.argv),
            "hint": self.hint,
            "session_id": self.session_id,
            "session_name": self.session_name,
            "window_id": self.window_id,
            "pane_id": self.pane_id,
            "detach_others": self.detach_others,
        }


def _safe_session_name(name: str) -> bool:
    return (
        isinstance(name, str)
        and bool(name)
        and "\0" not in name
        and not any(ch.isspace() for ch in name)
    )


def _attach_argv(
    *,
    session_name: str,
    pane_id: str,
    window_id: str | None,
    takeover: bool,
) -> tuple[str, ...]:
    """Build argv-only attach chain (tmux client ``;`` separator, no shell)."""
    argv: list[str] = ["tmux", "attach-session"]
    if takeover:
        argv.append("-d")
    argv.extend(["-t", session_name, ";"])
    if isinstance(window_id, str) and window_id:
        argv.extend(["select-window", "-t", window_id, ";"])
    argv.extend(["select-pane", "-t", pane_id])
    return tuple(argv)


def _switch_argv(
    *,
    session_id: str,
    window_id: str | None,
    pane_id: str,
) -> tuple[str, ...]:
    argv: list[str] = [
        "tmux",
        "switch-client",
        "-t",
        session_id,
        ";",
    ]
    if isinstance(window_id, str) and window_id:
        argv.extend(["select-window", "-t", window_id, ";"])
    argv.extend(["select-pane", "-t", pane_id])
    return tuple(argv)


def _select_argv(
    *,
    window_id: str | None,
    pane_id: str,
) -> tuple[str, ...]:
    argv: list[str] = ["tmux"]
    if isinstance(window_id, str) and window_id:
        argv.extend(["select-window", "-t", window_id, ";"])
    argv.extend(["select-pane", "-t", pane_id])
    return tuple(argv)


def plan_team_view(request: ViewRequest) -> ViewPlan:
    """Pure planner: environment + exact target → :class:`ViewPlan`.

    Never calls tmux. Callers must re-prove identity before executing.
    """
    mode = request.mode if request.mode in _VALID_MODES else MODE_NONE
    if request.takeover and mode == MODE_VIEW:
        mode = MODE_TAKEOVER

    target_sid = request.target_session_id
    target_name = request.target_session_name
    window_id = request.target_window_id
    pane_id = request.target_pane_id

    base = ViewPlan(
        action=ACTION_REFUSE,
        session_id=target_sid,
        session_name=target_name,
        window_id=window_id,
        pane_id=pane_id,
    )

    if not _safe_session_name(target_name):
        return ViewPlan(
            action=ACTION_REFUSE,
            reason="unsafe or missing target session name",
            session_id=target_sid,
            session_name=target_name,
            window_id=window_id,
            pane_id=pane_id,
        )
    if not isinstance(pane_id, str) or not pane_id.startswith("%"):
        return ViewPlan(
            action=ACTION_REFUSE,
            reason="missing exact target pane id",
            session_id=target_sid,
            session_name=target_name,
            window_id=window_id,
            pane_id=pane_id,
        )
    if not isinstance(target_sid, str) or not target_sid.startswith("$"):
        return ViewPlan(
            action=ACTION_REFUSE,
            reason="missing exact target session_id",
            session_id=target_sid,
            session_name=target_name,
            window_id=window_id,
            pane_id=pane_id,
        )

    # --json never changes the client, even on a TTY.
    if request.as_json or mode == MODE_NONE:
        return ViewPlan(
            action=ACTION_NONE,
            reason="view not requested" if mode == MODE_NONE else "json disables view effect",
            argv=_attach_argv(
                session_name=target_name,
                pane_id=pane_id,
                window_id=window_id,
                takeover=False,
            ),
            hint="pass --view (without --json) to restore the Team pane",
            session_id=target_sid,
            session_name=target_name,
            window_id=window_id,
            pane_id=pane_id,
        )

    attach_argv = _attach_argv(
        session_name=target_name,
        pane_id=pane_id,
        window_id=window_id,
        takeover=mode == MODE_TAKEOVER,
    )
    select_argv = _select_argv(window_id=window_id, pane_id=pane_id)
    switch_argv = _switch_argv(
        session_id=target_sid,
        window_id=window_id,
        pane_id=pane_id,
    )

    if mode == MODE_PRINT:
        # Prefer context-appropriate hint argv without executing.
        if request.inside_tmux:
            same = (
                isinstance(request.current_session_id, str)
                and request.current_session_id == target_sid
            )
            argv = select_argv if same else switch_argv
            action_hint = "select" if same else "switch-client"
        else:
            argv = attach_argv
            action_hint = "attach-session"
        return ViewPlan(
            action=ACTION_PRINT,
            reason=f"print-only {action_hint}",
            argv=argv,
            hint=" ".join(argv),
            session_id=target_sid,
            session_name=target_name,
            window_id=window_id,
            pane_id=pane_id,
            detach_others=mode == MODE_TAKEOVER,
        )

    # MODE_VIEW / MODE_TAKEOVER
    if request.inside_tmux:
        current = request.current_session_id
        if not isinstance(current, str) or not current.startswith("$"):
            return ViewPlan(
                action=ACTION_REFUSE,
                reason="inside tmux but current session_id unavailable",
                argv=switch_argv,
                hint=" ".join(switch_argv),
                session_id=target_sid,
                session_name=target_name,
                window_id=window_id,
                pane_id=pane_id,
            )
        if current == target_sid:
            return ViewPlan(
                action=ACTION_SELECT,
                reason="same session: select window and leader pane",
                argv=select_argv,
                hint=" ".join(select_argv),
                session_id=target_sid,
                session_name=target_name,
                window_id=window_id,
                pane_id=pane_id,
            )
        return ViewPlan(
            action=ACTION_SWITCH_CLIENT,
            reason="different session: switch-client then select",
            argv=switch_argv,
            hint=" ".join(switch_argv),
            session_id=target_sid,
            session_name=target_name,
            window_id=window_id,
            pane_id=pane_id,
        )

    # Outside tmux
    if not request.is_tty:
        return ViewPlan(
            action=ACTION_REFUSE,
            reason="outside tmux without TTY: refuse attach",
            argv=attach_argv,
            hint=" ".join(attach_argv),
            session_id=target_sid,
            session_name=target_name,
            window_id=window_id,
            pane_id=pane_id,
            detach_others=mode == MODE_TAKEOVER,
        )

    return ViewPlan(
        action=ACTION_ATTACH,
        reason=(
            "outside tmux TTY: attach-session"
            + (" with -d takeover" if mode == MODE_TAKEOVER else "")
        ),
        argv=attach_argv,
        hint=" ".join(attach_argv),
        session_id=target_sid,
        session_name=target_name,
        window_id=window_id,
        pane_id=pane_id,
        detach_others=mode == MODE_TAKEOVER,
    )


def provider_session_stub(*, requested: bool) -> dict[str, Any]:
    """Truthful ACP provider-session envelope (independent of tmux view).

    #103 does not wire host ACP ``session/resume``; report unsupported when
    requested so reconcile/view/provider outcomes stay separable.
    """
    base: dict[str, Any] = {
        "requested": bool(requested),
        "operation": "resume",
        "no_replay": True,
        "restore_code": False,
    }
    if not requested:
        base["status"] = "not_requested"
        return base
    base["status"] = "unsupported"
    base["hint"] = (
        "host ACP session/resume not wired; tmux view is independent "
        "(no_replay=true, restore_code=false)"
    )
    return base


def view_result_dict(
    plan: ViewPlan,
    *,
    requested: bool,
    status: str,
    mode: str | None = None,
    executed: bool = False,
    error: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``view`` object for resume/view JSON envelopes."""
    out: dict[str, Any] = {
        "requested": bool(requested),
        "status": status,
        "mode": mode,
        "session": plan.session_name,
        "session_id": plan.session_id,
        "window_id": plan.window_id,
        "leader_pane_id": plan.pane_id,
        "action": plan.action.lower() if plan.action else None,
        "hint": plan.hint,
        "argv": list(plan.argv),
        "executed": bool(executed),
        "detach_others": plan.detach_others,
        "reason": plan.reason,
    }
    if error:
        out["error"] = error
    if extra:
        out.update(dict(extra))
    return out


__all__ = [
    "ACTION_ATTACH",
    "ACTION_NONE",
    "ACTION_PRINT",
    "ACTION_REFUSE",
    "ACTION_SELECT",
    "ACTION_SWITCH_CLIENT",
    "MODE_NONE",
    "MODE_PRINT",
    "MODE_TAKEOVER",
    "MODE_VIEW",
    "ViewPlan",
    "ViewRequest",
    "plan_team_view",
    "provider_session_stub",
    "view_result_dict",
]
