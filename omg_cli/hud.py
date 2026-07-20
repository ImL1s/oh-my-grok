"""Lightweight HUD / statusline text for active OMG runs (research lifestyle surface).

Not a host-rendered TUI chrome — prints a one-line or short multi-line pack
agents and humans can paste into terminals.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from omg_cli.resume import build_resume_pack, resolve_run
from omg_cli.state import load_run_view


def hud_line(root: Path, run_id: str | None = None) -> str:
    """Single-line HUD: mode|status|stage|run|verified."""
    status = resolve_run(root, run_id)
    if status is None:
        return "omg-hud: no-active-run"
    rid = str(status.get("run_id") or "?")[:12]
    mode = status.get("mode") or "-"
    st = status.get("status") or "-"
    stage = status.get("stage") or status.get("phase") or "-"
    ver = "V" if status.get("verified") else "-"
    return f"omg-hud: {mode}|{st}|{stage}|run={rid}|{ver}"


def hud_pack(root: Path, run_id: str | None = None) -> dict[str, Any]:
    pack = build_resume_pack(root, run_id)
    pack["line"] = hud_line(root, run_id)
    if pack.get("ok") and pack.get("run_id"):
        view = load_run_view(Path(root), str(pack["run_id"]))
        if view:
            pack["schema_classification"] = view.get("schema_classification")
    return pack


__all__ = ["hud_line", "hud_pack"]
