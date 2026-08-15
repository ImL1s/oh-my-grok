"""Windows-safe AGENTS.md / .gitignore fragment merges used by setup and #77."""

from __future__ import annotations

from pathlib import Path

OMG_START = "<!-- OMG:START -->"
OMG_END = "<!-- OMG:END -->"
GITIGNORE_MARKER = "# oh-my-grok"


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read_template(name: str) -> str:
    path = _plugin_root() / "templates" / name
    if not path.is_file():
        raise FileNotFoundError(f"missing template: {path}")
    return path.read_text(encoding="utf-8")


def merge_agents_fragment(project_root: Path) -> str:
    """Write or merge AGENTS.fragment.md into project AGENTS.md.

    Returns action: 'created' | 'appended' | 'unchanged'.
    """
    fragment = _read_template("AGENTS.fragment.md").rstrip() + "\n"
    if OMG_START not in fragment:
        fragment = f"{OMG_START}\n{fragment}{OMG_END}\n"
    elif OMG_END not in fragment:
        fragment = fragment.rstrip() + f"\n{OMG_END}\n"

    agents_path = project_root / "AGENTS.md"
    if not agents_path.exists():
        agents_path.write_text(fragment, encoding="utf-8")
        return "created"

    existing = agents_path.read_text(encoding="utf-8")
    if OMG_START in existing:
        return "unchanged"

    sep = "" if existing.endswith("\n") else "\n"
    agents_path.write_text(existing + sep + "\n" + fragment, encoding="utf-8")
    return "appended"


def merge_gitignore_fragment(project_root: Path) -> str:
    """Write or merge gitignore fragment. Returns action string."""
    fragment = _read_template("gitignore.fragment").rstrip() + "\n"
    gi_path = project_root / ".gitignore"

    if not gi_path.exists():
        body = fragment
        if GITIGNORE_MARKER not in body:
            body = f"{GITIGNORE_MARKER}\n{body}"
        gi_path.write_text(body, encoding="utf-8")
        return "created"

    existing = gi_path.read_text(encoding="utf-8")
    if GITIGNORE_MARKER in existing:
        return "unchanged"
    key_lines = [
        ln.strip()
        for ln in fragment.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if key_lines and all(any(kl in line for line in existing.splitlines()) for kl in key_lines):
        return "unchanged"

    sep = "" if existing.endswith("\n") else "\n"
    block = fragment
    if GITIGNORE_MARKER not in block:
        block = f"{GITIGNORE_MARKER}\n{block}"
    gi_path.write_text(existing + sep + "\n" + block, encoding="utf-8")
    return "appended"
