"""Fail-closed parity completeness promotion gate (#78-D).

Catalogue seeds are not completeness proofs. Promoting ``source_status`` /
``category_status`` / ``inventory_status`` to ``complete`` requires a
versioned policy + deterministically reproducible proof. Strict inventory
checks invoke this gate; the maintainer script may emit a candidate proof
but never mutates inventory status.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from omg_cli.contracts.parity_schema import (
    PARITY_CATEGORY_TAXONOMY,
    SOURCE_STATUS_IDS,
    load_json_object,
)
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_git_oid,
    require_nonempty_string,
    require_object,
    require_safe_id,
    require_sha256,
    require_string_list,
)

__all__ = [
    "COMPLETENESS_POLICY_STORE_KIND",
    "COMPLETENESS_PROOF_STORE_KIND",
    "COMPLETENESS_SCHEMA_VERSION",
    "DEFAULT_POLICY_DIR_RELATIVE",
    "DEFAULT_PROOF_DIR_RELATIVE",
    "EXTRACTION_JSON_REGISTRY_V1",
    "CompletenessGateResult",
    "assert_completeness_promotion",
    "authenticate_pinned_checkout",
    "build_completeness_proof",
    "canonical_json_digest",
    "check_completeness_promotion_gate",
    "coverage_projection_for_source",
    "digest_coverage_projection",
    "digest_policy",
    "digest_seed_catalog",
    "digest_source_input",
    "digest_surface_index",
    "plan_completeness_proof",
    "reproduce_source_index",
    "validate_completeness_policy",
    "validate_completeness_proof",
    "verify_completeness_proof",
]

COMPLETENESS_POLICY_STORE_KIND = "parity-completeness-policy"
COMPLETENESS_PROOF_STORE_KIND = "parity-completeness-proof"
COMPLETENESS_SCHEMA_VERSION = 1
DEFAULT_POLICY_DIR_RELATIVE = "docs/parity/completeness/policies"
DEFAULT_PROOF_DIR_RELATIVE = "docs/parity/completeness/proofs"
EXTRACTION_JSON_REGISTRY_V1 = "json_registry_v1"

_POLICY_TOP_KEYS = frozenset(
    {
        "store_kind",
        "schema_version",
        "source",
        "repository",
        "discovery_rules",
    }
)
_DISCOVERY_KEYS = frozenset(
    {
        "version",
        "authoritative_registries",
        "category_assignment",
        "non_surface_exceptions",
    }
)
_REGISTRY_KEYS = frozenset({"path", "extraction_method"})
_EXCEPTION_KEYS = frozenset({"path", "rationale", "issue"})
_PROOF_TOP_KEYS = frozenset(
    {
        "store_kind",
        "schema_version",
        "source",
        "repository",
        "pin_revision",
        "checkout_provenance",
        "policy_digest",
        "seed_digest",
        "coverage_digest",
        "source_input_digest",
        "surface_index_digest",
        "discovered_surfaces",
        "unresolved_surfaces",
        "empty_category_partitions",
    }
)
_PROVENANCE_KEYS = frozenset({"method", "observed_revision"})
_CHECKOUT_AUTH_METHOD = "git_head_clean"
_SURFACE_KEYS = frozenset(
    {
        "surface_id",
        "kind",
        "category",
        "source_path",
        "anchor",
        "content_digest",
        "capability_ids",
    }
)


def canonical_json_digest(value: Any) -> str:
    """SHA-256 over canonical JSON (sorted keys, compact separators)."""
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_relative_posix(path_text: str, *, label: str) -> str:
    text = require_nonempty_string(path_text, label=label)
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts or pure.parts[0] == "~":
        raise ContractValidationError(f"{label} must be a relative POSIX path")
    return text.replace("\\", "/")


def _resolve_confined_file(root: Path, relative: str, *, label: str) -> Path:
    rel = _require_relative_posix(relative, label=label)
    root_resolved = root.resolve()
    current = root_resolved
    for part in PurePosixPath(rel).parts:
        current = current / part
        # Reject symlinks before following them (resolve() would hide them).
        if os.path.lexists(current) and os.path.islink(current):
            raise ContractValidationError(
                f"{label} must not be a symlink: {relative}"
            )
    try:
        resolved = current.resolve(strict=False)
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ContractValidationError(
            f"{label} escapes upstream root: {relative}"
        ) from exc
    if not os.path.isfile(current) or os.path.islink(current):
        raise ContractValidationError(f"{label} must be a regular file: {relative}")
    return current


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_git(root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def authenticate_pinned_checkout(
    upstream_root: Path | str,
    pin_revision: str,
) -> dict[str, str]:
    """Require upstream_root HEAD == pin_revision with a clean work tree.

    Fail closed when the directory is not its own git work-tree root, when HEAD
    drifts from the pin, or when any tracked/untracked dirty path is present.
    This binds filesystem bytes used for hashing to the claimed pin.
    """
    root = Path(upstream_root)
    if not root.is_dir():
        raise ContractValidationError(f"upstream_root is not a directory: {root}")
    pin = require_git_oid(pin_revision, label="pin_revision")

    inside = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise ContractValidationError(
            f"upstream_root is not a git work tree: {root}"
        )

    toplevel = _run_git(root, ["rev-parse", "--show-toplevel"])
    if toplevel.returncode != 0:
        raise ContractValidationError(
            f"git rev-parse --show-toplevel failed in {root}: {toplevel.stderr.strip()}"
        )
    top = Path(toplevel.stdout.strip()).resolve()
    if top != root.resolve():
        raise ContractValidationError(
            "upstream_root must be a git work-tree root matching the pin "
            f"(toplevel={top}, root={root.resolve()})"
        )

    head_proc = _run_git(root, ["rev-parse", "HEAD"])
    if head_proc.returncode != 0:
        raise ContractValidationError(
            f"git rev-parse HEAD failed in {root}: {head_proc.stderr.strip()}"
        )
    head = head_proc.stdout.strip().lower()
    if not head or head != pin:
        raise ContractValidationError(
            f"upstream_root HEAD {head!r} does not match pin_revision {pin!r}"
        )

    status = _run_git(
        root,
        ["status", "--porcelain=v1", "--untracked-files=all"],
    )
    if status.returncode != 0:
        raise ContractValidationError(
            f"git status failed in {root}: {status.stderr.strip()}"
        )
    dirty_lines = [line for line in status.stdout.splitlines() if line.strip()]
    if dirty_lines:
        sample = "; ".join(dirty_lines[:5])
        raise ContractValidationError(
            f"upstream_root is dirty relative to pin_revision {pin}: {sample}"
        )

    return {
        "method": _CHECKOUT_AUTH_METHOD,
        "observed_revision": head,
    }


def coverage_projection_for_source(
    inventory: Mapping[str, Any], source: str
) -> dict[str, Any]:
    """Normalized inventory coverage for one upstream source (digest input)."""
    source = require_nonempty_string(source, label="source")
    if source not in SOURCE_STATUS_IDS:
        raise ContractValidationError(
            f"unsupported completeness source {source!r}; "
            "host-baseline scope is unsupported here"
        )
    pins = inventory.get("upstream_pins")
    if not isinstance(pins, Mapping) or source not in pins:
        raise ContractValidationError(f"inventory missing upstream_pins[{source}]")
    pin = require_object(pins[source], label=f"upstream_pins[{source}]")
    repository = require_nonempty_string(
        pin.get("repository"), label=f"upstream_pins[{source}].repository"
    )
    revision = require_git_oid(
        pin.get("revision"), label=f"upstream_pins[{source}].revision"
    )

    capabilities: list[dict[str, Any]] = []
    for row in inventory.get("capabilities", []):
        if not isinstance(row, Mapping):
            continue
        upstream = row.get("upstream")
        if not isinstance(upstream, Mapping):
            continue
        if upstream.get("source") != source:
            continue
        cap_id = require_nonempty_string(row.get("id"), label="capability.id")
        classification = require_nonempty_string(
            row.get("classification"), label=f"{cap_id}.classification"
        )
        category = require_nonempty_string(
            row.get("category"), label=f"{cap_id}.category"
        )
        paths = upstream.get("source_paths")
        if not isinstance(paths, list):
            raise ContractValidationError(f"{cap_id}.upstream.source_paths required")
        path_list = sorted(
            _require_relative_posix(str(p), label=f"{cap_id}.source_paths[]")
            for p in paths
        )
        entry: dict[str, Any] = {
            "id": cap_id,
            "category": category,
            "classification": classification,
            "upstream_paths": path_list,
            "revision": require_git_oid(
                upstream.get("revision"), label=f"{cap_id}.upstream.revision"
            ),
            "issues": sorted(
                str(i)
                for i in (row.get("issues") or [])
                if isinstance(i, str) and i
            ),
            "gap": str(row.get("gap") or ""),
        }
        if classification == "alias":
            entry["alias_of"] = require_nonempty_string(
                row.get("alias_of"), label=f"{cap_id}.alias_of"
            )
        else:
            entry["alias_of"] = None
        capabilities.append(entry)
    capabilities.sort(key=lambda item: item["id"])
    return {
        "source": source,
        "pin": {"repository": repository, "revision": revision},
        "capabilities": capabilities,
    }


def digest_coverage_projection(inventory: Mapping[str, Any], source: str) -> str:
    return canonical_json_digest(coverage_projection_for_source(inventory, source))


def digest_seed_catalog(seed: Mapping[str, Any] | None) -> str:
    """Digest a committed upstream-snapshot seed (not a completeness proof)."""
    if seed is None:
        return canonical_json_digest({"seed": None})
    catalog = require_object(seed, label="upstream_seed")
    source = require_nonempty_string(catalog.get("source"), label="seed.source")
    pin = require_git_oid(catalog.get("pin_revision"), label="seed.pin_revision")
    caps_raw = catalog.get("capabilities")
    if not isinstance(caps_raw, list):
        raise ContractValidationError("seed.capabilities must be a list")
    caps: list[dict[str, Any]] = []
    for idx, cap in enumerate(caps_raw):
        row = require_object(cap, label=f"seed.capabilities[{idx}]")
        paths = row.get("source_paths")
        if not isinstance(paths, list):
            raise ContractValidationError(
                f"seed.capabilities[{idx}].source_paths must be a list"
            )
        caps.append(
            {
                "id": require_nonempty_string(row.get("id"), label="seed.cap.id"),
                "promise": str(row.get("promise") or ""),
                "source_paths": sorted(str(p) for p in paths),
            }
        )
    caps.sort(key=lambda item: item["id"])
    return canonical_json_digest(
        {"source": source, "pin_revision": pin, "capabilities": caps}
    )


def validate_completeness_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    policy = require_object(value, label="completeness_policy")
    require_exact_keys(policy, required=_POLICY_TOP_KEYS, label="completeness_policy")
    if policy.get("store_kind") != COMPLETENESS_POLICY_STORE_KIND:
        raise ContractValidationError(
            f"completeness_policy.store_kind must be {COMPLETENESS_POLICY_STORE_KIND!r}"
        )
    if policy.get("schema_version") != COMPLETENESS_SCHEMA_VERSION:
        raise ContractValidationError(
            f"completeness_policy.schema_version must be {COMPLETENESS_SCHEMA_VERSION}"
        )
    source = require_nonempty_string(policy.get("source"), label="policy.source")
    if source not in SOURCE_STATUS_IDS:
        raise ContractValidationError(
            f"policy.source {source!r} is not a parity SOURCE_STATUS_ID "
            "(host-baseline unsupported-scope)"
        )
    repository = require_nonempty_string(
        policy.get("repository"), label="policy.repository"
    )
    rules = require_object(policy.get("discovery_rules"), label="discovery_rules")
    require_exact_keys(rules, required=_DISCOVERY_KEYS, label="discovery_rules")
    if rules.get("version") != 1:
        raise ContractValidationError("discovery_rules.version must be 1")
    registries = rules.get("authoritative_registries")
    if not isinstance(registries, list) or not registries:
        raise ContractValidationError(
            "discovery_rules.authoritative_registries must be a non-empty list"
        )
    normalized_regs: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for idx, item in enumerate(registries):
        reg = require_object(item, label=f"authoritative_registries[{idx}]")
        require_exact_keys(
            reg, required=_REGISTRY_KEYS, label=f"authoritative_registries[{idx}]"
        )
        path = _require_relative_posix(
            str(reg["path"]), label=f"authoritative_registries[{idx}].path"
        )
        method = require_nonempty_string(
            reg.get("extraction_method"),
            label=f"authoritative_registries[{idx}].extraction_method",
        )
        if method != EXTRACTION_JSON_REGISTRY_V1:
            raise ContractValidationError(
                f"unsupported extraction_method {method!r}; "
                f"supported: {EXTRACTION_JSON_REGISTRY_V1}"
            )
        if path in seen_paths:
            raise ContractValidationError(
                f"duplicate authoritative registry path: {path}"
            )
        seen_paths.add(path)
        normalized_regs.append({"path": path, "extraction_method": method})

    assignment = require_object(
        rules.get("category_assignment"), label="category_assignment"
    )
    if not assignment:
        raise ContractValidationError("category_assignment must be non-empty")
    normalized_assignment: dict[str, str] = {}
    for kind, category in assignment.items():
        kind_s = require_safe_id(kind, label="category_assignment.kind")
        cat_s = require_nonempty_string(category, label="category_assignment.category")
        if cat_s not in PARITY_CATEGORY_TAXONOMY:
            raise ContractValidationError(
                f"category_assignment[{kind_s}] unknown category {cat_s!r}"
            )
        normalized_assignment[kind_s] = cat_s

    exceptions_raw = rules.get("non_surface_exceptions")
    if not isinstance(exceptions_raw, list):
        raise ContractValidationError("non_surface_exceptions must be a list")
    exceptions: list[dict[str, str]] = []
    seen_exc: set[str] = set()
    for idx, item in enumerate(exceptions_raw):
        exc = require_object(item, label=f"non_surface_exceptions[{idx}]")
        require_exact_keys(
            exc, required=_EXCEPTION_KEYS, label=f"non_surface_exceptions[{idx}]"
        )
        path = _require_relative_posix(
            str(exc["path"]), label=f"non_surface_exceptions[{idx}].path"
        )
        if path in seen_exc:
            raise ContractValidationError(f"duplicate non_surface exception: {path}")
        seen_exc.add(path)
        exceptions.append(
            {
                "path": path,
                "rationale": require_nonempty_string(
                    exc.get("rationale"),
                    label=f"non_surface_exceptions[{idx}].rationale",
                ),
                "issue": require_nonempty_string(
                    exc.get("issue"), label=f"non_surface_exceptions[{idx}].issue"
                ),
            }
        )
    exceptions.sort(key=lambda item: item["path"])

    return {
        "store_kind": COMPLETENESS_POLICY_STORE_KIND,
        "schema_version": COMPLETENESS_SCHEMA_VERSION,
        "source": source,
        "repository": repository,
        "discovery_rules": {
            "version": 1,
            "authoritative_registries": normalized_regs,
            "category_assignment": dict(sorted(normalized_assignment.items())),
            "non_surface_exceptions": exceptions,
        },
    }


def digest_policy(policy: Mapping[str, Any]) -> str:
    return canonical_json_digest(validate_completeness_policy(policy))


def _parse_json_registry_v1(
    *,
    registry_path: str,
    payload: Mapping[str, Any],
    category_assignment: Mapping[str, str],
    content_digest: str,
) -> list[dict[str, Any]]:
    body = require_object(payload, label=f"registry:{registry_path}")
    kind = require_safe_id(body.get("kind"), label=f"{registry_path}.kind")
    if kind not in category_assignment:
        raise ContractValidationError(
            f"registry {registry_path} kind {kind!r} missing from category_assignment"
        )
    category = category_assignment[kind]
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise ContractValidationError(f"{registry_path}.entries must be a list")
    surfaces: list[dict[str, Any]] = []
    for idx, raw in enumerate(entries):
        entry = require_object(raw, label=f"{registry_path}.entries[{idx}]")
        surface_id = require_safe_id(
            entry.get("id"), label=f"{registry_path}.entries[{idx}].id"
        )
        source_path = _require_relative_posix(
            str(entry.get("path")),
            label=f"{registry_path}.entries[{idx}].path",
        )
        anchor = require_nonempty_string(
            entry.get("anchor"), label=f"{registry_path}.entries[{idx}].anchor"
        )
        surfaces.append(
            {
                "surface_id": surface_id,
                "kind": kind,
                "category": category,
                "source_path": source_path,
                "anchor": anchor,
                "registry_path": registry_path,
                "registry_content_digest": content_digest,
            }
        )
    return surfaces


def reproduce_source_index(
    policy: Mapping[str, Any],
    upstream_root: Path | str,
    *,
    pin_revision: str,
) -> dict[str, Any]:
    """Discover surfaces from a supplied pinned checkout (fail-closed).

    Authenticates ``upstream_root`` to ``pin_revision`` (HEAD match + clean
    tree) before hashing any registry/surface bytes.
    """
    validated = validate_completeness_policy(policy)
    provenance = authenticate_pinned_checkout(upstream_root, pin_revision)
    root = Path(upstream_root)
    if not root.is_dir():
        raise ContractValidationError(f"upstream_root is not a directory: {root}")

    rules = validated["discovery_rules"]
    assignment = rules["category_assignment"]
    discovered: list[dict[str, Any]] = []
    input_parts: list[dict[str, str]] = []

    for reg in rules["authoritative_registries"]:
        path = reg["path"]
        file_path = _resolve_confined_file(
            root, path, label=f"registry:{path}"
        )
        digest = _file_digest(file_path)
        input_parts.append({"path": path, "content_digest": digest})
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ContractValidationError(
                f"registry {path} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ContractValidationError(f"registry {path} must be a JSON object")
        discovered.extend(
            _parse_json_registry_v1(
                registry_path=path,
                payload=payload,
                category_assignment=assignment,
                content_digest=digest,
            )
        )

    # Confirm each surface source_path is a confined regular file; digest content.
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for surface in discovered:
        sid = surface["surface_id"]
        if sid in seen_ids:
            raise ContractValidationError(f"duplicate surface_id: {sid}")
        seen_ids.add(sid)
        src = _resolve_confined_file(
            root, surface["source_path"], label=f"surface:{sid}.source_path"
        )
        content_digest = _file_digest(src)
        normalized.append(
            {
                "surface_id": sid,
                "kind": surface["kind"],
                "category": surface["category"],
                "source_path": surface["source_path"],
                "anchor": surface["anchor"],
                "content_digest": content_digest,
            }
        )
    normalized.sort(key=lambda item: item["surface_id"])
    input_parts.sort(key=lambda item: item["path"])

    present_categories = {item["category"] for item in normalized}
    empty_partitions = sorted(PARITY_CATEGORY_TAXONOMY - present_categories)

    return {
        "source": validated["source"],
        "repository": validated["repository"],
        "pin_revision": provenance["observed_revision"],
        "checkout_provenance": provenance,
        "discovered_surfaces": normalized,
        "empty_category_partitions": empty_partitions,
        "source_input": input_parts,
        "source_input_digest": canonical_json_digest(input_parts),
        "surface_index_digest": digest_surface_index(normalized),
    }


def digest_source_input(parts: Sequence[Mapping[str, Any]]) -> str:
    normalized = [
        {
            "path": require_nonempty_string(p.get("path"), label="source_input.path"),
            "content_digest": require_sha256(
                p.get("content_digest"), label="source_input.content_digest"
            ),
        }
        for p in parts
    ]
    normalized.sort(key=lambda item: item["path"])
    return canonical_json_digest(normalized)


def digest_surface_index(surfaces: Sequence[Mapping[str, Any]]) -> str:
    normalized = []
    for surface in surfaces:
        normalized.append(
            {
                "surface_id": require_nonempty_string(
                    surface.get("surface_id"), label="surface_id"
                ),
                "kind": require_nonempty_string(surface.get("kind"), label="kind"),
                "category": require_nonempty_string(
                    surface.get("category"), label="category"
                ),
                "source_path": _require_relative_posix(
                    str(surface.get("source_path")), label="source_path"
                ),
                "anchor": require_nonempty_string(
                    surface.get("anchor"), label="anchor"
                ),
                "content_digest": require_sha256(
                    surface.get("content_digest"), label="content_digest"
                ),
            }
        )
    normalized.sort(key=lambda item: item["surface_id"])
    return canonical_json_digest(normalized)


def _capability_index(inventory: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in inventory.get("capabilities", []):
        if not isinstance(row, Mapping):
            continue
        cap_id = row.get("id")
        if isinstance(cap_id, str) and cap_id:
            index[cap_id] = dict(row)
    return index


def _resolve_canonical_capability(
    cap_id: str,
    *,
    inventory: Mapping[str, Any],
    index: Mapping[str, Mapping[str, Any]],
    source: str,
    category: str,
    surface_id: str,
) -> dict[str, Any]:
    row = index.get(cap_id)
    if row is None:
        raise ContractValidationError(
            f"surface {surface_id} maps to unknown capability {cap_id!r}"
        )
    classification = row.get("classification")
    if classification == "alias":
        raise ContractValidationError(
            f"surface {surface_id} mapping rejects alias-only target {cap_id!r}"
        )
    upstream = row.get("upstream")
    if not isinstance(upstream, Mapping):
        raise ContractValidationError(
            f"capability {cap_id} missing upstream binding for surface {surface_id}"
        )
    upstream_source = upstream.get("source")
    if upstream_source != source:
        raise ContractValidationError(
            f"surface {surface_id} cross-source mapping: "
            f"proof source={source} capability source={upstream_source}"
        )
    row_category = row.get("category")
    if row_category != category:
        raise ContractValidationError(
            f"surface {surface_id} cross-category mapping: "
            f"surface category={category} capability category={row_category}"
        )
    # Host-baseline leakage: GROK_BUILD is never a SOURCE_STATUS_ID; also reject
    # explicit host_owned rows claiming upstream completeness for this source.
    if classification == "host_owned" and upstream_source == source:
        # host_owned may still catalogue an upstream mention; mapping a discovered
        # user-facing surface solely onto host_owned is unsupported for promotion.
        pass
    return dict(row)


def _apply_surface_mappings(
    *,
    surfaces: Sequence[Mapping[str, Any]],
    mappings: Mapping[str, Sequence[str]] | None,
    inventory: Mapping[str, Any],
    source: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Attach capability_ids; return (mapped_surfaces, unresolved_ids)."""
    index = _capability_index(inventory)
    mapping_table = {
        str(k): list(v) for k, v in (mappings or {}).items() if isinstance(v, Sequence)
    }
    mapped: list[dict[str, Any]] = []
    unresolved: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()

    for surface in surfaces:
        sid = require_nonempty_string(surface.get("surface_id"), label="surface_id")
        category = require_nonempty_string(surface.get("category"), label="category")
        cap_ids = mapping_table.get(sid)
        if not cap_ids:
            unresolved.append(sid)
            entry = dict(surface)
            entry["capability_ids"] = []
            mapped.append(entry)
            continue
        canonical_ids: list[str] = []
        for cap_id in cap_ids:
            cid = require_nonempty_string(cap_id, label=f"{sid}.capability_id")
            pair = (sid, cid)
            if pair in seen_pairs:
                raise ContractValidationError(
                    f"duplicate surface→capability mapping: {sid} → {cid}"
                )
            seen_pairs.add(pair)
            _resolve_canonical_capability(
                cid,
                inventory=inventory,
                index=index,
                source=source,
                category=category,
                surface_id=sid,
            )
            canonical_ids.append(cid)
        # Reject alias-only: at least one non-alias already enforced per id.
        entry = dict(surface)
        entry["capability_ids"] = sorted(set(canonical_ids))
        mapped.append(entry)

    # Detect mapping keys that do not correspond to discovered surfaces.
    discovered_ids = {s["surface_id"] for s in surfaces}
    for key in mapping_table:
        if key not in discovered_ids:
            raise ContractValidationError(
                f"mapping references undiscovered surface_id: {key}"
            )

    mapped.sort(key=lambda item: item["surface_id"])
    unresolved = sorted(set(unresolved))
    return mapped, unresolved


def validate_completeness_proof(value: Mapping[str, Any]) -> dict[str, Any]:
    proof = require_object(value, label="completeness_proof")
    require_exact_keys(proof, required=_PROOF_TOP_KEYS, label="completeness_proof")
    if proof.get("store_kind") != COMPLETENESS_PROOF_STORE_KIND:
        raise ContractValidationError(
            f"completeness_proof.store_kind must be {COMPLETENESS_PROOF_STORE_KIND!r}"
        )
    if proof.get("schema_version") != COMPLETENESS_SCHEMA_VERSION:
        raise ContractValidationError(
            f"completeness_proof.schema_version must be {COMPLETENESS_SCHEMA_VERSION}"
        )
    source = require_nonempty_string(proof.get("source"), label="proof.source")
    if source not in SOURCE_STATUS_IDS:
        raise ContractValidationError(
            f"proof.source {source!r} unsupported-scope for completeness"
        )
    repository = require_nonempty_string(
        proof.get("repository"), label="proof.repository"
    )
    pin = require_git_oid(proof.get("pin_revision"), label="proof.pin_revision")
    provenance_raw = require_object(
        proof.get("checkout_provenance"), label="checkout_provenance"
    )
    require_exact_keys(
        provenance_raw, required=_PROVENANCE_KEYS, label="checkout_provenance"
    )
    method = require_nonempty_string(
        provenance_raw.get("method"), label="checkout_provenance.method"
    )
    if method != _CHECKOUT_AUTH_METHOD:
        raise ContractValidationError(
            f"checkout_provenance.method must be {_CHECKOUT_AUTH_METHOD!r}"
        )
    observed = require_git_oid(
        provenance_raw.get("observed_revision"),
        label="checkout_provenance.observed_revision",
    )
    if observed != pin:
        raise ContractValidationError(
            "checkout_provenance.observed_revision must equal pin_revision"
        )
    policy_digest = require_sha256(proof.get("policy_digest"), label="policy_digest")
    seed_digest = require_sha256(proof.get("seed_digest"), label="seed_digest")
    coverage_digest = require_sha256(
        proof.get("coverage_digest"), label="coverage_digest"
    )
    source_input_digest = require_sha256(
        proof.get("source_input_digest"), label="source_input_digest"
    )
    surface_index_digest = require_sha256(
        proof.get("surface_index_digest"), label="surface_index_digest"
    )

    surfaces_raw = proof.get("discovered_surfaces")
    if not isinstance(surfaces_raw, list):
        raise ContractValidationError("discovered_surfaces must be a list")
    surfaces: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, raw in enumerate(surfaces_raw):
        surface = require_object(raw, label=f"discovered_surfaces[{idx}]")
        require_exact_keys(
            surface, required=_SURFACE_KEYS, label=f"discovered_surfaces[{idx}]"
        )
        sid = require_safe_id(
            surface.get("surface_id"), label=f"discovered_surfaces[{idx}].surface_id"
        )
        if sid in seen_ids:
            raise ContractValidationError(f"duplicate surface_id in proof: {sid}")
        seen_ids.add(sid)
        surfaces.append(
            {
                "surface_id": sid,
                "kind": require_safe_id(
                    surface.get("kind"), label=f"{sid}.kind"
                ),
                "category": require_nonempty_string(
                    surface.get("category"), label=f"{sid}.category"
                ),
                "source_path": _require_relative_posix(
                    str(surface.get("source_path")), label=f"{sid}.source_path"
                ),
                "anchor": require_nonempty_string(
                    surface.get("anchor"), label=f"{sid}.anchor"
                ),
                "content_digest": require_sha256(
                    surface.get("content_digest"), label=f"{sid}.content_digest"
                ),
                "capability_ids": require_string_list(
                    surface.get("capability_ids"),
                    label=f"{sid}.capability_ids",
                ),
            }
        )
    surfaces.sort(key=lambda item: item["surface_id"])

    unresolved = require_string_list(
        proof.get("unresolved_surfaces"), label="unresolved_surfaces"
    )
    empty_partitions = require_string_list(
        proof.get("empty_category_partitions"),
        label="empty_category_partitions",
    )
    for cat in empty_partitions:
        if cat not in PARITY_CATEGORY_TAXONOMY:
            raise ContractValidationError(
                f"empty_category_partitions unknown category {cat!r}"
            )

    # Internal consistency: surface index digest and unresolved set.
    index_surfaces = [
        {k: v for k, v in s.items() if k != "capability_ids"} for s in surfaces
    ]
    expected_index = digest_surface_index(index_surfaces)
    if expected_index != surface_index_digest:
        raise ContractValidationError(
            "proof.surface_index_digest does not match discovered_surfaces"
        )
    expected_unresolved = sorted(
        s["surface_id"] for s in surfaces if not s["capability_ids"]
    )
    if sorted(unresolved) != expected_unresolved:
        raise ContractValidationError(
            "proof.unresolved_surfaces does not match empty capability_ids"
        )

    return {
        "store_kind": COMPLETENESS_PROOF_STORE_KIND,
        "schema_version": COMPLETENESS_SCHEMA_VERSION,
        "source": source,
        "repository": repository,
        "pin_revision": pin,
        "checkout_provenance": {
            "method": method,
            "observed_revision": observed,
        },
        "policy_digest": policy_digest,
        "seed_digest": seed_digest,
        "coverage_digest": coverage_digest,
        "source_input_digest": source_input_digest,
        "surface_index_digest": surface_index_digest,
        "discovered_surfaces": surfaces,
        "unresolved_surfaces": sorted(unresolved),
        "empty_category_partitions": sorted(empty_partitions),
    }


def build_completeness_proof(
    *,
    policy: Mapping[str, Any],
    inventory: Mapping[str, Any],
    upstream_root: Path | str,
    seed: Mapping[str, Any] | None = None,
    surface_mappings: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Build a candidate proof from policy + checkout + inventory (no mutation)."""
    validated_policy = validate_completeness_policy(policy)
    source = validated_policy["source"]
    coverage = coverage_projection_for_source(inventory, source)
    pin = coverage["pin"]
    if pin["repository"] != validated_policy["repository"]:
        raise ContractValidationError(
            "policy.repository does not match inventory upstream_pins repository"
        )
    index = reproduce_source_index(
        validated_policy, upstream_root, pin_revision=pin["revision"]
    )
    mapped, unresolved = _apply_surface_mappings(
        surfaces=index["discovered_surfaces"],
        mappings=surface_mappings,
        inventory=inventory,
        source=source,
    )
    present = {s["category"] for s in mapped}
    empty_partitions = sorted(PARITY_CATEGORY_TAXONOMY - present)
    proof = {
        "store_kind": COMPLETENESS_PROOF_STORE_KIND,
        "schema_version": COMPLETENESS_SCHEMA_VERSION,
        "source": source,
        "repository": validated_policy["repository"],
        "pin_revision": pin["revision"],
        "checkout_provenance": dict(index["checkout_provenance"]),
        "policy_digest": digest_policy(validated_policy),
        "seed_digest": digest_seed_catalog(seed),
        "coverage_digest": canonical_json_digest(coverage),
        "source_input_digest": index["source_input_digest"],
        "surface_index_digest": digest_surface_index(
            [{k: v for k, v in s.items() if k != "capability_ids"} for s in mapped]
        ),
        "discovered_surfaces": mapped,
        "unresolved_surfaces": unresolved,
        "empty_category_partitions": empty_partitions,
    }
    return validate_completeness_proof(proof)


def plan_completeness_proof(
    *,
    policy: Mapping[str, Any],
    inventory: Mapping[str, Any],
    upstream_root: Path | str,
    seed: Mapping[str, Any] | None = None,
    surface_mappings: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Maintainer plan mode: emit candidate proof metadata; never write inventory."""
    proof = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=upstream_root,
        seed=seed,
        surface_mappings=surface_mappings,
    )
    return {
        "ok": True,
        "mode": "plan",
        "mutates_inventory": False,
        "mutates_proof_artifact": False,
        "candidate_proof": proof,
        "unresolved_surfaces": list(proof["unresolved_surfaces"]),
        "empty_category_partitions": list(proof["empty_category_partitions"]),
    }


def verify_completeness_proof(
    proof: Mapping[str, Any],
    *,
    policy: Mapping[str, Any],
    inventory: Mapping[str, Any],
    seed: Mapping[str, Any] | None = None,
    upstream_root: Path | str | None = None,
    require_no_unresolved: bool = True,
) -> dict[str, Any]:
    """Verify proof bindings; optionally reproduce against an upstream checkout."""
    validated_proof = validate_completeness_proof(proof)
    validated_policy = validate_completeness_policy(policy)
    source = validated_proof["source"]
    if validated_policy["source"] != source:
        raise ContractValidationError("proof.source does not match policy.source")
    if validated_policy["repository"] != validated_proof["repository"]:
        raise ContractValidationError(
            "proof.repository does not match policy.repository"
        )

    expected_policy = digest_policy(validated_policy)
    if validated_proof["policy_digest"] != expected_policy:
        raise ContractValidationError("proof.policy_digest drift")

    coverage = coverage_projection_for_source(inventory, source)
    if coverage["pin"]["repository"] != validated_proof["repository"]:
        raise ContractValidationError("proof.repository does not match inventory pin")
    if coverage["pin"]["revision"] != validated_proof["pin_revision"]:
        raise ContractValidationError("proof.pin_revision does not match inventory pin")
    expected_coverage = canonical_json_digest(coverage)
    if validated_proof["coverage_digest"] != expected_coverage:
        raise ContractValidationError("proof.coverage_digest drift")

    expected_seed = digest_seed_catalog(seed)
    if validated_proof["seed_digest"] != expected_seed:
        raise ContractValidationError("proof.seed_digest drift")

    # Re-validate mappings against inventory (alias / cross-source / unknown).
    index = _capability_index(inventory)
    for surface in validated_proof["discovered_surfaces"]:
        sid = surface["surface_id"]
        for cap_id in surface["capability_ids"]:
            _resolve_canonical_capability(
                cap_id,
                inventory=inventory,
                index=index,
                source=source,
                category=surface["category"],
                surface_id=sid,
            )

    if require_no_unresolved and validated_proof["unresolved_surfaces"]:
        raise ContractValidationError(
            "completeness proof has unresolved surfaces: "
            + ",".join(validated_proof["unresolved_surfaces"])
        )

    if upstream_root is not None:
        # Re-authenticate checkout bytes to the claimed pin before comparing digests.
        if (
            validated_proof["checkout_provenance"].get("observed_revision")
            != validated_proof["pin_revision"]
        ):
            raise ContractValidationError(
                "checkout_provenance.observed_revision does not match pin_revision"
            )
        reproduced = reproduce_source_index(
            validated_policy,
            upstream_root,
            pin_revision=validated_proof["pin_revision"],
        )
        if (
            reproduced["checkout_provenance"]["observed_revision"]
            != validated_proof["pin_revision"]
        ):
            raise ContractValidationError(
                "upstream_root HEAD does not match proof.pin_revision"
            )
        if reproduced["source_input_digest"] != validated_proof["source_input_digest"]:
            raise ContractValidationError("proof.source_input_digest reproduction drift")
        if reproduced["surface_index_digest"] != validated_proof["surface_index_digest"]:
            raise ContractValidationError(
                "proof.surface_index_digest reproduction drift"
            )
        if (
            sorted(reproduced["empty_category_partitions"])
            != validated_proof["empty_category_partitions"]
        ):
            raise ContractValidationError(
                "proof.empty_category_partitions reproduction drift"
            )

    return {
        "ok": True,
        "source": source,
        "pin_revision": validated_proof["pin_revision"],
        "unresolved_surfaces": list(validated_proof["unresolved_surfaces"]),
        "empty_category_partitions": list(
            validated_proof["empty_category_partitions"]
        ),
        "surfaces": len(validated_proof["discovered_surfaces"]),
    }


@dataclass(frozen=True)
class CompletenessGateResult:
    completeness_gate_checked: bool
    completeness_proofs_required: bool
    completeness_proofs_verified: int
    promoted_sources: tuple[str, ...]
    promoted_categories: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "completeness_gate_checked": self.completeness_gate_checked,
            "completeness_proofs_required": self.completeness_proofs_required,
            "completeness_proofs_verified": self.completeness_proofs_verified,
            "promoted_sources": list(self.promoted_sources),
            "promoted_categories": list(self.promoted_categories),
        }


def _load_optional_seed(repo_root: Path, source: str) -> dict[str, Any] | None:
    path = repo_root / "docs" / "parity" / "upstream-snapshots" / f"{source}.json"
    if not path.is_file():
        return None
    return load_json_object(path)


def _load_policy_and_proof(
    repo_root: Path,
    source: str,
    *,
    policies_by_source: Mapping[str, Mapping[str, Any]] | None = None,
    proofs_by_source: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if proofs_by_source is not None and source in proofs_by_source:
        proof = dict(proofs_by_source[source])
    else:
        proof_path = repo_root / DEFAULT_PROOF_DIR_RELATIVE / f"{source}.json"
        if not proof_path.is_file():
            raise ContractValidationError(
                f"source_status[{source}]==complete requires completeness proof at "
                f"{DEFAULT_PROOF_DIR_RELATIVE}/{source}.json"
            )
        proof = load_json_object(proof_path)

    if policies_by_source is not None and source in policies_by_source:
        policy = dict(policies_by_source[source])
    else:
        policy_path = repo_root / DEFAULT_POLICY_DIR_RELATIVE / f"{source}.json"
        if not policy_path.is_file():
            raise ContractValidationError(
                f"source_status[{source}]==complete requires completeness policy at "
                f"{DEFAULT_POLICY_DIR_RELATIVE}/{source}.json"
            )
        policy = load_json_object(policy_path)
    return policy, proof


def assert_completeness_promotion(
    inventory: Mapping[str, Any],
    *,
    repo_root: Path | str | None = None,
    proofs_by_source: Mapping[str, Mapping[str, Any]] | None = None,
    policies_by_source: Mapping[str, Mapping[str, Any]] | None = None,
    seeds_by_source: Mapping[str, Mapping[str, Any] | None] | None = None,
    upstream_roots: Mapping[str, Path | str] | None = None,
    allow_seed_as_proof: bool = False,
) -> CompletenessGateResult:
    """Fail closed when promoted statuses lack valid completeness proofs.

    ``allow_seed_as_proof`` is always rejected (seeds are never proofs); the
    parameter exists so tests can document that catalogue seeds do not satisfy
    the gate.
    """
    if allow_seed_as_proof:
        raise ContractValidationError(
            "upstream seed catalogue is not a completeness proof"
        )

    if inventory.get("schema_version") != 2:
        return CompletenessGateResult(
            completeness_gate_checked=True,
            completeness_proofs_required=False,
            completeness_proofs_verified=0,
            promoted_sources=(),
            promoted_categories=(),
        )

    source_status = inventory.get("source_status")
    category_status = inventory.get("category_status")
    if not isinstance(source_status, Mapping) or not isinstance(category_status, Mapping):
        raise ContractValidationError(
            "completeness gate requires source_status and category_status maps"
        )

    promoted_sources = tuple(
        sorted(
            s
            for s in SOURCE_STATUS_IDS
            if source_status.get(s) == "complete"
        )
    )
    promoted_categories = tuple(
        sorted(
            c
            for c, status in category_status.items()
            if status == "complete" and c in PARITY_CATEGORY_TAXONOMY
        )
    )
    inventory_promoted = inventory.get("inventory_status") == "complete"
    required = bool(promoted_sources or promoted_categories or inventory_promoted)

    if not required:
        return CompletenessGateResult(
            completeness_gate_checked=True,
            completeness_proofs_required=False,
            completeness_proofs_verified=0,
            promoted_sources=(),
            promoted_categories=(),
        )

    root = Path(repo_root) if repo_root is not None else None
    verified = 0
    verified_sources: set[str] = set()

    # Source promotions: each needs its own valid proof with no unresolved surfaces.
    for source in promoted_sources:
        if root is None and (
            proofs_by_source is None or source not in proofs_by_source
        ):
            raise ContractValidationError(
                f"source_status[{source}]==complete requires a completeness proof"
            )
        if root is not None:
            policy, proof = _load_policy_and_proof(
                root,
                source,
                policies_by_source=policies_by_source,
                proofs_by_source=proofs_by_source,
            )
        else:
            assert proofs_by_source is not None
            proof = dict(proofs_by_source[source])
            if policies_by_source is None or source not in policies_by_source:
                raise ContractValidationError(
                    f"source_status[{source}]==complete requires a completeness policy"
                )
            policy = dict(policies_by_source[source])

        seed: Mapping[str, Any] | None
        if seeds_by_source is not None and source in seeds_by_source:
            seed = seeds_by_source[source]
        elif root is not None:
            seed = _load_optional_seed(root, source)
        else:
            seed = None

        upstream = None
        if upstream_roots is not None and source in upstream_roots:
            upstream = upstream_roots[source]

        verify_completeness_proof(
            proof,
            policy=policy,
            inventory=inventory,
            seed=seed,
            upstream_root=upstream,
            require_no_unresolved=True,
        )
        verified += 1
        verified_sources.add(source)

    # Category promotions require valid proofs for ALL four sources and an
    # explicit empty partition (or discovered surfaces) for that category.
    if promoted_categories:
        needed_sources = tuple(SOURCE_STATUS_IDS)
        proofs_cache: dict[str, dict[str, Any]] = {}
        for source in needed_sources:
            if source in verified_sources:
                # Already validated above; still need the proof object for partitions.
                if proofs_by_source is not None and source in proofs_by_source:
                    proofs_cache[source] = validate_completeness_proof(
                        proofs_by_source[source]
                    )
                elif root is not None:
                    _policy, proof = _load_policy_and_proof(
                        root,
                        source,
                        policies_by_source=policies_by_source,
                        proofs_by_source=proofs_by_source,
                    )
                    proofs_cache[source] = validate_completeness_proof(proof)
                else:
                    raise ContractValidationError(
                        f"category promotion missing proof for source {source}"
                    )
                continue

            if root is None and (
                proofs_by_source is None or source not in proofs_by_source
            ):
                raise ContractValidationError(
                    "category_status complete requires completeness proofs "
                    f"for every source; missing {source}"
                )
            if root is not None:
                policy, proof = _load_policy_and_proof(
                    root,
                    source,
                    policies_by_source=policies_by_source,
                    proofs_by_source=proofs_by_source,
                )
            else:
                assert proofs_by_source is not None
                proof = dict(proofs_by_source[source])
                if policies_by_source is None or source not in policies_by_source:
                    raise ContractValidationError(
                        f"category promotion missing policy for source {source}"
                    )
                policy = dict(policies_by_source[source])

            if seeds_by_source is not None and source in seeds_by_source:
                seed = seeds_by_source[source]
            elif root is not None:
                seed = _load_optional_seed(root, source)
            else:
                seed = None
            upstream = None
            if upstream_roots is not None and source in upstream_roots:
                upstream = upstream_roots[source]

            verify_completeness_proof(
                proof,
                policy=policy,
                inventory=inventory,
                seed=seed,
                upstream_root=upstream,
                require_no_unresolved=True,
            )
            verified += 1
            verified_sources.add(source)
            proofs_cache[source] = validate_completeness_proof(proof)

        for category in promoted_categories:
            for source in needed_sources:
                proof = proofs_cache[source]
                has_surface = any(
                    s["category"] == category for s in proof["discovered_surfaces"]
                )
                has_empty = category in proof["empty_category_partitions"]
                if not has_surface and not has_empty:
                    raise ContractValidationError(
                        f"category_status[{category}]==complete missing source "
                        f"partition for {source} (need surfaces or explicit empty)"
                    )

    if inventory_promoted:
        # Indirectly requires every source + category complete (schema helper),
        # which already forced proofs above; still fail if any status lags.
        from omg_cli.contracts.parity_schema import inventory_is_complete

        if not inventory_is_complete(inventory):
            raise ContractValidationError(
                "inventory_status==complete requires every source_status and "
                "category_status to be complete (and therefore proof-gated)"
            )

    return CompletenessGateResult(
        completeness_gate_checked=True,
        completeness_proofs_required=True,
        completeness_proofs_verified=verified,
        promoted_sources=promoted_sources,
        promoted_categories=promoted_categories,
    )


def check_completeness_promotion_gate(
    inventory: Mapping[str, Any],
    *,
    repo_root: Path | str | None = None,
    proofs_by_source: Mapping[str, Mapping[str, Any]] | None = None,
    policies_by_source: Mapping[str, Mapping[str, Any]] | None = None,
    seeds_by_source: Mapping[str, Mapping[str, Any] | None] | None = None,
    upstream_roots: Mapping[str, Path | str] | None = None,
) -> dict[str, Any]:
    """Public gate entry used by strict parity checks."""
    result = assert_completeness_promotion(
        inventory,
        repo_root=repo_root,
        proofs_by_source=proofs_by_source,
        policies_by_source=policies_by_source,
        seeds_by_source=seeds_by_source,
        upstream_roots=upstream_roots,
    )
    return result.as_dict()
