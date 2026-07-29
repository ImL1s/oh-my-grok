"""Shared command context (#29 Phase 3).

Parsed once after argparse; available on ``args.omg_ctx`` for handlers.
Enables global output mode for #30 without nested parsers resetting flags.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

OutputMode = Literal["human", "json"]


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Cross-cutting options for one CLI invocation."""

    command: str
    root: Path | None
    safe: bool
    yolo: bool
    output: OutputMode

    @property
    def wants_json(self) -> bool:
        return self.output == "json"


def resolve_output_mode(args: argparse.Namespace) -> OutputMode:
    """Prefer explicit --json / --human; default remains human."""
    json_flag = bool(getattr(args, "json_output", False) or getattr(args, "json", False))
    human_flag = bool(getattr(args, "human_output", False) or getattr(args, "human", False))
    # Command-local --json (e.g. hud) still counts when global not set.
    if json_flag and human_flag:
        # Caller should have rejected via parser.error; defensive default.
        return "human"
    if json_flag:
        return "json"
    return "human"


def attach_command_context(
    args: argparse.Namespace,
    *,
    root: Path | None,
) -> CommandContext:
    """Build context and attach to ``args.omg_ctx``."""
    ctx = CommandContext(
        command=str(getattr(args, "command", "") or ""),
        root=root,
        safe=bool(getattr(args, "safe", False)),
        yolo=bool(getattr(args, "yolo", False)),
        output=resolve_output_mode(args),
    )
    args.omg_ctx = ctx
    return ctx


def get_context(args: argparse.Namespace) -> CommandContext | None:
    ctx = getattr(args, "omg_ctx", None)
    return ctx if isinstance(ctx, CommandContext) else None


__all__ = [
    "CommandContext",
    "OutputMode",
    "attach_command_context",
    "get_context",
    "resolve_output_mode",
]
