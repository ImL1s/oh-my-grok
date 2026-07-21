"""omg note — compaction-resistant project notepad under ``.omg/notepad.md``."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


HEADER = "# OMG notepad\n\n"


def notepad_path(root: Path) -> Path:
    return Path(root) / ".omg" / "notepad.md"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def run_note(
    argv_text: str,
    *,
    root: Path | None = None,
    priority: bool = False,
    show: bool = False,
) -> int:
    root = Path(root) if root is not None else Path.cwd()
    text = (argv_text or "").strip()
    if show or not text:
        sys_print = read_notes(root)
        print(sys_print, end="" if sys_print.endswith("\n") or not sys_print else "\n")
        return 0
    path = add_note(root, text, priority=priority)
    ttl = "permanent" if priority else "7d"
    print(f"noted ({ttl}): {path}")
    return 0
