"""#29 Phase 4: docs inventory stays in sync with COMMAND_SPECS."""

from __future__ import annotations

import re
from pathlib import Path

from omg_cli.command_registry import (
    COMMAND_SPECS,
    INVENTORY_END,
    INVENTORY_START,
    command_names,
    render_inventory_markdown,
)
from omg_cli.main import KNOWN_SUBCOMMANDS, build_parser

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "cli-commands.md"


def test_cli_commands_doc_markers_and_table() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert INVENTORY_START in text
    assert INVENTORY_END in text
    m = re.search(
        re.escape(INVENTORY_START) + r"(.*?)" + re.escape(INVENTORY_END),
        text,
        re.DOTALL,
    )
    assert m is not None
    body = m.group(1).strip()
    assert body == render_inventory_markdown().strip()


def test_registry_names_match_parser_and_doc() -> None:
    names = set(command_names())
    assert names == set(KNOWN_SUBCOMMANDS)
    # Parser top-level choices
    parser = build_parser()
    choices: set[str] = set()
    for action in parser._actions:
        raw = getattr(action, "choices", None)
        if isinstance(raw, dict) and "setup" in raw and "doctor" in raw:
            choices = set(raw)
            break
    assert choices == names
    # Doc lists every name as `name`
    text = DOC.read_text(encoding="utf-8")
    for name in command_names():
        assert f"`{name}`" in text, name


def test_generate_script_check_mode() -> None:
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_cli_commands_doc.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout.lower() or "ok" in proc.stderr.lower()


def test_families_cover_all_specs() -> None:
    families = {s.family for s in COMMAND_SPECS}
    expected = {
        "install",
        "run",
        "memory",
        "workflow",
        "team",
        "modes",
        "inspect",
        "mcp",
    }
    assert families == expected
