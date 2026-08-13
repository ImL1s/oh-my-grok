#!/usr/bin/env python3
"""Sanity-check user docs exist, zh / zh-TW cross-links, and routing-doc hrefs."""
from __future__ import annotations

import re
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
    # Canonical dual-host architecture + locale projections (EN remains normative — #133)
    "docs/architecture/agent-model-routing.md",
    "docs/architecture/agent-model-routing.zh.md",
    "docs/architecture/agent-model-routing.zh-TW.md",
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
    ("README.md", "docs/architecture/agent-model-routing.md"),
    ("docs/readme/README.zh-TW.md", "skills.zh-TW.md"),
    ("docs/readme/README.zh-TW.md", "architecture/agent-model-routing.md"),
    ("docs/readme/README.zh.md", "architecture/agent-model-routing.md"),
    ("docs/readme/README.md", "architecture/agent-model-routing.md"),
    ("docs/skills.md", "skills.zh-TW.md"),
    ("docs/skills.md", "skills.zh.md"),
    ("docs/skills.zh-TW.md", "skills.md"),
    ("docs/skills.zh.md", "skills.md"),
    ("docs/autopilot.md", "autopilot.zh-TW.md"),
    ("docs/autopilot.md", "autopilot.zh.md"),
    ("docs/security-model.md", "security-model.zh-TW.md"),
    ("docs/RELEASE.md", "RELEASE.zh-TW.md"),
    ("docs/README.md", "README.zh-TW.md"),
    ("docs/README.md", "architecture/agent-model-routing.md"),
    ("docs/README.zh.md", "architecture/agent-model-routing.md"),
    ("docs/README.zh-TW.md", "architecture/agent-model-routing.md"),
    ("docs/README.zh-TW.md", "skills.zh-TW.md"),
    (
        "docs/architecture/agent-model-routing.md",
        "first-class baseline",
    ),
    (
        "docs/architecture/agent-model-routing.md",
        "ImL1s/medley#287",
    ),
    (
        "docs/architecture/agent-model-routing.md",
        "ImL1s/medley#289",
    ),
    (
        "docs/architecture/agent-model-routing.md",
        "ImL1s/medley#207",
    ),
    (
        "docs/architecture/agent-model-routing.md",
        "ImL1s/medley#290",
    ),
    (
        "docs/architecture/agent-model-routing.md",
        "oh-my-grok#138",
    ),
    (
        "docs/architecture/agent-model-routing.md",
        "agent-model-routing.zh.md",
    ),
    (
        "docs/architecture/agent-model-routing.md",
        "agent-model-routing.zh-TW.md",
    ),
    (
        "docs/architecture/agent-model-routing.zh.md",
        "agent-model-routing.md",
    ),
    (
        "docs/architecture/agent-model-routing.zh.md",
        "first-class baseline",
    ),
    (
        "docs/architecture/agent-model-routing.zh.md",
        "ImL1s/medley#287",
    ),
    (
        "docs/architecture/agent-model-routing.zh.md",
        "ImL1s/medley#289",
    ),
    (
        "docs/architecture/agent-model-routing.zh.md",
        "ImL1s/medley#207",
    ),
    (
        "docs/architecture/agent-model-routing.zh.md",
        "ImL1s/medley#290",
    ),
    (
        "docs/architecture/agent-model-routing.zh.md",
        "oh-my-grok#138",
    ),
    (
        "docs/architecture/agent-model-routing.zh-TW.md",
        "agent-model-routing.md",
    ),
    (
        "docs/architecture/agent-model-routing.zh-TW.md",
        "first-class baseline",
    ),
    (
        "docs/architecture/agent-model-routing.zh-TW.md",
        "ImL1s/medley#287",
    ),
    (
        "docs/architecture/agent-model-routing.zh-TW.md",
        "ImL1s/medley#289",
    ),
    (
        "docs/architecture/agent-model-routing.zh-TW.md",
        "ImL1s/medley#207",
    ),
    (
        "docs/architecture/agent-model-routing.zh-TW.md",
        "ImL1s/medley#290",
    ),
    (
        "docs/architecture/agent-model-routing.zh-TW.md",
        "oh-my-grok#138",
    ),
    ("docs/README.md", "agent-model-routing.zh-TW.md"),
    ("docs/README.zh.md", "agent-model-routing.zh.md"),
    ("docs/README.zh-TW.md", "agent-model-routing.zh-TW.md"),
    ("docs/readme/README.md", "agent-model-routing.zh.md"),
    ("docs/readme/README.md", "agent-model-routing.zh-TW.md"),
]

ROUTING_DOCS = [
    "docs/architecture/agent-model-routing.md",
    "docs/architecture/agent-model-routing.zh.md",
    "docs/architecture/agent-model-routing.zh-TW.md",
    "docs/plans/2026-08-09-dual-host-agent-model-routing.md",
]
REQUIRED_EXTERNAL = {
    "https://github.com/ImL1s/oh-my-grok/issues/131",
    "https://github.com/ImL1s/oh-my-grok/issues/133",
    "https://github.com/ImL1s/oh-my-grok/issues/134",
    "https://github.com/ImL1s/oh-my-grok/issues/138",
    "https://github.com/ImL1s/medley/issues/287",
    "https://github.com/ImL1s/medley/issues/289",
    "https://github.com/ImL1s/medley/issues/207",
    "https://github.com/ImL1s/medley/issues/290",
}
_ROUTING_REQUIRE_EXTERNAL = (
    "docs/architecture/agent-model-routing.md",
    "docs/architecture/agent-model-routing.zh.md",
    "docs/architecture/agent-model-routing.zh-TW.md",
)

# Markdown links [text](dest) — not images ![alt](dest).
_MD_LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)]+)\)")
_BARE_GITHUB = re.compile(r"https://github.com/[^\s)\]>'\"`]+")


def _clean_href(raw: str) -> str:
    dest = raw.strip()
    if dest.startswith("<") and ">" in dest:
        dest = dest[1 : dest.index(">")].strip()
    if not dest:
        return dest
    # Optional markdown title: dest "title" / dest 'title'
    if dest[0] in {'"', "'"}:
        return dest
    return dest.split()[0]


def markdown_hrefs(text: str) -> list[str]:
    """Return dest strings from non-image markdown links."""
    return [_clean_href(m.group(2)) for m in _MD_LINK.finditer(text)]


def bare_github_urls(text: str) -> list[str]:
    """Return bare https://github.com/... URLs (trailing punct stripped)."""
    out: list[str] = []
    for m in _BARE_GITHUB.finditer(text):
        url = m.group(0).rstrip(".,;:'\"")
        if url:
            out.append(url)
    return out


def collect_https(text: str) -> set[str]:
    """Exact https hrefs from markdown links plus bare GitHub URLs."""
    found: set[str] = set()
    for href in markdown_hrefs(text):
        if href.lower().startswith("https://"):
            found.add(href)
    found.update(bare_github_urls(text))
    return found


def is_remote_dest(dest: str) -> bool:
    low = dest.lower()
    return low.startswith(("http://", "https://", "mailto:"))


def local_target(src_file: Path, dest: str) -> Path:
    """Resolve a local markdown dest (fragment stripped) against *src_file*."""
    path_part = dest.split("#", 1)[0]
    if not path_part:
        return src_file
    return (src_file.parent / path_part).resolve()


def check_routing_docs(*, root: Path = ROOT) -> list[str]:
    """Validate routing-doc local targets and required public issue URLs."""
    errors: list[str] = []
    for rel in ROUTING_DOCS:
        path = root / rel
        if not path.is_file():
            errors.append(f"missing {rel}")
            continue
        text = path.read_text(encoding="utf-8")
        hrefs = markdown_hrefs(text)
        https = collect_https(text)
        for dest in hrefs:
            if not dest or is_remote_dest(dest):
                continue
            target = local_target(path, dest)
            if not target.is_file():
                errors.append(f"{rel}: missing local target {dest!r} -> {target}")
        if rel in _ROUTING_REQUIRE_EXTERNAL:
            missing = sorted(REQUIRED_EXTERNAL - https)
            if missing:
                errors.append(
                    f"{rel}: missing exact external href/URL {missing}"
                )
    return errors


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
    errors.extend(check_routing_docs())
    # 16 skills
    skills = sorted(p.name for p in (ROOT / "skills").iterdir() if p.is_dir())
    if len(skills) != 16:
        errors.append(f"expected 16 skills, got {len(skills)}: {skills}")
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
