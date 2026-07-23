#!/usr/bin/env python3
"""Generate / check omg_capabilities.lock.json for the LOCAL CHECKOUT.

Hashes skills/omg-*/SKILL.md and agents/omg-*.md under the repo (or --root)
and writes/checks omg_capabilities.lock.json. This is a commit-hygiene / CI
guard: it catches uncommitted or unregenerated local skill/agent edits against
the committed lock. Installed frozen-snapshot drift (under
~/.grok/installed-plugins) is checked separately by doctor
``check_installed_capabilities_lock`` via ``compute_lock_for``.

Usage:
  python3 scripts/generate_capabilities_lock.py          # rewrite lock
  python3 scripts/generate_capabilities_lock.py --check   # exit 1 if stale
  python3 scripts/generate_capabilities_lock.py --root PATH
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Final, cast


LOCK_NAME = "omg_capabilities.lock.json"
SESSION_SURFACE_SCHEMA: Final = "omg-plugin-session-surface/v1"
MAX_SURFACE_SOURCE_BYTES: Final = 2_097_152
MCP_SOURCE: Final = "omg_cli/mcp/tools.py"
LSP_SOURCE: Final = "omg_cli/lsp_tools.py"
MCP_REGISTRATION: Final = ".mcp.json"
LSP_REGISTRATION: Final = ".lsp.json"
WORKFLOW_CONTRACT_SOURCE: Final = "omg_cli/contracts/workflow_contract.py"
WORKFLOW_NATIVE_SOURCE: Final = "omg_cli/workflows/grok_adapter.py"
ADVISOR_SOURCE: Final = "omg_cli/ask/providers.py"
ROLES_SOURCE: Final = "omg_cli/team/roles.py"
# Validators only: claims are always extracted from the installed source bytes.
EXPECTED_MCP_OPERATIONS: Final = (
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
EXPECTED_WORKFLOW_CONTRACT: Final = "repository-workflow/v1"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _frontmatter(path: Path) -> tuple[dict[str, str], str]:
    """Parse the small scalar subset used by skill/agent frontmatter.

    No YAML dependency is required.  Missing or malformed metadata is reported
    explicitly and the inventory falls back to the path-derived name.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}, "unreadable"
    if not lines or lines[0].strip() != "---":
        return {}, "missing"
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        return {}, "malformed"
    fields: dict[str, str] = {}
    malformed = False
    for line in lines[1:end]:
        if not line.strip() or line[:1].isspace() or line.lstrip().startswith("-"):
            continue
        if ":" not in line:
            malformed = True
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if not key or key in fields:
            malformed = True
            continue
        scalar = value.strip()
        if len(scalar) >= 2 and scalar[0] == scalar[-1] and scalar[0] in {"'", '"'}:
            scalar = scalar[1:-1]
        fields[key] = scalar
    return fields, "malformed" if malformed else "valid"


def _surface_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _surface_issue(
    issues: list[dict[str, str]], path: str, code: str, detail: str
) -> None:
    issues.append({"path": path, "code": code, "detail": detail[:256]})


def _python_source(
    root: Path,
    relative: str,
    *,
    surface: str,
    issues: list[dict[str, str]],
    bindings: list[dict[str, Any]],
) -> tuple[ast.Module | None, dict[str, Any]]:
    path = root / relative
    binding: dict[str, Any] = {
        "surface": surface,
        "path": relative,
        "sha256": None,
        "status": "missing",
    }
    bindings.append(binding)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        _surface_issue(issues, relative, f"W_{surface.upper()}_SOURCE_MISSING", "missing")
        return None, binding
    if resolved != path or not path.is_file():
        binding["status"] = "malformed"
        _surface_issue(
            issues,
            relative,
            f"W_{surface.upper()}_SOURCE_MALFORMED",
            "unsafe_path",
        )
        return None, binding
    try:
        if path.stat().st_size > MAX_SURFACE_SOURCE_BYTES:
            raise ValueError("oversized")
        body = path.read_bytes()
    except OSError:
        binding["status"] = "malformed"
        _surface_issue(issues, relative, f"W_{surface.upper()}_SOURCE_MALFORMED", "unreadable")
        return None, binding
    except ValueError:
        binding["status"] = "malformed"
        _surface_issue(issues, relative, f"W_{surface.upper()}_SOURCE_MALFORMED", "oversized")
        return None, binding
    binding["sha256"] = hashlib.sha256(body).hexdigest()
    try:
        tree = ast.parse(body.decode("utf-8"), filename=relative)
    except (UnicodeError, SyntaxError):
        binding["status"] = "malformed"
        _surface_issue(issues, relative, f"W_{surface.upper()}_SOURCE_MALFORMED", "invalid_python")
        return None, binding
    binding["status"] = "parsed"
    return tree, binding


def _json_registration(
    root: Path,
    relative: str,
    *,
    surface: str,
    issues: list[dict[str, str]],
    bindings: list[dict[str, Any]],
) -> tuple[Any | None, dict[str, Any]]:
    """Read and bind a registration manifest without importing any code."""
    path = root / relative
    binding: dict[str, Any] = {
        "surface": surface,
        "path": relative,
        "sha256": None,
        "status": "missing",
    }
    bindings.append(binding)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        _surface_issue(
            issues,
            relative,
            f"W_{surface.upper()}_REGISTRATION_MISSING",
            "missing",
        )
        return None, binding
    if resolved != path or not path.is_file():
        binding["status"] = "malformed"
        _surface_issue(
            issues,
            relative,
            f"W_{surface.upper()}_REGISTRATION_MALFORMED",
            "unsafe_path",
        )
        return None, binding
    try:
        if path.stat().st_size > MAX_SURFACE_SOURCE_BYTES:
            raise ValueError("oversized")
        body = path.read_bytes()
        binding["sha256"] = hashlib.sha256(body).hexdigest()

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate_key")
                value[key] = item
            return value

        value = json.loads(body.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except OSError:
        detail = "unreadable"
    except UnicodeError:
        detail = "invalid_utf8"
    except json.JSONDecodeError:
        detail = "invalid_json"
    except ValueError as exc:
        detail = str(exc)
    else:
        binding["status"] = "parsed"
        return value, binding
    binding["status"] = "malformed"
    _surface_issue(
        issues,
        relative,
        f"W_{surface.upper()}_REGISTRATION_MALFORMED",
        detail,
    )
    return None, binding


def _assignment(tree: ast.Module, name: str) -> ast.expr | None:
    matches: list[ast.expr] = []
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name and node.value is not None:
                matches.append(node.value)
        elif isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                matches.append(node.value)
    return matches[0] if len(matches) == 1 else None


def _literal(node: ast.expr | None) -> Any:
    if node is None:
        raise ValueError("missing assignment")
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and len(node.args) == 1
        and not node.keywords
    ):
        return frozenset(ast.literal_eval(node.args[0]))
    return ast.literal_eval(node)


def _string_sequence(node: ast.expr | None) -> list[str]:
    value = _literal(node)
    if not isinstance(value, (tuple, list, set, frozenset)) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError("expected string sequence")
    rows = list(value)
    if len(rows) != len(set(rows)):
        raise ValueError("duplicate strings")
    return sorted(rows) if isinstance(value, (set, frozenset)) else rows


def _dict_string_keys(node: ast.expr | None) -> list[str]:
    if not isinstance(node, ast.Dict):
        raise ValueError("expected mapping")
    keys = [key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)]
    if len(keys) != len(node.keys) or len(keys) != len(set(keys)):
        raise ValueError("mapping keys must be unique strings")
    return keys


def _tool_spec_names(node: ast.expr | None) -> list[str]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        raise ValueError("tool specs must be a sequence")
    names: list[str] = []
    for row in node.elts:
        if not isinstance(row, ast.Dict):
            raise ValueError("tool spec must be a mapping")
        found = [
            value.value
            for key, value in zip(row.keys, row.values)
            if isinstance(key, ast.Constant)
            and key.value == "name"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ]
        if len(found) != 1:
            raise ValueError("tool spec name is missing")
        names.append(found[0])
    if len(names) != len(set(names)):
        raise ValueError("tool spec names are duplicated")
    return names


def _role_taxonomy(node: ast.expr | None) -> dict[str, dict[str, str]]:
    if not isinstance(node, ast.Dict):
        raise ValueError("role taxonomy must be a mapping")
    result: dict[str, dict[str, str]] = {}
    for key, value in zip(node.keys, node.values):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            raise ValueError("role name is invalid")
        if key.value in result:
            raise ValueError("role name is duplicated")
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name) or value.func.id != "RoleMeta":
            raise ValueError("role metadata is invalid")
        keywords = {
            item.arg: item.value.value
            for item in value.keywords
            if item.arg is not None
            and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str)
        }
        if (
            set(keywords) != {"posture", "role_class"}
            or keywords["posture"] not in {"read-only", "read-write"}
            or keywords["role_class"]
            not in {"reviewer", "verifier", "executor", "planner", "orchestrator"}
        ):
            raise ValueError("role metadata is incomplete")
        result[key.value] = cast(dict[str, str], keywords)
    return result


def _advisor_defaults(tree: ast.Module) -> dict[str, Any]:
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AdvisorRoute"]
    if len(classes) != 1:
        raise ValueError("AdvisorRoute is missing")
    result: dict[str, Any] = {}
    for node in classes[0].body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            if node.target.id in {"posture", "worker_eligible", "auto_apply", "authoritative"}:
                result[node.target.id] = ast.literal_eval(node.value)
    if result != {
        "posture": "read-only",
        "worker_eligible": False,
        "auto_apply": False,
        "authoritative": False,
    }:
        raise ValueError("advisor posture is not fail-closed")
    return result


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    result: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            if node.name in result:
                raise ValueError("duplicate function")
            result[node.name] = node
    return result


def _discover_roles(
    root: Path,
    issues: list[dict[str, str]],
    bindings: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, str]], str]:
    tree, binding = _python_source(
        root, ROLES_SOURCE, surface="roles", issues=issues, bindings=bindings
    )
    if tree is None:
        return {}, str(binding["status"])
    try:
        roles = _role_taxonomy(_assignment(tree, "_ROLES"))
    except (TypeError, ValueError):
        binding["status"] = "malformed"
        _surface_issue(issues, ROLES_SOURCE, "W_ROLES_SOURCE_MALFORMED", "taxonomy")
        return {}, "malformed"
    binding["status"] = "valid"
    return roles, "claimed"


def _discover_advisors(
    root: Path,
    skill_names: set[str],
    issues: list[dict[str, str]],
    bindings: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    tree, binding = _python_source(
        root, ADVISOR_SOURCE, surface="advisor", issues=issues, bindings=bindings
    )
    empty = {
        "skills": [],
        "providers": {},
        "posture": "unclaimed",
        "worker_eligible": False,
        "auto_apply": False,
        "authoritative": False,
    }
    if tree is None:
        return empty, str(binding["status"])
    try:
        providers = _string_sequence(_assignment(tree, "PROVIDERS"))
        structured = set(
            _string_sequence(_assignment(tree, "STRUCTURED_VERDICT_PROVIDERS"))
        )
        advisor_skills = _string_sequence(_assignment(tree, "ADVISOR_SKILLS"))
        aliases = _literal(_assignment(tree, "ALIASES"))
        if not isinstance(aliases, dict) or not all(
            isinstance(alias, str) and isinstance(provider, str)
            for alias, provider in aliases.items()
        ):
            raise ValueError("advisor aliases")
        specs = _dict_string_keys(_assignment(tree, "SPECS"))
        defaults = _advisor_defaults(tree)
        if (
            set(specs) != set(providers)
            or not set(structured) <= set(providers)
            or not set(aliases.values()) <= set(providers)
        ):
            raise ValueError("advisor provider references mismatch")
        if not set(advisor_skills) <= skill_names:
            binding["status"] = "mismatch"
            missing = sorted(set(advisor_skills) - skill_names)
            _surface_issue(
                issues,
                ADVISOR_SOURCE,
                "W_ADVISOR_SOURCE_MISMATCH",
                f"missing_skills={','.join(missing)}",
            )
            return empty, "mismatch"
    except (TypeError, ValueError):
        if binding["status"] != "mismatch":
            binding["status"] = "malformed"
            _surface_issue(issues, ADVISOR_SOURCE, "W_ADVISOR_SOURCE_MALFORMED", "constants")
        return empty, str(binding["status"])
    policies = {
        provider: {
            "aliases": sorted(alias for alias, target in aliases.items() if target == provider),
            "structured_verdict": provider in structured,
        }
        for provider in sorted(providers)
    }
    binding["status"] = "valid"
    return {
        "skills": sorted(advisor_skills),
        "providers": policies,
        **defaults,
    }, "claimed"


def _discover_mcp(
    root: Path,
    issues: list[dict[str, str]],
    bindings: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    tree, source = _python_source(
        root, MCP_SOURCE, surface="mcp", issues=issues, bindings=bindings
    )
    registration, registration_binding = _json_registration(
        root,
        MCP_REGISTRATION,
        surface="mcp",
        issues=issues,
        bindings=bindings,
    )
    empty = {"operations": [], "operation_count": 0, "authoritative_state_mutation": False}
    if tree is None:
        return empty, str(source["status"])
    try:
        operations = _string_sequence(_assignment(tree, "EXACT_TOOL_NAMES"))
        specs = _tool_spec_names(_assignment(tree, "TOOL_SPECS"))
        handlers = _dict_string_keys(_assignment(tree, "TOOL_HANDLERS"))
        if (
            tuple(operations) != EXPECTED_MCP_OPERATIONS
            or specs != operations
            or handlers != operations
        ):
            raise RuntimeError("operation/spec/handler mismatch")
    except RuntimeError:
        source["status"] = "mismatch"
        _surface_issue(issues, MCP_SOURCE, "W_MCP_SOURCE_MISMATCH", "operation_spec_handler")
        return empty, "mismatch"
    except (TypeError, ValueError):
        source["status"] = "malformed"
        _surface_issue(issues, MCP_SOURCE, "W_MCP_SOURCE_MALFORMED", "constants")
        return empty, "malformed"
    source["status"] = "valid"
    if registration is None:
        return empty, str(registration_binding["status"])
    expected_registration = {
        "mcpServers": {
            "omg": {
                "command": "python3",
                "args": ["${GROK_PLUGIN_ROOT}/bin/omg", "mcp-server"],
            }
        }
    }
    if registration != expected_registration:
        registration_binding["status"] = "mismatch"
        _surface_issue(
            issues,
            MCP_REGISTRATION,
            "W_MCP_REGISTRATION_MISMATCH",
            "schema_or_command",
        )
        return empty, "mismatch"
    registration_binding["status"] = "valid"
    discovered = {
        "operations": operations,
        "operation_count": len(operations),
        "authoritative_state_mutation": False,
    }
    return discovered, "claimed"


def _discover_lsp(
    root: Path,
    issues: list[dict[str, str]],
    bindings: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    tree, source = _python_source(
        root, LSP_SOURCE, surface="lsp", issues=issues, bindings=bindings
    )
    registration, registration_binding = _json_registration(
        root,
        LSP_REGISTRATION,
        surface="lsp",
        issues=issues,
        bindings=bindings,
    )
    empty = {"owner": "unclaimed", "registration_file": None, "semantic_proxy_count": None}
    if tree is None:
        return empty, str(source["status"])
    try:
        config_name = _literal(_assignment(tree, "LSP_CONFIG_NAME"))
        semantic = _string_sequence(_assignment(tree, "SEMANTIC_PROXY_OPERATIONS"))
        functions = _functions(tree)
        strings = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        if (
            config_name != ".lsp.json"
            or semantic
            or not {"validate_registration", "load_registration", "registration_status"}
            <= functions.keys()
            or "host_owned" not in strings
        ):
            raise RuntimeError("registration/proxy mismatch")
    except RuntimeError:
        source["status"] = "mismatch"
        _surface_issue(issues, LSP_SOURCE, "W_LSP_SOURCE_MISMATCH", "registration_or_proxy")
        return empty, "mismatch"
    except (TypeError, ValueError):
        source["status"] = "malformed"
        _surface_issue(issues, LSP_SOURCE, "W_LSP_SOURCE_MALFORMED", "constants")
        return empty, "malformed"
    source["status"] = "valid"
    if registration is None:
        return empty, str(registration_binding["status"])
    expected_registration = {
        "pyright": {
            "command": "pyright-langserver",
            "args": ["--stdio"],
            "extensionToLanguage": {".py": "python"},
        }
    }
    if registration != expected_registration:
        registration_binding["status"] = "mismatch"
        _surface_issue(
            issues,
            LSP_REGISTRATION,
            "W_LSP_REGISTRATION_MISMATCH",
            "schema_or_command",
        )
        return empty, "mismatch"
    registration_binding["status"] = "valid"
    discovered = {
        "owner": "host",
        "registration_file": config_name,
        "semantic_proxy_count": len(semantic),
    }
    return discovered, "claimed"


def _discover_workflow(
    root: Path,
    issues: list[dict[str, str]],
    bindings: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    contract_tree, contract_binding = _python_source(
        root,
        WORKFLOW_CONTRACT_SOURCE,
        surface="workflow",
        issues=issues,
        bindings=bindings,
    )
    native_tree, native_binding = _python_source(
        root,
        WORKFLOW_NATIVE_SOURCE,
        surface="workflow",
        issues=issues,
        bindings=bindings,
    )
    empty = {
        "contract": None,
        "portable_classification": "unclaimed",
        "grok_native_projection": "optional_unclaimed",
    }
    if contract_tree is None or native_tree is None:
        status = contract_binding["status"] if contract_tree is None else native_binding["status"]
        return empty, str(status)
    try:
        contract = _literal(_assignment(contract_tree, "WORKFLOW_CONTRACT"))
        functions = _functions(native_tree)
        assess = functions.get("assess_native_capability")
        project = functions.get("project_to_rhai")
        native_strings = {
            node.value
            for node in ast.walk(native_tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        if assess is None or project is None:
            raise ValueError("workflow definitions")
        if contract != EXPECTED_WORKFLOW_CONTRACT:
            contract_binding["status"] = "mismatch"
            _surface_issue(
                issues,
                WORKFLOW_CONTRACT_SOURCE,
                "W_WORKFLOW_SOURCE_MISMATCH",
                "contract",
            )
            return empty, "mismatch"
        if "optional_unclaimed" not in native_strings or not any(
            isinstance(node, ast.Raise) for node in ast.walk(project)
        ):
            raise RuntimeError("native workflow posture mismatch")
    except RuntimeError:
        native_binding["status"] = "mismatch"
        _surface_issue(
            issues,
            WORKFLOW_NATIVE_SOURCE,
            "W_WORKFLOW_SOURCE_MISMATCH",
            "native_projection",
        )
        return empty, "mismatch"
    except (TypeError, ValueError):
        contract_binding["status"] = "malformed"
        native_binding["status"] = "malformed"
        _surface_issue(
            issues,
            WORKFLOW_CONTRACT_SOURCE,
            "W_WORKFLOW_SOURCE_MALFORMED",
            "contract_or_adapter",
        )
        return empty, "malformed"
    contract_binding["status"] = "valid"
    native_binding["status"] = "valid"
    return {
        "contract": contract,
        "portable_classification": "native_substitute",
        "grok_native_projection": "optional_unclaimed",
    }, "claimed"


def discover_session_surface(root: Path) -> dict[str, Any]:
    """Discover deterministic skills, agents, advisor, MCP, LSP and workflow truth.

    Arbitrary installed roots are parsed as bytes only; none of their Python is
    imported or executed.  A surface is claimed only when every defining source
    and registration manifest parses and agrees.
    """
    root = Path(root).resolve()
    skills: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    bindings: list[dict[str, Any]] = []
    roles, roles_status = _discover_roles(root, issues, bindings)
    for path in _capability_files(root):
        rel = path.relative_to(root).as_posix()
        fields, metadata_status = _frontmatter(path)
        digest = _sha256_file(path)
        if rel.startswith("skills/"):
            fallback_name = path.parent.name
            declared_name = fields.get("name")
            name = declared_name or fallback_name
            record: dict[str, Any] = {
                "name": name,
                "path": rel,
                "sha256": digest,
                "metadata_status": metadata_status,
            }
            skills.append(record)
            if (
                metadata_status != "valid"
                or declared_name is None
                or name != fallback_name
            ):
                detail = (
                    metadata_status
                    if metadata_status != "valid"
                    else "name_missing"
                    if declared_name is None
                    else "name_mismatch"
                )
                issues.append(
                    {
                        "path": rel,
                        "code": "W_SKILL_METADATA",
                        "detail": detail,
                    }
                )
            continue

        fallback_name = path.stem
        declared_name = fields.get("name")
        name = declared_name or fallback_name
        role = name[4:] if name.startswith("omg-") else name
        declared = fields.get("capabilityMode") or None
        canonical = roles.get(role, {}).get("posture")
        if canonical in {"read-only", "read-write"}:
            capability_mode = str(canonical)
            source = "role_taxonomy"
        elif declared in {"read-only", "read-write"}:
            capability_mode = declared
            source = "frontmatter"
        else:
            capability_mode = "unspecified"
            source = "unresolved"
        record = {
            "name": name,
            "path": rel,
            "sha256": digest,
            "metadata_status": metadata_status,
            "role": role,
            "capability_mode": capability_mode,
            "capability_source": source,
            "declared_capability_mode": declared,
        }
        agents.append(record)
        if (
            metadata_status != "valid"
            or declared_name is None
            or name != fallback_name
        ):
            detail = (
                metadata_status
                if metadata_status != "valid"
                else "name_missing"
                if declared_name is None
                else "name_mismatch"
            )
            issues.append(
                {
                    "path": rel,
                    "code": "W_AGENT_METADATA",
                    "detail": detail,
                }
            )
        if canonical is not None and declared not in {None, canonical}:
            issues.append(
                {
                    "path": rel,
                    "code": "W_AGENT_CAPABILITY_MISMATCH",
                    "detail": f"declared={declared};canonical={canonical}",
                }
            )

    skills.sort(key=lambda item: (item["name"], item["path"]))
    agents.sort(key=lambda item: (item["name"], item["path"]))
    skill_names = {str(item["name"]) for item in skills}
    advisor_routing, advisor_status = _discover_advisors(
        root, skill_names, issues, bindings
    )
    mcp, mcp_status = _discover_mcp(root, issues, bindings)
    lsp, lsp_status = _discover_lsp(root, issues, bindings)
    workflow, workflow_status = _discover_workflow(root, issues, bindings)
    bindings.sort(key=lambda item: (item["surface"], item["path"]))
    issues.sort(key=lambda item: (item["path"], item["code"], item["detail"]))
    return {
        "schema": SESSION_SURFACE_SCHEMA,
        "skills": skills,
        "agents": agents,
        "claim_status": {
            "roles": roles_status,
            "advisor_routing": advisor_status,
            "mcp": mcp_status,
            "lsp": lsp_status,
            "workflow": workflow_status,
        },
        "source_bindings": bindings,
        "advisor_routing": advisor_routing,
        "mcp": mcp,
        "lsp": lsp,
        "workflow": workflow,
        "issues": issues,
    }


def _capability_files(root: Path) -> list[Path]:
    """Return sorted absolute paths for skills/omg-*/SKILL.md and agents/omg-*.md."""
    root = Path(root)
    found: list[Path] = []
    skills = root / "skills"
    if skills.is_dir():
        for child in sorted(skills.iterdir()):
            if not child.is_dir() or not child.name.startswith("omg-"):
                continue
            skill = child / "SKILL.md"
            if skill.is_file():
                found.append(skill)
    agents = root / "agents"
    if agents.is_dir():
        for child in sorted(agents.iterdir()):
            if child.is_file() and child.name.startswith("omg-") and child.suffix == ".md":
                found.append(child)
    # Sort by repo-relative posix path
    found.sort(key=lambda p: p.relative_to(root).as_posix())
    return found


def _plugin_version(root: Path) -> str:
    path = Path(root) / "plugin.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("version") or "0")
    except (OSError, json.JSONDecodeError, TypeError):
        return "0"


def compute_lock_for(root: Path) -> dict[str, Any]:
    """Hash skills/omg-*/SKILL.md + agents/omg-*.md under an arbitrary root.

    Used for both the local checkout (commit-hygiene) and the installed frozen
    snapshot under ~/.grok/installed-plugins (OMX-parity installed-drift).
    """
    root = Path(root).resolve()
    files: dict[str, str] = {}
    for path in _capability_files(root):
        rel = path.relative_to(root).as_posix()
        files[rel] = _sha256_file(path)
    lines = [f"{rel}:{files[rel]}" for rel in sorted(files)]
    aggregate = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    session_surface = discover_session_surface(root)
    return {
        "version": _plugin_version(root),
        "files": files,
        "aggregate": aggregate,
        "session_surface": session_surface,
        "session_surface_aggregate": _surface_hash(session_surface),
    }


def compute_lock(root: Path) -> dict[str, Any]:
    """Compute capabilities lock dict for *root* (plugin / working tree)."""
    return compute_lock_for(root)


def read_lock(root: Path) -> dict[str, Any] | None:
    """Load on-disk lock or return None if missing/unreadable."""
    path = Path(root) / LOCK_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def write_lock(root: Path) -> Path:
    """Write omg_capabilities.lock.json at *root*; return path."""
    root = Path(root)
    lock = compute_lock(root)
    path = root / LOCK_NAME
    path.write_text(
        json.dumps(lock, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _diff_lock(stored: dict[str, Any], current: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if stored.get("version") != current.get("version"):
        lines.append(
            f"version: stored={stored.get('version')!r} current={current.get('version')!r}"
        )
    if stored.get("aggregate") != current.get("aggregate"):
        lines.append(
            f"aggregate: stored={stored.get('aggregate')} current={current.get('aggregate')}"
        )
    if stored.get("session_surface_aggregate") != current.get(
        "session_surface_aggregate"
    ):
        lines.append(
            "session_surface_aggregate: "
            f"stored={stored.get('session_surface_aggregate')} "
            f"current={current.get('session_surface_aggregate')}"
        )
    if stored.get("session_surface") != current.get("session_surface"):
        lines.append("session_surface: stored surface differs from current discovery")
    s_files = cast(
        dict[str, Any],
        stored.get("files") if isinstance(stored.get("files"), dict) else {},
    )
    c_files = cast(
        dict[str, Any],
        current.get("files") if isinstance(current.get("files"), dict) else {},
    )
    all_keys = sorted(set(s_files) | set(c_files))
    for key in all_keys:
        s = s_files.get(key)
        c = c_files.get(key)
        if s is None:
            lines.append(f"+ {key} (new, {c})")
        elif c is None:
            lines.append(f"- {key} (removed, was {s})")
        elif s != c:
            lines.append(f"~ {key}\n    stored:  {s}\n    current: {c}")
    return lines


def lock_matches(stored: dict[str, Any], current: dict[str, Any]) -> bool:
    """Return true only when every release/session capability binding matches."""
    return all(
        stored.get(field) == current.get(field)
        for field in (
            "version",
            "files",
            "aggregate",
            "session_surface",
            "session_surface_aggregate",
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate or check omg_capabilities.lock.json for the local checkout "
            "(commit-hygiene / CI guard on skills+agents; not installed-snapshot drift)"
        )
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="recompute local checkout and exit 1 if lock is stale (print diff)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="local checkout / plugin root (default: repo containing this script)",
    )
    args = parser.parse_args(argv)
    root = (args.root if args.root is not None else _repo_root()).resolve()

    current = compute_lock(root)
    if args.check:
        stored = read_lock(root)
        if stored is None:
            print(f"missing {LOCK_NAME} under {root}", file=sys.stderr)
            return 1
        if lock_matches(stored, current):
            print(
                f"ok: {len(current.get('files') or {})} files match "
                f"(aggregate={current['aggregate'][:12]}…)"
            )
            return 0
        print(f"stale {LOCK_NAME}:")
        for line in _diff_lock(stored, current):
            print(line)
        return 1

    path = write_lock(root)
    print(f"wrote {path} ({len(current['files'])} files, aggregate={current['aggregate'][:12]}…)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
