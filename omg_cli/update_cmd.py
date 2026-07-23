"""``omg update`` — source-safe or release-transactional refresh."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _emit_result(result) -> None:
    out = str(getattr(result, "stdout", "") or "")
    err = str(getattr(result, "stderr", "") or "")
    if out:
        print(out, end="" if out.endswith("\n") else "\n")
    if err:
        print(err, end="" if err.endswith("\n") else "\n", file=sys.stderr)


def _run(runner, argv: list[str], *, cwd: Path | None = None):
    try:
        return runner(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        print(f"omg update: command failed to start ({type(exc).__name__})", file=sys.stderr)
        return None


def _release_install_script(home: Path, grok_home: Path) -> Path | None:
    store = grok_home / "omg"
    pointer = store / "current-receipt"
    current = store / "current"
    if not os.path.lexists(pointer) and not os.path.lexists(current):
        return None
    try:
        from omg_cli.setup_cmd import verified_current_install

        verified = verified_current_install(
            store, home / ".local" / "bin" / "omg"
        )
        receipt = verified.receipt
        if receipt["mode"] != "release" or receipt["status"] not in {
            "installed",
            "completed_with_warning",
        }:
            return None
        script = verified.stage / "scripts" / "install.sh"
        return script if script.is_file() and not script.is_symlink() else None
    except Exception as exc:
        raise RuntimeError("managed release install failed confinement proof") from exc


def _development_source_checkout(home: Path, grok_home: Path) -> Path:
    """Return the exact original source recorded by a verified development install."""

    from omg_cli.setup_cmd import compute_package_identity, verified_current_install

    store = grok_home / "omg"
    verified = verified_current_install(store, home / ".local" / "bin" / "omg")
    receipt = verified.receipt
    if receipt.get("mode") != "development" or receipt.get("status") not in {
        "installed",
        "completed_with_warning",
    }:
        raise RuntimeError("current receipt is not a development install")
    source = receipt.get("source")
    installed = receipt.get("installed")
    if not isinstance(source, dict) or not isinstance(installed, dict):
        raise RuntimeError("development receipt source record is malformed")
    raw_value = source.get("package_realpath")
    if not isinstance(raw_value, str):
        raise RuntimeError("development receipt source path is malformed")
    raw = Path(raw_value)
    if not raw.is_absolute() or Path(os.path.normpath(str(raw))) != raw:
        raise RuntimeError("development receipt source path is not canonical")
    try:
        raw.lstat()
    except OSError as exc:
        raise RuntimeError("development receipt source checkout is absent") from exc
    if raw.is_symlink() or not raw.is_dir():
        raise RuntimeError("development receipt source checkout is not a directory")
    checkout = raw.resolve(strict=True)
    if checkout != raw:
        raise RuntimeError("development receipt source checkout traverses a symlink")
    identity = compute_package_identity(checkout)
    expected_digest = source.get("package_digest")
    expected_version = source.get("package_version")
    if (
        identity["digest"] != expected_digest
        or identity["version"] != expected_version
        or installed.get("package_digest") != expected_digest
        or installed.get("package_version") != expected_version
    ):
        raise RuntimeError("development receipt source checkout drifted from installed bytes")
    return checkout


def run_update(
    *,
    root: Path | None = None,
    runner=subprocess.run,
    home: Path | None = None,
    grok_home: Path | None = None,
) -> int:
    """Update exact installed bytes; never hide a failed refresh.

    A receipt-backed release install re-enters the no-checkout GitHub installer.
    A source checkout is updated only when Git reports it clean and fast-forward
    succeeds.  Dirty/diverged/unknown sources are preserved without mutation.
    """

    home_path = Path(home or os.environ.get("HOME") or Path.home()).resolve()
    if grok_home is None:
        raw = os.environ.get("GROK_HOME")
        grok_path = Path(raw).expanduser().resolve() if raw else home_path / ".grok"
    else:
        grok_path = Path(grok_home).resolve()

    if root is None:
        try:
            release_script = _release_install_script(home_path, grok_path)
        except RuntimeError:
            print(
                "omg update: managed install identity is corrupt; refusing mutation",
                file=sys.stderr,
            )
            return 1
        if release_script is not None:
            print("omg update: checksum-verified GitHub release transaction")
            result = _run(runner, ["bash", str(release_script)])
            if result is None:
                return 1
            _emit_result(result)
            rc = int(getattr(result, "returncode", 1))
            if rc != 0:
                print(f"omg update: release installer failed rc={rc}; prior install preserved", file=sys.stderr)
                return rc or 1
            print("omg update: installed receipt and strict readback passed")
            return 0
        try:
            checkout = _development_source_checkout(home_path, grok_path)
        except Exception:
            print(
                "omg update: no proven clean original development checkout; refusing mutation",
                file=sys.stderr,
            )
            return 1
    else:
        checkout = Path(root).resolve()
    if not checkout.is_dir():
        print("omg update: source root missing", file=sys.stderr)
        return 1
    print(f"omg update: source checkout {checkout}")

    if root is None:
        worktree = _run(
            runner,
            ["git", "-C", str(checkout), "rev-parse", "--show-toplevel"],
        )
        if (
            worktree is None
            or int(getattr(worktree, "returncode", 1)) != 0
            or Path(str(getattr(worktree, "stdout", "") or "").strip()).resolve()
            != checkout
        ):
            print(
                "omg update: receipt source is not the proven Git worktree root; preserved",
                file=sys.stderr,
            )
            return 1

    status = _run(
        runner,
        ["git", "-C", str(checkout), "status", "--porcelain=v1", "--untracked-files=all"],
    )
    if status is None or int(getattr(status, "returncode", 1)) != 0:
        print("omg update: cannot prove source checkout clean; preserved", file=sys.stderr)
        return 1
    if str(getattr(status, "stdout", "") or "").strip():
        print("omg update: dirty source checkout preserved; commit/stash or use release installer", file=sys.stderr)
        return 2

    for argv in (
        ["git", "-C", str(checkout), "fetch", "--tags", "--quiet"],
        ["git", "-C", str(checkout), "pull", "--ff-only"],
    ):
        result = _run(runner, argv)
        if result is None or int(getattr(result, "returncode", 1)) != 0:
            if result is not None:
                _emit_result(result)
            print("omg update: source refresh failed; checkout preserved", file=sys.stderr)
            return 1

    script = checkout / "scripts" / "install-plugin.sh"
    if not script.is_file() or not os.access(script, os.X_OK):
        print("omg update: immutable source installer missing", file=sys.stderr)
        return 1
    result = _run(runner, [str(script)], cwd=checkout)
    if result is None:
        return 1
    _emit_result(result)
    rc = int(getattr(result, "returncode", 1))
    if rc != 0:
        print(f"omg update: install-plugin.sh exited rc={rc}; no success claimed", file=sys.stderr)
        return rc or 1
    print("omg update: exact source install refreshed")
    return 0
