# omg_cli/doctor.py
"""omg doctor — health checks for plugin + CLI environment."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Callable


CheckFn = Callable[[], tuple[bool, str]]

HOOK_SCRIPTS = (
    "hooks/bin/_common.py",
    "hooks/bin/session_start.py",
    "hooks/bin/subagent_stop.py",
    "hooks/bin/stop.py",
    "hooks/bin/pre_tool_use_deny.py",
)


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _check(name: str, ok: bool, detail: str) -> tuple[str, bool, str]:
    return name, ok, detail


def check_grok_on_path() -> tuple[str, bool, str]:
    path = shutil.which("grok")
    if path:
        return _check("grok on PATH", True, path)
    return _check("grok on PATH", False, "grok not found (install Grok Build CLI)")


def check_plugin_json() -> tuple[str, bool, str]:
    path = plugin_root() / "plugin.json"
    if not path.is_file():
        return _check("plugin.json", False, f"missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return _check("plugin.json", False, f"invalid JSON: {e}")
    if not isinstance(data, dict) or "name" not in data:
        return _check("plugin.json", False, "JSON missing required 'name'")
    return _check("plugin.json", True, f"valid ({data.get('name')}@{data.get('version', '?')})")


def check_hooks_scripts() -> tuple[str, bool, str]:
    """Require each hooks/bin script to exist as a file and be executable (X_OK)."""
    root = plugin_root()
    missing: list[str] = []
    not_exec: list[str] = []
    for rel in HOOK_SCRIPTS:
        path = root / rel
        if not path.is_file():
            missing.append(rel)
            continue
        # os.access X_OK is the portable executable bit check on Unix;
        # on non-Unix platforms without execute bits this may always be True.
        if not os.access(path, os.X_OK):
            not_exec.append(rel)
    if missing:
        return _check("hooks scripts", False, f"missing: {', '.join(missing)}")
    if not_exec:
        return _check(
            "hooks scripts",
            False,
            f"not executable (+x required): {', '.join(not_exec)}",
        )
    return _check("hooks scripts", True, f"{len(HOOK_SCRIPTS)} present and executable")


def check_pre_tool_use() -> tuple[str, bool, str]:
    path = plugin_root() / "hooks" / "hooks.json"
    if not path.is_file():
        return _check("PreToolUse hook", False, f"missing {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return _check("PreToolUse hook", False, f"invalid JSON: {e}")
    hooks = (data.get("hooks") or {}) if isinstance(data, dict) else {}
    if "PreToolUse" not in hooks:
        return _check("PreToolUse hook", False, "PreToolUse key missing in hooks.json")
    entries = hooks["PreToolUse"]
    if not entries:
        return _check("PreToolUse hook", False, "PreToolUse empty")
    return _check("PreToolUse hook", True, f"{len(entries)} matcher group(s)")


def check_skills_omg_prefix() -> tuple[str, bool, str]:
    skills_dir = plugin_root() / "skills"
    if not skills_dir.is_dir():
        return _check("skills omg-*", False, "skills/ missing")
    bad: list[str] = []
    good = 0
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        skill = child / "SKILL.md"
        if not skill.is_file():
            bad.append(f"{child.name}/ (no SKILL.md)")
            continue
        if not child.name.startswith("omg-"):
            bad.append(child.name)
            continue
        good += 1
    if bad:
        return _check("skills omg-*", False, f"bad: {', '.join(bad)}")
    if good == 0:
        return _check("skills omg-*", False, "no skills found")
    return _check("skills omg-*", True, f"{good} skill(s)")


def check_agents_present() -> tuple[str, bool, str]:
    agents_dir = plugin_root() / "agents"
    if not agents_dir.is_dir():
        return _check("agents", False, "agents/ missing")
    expected = [
        "omg-orchestrator.md",
        "omg-executor.md",
        "omg-critic.md",
        "omg-verifier.md",
    ]
    missing = [n for n in expected if not (agents_dir / n).is_file()]
    if missing:
        return _check("agents", False, f"missing: {', '.join(missing)}")
    return _check("agents", True, f"{len(expected)} present")


def check_deny_importable() -> tuple[str, bool, str]:
    try:
        from omg_cli.deny import decide_pre_tool_use, should_deny_command

        assert callable(should_deny_command)
        assert callable(decide_pre_tool_use)
        return _check("deny module", True, "omg_cli.deny importable")
    except Exception as e:
        return _check("deny module", False, f"{type(e).__name__}: {e}")


def run_checks() -> list[tuple[str, bool, str]]:
    return [
        check_grok_on_path(),
        check_plugin_json(),
        check_hooks_scripts(),
        check_pre_tool_use(),
        check_skills_omg_prefix(),
        check_agents_present(),
        check_deny_importable(),
    ]


def run_doctor() -> int:
    results = run_checks()
    failed = 0
    print("oh-my-grok doctor")
    print("-" * 48)
    for name, ok, detail in results:
        tag = "OK  " if ok else "FAIL"
        print(f"[{tag}] {name}: {detail}")
        if not ok:
            failed += 1
    print("-" * 48)
    if failed:
        print(f"{failed} check(s) failed")
        return 1
    print("all checks passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    _ = argv
    return run_doctor()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
