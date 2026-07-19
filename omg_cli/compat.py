# omg_cli/compat.py
"""Claude Code / oh-my-claudecode isolation scanner for oh-my-grok.

Scans HOME and project for Claude-side hooks/plugins/magic-routing markers
that can conflict with the Grok-native oh-my-grok harness. Uses ``HOME`` from
the environment so tests can monkeypatch a fake home.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


ISOLATION_ADVICE = """\
[compat.claude] recommended:
  skills = false
  hooks = false
(see Grok harness compatibility / configuration docs)"""

# Magic keywords that indicate Claude Task / OMC routing in CLAUDE.md
CLAUDE_MD_MARKERS: tuple[str, ...] = (
    "oh-my-claudecode",
    "Task(",
    '"ralph"→',
    "'ralph'→",
    "ulw",
    "spawn_subagent",
)

# Markers that are high-signal for Claude-side orchestration (always risk)
_HIGH_SIGNAL_MARKERS: tuple[str, ...] = (
    "oh-my-claudecode",
    "Task(",
    '"ralph"→',
    "'ralph'→",
)

# Settings filenames under ~/.claude/
_SETTINGS_NAMES = ("settings.json", "settings.local.json")


@dataclass
class CompatFinding:
    """One isolation risk finding."""

    level: str  # "ok" | "warn" | "fail"
    code: str
    path: str
    detail: str

    def as_tuple(self) -> tuple[str, str, str, str]:
        return self.level, self.code, self.path, self.detail


@dataclass
class CompatReport:
    findings: list[CompatFinding] = field(default_factory=list)

    @property
    def has_risks(self) -> bool:
        return any(f.level in ("warn", "fail") for f in self.findings)

    @property
    def risk_findings(self) -> list[CompatFinding]:
        return [f for f in self.findings if f.level in ("warn", "fail")]


def home_dir() -> Path:
    """Return HOME directory from env (monkeypatchable via HOME)."""
    home = os.environ.get("HOME")
    if home:
        return Path(home)
    return Path.home()


def _load_json(path: Path) -> dict | list | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data


def _hooks_nonempty(data: object) -> bool:
    """True if settings JSON has a non-empty hooks mapping/list."""
    if not isinstance(data, dict):
        return False
    hooks = data.get("hooks")
    if hooks is None:
        return False
    if isinstance(hooks, dict):
        return len(hooks) > 0
    if isinstance(hooks, list):
        return len(hooks) > 0
    # non-empty string / other truthy
    return bool(hooks)


def scan_claude_settings(home: Path | None = None) -> list[CompatFinding]:
    """Scan ~/.claude/settings.json (+ local) for non-empty hooks."""
    home = Path(home) if home is not None else home_dir()
    findings: list[CompatFinding] = []
    claude_dir = home / ".claude"
    found_any_settings = False
    for name in _SETTINGS_NAMES:
        path = claude_dir / name
        if not path.is_file():
            continue
        found_any_settings = True
        data = _load_json(path)
        if data is None:
            findings.append(
                CompatFinding(
                    level="warn",
                    code="claude.settings.unreadable",
                    path=str(path),
                    detail="settings file present but unreadable/invalid JSON",
                )
            )
            continue
        if _hooks_nonempty(data):
            findings.append(
                CompatFinding(
                    level="warn",
                    code="claude.settings.hooks",
                    path=str(path),
                    detail="non-empty hooks configured (may conflict with Grok plugin)",
                )
            )
    if not found_any_settings:
        findings.append(
            CompatFinding(
                level="ok",
                code="claude.settings",
                path=str(claude_dir),
                detail="no Claude settings.json found",
            )
        )
    elif not any(f.code == "claude.settings.hooks" for f in findings):
        findings.append(
            CompatFinding(
                level="ok",
                code="claude.settings",
                path=str(claude_dir),
                detail="Claude settings present; no non-empty hooks",
            )
        )
    return findings


# Non-plugin bookkeeping dirs under ~/.claude/plugins/ (Claude CLI layout).
# Never reported as isolation risks even if present as directories.
_PLUGIN_DIR_DENYLIST = frozenset(
    {
        "cache",
        "marketplaces",
        "tmp",
        "temp",
    }
)


def _is_plugin_like(path: Path) -> bool:
    """Heuristic: directory looks like a Claude/Grok plugin install.

    Requires known plugin markers (plugin.json, hooks, skills, agents, …).
    Plain directories without markers are not plugin-like.
    """
    if not path.is_dir():
        return False
    markers = (
        path / "plugin.json",
        path / ".claude-plugin",
        path / "hooks" / "hooks.json",
        path / "skills",
        path / "agents",
        path / "manifest.json",
        path / ".marketplace.json",
    )
    if any(m.exists() for m in markers):
        return True
    # nested install layout: <name>/<version>/plugin.json
    try:
        for child in path.iterdir():
            if child.is_dir() and (
                (child / "plugin.json").is_file()
                or (child / "hooks" / "hooks.json").is_file()
            ):
                return True
    except OSError:
        return False
    return False


def scan_claude_plugins(
    home: Path | None = None,
    project_root: Path | None = None,
) -> list[CompatFinding]:
    """Scan ~/.claude/plugins/ and project .claude/plugins/ for plugin-like dirs.

    Only directories that pass ``_is_plugin_like`` are reported. Bookkeeping dirs
    (cache, marketplaces, tmp/temp) are denylisted and ignored.
    """
    home = Path(home) if home is not None else home_dir()
    findings: list[CompatFinding] = []
    roots: list[Path] = [home / ".claude" / "plugins"]
    if project_root is not None:
        roots.append(Path(project_root) / ".claude" / "plugins")

    for plugins_root in roots:
        if not plugins_root.is_dir():
            findings.append(
                CompatFinding(
                    level="ok",
                    code="claude.plugins",
                    path=str(plugins_root),
                    detail="plugins directory absent",
                )
            )
            continue
        plugin_like: list[str] = []
        try:
            children = sorted(plugins_root.iterdir(), key=lambda p: p.name)
        except OSError as e:
            findings.append(
                CompatFinding(
                    level="warn",
                    code="claude.plugins.unreadable",
                    path=str(plugins_root),
                    detail=f"cannot list plugins dir: {e}",
                )
            )
            continue
        for child in children:
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            if child.name.lower() in _PLUGIN_DIR_DENYLIST:
                continue
            if _is_plugin_like(child):
                plugin_like.append(child.name)
        if plugin_like:
            findings.append(
                CompatFinding(
                    level="warn",
                    code="claude.plugins",
                    path=str(plugins_root),
                    detail=f"plugin-like dir(s): {', '.join(plugin_like)}",
                )
            )
        else:
            findings.append(
                CompatFinding(
                    level="ok",
                    code="claude.plugins",
                    path=str(plugins_root),
                    detail="plugins dir empty (no plugin-like entries)",
                )
            )
    return findings


def _markers_in_text(text: str) -> list[str]:
    """Return list of CLAUDE_MD_MARKERS found in text (order preserved, unique)."""
    found: list[str] = []
    for marker in CLAUDE_MD_MARKERS:
        if marker in text:
            found.append(marker)
    return found


def scan_claude_md(
    home: Path | None = None,
    project_root: Path | None = None,
) -> list[CompatFinding]:
    """Scan CLAUDE.md / ~/.claude/CLAUDE.md for magic OMC / Task routing markers."""
    home = Path(home) if home is not None else home_dir()
    candidates: list[Path] = [
        home / ".claude" / "CLAUDE.md",
        home / "CLAUDE.md",
    ]
    if project_root is not None:
        pr = Path(project_root)
        candidates.extend(
            [
                pr / "CLAUDE.md",
                pr / ".claude" / "CLAUDE.md",
            ]
        )

    findings: list[CompatFinding] = []
    seen: set[str] = set()
    any_file = False
    for path in candidates:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        if not path.is_file():
            continue
        any_file = True
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            findings.append(
                CompatFinding(
                    level="warn",
                    code="claude.md.unreadable",
                    path=str(path),
                    detail=f"cannot read: {e}",
                )
            )
            continue
        hits = _markers_in_text(text)
        # Only report if high-signal markers or multiple routing markers present.
        # Bare "ulw" alone in unrelated text is noisy; require high-signal OR
        # at least one high-signal / Task / OMC-style routing hit.
        high = [h for h in hits if h in _HIGH_SIGNAL_MARKERS]
        # Also treat spawn_subagent + Task-like combo, or oh-my-claudecode already high
        if high or "oh-my-claudecode" in hits or "Task(" in hits:
            findings.append(
                CompatFinding(
                    level="warn",
                    code="claude.md.markers",
                    path=str(path),
                    detail=f"magic keywords: {', '.join(hits)}",
                )
            )
        elif hits:
            # weaker markers only (ulw, spawn_subagent alone) still warn —
            # plan lists them as detect targets
            findings.append(
                CompatFinding(
                    level="warn",
                    code="claude.md.markers",
                    path=str(path),
                    detail=f"magic keywords: {', '.join(hits)}",
                )
            )
        else:
            findings.append(
                CompatFinding(
                    level="ok",
                    code="claude.md",
                    path=str(path),
                    detail="no OMC/Task magic keywords",
                )
            )

    if not any_file:
        findings.append(
            CompatFinding(
                level="ok",
                code="claude.md",
                path="(none)",
                detail="no CLAUDE.md found under HOME/project",
            )
        )
    return findings


def scan_compat(
    home: Path | None = None,
    project_root: Path | None = None,
) -> CompatReport:
    """Run all Claude isolation scans. HOME-aware via home_dir() / env HOME."""
    home = Path(home) if home is not None else home_dir()
    findings: list[CompatFinding] = []
    findings.extend(scan_claude_settings(home=home))
    findings.extend(scan_claude_plugins(home=home, project_root=project_root))
    findings.extend(scan_claude_md(home=home, project_root=project_root))
    return CompatReport(findings=findings)


def format_isolation_banner() -> str:
    """Fixed isolation advice banner for setup / doctor."""
    return ISOLATION_ADVICE


def format_compat_lines(
    report: CompatReport,
    *,
    strict: bool = False,
) -> list[str]:
    """Human-readable lines for doctor output.

    In default mode risks are WARN; in strict mode risks are FAIL.
    """
    lines: list[str] = []
    for f in report.findings:
        if f.level == "ok":
            tag = "OK  "
        elif strict and f.level in ("warn", "fail"):
            tag = "FAIL"
        else:
            tag = "WARN" if f.level == "warn" else "FAIL"
        lines.append(f"[{tag}] compat.{f.code}: {f.detail} ({f.path})")
    return lines


def compat_exit_should_fail(report: CompatReport, *, strict: bool) -> bool:
    """True if doctor should exit non-zero due to compat risks under --strict."""
    if not strict:
        return False
    return report.has_risks
