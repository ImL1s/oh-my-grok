"""Memory-family CLI handlers (#29 Phase 2).

Commands: note, memory, tracker, compact.
Parser construction: ``register_memory_parsers`` (#29 Phase 4'); ``register_note_parser`` for early note (help order with install).
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



def register_memory_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    """Register memory-family argparse parsers (#29 Phase 4').

    Commands: memory, tracker, compact (``note`` stays early in main for help order).
    """
    p_memory = sub.add_parser(
        "memory",
        parents=[common],
        help="deterministic redacted project fact memory",
    )
    memory_sub = p_memory.add_subparsers(dest="memory_action")
    p_memory_put = memory_sub.add_parser("put", parents=[common], help="upsert user fact")
    p_memory_put.add_argument("key")
    p_memory_put.add_argument("value")
    p_memory_put.add_argument("--updated-at", default=None)
    p_memory_put.set_defaults(func=cmd_memory, memory_action="put")
    p_memory_search = memory_sub.add_parser(
        "search", parents=[common], help="search fact keys and values"
    )
    p_memory_search.add_argument("query")
    p_memory_search.add_argument("--limit", type=int, default=20)
    p_memory_search.set_defaults(func=cmd_memory, memory_action="search")
    p_memory_show = memory_sub.add_parser(
        "show", parents=[common], help="print canonical fact store"
    )
    p_memory_show.set_defaults(func=cmd_memory, memory_action="show", output=None)
    p_memory_export = memory_sub.add_parser(
        "export", parents=[common], help="write canonical fact store JSON"
    )
    p_memory_export.add_argument("--output", required=True)
    p_memory_export.set_defaults(func=cmd_memory, memory_action="export")
    p_memory_import = memory_sub.add_parser(
        "import", parents=[common], help="merge canonical fact store JSON"
    )
    p_memory_import.add_argument("file")
    p_memory_import.set_defaults(func=cmd_memory, memory_action="import")
    p_memory_rescan = memory_sub.add_parser(
        "rescan", parents=[common], help="replace scanner observations from JSON"
    )
    p_memory_rescan.add_argument("file")
    p_memory_rescan.add_argument("--observed-at", default=None)
    p_memory_rescan.set_defaults(func=cmd_memory, memory_action="rescan")
    p_memory.set_defaults(func=cmd_memory)

    p_tracker = sub.add_parser(
        "tracker",
        parents=[common],
        help="generation-fenced passive lifecycle projection",
    )
    tracker_sub = p_tracker.add_subparsers(dest="tracker_action")
    p_tracker_status = tracker_sub.add_parser(
        "status", parents=[common], help="show a projected run"
    )
    p_tracker_status.add_argument("--run", dest="run_id", required=True)
    p_tracker_status.set_defaults(func=cmd_tracker, tracker_action="status")
    p_tracker_project = tracker_sub.add_parser(
        "project", parents=[common], help="project journal or supplied events"
    )
    p_tracker_project.add_argument("--run", dest="run_id", required=True)
    p_tracker_project.add_argument("--generation", type=int, required=True)
    p_tracker_project.add_argument(
        "--events",
        default=None,
        help="optional JSON event array; otherwise read passive journals",
    )
    p_tracker_project.set_defaults(func=cmd_tracker, tracker_action="project")
    p_tracker_reconcile = tracker_sub.add_parser(
        "reconcile", parents=[common], help="reconcile signed native inventory"
    )
    p_tracker_reconcile.add_argument("--run", dest="run_id", required=True)
    p_tracker_reconcile.add_argument("--inventory", required=True)
    p_tracker_reconcile.set_defaults(func=cmd_tracker, tracker_action="reconcile")
    p_tracker.set_defaults(func=cmd_tracker)

    p_compact = sub.add_parser(
        "compact",
        parents=[common],
        help="lossless generation-fenced runtime compaction",
    )
    compact_sub = p_compact.add_subparsers(dest="compact_action")
    p_compact_create = compact_sub.add_parser(
        "create", parents=[common], help="create or adopt a checkpoint"
    )
    p_compact_create.add_argument("--run", dest="run_id", required=True)
    p_compact_create.add_argument("--generation", type=int, required=True)
    p_compact_create.add_argument("--guidance-file", required=True)
    p_compact_create.add_argument("--receipts", required=True)
    p_compact_create.add_argument("--recovery-manifest", required=True)
    p_compact_create.set_defaults(func=cmd_compact, compact_action="create")
    p_compact_show = compact_sub.add_parser(
        "show", parents=[common], help="validate and print checkpoint"
    )
    p_compact_show.add_argument("path")
    p_compact_show.set_defaults(func=cmd_compact, compact_action="show")
    p_compact_render = compact_sub.add_parser(
        "render", parents=[common], help="restore exact guidance bytes"
    )
    p_compact_render.add_argument("path")
    p_compact_render.add_argument("--guidance-out", required=True)
    p_compact_render.set_defaults(func=cmd_compact, compact_action="render")
    p_compact.set_defaults(func=cmd_compact)



def register_note_parser(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    """Register the early ``note`` command (#29 Phase 4').

    Kept separate so install early/late can sandwich historical help order.
    """
    p_note = sub.add_parser(
        "note",
        parents=[common],
        help="append a durable project note (.omg/notepad.md)",
    )
    p_note.add_argument(
        "text",
        nargs="*",
        help="note text (omit to show the notepad)",
    )
    p_note.add_argument(
        "--priority",
        action="store_true",
        help="permanent (else 7d TTL tag)",
    )
    p_note.add_argument(
        "--show",
        action="store_true",
        help="print the notepad and exit",
    )
    p_note.add_argument(
        "--prune",
        action="store_true",
        help="remove [7d] notes older than 7 days (permanent kept)",
    )
    p_note.set_defaults(func=cmd_note)


__all__ = [
    "register_memory_parsers",
    "register_note_parser",
    "cmd_compact",
    "cmd_memory",
    "cmd_note",
    "cmd_tracker",
]
