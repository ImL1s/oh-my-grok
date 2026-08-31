"""Read-only plugin agent catalog (#71).

Single registry of ``agents/omg-*.md``. YAML ``agents/catalog.yaml`` is the
editable source of truth; committed ``agents/catalog.json`` is generated for
the fail-closed loader. Dual-host routing (#131) must consume this catalog —
do not add a second plugin-agent registry.

Fail-closed: missing catalog, missing agent file, extra uncatalogued
``omg-*.md``, duplicate id, ``capability_mode`` outside
``{read-only, read-write}``, or agent frontmatter that omits, aliases, or
disagrees with catalog ``capabilityMode`` / ``permissionMode``. Grok agent
files must use those camelCase keys; snake_case aliases are rejected so a
read-only/plan agent cannot silently fall back to host defaults. Agent
markdown is opened with ``O_NOFOLLOW|O_NONBLOCK`` (POSIX) or Windows
CreateFileW/NtCreateFile ``FILE_FLAG_OPEN_REPARSE_POINT`` and read through
that pinned handle (fail-closed without a no-follow backend). Never ``execute`` /
``all``. Reviewer / verifier / planner tiers cannot receive ``read-write``.

Category routing (``resolve_category``) is deterministic and inspectable.
Antigravity ``agent.md`` files are static projections only — not an installed
AG plugin and not live AG evidence.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from omg_cli.catalog_yaml import CatalogYamlError, parse_yaml
from omg_cli.win32_nofollow import (
    Win32NofollowError,
    read_relative_regular,
    windows_nofollow_ready,
    write_path_regular,
)

SCHEMA = "omg-agents-catalog/v1"
KIND = "read_only_machine_catalog"
CATALOG_RELATIVE = "agents/catalog.json"
YAML_RELATIVE = "agents/catalog.yaml"
ANTIGRAVITY_PROJECTION_ROOT = "docs/parity/projections/antigravity/agents"

# Antigravity CLI custom agents are discovered directly from ``agents/*.md``
# when this repository is installed as a plugin. Unlike Grok, Agy grants only
# the tools named in frontmatter. Keep these profiles derived from the canonical
# capability/category catalog so read-only roles cannot inherit mutation tools.
ANTIGRAVITY_READ_TOOLS = (
    "find_by_name",
    "grep_search",
    "view_file",
    "list_dir",
    "read_url_content",
    "search_web",
)
ANTIGRAVITY_WRITE_TOOLS = (
    "multi_replace_file_content",
    "replace_file_content",
    "write_to_file",
    "run_command",
    "notebook_edit",
)
ANTIGRAVITY_VISUAL_WRITE_TOOLS = ("generate_image",)

ALLOWED_CAPABILITY_MODES = frozenset({"read-only", "read-write"})
FORBIDDEN_CAPABILITY_MODES = frozenset({"execute", "all"})
ALLOWED_PERMISSION_MODES = frozenset({"default", "plan"})
ALLOWED_TIERS = frozenset(
    {"orchestrator", "implementer", "reviewer", "verifier", "planner"}
)
ALLOWED_SPAWN_POLICIES = frozenset({"parent", "leaf"})
READ_ONLY_TIERS = frozenset({"reviewer", "verifier", "planner"})
READ_WRITE_TIERS = frozenset({"orchestrator", "implementer"})
ALLOWED_CATEGORIES = frozenset(
    {
        "quick",
        "deep",
        "ultrabrain",
        "visual-engineering",
        "research",
        "review",
    }
)
# OmO-style discipline routing. All six categories default to read-only.
# Write agents appear only when required_mode='read-write'.
CATEGORY_CANDIDATES: Mapping[str, tuple[str, ...]] = {
    "quick": ("omg-explore", "omg-analyst", "omg-planner", "omg-architect"),
    "deep": ("omg-planner", "omg-architect", "omg-analyst", "omg-explore"),
    "ultrabrain": ("omg-scientist", "omg-planner", "omg-architect", "omg-analyst"),
    "visual-engineering": (
        "omg-vision",
        "omg-code-reviewer",
        "omg-critic",
        "omg-designer",
    ),
    "research": (
        "omg-document-specialist",
        "omg-scientist",
        "omg-analyst",
        "omg-tracer",
        "omg-planner",
    ),
    "review": (
        "omg-code-reviewer",
        "omg-critic",
        "omg-security-reviewer",
        "omg-verifier",
    ),
}
CATEGORY_DEFAULT_MODE = "read-only"
HOST_NATIVE_PROFILES = frozenset(
    {"explore", "plan", "general-purpose", "general_purpose"}
)
GROK_PROJECTION_KIND = "plugin_agent"
ANTIGRAVITY_PROJECTION_KIND = "agent_md_projection"
MAX_AGENT_FILE_BYTES = 1 * 1024 * 1024
MAX_HANDOFF_TASK_CHARS = 500
MAX_HANDOFF_ITEMS = 12
MAX_HANDOFF_ITEM_CHARS = 200
_NOFOLLOW_ERRNOS = {errno.ELOOP, getattr(errno, "EMLINK", -1), errno.EINVAL}
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
_AGENT_OPTIONAL = ("aliases", "categories", "profile")
_ALIAS_RE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


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
    aliases: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    profile: str = ""

    def to_inspect_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "file": self.file,
            "capability_mode": self.capability_mode,
            "permission_mode": self.permission_mode,
            "tier": self.tier,
            "spawn_policy": self.spawn_policy,
            "aliases": list(self.aliases),
            "categories": list(self.categories),
            "profile": self.profile,
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


def yaml_catalog_path(root: Path | None = None) -> Path:
    return (root if root is not None else plugin_root()) / YAML_RELATIVE


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
    unknown = sorted(set(obj) - set(_AGENT_REQUIRED) - set(_AGENT_OPTIONAL))
    if unknown:
        raise AgentsCatalogError(
            f"{agent_id}: unknown catalog keys: {', '.join(unknown)}"
        )
    aliases = _parse_string_tuple(
        obj.get("aliases"), label=f"{label}.aliases", agent_id=agent_id
    )
    categories = _parse_string_tuple(
        obj.get("categories"), label=f"{label}.categories", agent_id=agent_id
    )
    for category in categories:
        if category not in ALLOWED_CATEGORIES:
            raise AgentsCatalogError(
                f"{agent_id}: category must be one of "
                f"{sorted(ALLOWED_CATEGORIES)}, got {category!r}"
            )
    profile = ""
    if "profile" in obj and obj["profile"] is not None and obj["profile"] != "":
        profile = _require_str(obj["profile"], label=f"{label}.profile").strip()
        if profile != profile.lower() or any(ch not in _ALIAS_RE_CHARS for ch in profile):
            raise AgentsCatalogError(
                f"{agent_id}: profile must be lowercase [a-z0-9-], got {profile!r}"
            )
    for alias in aliases:
        _require_alias_token(alias, agent_id=agent_id, label=f"{label}.aliases")
        if alias == agent_id:
            raise AgentsCatalogError(f"{agent_id}: alias must not equal id")
    return AgentRecord(
        id=agent_id,
        file=file_rel,
        capability_mode=capability_mode,
        permission_mode=permission_mode,
        tier=tier,
        spawn_policy=spawn_policy,
        projections={"grok": grok, "antigravity": ag},
        aliases=aliases,
        categories=categories,
        profile=profile,
    )


def _parse_string_tuple(value: Any, *, label: str, agent_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise AgentsCatalogError(f"{label} must be an array")
    out: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        token = _require_str(item, label=f"{label}[{index}]")
        if token in seen:
            raise AgentsCatalogError(f"{agent_id}: duplicate {label} value {token!r}")
        seen.add(token)
        out.append(token)
    return tuple(out)


def _require_alias_token(alias: str, *, agent_id: str, label: str) -> None:
    if alias != alias.lower() or not alias or alias[0] == "-" or alias[-1] == "-":
        raise AgentsCatalogError(
            f"{agent_id}: {label} must be lowercase [a-z0-9-], got {alias!r}"
        )
    if any(ch not in _ALIAS_RE_CHARS for ch in alias):
        raise AgentsCatalogError(
            f"{agent_id}: {label} must be lowercase [a-z0-9-], got {alias!r}"
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


def _posix_nofollow_ready() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )


def _windows_nofollow_ready() -> bool:
    return windows_nofollow_ready()


def _read_plugin_regular_text_windows(root: Path, relative: str) -> str:
    rel = _posix_relative(relative, label="agent file")
    parts = rel.split("/")
    try:
        body, _mode = read_relative_regular(
            root, parts, max_bytes=MAX_AGENT_FILE_BYTES
        )
    except Win32NofollowError as exc:
        if exc.kind in {"symlink", "missing", "not_regular"}:
            raise AgentsCatalogError(f"missing agent: {relative}") from exc
        if exc.kind == "size":
            raise AgentsCatalogError(
                f"agent file exceeds size bound: {relative}"
            ) from exc
        if exc.kind == "changed":
            raise AgentsCatalogError(
                f"agent file changed while reading: {relative}"
            ) from exc
        raise AgentsCatalogError(f"cannot read agent: {relative}") from exc
    try:
        return body.decode("utf-8")
    except UnicodeError as exc:
        raise AgentsCatalogError(f"cannot read agent: {relative}") from exc


def _read_plugin_regular_text(root: Path, relative: str) -> str:
    """Read a plugin-relative regular file without following a swapped symlink."""

    rel = _posix_relative(relative, label="agent file")
    if not _posix_nofollow_ready():
        if _windows_nofollow_ready():
            return _read_plugin_regular_text_windows(root, relative)
        raise AgentsCatalogError(
            "agent catalog load requires POSIX O_NOFOLLOW/dir_fd or Windows no-follow open"
        )
    parts = rel.split("/")
    if not parts or any(not part for part in parts):
        raise AgentsCatalogError(f"missing agent: {relative}")
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise AgentsCatalogError("cannot open plugin root") from exc
    current: int | None = None
    descriptor: int | None = None
    try:
        current = os.dup(root_fd)
        for component in parts[:-1]:
            try:
                nxt = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current,
                )
            except OSError as exc:
                if exc.errno in _NOFOLLOW_ERRNOS or exc.errno == errno.ELOOP:
                    raise AgentsCatalogError(f"missing agent: {relative}") from exc
                raise AgentsCatalogError(f"cannot read agent: {relative}") from exc
            os.close(current)
            current = nxt
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(parts[-1], flags, dir_fd=current)
        except OSError as exc:
            if (
                exc.errno in _NOFOLLOW_ERRNOS
                or exc.errno
                in {errno.ELOOP, errno.ENOENT, errno.ENXIO, errno.EAGAIN}
            ):
                raise AgentsCatalogError(f"missing agent: {relative}") from exc
            raise AgentsCatalogError(f"cannot read agent: {relative}") from exc
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AgentsCatalogError(f"missing agent: {relative}")
        if before.st_size > MAX_AGENT_FILE_BYTES:
            raise AgentsCatalogError(f"agent file exceeds size bound: {relative}")
        remaining = min(int(before.st_size) + 1, MAX_AGENT_FILE_BYTES + 1)
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) > MAX_AGENT_FILE_BYTES:
            raise AgentsCatalogError(f"agent file exceeds size bound: {relative}")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ) or after.st_size != len(body):
            raise AgentsCatalogError(f"agent file changed while reading: {relative}")
        try:
            return body.decode("utf-8")
        except UnicodeError as exc:
            raise AgentsCatalogError(f"cannot read agent: {relative}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if current is not None:
            os.close(current)
        os.close(root_fd)


def _read_agent_text_generation(root: Path, relative: str) -> str:
    """Generator/Windows read: reject symlinks, do not follow them."""
    rel = _posix_relative(relative, label="agent file")
    path = root / rel
    if path.is_symlink() or not path.is_file():
        raise AgentsCatalogError(f"missing agent: {relative}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AgentsCatalogError(f"cannot read agent: {relative}") from exc
    if len(data) > MAX_AGENT_FILE_BYTES:
        raise AgentsCatalogError(f"agent file exceeds size bound: {relative}")
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise AgentsCatalogError(f"cannot read agent: {relative}") from exc


def load_agents_catalog(
    root: Path | None = None,
    *,
    require_projections: bool = True,
    pin_files: bool = True,
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

    alias_owners: dict[str, str] = {}
    for record in records:
        owner = alias_owners.get(record.id)
        if owner is not None and owner != record.id:
            raise AgentsCatalogError(
                f"canonical id {record.id!r} collides with alias of {owner}"
            )
        alias_owners[record.id] = record.id
        for alias in record.aliases:
            if alias in alias_owners:
                raise AgentsCatalogError(
                    f"duplicate alias {alias!r} for {record.id} and {alias_owners[alias]}"
                )
            alias_owners[alias] = record.id

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
        if pin_files:
            text = _read_plugin_regular_text(base, record.file)
        else:
            text = _read_agent_text_generation(base, record.file)
        _require_frontmatter_matches_catalog(text, record)
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
                "read-only plugin catalog; YAML source plus generated JSON; "
                "AG files are projections only (not live AG evidence)"
            ),
        }
    category_routing = []
    for category in sorted(ALLOWED_CATEGORIES):
        try:
            category_routing.append(
                resolve_category(category, catalog=catalog)
            )
        except AgentsCatalogError as exc:
            category_routing.append(
                {
                    "category": category,
                    "error": str(exc),
                    "role_id": None,
                    "capability_mode": None,
                }
            )
    enforcement = []
    for record in catalog.agents:
        assert_agent_capability(record.id, record.capability_mode, catalog)
        blocked = False
        if record.tier in READ_ONLY_TIERS:
            try:
                assert_agent_capability(record.id, "read-write", catalog)
            except AgentsCatalogError:
                blocked = True
        enforcement.append(
            {
                "id": record.id,
                "tier": record.tier,
                "capability_mode": record.capability_mode,
                "read_write_blocked": blocked if record.tier in READ_ONLY_TIERS else None,
            }
        )
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
        "aliases": {
            alias: record.id
            for record in catalog.agents
            for alias in record.aliases
        },
        "category_routing": category_routing,
        "capability_enforcement": enforcement,
        "yaml_source": YAML_RELATIVE,
        "note": (
            "read-only plugin catalog; YAML source generates JSON; "
            "category routing is inspectable; "
            "Antigravity agent.md files are static projections only "
            "(not an installed AG plugin, not live AG evidence)"
        ),
    }


def lookup_agent(
    agent_id: str, catalog: AgentsCatalog
) -> AgentRecord | None:
    """Resolve a catalog id or alias. Unknown names return None."""
    token = (agent_id or "").strip().lower()
    if not token:
        return None
    by_id = catalog.by_id()
    if token in by_id:
        return by_id[token]
    for record in catalog.agents:
        if token in {alias.lower() for alias in record.aliases}:
            return record
    return None


def resolve_agent(agent_id: str, catalog: AgentsCatalog) -> AgentRecord:
    """Fail-closed lookup of a catalog id or alias."""
    record = lookup_agent(agent_id, catalog)
    if record is None:
        raise AgentsCatalogError(f"unknown agent {agent_id!r}")
    return record


def assert_agent_capability(
    agent_id: str,
    requested_mode: str,
    catalog: AgentsCatalog,
) -> AgentRecord:
    """Fail-closed capability floor for a catalog agent (or alias).

    Reviewer / verifier / planner cannot receive ``read-write``. Never
    ``execute`` / ``all``. Unknown names raise; callers that must fail-open
    (PreToolUse) should use :func:`lookup_agent` first.
    """
    record = resolve_agent(agent_id, catalog)
    mode = (requested_mode or "").strip().replace("_", "-").lower()
    if mode in FORBIDDEN_CAPABILITY_MODES:
        raise AgentsCatalogError(
            f"{record.id}: capability_mode {requested_mode!r} is forbidden "
            "(never execute/all)"
        )
    if mode not in ALLOWED_CAPABILITY_MODES:
        raise AgentsCatalogError(
            f"{record.id}: capability_mode must be one of "
            f"{sorted(ALLOWED_CAPABILITY_MODES)}, got {requested_mode!r}"
        )
    if record.tier in READ_ONLY_TIERS and mode != "read-only":
        raise AgentsCatalogError(
            f"{record.id}: tier {record.tier} cannot receive {mode}"
        )
    if record.capability_mode == "read-only" and mode == "read-write":
        raise AgentsCatalogError(
            f"{record.id}: catalog capability_mode=read-only cannot receive "
            "read-write"
        )
    return record


def resolve_category(
    category: str,
    *,
    required_mode: str | None = None,
    available: Iterable[str] | None = None,
    catalog: AgentsCatalog | None = None,
) -> dict[str, Any]:
    """Deterministic OmO-style category routing with inspectable evidence.

    Never silently selects a write agent when *required_mode* is ``read-only``
    or omitted (all six categories default to read-only).
    """
    name = (category or "").strip().lower()
    if name not in ALLOWED_CATEGORIES:
        raise AgentsCatalogError(
            f"unknown category {category!r}; expected one of "
            f"{sorted(ALLOWED_CATEGORIES)}"
        )
    mode = None if required_mode is None else str(required_mode).strip().replace(
        "_", "-"
    ).lower()
    if mode is not None:
        if mode in FORBIDDEN_CAPABILITY_MODES:
            raise AgentsCatalogError(
                f"category {name}: capability_mode {required_mode!r} is forbidden"
            )
        if mode not in ALLOWED_CAPABILITY_MODES:
            raise AgentsCatalogError(
                f"category {name}: required_mode must be read-only or read-write"
            )
    wanted = CATEGORY_DEFAULT_MODE if mode is None else mode
    loaded = catalog
    if loaded is None:
        loaded = load_agents_catalog(require_projections=False)
    by_id = loaded.by_id()
    if available is None:
        present = set(by_id)
    else:
        present = {str(item).strip() for item in available if str(item).strip()}
    fallbacks: list[str] = []
    for candidate in CATEGORY_CANDIDATES[name]:
        record = by_id.get(candidate)
        if record is None or candidate not in present:
            fallbacks.append(candidate)
            continue
        if wanted == "read-only" and record.capability_mode != "read-only":
            fallbacks.append(candidate)
            continue
        if wanted == "read-write" and record.capability_mode != "read-write":
            fallbacks.append(candidate)
            continue
        if record.tier in READ_ONLY_TIERS and wanted == "read-write":
            fallbacks.append(candidate)
            continue
        reason = (
            f"category {name} selected {record.id} "
            f"(capability_mode={record.capability_mode}"
        )
        if fallbacks:
            reason += f"; skipped unavailable/incompatible: {', '.join(fallbacks)}"
        reason += ")"
        return {
            "category": name,
            "role_id": record.id,
            "profile": record.profile or name,
            "capability_mode": record.capability_mode,
            "reason": reason,
            "fallbacks": list(fallbacks),
        }
    raise AgentsCatalogError(
        f"category {name}: no compatible agent for required_mode={wanted!r} "
        f"(skipped: {', '.join(fallbacks) or 'none'})"
    )


def _clip_handoff_text(value: str, *, limit: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _clip_handoff_items(items: Sequence[Any]) -> list[str]:
    out: list[str] = []
    for item in items[:MAX_HANDOFF_ITEMS]:
        clipped = _clip_handoff_text(str(item), limit=MAX_HANDOFF_ITEM_CHARS)
        if clipped:
            out.append(clipped)
    return out


def _result_schema_for(record: AgentRecord) -> str:
    if record.tier == "reviewer":
        return (
            '{"verdict":"APPROVE|REQUEST_CHANGES","findings":['
            '{"severity":"blocker|major|minor","file":"...","line":1,'
            '"evidence":"..."}]}'
        )
    if record.tier == "verifier":
        return (
            '{"verdict":"APPROVE|REQUEST_CHANGES|FAILED","evidence":['
            '{"criterion":"...","result":"pass|fail|untested"}]}'
        )
    if record.tier == "planner":
        return (
            '{"facts":[],"risks":[],"recommendation":"...",'
            '"open_questions":[]}'
        )
    if record.tier == "orchestrator":
        return (
            '{"slices":[],"spawns":[],"evidence":[],"blockers":[]}'
        )
    return (
        '{"summary":"...","files":[],"verification":[],"blockers":[]}'
    )


def render_handoff(
    agent_id: str,
    *,
    task: str,
    artifacts: Sequence[str] = (),
    decisions: Sequence[str] = (),
    catalog: AgentsCatalog | None = None,
    record: AgentRecord | None = None,
) -> str:
    """Compact spawn prompt: mission + floors, never full leader history."""
    loaded = record
    if loaded is None:
        cat = catalog or load_agents_catalog(require_projections=False)
        loaded = resolve_agent(agent_id, cat)
    mission = _clip_handoff_text(task, limit=MAX_HANDOFF_TASK_CHARS)
    artifact_lines = _clip_handoff_items(artifacts)
    decision_lines = _clip_handoff_items(decisions)
    independence = (
        "You cannot self-approve, self-stamp verified, or mutate "
        "`.omg/state/` passes/verified. Parent / `omg` CLI owns gates."
        if loaded.tier in READ_ONLY_TIERS or loaded.id == "omg-code-simplifier"
        else "Do not stamp `.omg/state/` passes/verified; parent / `omg` CLI owns gates."
    )
    art = "\n".join(f"- {item}" for item in artifact_lines) or "- (none)"
    dec = "\n".join(f"- {item}" for item in decision_lines) or "- (none)"
    return "\n".join(
        [
            "## Bounded context handoff",
            "",
            "Do **not** paste the full leader conversation or transcript.",
            "Ids, paths, and decisions only.",
            "",
            f"- Agent: `{loaded.id}`",
            f"- capability_mode: `{loaded.capability_mode}` (never `execute`/`all`)",
            f"- permission_mode: `{loaded.permission_mode}`",
            f"- tier: `{loaded.tier}`",
            f"- spawn_policy: `{loaded.spawn_policy}` (depth=1 unless parent)",
            f"- Mission: {mission or '(parent supplies a bounded task)'}",
            "",
            "### Artifacts (paths only)",
            art,
            "",
            "### Decisions already taken",
            dec,
            "",
            "### Result schema",
            "```json",
            _result_schema_for(loaded),
            "```",
            "",
            "### Independence",
            independence,
            "",
        ]
    )


def catalog_document_from_records(catalog: AgentsCatalog) -> dict[str, Any]:
    """Canonical YAML/JSON document (insertion-ordered)."""
    agents: list[dict[str, Any]] = []
    for record in catalog.agents:
        row: dict[str, Any] = {
            "id": record.id,
            "file": record.file,
            "capability_mode": record.capability_mode,
            "permission_mode": record.permission_mode,
            "tier": record.tier,
            "spawn_policy": record.spawn_policy,
        }
        if record.aliases:
            row["aliases"] = list(record.aliases)
        if record.categories:
            row["categories"] = list(record.categories)
        if record.profile:
            row["profile"] = record.profile
        row["projections"] = {
            "grok": {
                "kind": record.projections["grok"].kind,
                "path": record.projections["grok"].path,
            },
            "antigravity": {
                "kind": record.projections["antigravity"].kind,
                "path": record.projections["antigravity"].path,
            },
        }
        agents.append(row)
    return {
        "schema": SCHEMA,
        "kind": KIND,
        "source": YAML_RELATIVE,
        "note": (
            "Single plugin-agent registry for oh-my-grok. Dual-host routing "
            "(#131) must consume this catalog — do not add a second registry. "
            "Not live AG evidence. Antigravity agent.md files are static "
            "projections only."
        ),
        "allowed_capability_modes": ["read-only", "read-write"],
        "forbidden_capability_modes": ["execute", "all"],
        "agents": agents,
    }


def canonical_catalog_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def load_yaml_catalog_document(root: Path) -> dict[str, Any]:
    path = yaml_catalog_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AgentsCatalogError(f"cannot read YAML catalog: {path}: {exc}") from exc
    try:
        raw = parse_yaml(text)
    except CatalogYamlError as exc:
        raise AgentsCatalogError(f"invalid YAML catalog: {exc}") from exc
    if not isinstance(raw, dict):
        raise AgentsCatalogError("YAML catalog must be a mapping")
    return raw


def json_document_from_yaml(root: Path) -> dict[str, Any]:
    """Parse YAML and return the canonical JSON document shape."""
    raw = load_yaml_catalog_document(root)
    schema = _require_str(raw.get("schema"), label="schema")
    if schema != SCHEMA:
        raise AgentsCatalogError(f"unsupported catalog schema {schema!r}")
    kind = _require_str(raw.get("kind"), label="kind")
    if kind != KIND:
        raise AgentsCatalogError(f"catalog kind must be {KIND!r}, got {kind!r}")
    agents_raw = raw.get("agents")
    if not isinstance(agents_raw, list) or not agents_raw:
        raise AgentsCatalogError("catalog.agents must be a non-empty array")
    parsed = [_parse_agent(item, index=index) for index, item in enumerate(agents_raw)]
    parsed.sort(key=lambda item: item.id)
    catalog = AgentsCatalog(schema=schema, agents=tuple(parsed))
    document = catalog_document_from_records(catalog)
    if "note" in raw and isinstance(raw["note"], str) and raw["note"].strip():
        document["note"] = raw["note"].strip()
    return document


def check_catalog_yaml(root: Path) -> list[str]:
    """Return drift messages when YAML and generated JSON disagree."""
    errors: list[str] = []
    yaml_path = yaml_catalog_path(root)
    json_path = catalog_path(root)
    if not yaml_path.is_file():
        errors.append(f"missing {YAML_RELATIVE}")
        return errors
    try:
        expected = canonical_catalog_json(json_document_from_yaml(root))
    except AgentsCatalogError as exc:
        errors.append(str(exc))
        return errors
    if not json_path.is_file():
        errors.append(f"missing {CATALOG_RELATIVE}")
        return errors
    actual = json_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if actual != expected.replace("\r\n", "\n"):
        errors.append(f"stale {CATALOG_RELATIVE} (regenerate from {YAML_RELATIVE})")
    return errors


def write_catalog_json_from_yaml(root: Path) -> str:
    """Generate ``agents/catalog.json`` from YAML. Returns the relative path."""
    document = json_document_from_yaml(root)
    text = canonical_catalog_json(document)
    _atomic_write_text(root / CATALOG_RELATIVE, text)
    return CATALOG_RELATIVE


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic replace; POSIX uses O_NOFOLLOW and does not follow symlinks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise AgentsCatalogError(f"refusing symlink projection path: {path}")
    data = text.encode("utf-8")
    if _posix_nofollow_ready():
        _atomic_nofollow_write(path, data)
        return
    if _windows_nofollow_ready():
        try:
            write_path_regular(path, data)
        except Win32NofollowError as exc:
            raise AgentsCatalogError(f"cannot write {path}: {exc}") from exc
        return
    raise AgentsCatalogError(
        "agent catalog write requires POSIX O_NOFOLLOW/dir_fd or Windows no-follow open"
    )


def _atomic_nofollow_write(path: Path, data: bytes) -> None:
    parent = path.parent
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        parent_fd = os.open(parent, flags)
    except OSError as exc:
        raise AgentsCatalogError(f"cannot open projection parent: {parent}") from exc
    tmp_name = f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    tmp_fd = None
    try:
        tmp_fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=parent_fd,
        )
        written = 0
        while written < len(data):
            written += os.write(tmp_fd, data[written:])
        os.fsync(tmp_fd)
        os.close(tmp_fd)
        tmp_fd = None
        os.replace(tmp_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except OSError as exc:
        raise AgentsCatalogError(f"cannot write {path}: {exc}") from exc
    finally:
        if tmp_fd is not None:
            os.close(tmp_fd)
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


_FRONTMATTER_VALUE_MAX = 64


def strip_markdown_frontmatter(text: str) -> str:
    """Return markdown body after a leading YAML frontmatter fence."""
    if not text.startswith("---"):
        return text.strip()
    parts = text.split("---", 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return text.strip()


def _parse_agent_frontmatter(text: str, *, agent_id: str) -> dict[str, str]:
    """Parse scalar YAML-ish agent frontmatter. Fail closed on a missing fence."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise AgentsCatalogError(f"{agent_id}: missing YAML frontmatter")
    end = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = index
            break
    if end is None:
        raise AgentsCatalogError(f"{agent_id}: malformed YAML frontmatter")
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1].isspace() or stripped.startswith("-"):
            continue
        if ":" not in line:
            raise AgentsCatalogError(f"{agent_id}: malformed YAML frontmatter")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if not key:
            raise AgentsCatalogError(f"{agent_id}: malformed YAML frontmatter")
        scalar = raw_value.strip()
        if len(scalar) >= 2 and scalar[0] == scalar[-1] and scalar[0] in {"'", '"'}:
            scalar = scalar[1:-1]
        if key in fields:
            raise AgentsCatalogError(f"{agent_id}: duplicate frontmatter key")
        fields[key] = scalar
    return fields


def _frontmatter_canonical(
    fields: dict[str, str],
    *,
    camel: str,
    snake: str,
    agent_id: str,
) -> str:
    """Require the Grok camelCase key; reject snake_case aliases."""
    if snake in fields:
        raise AgentsCatalogError(
            f"{agent_id}: frontmatter must use {camel}, not {snake}"
        )
    value = fields.get(camel)
    if value is None or not str(value).strip():
        raise AgentsCatalogError(f"{agent_id}: frontmatter missing {camel}")
    value = str(value).strip()
    if len(value) > _FRONTMATTER_VALUE_MAX:
        raise AgentsCatalogError(f"{agent_id}: frontmatter {camel} is too long")
    return value


def _require_frontmatter_matches_catalog(text: str, record: AgentRecord) -> None:
    """Reject source frontmatter that omits or disagrees with the catalog posture."""

    fields = _parse_agent_frontmatter(text, agent_id=record.id)
    declared_cap = _frontmatter_canonical(
        fields,
        camel="capabilityMode",
        snake="capability_mode",
        agent_id=record.id,
    )
    declared_perm = _frontmatter_canonical(
        fields,
        camel="permissionMode",
        snake="permission_mode",
        agent_id=record.id,
    )
    if declared_cap.lower() in FORBIDDEN_CAPABILITY_MODES:
        raise AgentsCatalogError(
            f"{record.id}: frontmatter capabilityMode is forbidden "
            "(never execute/all)"
        )
    if declared_cap != record.capability_mode:
        raise AgentsCatalogError(
            f"{record.id}: frontmatter capabilityMode does not match catalog"
        )
    if declared_perm != record.permission_mode:
        raise AgentsCatalogError(
            f"{record.id}: frontmatter permissionMode does not match catalog"
        )


def _yaml_bool(value: bool) -> str:
    return "true" if value else "false"


def antigravity_tools_for(record: AgentRecord) -> tuple[str, ...]:
    """Return the least-privilege Agy tool profile for one catalog record."""

    tools = list(ANTIGRAVITY_READ_TOOLS)
    if record.capability_mode == "read-write":
        tools.extend(ANTIGRAVITY_WRITE_TOOLS)
        if "visual-engineering" in record.categories:
            tools.extend(ANTIGRAVITY_VISUAL_WRITE_TOOLS)
    return tuple(tools)


def _frontmatter_list(text: str, *, key: str, agent_id: str) -> tuple[str, ...] | None:
    """Read one top-level YAML list from fenced agent frontmatter."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise AgentsCatalogError(f"{agent_id}: missing YAML frontmatter")
    try:
        end = next(
            index for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise AgentsCatalogError(f"{agent_id}: malformed YAML frontmatter") from exc
    start = None
    for index, line in enumerate(lines[1:end], start=1):
        if line == f"{key}:":
            start = index + 1
            break
    if start is None:
        return None
    values: list[str] = []
    for line in lines[start:end]:
        if not line.startswith((" ", "\t")):
            break
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not stripped.startswith("- ") or not stripped[2:].strip():
            raise AgentsCatalogError(f"{agent_id}: malformed frontmatter {key} list")
        values.append(stripped[2:].strip())
    return tuple(values)


def render_antigravity_agent_tools(record: AgentRecord, source_text: str) -> str:
    """Insert/replace the Agy ``tools`` list while preserving agent content."""

    lines = source_text.replace("\r\n", "\n").splitlines()
    if not lines or lines[0].strip() != "---":
        raise AgentsCatalogError(f"{record.id}: missing YAML frontmatter")
    try:
        end = next(
            index for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise AgentsCatalogError(f"{record.id}: malformed YAML frontmatter") from exc

    block_start = None
    block_end = None
    for index in range(1, end):
        if lines[index] == "tools:":
            block_start = index
            block_end = index + 1
            while block_end < end and lines[block_end].startswith((" ", "\t")):
                block_end += 1
            break
    if block_start is None:
        block_start = end
        block_end = end

    block = ["tools:", *(f"  - {tool}" for tool in antigravity_tools_for(record))]
    rendered = lines[:block_start] + block + lines[block_end:]
    return "\n".join(rendered) + "\n"


def check_antigravity_agent_tools(root: Path) -> list[str]:
    """Report installable Agy tool frontmatter drift from the catalog."""

    errors: list[str] = []
    catalog = load_agents_catalog(root, require_projections=False, pin_files=False)
    for record in catalog.agents:
        path = root / record.file
        try:
            text = path.read_text(encoding="utf-8")
            actual = _frontmatter_list(text, key="tools", agent_id=record.id)
        except (OSError, UnicodeDecodeError, AgentsCatalogError) as exc:
            errors.append(f"{record.file}: {exc}")
            continue
        expected = antigravity_tools_for(record)
        if actual != expected:
            errors.append(f"stale {record.file} Antigravity tools")
    return errors


def write_antigravity_agent_tools(root: Path) -> list[str]:
    """Synchronize Agy tools in installable root agent frontmatter."""

    catalog = load_agents_catalog(root, require_projections=False, pin_files=False)
    written: list[str] = []
    for record in catalog.agents:
        path = root / record.file
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AgentsCatalogError(f"cannot read agent {record.file}: {exc}") from exc
        rendered = render_antigravity_agent_tools(record, source)
        if source.replace("\r\n", "\n") != rendered:
            _atomic_write_text(path, rendered)
            written.append(record.file)
    return written


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
            f"- Catalog: `{CATALOG_RELATIVE}` (generated from `{YAML_RELATIVE}`)",
            f"- capability_mode: `{record.capability_mode}` (never `execute`/`all`)",
            f"- spawn_policy: `{record.spawn_policy}` (depth=1 leaf vs parent)",
            "",
            render_handoff(
                record.id,
                task="(parent supplies a bounded mission; do not paste full leader history)",
                artifacts=(),
                decisions=(),
                record=record,
            ),
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
    loaded = catalog or load_agents_catalog(
        root, require_projections=False, pin_files=False
    )
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
    readme_rel = f"{ANTIGRAVITY_PROJECTION_ROOT}/README.md"
    rendered[readme_rel] = _projection_readme()
    written: list[str] = []
    for rel, text in rendered.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, text.replace("\r\n", "\n"))
        written.append(rel)
    _prune_obsolete_projections(root, set(rendered))
    written.sort()
    return written


def _prune_obsolete_projections(root: Path, expected: set[str]) -> None:
    agents_root = root / ANTIGRAVITY_PROJECTION_ROOT
    if not agents_root.is_dir():
        return
    for path in list(agents_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel not in expected:
            try:
                path.unlink()
            except OSError:
                continue
    dirs = sorted(
        (p for p in agents_root.rglob("*") if p.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for directory in dirs:
        try:
            next(directory.iterdir())
        except StopIteration:
            try:
                directory.rmdir()
            except OSError:
                continue


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

They are generated from `agents/catalog.yaml` → `agents/catalog.json` plus
`agents/omg-*.md` by `scripts/generate_agents_catalog.py` (which also writes
these projections). `scripts/generate_antigravity_agent_projections.py`
remains as a projection-only helper. Frontmatter maps OMG spawn/capability
floors onto documented AG keys (`mainAgent`, `subagent`,
`commandExecutionPolicy`). OMG does **not** claim Antigravity honors those
fields at runtime. Live AG smoke is **not** claimed.

Regenerate:

```bash
python scripts/generate_agents_catalog.py
python scripts/generate_agents_catalog.py --check
```
"""


__all__ = [
    "ALLOWED_CAPABILITY_MODES",
    "ALLOWED_CATEGORIES",
    "ANTIGRAVITY_PROJECTION_ROOT",
    "AgentRecord",
    "AgentsCatalog",
    "AgentsCatalogError",
    "CATALOG_RELATIVE",
    "CATEGORY_CANDIDATES",
    "FORBIDDEN_CAPABILITY_MODES",
    "HOST_NATIVE_PROFILES",
    "HostProjection",
    "KIND",
    "READ_ONLY_TIERS",
    "SCHEMA",
    "YAML_RELATIVE",
    "antigravity_tools_for",
    "antigravity_projection_relative",
    "assert_agent_capability",
    "canonical_catalog_json",
    "catalog_path",
    "check_antigravity_projections",
    "check_antigravity_agent_tools",
    "check_catalog_yaml",
    "inspect_agents_catalog",
    "json_document_from_yaml",
    "list_plugin_agent_files",
    "load_agents_catalog",
    "lookup_agent",
    "plugin_root",
    "render_antigravity_agent_md",
    "render_antigravity_projections",
    "render_handoff",
    "resolve_agent",
    "resolve_category",
    "strip_markdown_frontmatter",
    "write_antigravity_projections",
    "write_antigravity_agent_tools",
    "write_catalog_json_from_yaml",
    "yaml_catalog_path",
]
