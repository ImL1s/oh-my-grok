"""omg provider — Antigravity (and future) provider probe CLI (#67-A).

Commands: ``omg provider antigravity capabilities|doctor``.
Parser construction: ``register_provider_parsers``.

Handlers route through :class:`~omg_cli.providers.base.ProviderAdapter`
implementations — never provider-module free functions alone.
"""

from __future__ import annotations

import argparse
import sys

from omg_cli.cli_envelope import emit_data, emit_json, failure, success, wants_json
from omg_cli.providers.base import ProviderAdapter


def _resolve_adapter(name: str) -> ProviderAdapter:
    if name == "antigravity":
        from omg_cli.providers.antigravity import AntigravityProvider

        return AntigravityProvider()
    raise KeyError(name)


def cmd_provider(args: argparse.Namespace) -> int:
    """Dispatch ``provider <name> <action>``."""
    name = getattr(args, "provider_name", None)
    action = getattr(args, "provider_action", None)
    if name != "antigravity":
        print(
            f"omg provider: unknown provider {name!r}; expected: antigravity",
            file=sys.stderr,
        )
        return 2
    if action == "capabilities":
        return _cmd_provider_capabilities(args, name)
    if action == "doctor":
        return _cmd_provider_doctor(args, name)
    print(
        "usage: omg provider antigravity {capabilities,doctor}",
        file=sys.stderr,
    )
    return 2


def _cmd_provider_capabilities(args: argparse.Namespace, name: str) -> int:
    from omg_cli.providers.errors import (
        ProviderBinaryMissing,
        ProviderProbeError,
        ProviderVersionError,
    )

    try:
        adapter = _resolve_adapter(name)
        caps = adapter.probe_capabilities()
    except ProviderBinaryMissing as exc:
        emit_json(
            failure(
                f"provider.{name}.capabilities",
                "E_PROVIDER_MISSING",
                str(exc),
                next_action="Install agy or set OMG_AGY_BIN",
            )
        )
        return 1
    except (ProviderVersionError, ProviderProbeError) as exc:
        emit_json(
            failure(
                f"provider.{name}.capabilities",
                "E_PROVIDER_PROBE",
                str(exc),
            )
        )
        return 1

    payload = caps.to_dict()
    cmd = f"provider.{name}.capabilities"
    if wants_json(args):
        emit_json(success(cmd, capabilities=payload))
    else:
        emit_data(args, cmd, payload)
    return 0


def _cmd_provider_doctor(args: argparse.Namespace, name: str) -> int:
    adapter = _resolve_adapter(name)
    strict = bool(getattr(args, "strict", False))
    report = adapter.doctor(strict=strict)
    payload = report.to_dict()
    cmd = f"provider.{name}.doctor"
    if wants_json(args):
        emit_json(success(cmd, **payload))
    else:
        for line in report.checks:
            print(line)
        if report.capabilities is not None and not wants_json(args):
            print(
                f"compat={report.capabilities.compat_status} "
                f"version={report.capabilities.version} "
                f"live_call_ready={report.capabilities.live_call_ready}"
            )
    return int(report.exit_code)


def register_provider_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    """Register ``provider`` inspect-family parsers (#67-A)."""
    p_provider = sub.add_parser(
        "provider",
        parents=[common],
        help="provider probe (Antigravity capabilities/doctor; #67-A)",
    )
    prov_sub = p_provider.add_subparsers(dest="provider_name")
    p_agy = prov_sub.add_parser(
        "antigravity",
        parents=[common],
        help="Antigravity (agy) provider probe",
    )
    agy_sub = p_agy.add_subparsers(dest="provider_action")

    p_caps = agy_sub.add_parser(
        "capabilities",
        parents=[common],
        help="emit schema-versioned capabilities JSON envelope",
    )
    p_caps.set_defaults(func=cmd_provider, provider_action="capabilities")

    p_doc = agy_sub.add_parser(
        "doctor",
        parents=[common],
        help="classify install/compat readiness (use --strict to fail closed)",
    )
    p_doc.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero on missing binary or incompatible version",
    )
    p_doc.set_defaults(func=cmd_provider, provider_action="doctor")

    p_provider.set_defaults(func=cmd_provider)


__all__ = [
    "cmd_provider",
    "register_provider_parsers",
]
