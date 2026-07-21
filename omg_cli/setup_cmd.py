# omg_cli/setup_cmd.py
"""omg setup — ensure project dirs, merge AGENTS + gitignore fragments."""
from __future__ import annotations

import sys
from pathlib import Path

from omg_cli.state import ensure_omg_dirs

OMG_START = "<!-- OMG:START -->"
OMG_END = "<!-- OMG:END -->"
GITIGNORE_MARKER = "# oh-my-grok"


def plugin_root() -> Path:
    """Repo / plugin root (parent of omg_cli package)."""
    return Path(__file__).resolve().parents[1]


def _templates_dir() -> Path:
    return plugin_root() / "templates"


def _read_template(name: str) -> str:
    path = _templates_dir() / name
    if not path.is_file():
        raise FileNotFoundError(f"missing template: {path}")
    return path.read_text(encoding="utf-8")


def merge_agents_fragment(project_root: Path) -> str:
    """Write or merge AGENTS.fragment.md into project AGENTS.md.

    Returns action: 'created' | 'appended' | 'unchanged'.
    """
    fragment = _read_template("AGENTS.fragment.md").rstrip() + "\n"
    # Ensure markers wrap fragment for idempotent merge
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

    # Append marker block
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
    # Idempotent: if marker present or all key lines already ignored, skip
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


def run_setup(
    project_root: Path | None = None, *, install_rules: bool = True
) -> int:
    from omg_cli.compat import format_isolation_banner

    root = Path(project_root or Path.cwd()).resolve()
    ensure_omg_dirs(root)

    agents_action = merge_agents_fragment(root)
    gi_action = merge_gitignore_fragment(root)

    print(f"oh-my-grok setup complete in {root}")
    print(f"  .omg/ dirs: ensured")
    print(f"  AGENTS.md: {agents_action}")
    print(f"  .gitignore: {gi_action}")

    if install_rules:
        try:
            from omg_cli.guidance import GuidanceError, install_global_rules

            rpath, raction = install_global_rules()
            print(f"  {rpath}: {raction}")
        except GuidanceError as e:
            print(f"  global rules: SKIPPED ({e})")  # never crash setup

    print()
    print("Next: install the Grok Build plugin from this repo:")
    print()
    print(f"  cd {plugin_root()}")
    print("  grok plugin install . --trust")
    print()
    print("Global guidance (~/.grok/rules/omg.md) is installed and loads every")
    print("Grok session (skip with: omg setup --no-global-rules).")
    print()
    print("Then verify:")
    print("  omg doctor")
    print()
    # Always print isolation banner after success (compat.claude C1)
    print(format_isolation_banner())
    return 0


def main(argv: list[str] | None = None) -> int:
    _ = argv  # no flags yet
    return run_setup()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
