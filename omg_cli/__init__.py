"""oh-my-grok CLI package."""
from __future__ import annotations

import json
from pathlib import Path


def _load_version() -> str:
    plugin = Path(__file__).resolve().parents[1] / "plugin.json"
    try:
        data = json.loads(plugin.read_text(encoding="utf-8"))
        ver = str(data.get("version", "")).strip()
        return ver or "0.0.0"
    except (OSError, json.JSONDecodeError, TypeError):
        return "0.0.0"


__version__ = _load_version()
