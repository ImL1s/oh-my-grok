# omg_cli/doctor.py
"""omg doctor — health checks for plugin + CLI environment."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


CheckFn = Callable[[], tuple[bool, str]]

# Soft check result: (name, level, detail) where level in {"ok","warn","fail"}
SoftResult = tuple[str, str, str]

HOOK_SCRIPTS = (
    "hooks/bin/_common.py",
    "hooks/bin/session_start.py",
    "hooks/bin/subagent_stop.py",
    "hooks/bin/stop.py",
    "hooks/bin/pre_tool_use_deny.py",
)

PLUGIN_NAME = "oh-my-grok"

GLOBAL_PRETOOL_HOOK_NAME = "omg-pretool-deny.json"

SOFT_GATE_FOOTER = (
    "PreToolUse is fail-open soft-gate; not hard guarantee."
)

# Candidate grok CLI probes for plugin trust/inventory (best-effort).
_TRUST_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("grok", "plugin", "details", PLUGIN_NAME, "--json"),
    ("grok", "plugin", "list", "--json"),
    ("grok", "inspect", "--json"),
    ("grok", "plugin", "inspect", PLUGIN_NAME, "--json"),
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


def _matchers_cover_spawn(entries: list[Any]) -> bool:
    """True if any PreToolUse matcher string includes spawn_subagent or Task."""
    for group in entries:
        if not isinstance(group, dict):
            continue
        m = str(group.get("matcher") or "")
        if "spawn_subagent" in m or "Task" in m:
            return True
    return False


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
    if not _matchers_cover_spawn(list(entries) if isinstance(entries, list) else []):
        return _check(
            "PreToolUse hook",
            False,
            "matcher missing spawn_subagent|Task (spawn fail-closed gate)",
        )
    return _check(
        "PreToolUse hook",
        True,
        f"{len(entries)} matcher group(s); includes spawn_subagent|Task",
    )


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


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def check_global_pretool_hook() -> tuple[str, bool, str]:
    """Require ~/.grok/hooks/omg-pretool-deny.json with a resolvable deny script.

    Live 2026-07-19: plugin-bundled hooks alone did not appear in session
    hook_execution; soft-gate requires this global hook file.
    """
    path = _home() / ".grok" / "hooks" / GLOBAL_PRETOOL_HOOK_NAME
    if not path.is_file():
        return _check(
            "global PreToolUse soft-gate",
            False,
            f"missing {path} (run scripts/install-plugin.sh)",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return _check("global PreToolUse soft-gate", False, f"invalid JSON: {e}")
    # Extract first command string under hooks.PreToolUse[*].hooks[*].command
    commands: list[str] = []
    matchers: list[str] = []
    hooks_root = (data.get("hooks") or {}) if isinstance(data, dict) else {}
    for group in hooks_root.get("PreToolUse") or []:
        if not isinstance(group, dict):
            continue
        if group.get("matcher") is not None:
            matchers.append(str(group.get("matcher") or ""))
        for h in group.get("hooks") or []:
            if isinstance(h, dict) and isinstance(h.get("command"), str):
                commands.append(h["command"])
    if not commands:
        return _check(
            "global PreToolUse soft-gate",
            False,
            f"{path} has no PreToolUse command entries",
        )
    if not any("spawn_subagent" in m or "Task" in m for m in matchers):
        return _check(
            "global PreToolUse soft-gate",
            False,
            f"{path} matcher missing spawn_subagent|Task "
            "(re-run scripts/install-plugin.sh)",
        )
    # Prefer a path that looks like pre_tool_use_deny.py
    ok_path: str | None = None
    for cmd in commands:
        m = re.search(r'["\']([^"\']*pre_tool_use_deny\.py)["\']', cmd)
        if not m:
            m = re.search(r"(\S*pre_tool_use_deny\.py)", cmd)
        if m:
            candidate = Path(m.group(1))
            if candidate.is_file() and os.access(candidate, os.R_OK):
                ok_path = str(candidate)
                break
            return _check(
                "global PreToolUse soft-gate",
                False,
                f"deny script not found or unreadable: {candidate}",
            )
    if ok_path is None:
        # Command present but not our deny script — hard fail for this gate
        return _check(
            "global PreToolUse soft-gate",
            False,
            f"{path} commands do not reference pre_tool_use_deny.py: {commands!r}",
        )
    return _check(
        "global PreToolUse soft-gate",
        True,
        f"{path} → {ok_path}",
    )


def _run_grok_json(argv: tuple[str, ...] | list[str], *, timeout: float = 8.0) -> Any | None:
    """Run a grok argv expecting JSON on stdout. None on any failure."""
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # some CLIs wrap JSON after banners — try last JSON object/array
        for start in ("{", "["):
            idx = out.find(start)
            if idx >= 0:
                try:
                    return json.loads(out[idx:])
                except json.JSONDecodeError:
                    continue
        return None


def _summarize_plugin_payload(data: Any, *, source: str) -> SoftResult | None:
    """Extract enabled/trusted/hooks summary from heterogeneous grok JSON.

    Returns a SoftResult if this payload is useful for oh-my-grok inventory,
    else None (try next probe).
    """
    if data is None:
        return None

    # Normalize list payloads from `plugin list`
    candidates: list[dict[str, Any]] = []
    if isinstance(data, list):
        candidates = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict):
        # nested common shapes
        for key in ("plugins", "items", "data", "result"):
            nested = data.get(key)
            if isinstance(nested, list):
                candidates = [x for x in nested if isinstance(x, dict)]
                break
        if not candidates:
            candidates = [data]

    target: dict[str, Any] | None = None
    for item in candidates:
        name = str(item.get("name") or item.get("id") or item.get("plugin") or "")
        if name == PLUGIN_NAME or PLUGIN_NAME in name:
            target = item
            break
    if target is None and len(candidates) == 1 and isinstance(data, dict):
        # single details object without explicit name match still usable
        target = candidates[0]
    if target is None:
        # list succeeded but oh-my-grok not installed
        if isinstance(data, list) or (
            isinstance(data, dict)
            and any(k in data for k in ("plugins", "items", "data"))
        ):
            return (
                "plugin trust/inventory",
                "warn",
                f"inspect ok via {source}; {PLUGIN_NAME} not listed",
            )
        return None

    bits: list[str] = [f"source={source}"]
    for key in ("name", "version", "path", "source"):
        if key in target and target[key] is not None and key != "source":
            bits.append(f"{key}={target[key]}")
        elif key == "source" and "source" in target and target["source"] is not None:
            bits.append(f"plugin_source={target['source']}")

    # enabled / trusted / active flags (best-effort key names)
    enabled = target.get("enabled")
    if enabled is None:
        enabled = target.get("active")
    trusted = target.get("trusted")
    if trusted is None:
        trusted = target.get("isTrusted")
    if trusted is None:
        trusted = target.get("trust")

    if enabled is not None:
        bits.append(f"enabled={enabled}")
    if trusted is not None:
        bits.append(f"trusted={trusted}")

    hooks_info = target.get("hooks")
    if hooks_info is not None:
        if isinstance(hooks_info, (list, dict)):
            bits.append(f"hooks={len(hooks_info)}")
        else:
            bits.append(f"hooks={hooks_info}")

    # severity: missing trust when key present and false → warn
    level = "ok"
    if trusted is False:
        level = "warn"
        bits.append("plugin not trusted (recommend: grok plugin install . --trust)")
    if enabled is False:
        level = "warn"
        bits.append("plugin disabled")

    return ("plugin trust/inventory", level, "; ".join(bits))


def check_plugin_trust() -> SoftResult:
    """Best-effort grok plugin details/list/inspect for trust inventory.

    Never hard-fails when inspect is unavailable (WARN). Callers may promote
    WARN → FAIL under ``--strict``.
    """
    if not shutil.which("grok"):
        return (
            "plugin trust/inventory",
            "warn",
            "inspect unavailable (grok not on PATH)",
        )

    last_err = "no probe succeeded"
    for argv in _TRUST_COMMANDS:
        data = _run_grok_json(argv)
        if data is None:
            last_err = f"probe failed: {' '.join(argv)}"
            continue
        summary = _summarize_plugin_payload(data, source=" ".join(argv[1:4]))
        if summary is not None:
            return summary

    return (
        "plugin trust/inventory",
        "warn",
        f"inspect unavailable ({last_err})",
    )


def run_checks() -> list[tuple[str, bool, str]]:
    return [
        check_grok_on_path(),
        check_plugin_json(),
        check_hooks_scripts(),
        check_pre_tool_use(),
        check_global_pretool_hook(),
        check_skills_omg_prefix(),
        check_agents_present(),
        check_deny_importable(),
    ]


def run_soft_checks() -> list[SoftResult]:
    """Soft/best-effort checks (WARN by default; FAIL under --strict)."""
    return [check_plugin_trust()]


def _format_soft_tag(level: str, *, strict: bool) -> str:
    if level == "ok":
        return "OK  "
    if level == "fail" or (strict and level == "warn"):
        return "FAIL"
    return "WARN"


def run_doctor(
    *,
    strict: bool = False,
    project_root: Path | None = None,
) -> int:
    """Run hard checks + soft trust inventory + compat.claude isolation scan.

    Hard check failures always exit 1.
    Soft trust + compat risks are WARN by default (exit 0 if only warns);
    with ``strict=True`` any soft/compat risk becomes FAIL (exit 1).
    """
    from omg_cli.compat import (
        compat_exit_should_fail,
        format_compat_lines,
        format_isolation_banner,
        scan_compat,
    )

    results = run_checks()
    failed = 0
    soft_warns = 0
    print("oh-my-grok doctor")
    print("-" * 48)
    for name, ok, detail in results:
        tag = "OK  " if ok else "FAIL"
        print(f"[{tag}] {name}: {detail}")
        if not ok:
            failed += 1

    # Soft trust / inventory (best-effort)
    print("-" * 48)
    print("plugin trust / inventory (best-effort)")
    for name, level, detail in run_soft_checks():
        tag = _format_soft_tag(level, strict=strict)
        print(f"[{tag}] {name}: {detail}")
        if level == "fail" or (strict and level == "warn"):
            failed += 1
        elif level == "warn":
            soft_warns += 1

    # compat.claude isolation scan (always runs)
    print("-" * 48)
    print("compat.claude isolation")
    root = Path(project_root) if project_root is not None else Path.cwd().resolve()
    report = scan_compat(project_root=root)
    for line in format_compat_lines(report, strict=strict):
        print(line)
    if compat_exit_should_fail(report, strict=strict):
        failed += 1
        print("[FAIL] compat.claude: risks present under --strict")
    elif report.has_risks:
        soft_warns += 1

    print("-" * 48)
    print(format_isolation_banner())
    print("-" * 48)
    print(f"note: {SOFT_GATE_FOOTER}")
    print("-" * 48)
    if failed:
        print(f"{failed} check(s) failed")
        return 1
    if soft_warns or report.has_risks:
        print(
            "all hard checks passed "
            "(soft/compat risks WARN only; use --strict to fail)"
        )
        return 0
    print("all checks passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    strict = "--strict" in argv
    return run_doctor(strict=strict)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
