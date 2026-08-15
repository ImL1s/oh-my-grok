"""Agent/model policy CLI (#131/#134).

``omg agents list|explain`` is a read-only inspect surface. Human layouts
follow ``--width`` / ``COLUMNS`` (narrow/normal/wide) and never require color.
JSON is the typed ``AgentPolicyViewV1``. No provider probe, paid inference, or
``verified`` write. Medley catalog facts stay unsupported/unavailable on stock
Grok Build.
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
    from omg_cli.medley_inspect import MedleyInspectError, resolve_host_snapshot

    try:
        host, inspect_doc = resolve_host_snapshot(
            cli_path=getattr(args, "host_inspect", None),
        )
        rows = list_agent_policies(
            root=_plugin_root(),
            project_root=project_root(),
            user_home=_user_home(),
            host=host,
            inspect_doc=inspect_doc,
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
    except MedleyInspectError as exc:
        return _fail(args, "agents.list", exc)
    payload = {
        "host_tier": host.host_tier,
        "agents": [row.to_json() for row in rows],
    }
    if wants_json(args):
        emit_json(success("agents.list", data=payload))
        return 0
    from omg_cli.agent_policy_ux import render_list_human, terminal_width

    columns = terminal_width(override=getattr(args, "width", None))
    print(render_list_human(rows, columns=columns))
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    from omg_cli.agent_policy import AgentPolicyError, resolve_agent_policy, resume_pin
    from omg_cli.medley_inspect import MedleyInspectError, resolve_host_snapshot

    name = str(getattr(args, "agent_or_profile", "") or "")
    per_run = None
    model = getattr(args, "model", None)
    if model:
        per_run = {"model": str(model)}
    try:
        host, inspect_doc = resolve_host_snapshot(
            cli_path=getattr(args, "host_inspect", None),
        )
        view = resolve_agent_policy(
            name,
            root=_plugin_root(),
            project_root=project_root(),
            user_home=_user_home(),
            per_run=per_run,
            host=host,
            inspect_doc=inspect_doc,
        )
    except AgentPolicyError as exc:
        return _fail(args, "agents.explain", exc)
    except MedleyInspectError as exc:
        return _fail(args, "agents.explain", exc)
    payload = {"agent": view.to_json(), "resume": resume_pin(view)}
    if wants_json(args):
        emit_json(success("agents.explain", data=payload))
        return 0
    from omg_cli.agent_policy_ux import render_explain_human, terminal_width

    columns = terminal_width(override=getattr(args, "width", None))
    print(render_explain_human(view, columns=columns))
    return 0


def _fail(args: argparse.Namespace, command: str, exc: Exception) -> int:
    code = str(getattr(exc, "code", "E_AGENT_POLICY") or "E_AGENT_POLICY")
    message = str(exc)
    if wants_json(args):
        emit_json(failure(command, code, message))
    else:
        print(f"omg {command.replace('.', ' ')}: {code}: {message}", file=sys.stderr)
    return 2 if code == "E_AGENT_NOT_FOUND" else 1


def register_agents_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    p_agents = sub.add_parser(
        "agents",
        parents=[common],
        help=(
            "dual-host agent/model policy list/explain "
            "(Grok baseline; Medley inspect via --host-inspect / OMG_MEDLEY_INSPECT)"
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
    p_list.add_argument(
        "--host-inspect",
        dest="host_inspect",
        default=None,
        help=(
            "explicit Medley inspect JSON (medley.native-subagent-route.inspect/v1); "
            "never inferred from PATH. Env: OMG_MEDLEY_INSPECT"
        ),
    )
    p_list.add_argument(
        "--width",
        type=int,
        default=None,
        help="human layout columns (default: terminal / COLUMNS); JSON ignores this",
    )
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
    p_explain.add_argument(
        "--host-inspect",
        dest="host_inspect",
        default=None,
        help=(
            "explicit Medley inspect JSON (medley.native-subagent-route.inspect/v1); "
            "never inferred from PATH. Env: OMG_MEDLEY_INSPECT"
        ),
    )
    p_explain.add_argument(
        "--width",
        type=int,
        default=None,
        help="human layout columns (default: terminal / COLUMNS); JSON ignores this",
    )
    p_explain.set_defaults(func=cmd_agents, agents_action="explain")
    p_agents.set_defaults(func=cmd_agents)


__all__ = ["cmd_agents", "register_agents_parsers"]
