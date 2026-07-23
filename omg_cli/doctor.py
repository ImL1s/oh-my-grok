# omg_cli/doctor.py
"""omg doctor — health checks for plugin + CLI environment."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
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
    "hooks/bin/omg_pretool_deny_standalone.py",
)

# macOS TCC-protected home subdirectories: a checkout here made the global hook
# unreadable to grok processes without Documents/Desktop/Downloads access.
_TCC_PROTECTED_DIR_NAMES = ("Documents", "Desktop", "Downloads")

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
        "omg-analyst.md",
        "omg-code-reviewer.md",
        "omg-architect.md",
        "omg-qa-tester.md",
    ]
    missing = [n for n in expected if not (agents_dir / n).is_file()]
    if missing:
        return _check("agents", False, f"missing: {', '.join(missing)}")
    # Count all agent markdown files for honesty (core + optional)
    total = len([p for p in agents_dir.glob("*.md") if p.is_file()])
    return _check("agents", True, f"{len(expected)} required present ({total} total)")


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


_PY_IN_CMD_RE = re.compile(r"""["']([^"']+\.py)["']|(\S+\.py)""")


def _extract_pretool_hooks(data: Any) -> tuple[list[str], list[str], list[dict]]:
    """Return (commands, matchers, hook_entries) under hooks.PreToolUse[*].hooks[*]."""
    commands: list[str] = []
    matchers: list[str] = []
    entries: list[dict] = []
    hooks_root = (data.get("hooks") or {}) if isinstance(data, dict) else {}
    for group in hooks_root.get("PreToolUse") or []:
        if not isinstance(group, dict):
            continue
        if group.get("matcher") is not None:
            matchers.append(str(group.get("matcher") or ""))
        for h in group.get("hooks") or []:
            if isinstance(h, dict):
                entries.append(h)
                if isinstance(h.get("command"), str):
                    commands.append(h["command"])
    return commands, matchers, entries


def _run_hook_command(command: str, payload: str) -> tuple[int, str]:
    """Run the hook's ACTUAL shell command with a stdin event; return (rc, stdout).

    Exercises the real launcher (``python3 -I -S "<abs>" || true``) end-to-end —
    the only way to prove the installed hook actually runs and decides correctly.
    """
    cwd = "/tmp" if os.path.isdir("/tmp") else None
    proc = subprocess.run(
        ["/bin/sh", "-c", command],
        input=payload,
        capture_output=True,
        text=True,
        cwd=cwd,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        timeout=10,
    )
    return proc.returncode, (proc.stdout or "").strip()


def check_global_pretool_hook() -> tuple[str, bool, str]:
    """Require a SAFE global PreToolUse soft-gate under ``$GROK_HOME/hooks``.

    Hard predicates use real ``open()`` / ``resolve()`` / subprocess — NOT
    ``os.access``, which checks permission bits and cannot see macOS TCC (the very
    false-green that let a checkout-path install pass while grok could not open the
    script → python exit 2 → grok read it as an explicit deny → every tool blocked).
    FAIL when: json missing / malformed / has ≠1 command hook (a 2nd could exit 2) /
    matcher missing shell or spawn / script escapes ``$GROK_HOME`` (checkout or
    symlink) / not a readable regular file / a neutral-cwd behavioral smoke returns
    the wrong allow/deny decisions.
    """
    from omg_cli.hook_install import (
        MATCHER,
        STANDALONE_BASENAME,
        grok_home,
        launcher_command,
    )

    name = "global PreToolUse soft-gate"
    gh = grok_home()
    path = gh / "hooks" / GLOBAL_PRETOOL_HOOK_NAME
    if not path.is_file():
        return _check(name, False, f"missing {path} (run: omg install-hook)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return _check(name, False, f"unreadable/invalid json {path}: {e}")

    commands, matchers, entries = _extract_pretool_hooks(data)
    if not commands:
        return _check(name, False, f"{path} has no PreToolUse command entries")
    if len(commands) != 1:
        return _check(
            name, False,
            f"{path} has {len(commands)} command hooks (expected exactly 1; a second "
            "command is a second chance to exit 2 and block)",
        )
    if matchers != [MATCHER]:
        return _check(
            name, False,
            f"{path} matcher is not the canonical shell+spawn matcher: {matchers!r}",
        )
    for h in entries:
        if h.get("type") != "command":
            return _check(name, False, f"{path} hook type must be 'command' (got {h.get('type')!r})")
        to = h.get("timeout")
        if to is not None and (not isinstance(to, int) or isinstance(to, bool) or to <= 0 or to > 120):
            return _check(name, False, f"{path} invalid timeout: {to!r}")

    cmd = commands[0]
    # AUTHORITATIVE check first: the command must be EXACTLY our canonical launcher for
    # the canonical install path. `expected_py` is known directly (no regex), so a
    # metacharacter $GROK_HOME can't confuse path extraction and false-FAIL a correct
    # install. This rejects an appended `; exit 2` / `$()` / `` ` `` injection or a
    # dropped `|| true` — any of which could exit 2 (grok's explicit deny) and block
    # every tool, which a substring / decision-only smoke would miss.
    expected_py = gh / "hooks" / STANDALONE_BASENAME
    if cmd != launcher_command(expected_py):
        # Non-canonical — surface the most useful reason (regex only for the message).
        m = _PY_IN_CMD_RE.search(cmd)
        if m:
            script = Path(m.group(1) or m.group(2))
            try:
                r = script.resolve()
                r.relative_to(gh.resolve())
            except (OSError, ValueError):
                return _check(
                    name, False,
                    f"hook script escapes $GROK_HOME ({script}); checkout-path / symlink "
                    "installs brick other workspaces — run: omg install-hook",
                )
        return _check(
            name, False,
            f"{path} command is not the canonical `-I -S … || true` launcher for "
            f"{expected_py}: {cmd!r} — run: omg install-hook",
        )
    # Canonical command ⇒ the script IS expected_py. Resolve it (catch a symlink at the
    # canonical path escaping $GROK_HOME) and prove doctor can really open() it.
    try:
        resolved = expected_py.resolve()
        resolved.relative_to(gh.resolve())
    except (OSError, ValueError):
        return _check(
            name, False,
            f"hook script escapes $GROK_HOME via symlink ({expected_py}); run: omg install-hook",
        )
    if not resolved.is_file():
        return _check(name, False, f"hook script missing / not a regular file: {resolved}")
    try:
        with open(resolved, "rb"):           # REAL open — os.access cannot see TCC
            pass
    except OSError as e:
        return _check(name, False, f"hook script cannot be opened: {e}")

    # Behavioral smoke through the ACTUAL shell command (exercises -I -S + || true).
    # EVERY probe must exit 0 (a nonzero exit — esp. 2 — is grok's explicit deny) and
    # return the right JSON decision. Parse the decision; never substring-match.
    probes = (
        ("allow", '{"tool_name":"run_terminal_command","tool_input":{"command":"ls -la"}}'),
        ("deny", '{"tool_name":"run_terminal_command","tool_input":{"command":"claude -p x"}}'),
        ("deny", '{"tool_name":"spawn_subagent","tool_input":{"subagent_type":"explore"}}'),
    )
    for want, payload in probes:
        try:
            rc, out = _run_hook_command(cmd, payload)
        except (OSError, subprocess.SubprocessError) as e:
            return _check(name, False, f"hook smoke could not run: {e}")
        if rc != 0:
            return _check(
                name, False,
                f"hook smoke exited {rc} (must be 0; a nonzero/2 exit is grok's explicit "
                f"deny and would block the tool): {out!r}",
            )
        try:
            got = json.loads(out)["decision"]
        except Exception:
            return _check(name, False, f"hook smoke emitted non-decision JSON: {out!r}")
        if got != want:
            return _check(name, False, f"hook smoke decision {got!r} (want {want!r}) for {payload}")

    return _check(name, True, f"{path} → {resolved} (canonical launcher; smoke rc0 allow/deny/spawn ok)")


def check_global_pretool_hook_freshness() -> SoftResult:
    """WARN (→ FAIL under --strict) if the installed standalone drifted from the
    committed one, or if ``$GROK_HOME`` resolves under a TCC-protected location
    (doctor's own read succeeding does not prove grok's process can read it — only a
    live cross-workspace grok canary proves that seam)."""
    from omg_cli.hook_install import committed_standalone, grok_home, STANDALONE_BASENAME

    name = "global soft-gate freshness"
    gh = grok_home()
    installed = gh / "hooks" / STANDALONE_BASENAME
    if not installed.is_file():
        return (name, "ok", f"standalone not installed ({installed})")
    src = committed_standalone()
    try:
        ih = hashlib.sha256(installed.read_bytes()).hexdigest()
    except OSError as e:
        return (name, "warn", f"cannot hash installed hook: {e}")
    if src.is_file():
        try:
            sh = hashlib.sha256(src.read_bytes()).hexdigest()
        except OSError:
            sh = None
        if sh is not None and ih != sh:
            return (name, "warn", "installed standalone is STALE vs committed (run: omg install-hook)")
    try:
        parts = set(gh.resolve().parts)
    except OSError:
        parts = set(gh.parts)
    tcc = sorted(parts & set(_TCC_PROTECTED_DIR_NAMES))
    if tcc:
        return (
            name, "warn",
            f"$GROK_HOME is under TCC-protected {tcc}; a grok process without that "
            "access may fail to read the hook — prefer the default ~/.grok",
        )
    # Grok merges EVERY $GROK_HOME/hooks/*.json. Another PreToolUse command hook can
    # exit 2 and block a tool regardless of ours — we can't control third-party hooks,
    # so surface them (honest WARN, not our FAIL).
    others: list[str] = []
    hooks_dir = gh / "hooks"
    if hooks_dir.is_dir():
        for jf in sorted(hooks_dir.glob("*.json")):
            if jf.name == GLOBAL_PRETOOL_HOOK_NAME:
                continue
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            cmds, _mm, _ee = _extract_pretool_hooks(data)
            if cmds:
                others.append(jf.name)
    if others:
        return (
            name, "warn",
            f"other PreToolUse hook file(s) present ({others}); a hook there that exits 2 "
            "can also block a tool — audit them (grok merges all $GROK_HOME/hooks/*.json)",
        )
    return (name, "ok", f"installed standalone matches committed (sha {ih[:12]}…)")


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


# Foreign orchestration that can pollute OMG-only live attribution (Codex P0-5)
_FOREIGN_ORCH_MARKERS: tuple[str, ...] = (
    "oh-my-claudecode",
    "oh-my-codex",
    "ralph-loop",
    "oh-my-opencode",
)


def check_effective_discovery_foreign() -> SoftResult:
    """Soft: snapshot ``grok inspect --json`` for foreign orchestration markers.

    Does not hard-fail by default (WARN). Use ``omg doctor --strict`` to promote.
    """
    if not shutil.which("grok"):
        return (
            "effective discovery (foreign orch)",
            "warn",
            "grok not on PATH; cannot snapshot discovery graph",
        )
    data = _run_grok_json(("grok", "inspect", "--json"))
    if data is None:
        return (
            "effective discovery (foreign orch)",
            "warn",
            "grok inspect --json unavailable or non-JSON",
        )

    blob_parts: list[str] = []

    def _walk(obj: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("name", "id", "plugin", "skill", "path", "source"):
                    blob_parts.append(str(v))
                _walk(v, depth + 1)
        elif isinstance(obj, list):
            for x in obj[:200]:
                _walk(x, depth + 1)
        elif isinstance(obj, str) and len(obj) < 400:
            blob_parts.append(obj)

    _walk(data)
    blob = "\n".join(blob_parts).lower()
    hits = sorted({m for m in _FOREIGN_ORCH_MARKERS if m in blob})
    if hits:
        return (
            "effective discovery (foreign orch)",
            "warn",
            "foreign orchestration in grok inspect: "
            + ", ".join(hits)
            + " — live evidence may not be OMG-only; save inspect JSON with suite evidence",
        )
    return (
        "effective discovery (foreign orch)",
        "ok",
        "no high-signal foreign orch markers in inspect snapshot",
    )


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


def check_global_rules() -> SoftResult:
    """Soft: status of ~/.grok/rules/omg.md (GROK_HOME-aware) OMG contract."""
    from omg_cli.guidance import rules_status

    name = "global rules (~/.grok/rules/omg.md)"
    try:
        st = rules_status()
    except Exception as e:
        return (name, "warn", f"status unavailable ({type(e).__name__})")
    if st.get("corrupt"):
        return (name, "fail", "corrupt OMG markers — re-run: omg setup")
    if not st.get("present"):
        return (
            name,
            "warn",
            "not installed — run: omg setup (injects OMG contract every session)",
        )
    problems = []
    if not st.get("version_ok"):
        problems.append(
            f"version {st.get('installed_version')} != {st.get('expected_version')}"
        )
    if st.get("drift"):
        problems.append("hand-edited inside markers")
    if not st.get("source_hash_ok"):
        problems.append("source-hash mismatch")
    if problems:
        return (name, "warn", "; ".join(problems) + " — re-run: omg setup")
    return (name, "ok", f"present, v{st.get('installed_version')}")


def _plugin_list_entries(data: Any) -> list[dict[str, Any]]:
    """Normalize ``grok plugin list --json`` into a list of dict entries."""
    if data is None:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("plugins", "items", "data", "result"):
            nested = data.get(key)
            if isinstance(nested, list):
                return [x for x in nested if isinstance(x, dict)]
        return [data]
    return []


def _entry_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("id") or item.get("plugin") or "")


def _entry_source_path(item: dict[str, Any]) -> str:
    for key in ("source", "path", "installPath", "install_path"):
        val = item.get(key)
        if val is not None and str(val).strip():
            return str(val)
    return ""


def check_plugin_version_drift() -> SoftResult:
    """Soft: installed plugin version vs local plugin.json; detect duplicates."""
    name = "plugin version drift"
    try:
        local = str(
            json.loads((plugin_root() / "plugin.json").read_text(encoding="utf-8"))[
                "version"
            ]
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
        return (name, "warn", f"cannot read local plugin.json version ({e})")

    data = _run_grok_json(("grok", "plugin", "list", "--json"))
    if data is None:
        return (name, "warn", "cannot read installed inventory")

    matches = [
        item
        for item in _plugin_list_entries(data)
        if PLUGIN_NAME in _entry_name(item)
    ]
    if not matches:
        return (name, "warn", f"{PLUGIN_NAME} not installed via grok plugin")

    # Duplicate sources (different install paths for the same name)
    sources: list[str] = []
    for item in matches:
        src = _entry_source_path(item)
        if src:
            sources.append(src)
    unique_sources = {s.rstrip("/") for s in sources if s}
    if len(matches) > 1 and len(unique_sources) > 1:
        bits: list[str] = []
        for item in matches:
            key = _entry_name(item)
            src = _entry_source_path(item) or "?"
            bits.append(f"{key}@{src}")
        return (
            name,
            "warn",
            "duplicate oh-my-grok entries with differing source/path: "
            + "; ".join(bits)
            + " — run: grok plugin uninstall oh-my-grok (remove the stale one)",
        )

    mismatches: list[str] = []
    for item in matches:
        installed = item.get("version")
        if installed is None:
            continue
        installed_s = str(installed)
        if installed_s != local:
            mismatches.append(installed_s)

    if mismatches:
        shown = mismatches[0]
        return (
            name,
            "warn",
            f"installed {shown} != local {local} — re-run scripts/install-plugin.sh "
            "(grok plugin update)",
        )
    return (name, "ok", f"installed == local ({local})")


def check_plugin_enabled() -> SoftResult:
    """Soft: oh-my-grok present in GROK_HOME/config.toml [plugins].enabled."""
    name = "plugin enabled ([plugins].enabled)"
    grok_home = Path(os.environ.get("GROK_HOME") or (Path.home() / ".grok"))
    cfg = grok_home / "config.toml"
    if not cfg.is_file():
        return (
            name,
            "warn",
            "no ~/.grok/config.toml; cannot confirm enabled",
        )
    try:
        with cfg.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError:
        return (name, "warn", "config.toml unreadable")
    except OSError:
        return (name, "warn", "config.toml unreadable")

    enabled = (data.get("plugins") or {}).get("enabled") or []
    if not isinstance(enabled, list):
        enabled = []

    def _is_omg(entry: Any) -> bool:
        s = str(entry)
        return s == PLUGIN_NAME or s.endswith("/" + PLUGIN_NAME)

    if any(_is_omg(e) for e in enabled):
        return (name, "ok", "oh-my-grok in [plugins].enabled")
    return (
        name,
        "warn",
        "oh-my-grok NOT in [plugins].enabled — run: grok plugin enable oh-my-grok "
        "(plugins are disabled by default)",
    )


def _import_capabilities_lock_mod() -> Any:
    """Load scripts/generate_capabilities_lock.py (not a package module)."""
    import importlib.util

    script = plugin_root() / "scripts" / "generate_capabilities_lock.py"
    # Prefer import via scripts on sys.path when the file is reachable that way.
    scripts_dir = str(plugin_root() / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        import generate_capabilities_lock as mod  # type: ignore

        return mod
    except ImportError:
        pass
    if not script.is_file():
        raise ImportError(f"missing {script}")
    spec = importlib.util.spec_from_file_location("generate_capabilities_lock", script)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {script}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_capabilities_lock() -> SoftResult:
    """Soft: local-checkout skills/agents match omg_capabilities.lock.json (commit hygiene)."""
    name = "capabilities lock (local checkout)"
    try:
        mod = _import_capabilities_lock_mod()
    except Exception as e:
        return (name, "warn", f"cannot load lock generator ({type(e).__name__}: {e})")
    root = plugin_root()
    try:
        current = mod.compute_lock(root)
        stored = mod.read_lock(root)
    except Exception as e:
        return (name, "warn", f"lock compute failed ({type(e).__name__}: {e})")
    if stored is None:
        return (
            name,
            "warn",
            "no omg_capabilities.lock.json (run scripts/generate_capabilities_lock.py)",
        )
    if not mod.lock_matches(stored, current):
        return (
            name,
            "warn",
            "local checkout version/session surface/skills/agents changed since lock "
            "— regenerate: "
            "python3 scripts/generate_capabilities_lock.py "
            "(commit-hygiene guard; installed-version drift is covered by "
            "'plugin version drift')",
        )
    n = len(current.get("files") or {})
    return (name, "ok", f"local checkout: {n} files match lock")


def _entry_install_path(item: dict[str, Any]) -> str:
    """Installed frozen snapshot path (prefer installPath / path over source)."""
    for key in ("installPath", "install_path", "path"):
        val = item.get(key)
        if val is not None and str(val).strip():
            return str(val)
    return ""


def _paths_match_resolved(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, RuntimeError):
        return str(a).rstrip("/") == str(b).rstrip("/")


def check_installed_capabilities_lock() -> SoftResult:
    """Soft: INSTALLED frozen snapshot skills/agents match the committed lock.

    OMX-parity installed-drift detector. Distinct from
    ``check_capabilities_lock`` (local-checkout commit hygiene).
    """
    name = "installed capabilities lock"
    try:
        data = _run_grok_json(("grok", "plugin", "list", "--json"))
        if data is None:
            return (name, "warn", "cannot locate installed snapshot")

        checkout = plugin_root().resolve()
        matches = [
            item
            for item in _plugin_list_entries(data)
            if PLUGIN_NAME in _entry_name(item)
        ]
        installed_dir: Path | None = None
        for item in matches:
            # Entry whose source/path resolves to this checkout.
            candidates: list[str] = []
            src = _entry_source_path(item)
            if src:
                candidates.append(src)
            for key in ("source", "path"):
                val = item.get(key)
                if val is not None and str(val).strip():
                    s = str(val)
                    if s not in candidates:
                        candidates.append(s)
            if not any(_paths_match_resolved(c, checkout) for c in candidates):
                continue
            inst = _entry_install_path(item)
            if not inst:
                # source matched but no distinct install path field — skip
                continue
            installed_dir = Path(inst)
            break

        if installed_dir is None:
            return (name, "warn", "cannot locate installed snapshot")
        if not installed_dir.is_dir():
            return (name, "warn", "cannot locate installed snapshot")

        mod = _import_capabilities_lock_mod()
        installed_lock = mod.compute_lock_for(installed_dir)
        stored = mod.read_lock(plugin_root())
        if stored is None:
            return (
                name,
                "warn",
                "no omg_capabilities.lock.json in checkout — cannot verify installed",
            )
        if not mod.lock_matches(stored, installed_lock):
            return (
                name,
                "warn",
                "INSTALLED skills/agents differ or version/session surface drifted "
                "from committed lock — re-run "
                "scripts/install-plugin.sh (grok plugin update)",
            )
        return (
            name,
            "ok",
            "installed skills/agents match committed lock",
        )
    except Exception as e:
        return (
            name,
            "warn",
            f"cannot locate installed snapshot ({type(e).__name__}: {e})",
        )


def check_installed_release_identity() -> SoftResult:
    """Verify the immutable stage/current/CLI/plugin/receipt byte identity.

    Absence is an honest development/source-install warning.  Once an immutable
    receipt exists, any malformed receipt, pointer drift, package drift or owned
    global-file drift is a hard failure even in non-strict doctor mode.
    """

    name = "immutable install identity"
    try:
        from omg_cli.hook_install import grok_home
        from omg_cli.setup_cmd import compute_package_identity, verified_current_install

        gh = grok_home()
        store = gh / "omg"
        current = store / "current"
        receipt_pointer = store / "current-receipt"
        if not os.path.lexists(current) and not os.path.lexists(receipt_pointer):
            return (
                name,
                "warn",
                "no immutable release receipt (development/source install); "
                "use the checksum-verified release installer",
            )
        # During the install transaction the jointly switched CLI/plugin/current
        # surfaces must pass strict doctor *before* an installed receipt can be
        # published.  The installer supplies both exact values only to that child
        # process; validate every available byte/pointer invariant, then a second
        # environment-free strict doctor runs after receipt publication.
        if current.is_symlink() and not os.path.lexists(receipt_pointer):
            expected = os.environ.get("OMG_EXPECTED_INSTALL_DIGEST", "")
            expected_stage = os.environ.get("OMG_EXPECTED_INSTALL_STAGE", "")
            if not re.fullmatch(r"[0-9a-f]{64}", expected) or not expected_stage:
                return (name, "fail", "managed current pointer has no immutable receipt")
            stage = current.resolve(strict=True)
            if stage != Path(expected_stage).resolve(strict=True):
                return (name, "fail", "pending install stage differs from expected stage")
            stage_identity = compute_package_identity(stage)
            if stage_identity["digest"] != expected:
                return (name, "fail", "pending immutable stage digest differs")
            active_identity = compute_package_identity(plugin_root())
            if active_identity["digest"] != expected:
                return (name, "fail", "pending active CLI/plugin package differs")
            cli = _home() / ".local" / "bin" / "omg"
            if not cli.is_symlink() or cli.resolve(strict=True) != (stage / "bin" / "omg").resolve():
                return (name, "fail", "pending CLI pointer differs from stage")
            return (
                name,
                "ok",
                f"pending transaction version={stage_identity['version']} digest={expected[:16]}",
            )
        if not current.is_symlink() or not receipt_pointer.is_symlink():
            return (name, "fail", "managed current/receipt pointers are not symlinks")
        cli = _home() / ".local" / "bin" / "omg"
        verified = verified_current_install(store, cli)
        stage = verified.stage
        receipt = verified.receipt
        expected = str(receipt["installed"]["package_digest"])
        stage_identity = compute_package_identity(stage)
        if stage_identity["digest"] != expected:
            return (name, "fail", "immutable stage digest differs from receipt")
        recorded_inventory = receipt["installed"].get("inventory")
        if recorded_inventory is not None and recorded_inventory != stage_identity["inventory"]:
            return (name, "fail", "immutable stage inventory differs from receipt")
        active_identity = compute_package_identity(plugin_root())
        if active_identity["digest"] != expected:
            return (name, "fail", "active CLI/plugin package differs from receipt")
        if receipt["mode"] == "release":
            for key in ("asset_name", "asset_sha256", "checksums_sha256"):
                value = receipt["source"].get(key)
                if key == "checksums_sha256" and not value:
                    # Explicit --asset-sha256 is a valid manual/offline trust root.
                    continue
                if not isinstance(value, str) or (key.endswith("sha256") and not re.fullmatch(r"[0-9a-f]{64}", value)):
                    return (name, "fail", "release receipt checksum identity is incomplete")
        owned = receipt.get("owned_inventory")
        if not isinstance(owned, list):
            return (name, "fail", "receipt owned inventory is malformed")
        for row in owned:
            if not isinstance(row, dict) or row.get("kind") not in {"global_hook", "global_guidance"}:
                continue
            path = Path(str(row.get("path") or ""))
            expected_file = str(row.get("identity") or "")
            if row.get("kind") == "global_guidance":
                from omg_cli.guidance import render_managed_block, rules_status

                status = rules_status(version=str(receipt["installed"]["package_version"]), home=gh)
                actual_owned = hashlib.sha256(
                    render_managed_block(str(receipt["installed"]["package_version"])).encode("utf-8")
                ).hexdigest()
                if (
                    not status.get("present")
                    or status.get("corrupt")
                    or not status.get("version_ok")
                    or not status.get("source_hash_ok")
                    or status.get("drift")
                    or actual_owned != expected_file
                ):
                    return (name, "fail", "owned global_guidance block drifted")
                continue
            if (
                not path.is_file()
                or path.is_symlink()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected_file
            ):
                return (name, "fail", f"owned {row.get('kind')} bytes drifted")
        return (
            name,
            "ok",
            f"version={stage_identity['version']} digest={expected[:16]} receipt={receipt['receipt_hash'][:16]}",
        )
    except Exception as e:
        # Bound and redact exception type only; no tokens, prompts, commands or
        # credential-bearing URLs are echoed from malformed receipt content.
        return (name, "fail", f"identity readback failed ({type(e).__name__})")


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
    return [
        check_plugin_trust(),
        check_effective_discovery_foreign(),
        check_global_rules(),
        check_global_pretool_hook_freshness(),
        check_plugin_version_drift(),
        check_plugin_enabled(),
        check_capabilities_lock(),
        check_installed_capabilities_lock(),
        check_installed_release_identity(),
    ]


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
