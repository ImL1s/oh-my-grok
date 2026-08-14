"""Read-only plugin agent catalog (#71).

Single registry of ``agents/omg-*.md``. Dual-host routing (#131) must consume
this catalog — do not add a second plugin-agent registry.

Fail-closed: missing catalog, missing agent file, extra uncatalogued
``omg-*.md``, duplicate id, or ``capability_mode`` outside
``{read-only, read-write}``. Never ``execute`` / ``all``.

Not a routing runtime. Antigravity ``agent.md`` files are static projections
only — not an installed AG plugin and not live AG evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "omg-agents-catalog/v1"
KIND = "read_only_machine_catalog"
CATALOG_RELATIVE = "agents/catalog.json"
ANTIGRAVITY_PROJECTION_ROOT = "docs/parity/projections/antigravity/agents"

ALLOWED_CAPABILITY_MODES = frozenset({"read-only", "read-write"})
FORBIDDEN_CAPABILITY_MODES = frozenset({"execute", "all"})
ALLOWED_PERMISSION_MODES = frozenset({"default", "plan"})
ALLOWED_TIERS = frozenset(
    {"orchestrator", "implementer", "reviewer", "verifier", "planner"}
)
ALLOWED_SPAWN_POLICIES = frozenset({"parent", "leaf"})
READ_ONLY_TIERS = frozenset({"reviewer", "verifier", "planner"})
READ_WRITE_TIERS = frozenset({"orchestrator", "implementer"})
GROK_PROJECTION_KIND = "plugin_agent"
ANTIGRAVITY_PROJECTION_KIND = "agent_md_projection"
PROJECTION_BANNER_TITLE = "PROJECTION — not an installed Antigravity plugin"
PROJECTION_BANNER_NEEDLES = (
    "not an installed",
    "not live",
    "projection",
)

_AGENT_REQUIRED = (
    "id",
    "file",
    "capability_mode",
    "permission_mode",
    "tier",
    "spawn_policy",
    "projections",
)


class AgentsCatalogError(ValueError):
    """Fail-closed catalog load / validation error."""


@dataclass(frozen=True, slots=True)
class HostProjection:
    """One host projection target (Grok plugin agent or AG agent.md)."""

    kind: str
    path: str


@dataclass(frozen=True, slots=True)
class AgentRecord:
    """One catalogued plugin agent."""

    id: str
    file: str
    capability_mode: str
    permission_mode: str
    tier: str
    spawn_policy: str
    projections: Mapping[str, HostProjection]

    def to_inspect_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "file": self.file,
            "capability_mode": self.capability_mode,
            "permission_mode": self.permission_mode,
            "tier": self.tier,
            "spawn_policy": self.spawn_policy,
            "projections": {
                name: {"kind": proj.kind, "path": proj.path}
                for name, proj in self.projections.items()
            },
        }


@dataclass(frozen=True, slots=True)
class AgentsCatalog:
    """Loaded, validated catalog."""

    schema: str
    agents: tuple[AgentRecord, ...]

    def by_id(self) -> dict[str, AgentRecord]:
        return {agent.id: agent for agent in self.agents}


def plugin_root() -> Path:
    """Checkout / plugin root (parent of ``omg_cli``)."""
    return Path(__file__).resolve().parents[1]


def catalog_path(root: Path | None = None) -> Path:
    return (root if root is not None else plugin_root()) / CATALOG_RELATIVE


def antigravity_projection_relative(agent_id: str) -> str:
    return f"{ANTIGRAVITY_PROJECTION_ROOT}/{agent_id}/agent.md"


def list_plugin_agent_files(root: Path) -> list[Path]:
    """Sorted ``agents/omg-*.md`` files (non-recursive)."""
    agents_dir = root / "agents"
    if not agents_dir.is_dir():
        return []
    return sorted(
        path
        for path in agents_dir.glob("omg-*.md")
        if path.is_file() and not path.is_symlink()
    )


def _require_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentsCatalogError(f"{label} must be a JSON object")
    return value


def _require_str(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentsCatalogError(f"{label} must be a non-empty string")
    return value


def _require_keys(obj: Mapping[str, Any], required: tuple[str, ...], *, label: str) -> None:
    missing = [key for key in required if key not in obj]
    if missing:
        raise AgentsCatalogError(f"{label} missing keys: {', '.join(missing)}")


def _posix_relative(value: str, *, label: str) -> str:
    text = _require_str(value, label=label)
    if text.startswith("/") or text.startswith("\\") or ".." in Path(text).parts:
        raise AgentsCatalogError(f"{label} must be a relative posix path: {text!r}")
    if "\\" in text:
        raise AgentsCatalogError(f"{label} must use posix separators: {text!r}")
    return text


def _parse_projection(value: Any, *, label: str) -> HostProjection:
    obj = _require_object(value, label=label)
    _require_keys(obj, ("kind", "path"), label=label)
    return HostProjection(
        kind=_require_str(obj["kind"], label=f"{label}.kind"),
        path=_posix_relative(obj["path"], label=f"{label}.path"),
    )


def _parse_agent(value: Any, *, index: int) -> AgentRecord:
    label = f"agents[{index}]"
    obj = _require_object(value, label=label)
    _require_keys(obj, _AGENT_REQUIRED, label=label)
    agent_id = _require_str(obj["id"], label=f"{label}.id")
    if not agent_id.startswith("omg-") or agent_id != agent_id.lower():
        raise AgentsCatalogError(f"{label}.id must be lowercase omg-* , got {agent_id!r}")
    file_rel = _posix_relative(obj["file"], label=f"{label}.file")
    expected_file = f"agents/{agent_id}.md"
    if file_rel != expected_file:
        raise AgentsCatalogError(
            f"{label}.file must be {expected_file!r}, got {file_rel!r}"
        )
    capability_mode = _require_str(
        obj["capability_mode"], label=f"{label}.capability_mode"
    ).strip()
    lowered = capability_mode.lower()
    if lowered in FORBIDDEN_CAPABILITY_MODES:
        raise AgentsCatalogError(
            f"{label}.capability_mode {capability_mode!r} is forbidden "
            f"(never execute/all)"
        )
    if capability_mode not in ALLOWED_CAPABILITY_MODES:
        raise AgentsCatalogError(
            f"{label}.capability_mode must be one of "
            f"{sorted(ALLOWED_CAPABILITY_MODES)}, got {capability_mode!r}"
        )
    permission_mode = _require_str(
        obj["permission_mode"], label=f"{label}.permission_mode"
    )
    if permission_mode not in ALLOWED_PERMISSION_MODES:
        raise AgentsCatalogError(
            f"{label}.permission_mode must be one of "
            f"{sorted(ALLOWED_PERMISSION_MODES)}, got {permission_mode!r}"
        )
    tier = _require_str(obj["tier"], label=f"{label}.tier")
    if tier not in ALLOWED_TIERS:
        raise AgentsCatalogError(
            f"{label}.tier must be one of {sorted(ALLOWED_TIERS)}, got {tier!r}"
        )
    spawn_policy = _require_str(obj["spawn_policy"], label=f"{label}.spawn_policy")
    if spawn_policy not in ALLOWED_SPAWN_POLICIES:
        raise AgentsCatalogError(
            f"{label}.spawn_policy must be one of "
            f"{sorted(ALLOWED_SPAWN_POLICIES)}, got {spawn_policy!r}"
        )
    if tier == "orchestrator":
        if spawn_policy != "parent":
            raise AgentsCatalogError(
                f"{agent_id}: orchestrator spawn_policy must be parent"
            )
    elif spawn_policy != "leaf":
        raise AgentsCatalogError(f"{agent_id}: non-orchestrator spawn_policy must be leaf")
    if tier in READ_ONLY_TIERS and capability_mode != "read-only":
        raise AgentsCatalogError(
            f"{agent_id}: tier {tier} requires capability_mode=read-only"
        )
    if tier in READ_WRITE_TIERS and capability_mode != "read-write":
        raise AgentsCatalogError(
            f"{agent_id}: tier {tier} requires capability_mode=read-write"
        )
    projections_raw = _require_object(obj["projections"], label=f"{label}.projections")
    _require_keys(projections_raw, ("grok", "antigravity"), label=f"{label}.projections")
    grok = _parse_projection(projections_raw["grok"], label=f"{label}.projections.grok")
    ag = _parse_projection(
        projections_raw["antigravity"], label=f"{label}.projections.antigravity"
    )
    if grok.kind != GROK_PROJECTION_KIND:
        raise AgentsCatalogError(
            f"{agent_id}: grok projection kind must be {GROK_PROJECTION_KIND!r}"
        )
    if grok.path != file_rel:
        raise AgentsCatalogError(
            f"{agent_id}: grok projection path must equal file {file_rel!r}"
        )
    if ag.kind != ANTIGRAVITY_PROJECTION_KIND:
        raise AgentsCatalogError(
            f"{agent_id}: antigravity projection kind must be "
            f"{ANTIGRAVITY_PROJECTION_KIND!r}"
        )
    expected_ag = antigravity_projection_relative(agent_id)
    if ag.path != expected_ag:
        raise AgentsCatalogError(
            f"{agent_id}: antigravity projection path must be {expected_ag!r}"
        )
    extra_hosts = sorted(set(projections_raw) - {"grok", "antigravity"})
    if extra_hosts:
        raise AgentsCatalogError(
            f"{agent_id}: unknown projection hosts: {', '.join(extra_hosts)}"
        )
    return AgentRecord(
        id=agent_id,
        file=file_rel,
        capability_mode=capability_mode,
        permission_mode=permission_mode,
        tier=tier,
        spawn_policy=spawn_policy,
        projections={"grok": grok, "antigravity": ag},
    )


def _load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentsCatalogError(f"cannot read catalog: {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentsCatalogError(f"catalog is not valid JSON: {path}: {exc}") from exc


def load_agents_catalog(
    root: Path | None = None,
    *,
    require_projections: bool = True,
) -> AgentsCatalog:
    """Load and fail-closed validate the plugin agent catalog."""
    base = Path(root) if root is not None else plugin_root()
    path = catalog_path(base)
    if not path.is_file():
        raise AgentsCatalogError(f"missing agent catalog: {CATALOG_RELATIVE}")
    raw = _require_object(_load_json(path), label="catalog")
    schema = _require_str(raw.get("schema"), label="schema")
    if schema != SCHEMA:
        raise AgentsCatalogError(f"unsupported catalog schema {schema!r}")
    kind = _require_str(raw.get("kind"), label="kind")
    if kind != KIND:
        raise AgentsCatalogError(f"catalog kind must be {KIND!r}, got {kind!r}")
    agents_raw = raw.get("agents")
    if not isinstance(agents_raw, list) or not agents_raw:
        raise AgentsCatalogError("catalog.agents must be a non-empty array")
    seen: dict[str, int] = {}
    records: list[AgentRecord] = []
    for index, item in enumerate(agents_raw):
        record = _parse_agent(item, index=index)
        if record.id in seen:
            raise AgentsCatalogError(
                f"duplicate agent id {record.id!r} at indexes "
                f"{seen[record.id]} and {index}"
            )
        seen[record.id] = index
        records.append(record)

    disk_files = list_plugin_agent_files(base)
    disk_ids = {path.stem for path in disk_files}
    catalog_ids = {record.id for record in records}
    missing = sorted(catalog_ids - disk_ids)
    extra = sorted(disk_ids - catalog_ids)
    if missing:
        raise AgentsCatalogError(
            "missing agent file for catalog id(s): " + ", ".join(missing)
        )
    if extra:
        raise AgentsCatalogError(
            "uncatalogued agents/omg-*.md on disk: " + ", ".join(extra)
        )
    for record in records:
        agent_path = base / record.file
        if not agent_path.is_file() or agent_path.is_symlink():
            raise AgentsCatalogError(f"missing agent: {record.file}")
        if require_projections:
            rel = record.projections["antigravity"].path
            proj = base / rel
            if not proj.is_file() or proj.is_symlink():
                raise AgentsCatalogError(f"missing antigravity projection: {rel}")
    records.sort(key=lambda item: item.id)
    return AgentsCatalog(schema=schema, agents=tuple(records))


def inspect_agents_catalog(root: Path | None = None) -> dict[str, Any]:
    """Inspect payload for ``omg capabilities`` (never sets verified)."""
    base = Path(root) if root is not None else plugin_root()
    try:
        catalog = load_agents_catalog(base)
    except AgentsCatalogError as exc:
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
                "read-only plugin catalog; not routing runtime; "
                "AG files are projections only"
            ),
        }
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
        "agent_count": len(catalog.agents),
        "agents": [record.to_inspect_row() for record in catalog.agents],
        "note": (
            "read-only plugin catalog; not routing runtime; "
            "Antigravity agent.md files are static projections only "
            "(not an installed AG plugin, not live AG evidence)"
        ),
    }


def strip_markdown_frontmatter(text: str) -> str:
    """Return markdown body after a leading YAML frontmatter fence."""
    if not text.startswith("---"):
        return text.strip()
    parts = text.split("---", 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return text.strip()


def _yaml_bool(value: bool) -> str:
    return "true" if value else "false"


def render_antigravity_agent_md(record: AgentRecord, source_text: str) -> str:
    """Render a static AG ``agent.md`` projection (not an install)."""
    body = strip_markdown_frontmatter(source_text)
    is_parent = record.spawn_policy == "parent"
    frontmatter = "\n".join(
        [
            "---",
            f"name: {record.id}",
            f"description: OMG {record.tier} ({record.capability_mode}, "
            f"spawn={record.spawn_policy})",
            f"mainAgent: {_yaml_bool(is_parent)}",
            f"subagent: {_yaml_bool(not is_parent)}",
            "hidden: false",
            "inheritMcp: false",
            "commandExecutionPolicy: deny",
            f"omg_capability_mode: {record.capability_mode}",
            f"omg_permission_mode: {record.permission_mode}",
            f"omg_tier: {record.tier}",
            f"omg_spawn_policy: {record.spawn_policy}",
            f"omg_source_agent: {record.file}",
            "omg_projection: true",
            "---",
            "",
        ]
    )
    banner = "\n".join(
        [
            f"# {PROJECTION_BANNER_TITLE}",
            "",
            "This file is a static parity projection of the Grok plugin agent",
            f"`{record.file}`. It is not an installed Antigravity plugin,",
            "not live AG evidence, and does not mean `agy` install or",
            "`/agents` discovery works. Dual-host routing (#131) is not this file.",
            "",
            f"- Catalog: `{CATALOG_RELATIVE}`",
            f"- capability_mode: `{record.capability_mode}` (never `execute`/`all`)",
            f"- spawn_policy: `{record.spawn_policy}` (depth=1 leaf vs parent)",
            "",
            body,
            "",
        ]
    )
    return frontmatter + banner


def render_antigravity_projections(
    root: Path,
    *,
    catalog: AgentsCatalog | None = None,
) -> dict[str, str]:
    """Map relative projection paths to rendered markdown."""
    loaded = catalog or load_agents_catalog(root, require_projections=False)
    out: dict[str, str] = {}
    for record in loaded.agents:
        source = root / record.file
        if not source.is_file():
            raise AgentsCatalogError(f"missing agent: {record.file}")
        rel = record.projections["antigravity"].path
        out[rel] = render_antigravity_agent_md(
            record, source.read_text(encoding="utf-8")
        )
    return out


def write_antigravity_projections(root: Path) -> list[str]:
    """Write committed AG projections. Returns relative paths written."""
    rendered = render_antigravity_projections(root)
    written: list[str] = []
    for rel, text in rendered.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(rel)
    readme = root / ANTIGRAVITY_PROJECTION_ROOT / "README.md"
    readme.write_text(_projection_readme(), encoding="utf-8", newline="\n")
    written.append(f"{ANTIGRAVITY_PROJECTION_ROOT}/README.md")
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
        if not path.is_file():
            errors.append(f"missing {rel}")
            continue
        actual = path.read_text(encoding="utf-8")
        if actual.replace("\r\n", "\n") != text.replace("\r\n", "\n"):
            errors.append(f"stale {rel}")
    agents_root = root / ANTIGRAVITY_PROJECTION_ROOT
    if agents_root.is_dir():
        found = {
            path.relative_to(root).as_posix()
            for path in agents_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        extra = sorted(found - set(rendered))
        for rel in extra:
            errors.append(f"uncatalogued projection {rel}")
    return errors


def _projection_readme() -> str:
    return """# Antigravity agent.md projections

**Status:** static parity projection for
[#71](https://github.com/ImL1s/oh-my-grok/issues/71).

These files are **not**:

- an installed Antigravity plugin
- live AG evidence
- proof that `agy` install or `/agents` discovery works
- dual-host routing runtime ([#131](https://github.com/ImL1s/oh-my-grok/issues/131))

They are generated from `agents/catalog.json` plus `agents/omg-*.md` by
`scripts/generate_antigravity_agent_projections.py`. Frontmatter maps OMG
spawn/capability floors onto documented AG keys (`mainAgent`, `subagent`,
`commandExecutionPolicy`). OMG does **not** claim Antigravity honors those
fields at runtime.

Regenerate:

```bash
python scripts/generate_antigravity_agent_projections.py
python scripts/generate_antigravity_agent_projections.py --check
```
"""


__all__ = [
    "ALLOWED_CAPABILITY_MODES",
    "ANTIGRAVITY_PROJECTION_ROOT",
    "AgentRecord",
    "AgentsCatalog",
    "AgentsCatalogError",
    "CATALOG_RELATIVE",
    "FORBIDDEN_CAPABILITY_MODES",
    "HostProjection",
    "KIND",
    "SCHEMA",
    "antigravity_projection_relative",
    "catalog_path",
    "check_antigravity_projections",
    "inspect_agents_catalog",
    "list_plugin_agent_files",
    "load_agents_catalog",
    "plugin_root",
    "render_antigravity_agent_md",
    "render_antigravity_projections",
    "strip_markdown_frontmatter",
    "write_antigravity_projections",
]
