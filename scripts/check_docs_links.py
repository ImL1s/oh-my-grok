#!/usr/bin/env python3
"""Sanity-check user docs exist and zh-Hant cross-links are present."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = [
    "README.md",
    "README.zh-TW.md",
    "docs/README.md",
    "docs/README.zh-Hant.md",
    "docs/skills.md",
    "docs/skills.zh-Hant.md",
    "docs/autopilot.md",
    "docs/autopilot.zh-Hant.md",
    "docs/security-model.md",
    "skills/omg-using/SKILL.md",
    "skills/omg-autopilot/SKILL.md",
]

# (path, substring that must appear)
MARKERS = [
    ("README.md", "README.zh-TW.md"),
    ("README.md", "docs/skills.zh-Hant.md"),
    ("README.zh-TW.md", "docs/skills.zh-Hant.md"),
    ("docs/skills.md", "skills.zh-Hant.md"),
    ("docs/skills.zh-Hant.md", "skills.md"),
    ("docs/autopilot.md", "autopilot.zh-Hant.md"),
    ("docs/autopilot.zh-Hant.md", "autopilot.md"),
    ("docs/README.md", "README.zh-Hant.md"),
    ("docs/README.zh-Hant.md", "skills.zh-Hant.md"),
]


def main() -> int:
    errors: list[str] = []
    for rel in REQUIRED:
        p = ROOT / rel
        if not p.is_file():
            errors.append(f"missing {rel}")
    for rel, needle in MARKERS:
        p = ROOT / rel
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        if needle not in text:
            errors.append(f"{rel}: missing marker {needle!r}")
    # 15 skills
    skills = sorted(p.name for p in (ROOT / "skills").iterdir() if p.is_dir())
    if len(skills) != 15:
        errors.append(f"expected 15 skills, got {len(skills)}: {skills}")
    if errors:
        print("FAIL", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    print("docs_ok skills=", len(skills))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
