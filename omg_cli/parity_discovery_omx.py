"""OMX-specific static discovery extractors for parity completeness (#78-G)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import PurePosixPath
from typing import Any

from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_nonempty_string,
    require_object,
)

_OMX_CATALOG_STATUSES = frozenset(
    {"active", "alias", "merged", "deprecated", "internal"}
)
_OMX_INSTALLABLE_STATUSES = frozenset({"active", "internal"})
_OMX_SKILL_STATUS_KIND = {
    "active": "skill",
    "alias": "skill_alias",
    "merged": "skill_merged",
    "deprecated": "skill_deprecated",
    "internal": "skill_internal",
}
_OMX_AGENT_STATUS_KIND = {
    "active": "agent",
    "alias": "agent_alias",
    "merged": "agent_merged",
    "deprecated": "agent_deprecated",
    "internal": "agent_internal",
}


def _helpers():
    from omg_cli import parity_discovery as pd

    return pd._category_for_kind, pd._require_relative_posix


def _safe_entry_name(name: str, *, label: str) -> str:
    text = require_nonempty_string(name, label=label)
    if not re.fullmatch(r"[A-Za-z0-9][\w-]*", text):
        raise ContractValidationError(f"{label}: invalid catalog name {text!r}")
    return text


def extract_omx_catalog_manifest_v1(
    *,
    registry_path: str,
    registry_bytes: bytes,
    category_assignment: Mapping[str, str],
    pin_paths: set[str],
    file_digest: Callable[[bytes], str],
    read_blob: Callable[[str], bytes],
    options: Mapping[str, Any],
    exceptions: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], set[str]]:
    """Parse templates/catalog-manifest.json into skill/agent/catalog surfaces."""
    _category_for_kind, _require_relative_posix = _helpers()
    del _require_relative_posix
    try:
        payload = json.loads(registry_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            f"{registry_path} is not valid UTF-8 JSON: {exc}"
        ) from exc
    body = require_object(payload, label=registry_path)
    skills_raw = body.get("skills")
    agents_raw = body.get("agents")
    if not isinstance(skills_raw, list) or not skills_raw:
        raise ContractValidationError(f"{registry_path}.skills must be a non-empty list")
    if not isinstance(agents_raw, list) or not agents_raw:
        raise ContractValidationError(f"{registry_path}.agents must be a non-empty list")

    skills_dir = str(options.get("skills_dir", "skills")).rstrip("/")
    prompts_dir = str(options.get("prompts_dir", "prompts")).rstrip("/")
    reg_digest = file_digest(registry_bytes)
    surfaces: list[dict[str, Any]] = []
    input_parts: list[dict[str, str]] = [
        {"path": registry_path, "content_digest": reg_digest}
    ]
    consumed: set[str] = set()

    skill_by_name: dict[str, dict[str, Any]] = {}
    agent_by_name: dict[str, dict[str, Any]] = {}
    seen_skills: set[str] = set()
    seen_agents: set[str] = set()

    for idx, raw in enumerate(skills_raw):
        entry = require_object(raw, label=f"{registry_path}.skills[{idx}]")
        name = _safe_entry_name(str(entry.get("name", "")), label=f"skills[{idx}].name")
        norm = name.lower()
        if norm in seen_skills:
            raise ContractValidationError(f"duplicate normalized skill name: {name}")
        seen_skills.add(norm)
        status = require_nonempty_string(
            entry.get("status"), label=f"skills[{idx}].status"
        )
        if status not in _OMX_CATALOG_STATUSES:
            raise ContractValidationError(f"invalid catalog status: {status}")
        skill_by_name[name] = dict(entry)

    for idx, raw in enumerate(agents_raw):
        entry = require_object(raw, label=f"{registry_path}.agents[{idx}]")
        name = _safe_entry_name(str(entry.get("name", "")), label=f"agents[{idx}].name")
        norm = name.lower()
        if norm in seen_agents:
            raise ContractValidationError(f"duplicate normalized agent name: {name}")
        seen_agents.add(norm)
        status = require_nonempty_string(
            entry.get("status"), label=f"agents[{idx}].status"
        )
        if status not in _OMX_CATALOG_STATUSES:
            raise ContractValidationError(f"invalid catalog status: {status}")
        agent_by_name[name] = dict(entry)

    installable_names = {
        name
        for name, entry in {**skill_by_name, **agent_by_name}.items()
        if entry.get("status") in _OMX_INSTALLABLE_STATUSES
    }

    declared_skill_files: set[str] = set()
    for name, entry in sorted(skill_by_name.items(), key=lambda item: item[0].lower()):
        status = str(entry["status"])
        kind = _OMX_SKILL_STATUS_KIND[status]
        category = _category_for_kind(kind, category_assignment, label=registry_path)
        if status in {"alias", "merged"}:
            canonical = entry.get("canonical")
            if not isinstance(canonical, str) or not canonical.strip():
                raise ContractValidationError(
                    f"{status} without canonical: skill.{name}"
                )
            canonical = canonical.strip()
            if canonical not in skill_by_name and canonical not in agent_by_name:
                raise ContractValidationError(
                    f"canonical target missing: skill.{name} -> {canonical}"
                )
            if canonical not in installable_names:
                raise ContractValidationError(
                    f"canonical target not installable: skill.{name} -> {canonical}"
                )
        skill_file = f"{skills_dir}/{name}/SKILL.md"
        if status in _OMX_INSTALLABLE_STATUSES and skill_file not in pin_paths:
            raise ContractValidationError(f"declared skill missing at pin: {skill_file}")
        if skill_file in pin_paths:
            declared_skill_files.add(skill_file)
            raw = read_blob(skill_file)
            digest = file_digest(raw)
            source_path = skill_file
            input_parts.append({"path": skill_file, "content_digest": digest})
        else:
            digest = reg_digest
            source_path = registry_path
        surfaces.append(
            {
                "surface_id": f"skill.{name}",
                "kind": kind,
                "category": category,
                "source_path": source_path,
                "anchor": f"skill:{name}",
                "content_digest": digest,
            }
        )

    declared_prompt_files: set[str] = set()
    for name, entry in sorted(agent_by_name.items(), key=lambda item: item[0].lower()):
        status = str(entry["status"])
        kind = _OMX_AGENT_STATUS_KIND[status]
        kind_for_cat = kind if kind in category_assignment else "agent"
        category = _category_for_kind(
            kind_for_cat, category_assignment, label=registry_path
        )
        if status in {"alias", "merged"}:
            canonical = entry.get("canonical")
            if not isinstance(canonical, str) or not canonical.strip():
                raise ContractValidationError(
                    f"{status} without canonical: agent.{name}"
                )
            canonical = canonical.strip()
            if canonical not in agent_by_name and canonical not in skill_by_name:
                raise ContractValidationError(
                    f"canonical target missing: agent.{name} -> {canonical}"
                )
            if canonical not in installable_names:
                raise ContractValidationError(
                    f"canonical target not installable: agent.{name} -> {canonical}"
                )
        prompt_file = f"{prompts_dir}/{name}.md"
        if prompt_file not in pin_paths:
            raise ContractValidationError(
                f"declared agent prompt missing at pin: {prompt_file}"
            )
        declared_prompt_files.add(prompt_file)
        raw = read_blob(prompt_file)
        digest = file_digest(raw)
        input_parts.append({"path": prompt_file, "content_digest": digest})
        surfaces.append(
            {
                "surface_id": f"agent.{name}",
                "kind": kind,
                "category": category,
                "source_path": prompt_file,
                "anchor": f"agent:{name}",
                "content_digest": digest,
            }
        )

    for path in sorted(pin_paths):
        parts = PurePosixPath(path).parts
        if (
            len(parts) == 3
            and parts[0] == skills_dir
            and parts[2] == "SKILL.md"
        ):
            if path not in declared_skill_files and path not in exceptions:
                raise ContractValidationError(
                    f"undeclared skill file present in tree: {path}"
                )
            if path in exceptions:
                consumed.add(path)
        if (
            len(parts) == 2
            and parts[0] == prompts_dir
            and parts[1].endswith(".md")
        ):
            if path not in declared_prompt_files and path not in exceptions:
                raise ContractValidationError(
                    f"undeclared prompt file present in tree: {path}"
                )
            if path in exceptions:
                consumed.add(path)

    catalog_category = _category_for_kind(
        "catalog", category_assignment, label=registry_path
    )
    agent_catalog_category = catalog_category
    if "agent_catalog" in category_assignment:
        agent_catalog_category = _category_for_kind(
            "agent_catalog", category_assignment, label=registry_path
        )
    surfaces.append(
        {
            "surface_id": "catalog.skills",
            "kind": "catalog",
            "category": catalog_category,
            "source_path": registry_path,
            "anchor": "catalog:skills",
            "content_digest": reg_digest,
        }
    )
    surfaces.append(
        {
            "surface_id": "catalog.agents",
            "kind": "catalog",
            "category": agent_catalog_category,
            "source_path": registry_path,
            "anchor": "catalog:agents",
            "content_digest": reg_digest,
        }
    )
    return surfaces, input_parts, consumed


def _extract_omx_help_commands(source: str) -> list[str]:
    """Parse static `export const HELP = \\`...\\`;` for omx subcommands."""
    cleaned = source
    # Reject HELP reassignment / mutation that is not the static template literal.
    if re.search(
        r"(?<![A-Za-z0-9_])HELP\s*\+|(?<![A-Za-z0-9_])HELP\.replace|"
        r"(?<![A-Za-z0-9_])HELP\.concat|"
        r"(?<![A-Za-z0-9_])HELP\s*=(?!\s*`)",
        cleaned,
    ):
        raise ContractValidationError(
            "omx_help_surface_v1: HELP parser dynamic mutation rejected"
        )
    match = re.search(
        r"export\s+const\s+HELP\s*=\s*`([\s\S]*?)`",
        cleaned,
    )
    if not match:
        raise ContractValidationError(
            "omx_help_surface_v1: static export const HELP template not found"
        )
    help_body = match.group(1)
    if "${" in help_body:
        raise ContractValidationError(
            "omx_help_surface_v1: HELP parser dynamic mutation rejected"
        )
    commands: list[str] = []
    seen: set[str] = set()
    has_bare_omx = False
    for line in help_body.splitlines():
        m = re.match(r"^\s+omx(?:\s+([a-z][\w-]*)\b)?", line)
        if not m:
            continue
        cmd = m.group(1)
        if cmd is None:
            has_bare_omx = True
            continue
        norm = cmd.lower()
        if norm in seen:
            continue
        seen.add(norm)
        commands.append(cmd)
    if has_bare_omx and "launch" not in seen:
        commands.append("launch")
    if not commands:
        raise ContractValidationError("omx_help_surface_v1: no CLI commands in HELP")
    return sorted(commands, key=str.lower)


def extract_omx_help_surface_v1(
    *,
    registry_path: str,
    registry_bytes: bytes,
    category_assignment: Mapping[str, str],
    file_digest: Callable[[bytes], str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    _category_for_kind, _ = _helpers()
    try:
        text = registry_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractValidationError(
            f"{registry_path} is not valid UTF-8: {exc}"
        ) from exc
    commands = _extract_omx_help_commands(text)
    category = _category_for_kind("cli", category_assignment, label=registry_path)
    digest = file_digest(registry_bytes)
    surfaces = [
        {
            "surface_id": f"cli.{name}",
            "kind": "cli",
            "category": category,
            "source_path": registry_path,
            "anchor": f"cli:{name}",
            "content_digest": digest,
        }
        for name in commands
    ]
    return surfaces, [{"path": registry_path, "content_digest": digest}]


def extract_omx_launcher_bin_v1(
    *,
    registry_path: str,
    registry_bytes: bytes,
    category_assignment: Mapping[str, str],
    file_digest: Callable[[bytes], str],
    options: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    _category_for_kind, _ = _helpers()
    try:
        text = registry_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractValidationError(
            f"{registry_path} is not valid UTF-8: {exc}"
        ) from exc
    if "rememberOmxLaunchContext" not in text and "oh-my-codex CLI entry" not in text:
        raise ContractValidationError(
            "omx_launcher_bin_v1: launcher source missing static OMX entry markers"
        )
    bin_name = str(options.get("bin_name", "omx"))
    category = _category_for_kind("bin", category_assignment, label=registry_path)
    digest = file_digest(registry_bytes)
    surfaces = [
        {
            "surface_id": f"bin.{bin_name}",
            "kind": "bin",
            "category": category,
            "source_path": registry_path,
            "anchor": f"bin:{bin_name}",
            "content_digest": digest,
        }
    ]
    return surfaces, [{"path": registry_path, "content_digest": digest}]


def extract_codex_plugin_manifest_v1(
    *,
    registry_path: str,
    registry_bytes: bytes,
    category_assignment: Mapping[str, str],
    pin_paths: set[str],
    file_digest: Callable[[bytes], str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    _category_for_kind, _require_relative_posix = _helpers()
    try:
        payload = json.loads(registry_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            f"{registry_path} is not valid UTF-8 JSON: {exc}"
        ) from exc
    body = require_object(payload, label=registry_path)
    name = require_nonempty_string(body.get("name"), label=f"{registry_path}.name")
    hooks = body.get("hooks")
    skills = body.get("skills")
    if not isinstance(hooks, str) or not hooks.strip():
        raise ContractValidationError(f"{registry_path}.hooks must be a relative path")
    if not isinstance(skills, str) or not skills.strip():
        raise ContractValidationError(f"{registry_path}.skills must be a relative path")
    plugin_dir = str(PurePosixPath(registry_path).parent.parent)
    hooks_rel = hooks.strip().lstrip("./")
    skills_rel = skills.strip().lstrip("./").rstrip("/")
    hooks_path = _require_relative_posix(
        f"{plugin_dir}/{hooks_rel}", label=f"{registry_path}.hooks"
    )
    skills_marker = _require_relative_posix(
        f"{plugin_dir}/{skills_rel}", label=f"{registry_path}.skills"
    )
    if hooks_path not in pin_paths:
        raise ContractValidationError(
            f"codex plugin hooks missing at pin: {hooks_path}"
        )
    skills_ok = any(
        p == skills_marker or p.startswith(skills_marker.rstrip("/") + "/")
        for p in pin_paths
    )
    if not skills_ok:
        raise ContractValidationError(
            f"codex plugin skills root missing at pin: {skills_marker}"
        )
    category = _category_for_kind("catalog", category_assignment, label=registry_path)
    digest = file_digest(registry_bytes)
    surfaces = [
        {
            "surface_id": f"catalog.codex_plugin.{name}",
            "kind": "catalog",
            "category": category,
            "source_path": registry_path,
            "anchor": f"catalog:codex_plugin:{name}",
            "content_digest": digest,
        }
    ]
    return surfaces, [{"path": registry_path, "content_digest": digest}]
