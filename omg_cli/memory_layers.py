"""Inspect-only catalog of OMG memory layers (#74).

Layers stay separate stores with distinct writers and retention. This module
never merges them into one unbounded ``memory.json``.
"""

from __future__ import annotations

from typing import Any


MEMORY_LAYER_SCHEMA = 1

# Responsibilities/retention only. Paths are labels, not an invitation to merge.
MEMORY_LAYERS: tuple[dict[str, str], ...] = (
    {
        "id": "session_handoff",
        "store": ".omg/state/RESUME.md + compact checkpoints",
        "responsibility": (
            "Continue the current operator conversation and run routing "
            "without replaying transcripts."
        ),
        "retention": "Run-scoped; cleared after successful resume or compact fence.",
        "writer": "omg CLI (resume/compact); hooks may only refresh passively",
    },
    {
        "id": "project_facts",
        "store": ".omg/memory/facts.json",
        "responsibility": "Redacted durable project facts (user/scanner/import).",
        "retention": "Until explicit export/import/rescan; not a session transcript.",
        "writer": "omg memory put|import|rescan",
    },
    {
        "id": "wiki",
        "store": ".omg/wiki/",
        "responsibility": "Curated project pages (Karpathy-style local wiki).",
        "retention": "Durable pages until ingest overwrite; CLI is the writer.",
        "writer": "omg wiki ingest",
    },
    {
        "id": "notepads",
        "store": ".omg/notepad.md",
        "responsibility": "Operator scratch notes that survive compaction.",
        "retention": "7d TTL unless --priority (permanent).",
        "writer": "omg note",
    },
    {
        "id": "writer_memory",
        "store": "omg-writer-memory skill + .omg/artifacts proposals",
        "responsibility": "Draft docs/comments owned by the writer agent.",
        "retention": "Proposal-only until a human/CLI promotion; never state authority.",
        "writer": "agents under .omg/artifacts/; CLI may promote",
    },
    {
        "id": "research_artifacts",
        "store": ".omg/research/ + .omg/artifacts/",
        "responsibility": "Bounded research notes and non-authoritative artifacts.",
        "retention": "Project durable; not a substitute for run status.",
        "writer": "omg CLI / tools research sidecar (opt-in network)",
    },
    {
        "id": "goals_plans",
        "store": ".omg/ultragoal/ + .omg/plans/",
        "responsibility": "Goal ledger, stories, and ralplan/ulw plan documents.",
        "retention": "Durable until goal/plan CLI mutation; hash-chained where applicable.",
        "writer": "omg goal / ralplan / workflow plan",
    },
    {
        "id": "transient_runtime",
        "store": "state_root/state/events + leases + HUD projections",
        "responsibility": "Lifecycle journals, leases, and observatory snapshots.",
        "retention": "Session retain --since (this project state_root only).",
        "writer": "omg CLI + fail-open hooks (append-only journals)",
    },
)


def list_memory_layers() -> dict[str, Any]:
    """Return the inspect catalog. Never mutates stores."""
    return {
        "schema_version": MEMORY_LAYER_SCHEMA,
        "merged": False,
        "unbounded_memory_json": False,
        "layers": [dict(row) for row in MEMORY_LAYERS],
        "note": (
            "Layers are separate stores. Do not concatenate them into one "
            "memory.json. omg memory put|search|show remains the fact store."
        ),
    }


__all__ = ["MEMORY_LAYER_SCHEMA", "MEMORY_LAYERS", "list_memory_layers"]
