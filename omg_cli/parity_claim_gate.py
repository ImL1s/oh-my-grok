"""Release claim gate: overclaim scanner, live evidence freshness, upstream drift (#78-C)."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omg_cli.contracts.parity_schema import (
    SOURCE_STATUS_IDS,
    inventory_completion_claims_allowed,
    load_json_object,
    maturity_rank,
    max_runtime_maturity,
    validate_parity_inventory,
)
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.parity_refresh import (
    build_refresh_plan,
    canonical_changes_digest,
    committed_review_path,
    validate_upstream_catalog,
)

# Required upstream catalogue seeds for --release (GROK_BUILD is a pin, not a snapshot).
REQUIRED_UPSTREAM_SNAPSHOT_SOURCES = tuple(SOURCE_STATUS_IDS)

_DOC_SCAN_RELATIVE = (
    "README.md",
    "CHANGELOG.md",
    "docs/skills.md",
    "docs/parity/README.md",
    "docs/parity/schema-v2.md",
    "docs/parity/SUMMARY.md",
    "docs/parity/FEATURE-MATRIX.md",
    "docs/parity/MATRIX-OMC.md",
    "docs/parity/MATRIX-OMX.md",
    "docs/parity/MATRIX-OmO.md",
    "docs/parity/MATRIX-Antigravity.md",
    "docs/parity/GAPS.md",
    "docs/parity/SUMMARY.zh.md",
    "docs/parity/SUMMARY.zh-TW.md",
)
# Completeness-gated phrases: may turn off once inventory is complete + healthy.
_COMPLETENESS_FORBIDDEN_PHRASE_PATTERNS = (
    re.compile(r"(?i)full 1:1"),
    re.compile(r"(?i)complete parity"),
    re.compile(r"✅"),
    re.compile(r"(?i)parity \d+%"),
)
# Live-evidence phrases: stay on unless every capability is actually live_verified.
_LIVE_FORBIDDEN_PHRASE_PATTERNS = (
    re.compile(r"(?i)live[ _-]?verified"),
    re.compile(r"(?i)live[ _-]?proven"),
    re.compile(r"(?i)live[ _-]?tested"),
)
# Back-compat alias for tests / callers that still import the combined set.
_FORBIDDEN_PHRASE_PATTERNS = (
    _LIVE_FORBIDDEN_PHRASE_PATTERNS + _COMPLETENESS_FORBIDDEN_PHRASE_PATTERNS
)
_CAPABILITY_CLAIM_RE = re.compile(
    r"(?i)\b(healthy|live[-_ ]?(?:verified|proven|tested)|implemented)\b"
)
_NEGATED_CLAIM_PREFIX_RE = re.compile(
    r"(?i)\b(not(?:\s+\w+){0,3}|never)\s+$"
)
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`]+`")
_DRIFT_CHANGE_KINDS = frozenset({"added", "deleted", "renamed", "changed"})


def _now_or_utc(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _strip_markdown_code(text: str) -> str:
    """Remove fenced/inline code so schema vocabulary in backticks is not an overclaim."""
    stripped = _FENCED_CODE_RE.sub("", text)
    return _INLINE_CODE_RE.sub("", stripped)


def _doc_restrictions_active(inventory: dict[str, Any]) -> bool:
    # Completeness-gated phrases (full 1:1 / complete parity / ✅ / parity N%):
    # inventory_status alone is not enough — category_status and source_status must
    # also be complete, and every capability must reach at least healthy.
    if not inventory_completion_claims_allowed(inventory):
        return True
    for row in inventory.get("capabilities", []):
        if not isinstance(row, dict):
            continue
        try:
            peak = max_runtime_maturity(row)
        except ContractValidationError:
            return True
        if maturity_rank(peak) < maturity_rank("healthy"):
            return True
    return False


def _live_claim_restrictions_active(inventory: dict[str, Any]) -> bool:
    """Keep live-* phrase scan on unless every capability is live_verified."""
    rows = [
        row
        for row in inventory.get("capabilities", [])
        if isinstance(row, dict)
    ]
    if not rows:
        return True
    for row in rows:
        try:
            peak = max_runtime_maturity(row)
        except ContractValidationError:
            return True
        if maturity_rank(peak) < maturity_rank("live_verified"):
            return True
    return False


def _capability_index(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in inventory.get("capabilities", []):
        if not isinstance(row, dict):
            continue
        cap_id = row.get("id")
        if isinstance(cap_id, str) and cap_id:
            rows[cap_id] = row
    return rows


def _claimed_maturity_rank(token: str) -> int:
    normalized = token.lower().replace("-", "_").replace(" ", "_")
    if normalized == "implemented":
        return maturity_rank("healthy")
    if normalized.startswith("live"):
        return maturity_rank("live_verified")
    return maturity_rank("healthy")


def _scan_doc_text(
    *,
    relative: str,
    text: str,
    restrictions_active: bool,
    live_restrictions_active: bool,
    capability_rows: dict[str, dict[str, Any]],
) -> list[str]:
    violations: list[str] = []
    prose = _strip_markdown_code(text)
    patterns: list[re.Pattern[str]] = []
    if live_restrictions_active:
        patterns.extend(_LIVE_FORBIDDEN_PHRASE_PATTERNS)
    if restrictions_active:
        patterns.extend(_COMPLETENESS_FORBIDDEN_PHRASE_PATTERNS)
    for pattern in patterns:
        if pattern.search(prose):
            violations.append(
                f"forbidden phrase {pattern.pattern!r} in {relative}"
            )
    for cap_id, row in capability_rows.items():
        if cap_id not in text:
            continue
        try:
            peak = max_runtime_maturity(row)
            peak_rank = maturity_rank(peak)
        except ContractValidationError:
            continue
        for line in text.splitlines():
            if cap_id not in line:
                continue
            for match in _CAPABILITY_CLAIM_RE.finditer(line):
                if _NEGATED_CLAIM_PREFIX_RE.search(line[: match.start(1)]):
                    continue
                claimed_rank = _claimed_maturity_rank(match.group(1))
                if claimed_rank > peak_rank:
                    violations.append(
                        f"capability {cap_id!r} overclaimed in {relative} "
                        f"(claimed {match.group(1)!r}, peak {peak!r})"
                    )
    return violations


def scan_docs_for_overclaims(*, repo_root: Path, inventory: dict) -> list[str]:
    """Return human-readable overclaim violations (empty when docs are honest)."""
    root = Path(repo_root)
    restrictions_active = _doc_restrictions_active(inventory)
    live_restrictions_active = _live_claim_restrictions_active(inventory)
    capability_rows = _capability_index(inventory)
    violations: list[str] = []
    for relative in _DOC_SCAN_RELATIVE:
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        violations.extend(
            _scan_doc_text(
                relative=relative,
                text=text,
                restrictions_active=restrictions_active,
                live_restrictions_active=live_restrictions_active,
                capability_rows=capability_rows,
            )
        )
    return violations


def assert_live_evidence_fresh(
    inventory: dict,
    *,
    repo_root: Path | str | None = None,
    now: datetime | None = None,
) -> None:
    """Fail closed when live_verified rows carry stale or unverifiable evidence."""
    validate_parity_inventory(
        inventory,
        repo_root=repo_root,
        now=_now_or_utc(now),
    )


def _change_identity(change: Mapping[str, Any]) -> tuple[Any, ...]:
    kind = change.get("change_kind")
    if kind == "renamed":
        return (kind, change.get("from_id"), change.get("to_id"))
    return (kind, change.get("capability_id"))


def _review_change_entries(review_artifact: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not review_artifact:
        return []
    entries: list[dict[str, Any]] = []
    for key in ("changes", "acknowledgments"):
        raw = review_artifact.get(key)
        if isinstance(raw, list) and raw:
            entries.extend(entry for entry in raw if isinstance(entry, dict))
    return entries


def _review_binds_drift_context(
    review_artifact: dict[str, Any],
    *,
    source: str,
    from_revision: str,
    to_revision: str,
) -> bool:
    if review_artifact.get("store_kind") != "parity_refresh_review":
        return False
    if review_artifact.get("schema_version") != 1:
        return False
    if review_artifact.get("source") != source:
        return False
    if review_artifact.get("from_revision") != from_revision:
        return False
    if review_artifact.get("to_revision") != to_revision:
        return False
    return True


def _is_change_acknowledged(
    change: dict[str, Any],
    review_artifact: dict[str, Any] | None,
    *,
    source: str,
    from_revision: str,
    to_revision: str,
) -> bool:
    if not review_artifact:
        return False
    if not _review_binds_drift_context(
        review_artifact,
        source=source,
        from_revision=from_revision,
        to_revision=to_revision,
    ):
        return False
    identity = _change_identity(change)
    for entry in _review_change_entries(review_artifact):
        if entry.get("disposition") != "acknowledged":
            continue
        if _change_identity(entry) != identity:
            continue
        if entry.get("detail") != change.get("detail"):
            continue
        return True
    return False


def assert_upstream_drift_resolved(
    *,
    inventory: dict,
    upstream_catalog: dict,
    review_artifact: dict | None,
) -> None:
    """Fail when refresh plan diffs are not explicitly acknowledged."""
    source = upstream_catalog.get("source")
    new_pin = upstream_catalog.get("pin_revision")
    if not isinstance(source, str) or not isinstance(new_pin, str):
        raise ContractValidationError("upstream catalog missing source or pin_revision")

    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=upstream_catalog,
        source=source,
        new_pin=new_pin,
    )
    from_revision = plan["from_revision"]
    to_revision = plan["to_revision"]
    unresolved: list[str] = []
    for change in plan.get("changes", []):
        if not isinstance(change, dict):
            continue
        kind = change.get("change_kind")
        if kind not in _DRIFT_CHANGE_KINDS:
            continue
        if _is_change_acknowledged(
            change,
            review_artifact,
            source=source,
            from_revision=from_revision,
            to_revision=to_revision,
        ):
            continue
        unresolved.append(str(change))
    if unresolved:
        raise ContractValidationError(
            "upstream drift unresolved: "
            + "; ".join(unresolved)
        )


def _previous_release_tag(repo_root: Path) -> str | None:
    """Return the newest v* tag reachable from HEAD^ (durable release base)."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "describe",
                "--tags",
                "--abbrev=0",
                "--match",
                "v*",
                "HEAD^",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    tag = proc.stdout.strip()
    if proc.returncode != 0 or not tag:
        return None
    return tag


_INVENTORY_GIT_PATH = "docs/parity/omg-parity.json"
_REGULAR_BLOB_MODES = frozenset({"100644", "100755"})


def _run_git(
    repo_root: Path, *args: str, label: str
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ContractValidationError(f"git {label} failed: {exc}") from exc


def _assert_base_is_head_ancestor(repo_root: Path, base_ref: str) -> None:
    """Fail closed unless base_ref is an ancestor of HEAD."""
    proc = _run_git(
        repo_root,
        "merge-base",
        "--is-ancestor",
        base_ref,
        "HEAD",
        label="merge-base --is-ancestor",
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ContractValidationError(
            f"pin-transition base_ref must be a HEAD ancestor: {base_ref!r}"
            + (f" ({detail})" if detail else "")
        )


def _git_show_text_strict(repo_root: Path, object_spec: str) -> str:
    proc = _run_git(repo_root, "show", object_spec, label=f"show {object_spec}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ContractValidationError(
            f"git show failed for {object_spec}"
            + (f": {detail}" if detail else "")
        )
    return proc.stdout


def _git_show_inventory_strict(repo_root: Path, git_ref: str) -> dict[str, Any]:
    text = _git_show_text_strict(repo_root, f"{git_ref}:{_INVENTORY_GIT_PATH}")
    if not text.strip():
        raise ContractValidationError(
            f"empty inventory blob at {git_ref}:{_INVENTORY_GIT_PATH}"
        )
    try:
        return json_loads_object(text)
    except (ContractValidationError, ValueError) as exc:
        raise ContractValidationError(
            f"invalid inventory JSON at {git_ref}:{_INVENTORY_GIT_PATH}: {exc}"
        ) from exc


def _git_show_inventory_if_present(
    repo_root: Path, git_ref: str
) -> dict[str, Any] | None:
    """Load inventory at ref when the blob exists.

    Missing ref / missing path → ``None`` (candidate skip). Empty blob or
    invalid JSON → ``ContractValidationError`` (never soft-skip to a newer base).
    """
    object_spec = f"{git_ref}:{_INVENTORY_GIT_PATH}"
    proc = _run_git(repo_root, "show", object_spec, label=f"show {object_spec}")
    if proc.returncode != 0:
        return None
    text = proc.stdout
    if not text.strip():
        raise ContractValidationError(f"empty inventory blob at {object_spec}")
    try:
        return json_loads_object(text)
    except (ContractValidationError, ValueError) as exc:
        raise ContractValidationError(
            f"invalid inventory JSON at {object_spec}: {exc}"
        ) from exc


_CATALOG_GIT_PATH_TMPL = "docs/parity/upstream-snapshots/{source}.json"


def _git_show_catalog_strict(
    repo_root: Path, git_ref: str, source: str
) -> dict[str, Any]:
    """Load + validate historical upstream catalog; git/JSON errors hard-fail."""
    object_spec = f"{git_ref}:{_CATALOG_GIT_PATH_TMPL.format(source=source)}"
    text = _git_show_text_strict(repo_root, object_spec)
    if not text.strip():
        raise ContractValidationError(f"empty catalog blob at {object_spec}")
    try:
        return validate_upstream_catalog(json_loads_object(text))
    except (ContractValidationError, ValueError) as exc:
        raise ContractValidationError(
            f"invalid catalog JSON at {object_spec}: {exc}"
        ) from exc


def _iter_commit_parent_child_edges(
    repo_root: Path, base_ref: str
) -> list[tuple[str, str]]:
    """Enumerate every parent→child edge in the base_ref..HEAD commit DAG."""
    proc = _run_git(
        repo_root,
        "rev-list",
        "--parents",
        f"{base_ref}..HEAD",
        label="rev-list --parents",
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ContractValidationError(
            f"git rev-list failed for {base_ref}..HEAD"
            + (f": {detail}" if detail else "")
        )
    edges: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            # Root commit with no parents inside the range is unexpected when
            # base is a verified ancestor; still hard-fail rather than skip.
            if len(parts) == 1:
                raise ContractValidationError(
                    f"commit {parts[0]} in {base_ref}..HEAD has no parents"
                )
            continue
        child, *parents = parts
        for parent in parents:
            edges.append((parent, child))
    return edges


def _lexical_repo_relative(repo_root: Path, path: Path) -> str:
    """Return path relative to repo_root without following symlinks."""
    root = Path(repo_root)
    root_abs = root if root.is_absolute() else root.absolute()
    path_abs = path if path.is_absolute() else path.absolute()
    try:
        relative = path_abs.relative_to(root_abs)
    except ValueError as exc:
        raise ContractValidationError(
            f"committed refresh review path escapes repo root: {path}"
        ) from exc
    if any(part == ".." for part in relative.parts):
        raise ContractValidationError(
            f"committed refresh review path escapes repo root: {path}"
        )
    return relative.as_posix()


def _assert_no_symlink_path_components(repo_root: Path, relative: str) -> None:
    """Reject symlinks anywhere along the lexical relative path."""
    current = Path(repo_root)
    for part in relative.split("/"):
        if not part or part == ".":
            continue
        current = current / part
        try:
            st = current.lstat()
        except OSError as exc:
            raise ContractValidationError(
                f"committed refresh review path missing: {relative}"
            ) from exc
        if stat.S_ISLNK(st.st_mode):
            raise ContractValidationError(
                f"committed refresh review path contains symlink: {relative}"
            )


def _assert_review_blob_committed(repo_root: Path, path: Path) -> None:
    """Fail closed unless review is a regular tracked file matching HEAD bytes."""
    root = Path(repo_root)
    relative = _lexical_repo_relative(root, path)
    _assert_no_symlink_path_components(root, relative)
    target = root / relative
    try:
        st = target.lstat()
    except OSError as exc:
        raise ContractValidationError(
            f"committed refresh review path missing: {relative}"
        ) from exc
    if not stat.S_ISREG(st.st_mode):
        raise ContractValidationError(
            f"committed refresh review must be a regular file: {relative}"
        )
    if not (root / ".git").exists():
        raise ContractValidationError(
            f"committed refresh review requires a git repository: {relative}"
        )
    tracked = _run_git(
        root, "ls-files", "--error-unmatch", "--", relative, label="ls-files"
    )
    if tracked.returncode != 0:
        raise ContractValidationError(
            f"committed refresh review is not tracked by git: {relative}"
        )
    ls_tree = _run_git(
        root, "ls-tree", "HEAD", "--", relative, label="ls-tree"
    )
    if ls_tree.returncode != 0 or not ls_tree.stdout.strip():
        raise ContractValidationError(
            f"committed refresh review missing from HEAD tree: {relative}"
        )
    # ls-tree lines: <mode> <type> <object>\t<file>
    meta = ls_tree.stdout.strip().split("\t", 1)[0].split()
    if len(meta) < 3:
        raise ContractValidationError(
            f"committed refresh review HEAD ls-tree unreadable: {relative}"
        )
    mode, obj_type, _blob = meta[0], meta[1], meta[2]
    if obj_type != "blob" or mode not in _REGULAR_BLOB_MODES:
        raise ContractValidationError(
            f"committed refresh review HEAD entry must be a regular blob "
            f"(mode={mode}, type={obj_type}): {relative}"
        )
    diff = _run_git(
        root, "diff", "--quiet", "HEAD", "--", relative, label="diff --quiet"
    )
    if diff.returncode != 0:
        raise ContractValidationError(
            f"committed refresh review worktree differs from HEAD blob: {relative}"
        )


def json_loads_object(text: str) -> dict[str, Any]:
    import json

    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ContractValidationError("expected JSON object")
    return raw


@dataclass(frozen=True)
class BaseInventoryResolution:
    inventory: dict[str, Any]
    git_ref: str | None = None


def resolve_base_inventory(
    repo_root: Path,
    *,
    base_inventory: dict[str, Any] | None = None,
    base_inventory_path: Path | str | None = None,
    base_ref: str | None = None,
    require: bool = False,
) -> BaseInventoryResolution | None:
    """Resolve previous inventory for pin-transition review enforcement.

    When ``require`` is true (release mode), prefer a durable base:
    explicit ``base_ref`` / ``OMG_PARITY_BASE_REF``, then previous ``v*`` release
    tag, then ``origin/main`` / ``main``. ``HEAD^`` is intentionally omitted so
    an unreviewed pin bump cannot be masked by a later unrelated commit.

    File-backed ``base_inventory`` / ``--base-inventory`` never silently overrides
    an explicit ``base_ref``. In release mode (``require=True``), file-only base
    is refused: without git provenance the pin scanner cannot walk
    ``base_ref..HEAD`` and an A→B→A history would false-pass as A→A.
    Pair ``--base-inventory`` with ``--base-ref`` whose inventory blob matches
    the file to keep the DAG walk.
    """
    if base_inventory is not None and base_inventory_path is not None:
        raise ContractValidationError(
            "conflicting base inventory authorities: pass only one of "
            "base_inventory or --base-inventory / base_inventory_path"
        )

    file_inventory: dict[str, Any] | None = None
    if base_inventory is not None:
        file_inventory = base_inventory
    elif base_inventory_path is not None:
        file_inventory = load_json_object(Path(base_inventory_path))

    explicit = (base_ref or os.environ.get("OMG_PARITY_BASE_REF") or "").strip()

    if file_inventory is not None:
        if explicit:
            # Bind file to git provenance — never drop base_ref for endpoint-only.
            loaded = _git_show_inventory_if_present(repo_root, explicit)
            if loaded is None:
                raise ContractValidationError(
                    f"base inventory missing at explicit base_ref {explicit!r} "
                    f"({explicit}:{_INVENTORY_GIT_PATH})"
                )
            if loaded != file_inventory:
                raise ContractValidationError(
                    "base-inventory does not match git blob at "
                    f"base_ref {explicit!r} ({explicit}:{_INVENTORY_GIT_PATH})"
                )
            return BaseInventoryResolution(inventory=loaded, git_ref=explicit)
        if require:
            raise ContractValidationError(
                "release pin-transition gate requires git base provenance: "
                "pass --base-ref / OMG_PARITY_BASE_REF (optionally with "
                "--base-inventory bound to that ref's inventory blob); "
                "--base-inventory alone is insufficient for --release"
            )
        return BaseInventoryResolution(inventory=file_inventory, git_ref=None)

    refs: list[str] = []
    if explicit:
        refs.append(explicit)
    elif require:
        previous = _previous_release_tag(repo_root)
        if previous:
            refs.append(previous)
        refs.extend(["origin/main", "main"])
    else:
        refs.extend(["HEAD^", "origin/main", "main"])
    for ref in refs:
        # Missing ref/path → try next candidate. Present-but-invalid JSON/empty
        # blob raises (must not advance base to a newer main and hide transitions).
        loaded = _git_show_inventory_if_present(repo_root, ref)
        if loaded is not None:
            return BaseInventoryResolution(inventory=loaded, git_ref=ref)
        if explicit:
            raise ContractValidationError(
                f"base inventory missing at explicit base_ref {ref!r} "
                f"({ref}:{_INVENTORY_GIT_PATH})"
            )
    if require:
        raise ContractValidationError(
            "pin-transition gate requires base inventory "
            "(pass --base-ref / OMG_PARITY_BASE_REF, previous v* release tag, "
            "or origin/main|main:docs/parity/omg-parity.json; "
            "--base-inventory alone is insufficient for --release — pair it "
            "with --base-ref whose inventory blob matches the file)"
        )
    return None


def _pin_revision(inventory: Mapping[str, Any], source: str) -> str:
    pins = inventory.get("upstream_pins")
    if not isinstance(pins, dict) or source not in pins:
        raise ContractValidationError(
            f"inventory missing upstream_pins entry for {source!r}"
        )
    entry = pins[source]
    if not isinstance(entry, dict):
        raise ContractValidationError(f"upstream_pins[{source!r}] must be an object")
    revision = entry.get("revision")
    if not isinstance(revision, str):
        raise ContractValidationError(
            f"upstream_pins[{source!r}].revision must be a string"
        )
    return revision


def _iter_pin_transition_pairs(
    *,
    repo_root: Path,
    base_inventory: dict[str, Any],
    candidate_inventory: dict[str, Any],
    base_ref: str | None,
) -> list[tuple[dict[str, Any], dict[str, Any], str | None]]:
    """Yield (from_inventory, to_inventory, to_ref) pin-scan pairs.

    When ``base_ref`` is set, enumerate every parent→child edge in the
    ``base_ref..HEAD`` commit DAG (not path-simplified ``git log``) so a
    side-branch bump→revert→merge cannot hide intermediate pin transitions.
    Git / JSON / blob read errors hard-fail. Always ends with HEAD→candidate
    so a dirty worktree pin bump is still gated.
    """
    if not base_ref:
        return [(base_inventory, candidate_inventory, None)]

    _assert_base_is_head_ancestor(repo_root, base_ref)
    pairs: list[tuple[dict[str, Any], dict[str, Any], str | None]] = []
    edges = _iter_commit_parent_child_edges(repo_root, base_ref)
    for parent, child in edges:
        pairs.append(
            (
                _git_show_inventory_strict(repo_root, parent),
                _git_show_inventory_strict(repo_root, child),
                child,
            )
        )
    head_inventory = _git_show_inventory_strict(repo_root, "HEAD")
    pairs.append((head_inventory, candidate_inventory, None))
    if not edges:
        # Empty range (base is HEAD): still compare base blob to candidate.
        pairs.insert(
            0,
            (
                _git_show_inventory_strict(repo_root, base_ref),
                head_inventory,
                "HEAD",
            ),
        )
    return pairs


def _catalog_for_transition(
    *,
    repo_root: Path,
    source: str,
    at_ref: str | None,
    fallback: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    """Resolve catalog for a pin transition.

    When ``at_ref`` is set (historical DAG edge), load that commit's snapshot
    catalog strictly — never soft-fallback to the worktree catalog, which could
    hide a missing/invalid snapshot at the transition child.
    """
    if at_ref:
        return _git_show_catalog_strict(repo_root, at_ref, source)
    catalog = fallback.get(source)
    if catalog is None:
        raise ContractValidationError(
            f"missing upstream catalog for pin transition source {source!r}"
        )
    return catalog


def _require_single_pin_transition_review(
    *,
    root: Path,
    from_inventory: dict[str, Any],
    to_inventory: dict[str, Any],
    to_ref: str | None,
    source: str,
    catalogs_by_source: Mapping[str, dict[str, Any]],
    missing: list[str],
) -> None:
    from_revision = _pin_revision(from_inventory, source)
    to_revision = _pin_revision(to_inventory, source)
    if from_revision == to_revision:
        return
    catalog = _catalog_for_transition(
        repo_root=root,
        source=source,
        at_ref=to_ref,
        fallback=catalogs_by_source,
    )
    plan = build_refresh_plan(
        inventory=from_inventory,
        upstream_catalog=catalog,
        source=source,
        new_pin=to_revision,
    )
    digest = canonical_changes_digest(
        [c for c in plan.get("changes", []) if isinstance(c, dict)]
    )
    path = committed_review_path(
        root,
        source=source,
        from_revision=from_revision,
        to_revision=to_revision,
        change_digest=digest,
    )
    try:
        path.lstat()
    except OSError:
        try:
            missing.append(_lexical_repo_relative(root, path))
        except ContractValidationError:
            missing.append(str(path))
        return
    _assert_review_blob_committed(root, path)
    review = load_json_object(path)
    if not _review_binds_drift_context(
        review,
        source=source,
        from_revision=from_revision,
        to_revision=to_revision,
    ):
        raise ContractValidationError(
            f"committed refresh review context mismatch: {path.name}"
        )
    review_digest = review.get("change_digest")
    if review_digest != digest:
        raise ContractValidationError(
            f"committed refresh review change_digest mismatch for {source}: "
            f"expected {digest}, got {review_digest!r}"
        )
    for change in plan.get("changes", []):
        if not isinstance(change, dict):
            continue
        if change.get("change_kind") not in _DRIFT_CHANGE_KINDS:
            continue
        if not _is_change_acknowledged(
            change,
            review,
            source=source,
            from_revision=from_revision,
            to_revision=to_revision,
        ):
            raise ContractValidationError(
                f"committed refresh review missing acknowledgment for {change}"
            )


def assert_pin_transitions_reviewed(
    *,
    inventory: dict[str, Any],
    base_inventory: dict[str, Any],
    repo_root: Path,
    catalogs_by_source: Mapping[str, dict[str, Any]],
    base_ref: str | None = None,
) -> None:
    """Require committed docs/parity/reviews ledger when a source pin changes.

    When ``base_ref`` is a durable git ref, every parent→child inventory pin
    transition in the ``base_ref..HEAD`` commit DAG is enforced — not only the
    tip delta — so an unreviewed bump→revert on a merged side branch cannot
    hide intermediate pin transitions.
    """
    root = Path(repo_root)
    missing: list[str] = []
    for from_inventory, to_inventory, to_ref in _iter_pin_transition_pairs(
        repo_root=root,
        base_inventory=base_inventory,
        candidate_inventory=inventory,
        base_ref=base_ref,
    ):
        for source in REQUIRED_UPSTREAM_SNAPSHOT_SOURCES:
            _require_single_pin_transition_review(
                root=root,
                from_inventory=from_inventory,
                to_inventory=to_inventory,
                to_ref=to_ref,
                source=source,
                catalogs_by_source=catalogs_by_source,
                missing=missing,
            )
    if missing:
        raise ContractValidationError(
            "pin transition missing committed refresh review(s): " + ", ".join(missing)
        )


def _load_optional_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    return load_json_object(path)


def _iter_upstream_catalog_paths(
    repo_root: Path, upstream_catalog_path: Path | None
) -> list[tuple[str | None, Path]]:
    """Return (expected_source, path) pairs.

    When scanning the default snapshot directory, expected_source is bound to the
    filename (OMC.json → OMC). An explicit --catalog path has no filename bind
    (expected_source is None).
    """
    if upstream_catalog_path is not None:
        return [(None, Path(upstream_catalog_path))]
    snapshots = repo_root / "docs" / "parity" / "upstream-snapshots"
    if not snapshots.is_dir():
        raise ContractValidationError(
            "missing docs/parity/upstream-snapshots directory required for --release"
        )
    missing: list[str] = []
    entries: list[tuple[str | None, Path]] = []
    for source in REQUIRED_UPSTREAM_SNAPSHOT_SOURCES:
        path = snapshots / f"{source}.json"
        if not path.is_file():
            missing.append(source)
        else:
            entries.append((source, path))
    if missing:
        raise ContractValidationError(
            "missing required upstream snapshot(s): "
            + ", ".join(missing)
            + " (expected docs/parity/upstream-snapshots/{OMC,OMX,OmO,Antigravity}.json)"
        )
    return entries


def _assert_snapshot_source_matches_filename(
    *,
    expected_source: str,
    upstream_catalog: dict[str, Any],
    catalog_path: Path,
) -> None:
    actual = upstream_catalog.get("source")
    if actual != expected_source:
        raise ContractValidationError(
            f"upstream snapshot {catalog_path.name} source {actual!r} "
            f"!= expected {expected_source!r}"
        )


def _assert_snapshot_pin_matches_inventory(
    *,
    inventory: dict[str, Any],
    upstream_catalog: dict[str, Any],
) -> None:
    source = upstream_catalog.get("source")
    pin = upstream_catalog.get("pin_revision")
    if not isinstance(source, str) or not isinstance(pin, str):
        raise ContractValidationError("upstream catalog missing source or pin_revision")
    pins = inventory.get("upstream_pins")
    if not isinstance(pins, dict) or source not in pins:
        raise ContractValidationError(
            f"inventory missing upstream_pins entry for snapshot source {source!r}"
        )
    expected = pins[source].get("revision") if isinstance(pins[source], dict) else None
    if pin != expected:
        raise ContractValidationError(
            f"upstream snapshot pin_revision {pin!r} != "
            f"inventory upstream_pins[{source!r}].revision {expected!r}"
        )


def check_parity_release_claims(
    *,
    inventory_path: Path,
    repo_root: Path,
    upstream_catalog_path: Path | None = None,
    review_artifact_path: Path | None = None,
    base_inventory: dict[str, Any] | None = None,
    base_inventory_path: Path | str | None = None,
    base_ref: str | None = None,
    require_base_inventory: bool = False,
    now: datetime | None = None,
) -> dict:
    """Return ok payload or raise ContractValidationError."""
    root = Path(repo_root)
    path = Path(inventory_path)
    when = _now_or_utc(now)
    raw = load_json_object(path)
    inventory = validate_parity_inventory(raw, repo_root=root, now=when)

    overclaims = scan_docs_for_overclaims(repo_root=root, inventory=inventory)
    if overclaims:
        raise ContractValidationError(
            "release overclaim gate failed: " + "; ".join(overclaims)
        )

    review_artifact = _load_optional_json(
        Path(review_artifact_path) if review_artifact_path is not None else None
    )
    catalog_entries = _iter_upstream_catalog_paths(root, upstream_catalog_path)
    observed_sources: list[str] = []
    catalogs_by_source: dict[str, dict[str, Any]] = {}
    for expected_source, catalog_path in catalog_entries:
        catalog = validate_upstream_catalog(load_json_object(catalog_path))
        if expected_source is not None:
            _assert_snapshot_source_matches_filename(
                expected_source=expected_source,
                upstream_catalog=catalog,
                catalog_path=catalog_path,
            )
            actual_source = catalog.get("source")
            if isinstance(actual_source, str):
                observed_sources.append(actual_source)
                catalogs_by_source[actual_source] = catalog
        else:
            actual_source = catalog.get("source")
            if isinstance(actual_source, str):
                catalogs_by_source[actual_source] = catalog
        _assert_snapshot_pin_matches_inventory(
            inventory=inventory,
            upstream_catalog=catalog,
        )
        assert_upstream_drift_resolved(
            inventory=inventory,
            upstream_catalog=catalog,
            review_artifact=review_artifact,
        )

    if upstream_catalog_path is None:
        observed_set = set(observed_sources)
        required_set = set(REQUIRED_UPSTREAM_SNAPSHOT_SOURCES)
        if len(observed_sources) != len(observed_set):
            raise ContractValidationError(
                "duplicate upstream snapshot source(s) among required snapshots: "
                + ", ".join(
                    sorted(
                        s
                        for s in observed_set
                        if observed_sources.count(s) > 1
                    )
                )
            )
        if observed_set != required_set:
            raise ContractValidationError(
                "required upstream snapshot sources mismatch: "
                f"observed={sorted(observed_set)} "
                f"expected={sorted(required_set)}"
            )

    resolved_base = resolve_base_inventory(
        root,
        base_inventory=base_inventory,
        base_inventory_path=base_inventory_path,
        base_ref=base_ref,
        require=require_base_inventory,
    )
    if resolved_base is None:
        base_payload = inventory
        resolved_ref = None
    else:
        base_payload = resolved_base.inventory
        resolved_ref = resolved_base.git_ref
    assert_pin_transitions_reviewed(
        inventory=inventory,
        base_inventory=base_payload,
        repo_root=root,
        catalogs_by_source=catalogs_by_source,
        base_ref=resolved_ref,
    )

    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        relative = str(path)

    return {
        "ok": True,
        "inventory_status": inventory.get("inventory_status"),
        "schema_version": inventory.get("schema_version"),
        "overclaims": 0,
        "upstream_drift_checked": True,
        "upstream_drift_resolved": True,
        "pin_transitions_reviewed": True,
        "inventory_path": relative,
    }
