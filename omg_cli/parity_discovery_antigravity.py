"""Antigravity-specific static discovery extractors for parity completeness (#78-I).

The pinned antigravity-cli tree is documentation-only (README/CHANGELOG/examples/
ISSUE_TEMPLATE). Extractors admit only those surfaces — never TypeScript,
package.json, plugin, hooks, or agent registries.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_nonempty_string,
)

_SLUG_RE = re.compile(r"[A-Za-z0-9][\w-]*")
_SEMVER_HEADING_RE = re.compile(r"^##\s+(\d+\.\d+\.\d+)\s*$", re.MULTILINE)
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_FEATURE_ROW_RE = re.compile(
    r"^\|\s*\*\*(.+?)\*\*\s*\|",
    re.MULTILINE,
)

# Required README H2 titles (exact) — fail closed if any are missing.
_REQUIRED_README_H2 = (
    "Features at a Glance",
    "Integration",
    "Installation",
    "Usage",
    "Authentication",
)


def _helpers():
    from omg_cli import parity_discovery as pd

    return pd._category_for_kind, pd._require_relative_posix


def _slugify(text: str, *, label: str) -> str:
    raw = require_nonempty_string(text, label=label).strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    if not slug or not _SLUG_RE.fullmatch(slug):
        raise ContractValidationError(f"{label}: cannot slugify {text!r}")
    return slug


def _decode_utf8(registry_path: str, registry_bytes: bytes) -> str:
    try:
        return registry_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractValidationError(
            f"{registry_path} is not valid UTF-8: {exc}"
        ) from exc


def extract_antigravity_readme_catalog_v1(
    *,
    registry_path: str,
    registry_bytes: bytes,
    category_assignment: Mapping[str, str],
    file_digest: Callable[[bytes], str],
    options: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Parse README.md into documentation section/feature/catalog surfaces."""
    del options
    _category_for_kind, _ = _helpers()
    text = _decode_utf8(registry_path, registry_bytes)
    if not text.strip():
        raise ContractValidationError(f"{registry_path}: empty README rejected")

    h2_titles = [m.group(1).strip() for m in _H2_RE.finditer(text)]
    missing = [t for t in _REQUIRED_README_H2 if t not in h2_titles]
    if missing:
        raise ContractValidationError(
            f"{registry_path}: missing required H2 section(s): "
            + ", ".join(missing)
        )

    digest = file_digest(registry_bytes)
    surfaces: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(surface_id: str, kind: str, anchor: str) -> None:
        if surface_id in seen:
            raise ContractValidationError(
                f"{registry_path}: duplicate surface_id {surface_id}"
            )
        seen.add(surface_id)
        category = _category_for_kind(kind, category_assignment, label=registry_path)
        surfaces.append(
            {
                "surface_id": surface_id,
                "kind": kind,
                "category": category,
                "source_path": registry_path,
                "anchor": anchor,
                "content_digest": digest,
            }
        )

    catalog_kind = "doc-catalog"
    _add("doc.catalog.readme", catalog_kind, "catalog:readme")

    for title in h2_titles:
        slug = _slugify(title, label=f"{registry_path}.h2")
        _add(f"doc.section.{slug}", "doc-section", f"readme-h2:{slug}")

    features = [m.group(1).strip() for m in _FEATURE_ROW_RE.finditer(text)]
    # Drop table header echo rows that are not feature names (none match **…**
    # in the header line "| Feature | … |"). Require at least the known four.
    if len(features) < 4:
        raise ContractValidationError(
            f"{registry_path}: Features table under-parsed ({len(features)} rows)"
        )
    for feature in features:
        slug = _slugify(feature, label=f"{registry_path}.feature")
        _add(f"doc.feature.{slug}", "doc-feature", f"readme-feature:{slug}")

    # Usage documents the `agy` binary — structural fence check, not prose claim.
    if not re.search(r"^```(?:bash|sh)?[^\n]*\nagy\s*$", text, re.MULTILINE):
        raise ContractValidationError(
            f"{registry_path}: Usage fence for `agy` binary not found"
        )
    _add("doc.binary.agy", "doc-binary", "readme-binary:agy")

    input_parts = [{"path": registry_path, "content_digest": digest}]
    return surfaces, input_parts


def extract_antigravity_changelog_releases_v1(
    *,
    registry_path: str,
    registry_bytes: bytes,
    category_assignment: Mapping[str, str],
    file_digest: Callable[[bytes], str],
    options: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Parse CHANGELOG.md ``## X.Y.Z`` headings into release surfaces."""
    del options
    _category_for_kind, _ = _helpers()
    text = _decode_utf8(registry_path, registry_bytes)
    versions = [m.group(1) for m in _SEMVER_HEADING_RE.finditer(text)]
    if not versions:
        raise ContractValidationError(
            f"{registry_path}: no ## X.Y.Z release headings found"
        )
    seen_norm: set[str] = set()
    for ver in versions:
        norm = ver.lower()
        if norm in seen_norm:
            raise ContractValidationError(
                f"{registry_path}: duplicate release heading {ver}"
            )
        seen_norm.add(norm)

    digest = file_digest(registry_bytes)
    category = _category_for_kind("release", category_assignment, label=registry_path)
    surfaces: list[dict[str, Any]] = []
    for ver in versions:
        surfaces.append(
            {
                "surface_id": f"release.{ver}",
                "kind": "release",
                "category": category,
                "source_path": registry_path,
                "anchor": f"changelog:{ver}",
                "content_digest": digest,
            }
        )
    # Umbrella catalog for release history (documentation seed only).
    catalog_category = _category_for_kind(
        "doc-catalog", category_assignment, label=registry_path
    )
    surfaces.append(
        {
            "surface_id": "doc.catalog.changelog",
            "kind": "doc-catalog",
            "category": catalog_category,
            "source_path": registry_path,
            "anchor": "catalog:changelog",
            "content_digest": digest,
        }
    )
    input_parts = [{"path": registry_path, "content_digest": digest}]
    return surfaces, input_parts


def extract_antigravity_examples_tree_v1(
    *,
    registry_path: str,
    category_assignment: Mapping[str, str],
    pin_paths: set[str],
    file_digest: Callable[[bytes], str],
    read_blob: Callable[[str], bytes],
    options: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Enumerate a documented example directory (README.md + top-level *.sh)."""
    del options
    _category_for_kind, _require_relative_posix = _helpers()
    root_dir = _require_relative_posix(registry_path, label="registry_path").rstrip("/")
    readme_path = f"{root_dir}/README.md"
    if readme_path not in pin_paths:
        raise ContractValidationError(
            f"antigravity_examples_tree_v1: missing {readme_path}"
        )

    prefix = root_dir + "/"
    scripts: list[str] = []
    for path in sorted(pin_paths):
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix) :]
        if "/" in rest:
            continue  # top-level only (images/ ignored)
        if rest.endswith(".sh"):
            if not re.fullmatch(r"[A-Za-z0-9][\w-]*\.sh", rest):
                raise ContractValidationError(
                    f"antigravity_examples_tree_v1: invalid script name {path}"
                )
            scripts.append(path)
    if not scripts:
        raise ContractValidationError(
            f"antigravity_examples_tree_v1: no top-level *.sh under {root_dir}/"
        )

    dirname = root_dir.rsplit("/", 1)[-1]
    slug = _slugify(dirname, label=f"{root_dir}.dirname")
    category = _category_for_kind("example", category_assignment, label=root_dir)

    surfaces: list[dict[str, Any]] = []
    input_parts: list[dict[str, str]] = []

    readme_bytes = read_blob(readme_path)
    readme_digest = file_digest(readme_bytes)
    surfaces.append(
        {
            "surface_id": f"example.{slug}",
            "kind": "example",
            "category": category,
            "source_path": readme_path,
            "anchor": f"example:{slug}",
            "content_digest": readme_digest,
        }
    )
    input_parts.append({"path": readme_path, "content_digest": readme_digest})

    for script_path in scripts:
        stem = script_path.rsplit("/", 1)[-1][:-3]
        script_bytes = read_blob(script_path)
        script_digest = file_digest(script_bytes)
        surfaces.append(
            {
                "surface_id": f"example.{slug}.script.{stem}",
                "kind": "example",
                "category": category,
                "source_path": script_path,
                "anchor": f"example-script:{slug}:{stem}",
                "content_digest": script_digest,
            }
        )
        input_parts.append({"path": script_path, "content_digest": script_digest})

    return surfaces, input_parts


def extract_antigravity_issue_templates_v1(
    *,
    registry_path: str,
    category_assignment: Mapping[str, str],
    pin_paths: set[str],
    file_digest: Callable[[bytes], str],
    read_blob: Callable[[str], bytes],
    options: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Enumerate ``.github/ISSUE_TEMPLATE/*.{yml,yaml}`` governance templates."""
    del options
    _category_for_kind, _require_relative_posix = _helpers()
    root_dir = _require_relative_posix(registry_path, label="registry_path").rstrip("/")
    prefix = root_dir + "/"
    category = _category_for_kind(
        "issue-template", category_assignment, label=root_dir
    )

    templates: list[str] = []
    for path in sorted(pin_paths):
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix) :]
        if "/" in rest:
            continue
        if not (rest.endswith(".yml") or rest.endswith(".yaml")):
            continue
        stem = rest.rsplit(".", 1)[0]
        if not _SLUG_RE.fullmatch(stem):
            raise ContractValidationError(
                f"antigravity_issue_templates_v1: invalid template stem in {path}"
            )
        templates.append(path)

    if not templates:
        raise ContractValidationError(
            f"antigravity_issue_templates_v1: no templates under {root_dir}/"
        )

    surfaces: list[dict[str, Any]] = []
    input_parts: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in templates:
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        norm = stem.lower()
        if norm in seen:
            raise ContractValidationError(
                f"antigravity_issue_templates_v1: case-colliding template {stem}"
            )
        seen.add(norm)
        raw = read_blob(path)
        digest = file_digest(raw)
        surfaces.append(
            {
                "surface_id": f"issue-template.{stem}",
                "kind": "issue-template",
                "category": category,
                "source_path": path,
                "anchor": f"issue-template:{stem}",
                "content_digest": digest,
            }
        )
        input_parts.append({"path": path, "content_digest": digest})
    return surfaces, input_parts
