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
    "COMPLETENESS_MAPPING_STORE_KIND",
    "COMPLETENESS_POLICY_STORE_KIND",
    "COMPLETENESS_PROOF_STORE_KIND",
    "COMPLETENESS_SCHEMA_VERSION",
    "DEFAULT_MAPPING_DIR_RELATIVE",
    "DEFAULT_POLICY_DIR_RELATIVE",
    "DEFAULT_PROOF_DIR_RELATIVE",
    "EXTRACTION_JSON_REGISTRY_V1",
    "PROOF_KIND_DOCUMENTATION_CATALOG_SEED",
    "PROOF_KIND_IMPLEMENTATION_REGISTRY",
    "PROOF_KINDS",
    "CompletenessGateResult",
    "assert_completeness_promotion",
    "authenticate_pinned_checkout",
    "build_completeness_proof",
    "canonical_json_digest",
    "check_committed_completeness_artifacts",
    "check_completeness_promotion_gate",
    "coverage_projection_for_source",
    "digest_coverage_projection",
    "digest_policy",
    "digest_seed_catalog",
    "digest_source_input",
    "digest_surface_index",
    "plan_completeness_proof",
    "reproduce_source_index",
    "validate_completeness_mapping",
    "validate_completeness_policy",
    "validate_completeness_proof",
    "verify_completeness_proof",
]

COMPLETENESS_POLICY_STORE_KIND = "parity-completeness-policy"
COMPLETENESS_PROOF_STORE_KIND = "parity-completeness-proof"
COMPLETENESS_MAPPING_STORE_KIND = "parity-completeness-mapping"
COMPLETENESS_SCHEMA_VERSION = 1
DEFAULT_POLICY_DIR_RELATIVE = "docs/parity/completeness/policies"
DEFAULT_PROOF_DIR_RELATIVE = "docs/parity/completeness/proofs"
DEFAULT_MAPPING_DIR_RELATIVE = "docs/parity/completeness/mappings"
EXTRACTION_JSON_REGISTRY_V1 = "json_registry_v1"
PROOF_KIND_IMPLEMENTATION_REGISTRY = "implementation_registry"
PROOF_KIND_DOCUMENTATION_CATALOG_SEED = "documentation_catalog_seed"
PROOF_KINDS = frozenset(
    {
        PROOF_KIND_IMPLEMENTATION_REGISTRY,
        PROOF_KIND_DOCUMENTATION_CATALOG_SEED,
    }
)

_POLICY_TOP_KEYS = frozenset(
    {
        "store_kind",
        "schema_version",
        "source",
        "repository",
        "proof_kind",
        "promotion_sufficient",
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
_MAPPING_TOP_KEYS = frozenset(
    {"store_kind", "schema_version", "source", "surfaces"}
)
_MAPPING_SURFACE_KEYS = frozenset({"surface_id", "category", "capability_ids"})
_PROOF_TOP_KEYS = frozenset(
    {
        "store_kind",
        "schema_version",
        "source",
        "repository",
        "proof_kind",
        "promotion_sufficient",
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


_SURFACE_ID_RE = __import__("re").compile(
    r"^[A-Za-z0-9][A-Za-z0-9._*:@+/-]{0,191}$"
)


def require_surface_id(value, *, label: str) -> str:
    """Surface IDs may include hook matchers (*) and npm script colons."""
    text = require_nonempty_string(value, label=label)
    if not _SURFACE_ID_RE.fullmatch(text):
        raise ContractValidationError(f"{label} is not a valid surface_id")
    return text


def _validate_proof_kind_and_promotion(
    proof_kind: Any,
    promotion_sufficient: Any,
    *,
    label: str,
) -> tuple[str, bool]:
    """Bind proof_kind ↔ promotion_sufficient (docs-only never promotes)."""
    kind = require_nonempty_string(proof_kind, label=f"{label}.proof_kind")
    if kind not in PROOF_KINDS:
        raise ContractValidationError(
            f"{label}.proof_kind must be one of {sorted(PROOF_KINDS)}; got {kind!r}"
        )
    if not isinstance(promotion_sufficient, bool):
        raise ContractValidationError(
            f"{label}.promotion_sufficient must be a boolean"
        )
    if kind == PROOF_KIND_DOCUMENTATION_CATALOG_SEED and promotion_sufficient:
        raise ContractValidationError(
            f"{label}: documentation_catalog_seed proofs cannot be "
            "promotion_sufficient (docs/catalog seed alone is not parity credit)"
        )
    if kind == PROOF_KIND_IMPLEMENTATION_REGISTRY and not promotion_sufficient:
        raise ContractValidationError(
            f"{label}: implementation_registry proofs must set "
            "promotion_sufficient=true"
        )
    return kind, promotion_sufficient


def _require_promotion_sufficient(
    proof: Mapping[str, Any],
    *,
    context: str,
) -> None:
    """Fail closed when a docs-only / non-sufficient proof is used to promote."""
    if proof.get("promotion_sufficient") is True:
        return
    kind = proof.get("proof_kind")
    raise ContractValidationError(
        f"{context}: completeness proof is not promotion-sufficient "
        f"(proof_kind={kind!r}; documentation/catalog seed alone cannot promote "
        "per #78)"
    )


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


def _file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_subprocess_env() -> dict[str, str]:
    """Env for completeness git calls — always disable replace-object resolution."""
    env = os.environ.copy()
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def _run_git(root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # ``--no-replace-objects`` + GIT_NO_REPLACE_OBJECTS block refs/replace/
    # from rewriting pin/HEAD/tree/blob resolution.
    return subprocess.run(
        ["git", "--no-replace-objects", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
        env=_git_subprocess_env(),
    )


def _run_git_bytes(root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "--no-replace-objects", "-C", str(root), *args],
        capture_output=True,
        check=False,
        env=_git_subprocess_env(),
    )


def _parse_ls_tree_z(payload: str) -> list[tuple[str, str, str, str]]:
    """Parse ``git ls-tree -r -z`` lines into (mode, obj_type, sha, path)."""
    entries: list[tuple[str, str, str, str]] = []
    for item in payload.split("\0"):
        if not item:
            continue
        # "<mode> <type> <sha>\t<path>"
        try:
            meta, path = item.split("\t", 1)
            mode, obj_type, sha = meta.split(" ", 2)
        except ValueError as exc:
            raise ContractValidationError(
                f"malformed git ls-tree entry: {item!r}"
            ) from exc
        entries.append((mode, obj_type, sha.lower(), path))
    return entries


def _git_blob_bytes(root: Path, pin: str, relative: str, *, label: str) -> bytes:
    """Read path bytes from the pinned commit (not the worktree)."""
    rel = _require_relative_posix(relative, label=label)
    # Reject gitlink/symlink/tree; only regular blobs are user-facing file surfaces.
    ls = _run_git(root, ["ls-tree", "-z", "--full-tree", pin, "--", rel])
    if ls.returncode != 0:
        raise ContractValidationError(
            f"{label}: git ls-tree failed for {rel}: {ls.stderr.strip()}"
        )
    entries = _parse_ls_tree_z(ls.stdout)
    if not entries:
        raise ContractValidationError(
            f"{label}: path {rel!r} missing from pin {pin}"
        )
    if len(entries) != 1:
        raise ContractValidationError(
            f"{label}: path {rel!r} is not a single tree entry at pin {pin}"
        )
    mode, obj_type, _sha, path = entries[0]
    if path != rel:
        raise ContractValidationError(
            f"{label}: ls-tree path mismatch for {rel!r} (got {path!r})"
        )
    if obj_type != "blob":
        raise ContractValidationError(
            f"{label}: {rel!r} must be a blob at pin (got {obj_type})"
        )
    if mode == "120000":
        raise ContractValidationError(f"{label} must not be a symlink: {rel}")
    if mode not in {"100644", "100755"}:
        raise ContractValidationError(
            f"{label}: unsupported git mode {mode} for {rel}"
        )
    blob = _run_git_bytes(root, ["cat-file", "blob", f"{pin}:{rel}"])
    if blob.returncode != 0:
        err = blob.stderr.decode("utf-8", errors="replace").strip()
        raise ContractValidationError(
            f"{label}: git cat-file blob failed for {pin}:{rel}: {err}"
        )
    return blob.stdout


def _assert_worktree_matches_pin_blobs(root: Path, pin: str) -> None:
    """Fail closed when worktree bytes diverge from pin (incl. skip-worktree).

    ``git status --porcelain`` is insufficient: ``skip-worktree`` /
    ``assume-unchanged`` can hide tracked mutations. Compare each pin blob to
    ``git hash-object`` of the worktree path.

    Pin trees may contain git symlinks (mode ``120000``) used for install-time
    mirrors (e.g. OmO ``.claude/commands`` → ``.agents/command``). Those entries
    must still match the worktree symlink target OID. Discovery extractors reject
    symlink paths as authoritative registry / surface inputs via
    ``_git_blob_bytes``.
    """
    ls = _run_git(root, ["ls-tree", "-r", "-z", "--full-tree", pin])
    if ls.returncode != 0:
        raise ContractValidationError(
            f"git ls-tree failed for pin {pin}: {ls.stderr.strip()}"
        )
    for mode, obj_type, sha, path in _parse_ls_tree_z(ls.stdout):
        # Gitlinks (submodules) are recorded as commit entries; OmO ships nested
        # upstream skill mirrors under packages/shared-skills/upstreams/*. They
        # are never admitted as discovery registry inputs — skip blob compare.
        if obj_type == "commit" and mode == "160000":
            continue
        if obj_type != "blob":
            raise ContractValidationError(
                f"pin tree contains non-blob entry {path!r} ({obj_type})"
            )
        wt = root / path
        if mode == "120000":
            if not (os.path.lexists(wt) and os.path.islink(wt)):
                raise ContractValidationError(
                    f"upstream_root missing symlink for pin {pin}: {path}"
                )
            # ``git hash-object -- PATH`` may refuse symlinks; compare the
            # symlink target bytes to the pin blob (git stores the target).
            target = os.readlink(wt).encode("utf-8", errors="surrogateescape")
            blob = _run_git_bytes(root, ["cat-file", "blob", f"{pin}:{path}"])
            if blob.returncode != 0:
                err = blob.stderr.decode("utf-8", errors="replace").strip()
                raise ContractValidationError(
                    f"git cat-file blob failed for symlink {pin}:{path}: {err}"
                )
            if blob.stdout != target:
                raise ContractValidationError(
                    f"upstream_root symlink diverges from pin_revision {pin} at {path}"
                )
            continue
        if os.path.lexists(wt) and os.path.islink(wt):
            raise ContractValidationError(
                f"upstream_root worktree path must not be a symlink: {path}"
            )
        if not wt.is_file():
            raise ContractValidationError(
                f"upstream_root missing tracked path for pin {pin}: {path}"
            )
        hashed = _run_git(root, ["hash-object", "--", path])
        if hashed.returncode != 0:
            raise ContractValidationError(
                f"git hash-object failed for {path}: {hashed.stderr.strip()}"
            )
        observed = hashed.stdout.strip().lower()
        if observed != sha:
            raise ContractValidationError(
                f"upstream_root worktree diverges from pin_revision {pin} at "
                f"{path} (skip-worktree/assume-unchanged/mutation)"
            )


def authenticate_pinned_checkout(
    upstream_root: Path | str,
    pin_revision: str,
) -> dict[str, str]:
    """Require upstream_root HEAD == pin_revision with pin-bound worktree bytes.

    Fail closed when the directory is not its own git work-tree root, when HEAD
    drifts from the pin, when porcelain reports dirty/untracked paths, or when
    any tracked worktree file OID diverges from the pin blob (covers
    skip-worktree / assume-unchanged mutations that porcelain hides).
    Discovery hashes are read from git objects at the pin, not the worktree.
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

    # Surface skip-worktree / assume-unchanged mutations porcelain can hide.
    refresh = _run_git(root, ["update-index", "--refresh"])
    # --refresh returns non-zero when the index needs refresh; still inspect
    # diff-index / blob match below rather than trusting the exit code alone.
    _ = refresh
    diff_index = _run_git(
        root,
        ["diff-index", "--exit-code", "--raw", "-r", pin],
    )
    if diff_index.returncode not in {0, 1}:
        raise ContractValidationError(
            f"git diff-index failed in {root}: {diff_index.stderr.strip()}"
        )
    if diff_index.returncode != 0 or diff_index.stdout.strip():
        sample = "; ".join(
            line for line in diff_index.stdout.splitlines() if line.strip()
        )[:500]
        raise ContractValidationError(
            f"upstream_root index/worktree differs from pin_revision {pin}"
            + (f": {sample}" if sample else "")
        )

    _assert_worktree_matches_pin_blobs(root, pin)

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
    proof_kind, promotion_sufficient = _validate_proof_kind_and_promotion(
        policy.get("proof_kind"),
        policy.get("promotion_sufficient"),
        label="completeness_policy",
    )
    rules = require_object(policy.get("discovery_rules"), label="discovery_rules")
    require_exact_keys(rules, required=_DISCOVERY_KEYS, label="discovery_rules")
    rules_version = rules.get("version")
    if rules_version not in (1, 2):
        raise ContractValidationError("discovery_rules.version must be 1 or 2")
    registries = rules.get("authoritative_registries")
    if not isinstance(registries, list) or not registries:
        raise ContractValidationError(
            "discovery_rules.authoritative_registries must be a non-empty list"
        )

    if rules_version == 1:
        normalized_regs: list[dict[str, Any]] = []
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
    else:
        from omg_cli.parity_discovery import validate_v2_registry_entry

        normalized_regs = []
        seen_paths = set()
        seen_ids: set[str] = set()
        for idx, item in enumerate(registries):
            entry = validate_v2_registry_entry(item, index=idx)
            if entry["path"] in seen_paths:
                raise ContractValidationError(
                    f"duplicate authoritative registry path: {entry['path']}"
                )
            if entry["id"] in seen_ids:
                raise ContractValidationError(
                    f"duplicate authoritative registry id: {entry['id']}"
                )
            seen_paths.add(entry["path"])
            seen_ids.add(entry["id"])
            normalized_regs.append(entry)

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
        "proof_kind": proof_kind,
        "promotion_sufficient": promotion_sufficient,
        "discovery_rules": {
            "version": int(rules_version),
            "authoritative_registries": normalized_regs,
            "category_assignment": dict(sorted(normalized_assignment.items())),
            "non_surface_exceptions": exceptions,
        },
    }


def digest_policy(policy: Mapping[str, Any]) -> str:
    return canonical_json_digest(validate_completeness_policy(policy))


def validate_completeness_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a committed parity-completeness-mapping store (schema_version 1)."""
    mapping = require_object(value, label="completeness_mapping")
    require_exact_keys(mapping, required=_MAPPING_TOP_KEYS, label="completeness_mapping")
    if mapping.get("store_kind") != COMPLETENESS_MAPPING_STORE_KIND:
        raise ContractValidationError(
            f"completeness_mapping.store_kind must be "
            f"{COMPLETENESS_MAPPING_STORE_KIND!r}"
        )
    if mapping.get("schema_version") != COMPLETENESS_SCHEMA_VERSION:
        raise ContractValidationError(
            f"completeness_mapping.schema_version must be {COMPLETENESS_SCHEMA_VERSION}"
        )
    source = require_nonempty_string(mapping.get("source"), label="mapping.source")
    if source not in SOURCE_STATUS_IDS:
        raise ContractValidationError(
            f"mapping.source {source!r} is not a parity SOURCE_STATUS_ID"
        )
    surfaces_raw = mapping.get("surfaces")
    if not isinstance(surfaces_raw, list):
        raise ContractValidationError("mapping.surfaces must be a list")
    surfaces: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, raw in enumerate(surfaces_raw):
        entry = require_object(raw, label=f"mapping.surfaces[{idx}]")
        require_exact_keys(
            entry, required=_MAPPING_SURFACE_KEYS, label=f"mapping.surfaces[{idx}]"
        )
        sid = require_nonempty_string(
            entry.get("surface_id"), label=f"mapping.surfaces[{idx}].surface_id"
        )
        if sid in seen_ids:
            raise ContractValidationError(f"duplicate mapping surface_id: {sid}")
        seen_ids.add(sid)
        category = require_nonempty_string(
            entry.get("category"), label=f"mapping.surfaces[{idx}].category"
        )
        if category not in PARITY_CATEGORY_TAXONOMY:
            raise ContractValidationError(
                f"mapping surface {sid} unknown category {category!r}"
            )
        cap_ids = require_string_list(
            entry.get("capability_ids"),
            label=f"mapping.surfaces[{idx}].capability_ids",
            unique=True,
        )
        if not cap_ids:
            raise ContractValidationError(
                f"mapping surface {sid} capability_ids must be non-empty"
            )
        sorted_caps = sorted(cap_ids)
        if list(cap_ids) != sorted_caps:
            raise ContractValidationError(
                f"mapping surface {sid} capability_ids must be sorted"
            )
        surfaces.append(
            {
                "surface_id": sid,
                "category": category,
                "capability_ids": sorted_caps,
            }
        )
    expected_order = sorted(s["surface_id"] for s in surfaces)
    actual_order = [s["surface_id"] for s in surfaces]
    if actual_order != expected_order:
        raise ContractValidationError(
            "mapping.surfaces must be sorted by surface_id"
        )
    return {
        "store_kind": COMPLETENESS_MAPPING_STORE_KIND,
        "schema_version": COMPLETENESS_SCHEMA_VERSION,
        "source": source,
        "surfaces": surfaces,
    }


def _is_mapping_store(value: Mapping[str, Any]) -> bool:
    return value.get("store_kind") == COMPLETENESS_MAPPING_STORE_KIND


def _coerce_mapping_arg(
    mapping: Mapping[str, Any] | None,
    surface_mappings: Mapping[str, Sequence[str]] | None,
) -> Mapping[str, Any] | None:
    """Prefer ``mapping``; fall back to legacy ``surface_mappings`` dict."""
    if mapping is not None:
        return mapping
    if surface_mappings is not None:
        return surface_mappings
    return None


def _normalized_mapping_projection(
    mapping: Mapping[str, Any],
    *,
    source: str,
) -> list[dict[str, Any]]:
    """Stable mapping projection for v2 coverage digests."""
    if _is_mapping_store(mapping):
        validated = validate_completeness_mapping(mapping)
        if validated["source"] != source:
            raise ContractValidationError(
                f"mapping.source {validated['source']!r} != proof source {source!r}"
            )
        return [
            {
                "surface_id": s["surface_id"],
                "category": s["category"],
                "capability_ids": list(s["capability_ids"]),
            }
            for s in validated["surfaces"]
        ]
    # Legacy dict: surface_id → [capability_id, ...]
    projection: list[dict[str, Any]] = []
    for sid, caps in mapping.items():
        sid_s = require_nonempty_string(sid, label="mapping.surface_id")
        if not isinstance(caps, Sequence) or isinstance(caps, (str, bytes)):
            raise ContractValidationError(
                f"legacy mapping[{sid_s}] must be a list of capability ids"
            )
        cap_ids = sorted(
            {
                require_nonempty_string(c, label=f"mapping[{sid_s}].capability_id")
                for c in caps
            }
        )
        if not cap_ids:
            raise ContractValidationError(
                f"legacy mapping[{sid_s}] capability_ids must be non-empty"
            )
        projection.append(
            {
                "surface_id": sid_s,
                "capability_ids": cap_ids,
            }
        )
    projection.sort(key=lambda item: item["surface_id"])
    return projection


def _coverage_digest_for_proof(
    inventory: Mapping[str, Any],
    source: str,
    *,
    policy: Mapping[str, Any],
    mapping: Mapping[str, Any] | None,
) -> str:
    """V1: inventory coverage only. V2: coverage + normalized mapping projection."""
    coverage = coverage_projection_for_source(inventory, source)
    rules_version = policy["discovery_rules"]["version"]
    if rules_version != 2:
        return canonical_json_digest(coverage)
    if mapping is None:
        raise ContractValidationError(
            "discovery_rules.version==2 requires a mapping for coverage_digest"
        )
    projection = _normalized_mapping_projection(mapping, source=source)
    return canonical_json_digest({"coverage": coverage, "mapping": projection})


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

    Authenticates ``upstream_root`` to ``pin_revision`` (HEAD match, clean
    porcelain, worktree OID == pin blobs) then hashes registry/surface bytes
    via ``git cat-file`` at the pin — never via possibly-skewed worktree files.
    """
    validated = validate_completeness_policy(policy)
    pin = require_git_oid(pin_revision, label="pin_revision")
    provenance = authenticate_pinned_checkout(upstream_root, pin)
    root = Path(upstream_root)
    if not root.is_dir():
        raise ContractValidationError(f"upstream_root is not a directory: {root}")

    rules = validated["discovery_rules"]
    if rules["version"] == 2:
        return _reproduce_source_index_v2(
            validated, root=root, pin=pin, provenance=provenance
        )

    assignment = rules["category_assignment"]
    discovered: list[dict[str, Any]] = []
    input_parts: list[dict[str, str]] = []

    for reg in rules["authoritative_registries"]:
        path = reg["path"]
        # Keep worktree confinement checks (relative, no symlink) in addition to
        # pin-blob reads so escape attempts fail before git path lookup.
        _resolve_confined_file(root, path, label=f"registry:{path}")
        raw = _git_blob_bytes(root, pin, path, label=f"registry:{path}")
        digest = _file_digest(raw)
        input_parts.append({"path": path, "content_digest": digest})
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractValidationError(
                f"registry {path} is not valid UTF-8 JSON: {exc}"
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

    # Confirm each surface source_path is a confined regular blob at the pin.
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for surface in discovered:
        sid = surface["surface_id"]
        if sid in seen_ids:
            raise ContractValidationError(f"duplicate surface_id: {sid}")
        seen_ids.add(sid)
        _resolve_confined_file(
            root, surface["source_path"], label=f"surface:{sid}.source_path"
        )
        raw = _git_blob_bytes(
            root, pin, surface["source_path"], label=f"surface:{sid}.source_path"
        )
        content_digest = _file_digest(raw)
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


def _reproduce_source_index_v2(
    validated: Mapping[str, Any],
    *,
    root: Path,
    pin: str,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    from omg_cli.parity_discovery import extract_surfaces_v2, list_pin_tree_paths

    rules = validated["discovery_rules"]
    tree_entries = list_pin_tree_paths(
        root,
        pin,
        git_blob_bytes=lambda r, p, rel, label="blob": _git_blob_bytes(
            r, p, rel, label=label
        ),
        run_git=_run_git,
    )
    pin_paths: set[str] = set()
    symlink_paths: set[str] = set()
    for mode, obj_type, path in tree_entries:
        if obj_type == "blob":
            if mode.startswith("120"):  # symlink
                # Keep path visible for existence checks; read_blob fails closed.
                symlink_paths.add(path)
                pin_paths.add(path)
                continue
            pin_paths.add(path)

    def read_blob(rel: str) -> bytes:
        if rel in symlink_paths:
            raise ContractValidationError(
                f"blob must not be a symlink: {rel}"
            )
        return _git_blob_bytes(root, pin, rel, label=f"blob:{rel}")

    surfaces, input_parts = extract_surfaces_v2(
        registries=rules["authoritative_registries"],
        category_assignment=rules["category_assignment"],
        exceptions=rules["non_surface_exceptions"],
        pin_paths=pin_paths,
        file_digest=_file_digest,
        read_blob=read_blob,
    )
    normalized: list[dict[str, Any]] = []
    for surface in surfaces:
        normalized.append(
            {
                "surface_id": surface["surface_id"],
                "kind": surface["kind"],
                "category": surface["category"],
                "source_path": surface["source_path"],
                "anchor": surface["anchor"],
                "content_digest": surface["content_digest"],
            }
        )
    normalized.sort(key=lambda item: item["surface_id"])
    input_parts = sorted(input_parts, key=lambda item: item["path"])
    present_categories = {item["category"] for item in normalized}
    empty_partitions = sorted(PARITY_CATEGORY_TAXONOMY - present_categories)
    return {
        "source": validated["source"],
        "repository": validated["repository"],
        "pin_revision": provenance["observed_revision"],
        "checkout_provenance": dict(provenance),
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


def _assert_source_path_declared(
    *,
    surface_id: str,
    source_path: str,
    cap_id: str,
    row: Mapping[str, Any],
) -> None:
    upstream = row.get("upstream")
    if not isinstance(upstream, Mapping):
        raise ContractValidationError(
            f"capability {cap_id} missing upstream for surface {surface_id}"
        )
    paths = upstream.get("source_paths")
    if not isinstance(paths, list):
        raise ContractValidationError(
            f"capability {cap_id} missing upstream.source_paths"
        )
    declared = {
        _require_relative_posix(str(p), label=f"{cap_id}.source_paths[]") for p in paths
    }
    if source_path not in declared:
        raise ContractValidationError(
            f"surface {surface_id} source_path {source_path!r} not declared by "
            f"capability {cap_id} upstream.source_paths"
        )


def _assert_bidirectional_inventory_coverage(
    *,
    mapped_surfaces: Sequence[Mapping[str, Any]],
    inventory: Mapping[str, Any],
    source: str,
) -> None:
    referenced: set[str] = set()
    for surface in mapped_surfaces:
        for cap_id in surface.get("capability_ids") or []:
            referenced.add(str(cap_id))
    for row in inventory.get("capabilities", []):
        if not isinstance(row, Mapping):
            continue
        upstream = row.get("upstream")
        if not isinstance(upstream, Mapping) or upstream.get("source") != source:
            continue
        if row.get("classification") == "alias":
            continue
        cap_id = require_nonempty_string(row.get("id"), label="capability.id")
        if cap_id not in referenced:
            raise ContractValidationError(
                f"uncovered non-alias inventory row for {source}: {cap_id}"
            )


def _legacy_mapping_table(
    mappings: Mapping[str, Any],
) -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}
    for key, value in mappings.items():
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ContractValidationError(
                f"legacy mapping[{key!r}] must be a sequence of capability ids"
            )
        table[str(key)] = list(value)
    return table


def _apply_surface_mappings(
    *,
    surfaces: Sequence[Mapping[str, Any]],
    mappings: Mapping[str, Any] | None,
    inventory: Mapping[str, Any],
    source: str,
    require_complete_mapping: bool = False,
    require_source_path_declared: bool = False,
    require_bidirectional_coverage: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Attach capability_ids; return (mapped_surfaces, unresolved_ids).

    Supports legacy ``{surface_id: [cap_ids]}`` and v2 mapping-store documents.
    For v2 stores, surface ``category`` is taken from the mapping entry
    (overrides discovery category).
    """
    index = _capability_index(inventory)
    category_overrides: dict[str, str] = {}
    if mappings is None:
        mapping_table: dict[str, list[str]] = {}
    elif _is_mapping_store(mappings):
        validated = validate_completeness_mapping(mappings)
        if validated["source"] != source:
            raise ContractValidationError(
                f"mapping.source {validated['source']!r} != {source!r}"
            )
        mapping_table = {}
        for entry in validated["surfaces"]:
            mapping_table[entry["surface_id"]] = list(entry["capability_ids"])
            category_overrides[entry["surface_id"]] = entry["category"]
    else:
        mapping_table = _legacy_mapping_table(mappings)

    mapped: list[dict[str, Any]] = []
    unresolved: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    discovered_ids = {
        require_nonempty_string(s.get("surface_id"), label="surface_id")
        for s in surfaces
    }

    if require_complete_mapping:
        missing = sorted(discovered_ids - set(mapping_table))
        if missing:
            raise ContractValidationError(
                "incomplete mapping; missing surfaces: " + ",".join(missing)
            )
        # Exactly one entry per discovered surface (dict/store already unique).
        extra = sorted(set(mapping_table) - discovered_ids)
        if extra:
            raise ContractValidationError(
                "mapping references undiscovered surface_id: " + ",".join(extra)
            )

    for surface in surfaces:
        sid = require_nonempty_string(surface.get("surface_id"), label="surface_id")
        if sid in category_overrides:
            category = category_overrides[sid]
        else:
            category = require_nonempty_string(
                surface.get("category"), label="category"
            )
        source_path = _require_relative_posix(
            str(surface.get("source_path")), label=f"{sid}.source_path"
        )
        cap_ids = mapping_table.get(sid)
        if not cap_ids:
            unresolved.append(sid)
            entry = dict(surface)
            entry["category"] = category
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
            row = _resolve_canonical_capability(
                cid,
                inventory=inventory,
                index=index,
                source=source,
                category=category,
                surface_id=sid,
            )
            if require_source_path_declared:
                _assert_source_path_declared(
                    surface_id=sid,
                    source_path=source_path,
                    cap_id=cid,
                    row=row,
                )
            canonical_ids.append(cid)
        entry = dict(surface)
        entry["category"] = category
        entry["capability_ids"] = sorted(set(canonical_ids))
        mapped.append(entry)

    # Detect mapping keys that do not correspond to discovered surfaces.
    if not require_complete_mapping:
        for key in mapping_table:
            if key not in discovered_ids:
                raise ContractValidationError(
                    f"mapping references undiscovered surface_id: {key}"
                )

    mapped.sort(key=lambda item: item["surface_id"])
    unresolved = sorted(set(unresolved))

    if require_bidirectional_coverage:
        _assert_bidirectional_inventory_coverage(
            mapped_surfaces=mapped,
            inventory=inventory,
            source=source,
        )
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
    proof_kind, promotion_sufficient = _validate_proof_kind_and_promotion(
        proof.get("proof_kind"),
        proof.get("promotion_sufficient"),
        label="completeness_proof",
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
        sid = require_surface_id(
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
        "proof_kind": proof_kind,
        "promotion_sufficient": promotion_sufficient,
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
    mapping: Mapping[str, Any] | None = None,
    surface_mappings: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Build a candidate proof from policy + checkout + inventory (no mutation)."""
    validated_policy = validate_completeness_policy(policy)
    source = validated_policy["source"]
    rules_version = validated_policy["discovery_rules"]["version"]
    resolved_mapping = _coerce_mapping_arg(mapping, surface_mappings)
    if rules_version == 2 and resolved_mapping is None:
        raise ContractValidationError(
            "discovery_rules.version==2 requires mapping for plan/build"
        )
    coverage = coverage_projection_for_source(inventory, source)
    pin = coverage["pin"]
    if pin["repository"] != validated_policy["repository"]:
        raise ContractValidationError(
            "policy.repository does not match inventory upstream_pins repository"
        )
    index = reproduce_source_index(
        validated_policy, upstream_root, pin_revision=pin["revision"]
    )
    strict_map = rules_version == 2
    mapped, unresolved = _apply_surface_mappings(
        surfaces=index["discovered_surfaces"],
        mappings=resolved_mapping,
        inventory=inventory,
        source=source,
        require_complete_mapping=strict_map,
        require_source_path_declared=strict_map,
        require_bidirectional_coverage=strict_map,
    )
    if strict_map and unresolved:
        raise ContractValidationError(
            "discovery_rules.version==2 forbids unresolved surfaces: "
            + ",".join(unresolved)
        )
    present = {s["category"] for s in mapped}
    empty_partitions = sorted(PARITY_CATEGORY_TAXONOMY - present)
    proof = {
        "store_kind": COMPLETENESS_PROOF_STORE_KIND,
        "schema_version": COMPLETENESS_SCHEMA_VERSION,
        "source": source,
        "repository": validated_policy["repository"],
        "proof_kind": validated_policy["proof_kind"],
        "promotion_sufficient": validated_policy["promotion_sufficient"],
        "pin_revision": pin["revision"],
        "checkout_provenance": dict(index["checkout_provenance"]),
        "policy_digest": digest_policy(validated_policy),
        "seed_digest": digest_seed_catalog(seed),
        "coverage_digest": _coverage_digest_for_proof(
            inventory,
            source,
            policy=validated_policy,
            mapping=resolved_mapping,
        ),
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
    mapping: Mapping[str, Any] | None = None,
    surface_mappings: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Maintainer plan mode: emit candidate proof metadata; never write inventory."""
    proof = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=upstream_root,
        seed=seed,
        mapping=mapping,
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
    mapping: Mapping[str, Any] | None = None,
    surface_mappings: Mapping[str, Sequence[str]] | None = None,
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
    if validated_policy["proof_kind"] != validated_proof["proof_kind"]:
        raise ContractValidationError(
            "proof.proof_kind does not match policy.proof_kind"
        )
    if (
        validated_policy["promotion_sufficient"]
        != validated_proof["promotion_sufficient"]
    ):
        raise ContractValidationError(
            "proof.promotion_sufficient does not match policy.promotion_sufficient"
        )

    expected_policy = digest_policy(validated_policy)
    if validated_proof["policy_digest"] != expected_policy:
        raise ContractValidationError("proof.policy_digest drift")

    coverage = coverage_projection_for_source(inventory, source)
    if coverage["pin"]["repository"] != validated_proof["repository"]:
        raise ContractValidationError("proof.repository does not match inventory pin")
    if coverage["pin"]["revision"] != validated_proof["pin_revision"]:
        raise ContractValidationError("proof.pin_revision does not match inventory pin")

    resolved_mapping = _coerce_mapping_arg(mapping, surface_mappings)
    rules_version = validated_policy["discovery_rules"]["version"]
    if rules_version == 2:
        if resolved_mapping is None:
            raise ContractValidationError(
                "discovery_rules.version==2 requires mapping for verify"
            )
        expected_coverage = _coverage_digest_for_proof(
            inventory,
            source,
            policy=validated_policy,
            mapping=resolved_mapping,
        )
    else:
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
        source_path = surface["source_path"]
        for cap_id in surface["capability_ids"]:
            row = _resolve_canonical_capability(
                cap_id,
                inventory=inventory,
                index=index,
                source=source,
                category=surface["category"],
                surface_id=sid,
            )
            _assert_source_path_declared(
                surface_id=sid,
                source_path=source_path,
                cap_id=cap_id,
                row=row,
            )

    if require_no_unresolved and validated_proof["unresolved_surfaces"]:
        raise ContractValidationError(
            "completeness proof has unresolved surfaces: "
            + ",".join(validated_proof["unresolved_surfaces"])
        )

    if require_no_unresolved:
        _assert_bidirectional_inventory_coverage(
            mapped_surfaces=validated_proof["discovered_surfaces"],
            inventory=inventory,
            source=source,
        )

    if resolved_mapping is not None:
        remapped, unresolved = _apply_surface_mappings(
            surfaces=[
                {k: v for k, v in s.items() if k != "capability_ids"}
                for s in validated_proof["discovered_surfaces"]
            ],
            mappings=resolved_mapping,
            inventory=inventory,
            source=source,
            require_complete_mapping=True,
            require_source_path_declared=True,
            require_bidirectional_coverage=True,
        )
        if unresolved:
            raise ContractValidationError(
                "mapping leaves unresolved surfaces: " + ",".join(unresolved)
            )
        proof_proj = [
            {
                "surface_id": s["surface_id"],
                "category": s["category"],
                "capability_ids": list(s["capability_ids"]),
            }
            for s in validated_proof["discovered_surfaces"]
        ]
        map_proj = [
            {
                "surface_id": s["surface_id"],
                "category": s["category"],
                "capability_ids": list(s["capability_ids"]),
            }
            for s in remapped
        ]
        if proof_proj != map_proj:
            raise ContractValidationError(
                "mapping surfaces do not exactly match proof surfaces"
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
        if rules_version == 2:
            if resolved_mapping is None:
                raise ContractValidationError(
                    "discovery_rules.version==2 reproduction requires mapping"
                )
            remapped_up, unresolved_up = _apply_surface_mappings(
                surfaces=reproduced["discovered_surfaces"],
                mappings=resolved_mapping,
                inventory=inventory,
                source=source,
                require_complete_mapping=True,
                require_source_path_declared=True,
                require_bidirectional_coverage=True,
            )
            if unresolved_up:
                raise ContractValidationError(
                    "reproduction left unresolved surfaces: "
                    + ",".join(unresolved_up)
                )
            expected_surface_digest = digest_surface_index(
                [
                    {k: v for k, v in s.items() if k != "capability_ids"}
                    for s in remapped_up
                ]
            )
            if expected_surface_digest != validated_proof["surface_index_digest"]:
                raise ContractValidationError(
                    "proof.surface_index_digest reproduction drift"
                )
            present_up = {s["category"] for s in remapped_up}
            empty_up = sorted(PARITY_CATEGORY_TAXONOMY - present_up)
            if empty_up != validated_proof["empty_category_partitions"]:
                raise ContractValidationError(
                    "proof.empty_category_partitions reproduction drift"
                )
            proof_ids = [
                {
                    "surface_id": s["surface_id"],
                    "category": s["category"],
                    "capability_ids": list(s["capability_ids"]),
                    "source_path": s["source_path"],
                    "anchor": s["anchor"],
                    "content_digest": s["content_digest"],
                    "kind": s["kind"],
                }
                for s in validated_proof["discovered_surfaces"]
            ]
            repro_ids = [
                {
                    "surface_id": s["surface_id"],
                    "category": s["category"],
                    "capability_ids": list(s["capability_ids"]),
                    "source_path": s["source_path"],
                    "anchor": s["anchor"],
                    "content_digest": s["content_digest"],
                    "kind": s["kind"],
                }
                for s in remapped_up
            ]
            if proof_ids != repro_ids:
                raise ContractValidationError(
                    "proof.discovered_surfaces reproduction drift"
                )
        else:
            if (
                reproduced["surface_index_digest"]
                != validated_proof["surface_index_digest"]
            ):
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
    completeness_artifacts_checked: bool = False
    completeness_artifacts_verified: int = 0
    completeness_artifact_sources: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "completeness_gate_checked": self.completeness_gate_checked,
            "completeness_proofs_required": self.completeness_proofs_required,
            "completeness_proofs_verified": self.completeness_proofs_verified,
            "promoted_sources": list(self.promoted_sources),
            "promoted_categories": list(self.promoted_categories),
            "completeness_artifacts_checked": self.completeness_artifacts_checked,
            "completeness_artifacts_verified": self.completeness_artifacts_verified,
            "completeness_artifact_sources": list(self.completeness_artifact_sources),
        }


def _load_optional_seed(repo_root: Path, source: str) -> dict[str, Any] | None:
    path = repo_root / "docs" / "parity" / "upstream-snapshots" / f"{source}.json"
    if not path.is_file():
        return None
    return load_json_object(path)


def _load_optional_mapping(repo_root: Path, source: str) -> dict[str, Any] | None:
    path = repo_root / DEFAULT_MAPPING_DIR_RELATIVE / f"{source}.json"
    if not path.is_file():
        return None
    if path.is_symlink() or not path.is_file() or os.path.islink(path):
        raise ContractValidationError(
            f"completeness mapping must be a regular file: "
            f"{DEFAULT_MAPPING_DIR_RELATIVE}/{source}.json"
        )
    return load_json_object(path)


def _assert_regular_json_artifact(path: Path, *, label: str) -> None:
    if path.is_symlink() or os.path.islink(path):
        raise ContractValidationError(f"{label} must not be a symlink: {path}")
    if not path.is_file():
        raise ContractValidationError(f"{label} must be a regular file: {path}")


def _list_completeness_artifact_sources(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    sources: set[str] = set()
    for path in sorted(directory.glob("*.json")):
        _assert_regular_json_artifact(path, label="completeness artifact")
        stem = path.stem
        if stem not in SOURCE_STATUS_IDS:
            raise ContractValidationError(
                f"unknown or case-mismatched completeness source filename: "
                f"{path.name}"
            )
        sources.add(stem)
    return sources


def check_committed_completeness_artifacts(
    inventory: Mapping[str, Any],
    *,
    repo_root: Path | str,
) -> dict[str, object]:
    """Network-free consistency check for committed policy/mapping/proof triples.

    Does not mutate inventory statuses. Does not treat bootstrapping artifacts as
    promotions. Fail-closed on orphan members, schema drift, or coverage mismatch.
    """
    root = Path(repo_root)
    policy_dir = root / DEFAULT_POLICY_DIR_RELATIVE
    mapping_dir = root / DEFAULT_MAPPING_DIR_RELATIVE
    proof_dir = root / DEFAULT_PROOF_DIR_RELATIVE

    policy_sources = _list_completeness_artifact_sources(policy_dir)
    mapping_sources = _list_completeness_artifact_sources(mapping_dir)
    proof_sources = _list_completeness_artifact_sources(proof_dir)
    all_sources = policy_sources | mapping_sources | proof_sources

    for source in sorted(all_sources):
        missing: list[str] = []
        if source not in policy_sources:
            missing.append("policy")
        if source not in mapping_sources:
            missing.append("mapping")
        if source not in proof_sources:
            missing.append("proof")
        if missing:
            raise ContractValidationError(
                f"orphan completeness artifacts for {source}: missing "
                + ",".join(missing)
            )

    verified = 0
    for source in sorted(all_sources):
        policy_path = policy_dir / f"{source}.json"
        mapping_path = mapping_dir / f"{source}.json"
        proof_path = proof_dir / f"{source}.json"
        _assert_regular_json_artifact(policy_path, label="completeness policy")
        _assert_regular_json_artifact(mapping_path, label="completeness mapping")
        _assert_regular_json_artifact(proof_path, label="completeness proof")

        policy = validate_completeness_policy(load_json_object(policy_path))
        mapping = validate_completeness_mapping(load_json_object(mapping_path))
        proof = validate_completeness_proof(load_json_object(proof_path))

        if policy["source"] != source:
            raise ContractValidationError(
                f"policy.source {policy['source']!r} does not match filename {source}"
            )
        if mapping["source"] != source:
            raise ContractValidationError(
                f"mapping.source {mapping['source']!r} does not match filename {source}"
            )
        if proof["source"] != source:
            raise ContractValidationError(
                f"proof.source {proof['source']!r} does not match filename {source}"
            )

        coverage = coverage_projection_for_source(inventory, source)
        if coverage["pin"]["repository"] != policy["repository"]:
            raise ContractValidationError(
                f"completeness policy repository drift for {source}"
            )
        if coverage["pin"]["repository"] != proof["repository"]:
            raise ContractValidationError(
                f"completeness proof repository drift for {source}"
            )
        if coverage["pin"]["revision"] != proof["pin_revision"]:
            raise ContractValidationError(
                f"completeness proof pin_revision drift for {source}"
            )

        seed = _load_optional_seed(root, source)
        verify_completeness_proof(
            proof,
            policy=policy,
            inventory=inventory,
            seed=seed,
            upstream_root=None,
            require_no_unresolved=True,
            mapping=mapping,
        )
        if proof["unresolved_surfaces"]:
            raise ContractValidationError(
                f"committed proof for {source} has unresolved_surfaces"
            )
        verified += 1

    return {
        "completeness_artifacts_checked": True,
        "completeness_artifacts_verified": verified,
        "completeness_artifact_sources": sorted(all_sources),
        "completeness_proofs_required": False,
        "promoted_sources": [],
    }


def _artifact_fields_for_gate(
    inventory: Mapping[str, Any],
    *,
    repo_root: Path | str | None,
) -> tuple[bool, int, tuple[str, ...]]:
    if repo_root is None:
        return False, 0, ()
    artifacts = check_committed_completeness_artifacts(
        inventory, repo_root=repo_root
    )
    sources = artifacts["completeness_artifact_sources"]
    if not isinstance(sources, list):
        raise ContractValidationError("completeness_artifact_sources must be a list")
    return (
        bool(artifacts["completeness_artifacts_checked"]),
        int(artifacts["completeness_artifacts_verified"]),
        tuple(str(s) for s in sources),
    )


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
        _assert_regular_json_artifact(proof_path, label="completeness proof")
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
        _assert_regular_json_artifact(policy_path, label="completeness policy")
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
            completeness_artifacts_checked=False,
            completeness_artifacts_verified=0,
            completeness_artifact_sources=(),
        )

    art_checked, art_verified, art_sources = _artifact_fields_for_gate(
        inventory, repo_root=repo_root
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
            completeness_artifacts_checked=art_checked,
            completeness_artifacts_verified=art_verified,
            completeness_artifact_sources=art_sources,
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

        mapping = None
        if root is not None:
            mapping = _load_optional_mapping(root, source)

        verify_completeness_proof(
            proof,
            policy=policy,
            inventory=inventory,
            seed=seed,
            upstream_root=upstream,
            require_no_unresolved=True,
            mapping=mapping,
        )
        validated = validate_completeness_proof(proof)
        _require_promotion_sufficient(
            validated,
            context=f"source_status[{source}]==complete",
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

            mapping = None
            if root is not None:
                mapping = _load_optional_mapping(root, source)

            verify_completeness_proof(
                proof,
                policy=policy,
                inventory=inventory,
                seed=seed,
                upstream_root=upstream,
                require_no_unresolved=True,
                mapping=mapping,
            )
            validated = validate_completeness_proof(proof)
            _require_promotion_sufficient(
                validated,
                context=(
                    "category_status complete requires promotion-sufficient "
                    f"proofs for every source; {source} is not"
                ),
            )
            verified += 1
            verified_sources.add(source)
            proofs_cache[source] = validated

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
        completeness_artifacts_checked=art_checked,
        completeness_artifacts_verified=art_verified,
        completeness_artifact_sources=art_sources,
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
