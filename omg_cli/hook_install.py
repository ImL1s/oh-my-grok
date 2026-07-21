"""omg_cli/hook_install.py — install/repair the global PreToolUse soft-gate.

ONE transactional implementation shared by BOTH ``omg setup`` and
``scripts/install-plugin.sh`` (the shell script calls ``omg install-hook``), so
the two install paths cannot drift.

It installs the self-contained, stdlib-only standalone
(``hooks/bin/omg_pretool_deny_standalone.py``, produced by
``scripts/generate_standalone_hook.py``) into ``$GROK_HOME/hooks/`` and writes the
discovery JSON pointing at it via ``python3 -I -S <shlex-quoted-abs> || true``.

Why this shape (see docs/security-model.md + the standalone header):
- The global hook must live under ``$GROK_HOME`` (always readable by grok, not a
  TCC-protected ``~/Documents`` checkout, not tied to one workspace). Pointing it at
  a checkout script that also ``import``s ``omg_cli`` bricked every grok tool call
  (python couldn't ``open()`` it → exit 2 → grok read exit 2 as "explicit deny").
- ``|| true`` + the standalone's always-exit-0 / JSON-only-deny mean ANY interpreter
  failure (unreadable script, bad interpreter) fails OPEN, never closed. The path is
  ``shlex.quote``d so a ``$GROK_HOME`` containing shell metacharacters can't inject an
  ``exit 2`` (which would re-brick) or arbitrary commands.

Transactional invariants (never leave a broken hook active):
- **Stage → smoke → publish.** The candidate standalone is written to a temp file
  and smoked (``-I -S`` allow+deny, both must exit 0 with the right JSON) BEFORE it is
  ``os.replace``d onto the live path — a deny-for-benign candidate can never go live
  (its deny JSON at rc 0 would NOT be neutralized by ``|| true``).
- **No hook > broken hook.** Any noncanonical managed JSON on a failed/absent install
  is quarantined to a non-``.json`` name (grok discovers ``*.json``); quarantine is
  verified to have removed the active file.
- The JSON is published LAST, pointing only at the smoke-verified script.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

HOOK_JSON_NAME = "omg-pretool-deny.json"
STANDALONE_BASENAME = "omg_pretool_deny_standalone.py"
MATCHER = "run_terminal_command|Bash|Shell|spawn_subagent|Task"

_PY_IN_CMD_RE = re.compile(r"""["']([^"']+\.py)["']|(\S+\.py)""")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


class HookInstallError(Exception):
    """Raised for a transactional install failure (caller decides fail-open handling)."""


def grok_home(home: Path | None = None) -> Path:
    """Canonical grok config root (honors $GROK_HOME). Reused by setup/doctor/uninstall.

    The env value is user-expanded and made absolute so downstream shell-quoting and
    realpath-containment checks are well-defined. A caller-supplied *home* (tests,
    explicit) is trusted as-is.
    """
    if home is not None:
        return Path(home)
    raw = os.environ.get("GROK_HOME")
    if raw is not None and raw.strip() != "":
        p = Path(os.path.expanduser(raw.strip()))
        if not p.is_absolute():
            p = (Path.cwd() / p)
        return p
    return Path.home() / ".grok"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def committed_standalone(root: Path | None = None) -> Path:
    return (root or _repo_root()) / "hooks" / "bin" / STANDALONE_BASENAME


def launcher_command(installed_py: Path) -> str:
    """Shell command grok runs for the hook.

    ``-I -S`` = isolated + no-site: no PYTHONPATH / user-site / sibling-module /
    sitecustomize injection (the standalone is stdlib-only, so this is safe and
    hermetic). ``|| true`` normalizes ANY interpreter/startup failure (rc != 0,
    especially rc 2 = python "can't open file", which collides with grok's
    "explicit deny") to rc 0 → fail-open. Deny is carried by the standalone's
    stdout ``{"decision":"deny"}`` (honored regardless of exit code). The path is
    ``shlex.quote``d so a ``$GROK_HOME`` with shell metacharacters cannot break out
    of the argument to inject ``exit 2`` or run arbitrary commands.
    """
    return f"python3 -I -S {shlex.quote(str(installed_py))} || true"


def _hook_json_obj(installed_py: Path) -> dict:
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": MATCHER,
                    "hooks": [
                        {
                            "type": "command",
                            "command": launcher_command(installed_py),
                            "timeout": 5,
                        }
                    ],
                }
            ]
        }
    }


def render_hook_json(installed_py: Path) -> str:
    return json.dumps(_hook_json_obj(installed_py), indent=2) + "\n"


def _safe_read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _safe_read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _atomic_write(path: Path, data: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    ok = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        ok = True
    finally:
        if not ok:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _stage_file(final: Path, data: str, *, mode: int) -> Path:
    """Write *data* to a temp file in final's dir and return it — NOT yet published."""
    final.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(final.parent), prefix=f".{final.name}.stage.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    return Path(tmp)


def _smoke(py: Path) -> None:
    """Prove *py* runs and fails-open, exactly as grok will: python3 -I -S, neutral cwd.

    BOTH probes must exit 0 (a nonzero exit, esp. 2, is grok's explicit-deny) and
    return the right JSON decision. Run against the STAGING copy before publishing.
    """
    def run(payload: str) -> tuple[int, str]:
        proc = subprocess.run(
            ["python3", "-I", "-S", str(py)],
            input=payload,
            capture_output=True,
            text=True,
            cwd=tempfile.gettempdir(),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            timeout=10,
        )
        return proc.returncode, (proc.stdout or "").strip()

    def decision(payload: str) -> str:
        rc, out = run(payload)
        if rc != 0:
            raise HookInstallError(f"standalone smoke exited {rc} (must be 0): {out!r}")
        try:
            return json.loads(out)["decision"]
        except Exception as e:
            raise HookInstallError(f"standalone smoke emitted non-decision JSON: {out!r} ({e})")

    if decision('{"tool_name":"run_terminal_command","tool_input":{"command":"ls"}}') != "allow":
        raise HookInstallError("standalone smoke: benign command not allowed")
    if decision('{"tool_name":"run_terminal_command","tool_input":{"command":"claude -p x"}}') != "deny":
        raise HookInstallError("standalone smoke: external CLI not denied")
    if decision('{"tool_name":"spawn_subagent","tool_input":{"subagent_type":"explore"}}') != "deny":
        raise HookInstallError("standalone smoke: spawn without capability_mode not denied")


def json_target_outside_grok_home(json_path: Path, home: Path) -> bool:
    """True if an existing hook JSON references a .py script NOT under grok_home.

    That is the pre-fix "checkout-path" install (or a symlink escape) — the exact
    thing we migrate. Uses realpath so a symlink escaping grok_home is caught.
    """
    data = _safe_read_text(json_path)
    if data is None:
        return False
    try:
        obj = json.loads(data)
    except Exception:
        return False
    try:
        home_r = home.resolve()
    except OSError:
        home_r = home
    groups = ((obj.get("hooks") or {}).get("PreToolUse") or []) if isinstance(obj, dict) else []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for h in group.get("hooks") or []:
            cmd = h.get("command") if isinstance(h, dict) else None
            if not isinstance(cmd, str):
                continue
            m = _PY_IN_CMD_RE.search(cmd)
            if not m:
                continue
            candidate = m.group(1) or m.group(2)
            try:
                p = Path(candidate).resolve()
                p.relative_to(home_r)
            except (ValueError, OSError):
                return True
    return False


def _quarantine(json_path: Path) -> tuple[Path, bool]:
    """Rename a dangerous JSON to a non-``.json`` name so grok stops discovering it.

    Returns (dest, removed) where *removed* confirms the active ``.json`` is gone.
    """
    ts = int(time.time())
    dest = json_path.with_name(f"{json_path.stem}.broken-{ts}.bak")
    try:
        os.replace(json_path, dest)
    except OSError:
        pass
    return dest, (not json_path.is_file())


def install_global_hook(*, home: Path | None = None, root: Path | None = None) -> tuple[Path, str]:
    """Transactionally install/repair the global PreToolUse soft-gate.

    Returns ``(json_path, action)`` with action in {created, updated, repaired,
    unchanged, migrated, quarantined-no-source, skipped-no-source, failed:<Err>}.
    Never raises for a normal failure so ``omg setup`` never crashes.
    """
    gh = grok_home(home)
    hooks_dir = gh / "hooks"
    src = committed_standalone(root)
    json_path = hooks_dir / HOOK_JSON_NAME
    installed_py = hooks_dir / STANDALONE_BASENAME

    if not installed_py.is_absolute():  # canonical grok_home is absolute; guard anyway
        return json_path, "failed:NonAbsolutePath"

    canonical_json = render_hook_json(installed_py)
    prior_json = _safe_read_text(json_path)
    prior_py = _safe_read_bytes(installed_py)
    exists = json_path.is_file()
    outside_home = exists and json_target_outside_grok_home(json_path, gh)
    # Any managed json that is not byte-canonical is treated as dangerous/repairable.
    noncanonical = exists and (prior_json is None or prior_json != canonical_json)

    if not src.is_file():
        # Cannot install → never leave a dangerous json active.
        if noncanonical:
            _dest, removed = _quarantine(json_path)
            return json_path, "quarantined-no-source" if removed else "failed:QuarantineLeftActive"
        return json_path, "skipped-no-source"

    try:
        content = src.read_text(encoding="utf-8")
        compile(content, str(src), "exec")  # reject a corrupt/truncated source
        # STAGE the candidate, SMOKE it, and only then publish — a deny-for-benign
        # candidate (deny JSON at rc 0, which || true cannot neutralize) never goes live.
        staged = _stage_file(installed_py, content, mode=0o755)
        try:
            _smoke(staged)
        except Exception:
            try:
                os.unlink(staged)
            except OSError:
                pass
            raise
        os.replace(staged, installed_py)
        _atomic_write(json_path, canonical_json, mode=0o644)
    except Exception as e:  # noqa: BLE001
        if noncanonical and json_path.is_file():
            _quarantine(json_path)
        return json_path, f"failed:{type(e).__name__}"

    if outside_home:
        return json_path, "migrated"
    if prior_json is None:
        return json_path, "created"
    now_py = _safe_read_bytes(installed_py)
    if prior_json == canonical_json and prior_py == now_py:
        return json_path, "unchanged"
    if prior_json == canonical_json and prior_py != now_py:
        return json_path, "repaired"  # json was fine but the script was missing/stale/corrupt
    return json_path, "updated"


def remove_global_hook(*, home: Path | None = None) -> list[str]:
    """Uninstall: remove the JSON FIRST (stop discovery), then the standalone .py.

    The .py is only removed once the JSON is gone — reversing the order would leave
    an active json pointing at a missing script.
    """
    gh = grok_home(home)
    hooks_dir = gh / "hooks"
    removed: list[str] = []
    jpath = hooks_dir / HOOK_JSON_NAME
    json_gone = True
    if jpath.is_file():
        try:
            jpath.unlink()
            removed.append(str(jpath))
        except OSError:
            json_gone = False
    if json_gone:
        pypath = hooks_dir / STANDALONE_BASENAME
        if pypath.is_file():
            try:
                pypath.unlink()
                removed.append(str(pypath))
            except OSError:
                pass
    return removed


def main(argv: list[str] | None = None) -> int:
    """`omg install-hook` / `python3 -m omg_cli.hook_install` entrypoint."""
    import argparse

    parser = argparse.ArgumentParser(description="Install/repair the global PreToolUse soft-gate")
    parser.add_argument("--remove", action="store_true", help="uninstall the global hook")
    args = parser.parse_args(argv)
    if args.remove:
        removed = remove_global_hook()
        for r in removed:
            print(f"removed {r}")
        if not removed:
            print("global hook absent (nothing to remove)")
        return 0
    json_path, action = install_global_hook()
    print(f"global PreToolUse soft-gate: {json_path} -> {action}")
    # Nonzero when no working hook was installed (failed / no source), so callers
    # (install-plugin.sh, CI) can detect it.
    installed_ok = action in ("created", "updated", "repaired", "unchanged", "migrated")
    return 0 if installed_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
