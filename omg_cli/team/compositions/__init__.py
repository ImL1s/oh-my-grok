"""Team composition contracts (non-executing scaffolds).

Hyperplan V1 and Security Research V1 live here. Execution / result production
remain out of scope until separately specified.
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
from omg_cli.team.compositions.security_research import (
    SECURITY_RESEARCH_KIND,
    SECURITY_RESEARCH_REPORT_KIND,
    SECURITY_RESEARCH_SCHEMA_VERSION,
    SecurityResearchError,
    compile_security_research_v1,
    load_security_research_manifest,
    materialize_security_research_v1,
    parse_security_research_spec_v1,
    security_research_manifest_path,
    validate_security_research_report_v1,
)

__all__ = [
    "HYPERPLAN_DECISION_KIND",
    "HYPERPLAN_KIND",
    "HYPERPLAN_SCHEMA_VERSION",
    "HyperplanError",
    "SECURITY_RESEARCH_KIND",
    "SECURITY_RESEARCH_REPORT_KIND",
    "SECURITY_RESEARCH_SCHEMA_VERSION",
    "SecurityResearchError",
    "compile_hyperplan_v1",
    "compile_security_research_v1",
    "hyperplan_manifest_path",
    "load_hyperplan_manifest",
    "load_security_research_manifest",
    "materialize_hyperplan_v1",
    "materialize_security_research_v1",
    "parse_hyperplan_spec_v1",
    "parse_security_research_spec_v1",
    "security_research_manifest_path",
    "validate_hyperplan_decision_v1",
    "validate_security_research_report_v1",
]
