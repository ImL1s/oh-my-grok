"""omg note — compaction-resistant project notepad under ``.omg/notepad.md``."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
)
from omg_cli.contracts.writer_chain import sha256_hex
from omg_cli.redaction import redact_text


HEADER = "# OMG notepad\n\n"

_SEVEN_DAYS = timedelta(days=7)
_NOTE_LINE_RE = re.compile(r"^- \[(7d|permanent)\]\s+(\S+)(?:\s|$)")


def notepad_path(root: Path) -> Path:
    return Path(root) / ".omg" / "notepad.md"


def _notepad_lock(root: Path) -> Path:
    return Path(root) / ".omg" / "notepad.lock"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime | None:
    """Parse ISO-8601 timestamp; return None if unparseable (fail-safe keep)."""
    raw = (ts or "").strip()
    if not raw:
        return None
    # Python <3.11-friendly Z suffix; 3.11+ accepts Z in some builds.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def add_note(root: Path, text: str, *, priority: bool = False) -> Path:
    """Append a durable note line. Create file + header if missing."""
    root = Path(root)
    path = notepad_path(root)
    ensure_managed_dir(path.parent)
    safe_text = redact_text(str(text).rstrip()).replace("\r", " ").replace("\n", " ")
    ttl = "permanent" if priority else "7d"
    line = f"- [{ttl}] {_utc_now()} {safe_text}\n"
    with exclusive_lock(_notepad_lock(root)):
        current = path.read_text(encoding="utf-8") if path.is_file() else HEADER
        if current and not current.endswith("\n"):
            current += "\n"
        atomic_write_bytes(
            path,
            (current + line).encode("utf-8"),
            mode=DATA_FILE_MODE,
            replace=True,
        )
    return path


def read_notes(root: Path) -> str:
    path = notepad_path(root)
    if not path.is_file():
        return ""
    with exclusive_lock(_notepad_lock(root)):
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")


def prune_notes(root: Path, *, now_iso: str | None = None) -> tuple[int, int]:
    """Drop ``[7d]`` note lines older than 7 days; keep permanent and rest.

    Returns ``(kept, removed)`` line counts after rewrite.
    Unparseable timestamps on ``[7d]`` lines are kept (fail-safe).
    """
    root = Path(root)
    path = notepad_path(root)
    if not path.is_file():
        return (0, 0)

    now = _parse_iso(now_iso) if now_iso else datetime.now(timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    ensure_managed_dir(path.parent)
    with exclusive_lock(_notepad_lock(root)):
        raw = path.read_text(encoding="utf-8")
        lines = raw.splitlines()

        kept_lines: list[str] = []
        removed = 0
        for line in lines:
            m = _NOTE_LINE_RE.match(line)
            if m is None:
                kept_lines.append(line)
                continue
            kind, ts = m.group(1), m.group(2)
            if kind == "permanent":
                kept_lines.append(line)
                continue
            parsed = _parse_iso(ts)
            if parsed is None or (now - parsed) <= _SEVEN_DAYS:
                kept_lines.append(line)
                continue
            removed += 1

        out = "\n".join(kept_lines)
        if kept_lines or raw.endswith("\n"):
            if not out.endswith("\n"):
                out += "\n"
        atomic_write_bytes(
            path,
            out.encode("utf-8"),
            mode=DATA_FILE_MODE,
            replace=True,
        )

    return (len(kept_lines), removed)


def export_notes(root: Path) -> dict[str, str | int]:
    content = read_notes(root)
    safe = redact_text(content)
    return {
        "store_kind": "project_notepad_export",
        "schema_version": 1,
        "content": safe,
        "sha256": sha256_hex(safe.encode("utf-8")),
    }


def import_notes(root: Path, bundle: dict) -> dict[str, str | int]:
    if set(bundle) != {"store_kind", "schema_version", "content", "sha256"}:
        raise ValueError("notepad export keys mismatch")
    if bundle["store_kind"] != "project_notepad_export" or bundle["schema_version"] != 1:
        raise ValueError("notepad export header mismatch")
    content = bundle["content"]
    if not isinstance(content, str):
        raise ValueError("notepad export content must be text")
    safe = redact_text(content)
    digest = sha256_hex(safe.encode("utf-8"))
    if digest != bundle["sha256"]:
        raise ValueError("notepad export hash mismatch")
    path = notepad_path(root)
    ensure_managed_dir(path.parent)
    with exclusive_lock(_notepad_lock(root)):
        atomic_write_bytes(
            path,
            safe.encode("utf-8"),
            mode=DATA_FILE_MODE,
            replace=True,
        )
    return {**bundle, "content": safe, "sha256": digest}


def run_note(
    argv_text: str,
    *,
    root: Path | None = None,
    priority: bool = False,
    show: bool = False,
    prune: bool = False,
) -> int:
    root = Path(root) if root is not None else Path.cwd()
    if prune:
        kept, removed = prune_notes(root)
        print(f"pruned: removed {removed}, kept {kept}")
        return 0
    text = (argv_text or "").strip()
    if show or not text:
        sys_print = read_notes(root)
        print(sys_print, end="" if sys_print.endswith("\n") or not sys_print else "\n")
        return 0
    path = add_note(root, text, priority=priority)
    ttl = "permanent" if priority else "7d"
    print(f"noted ({ttl}): {path}")
    return 0
