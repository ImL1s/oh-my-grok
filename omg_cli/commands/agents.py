"""Agent/model policy CLI (#131).

``omg agents list|explain`` is a read-only inspect surface. It performs no
provider probe, paid inference, or ``verified`` write. Medley catalog facts
stay unsupported/unavailable on stock Grok Build.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from omg_cli.cli_envelope import emit_json, failure, success, wants_json
from omg_cli.cli_util import project_root


def _plugin_root() -> Path:
    from omg_cli.agents_catalog import plugin_root as catalog_root

    return catalog_root()


def _user_home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def cmd_agents(args: argparse.Namespace) -> int:
    action = getattr(args, "agents_action", None)
    if action == "list":
        return _cmd_list(args)
    if action == "explain":
        return _cmd_explain(args)
    print("usage: omg agents {list,explain} …", file=sys.stderr)
    return 2


def _cmd_list(args: argparse.Namespace) -> int:
    from omg_cli.agent_policy import (
        AgentPolicyError,
        filter_policy_views,
        list_agent_policies,
    )
    from omg_cli.host_capabilities import stock_grok_snapshot

    try:
        rows = list_agent_policies(
            root=_plugin_root(),
            project_root=project_root(),
            user_home=_user_home(),
            host=stock_grok_snapshot(),
        )
        rows = filter_policy_views(
            rows,
            agent=getattr(args, "agent", None),
            alias=getattr(args, "alias", None),
            category=getattr(args, "category", None),
            capability_floor=getattr(args, "capability_floor", None),
            policy_source=getattr(args, "policy_source", None),
            host_capability=getattr(args, "host_capability", None),
            status=getattr(args, "status", None),
        )
    except AgentPolicyError as exc:
        return _fail(args, "agents.list", exc)
    payload = {
        "host_tier": "original_grok_build",
        "agents": [row.to_json() for row in rows],
    }
    if wants_json(args):
        emit_json(success("agents.list", data=payload))
        return 0
    print(_render_list_human(rows))
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    from omg_cli.agent_policy import AgentPolicyError, resolve_agent_policy, resume_pin
    from omg_cli.host_capabilities import stock_grok_snapshot

    name = str(getattr(args, "agent_or_profile", "") or "")
    per_run = None
    model = getattr(args, "model", None)
    if model:
        per_run = {"model": str(model)}
    try:
        view = resolve_agent_policy(
            name,
            root=_plugin_root(),
            project_root=project_root(),
            user_home=_user_home(),
            per_run=per_run,
            host=stock_grok_snapshot(),
        )
    except AgentPolicyError as exc:
        return _fail(args, "agents.explain", exc)
    payload = {"agent": view.to_json(), "resume": resume_pin(view)}
    if wants_json(args):
        emit_json(success("agents.explain", data=payload))
        return 0
    print(_render_explain_human(view))
    return 0


def _fail(args: argparse.Namespace, command: str, exc: Exception) -> int:
    code = str(getattr(exc, "code", "E_AGENT_POLICY") or "E_AGENT_POLICY")
    message = str(exc)
    if wants_json(args):
        emit_json(failure(command, code, message))
    else:
        print(f"omg {command.replace('.', ' ')}: {code}: {message}", file=sys.stderr)
    return 2 if code == "E_AGENT_NOT_FOUND" else 1


def _intent_cell(row: object) -> str:
    mode = str(getattr(row, "baseline_mode", ""))
    extension = getattr(row, "requested_extension", None)
    candidates = getattr(row, "candidate_ids", ())
    facts = getattr(row, "host_facts", {}) or {}
    outcome = facts.get("medley_capability_outcome") if isinstance(facts, dict) else None
    if extension and candidates:
        if outcome in {"unsupported", "unavailable", "incompatible"}:
            return f"{mode} ({outcome})"
        shown = " -> ".join(str(item) for item in candidates[:2])
        if len(candidates) > 2:
            shown += " -> …"
        return shown
    return mode


def _policy_cell(row: object) -> str:
    if getattr(row, "requested_extension", None):
        return "optional extension"
    return "baseline"


def _render_list_human(rows: tuple[object, ...]) -> str:
    headers = ("Agent", "Host policy", "Model intent", "Status")
    table = [
        (
            str(getattr(row, "agent_id")),
            _policy_cell(row),
            _intent_cell(row),
            str(getattr(row, "status")),
        )
        for row in rows
    ]
    widths = [len(h) for h in headers]
    for line in table:
        for index, cell in enumerate(line):
            widths[index] = max(widths[index], len(cell))
    out = [
        "  ".join(headers[i].ljust(widths[i]) for i in range(len(headers)))
    ]
    for line in table:
        out.append("  ".join(line[i].ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(out)


def _render_explain_human(view: object) -> str:
    facts = getattr(view, "host_facts", {}) or {}
    reasons = getattr(view, "reasons", ())
    sections = [
        "Identity",
        f"  agent_id: {getattr(view, 'agent_id')}",
        f"  aliases: {', '.join(getattr(view, 'aliases', ()))}",
        f"  category: {getattr(view, 'category')}",
        f"  tier: {getattr(view, 'tier')}",
        "Capability/tool floor",
        f"  capability_floor: {getattr(view, 'capability_floor')}",
        "Policy source and precedence winner",
        f"  policy_id: {getattr(view, 'policy_id')}",
        f"  policy_source: {getattr(view, 'policy_source')}",
        f"  policy_digest: {getattr(view, 'policy_digest')}",
        "Original Grok Build baseline behavior",
        f"  baseline_mode: {getattr(view, 'baseline_mode')}",
        f"  baseline_model: {getattr(view, 'baseline_model')}",
        "Capability-gated Medley policy and candidate order",
        f"  requested_extension: {getattr(view, 'requested_extension')}",
        f"  candidate_ids: {', '.join(getattr(view, 'candidate_ids', ())) or '(none)'}",
        f"  medley_capability_outcome: {facts.get('medley_capability_outcome')}",
        f"  route_specific_facts: {facts.get('route_specific_facts')}",
        "Prompt profile/reasoning preference",
        f"  prompt_profile: {getattr(view, 'prompt_profile')}",
        f"  reasoning_preference: {getattr(view, 'reasoning_preference')}",
        "Selected host facts, when available",
        f"  selected_model_ref: {getattr(view, 'selected_model_ref')}",
        f"  route_kind: {getattr(view, 'route_kind')}",
        f"  route_receipt_digest: {getattr(view, 'route_receipt_digest')}",
        "Rejected/blocked reasons",
    ]
    if reasons:
        for reason in reasons:
            sections.append(
                f"  {getattr(reason, 'code')}: {getattr(reason, 'message')}"
            )
    else:
        sections.append("  (none)")
    sections.extend(
        [
            "Resume/attempt lineage",
            f"  attempt: {getattr(view, 'attempt')}",
            "Next action",
        ]
    )
    if reasons:
        sections.append(f"  {getattr(reasons[0], 'next_action')}")
    else:
        sections.append("  baseline inherit is ready on original Grok Build")
    sections.append(f"status: {getattr(view, 'status')}")
    return "\n".join(sections)


def register_agents_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    p_agents = sub.add_parser(
        "agents",
        parents=[common],
        help=(
            "dual-host agent/model policy list/explain "
            "(Grok baseline; Medley caps unsupported)"
        ),
    )
    agents_sub = p_agents.add_subparsers(dest="agents_action")
    p_list = agents_sub.add_parser(
        "list",
        parents=[common],
        help="list resolved agent/profile policies (read-only; no probe)",
    )
    p_list.add_argument("--agent", default=None, help="filter by agent id or alias")
    p_list.add_argument("--alias", default=None, help="filter by alias")
    p_list.add_argument("--category", default=None, help="filter by category/tier")
    p_list.add_argument(
        "--capability-floor",
        dest="capability_floor",
        default=None,
        help="filter by capability floor (read-only|read-write)",
    )
    p_list.add_argument(
        "--policy-source",
        dest="policy_source",
        default=None,
        help="filter by precedence winner (canonical|user|project|per_run)",
    )
    p_list.add_argument(
        "--host-capability",
        dest="host_capability",
        default=None,
        help="filter by capability id or id=state",
    )
    p_list.add_argument("--status", default=None, help="filter by view status")
    p_list.set_defaults(func=cmd_agents, agents_action="list")
    p_explain = agents_sub.add_parser(
        "explain",
        parents=[common],
        help="explain one agent/profile policy (read-only; no probe)",
    )
    p_explain.add_argument("agent_or_profile", help="agent id, alias, or builtin profile")
    p_explain.add_argument(
        "--model",
        default=None,
        help="per-run exact model override for this explain only (not persisted)",
    )
    p_explain.set_defaults(func=cmd_agents, agents_action="explain")
    p_agents.set_defaults(func=cmd_agents)


__all__ = ["cmd_agents", "register_agents_parsers"]
