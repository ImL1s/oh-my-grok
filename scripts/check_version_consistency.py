#!/usr/bin/env python3
"""Fail closed when current-facing version metadata drifts from plugin.json (#23).

Canonical product version: ``plugin.json`` → ``version``.

Usage::

    python scripts/check_version_consistency.py --check
    python scripts/check_version_consistency.py --write   # fix designated fields
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Same grammar as omg_cli.setup_cmd / release_transaction (incl. prerelease).
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
# Non-anchored token for scanning docs (no ^$).
SEMVER_TOKEN_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


@dataclass(frozen=True)
class Finding:
    path: str
    field: str
    expected: str
    observed: str
    detail: str = ""

    def format(self) -> str:
        base = (
            f"{self.path}: {self.field}: expected {self.expected!r}, "
            f"observed {self.observed!r}"
        )
        return f"{base} ({self.detail})" if self.detail else base


def load_canonical_version(root: Path = ROOT) -> str:
    plugin = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    ver = str(plugin.get("version") or "").strip()
    if not SEMVER_RE.fullmatch(ver):
        raise SystemExit(
            f"plugin.json version is not a valid SemVer (incl. prerelease): {ver!r}"
        )
    return ver


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).write_text(text, encoding="utf-8")


def check_python_version(root: Path, version: str) -> list[Finding]:
    text = _read(root, "omg_cli/__init__.py")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    observed = m.group(1) if m else "<missing>"
    if observed != version:
        return [
            Finding(
                "omg_cli/__init__.py",
                "__version__",
                version,
                observed,
            )
        ]
    return []


def check_capabilities_lock(root: Path, version: str) -> list[Finding]:
    path = "omg_capabilities.lock.json"
    data = json.loads(_read(root, path))
    observed = str(data.get("version") or "")
    if observed != version:
        return [Finding(path, "version", version, observed)]
    return []


def check_changelog_heading(root: Path, version: str) -> list[Finding]:
    text = _read(root, "CHANGELOG.md")
    # Keep a Changelog: ## [0.7.2] - YYYY-MM-DD
    # Do not use \b after ']': ']' is non-word so \b never matches before space.
    if re.search(rf"^## \[{re.escape(version)}\](?:\s|$)", text, re.M):
        return []
    return [
        Finding(
            "CHANGELOG.md",
            "section heading",
            f"## [{version}]",
            "<missing>",
            "current version heading required",
        )
    ]


# Current-facing exact substring checks (path, field label, template with {v}).
# Historical changelog bullets and feature-era docs are intentionally omitted.
_CURRENT_SNIPPETS: list[tuple[str, str, str]] = [
    ("README.md", "Version badge", "Version: **{v}** · License: MIT"),
    (
        "docs/readme/README.zh.md",
        "Version badge",
        "版本：**{v}** · License: MIT",
    ),
    (
        "docs/readme/README.zh-TW.md",
        "Version badge",
        "版本：**{v}** · License: MIT",
    ),
    (
        "docs/autopilot.md",
        "plugin version line",
        "matches [`plugin.json`](../plugin.json) (currently **{v}**).",
    ),
    (
        "docs/autopilot.zh.md",
        "plugin version line",
        "与 [`plugin.json`](../plugin.json) 一致（目前 **{v}**）。",
    ),
    (
        "docs/autopilot.zh-TW.md",
        "plugin version line",
        "與 [`plugin.json`](../plugin.json) 一致（目前 **{v}**）。",
    ),
    (
        "docs/security-model.md",
        "plugin version header",
        "Plugin version: **{v}**",
    ),
    (
        "docs/security-model.zh.md",
        "plugin version header",
        "Plugin 版本：**{v}**",
    ),
    (
        "docs/security-model.zh-TW.md",
        "plugin version header",
        "Plugin 版本：**{v}**",
    ),
    (
        "docs/RELEASE.md",
        "public asset name",
        "oh-my-grok-{v}.tar.gz",
    ),
    (
        "docs/RELEASE.zh.md",
        "public asset name",
        "oh-my-grok-{v}.tar.gz",
    ),
    (
        "docs/RELEASE.zh-TW.md",
        "public asset name",
        "oh-my-grok-{v}.tar.gz",
    ),
    ("docs/RELEASE.md", "Version row", "| Version | **{v}** |"),
    ("docs/RELEASE.zh.md", "Version row", "| Version | **{v}** |"),
    ("docs/RELEASE.zh-TW.md", "Version row", "| Version | **{v}** |"),
    ("docs/RELEASE.md", "Intended tag row", "| Intended tag | `v{v}` |"),
    ("docs/RELEASE.zh.md", "Intended tag row", "| Intended tag | `v{v}` |"),
    ("docs/RELEASE.zh-TW.md", "Intended tag row", "| Intended tag | `v{v}` |"),
]


# Patterns that must not embed a *stale* exact archive when claiming current pin.
# We require at least one oh-my-grok-{version}.tar.gz in these files.
_ARCHIVE_DOCS: tuple[str, ...] = (
    "README.md",
    "docs/readme/README.zh.md",
    "docs/readme/README.zh-TW.md",
    "docs/RELEASE.md",
    "docs/RELEASE.zh.md",
    "docs/RELEASE.zh-TW.md",
)


# Offline installer comment: prefer version-neutral template.
_INSTALL_SH_EXAMPLE_RE = re.compile(
    rf"(bash install\.sh --offline --archive \./oh-my-grok-)"
    rf"({SEMVER_TOKEN_RE.pattern}|<VERSION>)(\.tar\.gz)"
)
_INSTALL_SH_NEUTRAL = (
    "bash install.sh --offline --archive ./oh-my-grok-<VERSION>.tar.gz"
    " --checksums ./SHA256SUMS"
)


def check_snippets(root: Path, version: str) -> list[Finding]:
    findings: list[Finding] = []
    for rel, field, template in _CURRENT_SNIPPETS:
        path = root / rel
        if not path.is_file():
            findings.append(
                Finding(rel, field, template.format(v=version), "<file missing>")
            )
            continue
        text = path.read_text(encoding="utf-8")
        expected = template.format(v=version)
        if expected not in text:
            # Best-effort observed: first nearby version-like token
            m = re.search(r"\b\d+\.\d+\.\d+\b", text[:2000])
            observed = m.group(0) if m else "<not found>"
            findings.append(
                Finding(rel, field, expected, observed, "substring missing")
            )
    return findings


def check_archive_docs(root: Path, version: str) -> list[Finding]:
    findings: list[Finding] = []
    needle = f"oh-my-grok-{version}.tar.gz"
    archive_re = re.compile(rf"oh-my-grok-({SEMVER_TOKEN_RE.pattern})\.tar\.gz")
    for rel in _ARCHIVE_DOCS:
        path = root / rel
        if not path.is_file():
            findings.append(
                Finding(rel, "release archive example", needle, "<file missing>")
            )
            continue
        text = path.read_text(encoding="utf-8")
        if needle not in text:
            m = archive_re.search(text)
            observed = m.group(0) if m else "<none>"
            findings.append(
                Finding(rel, "release archive example", needle, observed)
            )
        # Every exact archive pin in current-facing docs must match canonical.
        for m in archive_re.finditer(text):
            if m.group(1) != version:
                findings.append(
                    Finding(
                        rel,
                        "stale archive pin",
                        needle,
                        m.group(0),
                        f"offset={m.start()}",
                    )
                )
    return findings


def check_tag_pins(root: Path, version: str) -> list[Finding]:
    """TAG=v… and oh-my-grok@v… pins in current-facing install docs."""
    findings: list[Finding] = []
    tag_re = re.compile(rf"TAG=v({SEMVER_TOKEN_RE.pattern})")
    plugin_re = re.compile(rf"oh-my-grok@v({SEMVER_TOKEN_RE.pattern})")
    expected_tag = f"v{version}"
    for rel in _ARCHIVE_DOCS:
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        tags = list(tag_re.finditer(text))
        if not tags and rel.startswith("README"):
            findings.append(
                Finding(rel, "TAG= assignment", f"TAG={expected_tag}", "<none>")
            )
        for m in tags:
            if m.group(1) != version:
                findings.append(
                    Finding(
                        rel,
                        "TAG= assignment",
                        f"TAG={expected_tag}",
                        m.group(0),
                        f"offset={m.start()}",
                    )
                )
        for m in plugin_re.finditer(text):
            if m.group(1) != version:
                findings.append(
                    Finding(
                        rel,
                        "plugin pin @v",
                        f"oh-my-grok@v{version}",
                        m.group(0),
                        f"offset={m.start()}",
                    )
                )
    return findings


def check_install_sh(root: Path, version: str) -> list[Finding]:
    """install.sh comment example must be version-neutral or match canonical."""
    rel = "scripts/install.sh"
    text = _read(root, rel)
    findings: list[Finding] = []
    if "<VERSION>" in text and "oh-my-grok-<VERSION>.tar.gz" in text:
        return findings
    m = _INSTALL_SH_EXAMPLE_RE.search(text)
    if not m:
        findings.append(
            Finding(
                rel,
                "offline archive example",
                "oh-my-grok-<VERSION>.tar.gz or oh-my-grok-{v}.tar.gz".format(
                    v=version
                ),
                "<pattern missing>",
            )
        )
        return findings
    observed = m.group(2)
    if observed != version:
        findings.append(
            Finding(
                rel,
                "offline archive example",
                f"<VERSION> or {version}",
                observed,
                "stale example; prefer version-neutral <VERSION>",
            )
        )
    return findings


def collect_findings(root: Path, version: str) -> list[Finding]:
    out: list[Finding] = []
    out.extend(check_python_version(root, version))
    out.extend(check_capabilities_lock(root, version))
    out.extend(check_changelog_heading(root, version))
    out.extend(check_snippets(root, version))
    out.extend(check_archive_docs(root, version))
    out.extend(check_tag_pins(root, version))
    out.extend(check_install_sh(root, version))
    return out


def write_fixes(root: Path, version: str) -> list[str]:
    """Deterministic rewrites for designated current-facing fields."""
    changed: list[str] = []

    # install.sh → version-neutral example
    rel = "scripts/install.sh"
    text = _read(root, rel)
    new = _INSTALL_SH_EXAMPLE_RE.sub(
        r"\1<VERSION>\3",
        text,
        count=1,
    )
    # If no match, inject neutral comment line near top if old hardcode present
    if "oh-my-grok-<VERSION>.tar.gz" not in new and "0.6.0.tar.gz" in new:
        new = new.replace(
            "oh-my-grok-0.6.0.tar.gz",
            "oh-my-grok-<VERSION>.tar.gz",
        )
    if new != text:
        _write(root, rel, new)
        changed.append(rel)

    # Autopilot version lines (replace any currently **X.Y.Z**)
    autopilot_res = [
        (
            "docs/autopilot.md",
            re.compile(
                r"(matches \[`plugin\.json`\]\(\.\./plugin\.json\) \(currently \*\*)"
                r"(\d+\.\d+\.\d+)(\*\*\)\.)"
            ),
            rf"\g<1>{version}\3",
        ),
        (
            "docs/autopilot.zh.md",
            re.compile(
                r"(与 \[`plugin\.json`\]\(\.\./plugin\.json\) 一致（目前 \*\*)"
                r"(\d+\.\d+\.\d+)(\*\*）。)"
            ),
            rf"\g<1>{version}\3",
        ),
        (
            "docs/autopilot.zh-TW.md",
            re.compile(
                r"(與 \[`plugin\.json`\]\(\.\./plugin\.json\) 一致（目前 \*\*)"
                r"(\d+\.\d+\.\d+)(\*\*）。)"
            ),
            rf"\g<1>{version}\3",
        ),
    ]
    for path, cre, repl in autopilot_res:
        t = _read(root, path)
        n = cre.sub(repl, t)
        if n != t:
            _write(root, path, n)
            changed.append(path)

    # README version badges
    badge_specs = [
        ("README.md", re.compile(r"(Version: \*\*)(\d+\.\d+\.\d+)(\*\*)"), version),
        (
            "docs/readme/README.zh.md",
            re.compile(r"(版本：\*\*)(\d+\.\d+\.\d+)(\*\*)"),
            version,
        ),
        (
            "docs/readme/README.zh-TW.md",
            re.compile(r"(版本：\*\*)(\d+\.\d+\.\d+)(\*\*)"),
            version,
        ),
    ]
    for path, cre, ver in badge_specs:
        t = _read(root, path)
        n = cre.sub(rf"\g<1>{ver}\3", t)
        # Also rewrite TAG=vX and archive names in install snippets (not history bullets)
        n2 = re.sub(
            r"(TAG=v)\d+\.\d+\.\d+",
            rf"\g<1>{ver}",
            n,
        )
        n2 = re.sub(
            r"oh-my-grok-\d+\.\d+\.\d+\.tar\.gz",
            f"oh-my-grok-{ver}.tar.gz",
            n2,
        )
        # Do not rewrite historical "v0.6.0:" bullets — those use **v0.6.0:** form
        # Restore historical lines that use **vX.Y.Z:** pattern if we over-wrote
        # Actually the archive re.sub is global and might hit only install section
        # Historical section uses **v0.6.0:** not archives. OK.
        # Pin comment: @v0.7.2 in install tip
        n2 = re.sub(
            r"(oh-my-grok@v)\d+\.\d+\.\d+",
            rf"\g<1>{ver}",
            n2,
        )
        if n2 != t:
            _write(root, path, n2)
            changed.append(path)

    # RELEASE docs: table rows + archives + tags
    for path in (
        "docs/RELEASE.md",
        "docs/RELEASE.zh.md",
        "docs/RELEASE.zh-TW.md",
    ):
        t = _read(root, path)
        n = re.sub(
            r"oh-my-grok-\d+\.\d+\.\d+\.tar\.gz",
            f"oh-my-grok-{version}.tar.gz",
            t,
        )
        n = re.sub(r"(TAG=v)\d+\.\d+\.\d+", rf"\g<1>{version}", n)
        n = re.sub(
            r"(\|\s*Version\s*\|\s*\*\*)\d+\.\d+\.\d+(\*\*\s*\|)",
            rf"\g<1>{version}\2",
            n,
        )
        n = re.sub(
            r"(\|\s*Intended tag\s*\|\s*`v)\d+\.\d+\.\d+(`\s*\|)",
            rf"\g<1>{version}\2",
            n,
        )
        if n != t:
            _write(root, path, n)
            changed.append(path)

    # security-model headers
    for path, cre in (
        (
            "docs/security-model.md",
            re.compile(r"(Plugin version: \*\*)(\d+\.\d+\.\d+)(\*\*)"),
        ),
        (
            "docs/security-model.zh.md",
            re.compile(r"(Plugin 版本：\*\*)(\d+\.\d+\.\d+)(\*\*)"),
        ),
        (
            "docs/security-model.zh-TW.md",
            re.compile(r"(Plugin 版本：\*\*)(\d+\.\d+\.\d+)(\*\*)"),
        ),
    ):
        t = _read(root, path)
        n = cre.sub(rf"\g<1>{version}\3", t)
        if n != t:
            _write(root, path, n)
            changed.append(path)

    return changed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any current-facing field drifts",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="rewrite designated current-facing fields to plugin.json version",
    )
    p.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root (default: checkout containing this script)",
    )
    args = p.parse_args(argv)
    root = args.root.resolve()
    version = load_canonical_version(root)

    if args.write:
        changed = write_fixes(root, version)
        # Second pass: report remaining failures
        findings = collect_findings(root, version)
        if changed:
            print("wrote:")
            for c in changed:
                print(f"  {c}")
        else:
            print("write: no designated fields needed changes")
        if findings:
            print("remaining failures after --write:", file=sys.stderr)
            for f in findings:
                print(f"  {f.format()}", file=sys.stderr)
            return 1
        print(f"ALL_VERSION_CONSISTENT version={version}")
        return 0

    findings = collect_findings(root, version)
    if findings:
        print(
            f"version consistency FAILED (canonical plugin.json={version}):",
            file=sys.stderr,
        )
        for f in findings:
            print(f"  {f.format()}", file=sys.stderr)
        return 1
    print(f"ALL_VERSION_CONSISTENT version={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
