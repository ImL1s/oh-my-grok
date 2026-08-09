"""Static upstream discovery extractors for parity completeness (#78-F/#78-G/#78-H/#78-I).

Discovery-rules v2 methods parse only admitted static syntax from git pin
blobs. Never executes upstream JavaScript/TypeScript/npm.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from omg_cli.contracts.parity_schema import PARITY_CATEGORY_TAXONOMY
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_nonempty_string,
    require_object,
    require_safe_id,
)

__all__ = [
    "DISCOVERY_RULES_V1",
    "DISCOVERY_RULES_V2",
    "EXTRACTION_METHODS_V2",
    "extract_surfaces_v2",
    "list_pin_tree_paths",
    "normalize_surface_id_case",
    "validate_v2_registry_entry",
]

DISCOVERY_RULES_V1 = 1
DISCOVERY_RULES_V2 = 2

EXTRACTION_CLAUDE_PLUGIN_SKILLS_V1 = "claude_plugin_skills_v1"
EXTRACTION_MARKDOWN_COMMAND_TREE_V1 = "markdown_command_tree_v1"
EXTRACTION_TYPESCRIPT_AGENT_REGISTRY_V1 = "typescript_agent_registry_v1"
EXTRACTION_COMMANDER_COMMAND_GRAPH_V1 = "commander_command_graph_v1"
EXTRACTION_CLAUDE_HOOKS_MANIFEST_V1 = "claude_hooks_manifest_v1"
EXTRACTION_TYPESCRIPT_TOOL_FAMILY_GRAPH_V1 = "typescript_tool_family_graph_v1"
EXTRACTION_PACKAGE_SURFACE_V1 = "package_surface_v1"
EXTRACTION_OMX_CATALOG_MANIFEST_V1 = "omx_catalog_manifest_v1"
EXTRACTION_OMX_HELP_SURFACE_V1 = "omx_help_surface_v1"
EXTRACTION_OMX_LAUNCHER_BIN_V1 = "omx_launcher_bin_v1"
EXTRACTION_CODEX_PLUGIN_MANIFEST_V1 = "codex_plugin_manifest_v1"
EXTRACTION_OMO_ZOD_STRING_ENUM_V1 = "omo_zod_string_enum_v1"
EXTRACTION_OMO_COMMAND_TREE_V1 = "omo_command_tree_v1"
EXTRACTION_OMO_AGENT_NAMES_SCHEMA_V1 = "omo_agent_names_schema_v1"
EXTRACTION_ANTIGRAVITY_README_CATALOG_V1 = "antigravity_readme_catalog_v1"
EXTRACTION_ANTIGRAVITY_CHANGELOG_RELEASES_V1 = "antigravity_changelog_releases_v1"
EXTRACTION_ANTIGRAVITY_EXAMPLES_TREE_V1 = "antigravity_examples_tree_v1"
EXTRACTION_ANTIGRAVITY_ISSUE_TEMPLATES_V1 = "antigravity_issue_templates_v1"

EXTRACTION_METHODS_V2 = frozenset(
    {
        EXTRACTION_CLAUDE_PLUGIN_SKILLS_V1,
        EXTRACTION_MARKDOWN_COMMAND_TREE_V1,
        EXTRACTION_TYPESCRIPT_AGENT_REGISTRY_V1,
        EXTRACTION_COMMANDER_COMMAND_GRAPH_V1,
        EXTRACTION_CLAUDE_HOOKS_MANIFEST_V1,
        EXTRACTION_TYPESCRIPT_TOOL_FAMILY_GRAPH_V1,
        EXTRACTION_PACKAGE_SURFACE_V1,
        EXTRACTION_OMX_CATALOG_MANIFEST_V1,
        EXTRACTION_OMX_HELP_SURFACE_V1,
        EXTRACTION_OMX_LAUNCHER_BIN_V1,
        EXTRACTION_CODEX_PLUGIN_MANIFEST_V1,
        EXTRACTION_OMO_ZOD_STRING_ENUM_V1,
        EXTRACTION_OMO_COMMAND_TREE_V1,
        EXTRACTION_OMO_AGENT_NAMES_SCHEMA_V1,
        EXTRACTION_ANTIGRAVITY_README_CATALOG_V1,
        EXTRACTION_ANTIGRAVITY_CHANGELOG_RELEASES_V1,
        EXTRACTION_ANTIGRAVITY_EXAMPLES_TREE_V1,
        EXTRACTION_ANTIGRAVITY_ISSUE_TEMPLATES_V1,
    }
)

_V2_REGISTRY_KEYS = frozenset({"id", "path", "extraction_method", "options"})
_METHOD_OPTION_KEYS: dict[str, frozenset[str]] = {
    EXTRACTION_CLAUDE_PLUGIN_SKILLS_V1: frozenset(),
    EXTRACTION_MARKDOWN_COMMAND_TREE_V1: frozenset(),
    EXTRACTION_TYPESCRIPT_AGENT_REGISTRY_V1: frozenset({"prompt_dir"}),
    EXTRACTION_COMMANDER_COMMAND_GRAPH_V1: frozenset(),
    EXTRACTION_CLAUDE_HOOKS_MANIFEST_V1: frozenset({"plugin_root"}),
    EXTRACTION_TYPESCRIPT_TOOL_FAMILY_GRAPH_V1: frozenset(),
    EXTRACTION_PACKAGE_SURFACE_V1: frozenset(
        {"governance_scripts", "required_files_roots", "include_bins"}
    ),
    EXTRACTION_OMX_CATALOG_MANIFEST_V1: frozenset(
        {"skills_dir", "prompts_dir"}
    ),
    EXTRACTION_OMX_HELP_SURFACE_V1: frozenset(),
    EXTRACTION_OMX_LAUNCHER_BIN_V1: frozenset({"bin_name"}),
    EXTRACTION_CODEX_PLUGIN_MANIFEST_V1: frozenset(),
    EXTRACTION_OMO_ZOD_STRING_ENUM_V1: frozenset(
        {
            "export_name",
            "kind",
            "surface_prefix",
            "emit_catalog",
            "catalog_kind",
        }
    ),
    EXTRACTION_OMO_COMMAND_TREE_V1: frozenset(),
    EXTRACTION_OMO_AGENT_NAMES_SCHEMA_V1: frozenset(),
    EXTRACTION_ANTIGRAVITY_README_CATALOG_V1: frozenset(),
    EXTRACTION_ANTIGRAVITY_CHANGELOG_RELEASES_V1: frozenset(),
    EXTRACTION_ANTIGRAVITY_EXAMPLES_TREE_V1: frozenset(),
    EXTRACTION_ANTIGRAVITY_ISSUE_TEMPLATES_V1: frozenset(),
}


def normalize_surface_id_case(surface_id: str, *, case_insensitive: bool) -> str:
    text = require_nonempty_string(surface_id, label="surface_id")
    return text.lower() if case_insensitive else text


def list_pin_tree_paths(
    root: Path,
    pin: str,
    *,
    git_blob_bytes: Callable[..., bytes],
    run_git: Callable[..., Any],
) -> list[tuple[str, str, str]]:
    """Return (mode, obj_type, path) for the pin tree (recursive)."""
    proc = run_git(root, ["ls-tree", "-r", "-z", pin])
    if proc.returncode != 0:
        raise ContractValidationError(
            f"git ls-tree failed for pin {pin}: {proc.stderr}"
        )
    entries: list[tuple[str, str, str]] = []
    for item in (proc.stdout or "").split("\0"):
        if not item:
            continue
        try:
            meta, path = item.split("\t", 1)
            mode, obj_type, _sha = meta.split(" ", 2)
        except ValueError as exc:
            raise ContractValidationError(
                f"unparseable ls-tree entry: {item!r}"
            ) from exc
        entries.append((mode, obj_type, path.replace("\\", "/")))
    return entries


def _require_relative_posix(path_text: str, *, label: str) -> str:
    text = require_nonempty_string(path_text, label=label)
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts or pure.parts[0] == "~":
        raise ContractValidationError(f"{label} must be a relative POSIX path")
    return text.replace("\\", "/")


def _category_for_kind(
    kind: str, category_assignment: Mapping[str, str], *, label: str
) -> str:
    if kind not in category_assignment:
        raise ContractValidationError(
            f"{label}: kind {kind!r} missing from category_assignment"
        )
    category = category_assignment[kind]
    if category not in PARITY_CATEGORY_TAXONOMY:
        raise ContractValidationError(
            f"{label}: unknown category {category!r} for kind {kind!r}"
        )
    return category


def validate_v2_registry_entry(
    item: Mapping[str, Any], *, index: int
) -> dict[str, Any]:
    label = f"authoritative_registries[{index}]"
    reg = require_object(item, label=label)
    require_exact_keys(reg, required=_V2_REGISTRY_KEYS, label=label)
    reg_id = require_safe_id(reg.get("id"), label=f"{label}.id")
    path = _require_relative_posix(str(reg["path"]), label=f"{label}.path")
    method = require_nonempty_string(
        reg.get("extraction_method"), label=f"{label}.extraction_method"
    )
    if method not in EXTRACTION_METHODS_V2:
        raise ContractValidationError(
            f"{label}: unsupported extraction_method {method!r}"
        )
    options_raw = reg.get("options")
    if not isinstance(options_raw, Mapping):
        raise ContractValidationError(f"{label}.options must be an object")
    allowed = _METHOD_OPTION_KEYS[method]
    unknown = sorted(set(options_raw) - allowed)
    if unknown:
        raise ContractValidationError(
            f"{label}.options has unknown keys for {method}: {','.join(unknown)}"
        )
    options: dict[str, Any] = {}
    for key, value in options_raw.items():
        key_s = require_nonempty_string(key, label=f"{label}.options.key")
        if key_s not in allowed:
            raise ContractValidationError(
                f"{label}.options key {key_s!r} not admitted by {method}"
            )
        options[key_s] = value
    # Normalize admitted option shapes.
    if method == EXTRACTION_TYPESCRIPT_AGENT_REGISTRY_V1:
        prompt_dir = options.get("prompt_dir", "agents")
        options["prompt_dir"] = _require_relative_posix(
            str(prompt_dir), label=f"{label}.options.prompt_dir"
        )
    if method == EXTRACTION_PACKAGE_SURFACE_V1:
        scripts = options.get("governance_scripts", [])
        if not isinstance(scripts, list) or not scripts:
            raise ContractValidationError(
                f"{label}.options.governance_scripts must be a non-empty list"
            )
        options["governance_scripts"] = sorted(
            {
                require_nonempty_string(s, label=f"{label}.governance_scripts[]")
                for s in scripts
            }
        )
        roots = options.get("required_files_roots", [])
        if not isinstance(roots, list) or not roots:
            raise ContractValidationError(
                f"{label}.options.required_files_roots must be a non-empty list"
            )
        options["required_files_roots"] = sorted(
            {
                _require_relative_posix(str(r), label=f"{label}.required_files_roots[]")
                for r in roots
            }
        )
        include_bins = options.get("include_bins", True)
        if "include_bins" in options_raw:
            if not isinstance(include_bins, bool):
                raise ContractValidationError(
                    f"{label}.options.include_bins must be a boolean"
                )
            options["include_bins"] = include_bins
        # When omitted, extractors default include_bins=True without rewriting policy.
    if method == EXTRACTION_CLAUDE_HOOKS_MANIFEST_V1:
        plugin_root = options.get("plugin_root")
        if plugin_root is not None:
            options["plugin_root"] = _require_relative_posix(
                str(plugin_root), label=f"{label}.options.plugin_root"
            ).rstrip("/")
    if method == EXTRACTION_OMX_CATALOG_MANIFEST_V1:
        skills_dir = options.get("skills_dir", "skills")
        prompts_dir = options.get("prompts_dir", "prompts")
        options["skills_dir"] = _require_relative_posix(
            str(skills_dir), label=f"{label}.options.skills_dir"
        ).rstrip("/")
        options["prompts_dir"] = _require_relative_posix(
            str(prompts_dir), label=f"{label}.options.prompts_dir"
        ).rstrip("/")
    if method == EXTRACTION_OMX_LAUNCHER_BIN_V1:
        bin_name = options.get("bin_name", "omx")
        options["bin_name"] = require_safe_id(
            bin_name, label=f"{label}.options.bin_name"
        )
    if method == EXTRACTION_OMO_ZOD_STRING_ENUM_V1:
        options["export_name"] = require_nonempty_string(
            options.get("export_name"), label=f"{label}.options.export_name"
        )
        options["kind"] = require_nonempty_string(
            options.get("kind"), label=f"{label}.options.kind"
        )
        options["surface_prefix"] = require_safe_id(
            options.get("surface_prefix"), label=f"{label}.options.surface_prefix"
        )
        emit_catalog = options.get("emit_catalog", False)
        if "emit_catalog" in options_raw and not isinstance(emit_catalog, bool):
            raise ContractValidationError(
                f"{label}.options.emit_catalog must be a boolean"
            )
        options["emit_catalog"] = bool(emit_catalog)
        if "catalog_kind" in options_raw or options.get("catalog_kind") is not None:
            options["catalog_kind"] = require_nonempty_string(
                options.get("catalog_kind", "catalog"),
                label=f"{label}.options.catalog_kind",
            )
        elif options["emit_catalog"]:
            options["catalog_kind"] = "catalog"
    return {
        "id": reg_id,
        "path": path,
        "extraction_method": method,
        "options": options,
    }


def _strip_ts_comments(text: str) -> str:
    """Remove // and /* */ comments outside of string literals (best-effort)."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                c = text[i]
                out.append(c)
                if c == "\\" and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                i += 2
                while i < n and text[i] not in "\n\r":
                    i += 1
                continue
            if nxt == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i = min(i + 2, n)
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_object_literal_keys(body: str) -> list[str]:
    """Parse top-level keys of a TS object literal body (no surrounding braces)."""
    keys: list[str] = []
    depth = 0
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if depth == 0:
            m_q = re.match(r"(['\"])((?:\\.|[^\\])*?)\1\s*:", body[i:])
            if m_q:
                key = m_q.group(2).encode("utf-8").decode("unicode_escape")
                keys.append(key)
                i += m_q.end()
                continue
            m_id = re.match(r"([A-Za-z_][\w-]*)\s*:", body[i:])
            if m_id:
                keys.append(m_id.group(1))
                i += m_id.end()
                continue
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < n:
                c = body[i]
                if c == "\\" and i + 1 < n:
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            continue
        if ch == "[":
            depth += 1
            i += 1
            continue
        if ch == "]":
            depth -= 1
            i += 1
            continue
        i += 1
    return keys


def _extract_get_agent_definitions_keys(source: str) -> list[str]:
    cleaned = _strip_ts_comments(source)
    marker = re.search(
        r"export\s+function\s+getAgentDefinitions\s*\(", cleaned
    )
    if not marker:
        raise ContractValidationError(
            "typescript_agent_registry_v1: getAgentDefinitions() not found"
        )
    # Prefer the agents record assigned inside the function.
    assign = re.search(
        r"const\s+agents\s*:\s*Record<[^>]+>\s*=\s*\{", cleaned[marker.start() :]
    )
    if not assign:
        raise ContractValidationError(
            "typescript_agent_registry_v1: agents record literal not found"
        )
    start_rel = marker.start() + assign.end() - 1  # points at '{'
    if cleaned[start_rel] != "{":
        raise ContractValidationError(
            "typescript_agent_registry_v1: agents assignment not an object literal"
        )
    depth = 0
    i = start_rel
    while i < len(cleaned):
        ch = cleaned[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < len(cleaned):
                c = cleaned[i]
                if c == "\\" and i + 1 < len(cleaned):
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body = cleaned[start_rel + 1 : i]
                keys = _parse_object_literal_keys(body)
                if not keys:
                    raise ContractValidationError(
                        "typescript_agent_registry_v1: empty agents registry"
                    )
                # Reject computed / spread keys leftovers
                if re.search(r"\[\s*[^\]]+\s*\]\s*:", body) or "..." in body:
                    # allow ... only inside nested values? Plan: computed/spread fail.
                    # Spread of values at top level looks like `...foo,` without key.
                    if re.search(r"(^|[,{])\s*\.\.\.", body) or re.search(
                        r"\[\s*[^\]]+\s*\]\s*:", body
                    ):
                        raise ContractValidationError(
                            "typescript_agent_registry_v1: computed/spread keys rejected"
                        )
                return keys
        i += 1
    raise ContractValidationError(
        "typescript_agent_registry_v1: unterminated agents object literal"
    )


def _parse_static_imports(source: str) -> dict[str, str]:
    """Map local binding → relative module path (without extension normalization)."""
    cleaned = _strip_ts_comments(source)
    imports: dict[str, str] = {}
    for m in re.finditer(
        r"import\s+\{([^}]+)\}\s+from\s+['\"](\.[^'\"]+)['\"]", cleaned
    ):
        names = m.group(1)
        mod = m.group(2)
        for part in names.split(","):
            part = part.strip()
            if not part:
                continue
            # Drop TypeScript `type` / `typeof` import modifiers.
            part = re.sub(r"^(type|typeof)\s+", "", part).strip()
            if not part:
                continue
            # support `foo as bar`
            bits = re.split(r"\s+as\s+", part)
            binding = bits[-1].strip()
            if not re.fullmatch(r"[A-Za-z_][\w]*", binding):
                raise ContractValidationError(
                    f"typescript import binding not a static identifier: {binding!r}"
                )
            imports[binding] = mod
    for m in re.finditer(
        r"import\s+\*\s+as\s+([A-Za-z_][\w]*)\s+from\s+['\"](\.[^'\"]+)['\"]",
        cleaned,
    ):
        raise ContractValidationError(
            "wildcard imports are not admitted for static discovery"
        )
    return imports


def _parse_all_tools_families(source: str) -> list[str]:
    cleaned = _strip_ts_comments(source)
    m = re.search(r"export\s+const\s+allTools\s*:\s*ToolDef\[\]\s*=\s*\[", cleaned)
    if not m:
        raise ContractValidationError(
            "typescript_tool_family_graph_v1: allTools array not found"
        )
    start = m.end() - 1
    depth = 0
    i = start
    while i < len(cleaned):
        ch = cleaned[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < len(cleaned):
                c = cleaned[i]
                if c == "\\" and i + 1 < len(cleaned):
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                body = cleaned[start + 1 : i]
                return _parse_all_tools_elements(body)
        i += 1
    raise ContractValidationError(
        "typescript_tool_family_graph_v1: unterminated allTools array"
    )


def _parse_all_tools_elements(body: str) -> list[str]:
    """Return registered family/singleton binding names from allTools elements."""
    elements: list[str] = []
    depth = 0
    i = 0
    token_start = 0
    n = len(body)

    def flush(segment: str) -> None:
        seg = segment.strip().rstrip(",")
        if not seg:
            return
        tag = re.fullmatch(
            r"\.\.\.\s*tagCategory\s*\(\s*([A-Za-z_][\w]*)\s+as\b[\s\S]*\)",
            seg,
        )
        if tag:
            elements.append(tag.group(1))
            return
        singleton = re.fullmatch(
            r"\{\s*\.\.\.\s*\(\s*([A-Za-z_][\w]*)\s+as\b[\s\S]*\}\s*,?\s*",
            seg,
        ) or re.fullmatch(
            r"\{\s*\.\.\.\s*\(\s*([A-Za-z_][\w]*)\s+as[\s\S]*\)\s*,\s*category\s*:[^}]+\}\s*",
            seg,
        )
        if singleton:
            elements.append(singleton.group(1))
            return
        # More permissive singleton: `{ ...(ident as unknown as ToolDef), category: ... }`
        singleton2 = re.match(
            r"\{\s*\.\.\.\s*\(\s*([A-Za-z_][\w]*)\s+as\b",
            seg,
        )
        if singleton2 and "category" in seg:
            elements.append(singleton2.group(1))
            return
        raise ContractValidationError(
            "typescript_tool_family_graph_v1: unsupported allTools element: "
            + seg[:120]
        )

    while i < n:
        ch = body[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < n:
                c = body[i]
                if c == "\\" and i + 1 < n:
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch in "{[(":
            depth += 1
            i += 1
            continue
        if ch in "}])":
            depth -= 1
            i += 1
            continue
        if ch == "," and depth == 0:
            flush(body[token_start:i])
            i += 1
            token_start = i
            continue
        i += 1
    flush(body[token_start:])
    if not elements:
        raise ContractValidationError(
            "typescript_tool_family_graph_v1: allTools has no admitted elements"
        )
    return elements


def _ts_call_argument(source: str, open_paren_end: int) -> tuple[str, int]:
    """Return (argument_text, index_after_closing_paren) for a call starting after '('."""
    depth = 1
    i = open_paren_end
    n = len(source)
    start = open_paren_end
    while i < n:
        ch = source[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < n:
                c = source[i]
                if c == "\\" and i + 1 < n:
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch in "{[(":
            depth += 1
            i += 1
            continue
        if ch in "}])":
            depth -= 1
            if depth == 0:
                return source[start:i].strip(), i + 1
            i += 1
            continue
        i += 1
    raise ContractValidationError(
        "commander_command_graph_v1: unterminated call argument list"
    )


def _validate_commander_token(token: str, *, label: str) -> str:
    text = token.split()[0] if token else ""
    if not text or not re.fullmatch(r"[A-Za-z0-9][\w:-]*", text):
        raise ContractValidationError(
            f"commander_command_graph_v1: invalid {label} {token!r}"
        )
    return text


def _resolve_local_command_binding(source: str, binding: str) -> str | None:
    local = re.search(
        rf"(?:const|let|var)\s+{re.escape(binding)}\s*=\s*new\s+Command\s*\(\s*(['\"])([^'\"]+)\1",
        source,
    )
    if not local:
        return None
    return _validate_commander_token(local.group(2), label="local Command name")


def _resolve_commander_factory_root_name(source: str, factory_name: str) -> str:
    """Resolve ``export function factory(): Command { ... return root }`` to root name."""
    cleaned = _strip_ts_comments(source)
    marker = re.search(
        rf"export\s+(?:async\s+)?function\s+{re.escape(factory_name)}\s*\(",
        cleaned,
    )
    if not marker:
        marker = re.search(
            rf"export\s+const\s+{re.escape(factory_name)}\s*=\s*"
            rf"(?:async\s*)?\([^)]*\)\s*(?::\s*[^=]+)?=>\s*{{",
            cleaned,
        )
    if not marker:
        raise ContractValidationError(
            f"commander_command_graph_v1: factory {factory_name!r} not found"
        )
    # Locate function body opening brace.
    brace = cleaned.find("{", marker.end() - 1)
    if brace < 0:
        raise ContractValidationError(
            f"commander_command_graph_v1: factory {factory_name!r} missing body"
        )
    depth = 0
    i = brace
    body_end = -1
    while i < len(cleaned):
        ch = cleaned[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < len(cleaned):
                c = cleaned[i]
                if c == "\\" and i + 1 < len(cleaned):
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body_end = i
                break
        i += 1
    if body_end < 0:
        raise ContractValidationError(
            f"commander_command_graph_v1: factory {factory_name!r} unterminated body"
        )
    body = cleaned[brace + 1 : body_end]
    ret = re.search(r"\breturn\s+([A-Za-z_][\w]*)\s*;?", body)
    if not ret:
        direct = re.search(
            r"\breturn\s+new\s+Command\s*\(\s*(['\"])([^'\"]+)\1",
            body,
        )
        if direct:
            return _validate_commander_token(
                direct.group(2), label="factory Command name"
            )
        raise ContractValidationError(
            f"commander_command_graph_v1: factory {factory_name!r} "
            "missing static return binding"
        )
    returned = ret.group(1)
    token = _resolve_local_command_binding(body, returned)
    if token is None:
        raise ContractValidationError(
            f"commander_command_graph_v1: factory {factory_name!r} "
            f"return binding {returned!r} is not a static Command"
        )
    return token


def _commander_command_names(source: str) -> list[str]:
    cleaned = _strip_ts_comments(source)
    # Reject dynamic construction patterns.
    if re.search(r"\.command\(\s*[^'\"`]", cleaned):
        # allow .command( with whitespace then quote only
        for m in re.finditer(r"\.command\s*\(\s*", cleaned):
            rest = cleaned[m.end() :]
            if not rest or rest[0] not in "'\"`":
                raise ContractValidationError(
                    "commander_command_graph_v1: dynamic command name rejected"
                )
    # Reject addCommand of non-static identifiers later via import resolution.
    names: list[str] = []
    for m in re.finditer(r"\.command\s*\(\s*(['\"])([^'\"]+)\1", cleaned):
        names.append(
            _validate_commander_token(m.group(2), label="command token")
        )
    # User-invocable Commander aliases are first-class surfaces.
    for m in re.finditer(r"\.alias\s*\(\s*", cleaned):
        rest = cleaned[m.end() :]
        if not rest or rest[0] not in "'\"`":
            raise ContractValidationError(
                "commander_command_graph_v1: dynamic alias rejected"
            )
        am = re.match(r"(['\"])([^'\"]+)\1", rest)
        if not am:
            raise ContractValidationError(
                "commander_command_graph_v1: dynamic alias rejected"
            )
        names.append(_validate_commander_token(am.group(2), label="alias token"))
    # Every .addCommand(...) must resolve statically or be rejected.
    imports = _parse_static_imports(cleaned)
    for m in re.finditer(r"\.addCommand\s*\(", cleaned):
        arg, _ = _ts_call_argument(cleaned, m.end())
        if not arg:
            raise ContractValidationError(
                "commander_command_graph_v1: empty addCommand argument rejected"
            )
        bare = re.fullmatch(r"([A-Za-z_][\w]*)", arg)
        if bare:
            binding = bare.group(1)
            if binding in imports:
                names.append(f"__import__:{binding}")
                continue
            token = _resolve_local_command_binding(cleaned, binding)
            if token is not None:
                names.append(token)
                continue
            raise ContractValidationError(
                f"commander_command_graph_v1: unresolved addCommand import {binding!r}"
            )
        factory = re.fullmatch(r"([A-Za-z_][\w]*)\s*\(\s*\)", arg)
        if factory:
            names.append(f"__factory__:{factory.group(1)}")
            continue
        raise ContractValidationError(
            "commander_command_graph_v1: non-static addCommand argument rejected: "
            + arg[:120]
        )
    if not names:
        raise ContractValidationError(
            "commander_command_graph_v1: no static .command() declarations found"
        )
    return names


def _hook_plugin_paths(
    command: str, *, plugin_root: str | None = None
) -> list[str]:
    """Extract relative repo paths from $CLAUDE_PLUGIN_ROOT / ${PLUGIN_ROOT}."""
    paths: list[str] = []
    patterns = (
        r'\$\{?CLAUDE_PLUGIN_ROOT\}?["\']?\s*/\s*["\']?([A-Za-z0-9_./-]+)',
        r'\$\{PLUGIN_ROOT\}["\']?\s*/\s*["\']?([A-Za-z0-9_./-]+)',
    )
    for pattern in patterns:
        for m in re.finditer(pattern, command):
            rel = m.group(1).replace("\\", "/")
            if plugin_root:
                rel = f"{plugin_root.rstrip('/')}/{rel.lstrip('./')}"
            paths.append(rel)
    if not paths:
        raise ContractValidationError(
            "claude_hooks_manifest_v1: hook command missing "
            "$CLAUDE_PLUGIN_ROOT or ${PLUGIN_ROOT} path"
        )
    for p in paths:
        _require_relative_posix(p, label="hook.command_path")
    return paths


def extract_claude_plugin_skills_v1(
    *,
    registry_path: str,
    registry_bytes: bytes,
    category_assignment: Mapping[str, str],
    pin_paths: set[str],
    file_digest: Callable[[bytes], str],
    read_blob: Callable[[str], bytes],
    exceptions: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    try:
        payload = json.loads(registry_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            f"{registry_path} is not valid UTF-8 JSON: {exc}"
        ) from exc
    body = require_object(payload, label=registry_path)
    skills = body.get("skills")
    if not isinstance(skills, list) or not skills:
        raise ContractValidationError(
            f"{registry_path}.skills must be a non-empty array"
        )
    category = _category_for_kind("skill", category_assignment, label=registry_path)
    catalog_category = _category_for_kind(
        "catalog", category_assignment, label=registry_path
    )
    reg_digest = file_digest(registry_bytes)
    surfaces: list[dict[str, Any]] = []
    input_parts: list[dict[str, str]] = [
        {"path": registry_path, "content_digest": reg_digest}
    ]
    declared_skill_files: set[str] = set()
    seen_names: set[str] = set()
    for idx, raw in enumerate(skills):
        if not isinstance(raw, str) or not raw.strip():
            raise ContractValidationError(
                f"{registry_path}.skills[{idx}] must be a relative directory string"
            )
        rel_dir = raw.strip().lstrip("./").rstrip("/")
        rel_dir = _require_relative_posix(rel_dir, label=f"{registry_path}.skills[{idx}]")
        name = PurePosixPath(rel_dir).name
        norm = name.lower()
        if norm in seen_names:
            raise ContractValidationError(
                f"duplicate normalized skill name: {name}"
            )
        seen_names.add(norm)
        skill_file = f"{rel_dir}/SKILL.md"
        if skill_file not in pin_paths:
            raise ContractValidationError(
                f"declared skill missing at pin: {skill_file}"
            )
        declared_skill_files.add(skill_file)
        raw_skill = read_blob(skill_file)
        surfaces.append(
            {
                "surface_id": f"skill.{name}",
                "kind": "skill",
                "category": category,
                "source_path": skill_file,
                "anchor": f"skill:{name}",
                "content_digest": file_digest(raw_skill),
            }
        )
        input_parts.append(
            {"path": skill_file, "content_digest": file_digest(raw_skill)}
        )

    # Cross-check direct skills/*/SKILL.md tree.
    for path in sorted(pin_paths):
        parts = PurePosixPath(path).parts
        if (
            len(parts) == 3
            and parts[0] == "skills"
            and parts[2] == "SKILL.md"
        ):
            if path not in declared_skill_files and path not in exceptions:
                raise ContractValidationError(
                    f"undeclared skill file present in tree: {path}"
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
    return surfaces, input_parts


def extract_markdown_command_tree_v1(
    *,
    registry_path: str,
    category_assignment: Mapping[str, str],
    pin_paths: set[str],
    file_digest: Callable[[bytes], str],
    read_blob: Callable[[str], bytes],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Enumerate direct commands/*.md under registry_path directory."""
    root_dir = registry_path.rstrip("/")
    category = _category_for_kind("command", category_assignment, label=root_dir)
    surfaces: list[dict[str, Any]] = []
    input_parts: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in sorted(pin_paths):
        parts = PurePosixPath(path).parts
        if len(parts) != 2 or parts[0] != root_dir or not parts[1].endswith(".md"):
            continue
        stem = parts[1][:-3]
        if not stem or not re.fullmatch(r"[A-Za-z0-9][\w-]*", stem):
            raise ContractValidationError(
                f"markdown_command_tree_v1: invalid command stem in {path}"
            )
        norm = stem.lower()
        if norm in seen:
            raise ContractValidationError(
                f"markdown_command_tree_v1: case-colliding command stem {stem}"
            )
        seen.add(norm)
        raw = read_blob(path)
        surfaces.append(
            {
                "surface_id": f"command.{stem}",
                "kind": "command",
                "category": category,
                "source_path": path,
                "anchor": f"command:{stem}",
                "content_digest": file_digest(raw),
            }
        )
        input_parts.append({"path": path, "content_digest": file_digest(raw)})
    if not surfaces:
        raise ContractValidationError(
            f"markdown_command_tree_v1: no commands under {root_dir}/"
        )
    return surfaces, input_parts


def extract_typescript_agent_registry_v1(
    *,
    registry_path: str,
    registry_bytes: bytes,
    category_assignment: Mapping[str, str],
    pin_paths: set[str],
    file_digest: Callable[[bytes], str],
    read_blob: Callable[[str], bytes],
    options: Mapping[str, Any],
    exceptions: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    try:
        text = registry_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractValidationError(
            f"{registry_path} is not valid UTF-8: {exc}"
        ) from exc
    keys = _extract_get_agent_definitions_keys(text)
    category = _category_for_kind("agent", category_assignment, label=registry_path)
    catalog_category = _category_for_kind(
        "catalog", category_assignment, label=registry_path
    )
    # catalog.agents uses agents_routing — override catalog default via kind agent_catalog if present
    if "agent_catalog" in category_assignment:
        catalog_category = _category_for_kind(
            "agent_catalog", category_assignment, label=registry_path
        )
    prompt_dir = str(options.get("prompt_dir", "agents"))
    reg_digest = file_digest(registry_bytes)
    surfaces: list[dict[str, Any]] = []
    input_parts: list[dict[str, str]] = [
        {"path": registry_path, "content_digest": reg_digest}
    ]
    seen: set[str] = set()
    registered_prompts: set[str] = set()
    for key in keys:
        norm = key.lower()
        if norm in seen:
            raise ContractValidationError(
                f"typescript_agent_registry_v1: duplicate normalized key {key}"
            )
        seen.add(norm)
        prompt = f"{prompt_dir}/{key}.md"
        if prompt not in pin_paths:
            raise ContractValidationError(
                f"typescript_agent_registry_v1: missing agent prompt {prompt}"
            )
        registered_prompts.add(prompt)
        raw = read_blob(prompt)
        surfaces.append(
            {
                "surface_id": f"agent.{key}",
                "kind": "agent",
                "category": category,
                "source_path": prompt,
                "anchor": f"agent:{key}",
                "content_digest": file_digest(raw),
            }
        )
        input_parts.append({"path": prompt, "content_digest": file_digest(raw)})

    # Every direct agents/*.md must be registered or excepted.
    for path in sorted(pin_paths):
        parts = PurePosixPath(path).parts
        if (
            len(parts) == 2
            and parts[0] == prompt_dir
            and parts[1].endswith(".md")
        ):
            if path not in registered_prompts and path not in exceptions:
                raise ContractValidationError(
                    f"unregistered agent prompt present: {path}"
                )

    surfaces.append(
        {
            "surface_id": "catalog.agents",
            "kind": "catalog",
            "category": catalog_category,
            "source_path": registry_path,
            "anchor": "catalog:agents",
            "content_digest": reg_digest,
        }
    )
    return surfaces, input_parts


def extract_commander_command_graph_v1(
    *,
    registry_path: str,
    registry_bytes: bytes,
    category_assignment: Mapping[str, str],
    file_digest: Callable[[bytes], str],
    read_blob: Callable[[str], bytes],
    resolve_import_path: Callable[[str, str], str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    try:
        text = registry_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractValidationError(
            f"{registry_path} is not valid UTF-8: {exc}"
        ) from exc
    category = _category_for_kind("cli", category_assignment, label=registry_path)
    names = _commander_command_names(text)
    imports = _parse_static_imports(_strip_ts_comments(text))
    expanded: list[str] = []
    input_parts: list[dict[str, str]] = [
        {"path": registry_path, "content_digest": file_digest(registry_bytes)}
    ]
    for name in names:
        if name.startswith("__import__:"):
            binding = name.split(":", 1)[1]
            if binding not in imports:
                raise ContractValidationError(
                    f"commander_command_graph_v1: unresolved import {binding}"
                )
            mod_rel = resolve_import_path(registry_path, imports[binding])
            mod_bytes = read_blob(mod_rel)
            input_parts.append(
                {"path": mod_rel, "content_digest": file_digest(mod_bytes)}
            )
            mod_names = [
                n
                for n in _commander_command_names(mod_bytes.decode("utf-8"))
                if not n.startswith(("__import__:", "__factory__:"))
            ]
            # Also accept `.name('x')` on Command objects
            cleaned = _strip_ts_comments(mod_bytes.decode("utf-8"))
            for m in re.finditer(r"\.name\s*\(\s*(['\"])([^'\"]+)\1", cleaned):
                mod_names.append(
                    _validate_commander_token(m.group(2), label="command name")
                )
            if not mod_names:
                raise ContractValidationError(
                    f"commander_command_graph_v1: imported module {mod_rel} "
                    "has no static command name"
                )
            expanded.extend(mod_names)
        elif name.startswith("__factory__:"):
            binding = name.split(":", 1)[1]
            if binding not in imports:
                # Local factory in the same file.
                try:
                    expanded.append(
                        _resolve_commander_factory_root_name(text, binding)
                    )
                except ContractValidationError as exc:
                    raise ContractValidationError(
                        f"commander_command_graph_v1: unresolved addCommand "
                        f"factory {binding!r}"
                    ) from exc
                continue
            mod_rel = resolve_import_path(registry_path, imports[binding])
            mod_bytes = read_blob(mod_rel)
            input_parts.append(
                {"path": mod_rel, "content_digest": file_digest(mod_bytes)}
            )
            expanded.append(
                _resolve_commander_factory_root_name(
                    mod_bytes.decode("utf-8"), binding
                )
            )
        else:
            expanded.append(name)

    surfaces: list[dict[str, Any]] = []
    seen: set[str] = set()
    for token in expanded:
        norm = token.lower()
        if norm in seen:
            raise ContractValidationError(
                f"commander_command_graph_v1: duplicate command id {token}"
            )
        seen.add(norm)
        surfaces.append(
            {
                "surface_id": f"cli.{token}",
                "kind": "cli",
                "category": category,
                "source_path": registry_path,
                "anchor": f"cli:{token}",
                "content_digest": file_digest(registry_bytes),
            }
        )
    return surfaces, input_parts


def extract_claude_hooks_manifest_v1(
    *,
    registry_path: str,
    registry_bytes: bytes,
    category_assignment: Mapping[str, str],
    pin_paths: set[str],
    file_digest: Callable[[bytes], str],
    read_blob: Callable[[str], bytes],
    options: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    try:
        payload = json.loads(registry_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            f"{registry_path} is not valid UTF-8 JSON: {exc}"
        ) from exc
    body = require_object(payload, label=registry_path)
    hooks_root = body.get("hooks")
    if not isinstance(hooks_root, Mapping) or not hooks_root:
        raise ContractValidationError(
            f"{registry_path}.hooks must be a non-empty object"
        )
    plugin_root = None
    if options:
        raw_root = options.get("plugin_root")
        if raw_root is not None:
            plugin_root = str(raw_root).rstrip("/")
    category = _category_for_kind("hook", category_assignment, label=registry_path)
    surfaces: list[dict[str, Any]] = []
    input_parts: list[dict[str, str]] = [
        {"path": registry_path, "content_digest": file_digest(registry_bytes)}
    ]
    seen: set[str] = set()
    for event, matchers in hooks_root.items():
        event_s = require_nonempty_string(event, label="hook.event")
        if not isinstance(matchers, list):
            raise ContractValidationError(
                f"hooks[{event_s}] must be a list of matcher objects"
            )
        for idx, matcher_obj in enumerate(matchers):
            mobj = require_object(matcher_obj, label=f"hooks[{event_s}][{idx}]")
            raw_matcher = mobj.get("matcher")
            if raw_matcher is None:
                matcher = "*"
            else:
                matcher = require_nonempty_string(
                    raw_matcher, label=f"hooks[{event_s}][{idx}].matcher"
                )
            hook_list = mobj.get("hooks")
            if not isinstance(hook_list, list) or not hook_list:
                raise ContractValidationError(
                    f"hooks[{event_s}][{idx}].hooks must be a non-empty list"
                )
            for h_idx, hook in enumerate(hook_list):
                hobj = require_object(
                    hook, label=f"hooks[{event_s}][{idx}].hooks[{h_idx}]"
                )
                if hobj.get("type") != "command":
                    raise ContractValidationError(
                        f"hooks[{event_s}][{idx}].hooks[{h_idx}]: "
                        "only type=command admitted"
                    )
                command = hobj.get("command")
                if not isinstance(command, str) or not command.strip():
                    raise ContractValidationError(
                        f"hooks[{event_s}][{idx}].hooks[{h_idx}].command "
                        "must be a string"
                    )
                paths = _hook_plugin_paths(command, plugin_root=plugin_root)
                script = paths[-1]
                if script not in pin_paths:
                    raise ContractValidationError(
                        f"hook script missing at pin: {script}"
                    )
                basename = PurePosixPath(script).name
                matcher_token = matcher.replace("|", "+")
                if not matcher_token or any(
                    ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._*:@+/-"
                    for ch in matcher_token
                ):
                    raise ContractValidationError(
                        f"hooks[{event_s}][{idx}]: matcher {matcher!r} not encodable as surface_id"
                    )
                surface_id = f"hook.{event_s}.{matcher_token}.{basename}"
                norm = surface_id.lower()
                if norm in seen:
                    raise ContractValidationError(
                        f"duplicate hook surface: {surface_id}"
                    )
                seen.add(norm)
                raw = read_blob(script)
                digest = file_digest(raw)
                surfaces.append(
                    {
                        "surface_id": surface_id,
                        "kind": "hook",
                        "category": category,
                        "source_path": script,
                        "anchor": surface_id,
                        "content_digest": digest,
                    }
                )
                input_parts.append({"path": script, "content_digest": digest})
    if not surfaces:
        raise ContractValidationError("claude_hooks_manifest_v1: no hook surfaces")
    return surfaces, input_parts


def extract_typescript_tool_family_graph_v1(
    *,
    registry_path: str,
    registry_bytes: bytes,
    category_assignment: Mapping[str, str],
    file_digest: Callable[[bytes], str],
    read_blob: Callable[[str], bytes],
    resolve_import_path: Callable[[str, str], str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    try:
        text = registry_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractValidationError(
            f"{registry_path} is not valid UTF-8: {exc}"
        ) from exc
    category = _category_for_kind(
        "mcp-family", category_assignment, label=registry_path
    )
    families = _parse_all_tools_families(text)
    imports = _parse_static_imports(_strip_ts_comments(text))
    reg_digest = file_digest(registry_bytes)
    surfaces: list[dict[str, Any]] = []
    input_parts: list[dict[str, str]] = [
        {"path": registry_path, "content_digest": reg_digest}
    ]
    seen: set[str] = set()
    for binding in families:
        norm = binding.lower()
        if norm in seen:
            raise ContractValidationError(
                f"duplicate mcp family binding: {binding}"
            )
        seen.add(norm)
        if binding not in imports:
            raise ContractValidationError(
                f"typescript_tool_family_graph_v1: unresolved import for {binding}"
            )
        mod_rel = resolve_import_path(registry_path, imports[binding])
        mod_bytes = read_blob(mod_rel)
        mod_digest = file_digest(mod_bytes)
        input_parts.append({"path": mod_rel, "content_digest": mod_digest})
        surfaces.append(
            {
                "surface_id": f"mcp-family.{binding}",
                "kind": "mcp-family",
                "category": category,
                "source_path": mod_rel,
                "anchor": f"mcp-family:{binding}",
                "content_digest": mod_digest,
            }
        )
    return surfaces, input_parts


def extract_package_surface_v1_full(
    *,
    registry_path: str,
    registry_bytes: bytes,
    category_assignment: Mapping[str, str],
    pin_paths: set[str],
    file_digest: Callable[[bytes], str],
    read_blob: Callable[[str], bytes],
    options: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    try:
        payload = json.loads(registry_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            f"{registry_path} is not valid UTF-8 JSON: {exc}"
        ) from exc
    body = require_object(payload, label=registry_path)
    bin_category = _category_for_kind("bin", category_assignment, label=registry_path)
    script_category = _category_for_kind(
        "npm-script", category_assignment, label=registry_path
    )
    reg_digest = file_digest(registry_bytes)
    surfaces: list[dict[str, Any]] = []
    input_parts: list[dict[str, str]] = [
        {"path": registry_path, "content_digest": reg_digest}
    ]
    bins = body.get("bin")
    include_bins = bool(options.get("include_bins", True))
    if include_bins:
        if not isinstance(bins, Mapping) or not bins:
            raise ContractValidationError(
                f"{registry_path}.bin must be a non-empty object"
            )
        seen: set[str] = set()
        for name, target in bins.items():
            name_s = require_nonempty_string(name, label="bin.name")
            norm = name_s.lower()
            if norm in seen:
                raise ContractValidationError(f"duplicate bin name: {name_s}")
            seen.add(norm)
            target_s = _require_relative_posix(str(target), label=f"bin[{name_s}]")
            if target_s not in pin_paths:
                raise ContractValidationError(f"bin target missing at pin: {target_s}")
            raw = read_blob(target_s)
            digest = file_digest(raw)
            surfaces.append(
                {
                    "surface_id": f"bin.{name_s}",
                    "kind": "bin",
                    "category": bin_category,
                    "source_path": target_s,
                    "anchor": f"bin:{name_s}",
                    "content_digest": digest,
                }
            )
            input_parts.append({"path": target_s, "content_digest": digest})
    elif bins is not None and not isinstance(bins, Mapping):
        raise ContractValidationError(f"{registry_path}.bin must be an object when present")

    scripts = body.get("scripts")
    if not isinstance(scripts, Mapping):
        raise ContractValidationError(f"{registry_path}.scripts must be an object")
    for script_name in options["governance_scripts"]:
        if script_name not in scripts:
            raise ContractValidationError(
                f"governance script missing from package.json: {script_name}"
            )
        surfaces.append(
            {
                "surface_id": f"npm-script.{script_name}",
                "kind": "npm-script",
                "category": script_category,
                "source_path": registry_path,
                "anchor": f"npm-script:{script_name}",
                "content_digest": reg_digest,
            }
        )

    files = body.get("files")
    if not isinstance(files, list):
        raise ContractValidationError(f"{registry_path}.files must be a list")
    files_set = {str(x) for x in files}
    for root_name in options["required_files_roots"]:
        # root may be a directory name or exact path entry
        ok = root_name in files_set or any(
            f == root_name or f.startswith(root_name.rstrip("/") + "/")
            for f in files_set
        )
        if not ok:
            raise ContractValidationError(
                f"package.json files[] missing required root {root_name!r}"
            )
    return surfaces, input_parts


def _resolve_ts_import(importer: str, spec: str, pin_paths: set[str]) -> str:
    """Resolve a relative TS import to a pin path (.ts preferred over .js)."""
    base_dir = str(PurePosixPath(importer).parent)
    joined = str(PurePosixPath(base_dir) / spec)
    # normalize ./ and ../
    parts: list[str] = []
    for part in PurePosixPath(joined).parts:
        if part == "..":
            if not parts:
                raise ContractValidationError(
                    f"import escapes repo: {spec} from {importer}"
                )
            parts.pop()
        elif part in (".", ""):
            continue
        else:
            parts.append(part)
    rel = "/".join(parts)
    candidates = [
        rel,
        rel + ".ts",
        rel + ".tsx",
        rel + ".js",
        rel + "/index.ts",
        rel + "/index.js",
    ]
    # If spec ends with .js, also try .ts (OMC style)
    if rel.endswith(".js"):
        candidates.insert(0, rel[:-3] + ".ts")
    for cand in candidates:
        if cand in pin_paths:
            mode_ok = True
            if mode_ok:
                return cand
    raise ContractValidationError(
        f"unresolved static import {spec!r} from {importer} (tried {candidates})"
    )


def extract_surfaces_v2(
    *,
    registries: Sequence[Mapping[str, Any]],
    category_assignment: Mapping[str, str],
    exceptions: Sequence[Mapping[str, str]],
    pin_paths: set[str],
    file_digest: Callable[[bytes], str],
    read_blob: Callable[[str], bytes],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Run all v2 registries; return (surfaces, source_input_parts)."""
    exc_paths = {
        _require_relative_posix(e["path"], label="exception.path") for e in exceptions
    }
    # Exceptions must exist at pin.
    for path in sorted(exc_paths):
        if path not in pin_paths:
            raise ContractValidationError(
                f"non_surface_exception path absent at pin: {path}"
            )

    all_surfaces: list[dict[str, Any]] = []
    all_inputs: list[dict[str, str]] = []
    consumed_exceptions: set[str] = set()

    def mark_exception_consumed(path: str) -> None:
        if path in exc_paths:
            consumed_exceptions.add(path)

    for reg in registries:
        method = reg["extraction_method"]
        path = reg["path"]
        options = reg["options"]
        if method == EXTRACTION_MARKDOWN_COMMAND_TREE_V1:
            # registry path is the directory
            surfaces, inputs = extract_markdown_command_tree_v1(
                registry_path=path,
                category_assignment=category_assignment,
                pin_paths=pin_paths,
                file_digest=file_digest,
                read_blob=read_blob,
            )
        elif method == EXTRACTION_OMO_COMMAND_TREE_V1:
            from omg_cli.parity_discovery_omo import extract_omo_command_tree_v1

            surfaces, inputs = extract_omo_command_tree_v1(
                registry_path=path,
                category_assignment=category_assignment,
                pin_paths=pin_paths,
                file_digest=file_digest,
                read_blob=read_blob,
            )
        elif method == EXTRACTION_ANTIGRAVITY_EXAMPLES_TREE_V1:
            from omg_cli.parity_discovery_antigravity import (
                extract_antigravity_examples_tree_v1,
            )

            surfaces, inputs = extract_antigravity_examples_tree_v1(
                registry_path=path,
                category_assignment=category_assignment,
                pin_paths=pin_paths,
                file_digest=file_digest,
                read_blob=read_blob,
                options=options,
            )
        elif method == EXTRACTION_ANTIGRAVITY_ISSUE_TEMPLATES_V1:
            from omg_cli.parity_discovery_antigravity import (
                extract_antigravity_issue_templates_v1,
            )

            surfaces, inputs = extract_antigravity_issue_templates_v1(
                registry_path=path,
                category_assignment=category_assignment,
                pin_paths=pin_paths,
                file_digest=file_digest,
                read_blob=read_blob,
                options=options,
            )
        else:
            if path not in pin_paths:
                raise ContractValidationError(
                    f"authoritative registry missing at pin: {path}"
                )
            raw = read_blob(path)

            if method == EXTRACTION_CLAUDE_PLUGIN_SKILLS_V1:
                surfaces, inputs = extract_claude_plugin_skills_v1(
                    registry_path=path,
                    registry_bytes=raw,
                    category_assignment=category_assignment,
                    pin_paths=pin_paths,
                    file_digest=file_digest,
                    read_blob=read_blob,
                    exceptions=exc_paths,
                )
                for e in exc_paths:
                    parts = PurePosixPath(e).parts
                    if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
                        mark_exception_consumed(e)
            elif method == EXTRACTION_TYPESCRIPT_AGENT_REGISTRY_V1:
                surfaces, inputs = extract_typescript_agent_registry_v1(
                    registry_path=path,
                    registry_bytes=raw,
                    category_assignment=category_assignment,
                    pin_paths=pin_paths,
                    file_digest=file_digest,
                    read_blob=read_blob,
                    options=options,
                    exceptions=exc_paths,
                )
                prompt_dir = str(options.get("prompt_dir", "agents"))
                for e in exc_paths:
                    parts = PurePosixPath(e).parts
                    if (
                        len(parts) == 2
                        and parts[0] == prompt_dir
                        and parts[1].endswith(".md")
                    ):
                        mark_exception_consumed(e)
            elif method == EXTRACTION_COMMANDER_COMMAND_GRAPH_V1:
                surfaces, inputs = extract_commander_command_graph_v1(
                    registry_path=path,
                    registry_bytes=raw,
                    category_assignment=category_assignment,
                    file_digest=file_digest,
                    read_blob=read_blob,
                    resolve_import_path=lambda imp, spec: _resolve_ts_import(
                        imp, spec, pin_paths
                    ),
                )
            elif method == EXTRACTION_CLAUDE_HOOKS_MANIFEST_V1:
                surfaces, inputs = extract_claude_hooks_manifest_v1(
                    registry_path=path,
                    registry_bytes=raw,
                    category_assignment=category_assignment,
                    pin_paths=pin_paths,
                    file_digest=file_digest,
                    read_blob=read_blob,
                    options=options,
                )
            elif method == EXTRACTION_TYPESCRIPT_TOOL_FAMILY_GRAPH_V1:
                surfaces, inputs = extract_typescript_tool_family_graph_v1(
                    registry_path=path,
                    registry_bytes=raw,
                    category_assignment=category_assignment,
                    file_digest=file_digest,
                    read_blob=read_blob,
                    resolve_import_path=lambda imp, spec: _resolve_ts_import(
                        imp, spec, pin_paths
                    ),
                )
            elif method == EXTRACTION_PACKAGE_SURFACE_V1:
                surfaces, inputs = extract_package_surface_v1_full(
                    registry_path=path,
                    registry_bytes=raw,
                    category_assignment=category_assignment,
                    pin_paths=pin_paths,
                    file_digest=file_digest,
                    read_blob=read_blob,
                    options=options,
                )
            elif method == EXTRACTION_OMX_CATALOG_MANIFEST_V1:
                from omg_cli.parity_discovery_omx import extract_omx_catalog_manifest_v1

                surfaces, inputs, consumed = extract_omx_catalog_manifest_v1(
                    registry_path=path,
                    registry_bytes=raw,
                    category_assignment=category_assignment,
                    pin_paths=pin_paths,
                    file_digest=file_digest,
                    read_blob=read_blob,
                    options=options,
                    exceptions=exc_paths,
                )
                consumed_exceptions.update(consumed)
            elif method == EXTRACTION_OMX_HELP_SURFACE_V1:
                from omg_cli.parity_discovery_omx import extract_omx_help_surface_v1

                surfaces, inputs = extract_omx_help_surface_v1(
                    registry_path=path,
                    registry_bytes=raw,
                    category_assignment=category_assignment,
                    file_digest=file_digest,
                )
            elif method == EXTRACTION_OMX_LAUNCHER_BIN_V1:
                from omg_cli.parity_discovery_omx import extract_omx_launcher_bin_v1

                surfaces, inputs = extract_omx_launcher_bin_v1(
                    registry_path=path,
                    registry_bytes=raw,
                    category_assignment=category_assignment,
                    file_digest=file_digest,
                    options=options,
                )
            elif method == EXTRACTION_CODEX_PLUGIN_MANIFEST_V1:
                from omg_cli.parity_discovery_omx import extract_codex_plugin_manifest_v1

                surfaces, inputs = extract_codex_plugin_manifest_v1(
                    registry_path=path,
                    registry_bytes=raw,
                    category_assignment=category_assignment,
                    pin_paths=pin_paths,
                    file_digest=file_digest,
                )
            elif method == EXTRACTION_OMO_ZOD_STRING_ENUM_V1:
                from omg_cli.parity_discovery_omo import extract_omo_zod_string_enum_v1

                surfaces, inputs = extract_omo_zod_string_enum_v1(
                    registry_path=path,
                    registry_bytes=raw,
                    category_assignment=category_assignment,
                    file_digest=file_digest,
                    options=options,
                )
            elif method == EXTRACTION_OMO_AGENT_NAMES_SCHEMA_V1:
                from omg_cli.parity_discovery_omo import extract_omo_agent_names_schema_v1

                surfaces, inputs = extract_omo_agent_names_schema_v1(
                    registry_path=path,
                    registry_bytes=raw,
                    category_assignment=category_assignment,
                    file_digest=file_digest,
                    options=options,
                )
            elif method == EXTRACTION_ANTIGRAVITY_README_CATALOG_V1:
                from omg_cli.parity_discovery_antigravity import (
                    extract_antigravity_readme_catalog_v1,
                )

                surfaces, inputs = extract_antigravity_readme_catalog_v1(
                    registry_path=path,
                    registry_bytes=raw,
                    category_assignment=category_assignment,
                    file_digest=file_digest,
                    options=options,
                )
            elif method == EXTRACTION_ANTIGRAVITY_CHANGELOG_RELEASES_V1:
                from omg_cli.parity_discovery_antigravity import (
                    extract_antigravity_changelog_releases_v1,
                )

                surfaces, inputs = extract_antigravity_changelog_releases_v1(
                    registry_path=path,
                    registry_bytes=raw,
                    category_assignment=category_assignment,
                    file_digest=file_digest,
                    options=options,
                )
            else:
                raise ContractValidationError(f"unhandled extraction_method {method}")

        all_surfaces.extend(surfaces)
        all_inputs.extend(inputs)

    # Stale exceptions: every exception must be consumed by exactly one registry.
    # Path-level exceptions for README etc. are consumed if referenced as
    # non-surface — mark generic path exceptions as consumed when present.
    for e in exceptions:
        path = e["path"]
        if path in consumed_exceptions:
            continue
        # Allow exceptions that simply exclude non-registered paths from closure
        # audits (README, docs): mark consumed if the path is not a skill/agent
        # hidden child that needed suppression — still require consumption.
        # For reviewed narrative paths, consume when listed.
        if PurePosixPath(path).name in {"README.md"} or path.startswith("docs/"):
            consumed_exceptions.add(path)
            continue
        raise ContractValidationError(
            f"stale non_surface_exception not consumed by any registry: {path}"
        )

    # Deduplicate input parts by path (last digest wins — must agree).
    by_path: dict[str, str] = {}
    for part in all_inputs:
        prev = by_path.get(part["path"])
        if prev is not None and prev != part["content_digest"]:
            raise ContractValidationError(
                f"conflicting content digest for {part['path']}"
            )
        by_path[part["path"]] = part["content_digest"]
    merged_inputs = [
        {"path": p, "content_digest": by_path[p]} for p in sorted(by_path)
    ]

    # Surface ID uniqueness (case-sensitive preserve; reject case collisions).
    seen_exact: set[str] = set()
    seen_norm: set[str] = set()
    for surface in all_surfaces:
        sid = surface["surface_id"]
        if sid in seen_exact:
            raise ContractValidationError(f"duplicate surface_id: {sid}")
        norm = sid.lower()
        if norm in seen_norm:
            raise ContractValidationError(
                f"case-colliding surface_id: {sid}"
            )
        seen_exact.add(sid)
        seen_norm.add(norm)

    all_surfaces.sort(key=lambda s: s["surface_id"])
    return all_surfaces, merged_inputs
