"""Host-neutral lifecycle hook registry and in-process dispatcher (#72).

Documents Grok vs wrapper vs Antigravity vs unsupported mappings. Does not
invent UserPromptSubmit injection. Does not replace deny.py / stop_gate.py
behavior: those handlers are delegated unchanged.

Kill switches (bus): ``OMG_DISABLE_HOOKS`` and ``DISABLE_OMG`` disable the
dispatcher; ``OMG_SKIP_HOOKS`` skips named hook ids, canonical events, and
legacy ``hooks/bin`` logical names (``stop``, ``pre_tool_use``,
``session_start``, ``subagent_stop``). Grok plugin scripts still
honor ``DISABLE_OMG`` / ``OMG_SKIP_HOOKS`` via ``hooks/bin/_common.py``.

Antigravity files under ``docs/parity/projections/antigravity/hooks/`` are
static projections — not an installed AG plugin and not live AG evidence.
Timeouts are recorded after the synchronous handler returns (Python cannot
preempt). Dispatch appends a bounded redacted journal row (fail-open).
Never sets ``verified``.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA = "omg-hooks-registry/v1"
KIND = "read_only_machine_registry"
REGISTRY_RELATIVE = "hooks/registry.json"
ANTIGRAVITY_PROJECTION_ROOT = "docs/parity/projections/antigravity/hooks"
MAX_HOOK_OUTPUT_BYTES = 16_384
MAX_HANDOFF_BYTES = 8_192
AGGREGATE_BUDGET_MS = 2_000
TIMEOUT_KIND = "post_hoc"
BUS_SOURCE = "omg-hooks-bus"

# Logical names honored by hooks/bin/_common.py hook_disabled().
_LEGACY_SKIP_ALIASES: dict[str, tuple[str, ...]] = {
    "stop": ("stop.request", "omg.stop.gate", "Stop"),
    "pre_tool_use": ("tool.pre", "omg.pretool.deny", "PreToolUse"),
    "session_start": ("session.start", "omg.session.start.observe", "SessionStart"),
    "subagent_stop": ("subagent.stop", "omg.subagent.stop.observe", "SubagentStop"),
}

CANONICAL_EVENTS = (
    "prompt.submit",
    "session.start",
    "session.end",
    "tool.pre",
    "tool.post",
    "tool.failure",
    "permission.request",
    "subagent.start",
    "subagent.stop",
    "compact.pre",
    "idle",
    "stop.request",
    "workflow.transition",
    "artifact.created",
    "job.terminal",
    "team.member.transition",
)

HOST_CAPABILITIES = frozenset(
    {
        "native_blocking",
        "native_passive",
        "wrapper",
        "reconciled",
        "projected",
        "unsupported",
    }
)
FAIL_POLICIES = frozenset({"fail-open", "fail-closed"})
RUNTIME_PROJECTIONS = frozenset({"grok", "omg-cli", "antigravity", "none"})
KNOWN_HANDLERS = frozenset(
    {
        "unsupported",
        "observe",
        "compact_handoff",
        "continuation_guard",
        "deny.decide_pre_tool_use",
        "stop_gate.decide_stop",
    }
)
# Required security ids must stay enabled on their canonical event/projection.
# id -> (handler, event, runtime_projection, fail_policy)
SECURITY_ACTIVE_BINDINGS: dict[str, tuple[str, str, str, str]] = {
    "omg.pretool.deny": (
        "deny.decide_pre_tool_use",
        "tool.pre",
        "grok",
        "fail-open",
    ),
    "omg.stop.gate": (
        "stop_gate.decide_stop",
        "stop.request",
        "grok",
        "fail-open",
    ),
    "omg.continuation.guard": (
        "continuation_guard",
        "workflow.transition",
        "omg-cli",
        "fail-closed",
    ),
}
SECURITY_HANDLER_BINDINGS = {
    hook_id: spec[0] for hook_id, spec in SECURITY_ACTIVE_BINDINGS.items()
}
PRIVACY_CLASSES = frozenset(
    {"security", "workflow", "observability", "routing", "handoff"}
)

# Grok host honesty: which canonical events the host can actually honor.
GROK_EVENT_MAP: dict[str, str] = {
    "prompt.submit": "unsupported",  # UserPromptSubmit: stdout ignored
    "session.start": "native_passive",
    "session.end": "unsupported",
    "tool.pre": "native_blocking",
    "tool.post": "unsupported",
    "tool.failure": "unsupported",
    "permission.request": "unsupported",
    "subagent.start": "unsupported",
    "subagent.stop": "native_passive",
    "compact.pre": "wrapper",  # Grok PreCompact unavailable; CLI wrapper
    "idle": "native_blocking",  # Stop pin, fail-open, cap 8
    "stop.request": "native_blocking",
    "workflow.transition": "reconciled",
    "artifact.created": "reconciled",
    "job.terminal": "wrapper",
    "team.member.transition": "wrapper",
}

# Event → allowed Grok/AG host_hook names. ``host_hook: null`` is always
# allowed (CLI wrapper / reconciled). Any other name fails closed on load.
HOST_HOOK_ALLOWLIST: dict[str, frozenset[str]] = {
    "prompt.submit": frozenset({"UserPromptSubmit"}),
    "session.start": frozenset({"SessionStart"}),
    "session.end": frozenset({"SessionEnd"}),
    "tool.pre": frozenset({"PreToolUse"}),
    "tool.post": frozenset({"PostToolUse"}),
    "tool.failure": frozenset({"PostToolUse"}),
    "permission.request": frozenset({"PermissionRequest"}),
    "subagent.start": frozenset({"SubagentStart"}),
    "subagent.stop": frozenset({"SubagentStop"}),
    "compact.pre": frozenset({"PreCompact"}),
    "idle": frozenset({"Stop"}),
    "stop.request": frozenset({"Stop"}),
    "workflow.transition": frozenset(),
    "artifact.created": frozenset(),
    "job.terminal": frozenset(),
    "team.member.transition": frozenset(),
}

# Canonical bus event → event_contract LIFECYCLE_EVENTS type.
BUS_EVENT_TYPES: dict[str, str] = {
    "prompt.submit": "turn_started",
    "session.start": "session_started",
    "session.end": "agent_closed",
    "tool.pre": "turn_started",
    "tool.post": "turn_completed",
    "tool.failure": "turn_completed",
    "permission.request": "turn_started",
    "subagent.start": "spawn_requested",
    "subagent.stop": "agent_closed",
    "compact.pre": "turn_started",
    "idle": "turn_started",
    "stop.request": "turn_started",
    "workflow.transition": "turn_completed",
    "artifact.created": "turn_completed",
    "job.terminal": "agent_closed",
    "team.member.transition": "turn_completed",
}

_HOOK_REQUIRED = (
    "id",
    "event",
    "runtime_projection",
    "host_capability",
    "priority",
    "timeout_ms",
    "fail_policy",
    "enabled",
    "handler",
)


class HooksRegistryError(ValueError):
    """Fail-closed registry load / validation error."""


@dataclass(frozen=True, slots=True)
class HookRecord:
    """One registry-owned lifecycle hook."""

    id: str
    event: str
    runtime_projection: str
    host_hook: str | None
    host_capability: str
    priority: int
    timeout_ms: int
    fail_policy: str
    enabled: bool
    required_capabilities: tuple[str, ...]
    privacy_class: str
    handler: str
    note: str

    def to_inspect_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event": self.event,
            "runtime_projection": self.runtime_projection,
            "host_hook": self.host_hook,
            "host_capability": self.host_capability,
            "priority": self.priority,
            "timeout_ms": self.timeout_ms,
            "fail_policy": self.fail_policy,
            "enabled": self.enabled,
            "handler": self.handler,
            "privacy_class": self.privacy_class,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class HooksRegistry:
    schema: str
    hooks: tuple[HookRecord, ...]

    def by_id(self) -> dict[str, HookRecord]:
        return {hook.id: hook for hook in self.hooks}

    def for_event(self, event: str) -> tuple[HookRecord, ...]:
        rows = [hook for hook in self.hooks if hook.event == event]
        rows.sort(key=lambda item: (item.priority, item.id))
        return tuple(rows)


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def registry_path(root: Path | None = None) -> Path:
    return (root if root is not None else plugin_root()) / REGISTRY_RELATIVE


def _require_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HooksRegistryError(f"{label} must be a JSON object")
    return value


def _require_str(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HooksRegistryError(f"{label} must be a non-empty string")
    return value


def _parse_hook(value: Any, *, index: int) -> HookRecord:
    label = f"hooks[{index}]"
    obj = _require_object(value, label=label)
    missing = [key for key in _HOOK_REQUIRED if key not in obj]
    if missing:
        raise HooksRegistryError(f"{label} missing keys: {', '.join(missing)}")
    hook_id = _require_str(obj["id"], label=f"{label}.id")
    event = _require_str(obj["event"], label=f"{label}.event")
    if event not in CANONICAL_EVENTS:
        raise HooksRegistryError(f"{hook_id}: unknown event {event!r}")
    projection = _require_str(
        obj["runtime_projection"], label=f"{label}.runtime_projection"
    )
    if projection not in RUNTIME_PROJECTIONS:
        raise HooksRegistryError(
            f"{hook_id}: runtime_projection must be one of {sorted(RUNTIME_PROJECTIONS)}"
        )
    cap = _require_str(obj["host_capability"], label=f"{label}.host_capability")
    if cap not in HOST_CAPABILITIES:
        raise HooksRegistryError(f"{hook_id}: unknown host_capability {cap!r}")
    if event == "prompt.submit" and projection == "grok":
        if cap != "unsupported" or obj["enabled"] is not False:
            raise HooksRegistryError(
                "prompt.submit on Grok must be unsupported and disabled "
                "(no UserPromptSubmit injection)"
            )
    grok_expected = GROK_EVENT_MAP[event]
    if projection == "grok" and cap != grok_expected:
        raise HooksRegistryError(
            f"{hook_id}: grok {event} must map as {grok_expected}, got {cap}"
        )
    policy = _require_str(obj["fail_policy"], label=f"{label}.fail_policy")
    if policy not in FAIL_POLICIES:
        raise HooksRegistryError(f"{hook_id}: fail_policy must be fail-open or fail-closed")
    if not isinstance(obj["enabled"], bool):
        raise HooksRegistryError(f"{hook_id}: enabled must be boolean")
    if not isinstance(obj["priority"], int) or isinstance(obj["priority"], bool):
        raise HooksRegistryError(f"{hook_id}: priority must be int")
    if not isinstance(obj["timeout_ms"], int) or obj["timeout_ms"] < 1:
        raise HooksRegistryError(f"{hook_id}: timeout_ms must be a positive int")
    privacy = _require_str(obj.get("privacy_class", "observability"), label="privacy")
    if privacy not in PRIVACY_CLASSES:
        raise HooksRegistryError(f"{hook_id}: unknown privacy_class")
    host_hook = obj.get("host_hook")
    if host_hook is not None:
        host_hook = _require_str(host_hook, label=f"{label}.host_hook")
        allowed = HOST_HOOK_ALLOWLIST.get(event)
        if allowed is None:
            raise HooksRegistryError(f"{hook_id}: event {event!r} has no host_hook allowlist")
        if host_hook not in allowed:
            raise HooksRegistryError(
                f"{hook_id}: host_hook {host_hook!r} is not allowed for event {event!r}"
            )
    required = obj.get("required_capabilities") or []
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise HooksRegistryError(f"{hook_id}: required_capabilities must be strings")
    handler = _require_str(obj["handler"], label=f"{label}.handler")
    if handler not in KNOWN_HANDLERS:
        raise HooksRegistryError(f"{hook_id}: unknown handler {handler!r}")
    expected_handler = SECURITY_HANDLER_BINDINGS.get(hook_id)
    if expected_handler is not None and handler != expected_handler:
        raise HooksRegistryError(
            f"{hook_id}: handler must be {expected_handler!r}, got {handler!r}"
        )
    return HookRecord(
        id=hook_id,
        event=event,
        runtime_projection=projection,
        host_hook=host_hook,
        host_capability=cap,
        priority=int(obj["priority"]),
        timeout_ms=int(obj["timeout_ms"]),
        fail_policy=policy,
        enabled=bool(obj["enabled"]),
        required_capabilities=tuple(required),
        privacy_class=privacy,
        handler=handler,
        note=str(obj.get("note") or ""),
    )


def load_hooks_registry(
    root: Path | None = None, *, allow_incomplete: bool = False
) -> HooksRegistry:
    if set(HOST_HOOK_ALLOWLIST) != set(CANONICAL_EVENTS):
        raise HooksRegistryError("HOST_HOOK_ALLOWLIST must cover every canonical event")
    if set(BUS_EVENT_TYPES) != set(CANONICAL_EVENTS):
        raise HooksRegistryError("BUS_EVENT_TYPES must cover every canonical event")
    if set(GROK_EVENT_MAP) != set(CANONICAL_EVENTS):
        raise HooksRegistryError("GROK_EVENT_MAP must cover every canonical event")
    base = Path(root) if root is not None else plugin_root()
    path = registry_path(base)
    if not path.is_file():
        raise HooksRegistryError(f"missing hook registry: {REGISTRY_RELATIVE}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HooksRegistryError(f"cannot read registry: {exc}") from exc
    obj = _require_object(raw, label="registry")
    schema = _require_str(obj.get("schema"), label="schema")
    if schema != SCHEMA:
        raise HooksRegistryError(f"unsupported registry schema {schema!r}")
    kind = _require_str(obj.get("kind"), label="kind")
    if kind != KIND:
        raise HooksRegistryError(f"registry kind must be {KIND!r}")
    events = obj.get("canonical_events")
    if events != list(CANONICAL_EVENTS):
        raise HooksRegistryError("canonical_events must match the frozen event list")
    hooks_raw = obj.get("hooks")
    if not isinstance(hooks_raw, list) or not hooks_raw:
        raise HooksRegistryError("registry.hooks must be a non-empty array")
    seen: set[str] = set()
    records: list[HookRecord] = []
    for index, item in enumerate(hooks_raw):
        record = _parse_hook(item, index=index)
        if record.id in seen:
            raise HooksRegistryError(f"duplicate hook id {record.id!r}")
        seen.add(record.id)
        records.append(record)
    if not allow_incomplete:
        missing = [
            hook_id
            for hook_id in SECURITY_ACTIVE_BINDINGS
            if hook_id not in seen
        ]
        if missing:
            raise HooksRegistryError(
                "missing required security hook ids: " + ", ".join(missing)
            )
        by_id = {record.id: record for record in records}
        for hook_id, (handler, event, projection, policy) in SECURITY_ACTIVE_BINDINGS.items():
            record = by_id[hook_id]
            if not record.enabled:
                raise HooksRegistryError(
                    f"{hook_id}: required security hook must be enabled"
                )
            if record.event != event:
                raise HooksRegistryError(
                    f"{hook_id}: required security hook event must be {event!r}"
                )
            if record.runtime_projection != projection:
                raise HooksRegistryError(
                    f"{hook_id}: required security hook runtime_projection "
                    f"must be {projection!r}"
                )
            if record.fail_policy != policy:
                raise HooksRegistryError(
                    f"{hook_id}: required security hook fail_policy must be {policy!r}"
                )
            if record.handler != handler:
                raise HooksRegistryError(
                    f"{hook_id}: handler must be {handler!r}, got {record.handler!r}"
                )
    records.sort(key=lambda item: (item.event, item.priority, item.id))
    return HooksRegistry(schema=schema, hooks=tuple(records))


def inspect_hooks_registry(
    root: Path | None = None, *, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Inspect payload (never verified / never live AG)."""
    base = Path(root) if root is not None else plugin_root()
    try:
        registry = load_hooks_registry(base)
    except HooksRegistryError as exc:
        return {
            "schema": SCHEMA,
            "ok": False,
            "configured": (base / REGISTRY_RELATIVE).is_file(),
            "installed": False,
            "enabled": False,
            "loadable": False,
            "observed": False,
            "healthy": False,
            "verified": False,
            "error": str(exc),
            "note": "hook registry; Grok UserPromptSubmit is not an injector",
        }
    grok_map = [
        {"event": event, "grok": cap, "antigravity": "projected"}
        for event, cap in GROK_EVENT_MAP.items()
    ]
    return {
        "schema": SCHEMA,
        "ok": True,
        "configured": True,
        "installed": True,
        "enabled": not bus_disabled(env),
        "loadable": True,
        "observed": False,
        "healthy": False,
        "verified": False,
        "hook_count": len(registry.hooks),
        "hooks": [hook.to_inspect_row() for hook in registry.hooks],
        "grok_event_map": grok_map,
        "timeout_kind": TIMEOUT_KIND,
        "security_hook_ids": list(SECURITY_HANDLER_BINDINGS),
        "note": (
            "Grok PreToolUse/Stop may block; SessionStart/SubagentStop are passive; "
            "UserPromptSubmit injection is unsupported. Antigravity hook files are "
            "projections only, not live AG evidence. Timeouts are post-hoc after "
            "the synchronous handler returns. Never sets verified."
        ),
    }


def bus_disabled(env: Mapping[str, str] | None = None) -> bool:
    """True when the in-process bus is globally disabled."""
    e = env if env is not None else os.environ
    for key in ("OMG_DISABLE_HOOKS", "DISABLE_OMG"):
        flag = str(e.get(key, "")).strip().lower()
        if flag in {"1", "true", "yes", "on"}:
            return True
    return False


def skipped_hook_ids(env: Mapping[str, str] | None = None) -> set[str]:
    e = env if env is not None else os.environ
    raw = str(e.get("OMG_SKIP_HOOKS", ""))
    tokens = {
        token.strip().lower()
        for chunk in raw.split(",")
        for token in chunk.split()
        if token.strip()
    }
    expanded = set(tokens)
    for alias, targets in _LEGACY_SKIP_ALIASES.items():
        if alias in tokens:
            expanded.update(item.lower() for item in targets)
    return expanded


def _hook_skipped(record: HookRecord, skip: set[str]) -> bool:
    names = {record.id.lower(), record.event.lower()}
    if record.host_hook:
        names.add(record.host_hook.lower())
    return bool(names & skip)


def resolve_continuation(active_owner: str | None, requested: str) -> str:
    """Delegate to the #70 skill-catalog continuation policy (single owner)."""
    from omg_cli.skills_catalog import (
        SkillsCatalogError,
        load_skills_catalog,
        plugin_root as skill_root,
        resolve_continuation as resolve_skill_continuation,
    )

    try:
        catalog = load_skills_catalog(skill_root(), require_projections=False)
    except SkillsCatalogError as exc:
        raise HooksRegistryError(f"continuation catalog unavailable: {exc}") from exc
    return resolve_skill_continuation(active_owner, requested, catalog=catalog)


def write_compact_handoff(
    root: Path,
    *,
    run_id: str,
    session_id: str,
    goal_id: str | None = None,
    task_ids: list[str] | None = None,
    transcript: str | None = None,
) -> dict[str, Any]:
    """Bounded ids-only compaction handoff. Transcript is refused, not copied."""
    if transcript:
        raise HooksRegistryError("compact handoff must not include a transcript")
    payload = {
        "schema": "omg-compact-handoff/v1",
        "run_id": str(run_id or "")[:128],
        "session_id": str(session_id or "")[:128],
        "goal_id": (goal_id or "")[:128] or None,
        "task_ids": [str(item)[:128] for item in (task_ids or [])[:32]],
        "transcript_included": False,
        "verified": False,
        "note": "ids only; Grok PreCompact is unavailable",
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_HANDOFF_BYTES:
        raise HooksRegistryError("compact handoff exceeds bounded size")
    dest_dir = Path(root) / ".omg" / "artifacts"
    dest = dest_dir / "compact-handoff.json"
    if dest.is_symlink() or dest_dir.is_symlink():
        raise HooksRegistryError("compact handoff may not be a symlink")
    try:
        from omg_cli.contracts.path_keys import (
            ContractPathError,
            atomic_write_bytes,
            ensure_managed_dir,
        )

        ensure_managed_dir(dest_dir)
        atomic_write_bytes(dest, body)
    except ContractPathError as exc:
        detail = str(exc).lower()
        posix_unsupported = "posix" in detail or "o_nofollow" in detail
        if not posix_unsupported:
            raise HooksRegistryError(f"compact handoff write refused: {exc}") from exc
        current = dest
        while True:
            if current.is_symlink():
                raise HooksRegistryError("compact handoff may not be a symlink") from exc
            parent = current.parent
            if parent == current:
                break
            current = parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
    return payload


def _validate_hook_output(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"status": "ok"}
    if not isinstance(raw, dict):
        raise HooksRegistryError("hook output must be an object")
    encoded = json.dumps(raw, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_HOOK_OUTPUT_BYTES:
        raise HooksRegistryError("hook output exceeds bounded size")
    if "command" in raw or "shell" in raw or "argv" in raw:
        raise HooksRegistryError("hook output must not emit commands")
    if raw.get("verified") is True or raw.get("passes") is True:
        raise HooksRegistryError("hook output cannot set verified/passes")
    return raw


def _run_handler(record: HookRecord, payload: Mapping[str, Any]) -> dict[str, Any]:
    handler = record.handler
    if handler == "unsupported":
        return {
            "status": "unsupported",
            "inject": False,
            "message": "Grok does not honor this lifecycle inject",
        }
    if handler == "observe":
        return {"status": "observed", "blocking": False}
    if handler == "compact_handoff":
        root = Path(str(payload.get("root") or "."))
        return write_compact_handoff(
            root,
            run_id=str(payload.get("run_id") or "unknown"),
            session_id=str(payload.get("session_id") or "unknown"),
            goal_id=payload.get("goal_id") if isinstance(payload.get("goal_id"), str) else None,
            task_ids=list(payload.get("task_ids") or [])
            if isinstance(payload.get("task_ids"), list)
            else None,
        )
    if handler == "continuation_guard":
        decision = resolve_continuation(
            payload.get("active_owner") if isinstance(payload.get("active_owner"), str) else None,
            str(payload.get("requested") or ""),
        )
        return {"status": "ok", "decision": decision, "verified": False}
    if handler == "deny.decide_pre_tool_use":
        from omg_cli.deny import decide_pre_tool_use

        return dict(decide_pre_tool_use(dict(payload)))
    if handler == "stop_gate.decide_stop":
        from omg_cli.stop_gate import decide_stop

        root = Path(str(payload.get("root") or "."))
        result = decide_stop(root, dict(payload))
        return dict(result) if result else {"decision": "allow"}
    raise HooksRegistryError(f"unknown handler {handler!r}")


def dispatch(
    event: str,
    payload: Mapping[str, Any] | None = None,
    *,
    root: Path | None = None,
    env: Mapping[str, str] | None = None,
    registry: HooksRegistry | None = None,
    handlers: Mapping[str, Callable[[HookRecord, Mapping[str, Any]], Any]] | None = None,
) -> dict[str, Any]:
    """Run enabled hooks for *event*. Fail-open unless the hook is fail-closed.

    A hook crash never corrupts run state (this function does not write
    ``.omg/state`` ``passes`` / ``verified``). Oversized/untrusted output is
    rejected. Timeout is cooperative: duration is recorded after the handler
    returns. Journal append is fail-open.
    """
    if event not in CANONICAL_EVENTS:
        raise HooksRegistryError(f"unknown event {event!r}")
    started = time.monotonic()
    body = dict(payload or {})
    result = _dispatch_event(
        event,
        body,
        root=root,
        env=env,
        registry=registry,
        handlers=handlers,
    )
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    result["timeout_kind"] = TIMEOUT_KIND
    result["verified"] = False
    if result.get("skipped") != "disabled":
        _fail_open_journal(result, event=event, payload=body)
    return result


def _dispatch_event(
    event: str,
    body: dict[str, Any],
    *,
    root: Path | None,
    env: Mapping[str, str] | None,
    registry: HooksRegistry | None,
    handlers: Mapping[str, Callable[[HookRecord, Mapping[str, Any]], Any]] | None,
) -> dict[str, Any]:
    e = env if env is not None else os.environ
    if bus_disabled(e):
        return {
            "ok": True,
            "event": event,
            "skipped": "disabled",
            "results": [],
            "verified": False,
        }
    loaded = registry or load_hooks_registry(root)
    skip = skipped_hook_ids(e)
    results: list[dict[str, Any]] = []
    started = time.monotonic()
    for record in loaded.for_event(event):
        if not record.enabled or _hook_skipped(record, skip):
            results.append(
                {
                    "id": record.id,
                    "status": "skipped",
                    "duration_ms": 0,
                    "timeout_kind": TIMEOUT_KIND,
                }
            )
            continue
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if elapsed_ms >= AGGREGATE_BUDGET_MS:
            row = {
                "id": record.id,
                "status": "budget_exceeded",
                "fail_policy": record.fail_policy,
                "duration_ms": elapsed_ms,
                "timeout_kind": TIMEOUT_KIND,
            }
            if record.fail_policy == "fail-closed":
                return {
                    "ok": False,
                    "event": event,
                    "results": results + [row],
                    "verified": False,
                    "error": "E_HOOK_FAIL_CLOSED",
                }
            row["fail_open"] = True
            results.append(row)
            continue
        hook_started = time.monotonic()
        try:
            runner = (handlers or {}).get(record.id)
            raw = runner(record, body) if runner else _run_handler(record, body)
            duration_ms = int((time.monotonic() - hook_started) * 1000)
            if duration_ms > record.timeout_ms:
                raise HooksRegistryError(
                    f"{record.id} exceeded timeout {record.timeout_ms}ms"
                )
            output = _validate_hook_output(raw)
            results.append(
                {
                    "id": record.id,
                    "status": "ok",
                    "output": output,
                    "duration_ms": duration_ms,
                    "timeout_ms": record.timeout_ms,
                    "timeout_kind": TIMEOUT_KIND,
                }
            )
        except Exception as exc:  # noqa: BLE001 — bus must fail-open
            duration_ms = int((time.monotonic() - hook_started) * 1000)
            row = {
                "id": record.id,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "fail_policy": record.fail_policy,
                "duration_ms": duration_ms,
                "timeout_ms": record.timeout_ms,
                "timeout_kind": TIMEOUT_KIND,
            }
            if record.fail_policy == "fail-closed":
                return {
                    "ok": False,
                    "event": event,
                    "results": results + [row],
                    "verified": False,
                    "error": "E_HOOK_FAIL_CLOSED",
                }
            row["fail_open"] = True
            results.append(row)
    return {"ok": True, "event": event, "results": results, "verified": False}


def _workspace_from_payload(payload: Mapping[str, Any]) -> Path | None:
    raw = payload.get("root") or payload.get("workspace")
    if not raw:
        return None
    try:
        return Path(str(raw))
    except (TypeError, ValueError):
        return None


def _fail_open_journal(
    result: dict[str, Any], *, event: str, payload: Mapping[str, Any]
) -> None:
    """Append a bounded redacted bus event. Never raises; never sets verified."""
    workspace = _workspace_from_payload(payload)
    if workspace is None:
        result["journal"] = {
            "ok": False,
            "skipped": "no_workspace",
            "verified": False,
        }
        return
    try:
        from omg_cli.runtime_events import append_bus_event

        summary = {
            "canonical_event": event,
            "ok": result.get("ok"),
            "skipped": result.get("skipped"),
            "error": result.get("error"),
            "duration_ms": result.get("duration_ms"),
            "timeout_kind": TIMEOUT_KIND,
            "results": [
                {
                    "id": row.get("id"),
                    "status": row.get("status"),
                    "duration_ms": row.get("duration_ms"),
                    "fail_open": row.get("fail_open"),
                }
                for row in (result.get("results") or [])
                if isinstance(row, dict)
            ],
            "verified": False,
        }
        journal = append_bus_event(
            workspace,
            canonical_event=event,
            event_type=BUS_EVENT_TYPES.get(event, "agent_failed"),
            payload=summary,
            run_id=payload.get("run_id") if isinstance(payload.get("run_id"), str) else None,
            session_id=(
                payload.get("session_id")
                if isinstance(payload.get("session_id"), str)
                else None
            ),
        )
        result["journal"] = {
            "ok": True,
            "source_sequence": journal.get("source_sequence"),
            "event_id": journal.get("event_id"),
            "verified": False,
        }
    except Exception as exc:  # noqa: BLE001 — journal must not crash a session
        result["journal"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "fail_open": True,
            "verified": False,
        }


def render_antigravity_projection(registry: HooksRegistry) -> str:
    lines = [
        "# Antigravity hook projection",
        "",
        "**Status:** static parity projection for "
        "[#72](https://github.com/ImL1s/oh-my-grok/issues/72).",
        "",
        "This is **not** an installed Antigravity plugin, not live AG evidence,",
        "and not proof that `agy` hook discovery works.",
        "",
        "`hooks.json` beside this README is a **static** hooks.json-shaped",
        "projection for a later install path (#77). Copying it does not mean",
        "`agy` loaded hooks.",
        "",
        "Grok honesty:",
        "",
        "- `PreToolUse` / `Stop` may block (Stop: grok >=0.2.107, cap 8/turn, fail-open).",
        "- `SessionStart` / `SubagentStop` are **passive** (stdout ignored).",
        "- `UserPromptSubmit` injection is **unsupported** — routing lives in the rules file.",
        "",
        "| Hook | Event | Grok capability | Fail policy |",
        "|------|-------|-----------------|-------------|",
    ]
    for hook in registry.hooks:
        lines.append(
            f"| `{hook.id}` | `{hook.event}` | `{hook.host_capability}` | `{hook.fail_policy}` |"
        )
    lines.extend(["", "Kill switches: `DISABLE_OMG`, `OMG_DISABLE_HOOKS`, `OMG_SKIP_HOOKS`.", ""])
    return "\n".join(lines)


def render_antigravity_hooks_json(registry: HooksRegistry) -> dict[str, Any]:
    """hooks.json-shaped static projection. Not live AG evidence."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for hook in registry.hooks:
        if hook.host_hook is None:
            continue
        if hook.host_capability == "unsupported":
            continue
        grouped.setdefault(hook.host_hook, []).append(
            {
                "matcher": "*",
                "omg_hook_id": hook.id,
                "projection": True,
                "live_ag": False,
                "hooks": [
                    {
                        "type": "command",
                        "command": f"omg-antigravity-hook-projection:{hook.id}",
                        "timeout": max(1, (hook.timeout_ms + 999) // 1000),
                    }
                ],
            }
        )
    return {
        "schema": "omg-antigravity-hooks-projection/v1",
        "kind": "static_projection",
        "verified": False,
        "live_ag": False,
        "observed": False,
        "healthy": False,
        "hooks": grouped,
        "note": (
            "Static hooks.json-shaped projection for #72. Not an installed "
            "Antigravity plugin and not proof that agy loaded hooks. "
            "UserPromptSubmit injection is unsupported."
        ),
    }


def antigravity_hooks_json_text(registry: HooksRegistry) -> str:
    return (
        json.dumps(
            render_antigravity_hooks_json(registry),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )


def write_antigravity_projection(root: Path) -> str:
    registry = load_hooks_registry(root)
    dest = root / ANTIGRAVITY_PROJECTION_ROOT
    dest.mkdir(parents=True, exist_ok=True)
    readme = dest / "README.md"
    hooks_json = dest / "hooks.json"
    readme.write_text(
        render_antigravity_projection(registry), encoding="utf-8", newline="\n"
    )
    hooks_json.write_text(
        antigravity_hooks_json_text(registry), encoding="utf-8", newline="\n"
    )
    return f"{ANTIGRAVITY_PROJECTION_ROOT}/README.md"


def check_antigravity_projection(root: Path) -> list[str]:
    registry = load_hooks_registry(root)
    expected_readme = render_antigravity_projection(registry)
    expected_json = antigravity_hooks_json_text(registry)
    errors: list[str] = []
    readme = root / ANTIGRAVITY_PROJECTION_ROOT / "README.md"
    hooks_json = root / ANTIGRAVITY_PROJECTION_ROOT / "hooks.json"
    if not readme.is_file():
        errors.append(f"missing {ANTIGRAVITY_PROJECTION_ROOT}/README.md")
    else:
        actual = readme.read_text(encoding="utf-8").replace("\r\n", "\n")
        if actual != expected_readme.replace("\r\n", "\n"):
            errors.append(f"stale {ANTIGRAVITY_PROJECTION_ROOT}/README.md")
    if not hooks_json.is_file():
        errors.append(f"missing {ANTIGRAVITY_PROJECTION_ROOT}/hooks.json")
    else:
        actual_json = hooks_json.read_text(encoding="utf-8").replace("\r\n", "\n")
        if actual_json != expected_json.replace("\r\n", "\n"):
            errors.append(f"stale {ANTIGRAVITY_PROJECTION_ROOT}/hooks.json")
    return errors


def install_antigravity_hook_projection(
    dest: Path | str,
    *,
    root: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Copy the static AG hook projection into *dest*.

    Callable later by the #77 install manifest. Does **not** claim that
    ``agy`` loaded hooks. Never sets ``verified``.
    """
    dest_dir = Path(dest)
    if dest_dir.exists() and dest_dir.is_file():
        raise HooksRegistryError("install dest must be a directory")
    registry = load_hooks_registry(root)
    files = {
        "README.md": render_antigravity_projection(registry),
        "hooks.json": antigravity_hooks_json_text(registry),
    }
    planned = [str(dest_dir / name) for name in files]
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            (dest_dir / name).write_text(body, encoding="utf-8", newline="\n")
    return {
        "ok": True,
        "dry_run": dry_run,
        "files": planned,
        "verified": False,
        "live_ag": False,
        "observed": False,
        "healthy": False,
        "note": "static projection copy; agy did not load these hooks",
    }


__all__ = [
    "AGGREGATE_BUDGET_MS",
    "ANTIGRAVITY_PROJECTION_ROOT",
    "BUS_EVENT_TYPES",
    "BUS_SOURCE",
    "CANONICAL_EVENTS",
    "GROK_EVENT_MAP",
    "HOST_HOOK_ALLOWLIST",
    "KIND",
    "MAX_HANDOFF_BYTES",
    "REGISTRY_RELATIVE",
    "SCHEMA",
    "SECURITY_ACTIVE_BINDINGS",
    "SECURITY_HANDLER_BINDINGS",
    "TIMEOUT_KIND",
    "HookRecord",
    "HooksRegistry",
    "HooksRegistryError",
    "bus_disabled",
    "check_antigravity_projection",
    "dispatch",
    "inspect_hooks_registry",
    "install_antigravity_hook_projection",
    "load_hooks_registry",
    "plugin_root",
    "render_antigravity_hooks_json",
    "resolve_continuation",
    "skipped_hook_ids",
    "write_antigravity_projection",
    "write_compact_handoff",
]
