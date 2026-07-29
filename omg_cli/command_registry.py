"""Authoritative top-level command inventory (#29).

``KNOWN_SUBCOMMANDS`` is the single source for host-launch recognition and
madmax intercept. ``build_parser()`` in ``main.py`` must register the same
names — enforced by ``tests/test_host_launcher.py`` / ``test_command_registry``.

Phase 1: inventory (name/help/family).
Phase 2: handlers live under ``omg_cli/commands/<family>.py``.
Phase 4′: inspect + run + memory families register parsers via
``register_*_parsers`` (``note`` still early in main; other families too).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Minimal command metadata (Phase 1 — inventory only)."""

    name: str
    help: str
    family: str  # install|run|memory|workflow|team|modes|inspect|mcp


# Order matches historical help / docs tables where practical.
COMMAND_SPECS: Final[tuple[CommandSpec, ...]] = (
    CommandSpec("setup", "scaffold .omg + install rules/hooks", "install"),
    CommandSpec("doctor", "health + drift checks", "install"),
    CommandSpec("update", "refresh plugin / guidance", "install"),
    CommandSpec("uninstall", "remove install artifacts", "install"),
    CommandSpec("install-hook", "install global PreToolUse hook", "install"),
    CommandSpec("note", "append project note", "memory"),
    CommandSpec("state", "active run status", "run"),
    CommandSpec("cancel", "abort active run", "run"),
    CommandSpec("resume", "print / clear RESUME.md", "run"),
    CommandSpec("session", "session recovery helpers", "run"),
    CommandSpec("recover", "bounded recovery", "run"),
    CommandSpec("memory", "project memory", "memory"),
    CommandSpec("tracker", "lifecycle tracker", "memory"),
    CommandSpec("compact", "compaction helpers", "memory"),
    CommandSpec("notify", "notification channels", "inspect"),
    CommandSpec("native-status", "native host status pack", "inspect"),
    CommandSpec("workflow", "repository workflows", "workflow"),
    CommandSpec("capabilities", "capabilities lock surface", "inspect"),
    CommandSpec("parity", "parity matrix", "inspect"),
    CommandSpec("wiki", "project wiki", "inspect"),
    CommandSpec("hud", "one-line HUD", "inspect"),
    CommandSpec("lsp", "host-owned .lsp.json inspection", "inspect"),
    CommandSpec("interview", "deep-interview gate", "workflow"),
    CommandSpec("goal", "ultragoal ledger", "workflow"),
    CommandSpec("accept", "acceptance + verified stamp", "team"),
    CommandSpec("integrate", "worktree integrate", "team"),
    CommandSpec("worker", "ULW worker ownership", "team"),
    CommandSpec("team", "experimental tmux team", "team"),
    CommandSpec("review", "structured dual review", "modes"),
    CommandSpec("qa", "ultraqa freeze/run", "modes"),
    CommandSpec("autopilot", "strict phase FSM", "modes"),
    CommandSpec("ulw", "ultrawork fan-out", "modes"),
    CommandSpec("ralph", "ralph persistence loop", "modes"),
    CommandSpec("ralplan", "ralplan consensus", "modes"),
    CommandSpec("ask", "human-only external advisor broker", "modes"),
    CommandSpec("pipeline", "plan→implement→verify FSM", "modes"),
    CommandSpec("dual-review", "critic then verifier", "modes"),
    CommandSpec("mcp-server", "stdio MCP server", "mcp"),
    CommandSpec("mcp-install", "install MCP registration", "mcp"),
)

KNOWN_SUBCOMMANDS: Final[frozenset[str]] = frozenset(s.name for s in COMMAND_SPECS)

# Marker for generated inventory fragment (docs/cli-commands.md).
INVENTORY_START = "<!-- OMG:CLI-COMMANDS:START -->"
INVENTORY_END = "<!-- OMG:CLI-COMMANDS:END -->"


def command_names() -> tuple[str, ...]:
    """Stable ordered top-level command names."""
    return tuple(s.name for s in COMMAND_SPECS)


def render_inventory_markdown() -> str:
    """Render the authoritative command inventory table (#29 Phase 4)."""
    lines = [
        "| Command | Family | Summary |",
        "|---------|--------|---------|",
    ]
    for spec in COMMAND_SPECS:
        lines.append(f"| `{spec.name}` | {spec.family} | {spec.help} |")
    return "\n".join(lines) + "\n"


def inventory_fragment() -> str:
    """Full marker-bounded fragment for docs embedding."""
    return (
        f"{INVENTORY_START}\n"
        f"{render_inventory_markdown()}"
        f"{INVENTORY_END}\n"
    )


__all__ = [
    "COMMAND_SPECS",
    "CommandSpec",
    "INVENTORY_END",
    "INVENTORY_START",
    "KNOWN_SUBCOMMANDS",
    "command_names",
    "inventory_fragment",
    "render_inventory_markdown",
]
