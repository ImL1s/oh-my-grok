"""Machine-readable parity, traceability and ownership inventory schema."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .capability_schema import CAPABILITY_TIERS, PARITY_CLASSIFICATIONS
from .state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_git_oid,
    require_integer,
    require_iso8601,
    require_nonempty_string,
    require_object,
    require_string_list,
)

# Parity inventory v2: single ordered maturity enum (not independent booleans).
PARITY_MATURITY_LEVELS = (
    "catalogued",
    "configured",
    "installed",
    "enabled",
    "loadable",
    "observed",
    "healthy",
    "live_verified",
)
PARITY_V2_CLASSIFICATIONS = (
    "faithful",
    "antigravity_native",
    "omg_native",
    "alias",
    "host_owned",
    "host_impossible",
    "optional_unclaimed",
    "excluded",
)
# Upstream pins only — OMG candidate commit is bound at check/release time.
UPSTREAM_PIN_IDS = (
    "OMC",
    "OMX",
    "OmO",
    "Antigravity",
    "GROK_BUILD",
)
# Inventory source coverage trackers (not pins): OMG/GROK_BUILD are not sources here.
SOURCE_STATUS_IDS = (
    "OMC",
    "OMX",
    "OmO",
    "Antigravity",
)
# Host runtime baseline (Grok Build) — independent of SOURCE_STATUS_IDS / parity score.
HOST_BASELINE_PIN_ID = "GROK_BUILD"
HOST_BASELINE_SNAPSHOT_RELATIVE = "docs/parity/upstream-snapshots/grok-build.json"
HOST_BASELINE_CLASSIFICATIONS = (
    "host_owned",
    "consumed_downstream",
    "irrelevant",
)
HOST_BASELINE_CATEGORIES = (
    "session",
    "subagent",
    "workflow",
    "queue",
    "dashboard",
    "permissions",
    "tmux",
    "extensions",
    "terminal",
    "mcp",
    "workspace",
    "auth",
    "reliability",
    "feedback",
    "tools",
    "slash",
)
HOST_BASELINE_MATURITY_LEVELS = (
    "catalogued",
    "configured",
    "installed",
    "enabled",
    "loadable",
    "observed",
    "healthy",
    "live_verified",
)
HOST_BASELINE_GENERATED_RELATIVE = (
    "docs/parity/generated/host-baseline.md",
    "docs/parity/generated/host-capability-matrix.md",
)
# #78-B required category taxonomy (constant; inventory file may lag during bootstrap).
PARITY_CATEGORY_TAXONOMY = frozenset(
    {
        "runtime_orchestration",
        "skills",
        "agents_routing",
        "team",
        "jobs",
        "hooks",
        "tools_mcp",
        "state_memory_observability",
        "install_update",
        "quality_visual_edit_safety",
        "antigravity",
        "platform_live_evidence",
        "parity_governance",
    }
)
INVENTORY_STATUS_VALUES = ("bootstrapping", "complete")
CATEGORY_STATUS_VALUES = ("bootstrapping", "complete")
NON_POSITIVE_CLASSIFICATIONS = frozenset(
    {"host_impossible", "excluded", "optional_unclaimed"}
)
# Classifications that may emit positive claim markers and must point at OMG
# implementation paths under strict (repo_root) validation. Alias / host_owned /
# optional_unclaimed / non-positive classes are excluded.
CLAIMABLE_IMPLEMENTATION_CLASSIFICATIONS = frozenset(
    {"faithful", "antigravity_native", "omg_native"}
)
# Canonical targets that forbid any positive maturity on an alias row.
ALIAS_NON_POSITIVE_TARGETS = frozenset(
    {"host_impossible", "excluded", "optional_unclaimed"}
)
POSITIVE_CLAIM_MIN_MATURITY = "healthy"
DEFAULT_LIVE_EVIDENCE_MAX_AGE_DAYS = 30
# User-observable capability IDs: dotted lowercase segments (not DUAL-001).
USER_OBSERVABLE_CAPABILITY_ID_RE = re.compile(
    r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
)
_MATURITY_EVIDENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "configured": ("configured_paths",),
    "installed": ("configured_paths", "install_evidence"),
    "enabled": ("configured_paths", "install_evidence", "enabled_evidence"),
    "loadable": (
        "configured_paths",
        "install_evidence",
        "enabled_evidence",
        "loadable_evidence",
    ),
    "observed": (
        "configured_paths",
        "install_evidence",
        "enabled_evidence",
        "loadable_evidence",
        "observed_evidence",
    ),
    "healthy": (
        "configured_paths",
        "install_evidence",
        "enabled_evidence",
        "loadable_evidence",
        "observed_evidence",
        "healthy_evidence",
    ),
    "live_verified": (
        "configured_paths",
        "install_evidence",
        "enabled_evidence",
        "loadable_evidence",
        "observed_evidence",
        "healthy_evidence",
    ),
}


FROZEN_PINS = {
    "OMG": "25a80b7f5e95dcf4a9e53dd71e71295a21030dd3",
    "OMA": "f8eeaae6f42ebbfc1c22be504277377332c0d8fe",
    "OMC": "67dddfc05ff29900d8251dcec0ed9dee3c947ffa",
    "OMX": "435d4a9cc982ffaf83fabbfbb8711ae6c178ffca",
    "GROK_BUILD": "a5589e958437d79e13db026eedcb1720bffd4063",
}
NORMATIVE_ARTIFACT_HASHES = {
    "requirements": "f9ff4cdad865330b2ea6db3443f19ce2ed48567ba3cc5164459822226e11805f",
    "prd": "0a9c2c644188bd461ffd96e0fc89f6ca017f2c5e6b15bbd28683b3d978c17952",
    "test_spec": "4cc4337225a3dcdb722351aedf573368ea23657e2d9ef9be1aca60f7927566d2",
    "plan": "29852abd254d1aa5c51b3a5a98739f0763a195f9c9b9b77ccea69e8ba3a770f5",
}
REQUIREMENT_ID_SET = (
    "DUAL-001",
    "DUAL-002",
    "DUAL-003",
    "LAUNCH-001",
    "LSP-001",
    "MCP-001",
    "OMA-AUTH-001",
    "OMA-G007-001",
    "OMA-HOOK-001",
    "OMA-IDENTITY-001",
    "OMA-INSTALL-001",
    "OMA-LSP-001",
    "OMA-MCP-001",
    "OMA-MEM-001",
    "OMA-NOTIFY-001",
    "OMA-SESSION-001",
    "OMA-TEAM-001",
    "OMG-EXT-001",
    "OMG-HOOK-001",
    "OMG-HOOK-002",
    "OMG-INSTALL-001",
    "OMG-LSP-001",
    "OMG-MCP-001",
    "OMG-MEM-001",
    "OMG-NOTIFY-001",
    "OMG-SESSION-001",
    "OMG-SPAWN-001",
    "OMG-TEAM-001",
    "OWN-001",
    "OWN-002",
    "OWN-003",
    "RELEASE-001",
    "RELEASE-002",
    "RESUME-001",
    "RESUME-002",
    "RESUME-003",
    "REVIEW-001",
    "TRACK-001",
    "TRUTH-001",
    "TRUTH-002",
    "WORKFLOW-001",
)
OMG_MCP_OPERATIONS = (
    "run_status.read",
    "trace.timeline",
    "trace.summary",
    "resume_metadata.read",
    "project_memory.search",
    "wiki.read",
    "team_status.read",
    "mailbox.list",
    "proposal.create",
)
OMA_MCP_OPERATIONS = (
    "run_status.read",
    "recovery_manifest.read",
    "wiki.search",
    "team_status.read",
    "mailbox.list",
    "proposal.create",
)


def _paths(prefix: str, names: tuple[str, ...], suffix: str = "") -> list[str]:
    return [f"{prefix}{name}{suffix}" for name in names]


OMG_OWNER_PATTERNS: dict[str, tuple[str, ...]] = {
    "OMG-W0": tuple(
        ["omg_cli/contracts/__init__.py"]
        + _paths(
            "omg_cli/contracts/",
            (
                "event_contract.py",
                "parity_schema.py",
                "team_envelope.py",
                "path_keys.py",
                "state_schemas.py",
                "tracker_contract.py",
                "resume_contract.py",
                "capability_schema.py",
                "writer_chain.py",
                "run_manifest.py",
                "release_transaction.py",
                "workflow_contract.py",
            ),
        )
        + [
            "omg_cli/parity_check.py",
            "omg_cli/parity_refresh.py",
            "omg_cli/parity_claim_gate.py",
            "omg_cli/parity_completeness.py",
        ]
        + ["docs/parity/omg-parity.json", "docs/parity/omg-traceability.json"]
        + [
            "docs/parity/README.md",
            "docs/parity/schema-v2.md",
            "docs/parity/completeness-schema-v1.md",
            "docs/parity/FEATURE-MATRIX.md",
            "docs/parity/GAPS.md",
            "docs/parity/SUMMARY.md",
            "docs/parity/SUMMARY.zh.md",
            "docs/parity/SUMMARY.zh-TW.md",
            "docs/parity/MATRIX-OMC.md",
            "docs/parity/MATRIX-OMX.md",
            "docs/parity/MATRIX-OmO.md",
            "docs/parity/MATRIX-Antigravity.md",
            "docs/parity/upstream-snapshots/grok-build.json",
            "docs/parity/generated/host-baseline.md",
            "docs/parity/generated/host-capability-matrix.md",
            "scripts/generate_host_baseline_docs.py",
        ]
        + [
            "docs/parity/upstream-snapshots/**",
            "docs/parity/reviews/**",
            "docs/parity/completeness/**",
        ]
        + [
            "omg_cli/parity_discovery.py",
            "omg_cli/parity_discovery_antigravity.py",
            "omg_cli/parity_discovery_omo.py",
            "omg_cli/parity_discovery_omx.py",
        ]
        + _paths(
            "scripts/",
            (
                "check_parity_inventory.py",
                "check_parity_completeness.py",
                "check_traceability.py",
                "check_writer_ownership.py",
                "generate_parity_docs.py",
            ),
        )
        + [
            "tests/fixtures/carrier/**",
            "tests/fixtures/recovery/**",
            "tests/fixtures/capabilities/**",
            "tests/fixtures/parity/**",
            "tests/fixtures/release/**",
            "tests/fixtures/workflow/**",
        ]
        + _paths(
            "tests/",
            (
                "test_parity_inventory.py",
                "test_parity_inventory_v2.py",
                "test_parity_generation.py",
                "test_parity_check.py",
                "test_parity_refresh.py",
                "test_parity_claim_gate.py",
                "test_parity_completeness.py",
                "test_parity_release_gate_acceptance.py",
                "test_parity_historical_banner.py",
                "test_parity_real_source_antigravity.py",
                "test_parity_real_source_omc.py",
                "test_parity_real_source_omo.py",
                "test_parity_real_source_omx.py",
                "test_traceability.py",
                "test_path_keys.py",
                "test_state_schemas.py",
                "test_writer_ownership.py",
                "test_writer_chain.py",
                "test_run_manifest.py",
                "test_release_transaction.py",
                "test_carrier_contract.py",
                "test_workflow_contract.py",
            ),
        )
    ),
    "OMG-W1": tuple(
        _paths(
            "scripts/",
            (
                "install.sh",
                "install-plugin.sh",
                "generate_standalone_hook.py",
                "e2e_realpath.py",
                "smoke.sh",
                "live_suite.sh",
                "omg_install_classifier.py",
                "canary_pretool.py",
                "release_attest.py",
            ),
        )
        + _paths(
            "omg_cli/",
            ("setup_cmd.py", "hook_install.py", "update_cmd.py", "uninstall_cmd.py", "doctor.py"),
        )
        + _paths(
            "tests/",
            (
                "test_install_cmd.py",
                "test_install_classifier.py",
                "test_install_gate_89.py",
                "test_hook_install.py",
                "test_hook_install_hardening.py",
                "test_update_uninstall.py",
                "test_doctor.py",
                "test_release_install.py",
                "test_guidance.py",
            ),
        )
    ),
    "OMG-W2": tuple(
        _paths(
            "omg_cli/",
            (
                "state.py",
                "host_session.py",
                "resume.py",
                "goals.py",
                "stop_gate.py",
                "note.py",
                "wiki.py",
                "runtime_events.py",
                "session_recovery.py",
                "project_memory.py",
                "tracker.py",
                "compaction.py",
                "capability_discovery.py",
                "redaction.py",
                "deny.py",
                "host_acp.py",
                "host_models.py",
                "host_probe.py",
            ),
        )
        + ["docs/host-compat.md", "tests/fixtures/host/**", "tests/fixtures/fake_grok_acp_agent.py"]
        + _paths(
            "hooks/bin/",
            ("_common.py", "pre_tool_use_deny.py", "session_start.py", "stop.py", "subagent_stop.py"),
        )
        + _paths(
            "tests/",
            (
                "test_state.py",
                "test_v2_regression_locks.py",
                "test_host_session.py",
                "test_resume.py",
                "test_goals.py",
                "test_stop_gate.py",
                "test_note.py",
                "test_hooks_common.py",
                "test_runtime_events.py",
                "test_lifecycle_hooks.py",
                "test_session_recovery.py",
                "test_project_memory.py",
                "test_tracker.py",
                "test_compaction.py",
                "test_capability_discovery.py",
                "test_redaction.py",
                "test_deny.py",
                "test_host_acp.py",
                "test_host_baseline_gate.py",
                "test_host_pin_transition.py",
                "test_host_probe.py",
                "test_host_snapshot_schema.py",
            ),
        )
    ),
    "OMG-W3": tuple(
        _paths(
            "omg_cli/team/",
            (
                "__init__.py",
                "api.py",
                "plane.py",
                "pipeline.py",
                "providers.py",
                "roles.py",
                "scaling.py",
                "routing.py",
                "mailbox.py",
                "liveness.py",
                "recovery.py",
                "worktree.py",
                "cli.py",
                "decomposition.py",
                "runtime.py",
                "tmux.py",
                "bootstrap.py",
                "launch.py",
                "operation_catalog.py",
                "operator.py",
                "presentation.py",
                "provider_ready.py",
                "replacement.py",
                "startup.py",
                "supervisor.py",
                "topology.py",
                "view.py",
            ),
        )
        + ["omg_cli/team/compositions/**"]
        + _paths("omg_cli/", ("workers.py", "integrate.py", "fanout.py"))
        + [
            "scripts/live_team_smoke.py",
            "tests/fixtures/team_worker_fixture.py",
            "tests/fixtures/providers/**",
            "tests/support/**",
            "tests/golden/team_operation_catalog_v1.json",
            "tests/golden/team_operation_catalog_v2.json",
            "tests/golden/team_operation_catalog_v3.json",
            "tests/golden/team_presentation_state_v1_dry_run.json",
            "tests/golden/team_hyperplan_v1_manifest.json",
            "tests/golden/team_hyperplan_v1_result_bundle.json",
            "tests/golden/team_hyperplan_v1_decision.json",
            "tests/golden/team_security_research_v1_manifest.json",
            "tests/golden/team_security_research_v1_result_bundle.json",
            "tests/golden/team_security_research_v1_report.json",
            "docs/team.md",
            "docs/team-operation-catalog-v1.md",
            "docs/team-operation-catalog-v2.md",
            "docs/team-operation-catalog-v3.md",
            "docs/team-presentation-state-v1.md",
            "docs/team-hyperplan-v1.md",
            "docs/team-security-research-v1.md",
        ]
        + _paths(
            "tests/",
            (
                "test_team_plane.py",
                "test_team_pipeline.py",
                "test_team_providers.py",
                "test_team_scaling.py",
                "test_team_routing.py",
                "test_workers.py",
                "test_integrate.py",
                "test_fanout.py",
                "test_team_mailbox.py",
                "test_team_api.py",
                "test_team_api_reliability.py",
                "test_team_heartbeat.py",
                "test_team_recovery.py",
                "test_team_worktree.py",
                "test_team_cli.py",
                "test_team_decomposition.py",
                "test_team_lifecycle.py",
                "test_team_runtime.py",
                "test_team_tmux_transport.py",
                "test_team_gate_default.py",
                "test_team_meta_mutate.py",
                "test_team_plan_only.py",
                "test_team_start_transaction.py",
                "test_team_agy_envelope.py",
                "test_team_bootstrap_100.py",
                "test_team_job_workers.py",
                "test_team_operation_catalog.py",
                "test_team_operator_101.py",
                "test_team_real_tmux_ux.py",
                "test_team_reconcile.py",
                "test_team_presentation_state.py",
                "test_team_hyperplan.py",
                "test_team_security_research.py",
                "test_team_replacement_attempts.py",
                "test_team_scale_tmux.py",
                "test_team_startup.py",
                "test_team_topology_102.py",
                "test_team_view.py",
            ),
        )
    ),
    "OMG-W4": tuple(
        _paths("omg_cli/mcp/", ("__init__.py", "server.py", "tools.py"))
        + [
            "omg_cli/lsp_tools.py",
            "omg_cli/ask/**",
            "omg_cli/providers/**",
            "omg_cli/commands/provider.py",
        ]
        + _paths(
            "omg_cli/workflows/",
            (
                "__init__.py",
                "schema.py",
                "registry.py",
                "planner.py",
                "runner.py",
                "replay.py",
                "permissions.py",
                "review.py",
                "grok_adapter.py",
            ),
        )
        + ["agents/*.md", "skills/*/SKILL.md", "scripts/generate_capabilities_lock.py"]
        + ["tests/fixtures/antigravity/**"]
        + _paths(
            "tests/",
            (
                "test_mcp_server.py",
                "test_lsp_symbols.py",
                "test_ask.py",
                "test_ask_agy.py",
                "antigravity_testutil.py",
                "test_antigravity_provider_probe.py",
                "test_antigravity_provider_run.py",
                "test_provider_process.py",
                "test_roles.py",
                "test_skill_inventory.py",
                "test_plugin_session_discovery.py",
                "test_capabilities_lock.py",
                "test_repository_workflows.py",
                "test_grok_workflow_adapter.py",
            ),
        )
    ),
    "OMG-W5": tuple(
        [
            "omg_cli/hud.py",
            "omg_cli/sidecar.py",
            "omg_cli/notify/**",
            "omg_cli/team/tmux_adapter.py",
        ]
        + _paths(
            "tests/",
            (
                "test_wiki_hud_lsp.py",
                "test_sidecar.py",
                "test_tmux_adapter.py",
                "test_notification_config.py",
                "test_notification_dispatcher.py",
                "test_notification_http.py",
            ),
        )
    ),
    "OMG-W6": tuple(
        _paths(
            "omg_cli/",
            (
                "__init__.py",
                "main.py",
                "autopilot.py",
                "implementation.py",
                "interview.py",
                "modes.py",
                "pipeline.py",
                "ralplan.py",
                "review.py",
                "qa.py",
                "guidance.py",
                "host_launcher.py",
                "madmax.py",
                "acceptance.py",
                "command_policy.py",
                "cli_envelope.py",
                "cli_util.py",
                "command_context.py",
                "command_registry.py",
                "package_release.py",
                "project_root.py",
            ),
        )
        + ["omg_cli/jobs/**", "docs/durable-jobs.md"]
        + _paths(
            "omg_cli/commands/",
            (
                "__init__.py",
                "inspect.py",
                "install.py",
                "job.py",
                "mcp.py",
                "memory.py",
                "modes.py",
                "run.py",
                "team.py",
                "workflow.py",
            ),
        )
        + [
            "CLAUDE.md",
            "pyproject.toml",
            "pytest.ini",
            "plugin.json",
            "hooks/hooks.json",
            "hooks/bin/omg_pretool_deny_standalone.py",
            ".mcp.json",
            ".lsp.json",
            ".gitignore",
            "omg_capabilities.lock.json",
            "templates/AGENTS.fragment.md",
            "templates/gitignore.fragment",
            "templates/omg-rules.md",
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
            "README.md",
            # Historical root locale README retained for rename/delete ownership.
            "README.zh-TW.md",
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md",
            "SECURITY.md",
            "requirements-dev.txt",
            "tests/__init__.py",
            "tests/fixtures/__init__.py",
            "scripts/check_docs_links.py",
            "scripts/check_static_coverage.py",
            "scripts/check_version_consistency.py",
            "scripts/generate_cli_commands_doc.py",
            "scripts/package_release.py",
            "scripts/static_checks.sh",
            "scripts/test_platform_contracts.sh",
            "scripts/live_autopilot_smoke.sh",
            "docs/research/**",
            "docs/superpowers/**",
            "docs/plans/**",
            "plans/**",
        ]
        + _paths(
            "docs/",
            (
                "README.md",
                "README.zh.md",
                "README.zh-TW.md",
                # Historical zh-Hant filenames retained for rename/delete ownership.
                "README.zh-Hant.md",
                "RELEASE.md",
                "RELEASE.zh.md",
                "RELEASE.zh-TW.md",
                "autopilot.md",
                "autopilot.zh.md",
                "autopilot.zh-TW.md",
                "autopilot.zh-Hant.md",
                "security-model.md",
                "security-model.zh.md",
                "security-model.zh-TW.md",
                "skills.md",
                "skills.zh.md",
                "skills.zh-TW.md",
                "skills.zh-Hant.md",
                "workflows.md",
                "workflows.zh.md",
                "workflows.zh-TW.md",
                "cli-commands.md",
                "cli-contract.md",
                "project-root.md",
            ),
        )
        + _paths(
            "docs/readme/",
            (
                "README.md",
                "README.zh.md",
                "README.zh-TW.md",
            ),
        )
        + _paths(
            "tests/",
            (
                "test_cli_router.py",
                "test_autopilot.py",
                "test_interview.py",
                "test_modes.py",
                "test_pipeline.py",
                "test_ralplan.py",
                "test_review.py",
                "test_qa.py",
                "test_packaging.py",
                "jobs_testutil.py",
                "test_jobs_acp_session.py",
                "test_jobs_antigravity.py",
                "test_jobs_auto_retry.py",
                "test_jobs_cli.py",
                "test_jobs_lease.py",
                "test_jobs_provider_registry.py",
                "test_jobs_recovery.py",
                "test_jobs_runtime.py",
                "test_docs_cli_drift.py",
                "test_release_readback.py",
                "test_host_launcher.py",
                "test_madmax.py",
                "test_acceptance.py",
                "test_autopilot_honesty_docs.py",
                "test_command_policy.py",
                "test_cli_commands_doc.py",
                "test_cli_envelope_golden.py",
                "test_cli_help_freeze.py",
                "test_command_context_envelope.py",
                "test_command_family_inspect.py",
                "test_command_family_install.py",
                "test_command_family_memory.py",
                "test_command_family_modes.py",
                "test_command_family_run.py",
                "test_command_family_team.py",
                "test_command_family_workflow.py",
                "test_command_registry.py",
                "test_compat.py",
                "test_package_release.py",
                "test_platform_host.py",
                "test_project_root.py",
                "test_safe_yolo_flags.py",
                "test_static_checks.py",
                "test_version_consistency.py",
            ),
        )
        + ["tests/report_validator/test_mock_report.py"]
    ),
    "OMG-W7": (),
}


def load_json_object(path: Path | str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"cannot read JSON object {path}: {exc}") from exc
    return require_object(value, label=str(path))


def maturity_rank(level: str) -> int:
    require_nonempty_string(level, label="maturity")
    if level not in PARITY_MATURITY_LEVELS:
        raise ContractValidationError(f"unknown maturity level: {level!r}")
    return PARITY_MATURITY_LEVELS.index(level)


def inventory_is_complete(inventory: Mapping[str, Any]) -> bool:
    if inventory.get("inventory_status") != "complete":
        return False
    categories = inventory.get("category_status")
    if not isinstance(categories, Mapping) or not categories:
        return False
    if PARITY_CATEGORY_TAXONOMY - set(categories):
        return False
    if not all(status == "complete" for status in categories.values()):
        return False
    sources = inventory.get("source_status")
    if not isinstance(sources, Mapping) or not sources:
        return False
    if set(sources) != set(SOURCE_STATUS_IDS):
        return False
    return all(status == "complete" for status in sources.values())


def inventory_completion_claims_allowed(inventory: Mapping[str, Any]) -> bool:
    """Percentages / green checkmarks only when every category and source is complete."""
    return inventory_is_complete(inventory)


def max_runtime_maturity(row: Mapping[str, Any]) -> str:
    maturity = row.get("maturity")
    if not isinstance(maturity, Mapping) or not maturity:
        raise ContractValidationError("capability maturity map is required")
    return max(
        (require_nonempty_string(level, label="maturity[]") for level in maturity.values()),
        key=maturity_rank,
    )


def claim_marker_for_capability(
    row: Mapping[str, Any],
    *,
    inventory: Mapping[str, Any],
) -> str | None:
    """Derived claimability marker for docs/CLI. Never overclaims."""
    classification = row.get("classification")
    if classification == "alias":
        target_id = row.get("alias_of")
        capabilities = inventory.get("capabilities")
        if isinstance(capabilities, list) and isinstance(target_id, str):
            for candidate in capabilities:
                if isinstance(candidate, Mapping) and candidate.get("id") == target_id:
                    return claim_marker_for_capability(
                        candidate, inventory=inventory
                    )
        return "catalogued"
    if classification in NON_POSITIVE_CLASSIFICATIONS:
        return str(classification)
    if not inventory_completion_claims_allowed(inventory):
        # Bootstrapping: maturity label only — no percentage or green check.
        try:
            return max_runtime_maturity(row)
        except ContractValidationError:
            return "catalogued"
    level = max_runtime_maturity(row)
    if maturity_rank(level) < maturity_rank(POSITIVE_CLAIM_MIN_MATURITY):
        return level
    # Complete inventory may show a positive marker only for claimable rows.
    return "healthy" if level == "healthy" else "live_verified"


def _parse_iso8601(value: str) -> datetime:
    text = require_iso8601(value, label="observed_at")
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    return datetime.fromisoformat(candidate)


def _require_relative_posix(path_text: str, *, label: str) -> str:
    text = require_nonempty_string(path_text, label=label)
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts or pure.parts[0] == "~":
        raise ContractValidationError(f"{label} must be a relative POSIX path")
    return text


def _path_exists(root: Path, relative: str) -> bool:
    return (root / relative).is_file() or (root / relative).is_dir()


def validate_parity_inventory(
    value: Mapping[str, Any],
    *,
    repo_root: Path | str | None = None,
    upstream_roots: Mapping[str, Path | str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    inventory = require_object(value, label="parity inventory")
    version = inventory.get("schema_version")
    if version == 1:
        return _validate_parity_inventory_v1(inventory)
    if version == 2:
        return _validate_parity_inventory_v2(
            inventory,
            repo_root=Path(repo_root) if repo_root is not None else None,
            upstream_roots={
                str(key): Path(path) for key, path in (upstream_roots or {}).items()
            },
            now=now or datetime.now(timezone.utc),
        )
    raise ContractValidationError(
        f"unsupported parity inventory schema_version={version!r}"
    )


def _validate_parity_inventory_v1(inventory: dict[str, Any]) -> dict[str, Any]:
    require_exact_keys(
        inventory,
        required={
            "store_kind",
            "schema_version",
            "repository_id",
            "ownership_manifest_id",
            "frozen_pins",
            "normative_artifact_hashes",
            "classifications",
            "capability_tiers",
            "requirement_ids",
            "mcp_operations",
            "semantic_lsp_proxy_count",
            "workflow",
            "rows",
        },
        label="parity inventory",
    )
    if inventory["store_kind"] != "parity_inventory" or inventory["schema_version"] != 1:
        raise ContractValidationError("parity inventory header mismatch")
    if inventory["repository_id"] != "OMG":
        raise ContractValidationError("parity inventory repository must be OMG")
    if inventory["ownership_manifest_id"] != "dual-parity-writers-v1":
        raise ContractValidationError("ownership manifest ID mismatch")
    if inventory["frozen_pins"] != FROZEN_PINS:
        raise ContractValidationError("frozen pin drift")
    if inventory["normative_artifact_hashes"] != NORMATIVE_ARTIFACT_HASHES:
        raise ContractValidationError("normative artifact hash drift")
    if inventory["classifications"] != list(PARITY_CLASSIFICATIONS):
        raise ContractValidationError("parity classification set/order drift")
    if inventory["capability_tiers"] != list(CAPABILITY_TIERS):
        raise ContractValidationError("capability tier set/order drift")
    if inventory["requirement_ids"] != list(REQUIREMENT_ID_SET):
        raise ContractValidationError("requirement ID set/order drift")
    if inventory["mcp_operations"] != list(OMG_MCP_OPERATIONS):
        raise ContractValidationError("OMG MCP operation inventory must contain exact nine")
    if inventory["semantic_lsp_proxy_count"] != 0:
        raise ContractValidationError("semantic LSP proxy count must be zero")
    workflow = require_object(inventory["workflow"], label="workflow inventory")
    require_exact_keys(
        workflow,
        required={"contract", "portable_classification", "grok_native_projection"},
        label="workflow inventory",
    )
    if workflow != {
        "contract": "repository-workflow/v1",
        "portable_classification": "native_substitute",
        "grok_native_projection": "optional_unclaimed",
    }:
        raise ContractValidationError("workflow inventory claim drift")
    rows = inventory["rows"]
    if not isinstance(rows, list) or [row.get("requirement_id") for row in rows] != list(
        REQUIREMENT_ID_SET
    ):
        raise ContractValidationError("parity rows must cover exact requirement IDs once")
    for row in rows:
        require_exact_keys(
            row,
            required={"requirement_id", "classification", "claim_state", "operation_tests"},
            label="parity row",
        )
        if row["classification"] not in PARITY_CLASSIFICATIONS:
            raise ContractValidationError("parity row classification invalid")
        if row["claim_state"] not in {"contract_only", "planned", "optional_unclaimed", "host_owned"}:
            raise ContractValidationError("parity row claim_state invalid")
        if not isinstance(row["operation_tests"], list) or not row["operation_tests"]:
            raise ContractValidationError("parity row operation_tests must be a non-empty array")
        if not all(isinstance(item, str) and item for item in row["operation_tests"]):
            raise ContractValidationError("parity row operation_tests must contain test IDs")
    return inventory


def _validate_parity_inventory_v2(
    inventory: dict[str, Any],
    *,
    repo_root: Path | None,
    upstream_roots: dict[str, Path],
    now: datetime,
) -> dict[str, Any]:
    require_exact_keys(
        inventory,
        required={
            "store_kind",
            "schema_version",
            "repository_id",
            "ownership_manifest_id",
            "inventory_status",
            "maturity_levels",
            "classifications",
            "upstream_pins",
            "category_status",
            "source_status",
            "live_evidence_max_age_days",
            "capabilities",
            "gaps",
        },
        label="parity inventory v2",
    )
    if inventory["store_kind"] != "parity_inventory" or inventory["schema_version"] != 2:
        raise ContractValidationError("parity inventory header mismatch")
    if inventory["repository_id"] != "OMG":
        raise ContractValidationError("parity inventory repository must be OMG")
    if inventory["ownership_manifest_id"] != "dual-parity-writers-v1":
        raise ContractValidationError("ownership manifest ID mismatch")
    if inventory["inventory_status"] not in INVENTORY_STATUS_VALUES:
        raise ContractValidationError("inventory_status invalid")
    if inventory["maturity_levels"] != list(PARITY_MATURITY_LEVELS):
        raise ContractValidationError("maturity level set/order drift")
    if inventory["classifications"] != list(PARITY_V2_CLASSIFICATIONS):
        raise ContractValidationError("parity v2 classification set/order drift")
    max_age = require_integer(
        inventory["live_evidence_max_age_days"],
        label="live_evidence_max_age_days",
        minimum=1,
    )

    pins = require_object(inventory["upstream_pins"], label="upstream_pins")
    if "OMG" in pins:
        raise ContractValidationError(
            "upstream_pins must not hardcode OMG candidate commit"
        )
    if set(pins) != set(UPSTREAM_PIN_IDS):
        raise ContractValidationError(
            "upstream_pins must be exactly "
            + ",".join(UPSTREAM_PIN_IDS)
        )
    for pin_id in UPSTREAM_PIN_IDS:
        pin = require_object(pins[pin_id], label=f"upstream_pins.{pin_id}")
        require_exact_keys(
            pin,
            required={"repository", "revision", "kind"},
            label=f"upstream_pins.{pin_id}",
        )
        require_nonempty_string(pin["repository"], label=f"{pin_id}.repository")
        if pin["kind"] != "commit":
            raise ContractValidationError(f"{pin_id}.kind must be commit")
        try:
            require_git_oid(pin["revision"], label=f"{pin_id}.revision")
        except ContractValidationError as exc:
            raise ContractValidationError(
                f"{pin_id} exact revision required (full git object id), got {pin['revision']!r}"
            ) from exc

    categories = require_object(inventory["category_status"], label="category_status")
    if not categories:
        raise ContractValidationError("category_status must be non-empty")
    missing = PARITY_CATEGORY_TAXONOMY - set(categories)
    if missing:
        raise ContractValidationError(
            "category_status missing required taxonomy categories: "
            + ", ".join(sorted(missing))
        )
    for name, status in categories.items():
        require_nonempty_string(name, label="category_status key")
        if status not in CATEGORY_STATUS_VALUES:
            raise ContractValidationError(f"category_status[{name!r}] invalid")

    sources = require_object(inventory["source_status"], label="source_status")
    if set(sources) != set(SOURCE_STATUS_IDS):
        raise ContractValidationError(
            "source_status must be exactly " + ",".join(SOURCE_STATUS_IDS)
        )
    for source_id in SOURCE_STATUS_IDS:
        status = sources[source_id]
        if status not in CATEGORY_STATUS_VALUES:
            raise ContractValidationError(f"source_status[{source_id!r}] invalid")

    capabilities = inventory["capabilities"]
    if not isinstance(capabilities, list) or not capabilities:
        raise ContractValidationError("capabilities must be a non-empty array")

    seen_ids: set[str] = set()
    id_to_row: dict[str, dict[str, Any]] = {}
    for row in capabilities:
        validated_row = _validate_capability_row_v2(
            row,
            pins=pins,
            categories=categories,
            repo_root=repo_root,
            upstream_roots=upstream_roots,
            max_age_days=max_age,
            now=now,
        )
        cap_id = validated_row["id"]
        if cap_id in seen_ids:
            raise ContractValidationError(f"duplicate capability id: {cap_id}")
        seen_ids.add(cap_id)
        id_to_row[cap_id] = validated_row

    for row in id_to_row.values():
        if row["classification"] == "alias":
            target = row.get("alias_of")
            if not isinstance(target, str) or target not in id_to_row:
                raise ContractValidationError(
                    f"alias {row['id']!r} requires existing canonical target"
                )
            canonical = id_to_row[target]
            if canonical["classification"] == "alias":
                raise ContractValidationError(
                    f"alias {row['id']!r} cannot target another alias"
                )
            _assert_alias_maturity_bounded(row, canonical)

    gaps = inventory["gaps"]
    if not isinstance(gaps, list):
        raise ContractValidationError("gaps must be an array")
    gap_ids: set[str] = set()
    for gap in gaps:
        g = require_object(gap, label="gap")
        require_exact_keys(
            g,
            required={
                "id",
                "priority",
                "status",
                "issues",
                "capability_ids",
                "summary",
            },
            label="gap",
        )
        gid = require_nonempty_string(g["id"], label="gap.id")
        if gid in gap_ids:
            raise ContractValidationError(f"duplicate gap id: {gid}")
        gap_ids.add(gid)
        require_nonempty_string(g["priority"], label="gap.priority")
        if g["status"] not in {"open", "closed", "deferred"}:
            raise ContractValidationError("gap.status invalid")
        issues = require_string_list(g["issues"], label="gap.issues", unique=True)
        if not issues or not all(item.startswith("#") for item in issues):
            raise ContractValidationError("gap.issues must be #N references")
        caps = require_string_list(g["capability_ids"], label="gap.capability_ids", unique=True)
        for cap_id in caps:
            if cap_id not in id_to_row:
                raise ContractValidationError(
                    f"gap {gid!r} references unknown capability {cap_id!r}"
                )
        require_nonempty_string(g["summary"], label="gap.summary")

    return inventory


def _validate_capability_row_v2(
    value: Any,
    *,
    pins: Mapping[str, Any],
    categories: Mapping[str, Any],
    repo_root: Path | None,
    upstream_roots: Mapping[str, Path],
    max_age_days: int,
    now: datetime,
) -> dict[str, Any]:
    row = require_object(value, label="capability")
    optional = {"alias_of", "last_verified_at", "notes"}
    require_exact_keys(
        row,
        required={
            "id",
            "category",
            "promise",
            "classification",
            "upstream",
            "omg_paths",
            "runtime_owner",
            "maturity",
            "evidence",
            "issues",
            "gap",
        },
        optional=optional,
        label="capability",
    )
    cap_id = require_nonempty_string(row["id"], label="capability.id")
    if not USER_OBSERVABLE_CAPABILITY_ID_RE.fullmatch(cap_id):
        raise ContractValidationError(
            f"capability id {cap_id!r} must be a user-observable dotted id"
        )
    category = require_nonempty_string(row["category"], label="capability.category")
    if category not in categories:
        raise ContractValidationError(f"capability category {category!r} not in category_status")
    require_nonempty_string(row["promise"], label="capability.promise")
    classification = require_nonempty_string(row["classification"], label="classification")
    if classification not in PARITY_V2_CLASSIFICATIONS:
        raise ContractValidationError(f"invalid classification: {classification!r}")
    if classification == "alias":
        require_nonempty_string(row.get("alias_of"), label="alias_of")
    elif "alias_of" in row and row["alias_of"] not in (None, ""):
        raise ContractValidationError("alias_of only allowed for alias classification")

    upstream = require_object(row["upstream"], label="upstream")
    require_exact_keys(
        upstream,
        required={"source", "revision", "source_paths"},
        label="upstream",
    )
    source = require_nonempty_string(upstream["source"], label="upstream.source")
    if source not in pins:
        raise ContractValidationError(f"upstream.source {source!r} not in upstream_pins")
    try:
        require_git_oid(upstream["revision"], label="upstream.revision")
    except ContractValidationError as exc:
        raise ContractValidationError(
            f"upstream exact revision required, got {upstream['revision']!r}"
        ) from exc
    if upstream["revision"] != pins[source]["revision"]:
        raise ContractValidationError(
            f"upstream.revision for {cap_id} must match pin {source}"
        )
    source_paths = require_string_list(
        upstream["source_paths"], label="upstream.source_paths", unique=True
    )
    if not source_paths:
        raise ContractValidationError("upstream.source_paths must be non-empty")
    for relative in source_paths:
        _require_relative_posix(relative, label="upstream.source_path")
    if source in upstream_roots:
        root = upstream_roots[source]
        for relative in source_paths:
            if not _path_exists(root, relative):
                raise ContractValidationError(
                    f"upstream source path missing under {source}: {relative}"
                )

    omg_paths = require_string_list(row["omg_paths"], label="omg_paths", unique=True)
    for relative in omg_paths:
        _require_relative_posix(relative, label="omg_path")
        if repo_root is not None and not _path_exists(repo_root, relative):
            raise ContractValidationError(f"omg implementation path missing: {relative}")
    if (
        repo_root is not None
        and classification in CLAIMABLE_IMPLEMENTATION_CLASSIFICATIONS
        and not omg_paths
    ):
        raise ContractValidationError(
            f"strict mode requires non-empty omg_paths for {classification} "
            f"capability {cap_id!r}"
        )

    require_nonempty_string(row["runtime_owner"], label="runtime_owner")
    maturity = require_object(row["maturity"], label="maturity")
    if not maturity:
        raise ContractValidationError("maturity map must be non-empty")
    for runtime, level in maturity.items():
        require_nonempty_string(runtime, label="maturity runtime")
        require_nonempty_string(level, label="maturity level")
        if level not in PARITY_MATURITY_LEVELS:
            raise ContractValidationError(f"unknown maturity {level!r}")

    evidence = require_object(row["evidence"], label="evidence")
    require_exact_keys(
        evidence,
        required={"tests", "docs", "live"},
        optional={
            "configured_paths",
            "install_evidence",
            "enabled_evidence",
            "loadable_evidence",
            "observed_evidence",
            "healthy_evidence",
        },
        label="evidence",
    )
    for field in ("tests", "docs"):
        require_string_list(evidence[field], label=f"evidence.{field}", unique=True)
    live = evidence["live"]
    if not isinstance(live, list):
        raise ContractValidationError("evidence.live must be an array")

    peak = max_runtime_maturity(row)
    _assert_maturity_prerequisites(row, peak=peak, repo_root=repo_root)

    if classification in NON_POSITIVE_CLASSIFICATIONS:
        if maturity_rank(peak) >= maturity_rank(POSITIVE_CLAIM_MIN_MATURITY):
            raise ContractValidationError(
                f"{classification} cannot generate positive claim "
                f"(maturity {peak!r} >= {POSITIVE_CLAIM_MIN_MATURITY})"
            )

    if peak == "live_verified":
        _assert_fresh_live_evidence(
            live,
            maturity=maturity,
            max_age_days=max_age_days,
            now=now,
        )

    issues = require_string_list(row["issues"], label="issues", unique=True)
    if not all(item.startswith("#") for item in issues):
        raise ContractValidationError("issues must be #N references")
    if not isinstance(row["gap"], str):
        raise ContractValidationError("gap must be a string")
    return row


def _assert_alias_maturity_bounded(
    alias_row: Mapping[str, Any],
    canonical: Mapping[str, Any],
) -> None:
    """Alias maturity/claimability must not exceed the canonical target."""
    alias_id = alias_row["id"]
    target_id = canonical["id"]
    target_class = canonical["classification"]
    alias_maturity = require_object(alias_row["maturity"], label="alias.maturity")
    target_maturity = require_object(canonical["maturity"], label="canonical.maturity")
    for runtime, level in alias_maturity.items():
        require_nonempty_string(runtime, label="alias maturity runtime")
        require_nonempty_string(level, label="alias maturity level")
        target_level = target_maturity.get(runtime)
        if not isinstance(target_level, str) or not target_level:
            raise ContractValidationError(
                f"alias {alias_id!r} runtime {runtime!r} missing on canonical "
                f"{target_id!r}"
            )
        if maturity_rank(level) > maturity_rank(target_level):
            raise ContractValidationError(
                f"alias {alias_id!r} maturity {level!r} exceeds canonical "
                f"{target_id!r} maturity {target_level!r} for runtime {runtime!r}"
            )
    if target_class in ALIAS_NON_POSITIVE_TARGETS:
        peak = max_runtime_maturity(alias_row)
        if maturity_rank(peak) >= maturity_rank(POSITIVE_CLAIM_MIN_MATURITY):
            raise ContractValidationError(
                f"alias {alias_id!r} cannot claim positive maturity when "
                f"canonical {target_id!r} is {target_class}"
            )


def _assert_maturity_prerequisites(
    row: Mapping[str, Any],
    *,
    peak: str,
    repo_root: Path | None = None,
) -> None:
    if peak == "catalogued":
        return
    required_fields = _MATURITY_EVIDENCE_FIELDS.get(peak)
    if not required_fields:
        raise ContractValidationError(f"no maturity prerequisites for {peak!r}")
    evidence = require_object(row["evidence"], label="evidence")
    for field in required_fields:
        values = evidence.get(field)
        if not isinstance(values, list) or not values:
            raise ContractValidationError(
                f"maturity prerequisite missing: {field} required for {peak}"
            )
        require_string_list(values, label=f"evidence.{field}", unique=True)
    if peak == "live_verified":
        live = evidence.get("live")
        if not isinstance(live, list) or not live:
            raise ContractValidationError(
                "maturity prerequisite missing: live evidence required for live_verified"
            )
    if (
        repo_root is not None
        and maturity_rank(peak) >= maturity_rank(POSITIVE_CLAIM_MIN_MATURITY)
    ):
        healthy = evidence.get("healthy_evidence")
        if not isinstance(healthy, list) or not healthy:
            raise ContractValidationError(
                "strict mode requires verifiable healthy_evidence for "
                f"{peak} capability {row.get('id')!r}"
            )
        paths = require_string_list(
            healthy, label="evidence.healthy_evidence", unique=True
        )
        for relative in paths:
            _require_relative_posix(relative, label="evidence.healthy_evidence")
            if not _path_exists(repo_root, relative):
                raise ContractValidationError(
                    f"healthy_evidence path missing under repo: {relative}"
                )


def _assert_fresh_live_evidence(
    live: list[Any],
    *,
    maturity: Mapping[str, Any],
    max_age_days: int,
    now: datetime,
) -> None:
    if not live:
        raise ContractValidationError("live_verified requires fresh live evidence")
    runtimes_needed = {
        runtime
        for runtime, level in maturity.items()
        if level == "live_verified"
    }
    covered: set[str] = set()
    for item in live:
        entry = require_object(item, label="live evidence")
        require_exact_keys(
            entry,
            required={"runtime", "platform", "version", "observed_at", "marker"},
            label="live evidence",
        )
        runtime = require_nonempty_string(entry["runtime"], label="live.runtime")
        require_nonempty_string(entry["platform"], label="live.platform")
        require_nonempty_string(entry["version"], label="live.version")
        require_nonempty_string(entry["marker"], label="live.marker")
        observed = _parse_iso8601(entry["observed_at"])
        age = now - observed
        if age > timedelta(days=max_age_days) or age < timedelta(0):
            raise ContractValidationError(
                f"live evidence for {runtime} is not fresh "
                f"(age={age.days}d, max={max_age_days}d)"
            )
        covered.add(runtime)
    missing = runtimes_needed - covered
    if missing:
        raise ContractValidationError(
            f"live_verified missing fresh runtime/platform evidence for {sorted(missing)}"
        )


def validate_traceability(value: Mapping[str, Any]) -> dict[str, Any]:
    trace = require_object(value, label="traceability inventory")
    require_exact_keys(
        trace,
        required={
            "store_kind",
            "schema_version",
            "repository_id",
            "ownership_manifest_id",
            "requirement_ids",
            "entries",
        },
        label="traceability inventory",
    )
    if trace["store_kind"] != "parity_traceability" or trace["schema_version"] != 1:
        raise ContractValidationError("traceability header mismatch")
    if trace["repository_id"] != "OMG" or trace["ownership_manifest_id"] != "dual-parity-writers-v1":
        raise ContractValidationError("traceability repository/ownership mismatch")
    if trace["requirement_ids"] != list(REQUIREMENT_ID_SET):
        raise ContractValidationError("traceability requirement set/order drift")
    entries = trace["entries"]
    if not isinstance(entries, list) or [entry.get("requirement_id") for entry in entries] != list(
        REQUIREMENT_ID_SET
    ):
        raise ContractValidationError("traceability entries must cover exact IDs once")
    for entry in entries:
        require_exact_keys(
            entry,
            required={"requirement_id", "waves", "code_paths", "test_paths", "evidence_tier"},
            label="traceability entry",
        )
        if not isinstance(entry["waves"], list) or not entry["waves"]:
            raise ContractValidationError("traceability entry needs at least one wave")
        if any(wave not in OMG_OWNER_PATTERNS for wave in entry["waves"]):
            raise ContractValidationError("traceability entry names unknown OMG wave")
        for field in ("code_paths", "test_paths"):
            if not isinstance(entry[field], list) or not entry[field]:
                raise ContractValidationError(f"traceability {field} must be non-empty")
        if entry["evidence_tier"] not in {"L0", "L1", "L2", "L3", "L4", "L5"}:
            raise ContractValidationError("traceability evidence tier invalid")
    return trace


def _require_relative_posix_path(path_text: str, *, label: str) -> str:
    text = require_nonempty_string(path_text, label=label)
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts or pure.parts[0] == "~":
        raise ContractValidationError(f"{label} must be a relative POSIX path")
    return text


def validate_host_baseline_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate independent Grok Build host-baseline snapshot (not SOURCE_STATUS)."""
    snapshot = require_object(value, label="host_baseline_snapshot")
    require_exact_keys(
        snapshot,
        required={
            "store_kind",
            "schema_version",
            "host_id",
            "repository",
            "public_commit",
            "source_revision",
            "release",
            "observed_version",
            "platform",
            "capabilities",
            "review",
            "generated",
            "issues",
            "maturity_floor",
        },
        label="host_baseline_snapshot",
    )
    if snapshot["store_kind"] != "host_baseline_snapshot":
        raise ContractValidationError("host_baseline_snapshot.store_kind mismatch")
    if snapshot["schema_version"] != 1:
        raise ContractValidationError(
            f"unsupported host_baseline schema_version={snapshot['schema_version']!r}"
        )
    if snapshot["host_id"] != HOST_BASELINE_PIN_ID:
        raise ContractValidationError(
            f"host_baseline_snapshot.host_id must be {HOST_BASELINE_PIN_ID!r}"
        )
    require_nonempty_string(snapshot["repository"], label="host_baseline.repository")
    require_git_oid(snapshot["public_commit"], label="host_baseline.public_commit")
    require_git_oid(snapshot["source_revision"], label="host_baseline.source_revision")
    require_nonempty_string(snapshot["release"], label="host_baseline.release")
    require_nonempty_string(
        snapshot["observed_version"], label="host_baseline.observed_version"
    )
    require_nonempty_string(snapshot["platform"], label="host_baseline.platform")
    if snapshot["maturity_floor"] not in HOST_BASELINE_MATURITY_LEVELS:
        raise ContractValidationError("host_baseline.maturity_floor invalid")
    if snapshot["maturity_floor"] == "live_verified":
        raise ContractValidationError(
            "host_baseline.maturity_floor must not claim live_verified without live proof"
        )
    issues = require_string_list(snapshot["issues"], label="host_baseline.issues")
    if not issues:
        raise ContractValidationError("host_baseline.issues must be non-empty")

    review = require_object(snapshot["review"], label="host_baseline.review")
    require_exact_keys(
        review,
        required={"status", "reviewed_pin", "notes"},
        label="host_baseline.review",
    )
    require_nonempty_string(review["status"], label="host_baseline.review.status")
    require_git_oid(review["reviewed_pin"], label="host_baseline.review.reviewed_pin")
    if review["reviewed_pin"] != snapshot["public_commit"]:
        raise ContractValidationError(
            "host_baseline.review.reviewed_pin must equal public_commit"
        )
    require_nonempty_string(review["notes"], label="host_baseline.review.notes")

    generated = require_object(snapshot["generated"], label="host_baseline.generated")
    require_exact_keys(
        generated,
        required={"docs"},
        label="host_baseline.generated",
    )
    docs = require_string_list(
        generated["docs"], label="host_baseline.generated.docs", unique=True
    )
    if not docs:
        raise ContractValidationError("host_baseline.generated.docs must be non-empty")
    for relative in docs:
        _require_relative_posix_path(relative, label="host_baseline.generated.docs[]")

    capabilities = snapshot["capabilities"]
    if not isinstance(capabilities, list) or not capabilities:
        raise ContractValidationError(
            "host_baseline.capabilities must be a non-empty array"
        )
    seen: set[str] = set()
    for index, raw in enumerate(capabilities):
        cap = require_object(raw, label=f"host_baseline.capabilities[{index}]")
        require_exact_keys(
            cap,
            required={
                "id",
                "category",
                "classification",
                "owner",
                "runtime",
                "status",
                "maturity",
                "promise",
                "evidence",
                "downstream_issues",
            },
            label=f"host_baseline.capabilities[{index}]",
        )
        cap_id = require_nonempty_string(
            cap["id"], label=f"host_baseline.capabilities[{index}].id"
        )
        if not USER_OBSERVABLE_CAPABILITY_ID_RE.match(cap_id):
            raise ContractValidationError(
                f"host capability id {cap_id!r} must be dotted lowercase"
            )
        if cap_id in seen:
            raise ContractValidationError(f"duplicate host capability id {cap_id!r}")
        seen.add(cap_id)
        category = require_nonempty_string(
            cap["category"], label=f"host_baseline.capabilities[{index}].category"
        )
        if category not in HOST_BASELINE_CATEGORIES:
            raise ContractValidationError(
                f"host capability {cap_id!r} category {category!r} not in "
                "HOST_BASELINE_CATEGORIES"
            )
        classification = require_nonempty_string(
            cap["classification"],
            label=f"host_baseline.capabilities[{index}].classification",
        )
        if classification not in HOST_BASELINE_CLASSIFICATIONS:
            raise ContractValidationError(
                f"host capability {cap_id!r} classification {classification!r} "
                "must be host_owned|consumed_downstream|irrelevant"
            )
        owner = require_nonempty_string(
            cap["owner"], label=f"host_baseline.capabilities[{index}].owner"
        )
        runtime = require_nonempty_string(
            cap["runtime"], label=f"host_baseline.capabilities[{index}].runtime"
        )
        if owner != "host":
            raise ContractValidationError(
                f"host capability {cap_id!r} owner must be 'host' (got {owner!r})"
            )
        if runtime != "grok":
            raise ContractValidationError(
                f"host capability {cap_id!r} runtime must be 'grok' (got {runtime!r})"
            )
        status = require_nonempty_string(
            cap["status"], label=f"host_baseline.capabilities[{index}].status"
        )
        maturity = require_nonempty_string(
            cap["maturity"], label=f"host_baseline.capabilities[{index}].maturity"
        )
        if status != maturity:
            raise ContractValidationError(
                f"host capability {cap_id!r} status must equal maturity"
            )
        if maturity not in HOST_BASELINE_MATURITY_LEVELS:
            raise ContractValidationError(
                f"host capability {cap_id!r} maturity {maturity!r} invalid"
            )
        if maturity == "live_verified":
            raise ContractValidationError(
                f"host capability {cap_id!r} must not claim live_verified in catalogue-only PR"
            )
        require_nonempty_string(
            cap["promise"], label=f"host_baseline.capabilities[{index}].promise"
        )
        evidence = require_object(
            cap["evidence"], label=f"host_baseline.capabilities[{index}].evidence"
        )
        require_exact_keys(
            evidence,
            required={"source_commit", "source_paths", "notes"},
            label=f"host_baseline.capabilities[{index}].evidence",
        )
        require_git_oid(
            evidence["source_commit"],
            label=f"host_baseline.capabilities[{index}].evidence.source_commit",
        )
        paths = require_string_list(
            evidence["source_paths"],
            label=f"host_baseline.capabilities[{index}].evidence.source_paths",
            unique=True,
        )
        if not paths:
            raise ContractValidationError(
                f"host capability {cap_id!r} evidence.source_paths must be non-empty"
            )
        for relative in paths:
            _require_relative_posix_path(
                relative,
                label=f"host_baseline.capabilities[{index}].evidence.source_paths[]",
            )
        require_nonempty_string(
            evidence["notes"],
            label=f"host_baseline.capabilities[{index}].evidence.notes",
        )
        require_string_list(
            cap["downstream_issues"],
            label=f"host_baseline.capabilities[{index}].downstream_issues",
            unique=True,
        )
        # host_owned / irrelevant must not claim OMG implementation evidence.
        if "omg_paths" in cap:
            omg_paths = cap["omg_paths"]
            if classification in {"host_owned", "irrelevant"}:
                if omg_paths not in (None, [], ()):
                    raise ContractValidationError(
                        f"host capability {cap_id!r} classification {classification!r} "
                        "must not claim omg_paths as implementation evidence"
                    )
            elif omg_paths is not None:
                if not isinstance(omg_paths, list):
                    raise ContractValidationError(
                        f"host capability {cap_id!r} omg_paths must be an array"
                    )
                for relative in require_string_list(
                    omg_paths,
                    label=f"host_baseline.capabilities[{index}].omg_paths",
                    unique=True,
                ):
                    _require_relative_posix_path(
                        relative,
                        label=f"host_baseline.capabilities[{index}].omg_paths[]",
                    )
    return snapshot
