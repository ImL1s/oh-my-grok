"""Read-only plugin skill catalog (#70 Wave A + Wave B/C playbooks).

Single registry for in-session ``skills/omg-*/SKILL.md`` plus classified
aliases. Dual-host routing must consume this catalog — do not add a second
skill list. Grok has no UserPromptSubmit injector; ``<workflow_routing>``
in the global rules file is rendered from this catalog.

Fail-closed: missing catalog, missing plugin SKILL.md, extra uncatalogued
plugin dirs, duplicate ids/aliases, host-native shadowing, unsafe resource
paths, empty plugin ``resources``, ``plugin_skill_count`` mismatch, or
``verified: true`` without live evidence (this slice never sets verified).

Antigravity SKILL.md files are static projections only — not an installed
AG plugin and not live AG evidence. Playbooks without live smoke stay
``configured``, not ``verified``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "omg-skills-catalog/v1"
KIND = "read_only_machine_catalog"
CATALOG_RELATIVE = "skills/catalog.json"
ANTIGRAVITY_PROJECTION_ROOT = "docs/parity/projections/antigravity/skills"
PLUGIN_SKILL_COUNT = 45

ALLOWED_CLASSIFICATIONS = frozenset(
    {
        "faithful",
        "antigravity_native",
        "omg_native",
        "alias",
        "host_owned",
        "excluded",
    }
)
ALLOWED_RUNTIME_OWNERS = frozenset(
    {"grok", "omg-cli", "antigravity", "team", "host", "none"}
)
ALLOWED_KINDS = frozenset({"canonical", "alias"})
ALLOWED_CAPABILITY_MODES = frozenset({"read-only", "read-write", "none"})
FORBIDDEN_CAPABILITY_MODES = frozenset({"execute", "all"})
ALLOWED_CONTINUATION = frozenset({"owner", "none"})
ALLOWED_CONFLICT = frozenset({"refuse", "adopt_existing", "artifact_only", "none"})
ALLOWED_IMPL_STATUS = frozenset(
    {"configured", "catalogued", "deferred", "excluded", "plugin"}
)
ALLOWED_LIVE = frozenset({"unproven", "none"})
GROK_PLUGIN_KIND = "plugin_skill"
GROK_CATALOG_ONLY_KIND = "catalog_only"
ANTIGRAVITY_PROJECTION_KIND = "skill_md_projection"
ANTIGRAVITY_NONE_KIND = "none"

# Host slash/commands that aliases must not install as plugin skill dirs.
HOST_NATIVE_PROTECTED = frozenset(
    {"plan", "goal", "loop", "compact", "help", "agents", "mcp", "skills", "plugin"}
)

CONTINUATION_OWNERS = frozenset(
    {
        "omg-autopilot",
        "omg-ralph",
        "omg-pipeline",
        "omg-ultrawork",
        "omg-ultragoal",
        "omg-ultraqa",
        "omg-team",
        "omg-ralplan",
    }
)

# Keyword collision priority documented in rules + docs/skills.md.
# cancel > ralplan > autopilot > ultragoal > ralph > ulw, then remaining
# continuation owners, then other plugin skills.
ROUTING_PRIORITY_HEAD = (
    "omg-cancel",
    "omg-ralplan",
    "omg-autopilot",
    "omg-ultragoal",
    "omg-ralph",
    "omg-ultrawork",
)

# Same-run evidence contribution that must not start a second loop.
_ADOPT_PAIRS = frozenset(
    {
        ("omg-autopilot", "omg-ultraqa"),
        ("omg-autopilot", "omg-dual-review"),
        ("omg-autopilot", "omg-ralplan"),
        ("omg-autopilot", "omg-deep-interview"),
        ("omg-ralph", "omg-ultraqa"),
        ("omg-ralph", "omg-dual-review"),
        ("omg-pipeline", "omg-dual-review"),
        ("omg-pipeline", "omg-ultraqa"),
        ("omg-ultrawork", "omg-dual-review"),
        ("omg-team", "omg-dual-review"),
        ("omg-ultragoal", "omg-ralph"),
        ("omg-ultragoal", "omg-ultrawork"),
        ("omg-ultragoal", "omg-autopilot"),
    }
)

_ALWAYS_ADOPT = frozenset({"omg-cancel", "omg-using"})
_ARTIFACT_ONLY_DEFAULT = frozenset(
    {
        "omg-wiki",
        "omg-hud",
        "omg-lsp",
        "omg-ask",
        "omg-trace",
        "omg-skill",
        "omg-configure-notifications",
        "omg-mcp-setup",
        "omg-deepinit",
        "omg-project-session-manager",
        "omg-writer-memory",
        "omg-external-context",
        "omg-best-practice-research",
        "omg-design",
        "omg-git-master",
        "omg-ecomode",
        "omg-release",
    }
)

_INFO_QUESTION = re.compile(
    r"^\s*(what(?:'s| is| are)|how (?:does|do|is)|explain|tell me about)\b",
    re.IGNORECASE,
)

PROJECTION_BANNER_TITLE = "PROJECTION — not an installed Antigravity plugin"
PROJECTION_BANNER_NEEDLES = (
    "not an installed",
    "not live",
    "projection",
)

_CANONICAL_REQUIRED = (
    "id",
    "kind",
    "classification",
    "runtime_owner",
    "capability_mode",
    "continuation",
    "conflict_policy",
    "implementation_status",
    "live_verification",
    "verified",
    "projections",
)


class SkillsCatalogError(ValueError):
    """Fail-closed catalog load / validation error."""


@dataclass(frozen=True, slots=True)
class HostProjection:
    """One host projection target (Grok plugin skill or AG SKILL.md)."""

    kind: str
    path: str


@dataclass(frozen=True, slots=True)
class SkillRecord:
    """One catalogued skill or alias."""

    id: str
    kind: str
    classification: str
    runtime_owner: str
    file: str | None
    aliases: tuple[str, ...]
    canonical: str | None
    sources: tuple[dict[str, str], ...]
    cli_twin: str | None
    capability_mode: str
    continuation: str
    conflict_policy: str
    implementation_status: str
    live_verification: str
    verified: bool
    triggers: tuple[str, ...]
    pipeline_next: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    resources: tuple[str, ...]
    host_native_protected: bool
    deferred_issue: int | None
    exclude_reason: str | None
    projections: Mapping[str, HostProjection]

    def to_inspect_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "classification": self.classification,
            "runtime_owner": self.runtime_owner,
            "file": self.file,
            "aliases": list(self.aliases),
            "canonical": self.canonical,
            "cli_twin": self.cli_twin,
            "capability_mode": self.capability_mode,
            "continuation": self.continuation,
            "conflict_policy": self.conflict_policy,
            "implementation_status": self.implementation_status,
            "live_verification": self.live_verification,
            "verified": False,
            "host_native_protected": self.host_native_protected,
        }
        if self.deferred_issue is not None:
            row["deferred_issue"] = self.deferred_issue
        if self.exclude_reason:
            row["exclude_reason"] = self.exclude_reason
        row["projections"] = {
            name: {"kind": proj.kind, "path": proj.path}
            for name, proj in self.projections.items()
        }
        return row


@dataclass(frozen=True, slots=True)
class SkillsCatalog:
    """Loaded, validated catalog."""

    schema: str
    skills: tuple[SkillRecord, ...]
    alias_index: Mapping[str, str]

    def by_id(self) -> dict[str, SkillRecord]:
        return {record.id: record for record in self.skills}

    @property
    def plugin_skills(self) -> tuple[SkillRecord, ...]:
        return tuple(
            record
            for record in self.skills
            if record.kind == "canonical" and record.file is not None
        )

    def resolve(self, name: str) -> SkillRecord | None:
        key = (name or "").strip().lower()
        if not key:
            return None
        by_id = self.by_id()
        if key in by_id:
            record = by_id[key]
            if record.kind == "alias" and record.canonical:
                return by_id.get(record.canonical)
            return record
        canonical_id = self.alias_index.get(key)
        if canonical_id:
            return by_id.get(canonical_id)
        return None


def plugin_root() -> Path:
    """Checkout / plugin root (parent of ``omg_cli``)."""
    return Path(__file__).resolve().parents[1]


def catalog_path(root: Path | None = None) -> Path:
    return (root if root is not None else plugin_root()) / CATALOG_RELATIVE


def antigravity_projection_relative(skill_id: str) -> str:
    return f"{ANTIGRAVITY_PROJECTION_ROOT}/{skill_id}/SKILL.md"


def list_plugin_skill_dirs(root: Path) -> list[Path]:
    """Sorted ``skills/omg-*`` directories that contain SKILL.md (non-recursive)."""
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(skills_dir.iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        skill = path / "SKILL.md"
        if skill.is_file() and not skill.is_symlink() and path.name.startswith("omg-"):
            found.append(path)
    return found


def _require_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SkillsCatalogError(f"{label} must be a JSON object")
    return value


def _require_str(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SkillsCatalogError(f"{label} must be a non-empty string")
    return value


def _require_keys(obj: Mapping[str, Any], required: tuple[str, ...], *, label: str) -> None:
    missing = [key for key in required if key not in obj]
    if missing:
        raise SkillsCatalogError(f"{label} missing keys: {', '.join(missing)}")


def _posix_relative(value: str, *, label: str) -> str:
    text = _require_str(value, label=label)
    if "\x00" in text:
        raise SkillsCatalogError(f"{label} must not contain NUL")
    if text.startswith("/") or text.startswith("\\") or ".." in Path(text).parts:
        raise SkillsCatalogError(f"{label} must be a relative posix path: {text!r}")
    if "\\" in text:
        raise SkillsCatalogError(f"{label} must use posix separators: {text!r}")
    return text


def _str_tuple(value: Any, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise SkillsCatalogError(f"{label} must be an array of non-empty strings")
    return tuple(item.strip() for item in value)


def _parse_projection(value: Any, *, label: str) -> HostProjection:
    obj = _require_object(value, label=label)
    _require_keys(obj, ("kind", "path"), label=label)
    kind = _require_str(obj["kind"], label=f"{label}.kind")
    path = obj["path"]
    if kind == ANTIGRAVITY_NONE_KIND or kind == GROK_CATALOG_ONLY_KIND:
        if path not in ("", None):
            path_text = path if isinstance(path, str) else ""
            if path_text:
                raise SkillsCatalogError(
                    f"{label}.path must be empty for kind {kind!r}"
                )
        return HostProjection(kind=kind, path="")
    return HostProjection(
        kind=kind,
        path=_posix_relative(str(path), label=f"{label}.path"),
    )


def _parse_skill(value: Any, *, index: int) -> SkillRecord:
    label = f"skills[{index}]"
    obj = _require_object(value, label=label)
    kind = _require_str(obj.get("kind"), label=f"{label}.kind")
    if kind not in ALLOWED_KINDS:
        raise SkillsCatalogError(f"{label}.kind must be one of {sorted(ALLOWED_KINDS)}")
    skill_id = _require_str(obj.get("id"), label=f"{label}.id").lower()
    if skill_id != _require_str(obj.get("id"), label=f"{label}.id"):
        raise SkillsCatalogError(f"{label}.id must be lowercase, got {obj.get('id')!r}")
    classification = _require_str(obj.get("classification"), label=f"{label}.classification")
    if classification not in ALLOWED_CLASSIFICATIONS:
        raise SkillsCatalogError(
            f"{label}.classification must be one of {sorted(ALLOWED_CLASSIFICATIONS)}"
        )
    if kind == "alias":
        canonical = _require_str(obj.get("canonical"), label=f"{label}.canonical")
        if classification != "alias":
            raise SkillsCatalogError(f"{skill_id}: alias records must classify as alias")
        host_native = bool(obj.get("host_native_protected", False))
        if skill_id in HOST_NATIVE_PROTECTED and not host_native:
            raise SkillsCatalogError(
                f"{skill_id}: host-native name must set host_native_protected=true"
            )
        grok = HostProjection(kind=GROK_CATALOG_ONLY_KIND, path="")
        ag = HostProjection(kind=ANTIGRAVITY_NONE_KIND, path="")
        projections_raw = obj.get("projections")
        if isinstance(projections_raw, dict):
            grok = _parse_projection(
                projections_raw.get("grok", {"kind": GROK_CATALOG_ONLY_KIND, "path": ""}),
                label=f"{label}.projections.grok",
            )
            ag = _parse_projection(
                projections_raw.get(
                    "antigravity", {"kind": ANTIGRAVITY_NONE_KIND, "path": ""}
                ),
                label=f"{label}.projections.antigravity",
            )
        return SkillRecord(
            id=skill_id,
            kind="alias",
            classification="alias",
            runtime_owner=_require_str(
                obj.get("runtime_owner", "none"), label=f"{label}.runtime_owner"
            )
            if obj.get("runtime_owner")
            else "none",
            file=None,
            aliases=(),
            canonical=canonical,
            sources=(),
            cli_twin=None,
            capability_mode="none",
            continuation="none",
            conflict_policy="none",
            implementation_status="catalogued",
            live_verification="none",
            verified=False,
            triggers=(),
            pipeline_next=(),
            required_capabilities=(),
            resources=(),
            host_native_protected=host_native,
            deferred_issue=None,
            exclude_reason=None,
            projections={"grok": grok, "antigravity": ag},
        )

    _require_keys(obj, _CANONICAL_REQUIRED, label=label)
    if not skill_id.startswith("omg-"):
        raise SkillsCatalogError(f"{label}.id must be omg-* canonical, got {skill_id!r}")
    short = skill_id.removeprefix("omg-")
    if short in HOST_NATIVE_PROTECTED:
        raise SkillsCatalogError(
            f"{skill_id}: canonical plugin id would shadow host-native {short!r}"
        )
    runtime_owner = _require_str(obj["runtime_owner"], label=f"{label}.runtime_owner")
    if runtime_owner not in ALLOWED_RUNTIME_OWNERS:
        raise SkillsCatalogError(
            f"{label}.runtime_owner must be one of {sorted(ALLOWED_RUNTIME_OWNERS)}"
        )
    file_rel = obj.get("file")
    if file_rel is None:
        file_path = None
    else:
        file_path = _posix_relative(file_rel, label=f"{label}.file")
        expected = f"skills/{skill_id}/SKILL.md"
        if file_path != expected:
            raise SkillsCatalogError(
                f"{label}.file must be {expected!r}, got {file_path!r}"
            )
    capability_mode = _require_str(
        obj["capability_mode"], label=f"{label}.capability_mode"
    ).strip()
    lowered = capability_mode.lower()
    if lowered in FORBIDDEN_CAPABILITY_MODES:
        raise SkillsCatalogError(
            f"{label}.capability_mode {capability_mode!r} is forbidden "
            f"(never execute/all)"
        )
    if capability_mode not in ALLOWED_CAPABILITY_MODES:
        raise SkillsCatalogError(
            f"{label}.capability_mode must be one of "
            f"{sorted(ALLOWED_CAPABILITY_MODES)}"
        )
    continuation = _require_str(obj["continuation"], label=f"{label}.continuation")
    if continuation not in ALLOWED_CONTINUATION:
        raise SkillsCatalogError(
            f"{label}.continuation must be one of {sorted(ALLOWED_CONTINUATION)}"
        )
    conflict_policy = _require_str(
        obj["conflict_policy"], label=f"{label}.conflict_policy"
    )
    if conflict_policy not in ALLOWED_CONFLICT:
        raise SkillsCatalogError(
            f"{label}.conflict_policy must be one of {sorted(ALLOWED_CONFLICT)}"
        )
    impl = _require_str(
        obj["implementation_status"], label=f"{label}.implementation_status"
    )
    if impl not in ALLOWED_IMPL_STATUS:
        raise SkillsCatalogError(
            f"{label}.implementation_status must be one of {sorted(ALLOWED_IMPL_STATUS)}"
        )
    live = _require_str(obj["live_verification"], label=f"{label}.live_verification")
    if live not in ALLOWED_LIVE:
        raise SkillsCatalogError(
            f"{label}.live_verification must be one of {sorted(ALLOWED_LIVE)}"
        )
    verified = obj["verified"]
    if verified is not False:
        raise SkillsCatalogError(
            f"{skill_id}: verified must be false without live evidence"
        )
    if live != "unproven" and live != "none":
        raise SkillsCatalogError(f"{skill_id}: live_verification not a live claim")
    aliases = _str_tuple(obj.get("aliases"), label=f"{label}.aliases")
    for alias in aliases:
        if alias != alias.lower():
            raise SkillsCatalogError(f"{skill_id}: alias {alias!r} must be lowercase")
        if alias.startswith("omg-"):
            raise SkillsCatalogError(
                f"{skill_id}: aliases must be short names, not {alias!r}"
            )
    sources_raw = obj.get("sources") or []
    if not isinstance(sources_raw, list):
        raise SkillsCatalogError(f"{label}.sources must be an array")
    sources: list[dict[str, str]] = []
    for item in sources_raw:
        src = _require_object(item, label=f"{label}.sources[]")
        sources.append(
            {
                "project": _require_str(src.get("project"), label="sources.project"),
                "name": _require_str(src.get("name"), label="sources.name"),
            }
        )
    cli_twin = obj.get("cli_twin")
    if cli_twin is not None:
        cli_twin = _require_str(cli_twin, label=f"{label}.cli_twin")
    deferred = obj.get("deferred_issue")
    if deferred is not None:
        if not isinstance(deferred, int) or isinstance(deferred, bool) or deferred < 1:
            raise SkillsCatalogError(f"{label}.deferred_issue must be a positive int")
    exclude_reason = obj.get("exclude_reason")
    if exclude_reason is not None:
        exclude_reason = _require_str(exclude_reason, label=f"{label}.exclude_reason")
    if classification == "excluded" and not exclude_reason:
        raise SkillsCatalogError(f"{skill_id}: excluded skills need exclude_reason")
    projections_raw = _require_object(obj["projections"], label=f"{label}.projections")
    _require_keys(projections_raw, ("grok", "antigravity"), label=f"{label}.projections")
    grok = _parse_projection(projections_raw["grok"], label=f"{label}.projections.grok")
    ag = _parse_projection(
        projections_raw["antigravity"], label=f"{label}.projections.antigravity"
    )
    if file_path is not None:
        if grok.kind != GROK_PLUGIN_KIND or grok.path != file_path:
            raise SkillsCatalogError(
                f"{skill_id}: grok projection must be {GROK_PLUGIN_KIND} {file_path}"
            )
        if ag.kind != ANTIGRAVITY_PROJECTION_KIND:
            raise SkillsCatalogError(
                f"{skill_id}: plugin skills need antigravity skill_md_projection"
            )
        expected_ag = antigravity_projection_relative(skill_id)
        if ag.path != expected_ag:
            raise SkillsCatalogError(
                f"{skill_id}: antigravity projection path must be {expected_ag!r}"
            )
        if impl not in {"plugin", "configured"}:
            raise SkillsCatalogError(
                f"{skill_id}: plugin SKILL.md requires implementation_status "
                f"plugin|configured"
            )
    else:
        if grok.kind != GROK_CATALOG_ONLY_KIND:
            raise SkillsCatalogError(
                f"{skill_id}: catalog-only grok projection kind must be "
                f"{GROK_CATALOG_ONLY_KIND}"
            )
        if ag.kind != ANTIGRAVITY_NONE_KIND:
            raise SkillsCatalogError(
                f"{skill_id}: catalog-only antigravity projection kind must be none"
            )
    extra_hosts = sorted(set(projections_raw) - {"grok", "antigravity"})
    if extra_hosts:
        raise SkillsCatalogError(
            f"{skill_id}: unknown projection hosts: {', '.join(extra_hosts)}"
        )
    resources = _str_tuple(obj.get("resources"), label=f"{label}.resources")
    for rel in resources:
        _posix_relative(rel, label=f"{label}.resources")
    if file_path is not None and not resources:
        raise SkillsCatalogError(
            f"{skill_id}: plugin skills must declare at least one resource"
        )
    return SkillRecord(
        id=skill_id,
        kind="canonical",
        classification=classification,
        runtime_owner=runtime_owner,
        file=file_path,
        aliases=aliases,
        canonical=None,
        sources=tuple(sources),
        cli_twin=cli_twin,
        capability_mode=capability_mode,
        continuation=continuation,
        conflict_policy=conflict_policy,
        implementation_status=impl,
        live_verification=live,
        verified=False,
        triggers=_str_tuple(obj.get("triggers"), label=f"{label}.triggers"),
        pipeline_next=_str_tuple(obj.get("pipeline_next"), label=f"{label}.pipeline_next"),
        required_capabilities=_str_tuple(
            obj.get("required_capabilities"), label=f"{label}.required_capabilities"
        ),
        resources=resources,
        host_native_protected=bool(obj.get("host_native_protected", False)),
        deferred_issue=deferred,
        exclude_reason=exclude_reason,
        projections={"grok": grok, "antigravity": ag},
    )


def _load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillsCatalogError(f"cannot read catalog: {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SkillsCatalogError(f"catalog is not valid JSON: {path}: {exc}") from exc


def load_skills_catalog(
    root: Path | None = None,
    *,
    require_projections: bool = True,
) -> SkillsCatalog:
    """Load and fail-closed validate the plugin skill catalog."""
    base = Path(root) if root is not None else plugin_root()
    path = catalog_path(base)
    if not path.is_file():
        raise SkillsCatalogError(f"missing skill catalog: {CATALOG_RELATIVE}")
    raw = _require_object(_load_json(path), label="catalog")
    schema = _require_str(raw.get("schema"), label="schema")
    if schema != SCHEMA:
        raise SkillsCatalogError(f"unsupported catalog schema {schema!r}")
    kind = _require_str(raw.get("kind"), label="kind")
    if kind != KIND:
        raise SkillsCatalogError(f"catalog kind must be {KIND!r}, got {kind!r}")
    skills_raw = raw.get("skills")
    if not isinstance(skills_raw, list) or not skills_raw:
        raise SkillsCatalogError("catalog.skills must be a non-empty array")
    seen: dict[str, int] = {}
    records: list[SkillRecord] = []
    alias_index: dict[str, str] = {}

    def _claim_name(name: str, *, owner: str, index: int) -> None:
        if name in seen:
            raise SkillsCatalogError(
                f"duplicate skill id/alias {name!r} at indexes {seen[name]} and {index}"
            )
        seen[name] = index
        if name in HOST_NATIVE_PROTECTED and owner.startswith("omg-"):
            # Allowed only as host_native_protected alias rows, claimed above.
            pass

    for index, item in enumerate(skills_raw):
        record = _parse_skill(item, index=index)
        _claim_name(record.id, owner=record.id, index=index)
        records.append(record)
        if record.kind == "alias":
            alias_index[record.id] = str(record.canonical)
        for alias in record.aliases:
            if alias in HOST_NATIVE_PROTECTED:
                raise SkillsCatalogError(
                    f"{record.id}: alias {alias!r} is host-native; use an alias "
                    "record with host_native_protected=true"
                )
            _claim_name(alias, owner=record.id, index=index)
            alias_index[alias] = record.id

    by_id = {record.id: record for record in records}
    for record in records:
        if record.kind == "alias":
            target = by_id.get(str(record.canonical))
            if target is None or target.kind != "canonical":
                raise SkillsCatalogError(
                    f"{record.id}: alias canonical {record.canonical!r} is missing"
                )
        for nxt in record.pipeline_next:
            if nxt not in by_id or by_id[nxt].kind != "canonical":
                raise SkillsCatalogError(
                    f"{record.id}: pipeline_next {nxt!r} is not a canonical skill"
                )

    disk_dirs = list_plugin_skill_dirs(base)
    disk_ids = {path.name for path in disk_dirs}
    plugin_ids = {record.id for record in records if record.file is not None}
    declared_count = raw.get("plugin_skill_count")
    if not isinstance(declared_count, int) or isinstance(declared_count, bool):
        raise SkillsCatalogError("catalog.plugin_skill_count must be an integer")
    if declared_count != len(plugin_ids):
        raise SkillsCatalogError(
            f"catalog.plugin_skill_count {declared_count} != "
            f"{len(plugin_ids)} plugin skills"
        )
    missing = sorted(plugin_ids - disk_ids)
    extra = sorted(disk_ids - plugin_ids)
    if missing:
        raise SkillsCatalogError(
            "missing SKILL.md for catalog id(s): " + ", ".join(missing)
        )
    if extra:
        raise SkillsCatalogError(
            "uncatalogued skills/omg-* on disk: " + ", ".join(extra)
        )
    for record in records:
        if record.file:
            skill_path = base / record.file
            if not skill_path.is_file() or skill_path.is_symlink():
                raise SkillsCatalogError(f"missing skill: {record.file}")
            if require_projections:
                rel = record.projections["antigravity"].path
                proj = base / rel
                if not proj.is_file() or proj.is_symlink():
                    raise SkillsCatalogError(f"missing antigravity projection: {rel}")
            for rel in record.resources:
                resolved = resolve_skill_resource(
                    base, record.id, rel, catalog=None, record=record
                )
                if not resolved.is_file():
                    raise SkillsCatalogError(
                        f"{record.id}: missing bundled resource {rel!r}"
                    )
    records.sort(key=lambda item: (0 if item.kind == "canonical" else 1, item.id))
    return SkillsCatalog(
        schema=schema, skills=tuple(records), alias_index=alias_index
    )


def inspect_skills_catalog(root: Path | None = None) -> dict[str, Any]:
    """Inspect payload for ``omg capabilities`` / ``omg skill`` (never verified)."""
    base = Path(root) if root is not None else plugin_root()
    try:
        catalog = load_skills_catalog(base)
    except SkillsCatalogError as exc:
        return {
            "schema": SCHEMA,
            "ok": False,
            "configured": (base / CATALOG_RELATIVE).is_file(),
            "installed": False,
            "enabled": False,
            "loadable": False,
            "observed": False,
            "healthy": False,
            "verified": False,
            "classification": "native_substitute",
            "error": str(exc),
            "note": (
                "read-only skill catalog; Grok <workflow_routing> is rendered "
                "from this catalog (not a UserPromptSubmit injector); "
                "AG files are projections only"
            ),
        }
    plugin_n = len(catalog.plugin_skills)
    return {
        "schema": SCHEMA,
        "ok": True,
        "configured": True,
        "installed": True,
        "enabled": True,
        "loadable": True,
        "observed": False,
        "healthy": False,
        "verified": False,
        "classification": "native_substitute",
        "plugin_skill_count": plugin_n,
        "catalog_count": len(catalog.skills),
        "skills": [record.to_inspect_row() for record in catalog.skills],
        "note": (
            "read-only skill catalog; Grok <workflow_routing> is rendered from "
            "this catalog (not a UserPromptSubmit injector). "
            "Antigravity SKILL.md files are static projections only "
            "(not an installed AG plugin, not live AG evidence). "
            "A playbook without live smoke is configured, not verified."
        ),
    }


def is_informational_question(text: str) -> bool:
    """True when the prompt is asking *about* a skill, not invoking it."""
    return bool(_INFO_QUESTION.search(text or ""))


def _trigger_contained(haystack: str, needle: str) -> bool:
    """True when *needle* appears on token/phrase boundaries (not as a substring)."""
    token = needle.strip().lower()
    if not token:
        return False
    pattern = r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])"
    return re.search(pattern, haystack) is not None


def resolve_trigger(catalog: SkillsCatalog, text: str) -> SkillRecord | None:
    """Map user text to a canonical skill; suppress informational questions."""
    if is_informational_question(text):
        return None
    lowered = (text or "").strip().lower()
    if not lowered:
        return None
    # Prefer longer trigger phrases, then exact alias/id tokens.
    candidates: list[tuple[int, SkillRecord]] = []
    for record in catalog.skills:
        if record.kind != "canonical":
            continue
        names = (record.id, record.id.removeprefix("omg-"), *record.aliases, *record.triggers)
        for name in names:
            needle = name.strip().lower()
            if needle and _trigger_contained(lowered, needle):
                candidates.append((len(needle), record))
    if not candidates:
        token = lowered.split()[0].lstrip("/")
        return catalog.resolve(token)
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def resolve_continuation(
    active_owner: str | None, requested: str, *, catalog: SkillsCatalog | None = None
) -> str:
    """Deterministic loop conflict: refuse | adopt_existing | artifact_only.

    Exactly one continuation owner may run. Cancel/using always adopt.
    Compatible QA/review under an active autopilot/ralph loop adopts.
    """
    requested_id = requested.strip().lower()
    if catalog is not None:
        record = catalog.resolve(requested_id)
        if record is not None:
            requested_id = record.id
    active = (active_owner or "").strip().lower() or None
    if catalog is not None and active:
        owner = catalog.resolve(active)
        active = owner.id if owner is not None else active
    if not active:
        return "none"
    if requested_id == active:
        return "adopt_existing"
    if requested_id in _ALWAYS_ADOPT:
        return "adopt_existing"
    if (active, requested_id) in _ADOPT_PAIRS:
        return "adopt_existing"
    if requested_id in _ARTIFACT_ONLY_DEFAULT:
        return "artifact_only"
    if requested_id in CONTINUATION_OWNERS and active in CONTINUATION_OWNERS:
        return "refuse"
    if catalog is not None:
        record = catalog.by_id().get(requested_id)
        if record is not None and record.conflict_policy in ALLOWED_CONFLICT:
            if record.conflict_policy != "none":
                return record.conflict_policy
    if requested_id in CONTINUATION_OWNERS:
        return "refuse"
    return "artifact_only"


def resolve_skill_resource(
    root: Path,
    skill_id: str,
    rel: str,
    *,
    catalog: SkillsCatalog | None = None,
    record: SkillRecord | None = None,
) -> Path:
    """Resolve a bundled skill resource with fail-closed confinement."""
    if not skill_id or not rel:
        raise SkillsCatalogError("skill resource path is required")
    rel_posix = _posix_relative(rel, label="resource")
    base = Path(root).resolve()
    loaded = record
    if loaded is None and catalog is not None:
        loaded = catalog.by_id().get(skill_id)
    skills_root = base / "skills"
    skill_dir_raw = skills_root / skill_id
    if skill_dir_raw.is_symlink():
        raise SkillsCatalogError("skill resources may not be symlinks")
    skill_dir = skill_dir_raw.resolve()
    try:
        skill_dir.relative_to(skills_root.resolve())
    except ValueError as exc:
        raise SkillsCatalogError("skill directory escapes skills/") from exc
    cursor = skill_dir_raw
    for part in Path(rel_posix).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise SkillsCatalogError("skill resources may not be symlinks")
    try:
        candidate = cursor.resolve()
    except ValueError as exc:
        raise SkillsCatalogError(f"resource {rel_posix!r} is not a valid path") from exc
    try:
        candidate.relative_to(skill_dir)
    except ValueError as exc:
        raise SkillsCatalogError(
            f"resource {rel_posix!r} escapes skill directory {skill_id}"
        ) from exc
    if loaded is not None and rel_posix not in loaded.resources:
        raise SkillsCatalogError(
            f"{skill_id}: resource {rel_posix!r} is not declared in the catalog"
        )
    return candidate


def required_capability_diagnostics(
    record: SkillRecord, available: Mapping[str, bool] | None = None
) -> dict[str, Any]:
    """Fail-fast blocked result when a declared capability is missing."""
    have = available or {}
    missing = [name for name in record.required_capabilities if not have.get(name, False)]
    if not missing:
        return {"ok": True, "blocked": False, "missing": []}
    return {
        "ok": False,
        "blocked": True,
        "verified": False,
        "missing": missing,
        "error": "E_SKILL_CAPABILITY_MISSING",
        "message": (
            f"{record.id} is blocked; missing capabilities: {', '.join(missing)}. "
            "This is not completion."
        ),
        "next_action": "Install/enable the required MCP/plugin capability, then retry",
    }


def strip_markdown_frontmatter(text: str) -> str:
    """Return markdown body after a leading YAML frontmatter fence."""
    if not text.startswith("---"):
        return text.strip()
    parts = text.split("---", 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return text.strip()


def render_antigravity_skill_md(record: SkillRecord, source_text: str) -> str:
    """Render a static AG SKILL.md projection (not an install)."""
    body = strip_markdown_frontmatter(source_text)
    frontmatter = "\n".join(
        [
            "---",
            f"name: {record.id}",
            f"description: OMG skill projection ({record.classification}, "
            f"{record.capability_mode})",
            "omg_projection: true",
            f"omg_classification: {record.classification}",
            f"omg_capability_mode: {record.capability_mode}",
            f"omg_source_skill: {record.file or ''}",
            "---",
            "",
        ]
    )
    banner = "\n".join(
        [
            f"# {PROJECTION_BANNER_TITLE}",
            "",
            "This file is a static parity projection of the Grok plugin skill",
            f"`{record.file}`. It is not an installed Antigravity plugin,",
            "not live AG evidence, and does not mean `agy` skill discovery works.",
            "",
            f"- Catalog: `{CATALOG_RELATIVE}`",
            f"- capability_mode: `{record.capability_mode}` (never `execute`/`all`)",
            "- Playbook without runtime evidence stays `configured`, not `verified`.",
            "",
            body,
            "",
        ]
    )
    return frontmatter + banner


def render_antigravity_projections(
    root: Path,
    *,
    catalog: SkillsCatalog | None = None,
) -> dict[str, str]:
    """Map relative projection paths to rendered markdown (plugin skills only)."""
    loaded = catalog or load_skills_catalog(root, require_projections=False)
    out: dict[str, str] = {}
    for record in loaded.plugin_skills:
        source = root / str(record.file)
        if not source.is_file():
            raise SkillsCatalogError(f"missing skill: {record.file}")
        rel = record.projections["antigravity"].path
        out[rel] = render_antigravity_skill_md(
            record, source.read_text(encoding="utf-8")
        )
    return out


def routing_order(catalog: SkillsCatalog) -> tuple[SkillRecord, ...]:
    """Plugin skills in documented keyword-collision priority."""
    by_id = catalog.by_id()
    ordered: list[SkillRecord] = []
    seen: set[str] = set()
    for skill_id in ROUTING_PRIORITY_HEAD:
        record = by_id.get(skill_id)
        if record is not None and record.kind == "canonical":
            ordered.append(record)
            seen.add(skill_id)
    for skill_id in sorted(CONTINUATION_OWNERS - seen):
        record = by_id.get(skill_id)
        if record is not None and record.kind == "canonical":
            ordered.append(record)
            seen.add(skill_id)
    others = [
        record
        for record in catalog.plugin_skills
        if record.id not in seen
    ]
    others.sort(key=lambda item: item.id)
    ordered.extend(others)
    return tuple(ordered)


def render_workflow_routing(catalog: SkillsCatalog) -> str:
    """Inner text of ``<workflow_routing>`` generated from catalog triggers.

    Source of truth is ``skills/catalog.json`` — do not keep a second skill list.
    Informational questions stay suppressed via ``resolve_trigger``.
    """
    lines = [
        "Keyword routing is generated from `skills/catalog.json` (triggers + aliases).",
        "Grok has no UserPromptSubmit injector — this rules section is the router.",
        "Dual-host: Antigravity projections consume the same catalog; they are",
        "not an installed AG plugin and not live AG evidence.",
        "Informational questions (`what is ralph?`, `how does autopilot work?`)",
        "do **not** activate a skill.",
        "",
        "Priority when several keywords match:",
        "`cancel` > `ralplan` > `autopilot` > `ultragoal` > `ralph` > `ulw`,",
        "then remaining continuation owners (`pipeline`, `ultraqa`, `team`),",
        "then other plugin skills.",
        "",
        "Continuation owners (exactly one may run): "
        + ", ".join(f"`{name}`" for name in sorted(CONTINUATION_OWNERS))
        + ".",
        "",
        "| Triggers / aliases | Skill | CLI twin | Conflict |",
        "|--------------------|-------|----------|----------|",
    ]
    for record in routing_order(catalog):
        names: list[str] = []
        for raw in (
            record.id.removeprefix("omg-"),
            *record.aliases,
            *record.triggers,
        ):
            token = raw.strip()
            if token and token not in names:
                names.append(token)
        triggers = ", ".join(f"`{name}`" for name in names[:10]) or f"`{record.id}`"
        cli = record.cli_twin or "—"
        conflict = (
            "continuation owner"
            if record.continuation == "owner"
            else record.conflict_policy
        )
        lines.append(
            f"| {triggers} | `{record.id}` | `{cli}` | `{conflict}` |"
        )
    return "\n".join(lines)


def _refuse_symlink_dest(path: Path) -> None:
    """Refuse to follow or replace a symlink destination."""
    if path.is_symlink():
        raise SkillsCatalogError(
            f"refusing symlink dest: {path.as_posix()}"
        )
    if (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and path.exists()
    ):
        flags = os.O_RDONLY | os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
            os.close(fd)
        except OSError as exc:
            raise SkillsCatalogError(
                f"refusing dest (O_NOFOLLOW): {path.as_posix()}: {exc}"
            ) from exc


def _atomic_write_text(path: Path, text: str) -> None:
    """Same-dir temp + ``os.replace``. Refuses symlink dest; POSIX O_NOFOLLOW."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        _refuse_symlink_dest(dest)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(dest.parent),
        prefix=f".{dest.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, dest)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _prune_obsolete_projections(root: Path, keep: set[str]) -> list[str]:
    """Remove projection files not in *keep*. Always keep README.md. No follow."""
    skills_root = root / ANTIGRAVITY_PROJECTION_ROOT
    if not skills_root.is_dir() or skills_root.is_symlink():
        return []
    removed: list[str] = []
    keep = set(keep)
    keep.add(f"{ANTIGRAVITY_PROJECTION_ROOT}/README.md")
    files = sorted(
        (path for path in skills_root.rglob("*") if path.is_file()),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for path in files:
        if path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel not in keep:
            path.unlink()
            removed.append(rel)
    dirs = sorted(
        (path for path in skills_root.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for path in dirs:
        if path == skills_root or path.is_symlink():
            continue
        try:
            next(path.iterdir())
        except StopIteration:
            path.rmdir()
        except OSError:
            pass
    return removed


def _projection_readme() -> str:
    return """# Antigravity skill projections

**Status:** static parity projection for
[#70](https://github.com/ImL1s/oh-my-grok/issues/70).

These files are **not**:

- an installed Antigravity plugin
- live AG evidence
- proof that `agy` install or skill discovery works
- a Grok UserPromptSubmit injector

They are generated from `skills/catalog.json` plus `skills/omg-*/SKILL.md` by
`scripts/generate_antigravity_skill_projections.py`. Dual-host **routing**
consumes the same catalog: Grok global rules fill `<workflow_routing>` from
triggers/aliases; these AG files are projections of the playbook body only.

Catalog-only / alias / excluded rows have no AG file. Playbooks without live
smoke stay `configured`, not `verified`.

Regenerate:

```bash
python scripts/generate_antigravity_skill_projections.py
python scripts/generate_antigravity_skill_projections.py --check
```
"""


def write_antigravity_projections(root: Path) -> list[str]:
    """Write committed AG projections (atomic, no-follow). Prunes obsolete files."""
    rendered = render_antigravity_projections(root)
    written: list[str] = []
    for rel, text in rendered.items():
        _atomic_write_text(root / rel, text)
        written.append(rel)
    readme_rel = f"{ANTIGRAVITY_PROJECTION_ROOT}/README.md"
    _atomic_write_text(root / readme_rel, _projection_readme())
    written.append(readme_rel)
    _prune_obsolete_projections(root, set(written))
    written.sort()
    return written


def check_antigravity_projections(root: Path) -> list[str]:
    """Return drift messages (empty when committed projections match)."""
    errors: list[str] = []
    rendered = render_antigravity_projections(root)
    expected_readme = _projection_readme()
    readme_rel = f"{ANTIGRAVITY_PROJECTION_ROOT}/README.md"
    rendered[readme_rel] = expected_readme
    for rel, text in sorted(rendered.items()):
        path = root / rel
        if path.is_symlink():
            errors.append(f"symlink dest {rel}")
            continue
        if not path.is_file():
            errors.append(f"missing {rel}")
            continue
        actual = path.read_text(encoding="utf-8")
        if actual.replace("\r\n", "\n") != text.replace("\r\n", "\n"):
            errors.append(f"stale {rel}")
    skills_root = root / ANTIGRAVITY_PROJECTION_ROOT
    if skills_root.is_dir() and not skills_root.is_symlink():
        found = {
            path.relative_to(root).as_posix()
            for path in skills_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        extra = sorted(found - set(rendered))
        for rel in extra:
            errors.append(f"uncatalogued projection {rel}")
    return errors


_CATALOG_DOC_COPY = {
    "en": {
        "title": "# Skill parity catalog",
        "generated": (
            "Generated from [`skills/catalog.json`](../../skills/catalog.json). "
            "Do not hand-edit this table."
        ),
        "wave": (
            "**#70 Wave B/C:** Grok plugin playbooks (original 16 plus Wave B/C). "
            "Catalog rows are **not** live-verified and must not set `verified`. "
            "Dual-host routing consumes this catalog: Grok rules "
            "`<workflow_routing>` is generated from triggers/aliases."
        ),
        "ag": (
            "Antigravity files under "
            "`docs/parity/projections/antigravity/skills/` are **projections only**."
        ),
        "header": (
            "| ID | Kind | Classification | Owner | CLI twin | Status | Live | Continuation |"
        ),
        "sep": "|----|------|----------------|-------|----------|--------|------|--------------|",
        "host_h": "## Host-native protection",
        "host_p": (
            "These names cannot be Grok plugin directories and cannot silently "
            "replace host slash commands:"
        ),
        "host_alias": (
            "`plan` and `goal` exist only as **aliases** (`host_native_protected`) "
            "resolving to `omg-ralplan` / `omg-ultragoal`."
        ),
        "cont_h": "## Continuation authority",
        "cont_one": "Exactly one of: ",
        "cont_p": (
            "Conflicts resolve to `refuse`, `adopt_existing`, or `artifact_only` "
            "(`omg_cli.skills_catalog.resolve_continuation`). Cancel/using always "
            "adopt. Wiki/HUD/LSP/ask are artifact-only under an active loop."
        ),
    },
    "zh": {
        "title": "# Skill 对等目录",
        "generated": (
            "由 [`skills/catalog.json`](../../skills/catalog.json) 生成。请勿手改此表。"
        ),
        "wave": (
            "**#70 Wave B/C：** Grok 插件 playbook（原 16 个 + Wave B/C）。"
            "目录项 **不是** live-verified，禁止把 `verified` 设为 true。"
            "双宿主路由共用同一份 catalog：Grok 规则文件的 `<workflow_routing>` "
            "由 triggers/aliases 生成。"
        ),
        "ag": (
            "`docs/parity/projections/antigravity/skills/` 下的 Antigravity 文件"
            "**只是投影**。"
        ),
        "header": (
            "| ID | 类型 | 分类 | 所有者 | CLI 孪生 | 状态 | Live | 续跑 |"
        ),
        "sep": "|----|------|------|--------|----------|------|------|------|",
        "host_h": "## Host-native 保护",
        "host_p": "这些名字不能成为 Grok 插件目录，也不能静默替换宿主 slash 命令：",
        "host_alias": (
            "`plan` 与 `goal` 仅为 **别名**（`host_native_protected`），"
            "解析到 `omg-ralplan` / `omg-ultragoal`。"
        ),
        "cont_h": "## 续跑权",
        "cont_one": "同时只能有一个：",
        "cont_p": (
            "冲突解析为 `refuse`、`adopt_existing` 或 `artifact_only`。"
            "cancel/using 一律 adopt。循环进行中 wiki/HUD/LSP/ask 仅写制品。"
        ),
    },
    "zh-TW": {
        "title": "# Skill 對等目錄",
        "generated": (
            "由 [`skills/catalog.json`](../../skills/catalog.json) 生成。請勿手改此表。"
        ),
        "wave": (
            "**#70 Wave B/C：** Grok 外掛 playbook（原 16 個 + Wave B/C）。"
            "目錄列 **不是** live-verified，禁止把 `verified` 設為 true。"
            "雙宿主路由共用同一份 catalog：Grok 規則檔的 `<workflow_routing>` "
            "由 triggers/aliases 生成。"
        ),
        "ag": (
            "`docs/parity/projections/antigravity/skills/` 下的 Antigravity 檔案"
            "**只是投影**。"
        ),
        "header": (
            "| ID | 種類 | 分類 | 擁有者 | CLI 孿生 | 狀態 | Live | 續跑 |"
        ),
        "sep": "|----|------|------|--------|----------|------|------|------|",
        "host_h": "## Host-native 保護",
        "host_p": "這些名字不能成為 Grok 外掛目錄，也不能靜默取代宿主 slash 命令：",
        "host_alias": (
            "`plan` 與 `goal` 僅為 **別名**（`host_native_protected`），"
            "解析到 `omg-ralplan` / `omg-ultragoal`。"
        ),
        "cont_h": "## 續跑權",
        "cont_one": "同時只能有一個：",
        "cont_p": (
            "衝突解析為 `refuse`、`adopt_existing` 或 `artifact_only`。"
            "cancel/using 一律 adopt。循環進行中 wiki/HUD/LSP/ask 只寫製品。"
        ),
    },
}


def render_catalog_markdown(catalog: SkillsCatalog, locale: str = "en") -> str:
    """Generate a catalog table (docs/parity/skills-catalog*.md)."""
    copy = _CATALOG_DOC_COPY.get(locale)
    if copy is None:
        raise SkillsCatalogError(f"unsupported catalog locale {locale!r}")
    lines = [
        copy["title"],
        "",
        copy["generated"],
        "",
        copy["wave"],
        "",
        copy["ag"],
        "",
        copy["header"],
        copy["sep"],
    ]
    for record in catalog.skills:
        cli = record.cli_twin or "—"
        file_note = "plugin" if record.file else record.kind
        lines.append(
            f"| `{record.id}` | {file_note} | `{record.classification}` | "
            f"`{record.runtime_owner}` | `{cli}` | `{record.implementation_status}` | "
            f"`{record.live_verification}` | `{record.continuation}` |"
        )
    lines.extend(
        [
            "",
            copy["host_h"],
            "",
            copy["host_p"],
            "",
            ", ".join(f"`{name}`" for name in sorted(HOST_NATIVE_PROTECTED)),
            "",
            copy["host_alias"],
            "",
            copy["cont_h"],
            "",
            copy["cont_one"]
            + ", ".join(f"`{name}`" for name in sorted(CONTINUATION_OWNERS)),
            "",
            copy["cont_p"],
            "",
        ]
    )
    return "\n".join(lines) + "\n"


CATALOG_DOC_RELATIVE = "docs/parity/skills-catalog.md"
CATALOG_DOC_LOCALES: Mapping[str, str] = {
    "en": CATALOG_DOC_RELATIVE,
    "zh": "docs/parity/skills-catalog.zh.md",
    "zh-TW": "docs/parity/skills-catalog.zh-TW.md",
}


def write_catalog_markdown(root: Path) -> list[str]:
    catalog = load_skills_catalog(root, require_projections=False)
    written: list[str] = []
    for locale, rel in CATALOG_DOC_LOCALES.items():
        text = render_catalog_markdown(catalog, locale=locale)
        _atomic_write_text(root / rel, text)
        written.append(rel)
    return written


def check_catalog_markdown(root: Path) -> list[str]:
    catalog = load_skills_catalog(root, require_projections=False)
    errors: list[str] = []
    for locale, rel in CATALOG_DOC_LOCALES.items():
        expected = render_catalog_markdown(catalog, locale=locale)
        path = root / rel
        if path.is_symlink():
            errors.append(f"symlink dest {rel}")
            continue
        if not path.is_file():
            errors.append(f"missing {rel}")
            continue
        actual = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        if actual != expected.replace("\r\n", "\n"):
            errors.append(f"stale {rel}")
    return errors


__all__ = [
    "ALLOWED_CAPABILITY_MODES",
    "ANTIGRAVITY_PROJECTION_ROOT",
    "CATALOG_DOC_LOCALES",
    "CATALOG_DOC_RELATIVE",
    "CATALOG_RELATIVE",
    "CONTINUATION_OWNERS",
    "FORBIDDEN_CAPABILITY_MODES",
    "HOST_NATIVE_PROTECTED",
    "KIND",
    "PLUGIN_SKILL_COUNT",
    "ROUTING_PRIORITY_HEAD",
    "SCHEMA",
    "HostProjection",
    "SkillRecord",
    "SkillsCatalog",
    "SkillsCatalogError",
    "antigravity_projection_relative",
    "catalog_path",
    "check_antigravity_projections",
    "check_catalog_markdown",
    "inspect_skills_catalog",
    "is_informational_question",
    "list_plugin_skill_dirs",
    "load_skills_catalog",
    "plugin_root",
    "render_workflow_routing",
    "required_capability_diagnostics",
    "resolve_continuation",
    "resolve_skill_resource",
    "resolve_trigger",
    "render_antigravity_skill_md",
    "render_antigravity_projections",
    "render_catalog_markdown",
    "routing_order",
    "strip_markdown_frontmatter",
    "write_antigravity_projections",
    "write_catalog_markdown",
]
