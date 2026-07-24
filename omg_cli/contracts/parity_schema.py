"""Machine-readable parity, traceability and ownership inventory schema."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .capability_schema import CAPABILITY_TIERS, PARITY_CLASSIFICATIONS
from .state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_object,
)


FROZEN_PINS = {
    "OMG": "25a80b7f5e95dcf4a9e53dd71e71295a21030dd3",
    "OMA": "f8eeaae6f42ebbfc1c22be504277377332c0d8fe",
    "OMC": "67dddfc05ff29900d8251dcec0ed9dee3c947ffa",
    "OMX": "435d4a9cc982ffaf83fabbfbb8711ae6c178ffca",
    "GROK_BUILD": "7cfcb20d2b50b0d18801a6c0af2e401c0e060894",
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
        + ["docs/parity/omg-parity.json", "docs/parity/omg-traceability.json"]
        + _paths(
            "scripts/",
            ("check_parity_inventory.py", "check_traceability.py", "check_writer_ownership.py"),
        )
        + [
            "tests/fixtures/carrier/**",
            "tests/fixtures/recovery/**",
            "tests/fixtures/capabilities/**",
            "tests/fixtures/release/**",
            "tests/fixtures/workflow/**",
        ]
        + _paths(
            "tests/",
            (
                "test_parity_inventory.py",
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
            ),
        )
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
            ),
        )
    ),
    "OMG-W3": tuple(
        _paths(
            "omg_cli/team/",
            (
                "__init__.py",
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
            ),
        )
        + _paths("omg_cli/", ("workers.py", "integrate.py", "fanout.py"))
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
                "test_team_heartbeat.py",
                "test_team_recovery.py",
                "test_team_worktree.py",
            ),
        )
    ),
    "OMG-W4": tuple(
        _paths("omg_cli/mcp/", ("__init__.py", "server.py", "tools.py"))
        + ["omg_cli/lsp_tools.py", "omg_cli/ask/**"]
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
        + _paths(
            "tests/",
            (
                "test_mcp_server.py",
                "test_lsp_symbols.py",
                "test_ask.py",
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
                "modes.py",
                "pipeline.py",
                "ralplan.py",
                "review.py",
                "qa.py",
                "guidance.py",
                "host_launcher.py",
                "madmax.py",
            ),
        )
        + [
            "pyproject.toml",
            "plugin.json",
            "hooks/hooks.json",
            "hooks/bin/omg_pretool_deny_standalone.py",
            ".mcp.json",
            ".lsp.json",
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
            "scripts/check_docs_links.py",
            "docs/research/**",
            "docs/superpowers/**",
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
                "test_modes.py",
                "test_pipeline.py",
                "test_ralplan.py",
                "test_review.py",
                "test_qa.py",
                "test_packaging.py",
                "test_docs_cli_drift.py",
                "test_release_readback.py",
                "test_host_launcher.py",
                "test_madmax.py",
            ),
        )
    ),
    "OMG-W7": (),
}


def load_json_object(path: Path | str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"cannot read JSON object {path}: {exc}") from exc
    return require_object(value, label=str(path))


def validate_parity_inventory(value: Mapping[str, Any]) -> dict[str, Any]:
    inventory = require_object(value, label="parity inventory")
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
