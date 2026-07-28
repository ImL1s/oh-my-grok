"""#23 current-facing version consistency guard."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_version_consistency.py"


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(cwd or ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def test_check_passes_on_repo_checkout() -> None:
    proc = _run("--check")
    assert proc.returncode == 0, proc.stderr
    assert "ALL_VERSION_CONSISTENT" in proc.stdout


def test_write_is_idempotent() -> None:
    first = _run("--write")
    assert first.returncode == 0, first.stderr
    second = _run("--write")
    assert second.returncode == 0, second.stderr
    assert "no designated fields needed changes" in second.stdout


def test_stale_python_version_fails(tmp_path: Path) -> None:
    # Minimal fake repo with canonical 9.9.9 and drifted Python.
    _seed_min_repo(tmp_path, plugin_ver="9.9.9", py_ver="0.0.1")
    proc = _run("--check", "--root", str(tmp_path))
    assert proc.returncode == 1
    assert "omg_cli/__init__.py" in proc.stderr
    assert "9.9.9" in proc.stderr
    assert "0.0.1" in proc.stderr


def test_stale_capabilities_lock_fails(tmp_path: Path) -> None:
    _seed_min_repo(tmp_path, plugin_ver="1.2.3", lock_ver="0.0.0")
    proc = _run("--check", "--root", str(tmp_path))
    assert proc.returncode == 1
    assert "omg_capabilities.lock.json" in proc.stderr


def test_stale_readme_badge_fails(tmp_path: Path) -> None:
    _seed_min_repo(tmp_path, plugin_ver="2.0.0", readme_ver="1.0.0")
    proc = _run("--check", "--root", str(tmp_path))
    assert proc.returncode == 1
    assert "README.md" in proc.stderr


def test_historical_changelog_old_version_allowed(tmp_path: Path) -> None:
    root = _seed_min_repo(tmp_path, plugin_ver="3.0.0")
    cl = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.6.0]" in cl  # historical section remains
    proc = _run("--check", "--root", str(root))
    assert proc.returncode == 0, proc.stderr


def test_stale_install_sh_example_fails(tmp_path: Path) -> None:
    root = _seed_min_repo(tmp_path, plugin_ver="4.0.0", install_ver="0.6.0")
    proc = _run("--check", "--root", str(root))
    assert proc.returncode == 1
    assert "scripts/install.sh" in proc.stderr


def test_write_fixes_install_and_autopilot(tmp_path: Path) -> None:
    root = _seed_min_repo(
        tmp_path,
        plugin_ver="5.1.0",
        install_ver="0.6.0",
        autopilot_ver="0.6.0",
    )
    proc = _run("--write", "--root", str(root))
    assert proc.returncode == 0, proc.stderr
    install = (root / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "oh-my-grok-<VERSION>.tar.gz" in install
    ap = (root / "docs" / "autopilot.md").read_text(encoding="utf-8")
    assert "currently **5.1.0**" in ap
    # second write clean
    proc2 = _run("--write", "--root", str(root))
    assert proc2.returncode == 0
    assert "no designated fields needed changes" in proc2.stdout


def _seed_min_repo(
    tmp_path: Path,
    *,
    plugin_ver: str,
    py_ver: str | None = None,
    lock_ver: str | None = None,
    readme_ver: str | None = None,
    install_ver: str = "<VERSION>",
    autopilot_ver: str | None = None,
) -> Path:
    py_ver = py_ver or plugin_ver
    lock_ver = lock_ver or plugin_ver
    readme_ver = readme_ver or plugin_ver
    autopilot_ver = autopilot_ver or plugin_ver

    (tmp_path / "plugin.json").write_text(
        json.dumps({"name": "oh-my-grok", "version": plugin_ver}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "omg_cli").mkdir()
    (tmp_path / "omg_cli" / "__init__.py").write_text(
        f'__version__ = "{py_ver}"\n',
        encoding="utf-8",
    )
    (tmp_path / "omg_capabilities.lock.json").write_text(
        json.dumps({"version": lock_ver, "files": {}}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n\n## [{plugin_ver}] - 2099-01-01\n"
        f"\n## [0.6.0] - 2026-07-23\n- historical\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        f"Version: **{readme_ver}** · License: MIT\n"
        f"TAG=v{plugin_ver}\n"
        f"curl … oh-my-grok-{plugin_ver}.tar.gz\n",
        encoding="utf-8",
    )
    for rel, body in (
        (
            "docs/readme/README.zh.md",
            f"版本：**{plugin_ver}** · License: MIT\n"
            f"oh-my-grok-{plugin_ver}.tar.gz\n",
        ),
        (
            "docs/readme/README.zh-TW.md",
            f"版本：**{plugin_ver}** · License: MIT\n"
            f"oh-my-grok-{plugin_ver}.tar.gz\n",
        ),
        (
            "docs/autopilot.md",
            f"**Plugin version:** matches [`plugin.json`](../plugin.json) "
            f"(currently **{autopilot_ver}**).\n",
        ),
        (
            "docs/autopilot.zh.md",
            f"**版本：** 与 [`plugin.json`](../plugin.json) 一致（目前 **{plugin_ver}**）。\n",
        ),
        (
            "docs/autopilot.zh-TW.md",
            f"**版本：** 與 [`plugin.json`](../plugin.json) 一致（目前 **{plugin_ver}**）。\n",
        ),
        (
            "docs/security-model.md",
            f"Plugin version: **{plugin_ver}**\n",
        ),
        (
            "docs/security-model.zh.md",
            f"Plugin 版本：**{plugin_ver}**\n",
        ),
        (
            "docs/security-model.zh-TW.md",
            f"Plugin 版本：**{plugin_ver}**\n",
        ),
        (
            "docs/RELEASE.md",
            f"| Version | **{plugin_ver}** |\n"
            f"| Intended tag | `v{plugin_ver}` |\n"
            f"| Public assets | `oh-my-grok-{plugin_ver}.tar.gz` |\n",
        ),
        (
            "docs/RELEASE.zh.md",
            f"| Version | **{plugin_ver}** |\n"
            f"| Intended tag | `v{plugin_ver}` |\n"
            f"| Public assets | `oh-my-grok-{plugin_ver}.tar.gz` |\n",
        ),
        (
            "docs/RELEASE.zh-TW.md",
            f"| Version | **{plugin_ver}** |\n"
            f"| Intended tag | `v{plugin_ver}` |\n"
            f"| Public assets | `oh-my-grok-{plugin_ver}.tar.gz` |\n",
        ),
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    (tmp_path / "scripts").mkdir()
    if install_ver == "<VERSION>":
        archive = "oh-my-grok-<VERSION>.tar.gz"
    else:
        archive = f"oh-my-grok-{install_ver}.tar.gz"
    (tmp_path / "scripts" / "install.sh").write_text(
        f"# offline:\n"
        f"#   bash install.sh --offline --archive ./{archive} "
        f"--checksums ./SHA256SUMS\n",
        encoding="utf-8",
    )
    return tmp_path
