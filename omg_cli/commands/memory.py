"""Memory-family CLI handlers (#29 Phase 2).

Commands: note, memory, tracker, compact.
Parser construction remains in ``main.build_parser``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omg_cli.cli_envelope import emit_data
from omg_cli.cli_util import project_root, read_json_path, write_json_path


def cmd_note(args: argparse.Namespace) -> int:
    from omg_cli.note import run_note

    return run_note(
        " ".join(args.text),
        root=project_root(),
        priority=bool(getattr(args, "priority", False)),
        show=bool(getattr(args, "show", False)),
        prune=bool(getattr(args, "prune", False)),
    )


def cmd_memory(args: argparse.Namespace) -> int:
    """Operate the deterministic, redacted project fact store."""
    from datetime import datetime, timezone

    from omg_cli.project_memory import (
        export_memory,
        import_memory,
        rescan_memory,
        search_memory,
        upsert_fact,
    )

    root = project_root()
    action = getattr(args, "memory_action", None)
    try:
        if action == "put":
            observed_at = getattr(args, "updated_at", None) or datetime.now(
                timezone.utc
            ).isoformat().replace("+00:00", "Z")
            result: object = upsert_fact(
                root,
                key=args.key,
                value=args.value,
                source="user",
                updated_at=observed_at,
            )
        elif action == "search":
            result = search_memory(root, args.query, limit=args.limit)
        elif action in {"show", "export"}:
            store = export_memory(root)
            result = store
            if getattr(args, "output", None):
                target = write_json_path(args.output, result)
                emit_data(
                    args,
                    "memory.export",
                    {"path": str(target), "facts": len(store["facts"])},
                )
                return 0
        elif action == "import":
            value = read_json_path(args.file, label="memory import")
            if not isinstance(value, dict):
                raise ValueError("memory import must be a JSON object")
            result = import_memory(root, value)
        elif action == "rescan":
            value = read_json_path(args.file, label="memory rescan")
            facts = value.get("facts") if isinstance(value, dict) else value
            if not isinstance(facts, list) or not all(
                isinstance(row, dict) for row in facts
            ):
                raise ValueError("memory rescan must contain a JSON fact array")
            observed_at = getattr(args, "observed_at", None) or datetime.now(
                timezone.utc
            ).isoformat().replace("+00:00", "Z")
            result = rescan_memory(root, facts, observed_at=observed_at)
        else:
            print("omg memory: action required", file=sys.stderr)
            return 2
    except (OSError, ValueError) as exc:
        print(f"omg memory: {exc}", file=sys.stderr)
        return 1
    emit_data(args, "memory", result)
    return 0


def cmd_tracker(args: argparse.Namespace) -> int:
    """Project passive lifecycle journals into the canonical tracker view."""
    from omg_cli.contracts.state_schemas import ContractValidationError
    from omg_cli.runtime_events import read_all_runtime_events
    from omg_cli.tracker import (
        TrackerError,
        load_tracker_projection,
        project_lifecycle_events,
        reconcile_native_inventory,
    )

    root = project_root()
    action = getattr(args, "tracker_action", None)
    try:
        if action == "status":
            result = load_tracker_projection(root, args.run_id)
            if result is None:
                result = {
                    "run_id": args.run_id,
                    "status": "not_projected",
                    "authoritative": False,
                }
        elif action == "project":
            if getattr(args, "events", None):
                value = read_json_path(args.events, label="tracker events")
                if isinstance(value, dict):
                    value = value.get("events")
                if not isinstance(value, list) or not all(
                    isinstance(row, dict) for row in value
                ):
                    raise ValueError("tracker events must be a JSON array")
                events = value
            else:
                events = [
                    row
                    for row in read_all_runtime_events(root)
                    if row.get("run_id") == args.run_id
                ]
            result = project_lifecycle_events(
                root,
                run_id=args.run_id,
                generation=args.generation,
                events=events,
            )
        elif action == "reconcile":
            value = read_json_path(args.inventory, label="native inventory")
            inventory = value.get("inventory") if isinstance(value, dict) else value
            if not isinstance(inventory, list) or not all(
                isinstance(row, dict) for row in inventory
            ):
                raise ValueError("native inventory must be a JSON array")
            result = reconcile_native_inventory(
                root,
                run_id=args.run_id,
                inventory=inventory,
            )
        else:
            print("omg tracker: action required", file=sys.stderr)
            return 2
    except (OSError, ValueError, TrackerError, ContractValidationError) as exc:
        print(f"omg tracker: {exc}", file=sys.stderr)
        return 1
    emit_data(args, "tracker", result)
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    """Create/read generation-fenced compaction checkpoints."""
    from omg_cli.compaction import (
        CompactionError,
        create_compaction_checkpoint,
        load_compaction_checkpoint,
        render_resume_context,
    )
    from omg_cli.contracts.state_schemas import ContractValidationError
    from omg_cli.contracts.writer_chain import sha256_hex

    action = getattr(args, "compact_action", None)
    try:
        if action == "create":
            receipts_value = read_json_path(args.receipts, label="compaction receipts")
            receipts = (
                receipts_value.get("receipts")
                if isinstance(receipts_value, dict)
                else receipts_value
            )
            recovery = read_json_path(
                args.recovery_manifest, label="recovery manifest"
            )
            if not isinstance(receipts, list) or not all(
                isinstance(row, dict) for row in receipts
            ):
                raise ValueError("compaction receipts must be a JSON array")
            if not isinstance(recovery, dict):
                raise ValueError("recovery manifest must be a JSON object")
            result: object = create_compaction_checkpoint(
                project_root(),
                run_id=args.run_id,
                generation=args.generation,
                guidance=Path(args.guidance_file).read_bytes(),
                receipts=receipts,
                recovery_manifest=recovery,
            )
        elif action == "show":
            result = load_compaction_checkpoint(args.path)
        elif action == "render":
            rendered = render_resume_context(load_compaction_checkpoint(args.path))
            guidance = rendered.pop("guidance")
            target = Path(args.guidance_out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(guidance)
            result = {
                **rendered,
                "guidance_path": str(target),
                "guidance_sha256": sha256_hex(guidance),
            }
        else:
            print("omg compact: action required", file=sys.stderr)
            return 2
    except (OSError, ValueError, CompactionError, ContractValidationError) as exc:
        print(f"omg compact: {exc}", file=sys.stderr)
        return 1
    emit_data(args, "compact", result)
    return 0


__all__ = [
    "cmd_compact",
    "cmd_memory",
    "cmd_note",
    "cmd_tracker",
]
