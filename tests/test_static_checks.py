"""#24 static analysis entrypoint contracts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_static_coverage_check_passes() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_static_coverage.py"), "--check"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "static_coverage_ok" in proc.stdout


def test_static_checks_script_green() -> None:
    """Reproduce CI static step locally (requires ruff+mypy on PATH/venv)."""
    env = {**dict(**{k: v for k, v in __import__("os").environ.items()})}
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "static_checks.sh")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert "ALL_STATIC_CHECKS_OK" in proc.stdout


def test_ruff_roots_discover_new_module(tmp_path: Path) -> None:
    """A temporary bad module under omg_cli is detected without workflow edits.

    Uses a throwaway file name under the real tree is risky; instead assert the
    coverage inventory would include any path under omg_cli/ via STATIC_ROOTS.
    """
    from scripts.check_static_coverage import STATIC_ROOTS, _under_static_root

    assert "omg_cli" in STATIC_ROOTS
    assert _under_static_root(Path("omg_cli/new_module_xyz.py"))
    assert not _under_static_root(Path("vendor/x.py"))
