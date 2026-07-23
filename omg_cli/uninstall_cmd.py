"""omg uninstall — remove plugin, global hook, OMG rules block (never project .omg/)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import NamedTuple


class _ManagedFileSnapshot(NamedTuple):
    path: Path
    content: bytes | None
    mode: int | None


class _PluginSnapshot(NamedTuple):
    present: bool
    path: Path | None
    enabled: bool
    digest: str | None
    inventory: list[dict]


def _snapshot_managed_files(paths: tuple[Path, ...]) -> list[_ManagedFileSnapshot]:
    """Capture receipt-owned regular files for all-or-prior removal rollback."""
    snapshots: list[_ManagedFileSnapshot] = []
    for path in paths:
        if not os.path.lexists(path):
            snapshots.append(_ManagedFileSnapshot(path, None, None))
            continue
        before = path.lstat()
        if path.is_symlink() or not path.is_file():
            raise OSError(f"managed path is not a regular file: {path}")
        content = path.read_bytes()
        after = path.lstat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise OSError(f"managed path changed while snapshotting: {path}")
        snapshots.append(_ManagedFileSnapshot(path, content, before.st_mode & 0o777))
    return snapshots


def _restore_managed_files(snapshots: list[_ManagedFileSnapshot]) -> None:
    """Atomically restore exact bytes/modes without relying on unlink."""
    for snapshot in snapshots:
        if snapshot.content is None:
            if os.path.lexists(snapshot.path):
                if snapshot.path.is_dir() and not snapshot.path.is_symlink():
                    raise OSError(f"managed path became a directory: {snapshot.path}")
                snapshot.path.unlink()
            continue
        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = snapshot.path.with_name(
            f".{snapshot.path.name}.uninstall-rollback-{uuid.uuid4().hex}"
        )
        try:
            temporary.write_bytes(snapshot.content)
            if snapshot.mode is None:  # pragma: no cover - NamedTuple invariant
                raise OSError("managed file snapshot has no mode")
            temporary.chmod(snapshot.mode)
            os.replace(temporary, snapshot.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    for snapshot in snapshots:
        if snapshot.content is None:
            if os.path.lexists(snapshot.path):
                raise OSError(f"managed absent path was not restored: {snapshot.path}")
        elif (
            snapshot.path.is_symlink()
            or not snapshot.path.is_file()
            or snapshot.path.read_bytes() != snapshot.content
            or snapshot.path.stat().st_mode & 0o777 != snapshot.mode
        ):
            raise OSError(f"managed file rollback readback failed: {snapshot.path}")


def _plugin_rows(runner) -> list[dict]:
    result = runner(
        ["grok", "plugin", "list", "--json"],
        capture_output=True,
        text=True,
    )
    if int(getattr(result, "returncode", 1)) != 0:
        raise OSError("grok plugin inventory readback failed")
    payload = json.loads(str(getattr(result, "stdout", "") or ""))
    if isinstance(payload, dict):
        for key in ("plugins", "items", "data", "result"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        raise OSError("grok plugin inventory is malformed")
    rows = [
        row
        for row in payload
        if isinstance(row, dict)
        and (
            str(row.get("name") or row.get("id") or row.get("plugin") or "")
            == "oh-my-grok"
            or str(row.get("name") or row.get("id") or row.get("plugin") or "").startswith(
                "oh-my-grok@"
            )
        )
    ]
    if len(rows) > 1:
        raise OSError("grok plugin inventory is ambiguous")
    return rows


def _snapshot_plugin(runner, *, grok_home: Path) -> _PluginSnapshot:
    rows = _plugin_rows(runner)
    if not rows:
        return _PluginSnapshot(False, None, False, None, [])
    from omg_cli.setup_cmd import (
        _plugin_entry_is_enabled,
        _resolve_entry_identity,
    )

    path, identity = _resolve_entry_identity(rows[0], allow_source_fallback=False)
    return _PluginSnapshot(
        True,
        path,
        _plugin_entry_is_enabled(rows[0], grok_home=grok_home),
        str(identity["digest"]),
        list(identity["inventory"]),
    )


def _restore_plugin(
    runner,
    snapshot: _PluginSnapshot,
    *,
    grok_home: Path,
    source: Path | None = None,
) -> None:
    """Restore host plugin presence/content/enabled state and prove the readback.

    ``source`` is a receipt-proven byte-identical fallback install source (the
    immutable stage) for the host-copy model, where the snapshot path is the
    Grok-managed copy that the host uninstall already deleted.
    """

    current = _snapshot_plugin(runner, grok_home=grok_home)
    if current == snapshot:
        return
    if current.present:
        raise OSError("plugin changed concurrently before rollback")
    if snapshot.present:
        if snapshot.path is None:
            raise OSError("plugin rollback snapshot has no path")
        from omg_cli.setup_cmd import compute_package_identity

        install_source = snapshot.path
        if not snapshot.path.is_dir() and source is not None:
            install_source = source
        identity = compute_package_identity(install_source)
        if (
            identity["digest"] != snapshot.digest
            or identity["inventory"] != snapshot.inventory
        ):
            raise OSError("plugin rollback source bytes drifted")
        result = runner(
            ["grok", "plugin", "install", str(install_source), "--trust"],
            capture_output=True,
            text=True,
        )
        if int(getattr(result, "returncode", 1)) != 0:
            raise OSError("plugin rollback install failed")
        state_command = "enable" if snapshot.enabled else "disable"
        result = runner(
            ["grok", "plugin", state_command, "oh-my-grok"],
            capture_output=True,
            text=True,
        )
        if int(getattr(result, "returncode", 1)) != 0:
            raise OSError(f"plugin rollback {state_command} failed")

    actual = _snapshot_plugin(runner, grok_home=grok_home)
    if (
        actual.present != snapshot.present
        or actual.enabled != snapshot.enabled
        or actual.digest != snapshot.digest
        or actual.inventory != snapshot.inventory
        or (
            snapshot.path is not None
            and (actual.path is None or actual.path.resolve() != snapshot.path.resolve())
        )
    ):
        raise OSError("plugin rollback post-restore readback differs from snapshot")


def _restore_exact_symlink(path: Path, target: str) -> None:
    if os.path.lexists(path):
        if path.is_symlink() and os.readlink(path) == target:
            return
        raise OSError(f"managed pointer changed concurrently: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    if not path.is_symlink() or os.readlink(path) != target:
        raise OSError(f"managed pointer rollback readback failed: {path}")


def _checkout_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _grok_home(home: Path | None) -> Path:
    # Single source of truth for the grok config root (honors $GROK_HOME).
    from omg_cli.hook_install import grok_home

    return grok_home(home)


def run_uninstall(
    *,
    yes: bool = False,
    runner=subprocess.run,
    home: Path | None = None,
) -> int:
    """Remove OMG install surfaces. Requires --yes to mutate.

    Never removes project ``.omg/`` state. Never deletes USER:OMG:POLICY blocks
    (guidance.uninstall_global_rules preserves non-OMG content).
    """
    gh = _grok_home(home).expanduser().resolve()
    hook = gh / "hooks" / "omg-pretool-deny.json"
    rules = gh / "rules" / "omg.md"
    home_root = Path(os.environ.get("HOME") or Path.home()).expanduser().resolve()
    link = home_root / ".local" / "bin" / "omg"
    checkout = _checkout_root()
    store = gh / "omg"
    current = store / "current"
    receipt_pointer = store / "current-receipt"

    receipt_path: Path | None = None
    receipt: dict | None = None
    verified_stage: Path | None = None
    if os.path.lexists(receipt_pointer):
        if not receipt_pointer.is_symlink():
            print(
                "omg uninstall: corrupt immutable receipt; refusing mutation (InstallError)",
                file=sys.stderr,
            )
            return 1
        try:
            from omg_cli.setup_cmd import verified_current_install

            verified = verified_current_install(store, link)
            receipt_path = verified.receipt_path
            receipt = verified.receipt
            verified_stage = verified.stage
        except Exception as exc:  # noqa: BLE001
            print(
                f"omg uninstall: corrupt immutable receipt; refusing mutation ({type(exc).__name__})",
                file=sys.stderr,
            )
            return 1
    elif os.path.lexists(current):
        print(
            "omg uninstall: managed current pointer has no receipt; refusing mutation",
            file=sys.stderr,
        )
        return 1

    if not yes:
        print("omg uninstall: dry run (no changes). Would remove:")
        print("  - grok plugin uninstall oh-my-grok --confirm")
        print(f"  - global hook json (if present): {hook}")
        print(f"  - global hook standalone (if present): {hook.with_name('omg_pretool_deny_standalone.py')}")
        print(f"  - OMG managed block in rules (if present): {rules}")
        if receipt is not None:
            print(f"  - receipt-owned immutable stage: {receipt['installed']['stage_realpath']}")
            print(f"  - managed current/receipt pointers under: {store}")
        print(
            f"  - ~/.local/bin/omg only if it is a symlink into this checkout "
            f"({checkout})"
        )
        print("  - project .omg/ state: NOT removed (intentionally left untouched)")
        print("re-run with --yes to actually perform removal")
        return 0

    # Receipt-backed installs are fail-closed: prove installed bytes before the
    # host mutation.  Legacy development installs retain the older best-effort
    # path, but never gain authority over foreign CLI/config/state.
    if receipt is not None:
        try:
            from omg_cli.setup_cmd import compute_package_identity
            import hashlib

            expected = str(receipt["installed"]["package_digest"])
            plugin_path = verified_stage
            if plugin_path is None:  # pragma: no cover - guarded by receipt
                raise ValueError("verified receipt has no stage")
            if compute_package_identity(plugin_path)["digest"] != expected:
                print("omg uninstall: host plugin bytes drifted; preserved", file=sys.stderr)
                return 1
            owned_hooks = {
                str(row.get("path")): str(row.get("identity"))
                for row in receipt.get("owned_inventory", [])
                if isinstance(row, dict) and row.get("kind") == "global_hook"
            }
            for managed in (hook, hook.with_name("omg_pretool_deny_standalone.py")):
                expected_file = owned_hooks.get(str(managed))
                if managed.is_file() and (
                    managed.is_symlink()
                    or expected_file is None
                    or hashlib.sha256(managed.read_bytes()).hexdigest() != expected_file
                ):
                    print(f"omg uninstall: drifted global hook preserved: {managed}", file=sys.stderr)
                    return 1
            from omg_cli.guidance import render_managed_block, rules_status

            guidance_rows = {
                str(row.get("path")): str(row.get("identity"))
                for row in receipt.get("owned_inventory", [])
                if isinstance(row, dict) and row.get("kind") == "global_guidance"
            }
            status = rules_status(
                version=str(receipt["installed"]["package_version"]),
                home=gh,
            )
            if status.get("present"):
                expected_guidance = guidance_rows.get(str(rules))
                actual_guidance = hashlib.sha256(
                    render_managed_block(
                        str(receipt["installed"]["package_version"])
                    ).encode("utf-8")
                ).hexdigest()
                if (
                    status.get("corrupt")
                    or not status.get("version_ok")
                    or not status.get("source_hash_ok")
                    or status.get("drift")
                    or expected_guidance != actual_guidance
                ):
                    print("omg uninstall: drifted managed guidance preserved", file=sys.stderr)
                    return 1
        except Exception as exc:  # noqa: BLE001
            print(
                f"omg uninstall: exact identity preflight failed ({type(exc).__name__}); preserved",
                file=sys.stderr,
            )
            return 1

    managed_paths = (hook, hook.with_name("omg_pretool_deny_standalone.py"), rules)
    managed_snapshots: list[_ManagedFileSnapshot] = []
    plugin_snapshot: _PluginSnapshot | None = None
    pointer_targets: dict[Path, str] = {}
    plugin_mutated = False
    removed_pointers: set[Path] = set()
    if receipt is not None:
        try:
            managed_snapshots = _snapshot_managed_files(managed_paths)
            plugin_snapshot = _snapshot_plugin(runner, grok_home=gh)
            # verified_current_install already proved the receipt's
            # plugin_realpath is canonical, confined to the stage or Grok's
            # managed copy root, and byte-identical to the stage.
            receipt_plugin = Path(str(receipt["installed"]["plugin_realpath"]))
            if (
                not plugin_snapshot.present
                or plugin_snapshot.path is None
                or verified_stage is None
                or plugin_snapshot.path.resolve()
                not in {verified_stage.resolve(), receipt_plugin.resolve()}
                or plugin_snapshot.digest != receipt["installed"]["package_digest"]
            ):
                raise OSError("host plugin snapshot differs from immutable receipt")
            pointer_targets = {
                link: os.readlink(link),
                current: os.readlink(current),
                receipt_pointer: os.readlink(receipt_pointer),
            }
        except Exception as exc:  # noqa: BLE001
            print(
                f"omg uninstall: transactional snapshot failed ({type(exc).__name__}); preserved",
                file=sys.stderr,
            )
            return 1

    def rollback(reason: str) -> int:
        failures: list[str] = []
        if receipt is not None:
            for path in (receipt_pointer, current, link):
                if path in removed_pointers:
                    try:
                        _restore_exact_symlink(
                            path,
                            pointer_targets[path],
                        )
                    except Exception as exc:  # noqa: BLE001
                        failures.append(f"{path.name}:{type(exc).__name__}")
            try:
                _restore_managed_files(managed_snapshots)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"managed:{type(exc).__name__}")
            if plugin_mutated and plugin_snapshot is not None:
                try:
                    _restore_plugin(
                        runner,
                        plugin_snapshot,
                        grok_home=gh,
                        source=verified_stage,
                    )
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"plugin:{type(exc).__name__}")
        suffix = (
            f"; rollback readback FAILED ({', '.join(failures)})"
            if failures
            else "; exact prior state restored and read back"
        )
        print(f"omg uninstall: {reason}{suffix}", file=sys.stderr)
        return 1

    # 1. grok plugin uninstall.  Receipt-backed failure is hard; legacy remains
    # visible best-effort for compatibility with old local installs.
    try:
        result = runner(
            ["grok", "plugin", "uninstall", "oh-my-grok", "--confirm"],
            capture_output=True,
            text=True,
        )
        print(
            "omg uninstall: grok plugin uninstall oh-my-grok "
            f"(rc={getattr(result, 'returncode', '?')})"
        )
        if receipt is not None and getattr(result, "returncode", 1) == 0:
            plugin_mutated = True
        if receipt is not None and getattr(result, "returncode", 1) != 0:
            print("omg uninstall: host refused removal; managed files preserved", file=sys.stderr)
            return 1
    except OSError as exc:
        if receipt is not None:
            return rollback(f"host plugin uninstall failed ({type(exc).__name__})")
        print(f"omg uninstall: grok plugin uninstall skipped: {exc}")

    # 2. remove global hook (json FIRST, then standalone .py — never leave an
    #    active json pointing at a missing script). Shared with the installer.
    try:
        from omg_cli.hook_install import remove_global_hook

        if receipt is not None:
            owned = {
                str(row.get("path")): str(row.get("identity"))
                for row in receipt.get("owned_inventory", [])
                if isinstance(row, dict) and row.get("kind") == "global_hook"
            }
            import hashlib

            for managed in (hook, hook.with_name("omg_pretool_deny_standalone.py")):
                expected_hook_identity = owned.get(str(managed))
                if managed.is_file() and (
                    managed.is_symlink()
                    or expected_hook_identity is None
                    or hashlib.sha256(managed.read_bytes()).hexdigest()
                    != expected_hook_identity
                ):
                    return rollback(f"global hook changed concurrently: {managed}")
        removed = remove_global_hook(home=gh)
        if receipt is not None and any(os.path.lexists(path) for path in managed_paths[:2]):
            raise OSError("receipt-owned global hook removal was incomplete")
        if removed:
            for r in removed:
                print(f"omg uninstall: removed {r}")
        else:
            print(f"omg uninstall: global hook absent ({hook})")
    except Exception as exc:  # noqa: BLE001 — legacy remains best-effort
        print(f"omg uninstall: could not remove global hook: {exc}", file=sys.stderr)
        if receipt is not None:
            return rollback("global hook removal failed")

    # 3. strip OMG managed rules block (preserve USER policy / foreign content)
    try:
        from omg_cli.guidance import GuidanceCorruptionError, uninstall_global_rules

        path, action = uninstall_global_rules(home=gh)
        if receipt is not None:
            from omg_cli.guidance import rules_status

            if rules_status(
                version=str(receipt["installed"]["package_version"]),
                home=gh,
            ).get("present"):
                raise OSError("receipt-owned managed guidance removal was incomplete")
        print(f"omg uninstall: rules {path} -> {action}")
    except GuidanceCorruptionError as exc:
        print(
            f"omg uninstall: rules file corrupt, left untouched: {exc}",
            file=sys.stderr,
        )
        if receipt is not None:
            return rollback("managed guidance removal failed")
    except Exception as exc:  # noqa: BLE001 — receipt path must fail closed
        print(f"omg uninstall: could not remove managed guidance: {exc}", file=sys.stderr)
        if receipt is not None:
            return rollback("managed guidance removal failed")

    # 4. remove the CLI pointer only when exact receipt/legacy ownership proves it.
    if receipt is not None:
        try:
            expected = pointer_targets[link]
            if not link.is_symlink() or os.readlink(link) != expected:
                raise OSError("CLI pointer changed concurrently")
            target = link.resolve(strict=True)
            if verified_stage is None or target != (verified_stage / "bin" / "omg").resolve():
                raise OSError("CLI pointer target differs from immutable receipt")
            link.unlink()
            removed_pointers.add(link)
            if os.path.lexists(link):
                raise OSError("CLI pointer unlink did not remove the directory entry")
            print(f"omg uninstall: removed symlink {link} -> {target}")
        except OSError as exc:
            return rollback(f"CLI pointer removal failed ({type(exc).__name__})")
    elif link.is_symlink():
        try:
            target = link.resolve()
            checkout_resolved = checkout.resolve()
            try:
                target.relative_to(checkout_resolved)
                in_checkout = True
            except ValueError:
                in_checkout = False
            if in_checkout:
                link.unlink()
                print(f"omg uninstall: removed symlink {link} -> {target}")
            else:
                print(f"omg uninstall: left {link} (symlink target not in this checkout)")
        except OSError as exc:
            print(f"omg uninstall: could not inspect/remove {link}: {exc}", file=sys.stderr)
    elif link.exists():
        print(f"omg uninstall: left {link} (not a symlink)")
    else:
        print(f"omg uninstall: CLI link absent ({link})")

    # 5. Receipt-owned pointers and immutable stage.  Historical receipts stay
    # for audit; an immutable `uninstalled` receipt records the terminal action.
    if receipt is not None and receipt_path is not None:
        if verified_stage is None:  # pragma: no cover - guarded by receipt
            return rollback("verified stage missing")
        stage = verified_stage
        try:
            if not current.is_symlink() or os.readlink(current) != pointer_targets[current]:
                raise OSError("current pointer changed concurrently")
            current.unlink()
            removed_pointers.add(current)
            if os.path.lexists(current):
                raise OSError("current pointer unlink did not remove the directory entry")
            print(f"omg uninstall: removed {current}")
            if (
                not receipt_pointer.is_symlink()
                or os.readlink(receipt_pointer) != pointer_targets[receipt_pointer]
            ):
                raise OSError("receipt pointer changed concurrently")
            receipt_pointer.unlink()
            removed_pointers.add(receipt_pointer)
            if os.path.lexists(receipt_pointer):
                raise OSError("receipt pointer unlink did not remove the directory entry")
            print(f"omg uninstall: removed {receipt_pointer}")
        except OSError as exc:
            return rollback(f"managed pointer removal failed ({type(exc).__name__})")
        try:
            from omg_cli.setup_cmd import _receipt_material, _write_install_receipt

            source = {
                "root_realpath": receipt["source"]["package_realpath"],
                "version": receipt["source"]["package_version"],
                "digest": receipt["source"]["package_digest"],
                "inventory": receipt["installed"].get("inventory") or [],
            }
            material = _receipt_material(
                transaction_id=uuid.uuid4().hex,
                status="uninstalled",
                mode=receipt["mode"],
                source=source,
                stage=stage,
                plugin_path=None,
                asset={
                    "asset_name": receipt["source"].get("asset_name") or "",
                    "asset_sha256": receipt["source"].get("asset_sha256") or "",
                    "checksums_sha256": receipt["source"].get("checksums_sha256") or "",
                }
                if receipt["source"].get("asset_name")
                else None,
                source_uri=receipt["source"].get("uri"),
                source_tag=receipt["source"].get("tag"),
                commands=[],
                owned_inventory=[
                    {
                        "path": str(receipt_path),
                        "kind": "prior_receipt",
                        "identity": receipt["receipt_hash"],
                    }
                ],
            )
            terminal, _data = _write_install_receipt(store / "receipts", material)
            print(f"omg uninstall: wrote immutable terminal receipt {terminal}")
        except Exception as exc:  # noqa: BLE001
            return rollback(f"terminal receipt failed ({type(exc).__name__})")
        if stage.is_dir():
            for path in sorted(stage.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                try:
                    path.chmod(0o700 if path.is_dir() else 0o600)
                except OSError:
                    pass
            stage.chmod(0o700)
            import shutil

            try:
                shutil.rmtree(stage)
            except OSError as exc:
                # The uninstall transaction is already durably complete.  Keep
                # the now-unreferenced stage for a later safe cleanup rather
                # than claiming a rollback that cannot reconstruct partial bytes.
                print(
                    f"omg uninstall: immutable stage cleanup deferred ({type(exc).__name__})",
                    file=sys.stderr,
                )
            else:
                print(f"omg uninstall: removed immutable stage {stage}")

    # 6. never touch project .omg/
    print(
        "omg uninstall: project `.omg/` state was intentionally left untouched"
    )
    return 0
