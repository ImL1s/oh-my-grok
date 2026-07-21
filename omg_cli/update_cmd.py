"""omg update — git pull (best-effort) + refresh installed plugin."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def run_update(*, root: Path | None = None, runner=subprocess.run) -> int:
    """Pull latest checkout and re-run install-plugin.sh (best-effort).

    Returns 0 on the success path; nonzero only if root is missing.
    """
    root = root or Path(__file__).resolve().parents[1]
    root = Path(root)
    if not root.is_dir():
        print(f"omg update: root missing: {root}", file=sys.stderr)
        return 1

    print(f"omg update: {root}")

    # 1–2. git fetch + pull --ff-only (continue on dirty tree / not a git repo)
    try:
        fetch = runner(
            ["git", "-C", str(root), "fetch", "--tags", "--quiet"],
            capture_output=True,
            text=True,
        )
        if getattr(fetch, "returncode", 1) != 0:
            print(
                "omg update: git fetch failed "
                f"(rc={getattr(fetch, 'returncode', '?')}); continuing",
                file=sys.stderr,
            )
    except OSError as exc:
        print(f"omg update: git fetch error: {exc}; continuing", file=sys.stderr)

    try:
        pull = runner(
            ["git", "-C", str(root), "pull", "--ff-only"],
            capture_output=True,
            text=True,
        )
        if getattr(pull, "returncode", 1) != 0:
            err = (getattr(pull, "stderr", None) or getattr(pull, "stdout", None) or "").strip()
            note = err or "dirty tree / not a git repo / diverged history"
            print(f"omg update: git pull --ff-only failed ({note}); continuing")
        else:
            print("omg update: git pull --ff-only ok")
    except OSError as exc:
        print(f"omg update: git pull error: {exc}; continuing")

    # 3. scripts/install-plugin.sh if present and executable
    script = root / "scripts" / "install-plugin.sh"
    if script.is_file() and os.access(script, os.X_OK):
        try:
            result = runner(
                [str(script)],
                cwd=str(root),
                capture_output=True,
                text=True,
            )
            if getattr(result, "returncode", 1) != 0:
                # Forward captured script output so recovery instructions
                # (e.g. reinstall after failed refresh) reach the user.
                out = getattr(result, "stdout", None) or ""
                err = getattr(result, "stderr", None) or ""
                if out:
                    print(out, end="" if out.endswith("\n") else "\n")
                if err:
                    print(err, end="" if err.endswith("\n") else "\n", file=sys.stderr)
                print(
                    "omg update: install-plugin.sh exited "
                    f"rc={getattr(result, 'returncode', '?')}",
                    file=sys.stderr,
                )
            else:
                print("omg update: install-plugin.sh ok")
        except OSError as exc:
            print(f"omg update: install-plugin.sh error: {exc}", file=sys.stderr)
    else:
        print(f"omg update: install-plugin.sh not found or not executable at {script}")

    print("next: omg doctor")
    return 0
