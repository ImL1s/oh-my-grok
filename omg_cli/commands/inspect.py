"""Inspect-family CLI handlers (#29 Phase 2).

Commands: wiki, hud, lsp, notify, native-status, capabilities, parity.
Parser construction remains in ``main.build_parser`` until a later phase.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from omg_cli.cli_util import notification_config, project_root, read_json_path


def cmd_wiki(args: argparse.Namespace) -> int:
    from omg_cli.wiki import WikiError, ingest, list_pages, query

    root = project_root()
    action = getattr(args, "wiki_action", None)
    try:
        if action == "ingest":
            tags = []
            raw_tags = getattr(args, "tags", None)
            if raw_tags:
                tags = [t.strip() for t in str(raw_tags).split(",") if t.strip()]
            body = getattr(args, "text", None) or ""
            if getattr(args, "file", None):
                body = Path(args.file).read_text(encoding="utf-8")
            result = ingest(
                root,
                title=str(args.title),
                body=body,
                tags=tags,
                source=getattr(args, "source", None),
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if action == "list":
            print(json.dumps(list_pages(root), indent=2, ensure_ascii=False))
            return 0
        if action == "query":
            hits = query(root, str(args.q), limit=int(getattr(args, "limit", 20)))
            print(json.dumps(hits, indent=2, ensure_ascii=False))
            return 0
    except WikiError as e:
        print(f"wiki failed: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"wiki failed: {e}", file=sys.stderr)
        return 1
    print("usage: omg wiki {ingest,list,query} …", file=sys.stderr)
    return 2


def cmd_hud(args: argparse.Namespace) -> int:
    from omg_cli.hud import hud_line, hud_pack

    root = project_root()
    rid = getattr(args, "run_id", None)
    if getattr(args, "json", False):
        print(json.dumps(hud_pack(root, rid), indent=2, ensure_ascii=False))
    else:
        print(hud_line(root, rid))
    return 0


def cmd_lsp(args: argparse.Namespace) -> int:
    """LSP registration inspection only (#28) — no semantic proxy."""
    from omg_cli.lsp_tools import (
        LSPRegistrationError,
        probe_tools,
        registration_status,
        validate_registration,
    )

    action = getattr(args, "lsp_action", None)
    if action == "status" or action is None:
        print(json.dumps(probe_tools(), indent=2, ensure_ascii=False))
        return 0
    if action == "validate":
        root = project_root()
        path = root / ".lsp.json"
        try:
            if not path.is_file():
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "schema_version": 1,
                            "command": "lsp.validate",
                            "error": "E_LSP_MISSING",
                            "path": str(path),
                            "message": ".lsp.json not found",
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                return 1
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise LSPRegistrationError(".lsp.json must be a JSON object")
            servers = validate_registration(raw)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "schema_version": 1,
                        "command": "lsp.validate",
                        "path": str(path),
                        "servers": sorted(servers.keys()),
                        "status": registration_status(root),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        except (OSError, json.JSONDecodeError, LSPRegistrationError) as exc:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "schema_version": 1,
                        "command": "lsp.validate",
                        "error": "E_LSP_INVALID",
                        "path": str(path),
                        "message": str(exc),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 1
    if action in {"check", "symbols", "diagnostics"}:
        status = probe_tools()
        result = {
            "ok": False,
            "schema_version": 1,
            "command": f"lsp.{action}",
            "error": "E_LSP_HOST_OWNED",
            "ownership": "host_owned",
            "status": "semantic_proxy_unsupported",
            "operation": action,
            "path": str(Path(getattr(args, "path", "") or ".")),
            "semantic_proxy_operations": status.get("semantic_proxy_operations") or [],
            "message": (
                "semantic LSP operations belong to the Grok host; OMG only "
                "validates .lsp.json registration (use: omg lsp status|validate)"
            ),
            "next_action": "Use the host IDE/Grok LSP client for symbols/diagnostics",
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1
    print(
        "usage: omg lsp {status,validate} "
        "[legacy: check|symbols|diagnostics → E_LSP_HOST_OWNED]\n"
        "OMG does not proxy semantic LSP; registration inspection only (#28).",
        file=sys.stderr,
    )
    return 2


def cmd_notify(args: argparse.Namespace) -> int:
    """Operate the outbound-only, non-authoritative notification queue."""
    from omg_cli.notify import (
        create_notification_event,
        enqueue_notification,
        process_notification_queue,
    )

    action = getattr(args, "notify_action", None)
    try:
        if action == "status":
            result: object = {
                "config": notification_config(getattr(args, "config", None)),
                "inbound_listener": False,
                "authoritative": False,
            }
        else:
            nonce = os.environ.get("OMG_NOTIFICATION_OWNER_NONCE", "")
            if not nonce:
                raise ValueError("OMG_NOTIFICATION_OWNER_NONCE is required")
            owner = {
                "owner_id": args.owner_id,
                "generation": args.generation,
                "owner_nonce": nonce,
            }
            if action == "send":
                event = create_notification_event(
                    severity=args.severity,
                    title=args.title,
                    message=args.message,
                    owner_id=args.owner_id,
                    generation=args.generation,
                    owner_nonce=nonce,
                    stable_source_id=getattr(args, "stable_source_id", None),
                )
                result = enqueue_notification(
                    project_root(),
                    event,
                    owner=owner,
                    max_attempts=args.max_attempts,
                )
            elif action == "process":
                result = process_notification_queue(
                    project_root(),
                    notification_config(getattr(args, "config", None)),
                    owner=owner,
                    max_records=args.max_records,
                    rate_limit_per_second=args.rate_limit,
                )
            else:
                print("omg notify: action required", file=sys.stderr)
                return 2
    except (OSError, ValueError) as exc:
        print(f"omg notify: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_native_status(args: argparse.Namespace) -> int:
    """Report only public native UI/workflow observations."""
    from omg_cli.sidecar import native_dashboard_status
    from omg_cli.workflows.grok_adapter import (
        assess_native_capability,
        safe_headless_probe,
    )

    result = {
        "native_dashboard": native_dashboard_status(),
        "native_workflow": assess_native_capability(project_root()),
        "headless_probe": safe_headless_probe(
            timeout_seconds=float(getattr(args, "timeout", 5.0))
        )
        if bool(getattr(args, "probe", False))
        else {
            "attempted": False,
            "status": "optional_unclaimed",
            "note": "pass --probe for bounded help-only observation",
        },
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    """Report independent capability tiers without inferring host health."""
    import importlib.util

    from omg_cli import __version__
    from omg_cli.contracts.capability_schema import CAPABILITY_TIERS
    from omg_cli.lsp_tools import registration_status
    from omg_cli.sidecar import native_dashboard_status
    from omg_cli.workflows.grok_adapter import assess_native_capability

    root = project_root()
    lock_path = root / "omg_capabilities.lock.json"
    try:
        lock = (
            read_json_path(lock_path, label="capability lock")
            if lock_path.is_file()
            else None
        )
        lsp = registration_status(root)
        workflow = assess_native_capability(root)
        notification = notification_config(
            getattr(args, "notification_config", None)
        )
    except (OSError, ValueError) as exc:
        print(f"omg capabilities: {exc}", file=sys.stderr)
        return 1
    mcp_installed = importlib.util.find_spec("omg_cli.mcp.server") is not None
    workflow_installed = importlib.util.find_spec("omg_cli.workflows.runner") is not None
    result = {
        "schema": "omg-capability-status/v1",
        "tiers": list(CAPABILITY_TIERS),
        "version": __version__,
        "surfaces": {
            "mcp": {
                "configured": (root / ".mcp.json").is_file(),
                "installed": mcp_installed,
                "enabled": False,
                "loadable": mcp_installed,
                "observed": False,
                "healthy": False,
                "verified": False,
                "classification": "native_substitute",
                "note": "fresh Grok session evidence is required above configured/loadable",
            },
            "lsp": {
                "configured": lsp["registered"],
                "installed": any(row["command_available"] for row in lsp["servers"]),
                "enabled": False,
                "loadable": lsp["configuration_valid"],
                "observed": lsp["host_observed"],
                "healthy": lsp["healthy"],
                "verified": lsp["healthy"],
                "classification": "host_owned",
                "status": lsp["status"],
            },
            "repository_workflow": {
                "configured": True,
                "installed": workflow_installed,
                "enabled": True,
                "loadable": workflow_installed,
                "observed": False,
                "healthy": False,
                "verified": False,
                "classification": "native_substitute",
                "scope": "product-owned runner only",
            },
            "grok_native_workflow": {
                "configured": workflow["local_bundle_observed"],
                "installed": workflow["local_bundle_observed"],
                "enabled": False,
                "loadable": False,
                "observed": workflow["fresh_invocation_observed"],
                "healthy": workflow["semantic_claim"],
                "verified": workflow["semantic_claim"],
                "classification": "optional_unclaimed",
                "status": workflow["status"],
            },
            "notifications": {
                "configured": notification["enabled"],
                "installed": True,
                "enabled": notification["enabled"],
                "loadable": True,
                "observed": False,
                "healthy": False,
                "verified": False,
                "authoritative": False,
                "classification": "native_substitute",
            },
            "native_dashboard": {
                "configured": False,
                "installed": False,
                "enabled": False,
                "loadable": False,
                "observed": False,
                "healthy": False,
                "verified": False,
                "classification": "optional_unclaimed",
                "status": native_dashboard_status(),
            },
        },
        "lock": lock,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_parity(args: argparse.Namespace) -> int:
    """Delegate run-manifest operations and release-bundle readback."""
    from omg_cli.contracts.release_transaction import verify_release_bundle_files
    from omg_cli.contracts.run_manifest import main as run_manifest_main
    from omg_cli.contracts.state_schemas import ContractValidationError
    from omg_cli.contracts.writer_chain import sha256_hex

    action = getattr(args, "parity_action", None)
    if action == "run":
        return int(run_manifest_main(list(getattr(args, "manifest_args", None) or [])))
    if action != "release-readback":
        print("omg parity: action required", file=sys.stderr)
        return 2
    root = project_root()
    try:
        manifest_path = Path(args.manifest).resolve()
        relative = manifest_path.relative_to(root).as_posix()
        manifest = read_json_path(manifest_path, label="release bundle manifest")
        if not isinstance(manifest, dict):
            raise ValueError("release bundle manifest must be a JSON object")
        registries: object = []
        if getattr(args, "claimed_registries", None):
            registries = read_json_path(
                args.claimed_registries, label="claimed registries"
            )
            if isinstance(registries, dict):
                registries = registries.get("claimed_registries")
        if not isinstance(registries, list) or not all(
            isinstance(row, dict) for row in registries
        ):
            raise ValueError("claimed registries must be a JSON array")
        verified = verify_release_bundle_files(
            root,
            manifest,
            manifest_relative_path=relative,
            claimed_registries=registries,
        )
        result = {
            "verified": True,
            "manifest_path": relative,
            "manifest_sha256": sha256_hex(manifest_path.read_bytes()),
            "candidate_commit": verified["candidate_commit"],
            "candidate_tree": verified["candidate_tree"],
            "semver": verified["semver"],
            "public_upload_order": verified["public_upload_order"],
            "release_asset_root": verified["release_asset_root"],
        }
    except (OSError, ValueError, ContractValidationError) as exc:
        print(f"omg parity: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


__all__ = [
    "cmd_capabilities",
    "cmd_hud",
    "cmd_lsp",
    "cmd_native_status",
    "cmd_notify",
    "cmd_parity",
    "cmd_wiki",
]
