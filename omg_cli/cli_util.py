"""Shared CLI adapter helpers (command-layer only; no business FSMs).

Extracted for #29 family modules so handlers do not import ``main``.
"""

from __future__ import annotations

import json
from pathlib import Path


def project_root() -> Path:
    """Canonical project root for command handlers."""
    from omg_cli.project_root import project_root as _resolve

    return _resolve()


def read_json_path(path: Path | str, *, label: str) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not readable JSON: {exc}") from exc


def write_json_path(path: Path | str, value: object) -> Path:
    from omg_cli.contracts.writer_chain import canonical_json_bytes

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(canonical_json_bytes(value))
    return target


def notification_config(path: str | None) -> dict:
    from omg_cli.notify import disabled_notification_config, load_notification_config

    if path is None:
        default = project_root() / ".omg" / "notifications.json"
        if not default.is_file():
            return disabled_notification_config()
        path = str(default)
    return load_notification_config(path)


__all__ = [
    "notification_config",
    "project_root",
    "read_json_path",
    "write_json_path",
]
