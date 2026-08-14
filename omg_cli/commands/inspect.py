"""Inspect-family CLI handlers (#29 Phase 2).

Commands: wiki, hud, lsp, notify, native-status, capabilities, parity.
Parser construction: ``register_inspect_parsers`` (#29 Phase 4').
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from omg_cli.cli_envelope import emit_data, emit_json, wants_json
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
            emit_data(args, "wiki.ingest", result)
            return 0
        if action == "list":
            emit_data(args, "wiki.list", list_pages(root))
            return 0
        if action == "query":
            hits = query(root, str(args.q), limit=int(getattr(args, "limit", 20)))
            emit_data(args, "wiki.query", hits)
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
    if wants_json(args) or getattr(args, "json", False):
        emit_data(args, "hud", hud_pack(root, rid))
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
        emit_data(args, "lsp.status", probe_tools())
        return 0
    if action == "validate":
        root = project_root()
        path = root / ".lsp.json"
        try:
            if not path.is_file():
                emit_json(
                    {
                        "ok": False,
                        "schema_version": 1,
                        "command": "lsp.validate",
                        "error": "E_LSP_MISSING",
                        "path": str(path),
                        "message": ".lsp.json not found",
                    }
                )
                return 1
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise LSPRegistrationError(".lsp.json must be a JSON object")
            servers = validate_registration(raw)
            emit_json(
                {
                    "ok": True,
                    "schema_version": 1,
                    "command": "lsp.validate",
                    "path": str(path),
                    "servers": sorted(servers.keys()),
                    "status": registration_status(root),
                }
            )
            return 0
        except (OSError, json.JSONDecodeError, LSPRegistrationError) as exc:
            emit_json(
                {
                    "ok": False,
                    "schema_version": 1,
                    "command": "lsp.validate",
                    "error": "E_LSP_INVALID",
                    "path": str(path),
                    "message": str(exc),
                }
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
        emit_json(result)
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
    emit_data(args, "notify", result)
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
    emit_data(args, "native-status", result)
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
    emit_data(args, "capabilities", result)
    return 0


def _cmd_parity_release_bundle(args: argparse.Namespace) -> int:
    from omg_cli import __version__
    from omg_cli.contracts.state_schemas import ContractValidationError
    from omg_cli.release_bundle import (
        ReleaseBundleError,
        produce_release_bundle_from_files,
    )

    root = Path(getattr(args, "root", None) or project_root())
    receipt = None
    if getattr(args, "receipt", None):
        loaded = read_json_path(args.receipt, label="build receipt")
        if not isinstance(loaded, dict):
            print("omg parity: build receipt must be a JSON object", file=sys.stderr)
            return 2
        receipt = loaded
    try:
        result = produce_release_bundle_from_files(
            root,
            run_id=str(args.run_id),
            candidate_commit=str(args.candidate_commit),
            candidate_tree=str(args.candidate_tree),
            semver=str(getattr(args, "semver", None) or __version__),
            archive=Path(args.archive),
            checksums=Path(args.checksums),
            build_receipt=receipt,
            live_receipt=bool(getattr(args, "live_receipt", False)),
            write=bool(getattr(args, "write_layout", False)),
        )
    except (OSError, ValueError, ContractValidationError, ReleaseBundleError) as exc:
        print(f"omg parity: {exc}", file=sys.stderr)
        return 1
    public = {
        "ok": True,
        "manifest_relative_path": result["manifest_relative_path"],
        "manifest_sha256": result["manifest_sha256"],
        "semver": result["manifest"]["semver"],
        "candidate_commit": result["manifest"]["candidate_commit"],
        "release_asset_root": result["manifest"]["release_asset_root"],
        "public_upload_order": result["manifest"]["public_upload_order"],
    }
    if "manifest_path" in result:
        public["manifest_path"] = result["manifest_path"]
    emit_data(args, "parity.release-bundle", public)
    return 0


def _cmd_parity_release_evidence(args: argparse.Namespace) -> int:
    from omg_cli.contracts.writer_chain import canonical_json_bytes
    from omg_cli.release_evidence import (
        ReleaseEvidenceError,
        produce_release_evidence_from_facts,
    )

    payload = read_json_path(args.facts, label="release evidence facts")
    if not isinstance(payload, dict):
        print("omg parity: facts must be a JSON object", file=sys.stderr)
        return 2
    try:
        evidence = produce_release_evidence_from_facts(payload)
        body = canonical_json_bytes(evidence)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(body)
        os.chmod(output, 0o600)
    except (OSError, ValueError, ReleaseEvidenceError) as exc:
        print(f"omg parity: {exc}", file=sys.stderr)
        return 1
    emit_data(
        args,
        "parity.release-evidence",
        {
            "ok": True,
            "output": str(output),
            "run_id": evidence["run_id"],
            "final_state": evidence["final_state"],
            "transaction_identity_hash": evidence["transaction_identity_hash"],
        },
    )
    return 0


def cmd_parity(args: argparse.Namespace) -> int:
    """Run-manifest, release-readback, inventory check, and gap listing."""
    action = getattr(args, "parity_action", None)
    if action == "release-bundle":
        return _cmd_parity_release_bundle(args)
    if action == "release-evidence":
        return _cmd_parity_release_evidence(args)

    from omg_cli.contracts.parity_schema import (
        load_json_object,
        validate_parity_inventory,
    )
    from omg_cli.contracts.release_transaction import verify_release_bundle_files
    from omg_cli.contracts.run_manifest import main as run_manifest_main
    from omg_cli.contracts.state_schemas import ContractValidationError
    from omg_cli.contracts.writer_chain import sha256_hex
    from omg_cli.parity_check import check_parity_inventory, filter_parity_gaps
    from omg_cli.setup_cmd import plugin_root

    if action == "run":
        return int(run_manifest_main(list(getattr(args, "manifest_args", None) or [])))

    if action == "check":
        root = plugin_root()
        inventory_path = root / "docs" / "parity" / "omg-parity.json"
        strict = bool(getattr(args, "strict", False))
        release = bool(getattr(args, "release", False))
        base_inventory = getattr(args, "base_inventory", None)
        base_ref = getattr(args, "base_ref", None)
        try:
            result = check_parity_inventory(
                inventory_path=inventory_path,
                repo_root=root,
                strict=strict,
                release=release,
                base_inventory_path=Path(base_inventory) if base_inventory else None,
                base_ref=base_ref,
            )
        except ContractValidationError as exc:
            emit_data(
                args,
                "parity.check",
                {
                    "ok": False,
                    "error": str(exc),
                    "strict": strict or release,
                    "release": release,
                },
            )
            return 1
        emit_data(args, "parity.check", result)
        return 0

    if action == "gaps":
        root = plugin_root()
        inventory_path = root / "docs" / "parity" / "omg-parity.json"
        try:
            inventory = validate_parity_inventory(
                load_json_object(inventory_path),
                repo_root=root,
            )
        except ContractValidationError as exc:
            emit_data(
                args,
                "parity.gaps",
                {"ok": False, "error": str(exc)},
            )
            return 1
        priority = getattr(args, "priority", None)
        include_all = bool(getattr(args, "all_gaps", False))
        gaps = filter_parity_gaps(
            inventory,
            priority=priority,
            include_all=include_all,
        )
        result = {
            "ok": True,
            "priority": priority,
            "include_all": include_all,
            "open_only": not include_all,
            "count": len(gaps),
            "gaps": gaps,
            "inventory_status": inventory.get("inventory_status"),
        }
        emit_data(args, "parity.gaps", result)
        return 0

    if action == "refresh":
        from omg_cli.parity_refresh import build_refresh_plan, write_refresh_review_artifact

        if not getattr(args, "plan", False):
            emit_data(
                args,
                "parity.refresh",
                {
                    "ok": False,
                    "error": "--plan is required (plan-only; no inventory mutation)",
                },
            )
            return 1
        catalog_arg = getattr(args, "catalog", None)
        if not catalog_arg:
            emit_data(
                args,
                "parity.refresh",
                {"ok": False, "error": "--catalog is required"},
            )
            return 1
        root = plugin_root()
        proj = project_root()
        inventory_path = root / "docs" / "parity" / "omg-parity.json"
        try:
            inventory = validate_parity_inventory(
                load_json_object(inventory_path),
                repo_root=root,
            )
            upstream_catalog = load_json_object(Path(catalog_arg))
            plan = build_refresh_plan(
                inventory=inventory,
                upstream_catalog=upstream_catalog,
                source=str(args.source),
                new_pin=str(args.pin),
            )
            artifact = write_refresh_review_artifact(proj, plan)
            result = {
                "ok": True,
                "artifact_path": artifact.relative_to(proj).as_posix(),
                "source": plan["source"],
                "from_revision": plan["from_revision"],
                "to_revision": plan["to_revision"],
                "change_count": len(plan["changes"]),
                "guards": plan["guards"],
            }
        except (OSError, ValueError, ContractValidationError) as exc:
            emit_data(
                args,
                "parity.refresh",
                {"ok": False, "error": str(exc)},
            )
            return 1
        emit_data(args, "parity.refresh", result)
        return 0

    if action != "release-readback":
        print(
            "omg parity: action required "
            "(run|release-readback|release-bundle|release-evidence|check|gaps|refresh)",
            file=sys.stderr,
        )
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
    emit_data(args, "parity.release-readback", result)
    return 0



def register_inspect_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
    *,
    phase: str = "all",
) -> None:
    """Register inspect-family argparse parsers (#29 Phase 4').

    ``phase``:
      - ``early``: notify, native-status (before workflow in help order)
      - ``late``: capabilities, parity, wiki, hud, lsp
      - ``all``: both (tests / future single call)
    """
    if phase not in {"early", "late", "all"}:
        raise ValueError(f"unknown inspect register phase: {phase!r}")
    if phase in ("early", "all"):
        p_notify = sub.add_parser(
            "notify",
            parents=[common],
            help="outbound-only non-authoritative notification queue",
        )
        notify_sub = p_notify.add_subparsers(dest="notify_action")
        p_notify_status = notify_sub.add_parser(
            "status", parents=[common], help="show validated adapter configuration"
        )
        p_notify_status.add_argument("--config", default=None)
        p_notify_status.set_defaults(func=cmd_notify, notify_action="status")
        p_notify_send = notify_sub.add_parser(
            "send", parents=[common], help="enqueue one bounded notification"
        )
        p_notify_send.add_argument("--owner", dest="owner_id", required=True)
        p_notify_send.add_argument("--generation", type=int, required=True)
        p_notify_send.add_argument(
            "--severity", choices=("info", "success", "warning", "error"), default="info"
        )
        p_notify_send.add_argument("--title", required=True)
        p_notify_send.add_argument("--message", required=True)
        p_notify_send.add_argument("--stable-source-id", default=None)
        p_notify_send.add_argument("--max-attempts", type=int, default=3)
        p_notify_send.set_defaults(func=cmd_notify, notify_action="send")
        p_notify_process = notify_sub.add_parser(
            "process", parents=[common], help="deliver a bounded queue batch"
        )
        p_notify_process.add_argument("--owner", dest="owner_id", required=True)
        p_notify_process.add_argument("--generation", type=int, required=True)
        p_notify_process.add_argument("--config", default=None)
        p_notify_process.add_argument("--max-records", type=int, default=32)
        p_notify_process.add_argument("--rate-limit", type=float, default=10.0)
        p_notify_process.set_defaults(func=cmd_notify, notify_action="process")
        p_notify.set_defaults(func=cmd_notify)

        p_native_status = sub.add_parser(
            "native-status",
            parents=[common],
            help="honest public Grok dashboard/workflow observation tiers",
        )
        p_native_status.add_argument(
            "--probe",
            action="store_true",
            help="run bounded grok --help observation (never invoke slash commands)",
        )
        p_native_status.add_argument("--timeout", type=float, default=5.0)
        p_native_status.set_defaults(func=cmd_native_status)
    if phase in ("late", "all"):
        p_capabilities = sub.add_parser(
            "capabilities",
            parents=[common],
            help="independent configured→verified capability tiers",
        )
        p_capabilities.add_argument("--notification-config", default=None)
        p_capabilities.set_defaults(func=cmd_capabilities)

        p_parity = sub.add_parser(
            "parity",
            parents=[common],
            help="parity inventory check/gaps plus frozen run-manifest verification",
        )
        parity_sub = p_parity.add_subparsers(dest="parity_action")
        p_parity_run = parity_sub.add_parser(
            "run",
            parents=[common],
            help="delegate the exact W0 run-manifest engine",
        )
        p_parity_run.add_argument(
            "manifest_args",
            nargs=argparse.REMAINDER,
            help="run-manifest action and arguments",
        )
        p_parity_run.set_defaults(func=cmd_parity, parity_action="run")
        p_parity_readback = parity_sub.add_parser(
            "release-readback",
            parents=[common],
            help="verify the exact prebuilt release-bundle file set",
        )
        p_parity_readback.add_argument("--manifest", required=True)
        p_parity_readback.add_argument("--claimed-registries", default=None)
        p_parity_readback.set_defaults(func=cmd_parity, parity_action="release-readback")
        p_parity_bundle = parity_sub.add_parser(
            "release-bundle",
            parents=[common],
            help="construct the canonical release-bundle-manifest (#169)",
        )
        p_parity_bundle.add_argument("--run-id", required=True)
        p_parity_bundle.add_argument("--archive", required=True)
        p_parity_bundle.add_argument("--checksums", required=True)
        p_parity_bundle.add_argument("--candidate-commit", required=True)
        p_parity_bundle.add_argument("--candidate-tree", required=True)
        p_parity_bundle.add_argument("--semver", default=None)
        p_parity_bundle.add_argument("--receipt", default=None)
        p_parity_bundle.add_argument("--live-receipt", action="store_true")
        p_parity_bundle.add_argument(
            "--write",
            dest="write_layout",
            action="store_true",
            help="write canonical .omg/artifacts/... layout",
        )
        p_parity_bundle.add_argument("--root", default=None)
        p_parity_bundle.set_defaults(func=cmd_parity, parity_action="release-bundle")
        p_parity_evidence = parity_sub.add_parser(
            "release-evidence",
            parents=[common],
            help="construct release-evidence-input.json from observed facts (#169)",
        )
        p_parity_evidence.add_argument("--facts", required=True)
        p_parity_evidence.add_argument("--output", required=True)
        p_parity_evidence.set_defaults(func=cmd_parity, parity_action="release-evidence")
        p_parity_check = parity_sub.add_parser(
            "check",
            parents=[common],
            help="validate canonical parity inventory (schema + optional --strict paths)",
        )
        p_parity_check.add_argument(
            "--strict",
            action="store_true",
            help="fail closed on schema/path/overclaim drift",
        )
        p_parity_check.add_argument(
            "--release",
            action="store_true",
            help="fail closed on upstream drift, stale live evidence, and docs overclaim",
        )
        p_parity_check.add_argument(
            "--base-inventory",
            default=None,
            help=(
                "previous parity inventory JSON; for --release must be paired with "
                "--base-ref whose inventory blob matches the file "
                "(file-only base is insufficient)"
            ),
        )
        p_parity_check.add_argument(
            "--base-ref",
            default=None,
            help=(
                "durable git ref for base inventory (or set OMG_PARITY_BASE_REF); "
                "required for --release pin-transition DAG walk; "
                "release mode prefers previous v* tag over HEAD^"
            ),
        )
        p_parity_check.set_defaults(func=cmd_parity, parity_action="check")
        p_parity_gaps = parity_sub.add_parser(
            "gaps",
            parents=[common],
            help="list open parity gaps from the canonical inventory",
        )
        p_parity_gaps.add_argument(
            "--priority",
            default=None,
            help="filter by priority (e.g. P0); still open-only unless --all",
        )
        p_parity_gaps.add_argument(
            "--all",
            dest="all_gaps",
            action="store_true",
            help="include non-open gaps (closed/deferred)",
        )
        p_parity_gaps.set_defaults(func=cmd_parity, parity_action="gaps")
        from omg_cli.contracts.parity_schema import SOURCE_STATUS_IDS

        p_parity_refresh = parity_sub.add_parser(
            "refresh",
            parents=[common],
            help="plan-only upstream pin refresh (writes review artifact; never upgrades maturity)",
        )
        p_parity_refresh.add_argument(
            "--source",
            required=True,
            choices=list(SOURCE_STATUS_IDS),
        )
        p_parity_refresh.add_argument("--pin", required=True, help="full git commit oid")
        p_parity_refresh.add_argument(
            "--plan",
            action="store_true",
            help="required: emit review artifact only (no inventory mutation)",
        )
        p_parity_refresh.add_argument(
            "--catalog",
            default=None,
            help="path to upstream catalog fixture JSON",
        )
        p_parity_refresh.set_defaults(func=cmd_parity, parity_action="refresh")
        p_parity.set_defaults(func=cmd_parity)

        p_wiki = sub.add_parser(
            "wiki",
            parents=[common],
            help="local markdown wiki under .omg/wiki",
        )
        wiki_sub = p_wiki.add_subparsers(dest="wiki_action")
        p_w_ing = wiki_sub.add_parser("ingest", parents=[common], help="append/create page")
        p_w_ing.add_argument("--title", required=True)
        p_w_ing.add_argument("--text", default=None, help="page body text")
        p_w_ing.add_argument("--file", default=None, help="read body from file")
        p_w_ing.add_argument("--tags", default=None, help="comma-separated tags")
        p_w_ing.add_argument("--source", default=None, help="optional source note")
        p_w_ing.set_defaults(func=cmd_wiki)
        p_w_list = wiki_sub.add_parser("list", parents=[common], help="list wiki pages")
        p_w_list.set_defaults(func=cmd_wiki)
        p_w_q = wiki_sub.add_parser("query", parents=[common], help="keyword search")
        p_w_q.add_argument("q", help="search string")
        p_w_q.add_argument("--limit", type=int, default=20)
        p_w_q.set_defaults(func=cmd_wiki)
        p_wiki.set_defaults(func=cmd_wiki)

        p_hud = sub.add_parser(
            "hud",
            parents=[common],
            help="one-line HUD for active (or --run) status",
        )
        p_hud.add_argument("--run", dest="run_id", default=None)
        # --json inherited from common (json_output)
        p_hud.set_defaults(func=cmd_hud)

        p_lsp = sub.add_parser(
            "lsp",
            parents=[common],
            help=(
                "inspect host-owned .lsp.json registration only "
                "(no semantic proxy; #28)"
            ),
            description=(
                "Inspect host-owned .lsp.json registration only. "
                "OMG has no semantic proxy; use status|validate. "
                "Legacy check|symbols|diagnostics always return E_LSP_HOST_OWNED."
            ),
        )
        lsp_sub = p_lsp.add_subparsers(dest="lsp_action")
        p_lsp_st = lsp_sub.add_parser(
            "status",
            parents=[common],
            help="inspect registration and command availability (primary)",
        )
        p_lsp_st.set_defaults(func=cmd_lsp)
        p_lsp_val = lsp_sub.add_parser(
            "validate",
            parents=[common],
            help="validate .lsp.json shape and report precise field errors (primary)",
        )
        p_lsp_val.set_defaults(func=cmd_lsp)
        p_lsp_ck = lsp_sub.add_parser(
            "check",
            parents=[common],
            help="LEGACY: always E_LSP_HOST_OWNED (use host IDE/Grok LSP)",
        )
        p_lsp_ck.add_argument("path", help="file path")
        p_lsp_ck.set_defaults(func=cmd_lsp)
        p_lsp_sym = lsp_sub.add_parser(
            "symbols",
            parents=[common],
            help="LEGACY: always E_LSP_HOST_OWNED (use host IDE/Grok LSP)",
        )
        p_lsp_sym.add_argument("path", help="Python file path")
        p_lsp_sym.set_defaults(func=cmd_lsp)
        p_lsp_diag = lsp_sub.add_parser(
            "diagnostics",
            parents=[common],
            help="LEGACY: always E_LSP_HOST_OWNED (use host IDE/Grok LSP)",
        )
        p_lsp_diag.add_argument("path", help="Python file path")
        p_lsp_diag.set_defaults(func=cmd_lsp)
        p_lsp.set_defaults(func=cmd_lsp)

__all__ = [
    "register_inspect_parsers",
    "cmd_capabilities",
    "cmd_hud",
    "cmd_lsp",
    "cmd_native_status",
    "cmd_notify",
    "cmd_parity",
    "cmd_wiki",
]
