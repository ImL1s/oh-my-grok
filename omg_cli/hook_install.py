"""omg_cli/hook_install.py — install/repair the global PreToolUse soft-gate.

ONE transactional implementation shared by BOTH ``omg setup`` and
``scripts/install-plugin.sh`` (the shell script calls ``omg install-hook``), so
the two install paths cannot drift.

It installs the self-contained, stdlib-only standalone
(``hooks/bin/omg_pretool_deny_standalone.py``, produced by
``scripts/generate_standalone_hook.py``) into ``$GROK_HOME/hooks/`` and writes the
discovery JSON pointing at it via ``python3 -I -S "<abs>" || true``.

Why this shape (see docs/security-model.md + hooks/bin/omg_pretool_deny_standalone.py):
- The global hook must live under ``$GROK_HOME`` (always readable by grok, not a
  TCC-protected ``~/Documents`` checkout, not tied to one workspace). Pointing it at
  a checkout script that also ``import``s ``omg_cli`` bricked every grok tool call
  (python couldn't ``open()`` it → exit 2 → grok read exit 2 as "explicit deny").
- ``|| true`` + the standalone's always-exit-0 / JSON-only-deny mean ANY failure
  (unreadable script, bad interpreter) fails OPEN, never closed.

Invariants:
- **Never leave a broken hook active.** Atomic writes; validate + smoke the
  standalone before publishing the JSON. On failure, QUARANTINE a dangerous old
  checkout-pointing JSON to a non-``.json`` name (grok discovers ``*.json``) rather
  than leave it denying every tool.
- **No hook > broken hook.** Never write a JSON that points at a missing/unverified
  target.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

HOOK_JSON_NAME = "omg-pretool-deny.json"
STANDALONE_BASENAME = "omg_pretool_deny_standalone.py"
MATCHER = "run_terminal_command|Bash|Shell|spawn_subagent|Task"

_PY_IN_CMD_RE = re.compile(r"""["']([^"']+\.py)["']|(\S+\.py)""")


class HookInstallError(Exception):
    """Raised for a transactional install failure (caller decides fail-open handling)."""


def grok_home(home: Path | None = None) -> Path:
    """Canonical grok config root (honors $GROK_HOME). Reused by setup/doctor/uninstall."""
    if home is not None:
        return Path(home)
    raw = os.environ.get("GROK_HOME")
    if raw is not None and raw.strip() != "":
        return Path(raw)
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
    stdout ``{"decision":"deny"}`` (honored regardless of exit code).
    """
    return f'python3 -I -S "{installed_py}" || true'


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


def _atomic_write(path: Path, data: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = None  # consumed by replace
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _smoke(py: Path) -> None:
    """Prove the installed script actually runs and fails-open, from a neutral cwd.

    ``python3 -I -S`` with a minimal env: this is the exact runtime grok will use
    for the hook, so a green smoke here is real proof it will not brick sessions.
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

    rc, out = run('{"tool_name":"run_terminal_command","tool_input":{"command":"ls"}}')
    if rc != 0 or '"allow"' not in out:
        raise HookInstallError(f"standalone smoke(allow) failed: rc={rc} out={out!r}")
    rc, out = run('{"tool_name":"run_terminal_command","tool_input":{"command":"claude -p x"}}')
    if rc != 0 or '"deny"' not in out:
        raise HookInstallError(f"standalone smoke(deny) failed: rc={rc} out={out!r}")


def json_target_outside_grok_home(json_path: Path, home: Path) -> bool:
    """True if an existing hook JSON references a .py script NOT under grok_home.

    That is the pre-fix "checkout-path" install (or a symlink escape) that bricks
    other workspaces — the exact thing we must migrate/quarantine.
    """
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    try:
        home_r = home.resolve()
    except OSError:
        home_r = home
    groups = ((data.get("hooks") or {}).get("PreToolUse") or []) if isinstance(data, dict) else []
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


def _quarantine(json_path: Path) -> Path:
    """Rename a dangerous JSON to a non-``.json`` name so grok stops discovering it."""
    ts = int(time.time())
    dest = json_path.with_name(f"{json_path.stem}.broken-{ts}.bak")
    try:
        os.replace(json_path, dest)
    except OSError:
        pass
    return dest


def install_global_hook(*, home: Path | None = None, root: Path | None = None) -> tuple[Path, str]:
    """Transactionally install/repair the global PreToolUse soft-gate.

    Returns ``(json_path, action)`` with action in {created, updated, unchanged,
    migrated, quarantined-no-source, skipped-no-source, failed:<Err>}.
    Never raises for a normal failure so ``omg setup`` never crashes.
    """
    gh = grok_home(home)
    hooks_dir = gh / "hooks"
    src = committed_standalone(root)
    json_path = hooks_dir / HOOK_JSON_NAME
    installed_py = hooks_dir / STANDALONE_BASENAME

    was_broken = json_path.is_file() and json_target_outside_grok_home(json_path, gh)
    prior = json_path.read_text(encoding="utf-8") if json_path.is_file() else None

    if not src.is_file():
        # Cannot install → never leave a checkout-pointing json active.
        if was_broken:
            _quarantine(json_path)
            return json_path, "quarantined-no-source"
        return json_path, "skipped-no-source"

    try:
        content = src.read_text(encoding="utf-8")
        compile(content, str(src), "exec")  # reject a corrupt/truncated source
        _atomic_write(installed_py, content, mode=0o755)
        _smoke(installed_py)  # prove it runs + fails-open BEFORE publishing the json
        new_json = render_hook_json(installed_py)
        _atomic_write(json_path, new_json, mode=0o644)
    except Exception as e:  # noqa: BLE001
        if was_broken and json_path.is_file():
            _quarantine(json_path)
        return json_path, f"failed:{type(e).__name__}"

    if was_broken:
        return json_path, "migrated"
    if prior is None:
        return json_path, "created"
    if prior == new_json:
        return json_path, "unchanged"
    return json_path, "updated"


def remove_global_hook(*, home: Path | None = None) -> list[str]:
    """Uninstall: remove the JSON FIRST (stop discovery), then the standalone .py.

    Reversing the order would leave an active json pointing at a missing script.
    """
    gh = grok_home(home)
    hooks_dir = gh / "hooks"
    removed: list[str] = []
    for name in (HOOK_JSON_NAME, STANDALONE_BASENAME):
        p = hooks_dir / name
        if p.is_file():
            try:
                p.unlink()
                removed.append(str(p))
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
    return 0 if not action.startswith("failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
