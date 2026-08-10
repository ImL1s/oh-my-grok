"""Team composition contracts (non-executing scaffolds).

Hyperplan V1 lives here. Security-research and other compositions are out of
scope for this package until separately specified.
"""

from __future__ import annotations

from omg_cli.team.compositions.hyperplan import (
    HYPERPLAN_DECISION_KIND,
    HYPERPLAN_KIND,
    HYPERPLAN_SCHEMA_VERSION,
    HyperplanError,
    compile_hyperplan_v1,
    hyperplan_manifest_path,
    load_hyperplan_manifest,
    materialize_hyperplan_v1,
    parse_hyperplan_spec_v1,
    validate_hyperplan_decision_v1,
)

__all__ = [
    "HYPERPLAN_DECISION_KIND",
    "HYPERPLAN_KIND",
    "HYPERPLAN_SCHEMA_VERSION",
    "HyperplanError",
    "compile_hyperplan_v1",
    "hyperplan_manifest_path",
    "load_hyperplan_manifest",
    "materialize_hyperplan_v1",
    "parse_hyperplan_spec_v1",
    "validate_hyperplan_decision_v1",
]
