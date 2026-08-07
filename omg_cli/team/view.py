"""Pure Team view planner (#103).

Maps operator context + exact topology target → a typed :class:`ViewPlan`
without tmux, subprocess, or mutation. Effects live in ``tmux`` / ``operator``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from omg_cli.host_models import FeatureGateResult


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
    socket_path: str | None = None


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
    session_id: str,
    pane_id: str,
    window_id: str | None,
    takeover: bool,
    socket_path: str | None = None,
) -> tuple[str, ...]:
    """Build argv-only attach chain targeting exact ``$session_id`` (not name).

    Session names are mutable and reusable — attach must use the proved
    ``$N`` id (same authority as switch-client) to avoid name-reuse TOCTOU.
    """
    argv: list[str] = ["tmux"]
    if (
        isinstance(socket_path, str)
        and socket_path
        and "\0" not in socket_path
        and not any(ch.isspace() for ch in socket_path)
    ):
        argv.extend(["-S", socket_path])
    argv.append("attach-session")
    if takeover:
        argv.append("-d")
    argv.extend(["-t", session_id, ";"])
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
                session_id=target_sid,
                pane_id=pane_id,
                window_id=window_id,
                takeover=False,
                socket_path=request.socket_path,
            ),
            hint="pass --view (without --json) to restore the Team pane",
            session_id=target_sid,
            session_name=target_name,
            window_id=window_id,
            pane_id=pane_id,
        )

    attach_argv = _attach_argv(
        session_id=target_sid,
        pane_id=pane_id,
        window_id=window_id,
        takeover=mode == MODE_TAKEOVER,
        socket_path=request.socket_path,
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


def invoke_provider_session_resume(gate: FeatureGateResult) -> dict[str, Any]:
    """Gate-allowed provider resume entry (no ACP transport in this slice).

    Called only when ``gate.state == "AVAILABLE"``. Real ``session/resume``
    transport / session/load remain later #105 work — this helper records that
    the host gate was consumed and the modern path is permitted.
    """
    if gate.state != "AVAILABLE":
        raise ValueError(
            f"invoke_provider_session_resume requires AVAILABLE gate, got {gate.state}"
        )
    return {
        "invoked": True,
        "transport_wired": False,
        "hint": (
            "host session_resume gate AVAILABLE; ACP session/resume transport "
            "not wired in this OMG slice (tmux view remains independent; "
            "no_replay=true, restore_code=false)"
        ),
    }


def provider_session_result(
    *,
    requested: bool,
    gate: FeatureGateResult | None = None,
    provider_resume: Callable[[FeatureGateResult], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Truthful ACP provider-session envelope from a host :class:`FeatureGateResult`.

    Independent of tmux view/attach. Does not re-parse host versions — callers
    (CLI) inject the gate from :func:`omg_cli.host_probe.evaluate_feature_gate`.

    Status mapping:
    - not requested → ``not_requested``
    - AVAILABLE → invoke provider helper; status ``available``
    - LEGACY → ``legacy`` + actionable ``next_action`` (never pretends available)
    - BLOCKED → ``blocked`` (fail-closed when ``gate.required``)
    - requested but no gate → ``blocked`` (refuse rather than invent support)
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

    if gate is None:
        base["status"] = "blocked"
        base["reason"] = "provider session requested without a host FeatureGateResult"
        base["next_action"] = (
            "Pass session_resume_gate from probe_host/evaluate_feature_gate "
            "(see docs/host-compat.md)"
        )
        base["required"] = True
        return base

    base["gate"] = gate.to_dict()
    base["capability"] = gate.capability
    base["required"] = bool(gate.required)

    if gate.state == "AVAILABLE":
        helper = provider_resume or invoke_provider_session_resume
        helper_out = dict(helper(gate))
        # Envelope owns status; helper must not downgrade AVAILABLE → success claim.
        helper_out.pop("status", None)
        base["reason"] = gate.reason
        base.update(helper_out)
        base["status"] = "available"
        return base

    if gate.state == "LEGACY":
        base["status"] = "legacy"
        base["reason"] = gate.reason
        if gate.next_action:
            base["next_action"] = gate.next_action
        base["hint"] = (
            "legacy provider-session path; not AVAILABLE — "
            "tmux view success does not imply provider resume"
        )
        return base

    # BLOCKED (or unknown)
    base["status"] = "blocked"
    base["reason"] = gate.reason
    if gate.next_action:
        base["next_action"] = gate.next_action
    base["hint"] = (
        "provider session resume refused by host gate; "
        "tmux view is independent and must not be treated as resume success"
    )
    return base


def provider_session_stub(*, requested: bool) -> dict[str, Any]:
    """Deprecated alias — prefer :func:`provider_session_result` with a gate.

    Kept for import compatibility; requested=True without a gate is blocked.
    """
    return provider_session_result(requested=requested, gate=None)


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
    "invoke_provider_session_resume",
    "plan_team_view",
    "provider_session_result",
    "provider_session_stub",
    "view_result_dict",
]
