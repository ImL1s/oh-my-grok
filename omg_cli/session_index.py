"""Session search, friction, replay, timeline, observatory, and retention (#74).

Events under ``resolve_state_root().state_dir`` remain the source of truth.
This module never re-executes host commands, never writes ``passes``/``verified``,
and never prints raw prompts, responses, tool output, or secrets.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from omg_cli.project_root import path_is_under
from omg_cli.redaction import REDACTED, redact_text, redact_value
from omg_cli.state_root import StateRootResolution, resolve_state_root


SESSION_INDEX_SCHEMA = 1
MAX_JOURNAL_BYTES = 1_048_576
MAX_EVENTS_PER_STORE = 4_096
MAX_DIAGNOSTICS = 32
MAX_RESULTS = 256
MAX_CONTEXT = 5
MAX_EXCERPT_CHARS = 240
MAX_ARTIFACT_SCAN = 256
MAX_RUN_SCAN = 64
MAX_JOB_SCAN = 64
IDLE_GAP_SECONDS = 15 * 60
LARGE_EVENT_BYTES = 8_192
LARGE_ARTIFACT_BYTES = 1_048_576
STALE_LEASE_SECONDS = 3_600
RAW_CONTENT_KEYS = frozenset(
    {
        "prompt",
        "prompts",
        "response",
        "responses",
        "message",
        "messages",
        "transcript",
        "tool_output",
        "tool_result",
        "tool_input",
        "stdout",
        "stderr",
        "command",
        "commands",
        "completion",
        "raw_prompt",
        "raw_response",
    }
)
HOST_ID_KEYS = (
    "session_id",
    "grok_session_id",
    "host_session_id",
    "host_uuid",
    "session_uuid",
    "uuid",
    "provenance_uuid",
)
_SINCE_RE = re.compile(r"^(\d+)([smhdw])$", re.IGNORECASE)
_HOME_PATH_RE = re.compile(r"(?P<prefix>(?:/Users|/home)/)(?P<user>[^/\s\"']+)")
_WIN_HOME_RE = re.compile(r"(?P<prefix>(?i:[A-Z]:\\Users\\))(?P<user>[^\\\s\"']+)")


class SessionIndexError(ValueError):
    """Fail-closed session-index error with a stable envelope code."""

    def __init__(self, message: str, *, code: str = "E_SESSION_INDEX") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProjectStore:
    """One confined state store (current project or a discovered sibling)."""

    project_key: str
    state_dir: Path
    project_root: Path | None
    source: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_since(value: str, *, now: datetime | None = None) -> datetime:
    """Parse ``7d`` / ``24h`` / RFC3339 into an aware UTC cutoff."""

    text = (value or "").strip()
    if not text:
        raise SessionIndexError(
            "since must be a relative duration or RFC3339",
            code="E_SESSION_SINCE",
        )
    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    match = _SINCE_RE.fullmatch(text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        delta = {
            "s": timedelta(seconds=amount),
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
        }[unit]
        return current - delta
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise SessionIndexError(
            "since must be like 7d, 24h, or RFC3339",
            code="E_SESSION_SINCE",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def redact_home_paths(value: str) -> str:
    """Redact credentials then full home-shaped paths."""

    cleaned = redact_text(value)
    homes: list[str] = []
    for candidate in (
        (os.environ.get("HOME") or "").strip(),
        (os.environ.get("USERPROFILE") or "").strip(),
    ):
        if candidate:
            homes.append(candidate)
            try:
                homes.append(str(Path(candidate).expanduser().resolve()))
            except OSError:
                pass
    try:
        homes.append(str(Path.home().resolve()))
    except OSError:
        pass
    seen: set[str] = set()
    for home in sorted(homes, key=len, reverse=True):
        if not home or home in seen:
            continue
        seen.add(home)
        if home in cleaned:
            cleaned = cleaned.replace(home, "<home>")
    cleaned = _HOME_PATH_RE.sub(r"\g<prefix><user>", cleaned)
    cleaned = _WIN_HOME_RE.sub(r"\g<prefix><user>", cleaned)
    return cleaned


def _redact_public(value: Any) -> Any:
    redacted = redact_value(value)
    if isinstance(redacted, str):
        return redact_home_paths(redacted)
    if isinstance(redacted, dict):
        return {str(key): _redact_public(item) for key, item in redacted.items()}
    if isinstance(redacted, list):
        return [_redact_public(item) for item in redacted]
    return redacted


def _confine_under(root: Path, candidate: Path) -> Path | None:
    try:
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve()
        base = root.resolve()
    except OSError:
        return None
    if not path_is_under(resolved, base):
        return None
    try:
        if resolved.is_symlink():
            return None
    except OSError:
        return None
    return resolved


def _is_fs_root(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return True
    return resolved.parent == resolved


def _is_home(path: Path) -> bool:
    try:
        return path.resolve() == Path.home().resolve()
    except OSError:
        return False


def _public_rel(path: Path, state_dir: Path) -> str:
    confined = _confine_under(state_dir, path)
    if confined is None:
        return path.name
    try:
        rel = confined.relative_to(state_dir.resolve())
    except ValueError:
        return path.name
    return rel.as_posix()


def _drop_raw_content(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower().replace("-", "_")
            if lowered in RAW_CONTENT_KEYS or any(
                part in lowered for part in ("prompt", "transcript")
            ):
                continue
            out[str(key)] = _drop_raw_content(item)
        return out
    if isinstance(value, list):
        return [_drop_raw_content(item) for item in value]
    return value


def _extract_host_ids(record: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def _add(raw: object) -> None:
        if not isinstance(raw, str):
            return
        text = raw.strip()
        if not text or text in seen:
            return
        seen.add(text)
        found.append(text)

    for key in HOST_ID_KEYS:
        _add(record.get(key))
    payload = record.get("payload")
    if isinstance(payload, Mapping):
        for key in HOST_ID_KEYS:
            _add(payload.get(key))
        provenance = payload.get("provenance")
        if isinstance(provenance, Mapping):
            for key in HOST_ID_KEYS:
                _add(provenance.get(key))
        host = payload.get("host")
        if isinstance(host, Mapping):
            _add(host.get("uuid"))
            _add(host.get("session_id"))
    provenance = record.get("provenance")
    if isinstance(provenance, Mapping):
        for key in HOST_ID_KEYS:
            _add(provenance.get(key))
    return found


def _searchable_text(record: Mapping[str, Any]) -> str:
    parts: list[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                lowered = str(key).lower().replace("-", "_")
                if (
                    lowered in RAW_CONTENT_KEYS
                    or "prompt" in lowered
                    or "transcript" in lowered
                ):
                    continue
                _walk(item)
            return
        if isinstance(value, list):
            for item in value:
                _walk(item)
            return
        if isinstance(value, str):
            text = value.strip()
            if text and text != REDACTED:
                parts.append(text)

    public = _drop_raw_content(_redact_public(dict(record)))
    if isinstance(public, Mapping):
        _walk(public)
    return " ".join(parts)


def resolve_current_store(
    *,
    cwd: Path | str | None = None,
    explicit_project_root: Path | str | None = None,
) -> tuple[StateRootResolution, ProjectStore]:
    resolution = resolve_state_root(
        cwd=cwd,
        explicit_project_root=explicit_project_root,
    )
    store = ProjectStore(
        project_key=resolution.project_key,
        state_dir=resolution.state_dir,
        project_root=resolution.project_root,
        source="current",
    )
    return resolution, store


def _central_parent(resolution: StateRootResolution) -> Path | None:
    if resolution.scope != "centralized":
        return None
    return resolution.state_dir.parent


def discover_stores(
    *,
    cwd: Path | str | None = None,
    explicit_project_root: Path | str | None = None,
    project: str = "current",
) -> tuple[StateRootResolution, list[ProjectStore]]:
    """Return confined stores. ``project=all`` requires explicit discovery."""

    scope = (project or "current").strip().lower() or "current"
    if scope not in {"current", "all"}:
        raise SessionIndexError(
            "project must be current or all",
            code="E_SESSION_PROJECT",
        )
    resolution, current = resolve_current_store(
        cwd=cwd,
        explicit_project_root=explicit_project_root,
    )
    stores = [current]
    if scope != "all":
        return resolution, stores

    seen: set[Path] = set()
    try:
        seen.add(current.state_dir.resolve())
    except OSError:
        seen.add(current.state_dir)

    def _add(
        state_dir: Path,
        *,
        project_key: str,
        project_root: Path | None,
        source: str,
    ) -> None:
        try:
            key = state_dir.resolve()
        except OSError:
            return
        if key in seen:
            return
        if not (state_dir / "state").is_dir():
            return
        seen.add(key)
        stores.append(
            ProjectStore(
                project_key=project_key,
                state_dir=state_dir,
                project_root=project_root,
                source=source,
            )
        )

    parent = _central_parent(resolution)
    if parent is not None and parent.is_dir() and not parent.is_symlink():
        try:
            children = list(parent.iterdir())
        except OSError:
            children = []
        for child in children:
            if not child.is_dir() or child.is_symlink():
                continue
            if _confine_under(parent, child) is None:
                continue
            _add(
                child,
                project_key=child.name,
                project_root=None,
                source="centralized_sibling",
            )

    known_path = current.state_dir / "state" / "known-roots.json"
    confined_known = _confine_under(current.state_dir, known_path)
    if confined_known is not None and confined_known.is_file() and parent is not None:
        try:
            parsed = json.loads(confined_known.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            rows = parsed.get("roots")
            if isinstance(rows, list):
                for row in rows[:64]:
                    if not isinstance(row, dict):
                        continue
                    key = str(row.get("project_key") or "").strip()
                    if not key or "/" in key or "\\" in key or key in {".", ".."}:
                        continue
                    _add(
                        parent / key,
                        project_key=key,
                        project_root=None,
                        source="known-roots",
                    )
    return resolution, stores


def _event_dirs(store: ProjectStore) -> list[Path]:
    dirs: list[Path] = []
    primary = store.state_dir / "state" / "events"
    if _confine_under(store.state_dir, primary) is not None or primary.parent.exists():
        dirs.append(primary)
    if store.project_root is not None:
        leftover = store.project_root / ".omg" / "state" / "events"
        leftover_root = store.project_root / ".omg"
        if _confine_under(leftover_root, leftover) is not None:
            try:
                if leftover.resolve() != primary.resolve():
                    dirs.append(leftover)
            except OSError:
                dirs.append(leftover)
    return dirs


def _append_diag(diagnostics: list[dict[str, Any]], row: dict[str, Any]) -> None:
    if len(diagnostics) >= MAX_DIAGNOSTICS:
        return
    diagnostics.append(row)


def _read_json_object(path: Path, *, root: Path) -> dict[str, Any] | None:
    confined = _confine_under(root, path)
    if confined is None or not confined.is_file():
        return None
    try:
        if confined.stat().st_size > MAX_JOURNAL_BYTES:
            return None
        parsed = json.loads(confined.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def iter_store_events(
    store: ProjectStore,
    *,
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Read append-only journals; skip unknown/corrupt rows with diagnostics."""

    rows: list[dict[str, Any]] = []
    diag = diagnostics if diagnostics is not None else []
    for directory in _event_dirs(store):
        confine_root = store.state_dir
        leftover_root = (
            (store.project_root / ".omg") if store.project_root is not None else None
        )
        try:
            if not directory.is_dir() or directory.is_symlink():
                continue
            files = sorted(directory.glob("*.jsonl"))
        except OSError as exc:
            _append_diag(
                diag,
                {
                    "reason": "unreadable_dir",
                    "source": directory.name,
                    "detail": type(exc).__name__,
                },
            )
            continue
        for path in files:
            confined = _confine_under(confine_root, path)
            if confined is None and leftover_root is not None:
                confined = _confine_under(leftover_root, path)
            if confined is None:
                _append_diag(diag, {"reason": "path_escaped", "source": path.name})
                continue
            try:
                if confined.is_symlink():
                    _append_diag(
                        diag, {"reason": "symlink_skipped", "source": confined.name}
                    )
                    continue
                size = confined.stat().st_size
            except OSError as exc:
                _append_diag(
                    diag,
                    {
                        "reason": "unreadable_file",
                        "source": confined.name,
                        "detail": type(exc).__name__,
                    },
                )
                continue
            try:
                if size > MAX_JOURNAL_BYTES:
                    with confined.open("rb") as handle:
                        handle.seek(max(0, size - MAX_JOURNAL_BYTES))
                        raw = handle.read(MAX_JOURNAL_BYTES)
                    newline = raw.find(b"\n")
                    if newline >= 0:
                        raw = raw[newline + 1 :]
                    _append_diag(
                        diag,
                        {
                            "reason": "journal_tail_truncated",
                            "source": confined.name,
                            "bytes": size,
                        },
                    )
                else:
                    raw = confined.read_bytes()
            except OSError as exc:
                _append_diag(
                    diag,
                    {
                        "reason": "unreadable_file",
                        "source": confined.name,
                        "detail": type(exc).__name__,
                    },
                )
                continue
            for index, line in enumerate(raw.splitlines(), start=1):
                if len(rows) >= MAX_EVENTS_PER_STORE:
                    _append_diag(
                        diag,
                        {"reason": "event_scan_truncated", "source": confined.name},
                    )
                    break
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    _append_diag(
                        diag,
                        {
                            "reason": "invalid_json",
                            "source": confined.name,
                            "line": index,
                        },
                    )
                    continue
                if not isinstance(parsed, dict):
                    _append_diag(
                        diag,
                        {
                            "reason": "unknown_record",
                            "source": confined.name,
                            "line": index,
                        },
                    )
                    continue
                event = dict(parsed)
                event.setdefault("_project_key", store.project_key)
                event.setdefault("_source_file", confined.name)
                event.setdefault("_source_line", index)
                event.setdefault("_byte_size", len(line))
                rows.append(event)
    rows.sort(
        key=lambda row: (
            str(row.get("observed_at") or ""),
            str(row.get("source") or ""),
            int(row["source_sequence"])
            if isinstance(row.get("source_sequence"), int)
            else 0,
            str(row.get("event_id") or ""),
        )
    )
    return rows


def _iter_run_summaries(store: ProjectStore) -> list[dict[str, Any]]:
    runs_dir = store.state_dir / "state" / "runs"
    confined = _confine_under(store.state_dir, runs_dir)
    if confined is None or not confined.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    try:
        children = list(confined.iterdir())
    except OSError:
        return []
    for child in children[:MAX_RUN_SCAN]:
        if not child.is_dir() or child.is_symlink():
            continue
        parsed = _read_json_object(child / "status.json", root=store.state_dir)
        if parsed is None:
            continue
        parsed.setdefault("run_id", child.name)
        parsed["_project_key"] = store.project_key
        parsed["_record_kind"] = "run_summary"
        rows.append(parsed)
    return rows


def _public_event(event: Mapping[str, Any], *, store: ProjectStore) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    host_ids = _extract_host_ids(event)
    provider = None
    team_id = None
    if isinstance(payload, Mapping):
        provider = payload.get("provider")
        team_id = payload.get("team_id")
    body = _drop_raw_content(
        _redact_public(
            {
                "event_id": event.get("event_id"),
                "event_type": event.get("event_type"),
                "source": event.get("source"),
                "run_id": event.get("run_id"),
                "session_id": event.get("session_id"),
                "observed_at": event.get("observed_at"),
                "provider": provider if provider is not None else event.get("provider"),
                "team_id": team_id if team_id is not None else event.get("team_id"),
                "host_ids": host_ids,
                "locality": "indexed",
                "project_key": store.project_key,
            }
        )
    )
    excerpt = _searchable_text(event)
    if len(excerpt) > MAX_EXCERPT_CHARS:
        excerpt = excerpt[:MAX_EXCERPT_CHARS]
    if isinstance(body, dict):
        body["excerpt"] = redact_home_paths(excerpt)
        return body
    return {"excerpt": redact_home_paths(excerpt)}


def _matches_query(event: Mapping[str, Any], query: str, *, case_sensitive: bool) -> bool:
    haystack = _searchable_text(event)
    needle = query if case_sensitive else query.lower()
    text = haystack if case_sensitive else haystack.lower()
    if not needle:
        return False
    if needle in {REDACTED.lower(), "[redacted]"}:
        return False
    return needle in text


def _filter_event(
    event: Mapping[str, Any],
    *,
    since: datetime | None,
    session_id: str | None,
    run_id: str | None,
    provider: str | None,
    team_id: str | None,
) -> bool:
    observed = _parse_timestamp(event.get("observed_at") or event.get("updated_at"))
    if since is not None and (observed is None or observed < since):
        return False
    if session_id:
        ids = {str(event.get("session_id") or "")}
        ids.update(_extract_host_ids(event))
        if session_id not in ids:
            return False
    if run_id and str(event.get("run_id") or "") != run_id:
        return False
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    if provider:
        got = payload.get("provider") if isinstance(payload, Mapping) else event.get("provider")
        if str(got or "") != provider:
            return False
    if team_id:
        got = payload.get("team_id") if isinstance(payload, Mapping) else event.get("team_id")
        if str(got or "") != team_id:
            return False
    return True


def _public_query(text: str) -> str:
    lowered = text.lower()
    if any(
        part in lowered
        for part in ("prompt", "transcript", "password", "secret", "token", "authorization")
    ):
        return REDACTED
    return redact_home_paths(text)


def search_sessions(
    query: str,
    *,
    cwd: Path | str | None = None,
    project: str = "current",
    since: str | None = None,
    limit: int = 50,
    context: int = 0,
    case_sensitive: bool = False,
    session_id: str | None = None,
    run_id: str | None = None,
    provider: str | None = None,
    team_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    text = (query or "").strip()
    if not text:
        raise SessionIndexError("query is required", code="E_SESSION_QUERY")
    cap = max(1, min(int(limit), MAX_RESULTS))
    ctx = max(0, min(int(context), MAX_CONTEXT))
    cutoff = parse_since(since, now=now) if since else None
    resolution, stores = discover_stores(cwd=cwd, project=project)
    diagnostics: list[dict[str, Any]] = []
    hits: list[dict[str, Any]] = []
    scanned = 0
    for store in stores:
        events = iter_store_events(store, diagnostics=diagnostics)
        summaries = _iter_run_summaries(store)
        combined: list[dict[str, Any]] = list(events)
        for summary in summaries:
            combined.append(
                {
                    "event_id": f"run-{summary.get('run_id')}",
                    "event_type": "run_summary",
                    "source": "run-status",
                    "run_id": summary.get("run_id"),
                    "session_id": summary.get("grok_session_id")
                    or summary.get("session_id"),
                    "observed_at": summary.get("updated_at") or summary.get("created_at"),
                    "payload": {
                        "mode": summary.get("mode"),
                        "status": summary.get("status"),
                        "provider": summary.get("provider"),
                        "team_id": summary.get("team_id"),
                        "grok_session_id": summary.get("grok_session_id"),
                    },
                    "_project_key": store.project_key,
                }
            )
        scanned += len(combined)
        for index, event in enumerate(combined):
            if len(hits) >= cap:
                break
            if not _filter_event(
                event,
                since=cutoff,
                session_id=session_id,
                run_id=run_id,
                provider=provider,
                team_id=team_id,
            ):
                continue
            if not _matches_query(event, text, case_sensitive=case_sensitive):
                continue
            neighbors: list[dict[str, Any]] = []
            if ctx:
                start = max(0, index - ctx)
                end = min(len(combined), index + ctx + 1)
                neighbors = [
                    _public_event(combined[i], store=store)
                    for i in range(start, end)
                    if i != index
                ]
            hit = _public_event(event, store=store)
            hit["context"] = neighbors
            hits.append(hit)
    return {
        "schema_version": SESSION_INDEX_SCHEMA,
        "command": "session.search",
        "query": _public_query(text),
        "project": project,
        "project_key": resolution.project_key,
        "since": since,
        "limit": cap,
        "case_sensitive": bool(case_sensitive),
        "scanned": scanned,
        "hits": hits,
        "diagnostics": diagnostics,
        "raw_content": False,
        "executed": False,
    }


def _dir_size_and_count(
    path: Path, *, root: Path, limit: int = MAX_ARTIFACT_SCAN
) -> tuple[int, int]:
    confined = _confine_under(root, path)
    if confined is None or not confined.is_dir():
        return 0, 0
    total = 0
    count = 0
    stack = [confined]
    while stack and count < limit:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if count >= limit:
                break
            if entry.is_symlink():
                continue
            try:
                if entry.is_dir():
                    if _confine_under(root, entry) is not None:
                        stack.append(entry)
                    continue
                if entry.is_file():
                    total += entry.stat().st_size
                    count += 1
            except OSError:
                continue
    return total, count


def _job_failure_count(project_root: Path | None) -> int:
    if project_root is None:
        return 0
    jobs = project_root / ".omg" / "jobs"
    root = project_root / ".omg"
    confined = _confine_under(root, jobs)
    if confined is None or not confined.is_dir():
        return 0
    failed = 0
    try:
        children = list(confined.iterdir())
    except OSError:
        return 0
    for child in children[:MAX_JOB_SCAN]:
        if not child.is_dir() or child.is_symlink():
            continue
        payload = _read_json_object(child / "job.json", root=root)
        if payload is None:
            continue
        if str(payload.get("state") or "") == "failed":
            failed += 1
    return failed


def friction_report(
    *,
    cwd: Path | str | None = None,
    since: str = "24h",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Metadata-only friction signals. No private content by default."""

    cutoff = parse_since(since, now=now)
    resolution, stores = discover_stores(cwd=cwd, project="current")
    store = stores[0]
    diagnostics: list[dict[str, Any]] = []
    events = [
        event
        for event in iter_store_events(store, diagnostics=diagnostics)
        if _filter_event(
            event,
            since=cutoff,
            session_id=None,
            run_id=None,
            provider=None,
            team_id=None,
        )
    ]
    large_events = 0
    errors = 0
    hook_timeouts = 0
    permission_blocks = 0
    idle_gaps = 0
    previous_ts: datetime | None = None
    for event in events:
        size = event.get("_byte_size")
        if isinstance(size, int) and size >= LARGE_EVENT_BYTES:
            large_events += 1
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        diagnostic = str(payload.get("diagnostic") or "")
        blob = f"{event_type} {diagnostic}".lower()
        if event_type in {"agent_failed"} or "error" in blob or "failed" in blob:
            errors += 1
        if "timeout" in blob:
            hook_timeouts += 1
        if "permission" in blob or "e_denied" in blob or "blocked_permission" in blob:
            permission_blocks += 1
        observed = _parse_timestamp(event.get("observed_at"))
        if previous_ts is not None and observed is not None:
            gap = (observed - previous_ts).total_seconds()
            if gap >= IDLE_GAP_SECONDS:
                idle_gaps += 1
        if observed is not None:
            previous_ts = observed

    rework = 0
    stale_leases = 0
    clock = now or _utc_now()
    for summary in _iter_run_summaries(store):
        phase = str(
            summary.get("phase") or summary.get("stage") or summary.get("status") or ""
        )
        if "rework" in phase.lower():
            rework += 1
        extra = summary.get("rework_count")
        if isinstance(extra, int) and not isinstance(extra, bool) and extra > 1:
            rework += extra
        lease = summary.get("execution_lease")
        lease_path = (
            store.state_dir
            / "state"
            / "runs"
            / str(summary.get("run_id") or "")
            / "execution.lease.json"
        )
        lease_obj = (
            lease
            if isinstance(lease, Mapping)
            else _read_json_object(lease_path, root=store.state_dir)
        )
        if isinstance(lease_obj, Mapping):
            if str(lease_obj.get("state") or "") == "released":
                continue
            acquired = _parse_timestamp(lease_obj.get("acquired_at"))
            pid = lease_obj.get("pid")
            live = isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            if live:
                try:
                    os.kill(int(pid), 0)
                except OSError:
                    live = False
                except Exception:
                    live = False
            stale_age = (
                acquired is not None
                and (clock - acquired).total_seconds() > STALE_LEASE_SECONDS
            )
            if not live or stale_age:
                stale_leases += 1

    artifact_root = (store.project_root or store.state_dir) / ".omg" / "artifacts"
    scan_root = (store.project_root / ".omg") if store.project_root else store.state_dir
    artifact_bytes, artifact_files = _dir_size_and_count(artifact_root, root=scan_root)
    high_artifact = artifact_bytes >= LARGE_ARTIFACT_BYTES or artifact_files >= 128
    failed_jobs = _job_failure_count(store.project_root)
    error_rate = (errors / len(events)) if events else 0.0
    signals = {
        "high_artifact_size": {
            "triggered": high_artifact,
            "bytes": artifact_bytes,
            "files": artifact_files,
        },
        "large_events": {"triggered": large_events > 0, "count": large_events},
        "error_rate": {
            "triggered": error_rate >= 0.25 and len(events) >= 4,
            "rate": round(error_rate, 4),
            "errors": errors,
            "events": len(events),
        },
        "idle_gaps": {"triggered": idle_gaps > 0, "count": idle_gaps},
        "failed_jobs": {"triggered": failed_jobs > 0, "count": failed_jobs},
        "hook_timeouts": {"triggered": hook_timeouts > 0, "count": hook_timeouts},
        "repeated_rework": {"triggered": rework > 1, "count": rework},
        "stale_leases": {"triggered": stale_leases > 0, "count": stale_leases},
        "permission_blocks": {
            "triggered": permission_blocks > 0,
            "count": permission_blocks,
        },
    }
    triggered = [name for name, row in signals.items() if row.get("triggered")]
    return {
        "schema_version": SESSION_INDEX_SCHEMA,
        "command": "session.friction",
        "since": since,
        "project_key": resolution.project_key,
        "private_content": False,
        "signals": signals,
        "triggered": triggered,
        "diagnostics": diagnostics,
    }


def _artifact_links(event: Mapping[str, Any], store: ProjectStore) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    run_id = event.get("run_id")
    if (
        isinstance(run_id, str)
        and run_id
        and run_id not in {".", ".."}
        and "/" not in run_id
        and "\\" not in run_id
    ):
        status = store.state_dir / "state" / "runs" / run_id / "status.json"
        exists = _confine_under(store.state_dir, status) is not None and status.is_file()
        links.append({"kind": "run_status", "run_id": run_id, "present": bool(exists)})
    session_id = event.get("session_id")
    if isinstance(session_id, str) and session_id:
        links.append({"kind": "host_session", "session_id": session_id, "present": True})
    return links


def session_timeline(
    *,
    cwd: Path | str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    cap = max(1, min(int(limit), MAX_RESULTS))
    resolution, stores = discover_stores(cwd=cwd, project="current")
    store = stores[0]
    diagnostics: list[dict[str, Any]] = []
    events = iter_store_events(store, diagnostics=diagnostics)
    selected: list[dict[str, Any]] = []
    for event in events:
        if run_id and str(event.get("run_id") or "") != run_id:
            continue
        if session_id:
            ids = {str(event.get("session_id") or "")}
            ids.update(_extract_host_ids(event))
            if session_id not in ids:
                continue
        public = _public_event(event, store=store)
        public["artifacts"] = _artifact_links(event, store)
        selected.append(public)
        if len(selected) >= cap:
            break
    return {
        "schema_version": SESSION_INDEX_SCHEMA,
        "command": "trace.timeline",
        "run_id": run_id,
        "session_id": session_id,
        "project_key": resolution.project_key,
        "count": len(selected),
        "events": selected,
        "diagnostics": diagnostics,
        "executed": False,
        "raw_content": False,
    }


def _cwd_safe_for_restore(
    cwd: Path, resolution: StateRootResolution
) -> tuple[bool, str]:
    try:
        here = cwd.resolve()
    except OSError:
        return False, "cwd_unresolvable"
    if _is_home(here) or _is_fs_root(here):
        return False, "cwd_is_home_or_root"
    try:
        root = resolution.project_root.resolve()
    except OSError:
        return False, "project_root_unresolvable"
    if here == root:
        return True, "project_root"
    worktrees = root / ".omg" / "worktrees"
    if path_is_under(here, worktrees):
        return True, "omg_worktree"
    return False, "cwd_not_project_worktree"


def replay_session(
    session_id: str,
    *,
    cwd: Path | str | None = None,
    summary: bool = True,
    restore_code: bool = False,
    operator_cwd: Path | str | None = None,
) -> dict[str, Any]:
    """Deterministic timeline replay. NEVER re-executes commands."""

    sid = (session_id or "").strip()
    if not sid:
        raise SessionIndexError("session id is required", code="E_SESSION_ID")
    resolution, stores = discover_stores(cwd=cwd, project="current")
    store = stores[0]
    diagnostics: list[dict[str, Any]] = []
    events = [
        event
        for event in iter_store_events(store, diagnostics=diagnostics)
        if sid == str(event.get("session_id") or "") or sid in _extract_host_ids(event)
    ]
    timeline = []
    run_ids: list[str] = []
    for event in events:
        public = _public_event(event, store=store)
        public["artifacts"] = _artifact_links(event, store)
        if not summary and isinstance(event.get("payload"), Mapping):
            public["payload_keys"] = sorted(
                str(key)
                for key in event["payload"]
                if str(key).lower().replace("-", "_") not in RAW_CONTENT_KEYS
            )
        timeline.append(public)
        run_id = event.get("run_id")
        if isinstance(run_id, str) and run_id and run_id not in run_ids:
            run_ids.append(run_id)

    here = Path(operator_cwd) if operator_cwd is not None else Path.cwd()
    restore_code_body: dict[str, Any] = {
        "requested": bool(restore_code),
        "executed": False,
        "status": "not_requested",
    }
    if restore_code:
        safe, reason = _cwd_safe_for_restore(here, resolution)
        if not safe:
            raise SessionIndexError(
                f"restore-code refused: unsafe cwd/worktree ({reason})",
                code="E_RESTORE_CODE_UNSAFE",
            )
        restore_code_body = {
            "requested": True,
            "executed": False,
            "status": "refused",
            "code": "E_RESTORE_CODE_NOT_EXECUTED",
            "reason": "replay_never_mutates_tree",
        }

    host_ids: list[str] = []
    for event in events:
        for item in _extract_host_ids(event):
            if item not in host_ids:
                host_ids.append(item)
    if sid not in host_ids:
        host_ids.insert(0, sid)

    return {
        "schema_version": SESSION_INDEX_SCHEMA,
        "command": "session.replay",
        "session_id": sid,
        "summary": bool(summary),
        "executed": False,
        "commands_run": [],
        "timeline": timeline,
        "outcomes": {
            "resume_conversation": {
                "status": "available" if events else "unavailable",
                "host_ids": host_ids,
                "note": "Attach/resume the host UUID; do not replay the transcript.",
            },
            "restore_code": restore_code_body,
            "reconcile_omg_run": {
                "status": "available" if run_ids else "unavailable",
                "run_ids": run_ids,
                "note": "Use omg state / omg resume; replay does not mutate run status.",
            },
            "restore_tmux": {
                "status": "unavailable",
                "executed": False,
                "reason": "replay_does_not_reattach_tmux",
            },
        },
        "diagnostics": diagnostics,
        "raw_content": False,
        "project_key": resolution.project_key,
    }


def observatory(
    *,
    cwd: Path | str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Operator snapshot. Reuses HUD reads; does not add a second slow scan."""

    from omg_cli.hud import collect_hud_snapshot

    root = Path(cwd) if cwd is not None else Path.cwd()
    snapshot = collect_hud_snapshot(root, run_id)
    run = snapshot.get("run") if isinstance(snapshot.get("run"), Mapping) else {}
    team = snapshot.get("team") if isinstance(snapshot.get("team"), Mapping) else {}
    status = str(run.get("status") or "")
    verified = bool(run.get("verified"))
    mode = str(run.get("mode") or "")
    blocked = int(team.get("blocked_count") or 0)
    failed = status in {"failed", "error"} or blocked > 0
    iteration = run.get("iteration") or run.get("iterations_completed")
    max_iter = run.get("max_iter")
    retry_budget: dict[str, Any] = {"status": "unknown"}
    if isinstance(max_iter, int) and not isinstance(max_iter, bool):
        used = (
            iteration
            if isinstance(iteration, int) and not isinstance(iteration, bool)
            else 0
        )
        retry_budget = {
            "status": "available",
            "max_iter": max_iter,
            "used": used,
            "remaining": max(0, max_iter - used),
        }
    if verified:
        next_action = "done (verified)"
    elif status == "cancelled":
        next_action = "none (cancelled)"
    elif failed:
        next_action = "inspect omg session friction / logs, then fix or omg cancel"
    elif mode == "ulw":
        next_action = "omg integrate (if envelopes) → omg accept"
    elif mode == "ralph":
        next_action = "omg ralph --resume <run>" if run.get("run_id") else "omg ralph"
    elif not run.get("found"):
        next_action = "start a run or omg resume"
    else:
        next_action = "omg state --human"
    goal = snapshot.get("goal") or run.get("goal") or ""
    if isinstance(goal, str) and len(goal) > 120:
        goal = goal[:117] + "..."
    return {
        "schema_version": SESSION_INDEX_SCHEMA,
        "command": "session.observatory",
        "hud_reused": True,
        "run": {
            "found": bool(run.get("found")),
            "run_id": run.get("run_id"),
            "mode": mode or None,
            "status": status or None,
            "goal": redact_home_paths(str(goal)) if goal else None,
            "verified": verified,
        },
        "jobs_team": {
            "task_count": team.get("task_count", 0),
            "active_count": team.get("active_count", 0),
            "blocked_count": blocked,
            "transport": team.get("transport"),
        },
        "blocked_or_failure": failed,
        "retry_budget": retry_budget,
        "next_operator_action": next_action,
        "stale": bool(snapshot.get("stale")),
        "partial": bool(snapshot.get("partial")),
        "authoritative": False,
        "read_only": True,
    }


def _iter_retain_targets(store: ProjectStore) -> Iterable[Path]:
    for directory_name in ("events",):
        directory = store.state_dir / "state" / directory_name
        confined = _confine_under(store.state_dir, directory)
        if confined is None or not confined.is_dir():
            continue
        try:
            entries = list(confined.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink() or not entry.is_file():
                continue
            if _confine_under(store.state_dir, entry) is None:
                continue
            yield entry


def retain_events(
    *,
    cwd: Path | str | None = None,
    since: str,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Retention for this project's state_root only. Default is dry-run."""

    cutoff = parse_since(since, now=now)
    resolution, stores = discover_stores(cwd=cwd, project="current")
    store = stores[0]
    current = now or _utc_now()
    planned: list[dict[str, Any]] = []
    deleted = 0
    for path in _iter_retain_targets(store):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        row = {
            "path": _public_rel(path, store.state_dir),
            "mtime": mtime.isoformat().replace("+00:00", "Z"),
            "action": "delete" if apply else "would_delete",
        }
        planned.append(row)
        if apply:
            try:
                path.unlink()
                deleted += 1
            except OSError:
                row["action"] = "delete_failed"
    return {
        "schema_version": SESSION_INDEX_SCHEMA,
        "command": "session.retain",
        "dry_run": not apply,
        "apply": bool(apply),
        "since": since,
        "cutoff": cutoff.isoformat().replace("+00:00", "Z"),
        "project_key": resolution.project_key,
        "scope": "current_state_root_only",
        "candidates": planned,
        "deleted": deleted if apply else 0,
        "now": current.isoformat().replace("+00:00", "Z"),
    }


__all__ = [
    "MAX_RESULTS",
    "ProjectStore",
    "SESSION_INDEX_SCHEMA",
    "SessionIndexError",
    "discover_stores",
    "friction_report",
    "iter_store_events",
    "observatory",
    "parse_since",
    "redact_home_paths",
    "replay_session",
    "retain_events",
    "search_sessions",
    "session_timeline",
]
