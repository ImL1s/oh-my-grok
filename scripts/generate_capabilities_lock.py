#!/usr/bin/env python3
"""Generate / check omg_capabilities.lock.json for the LOCAL CHECKOUT.

Hashes skills/omg-*/SKILL.md and agents/omg-*.md under the repo (or --root)
and writes/checks omg_capabilities.lock.json. This is a commit-hygiene / CI
guard: it catches uncommitted or unregenerated local skill/agent edits against
the committed lock. Installed frozen-snapshot drift (under
~/.grok/installed-plugins) is checked separately by doctor
``check_installed_capabilities_lock`` via ``compute_lock_for``.

Usage:
  python3 scripts/generate_capabilities_lock.py          # rewrite lock
  python3 scripts/generate_capabilities_lock.py --check   # exit 1 if stale
  python3 scripts/generate_capabilities_lock.py --root PATH
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


LOCK_NAME = "omg_capabilities.lock.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _capability_files(root: Path) -> list[Path]:
    """Return sorted absolute paths for skills/omg-*/SKILL.md and agents/omg-*.md."""
    root = Path(root)
    found: list[Path] = []
    skills = root / "skills"
    if skills.is_dir():
        for child in sorted(skills.iterdir()):
            if not child.is_dir() or not child.name.startswith("omg-"):
                continue
            skill = child / "SKILL.md"
            if skill.is_file():
                found.append(skill)
    agents = root / "agents"
    if agents.is_dir():
        for child in sorted(agents.iterdir()):
            if child.is_file() and child.name.startswith("omg-") and child.suffix == ".md":
                found.append(child)
    # Sort by repo-relative posix path
    found.sort(key=lambda p: p.relative_to(root).as_posix())
    return found


def _plugin_version(root: Path) -> str:
    path = Path(root) / "plugin.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("version") or "0")
    except (OSError, json.JSONDecodeError, TypeError):
        return "0"


def compute_lock_for(root: Path) -> dict[str, Any]:
    """Hash skills/omg-*/SKILL.md + agents/omg-*.md under an arbitrary root.

    Used for both the local checkout (commit-hygiene) and the installed frozen
    snapshot under ~/.grok/installed-plugins (OMX-parity installed-drift).
    """
    root = Path(root).resolve()
    files: dict[str, str] = {}
    for path in _capability_files(root):
        rel = path.relative_to(root).as_posix()
        files[rel] = _sha256_file(path)
    lines = [f"{rel}:{files[rel]}" for rel in sorted(files)]
    aggregate = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return {
        "version": _plugin_version(root),
        "files": files,
        "aggregate": aggregate,
    }


def compute_lock(root: Path) -> dict[str, Any]:
    """Compute capabilities lock dict for *root* (plugin / working tree)."""
    return compute_lock_for(root)


def read_lock(root: Path) -> dict[str, Any] | None:
    """Load on-disk lock or return None if missing/unreadable."""
    path = Path(root) / LOCK_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def write_lock(root: Path) -> Path:
    """Write omg_capabilities.lock.json at *root*; return path."""
    root = Path(root)
    lock = compute_lock(root)
    path = root / LOCK_NAME
    path.write_text(
        json.dumps(lock, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _diff_lock(stored: dict[str, Any], current: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if stored.get("version") != current.get("version"):
        lines.append(
            f"version: stored={stored.get('version')!r} current={current.get('version')!r}"
        )
    if stored.get("aggregate") != current.get("aggregate"):
        lines.append(
            f"aggregate: stored={stored.get('aggregate')} current={current.get('aggregate')}"
        )
    s_files = stored.get("files") if isinstance(stored.get("files"), dict) else {}
    c_files = current.get("files") if isinstance(current.get("files"), dict) else {}
    all_keys = sorted(set(s_files) | set(c_files))
    for key in all_keys:
        s = s_files.get(key)
        c = c_files.get(key)
        if s is None:
            lines.append(f"+ {key} (new, {c})")
        elif c is None:
            lines.append(f"- {key} (removed, was {s})")
        elif s != c:
            lines.append(f"~ {key}\n    stored:  {s}\n    current: {c}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate or check omg_capabilities.lock.json for the local checkout "
            "(commit-hygiene / CI guard on skills+agents; not installed-snapshot drift)"
        )
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="recompute local checkout and exit 1 if lock is stale (print diff)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="local checkout / plugin root (default: repo containing this script)",
    )
    args = parser.parse_args(argv)
    root = (args.root if args.root is not None else _repo_root()).resolve()

    current = compute_lock(root)
    if args.check:
        stored = read_lock(root)
        if stored is None:
            print(f"missing {LOCK_NAME} under {root}", file=sys.stderr)
            return 1
        if stored.get("aggregate") == current.get("aggregate") and (
            stored.get("files") == current.get("files")
        ):
            print(
                f"ok: {len(current.get('files') or {})} files match "
                f"(aggregate={current['aggregate'][:12]}…)"
            )
            return 0
        print(f"stale {LOCK_NAME}:")
        for line in _diff_lock(stored, current):
            print(line)
        return 1

    path = write_lock(root)
    print(f"wrote {path} ({len(current['files'])} files, aggregate={current['aggregate'][:12]}…)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
