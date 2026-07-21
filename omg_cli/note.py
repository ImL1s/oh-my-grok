"""omg note — compaction-resistant project notepad under ``.omg/notepad.md``."""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


HEADER = "# OMG notepad\n\n"

_SEVEN_DAYS = timedelta(days=7)
_NOTE_LINE_RE = re.compile(r"^- \[(7d|permanent)\]\s+(\S+)(?:\s|$)")


def notepad_path(root: Path) -> Path:
    return Path(root) / ".omg" / "notepad.md"


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
    try:
        from omg_cli.state import ensure_omg_dirs

        ensure_omg_dirs(root)
    except Exception:
        (root / ".omg").mkdir(parents=True, exist_ok=True)

    path = notepad_path(root)
    if not path.is_file():
        path.write_text(HEADER, encoding="utf-8")

    ttl = "permanent" if priority else "7d"
    line = f"- [{ttl}] {_utc_now()} {text.rstrip()}\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return path


def read_notes(root: Path) -> str:
    path = notepad_path(root)
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
        # kind == "7d"
        parsed = _parse_iso(ts)
        if parsed is None:
            kept_lines.append(line)  # fail-safe: keep unparseable
            continue
        if (now - parsed) > _SEVEN_DAYS:
            removed += 1
            continue
        kept_lines.append(line)

    out = "\n".join(kept_lines)
    if kept_lines or raw.endswith("\n"):
        if not out.endswith("\n"):
            out += "\n"

    # Atomic rewrite
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(out, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    return (len(kept_lines), removed)


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
