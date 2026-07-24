#!/usr/bin/env python3
"""Sanity-check user docs exist and zh / zh-TW cross-links are present."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = [
    "README.md",
    "docs/readme/README.md",
    "docs/readme/README.zh.md",
    "docs/readme/README.zh-TW.md",
    "docs/README.md",
    "docs/README.zh.md",
    "docs/README.zh-TW.md",
    "docs/skills.md",
    "docs/skills.zh.md",
    "docs/skills.zh-TW.md",
    "docs/autopilot.md",
    "docs/autopilot.zh.md",
    "docs/autopilot.zh-TW.md",
    "docs/workflows.md",
    "docs/workflows.zh.md",
    "docs/workflows.zh-TW.md",
    "docs/security-model.md",
    "docs/security-model.zh.md",
    "docs/security-model.zh-TW.md",
    "docs/RELEASE.md",
    "docs/RELEASE.zh.md",
    "docs/RELEASE.zh-TW.md",
    "skills/omg-using/SKILL.md",
    "skills/omg-autopilot/SKILL.md",
]

# (path, substring that must appear)
MARKERS = [
    ("README.md", "docs/readme/README.zh-TW.md"),
    ("README.md", "docs/readme/README.zh.md"),
    ("README.md", "docs/skills.zh-TW.md"),
    ("docs/readme/README.zh-TW.md", "skills.zh-TW.md"),
    ("docs/skills.md", "skills.zh-TW.md"),
    ("docs/skills.md", "skills.zh.md"),
    ("docs/skills.zh-TW.md", "skills.md"),
    ("docs/skills.zh.md", "skills.md"),
    ("docs/autopilot.md", "autopilot.zh-TW.md"),
    ("docs/autopilot.md", "autopilot.zh.md"),
    ("docs/security-model.md", "security-model.zh-TW.md"),
    ("docs/RELEASE.md", "RELEASE.zh-TW.md"),
    ("docs/README.md", "README.zh-TW.md"),
    ("docs/README.zh-TW.md", "skills.zh-TW.md"),
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
    # No legacy zh-Hant *filenames*; mention in policy prose is OK.
    for path in ROOT.rglob("*.md"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("docs/research/") or rel.startswith(".omx/") or rel.startswith(".omg/"):
            continue
        if "/.omg/" in f"/{rel}/" or "/.omx/" in f"/{rel}/":
            continue
        if "zh-Hant" in path.name:
            errors.append(f"legacy zh-Hant filename: {rel}")
        elif "zh-Hant" in path.read_text(encoding="utf-8", errors="ignore"):
            # Allow explicit deprecation notes in locale policy docs.
            if rel in {
                "CONTRIBUTING.md",
                "docs/readme/README.md",
                "docs/readme/README.zh.md",
                "docs/readme/README.zh-TW.md",
            }:
                continue
            errors.append(f"{rel}: contains zh-Hant reference")
    if errors:
        print("FAIL", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    print("docs_ok skills=", len(skills))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
