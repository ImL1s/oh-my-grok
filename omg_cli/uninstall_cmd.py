"""omg uninstall — remove plugin, global hook, OMG rules block (never project .omg/)."""

from __future__ import annotations

import json
import base64
import hashlib
import os
import subprocess
import sys
import tempfile
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


def _bytes_identity(content: bytes | None) -> str | None:
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _fsync_parent(path: Path) -> None:
    """Best-effort parent directory fsync; required on POSIX, ignored on Windows."""
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(directory_fd)
    except OSError:
        if os.name != "nt":
            raise
    finally:
        os.close(directory_fd)


def _expected_rules_post(content: bytes | None) -> bytes | None:
    if content is None:
        return None
    from omg_cli.guidance import _extract_managed_block

    text = content.decode("utf-8")
    span = _extract_managed_block(text)
    if span is None:
        return content
    remainder = (text[: span[0]] + text[span[1] :]).strip()
    return (remainder + "\n").encode("utf-8") if remainder else None


def _write_durable_grok_uninstall(
    path: Path,
    *,
    managed: list[_ManagedFileSnapshot],
    rules: Path,
    plugin: _PluginSnapshot,
    stage: Path,
    pointers: dict[Path, str],
    receipt_path: Path,
    receipt_hash: str,
) -> None:
    rows = []
    for row in managed:
        post = _expected_rules_post(row.content) if row.path == rules else None
        rows.append(
            {
                "path": str(row.path),
                "content": base64.b64encode(row.content).decode() if row.content is not None else None,
                "mode": row.mode,
                "prior_sha256": _bytes_identity(row.content),
                "post_sha256": _bytes_identity(post),
            }
        )
    payload = {
        "schema": "omg-grok-uninstall/v1",
        "managed": rows,
        "plugin": {
            **plugin._asdict(),
            "path": str(plugin.path) if plugin.path is not None else None,
        },
        "stage": str(stage),
        "receipt_path": str(receipt_path),
        "receipt_hash": receipt_hash,
        "pointers": {str(key): value for key, value in pointers.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (os.path.lexists(path) and not path.is_file()):
        raise OSError("durable Grok journal path is unsafe")
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _recover_durable_grok_uninstall(
    path: Path, *, runner, grok_home: Path, expected_paths: set[Path]
) -> bool:
    if not path.is_file() or path.is_symlink():
        return not os.path.lexists(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema") != "omg-grok-uninstall/v1":
            return False
        receipt_path = Path(raw["receipt_path"])
        receipts_root = (grok_home / "omg" / "receipts").absolute()
        if (
            receipt_path.is_symlink()
            or receipt_path.parent.absolute() != receipts_root
            or not receipt_path.is_file()
        ):
            return False
        from omg_cli.setup_cmd import read_install_receipt

        receipt = read_install_receipt(receipt_path)
        if receipt.get("receipt_hash") != raw.get("receipt_hash"):
            return False
        stage = Path(raw["stage"])
        if (
            str(stage.absolute()) != str(receipt["installed"]["stage_realpath"])
            or not stage.is_dir()
            or stage.is_symlink()
        ):
            return False
        managed: list[_ManagedFileSnapshot] = []
        for row in raw["managed"]:
            target = Path(row["path"])
            if target not in expected_paths or target.is_symlink():
                return False
            content = base64.b64decode(row["content"]) if row["content"] is not None else None
            if _bytes_identity(content) != row["prior_sha256"]:
                return False
            current_file = target.is_file() and not target.is_symlink()
            current_absent = not os.path.lexists(target)
            if not current_file and not current_absent:
                return False
            current_id = _bytes_identity(target.read_bytes() if current_file else None)
            current_mode = (target.stat().st_mode & 0o777) if current_file else None
            current_kind = "file" if current_file else "absent"
            prior_kind = "file" if row["prior_sha256"] is not None else "absent"
            post_kind = "file" if row["post_sha256"] is not None else "absent"
            allowed = {
                (prior_kind, row["prior_sha256"], row["mode"] if prior_kind == "file" else None),
                (post_kind, row["post_sha256"], row["mode"] if post_kind == "file" else None),
            }
            if (current_kind, current_id, current_mode) not in allowed:
                return False
            managed.append(_ManagedFileSnapshot(target, content, row["mode"]))
        pointers = {Path(key): value for key, value in raw["pointers"].items()}
        for pointer, expected in pointers.items():
            if pointer not in expected_paths:
                return False
            if os.path.lexists(pointer) and (
                not pointer.is_symlink() or os.readlink(pointer) != expected
            ):
                return False
        plugin_raw = raw["plugin"]
        plugin = _PluginSnapshot(
            bool(plugin_raw["present"]),
            Path(plugin_raw["path"]) if plugin_raw["path"] else None,
            bool(plugin_raw["enabled"]),
            plugin_raw["digest"],
            plugin_raw["inventory"],
        )
        allowed_plugin_paths = {
            str(stage.absolute()),
            str(Path(str(receipt["installed"]["plugin_realpath"])).absolute()),
        }
        if (
            not plugin.present
            or plugin.path is None
            or str(plugin.path.absolute()) not in allowed_plugin_paths
            or plugin.digest != receipt["installed"]["package_digest"]
            or plugin.inventory != receipt["installed"].get("inventory", [])
        ):
            return False
        receipt_pointer = grok_home / "omg" / "current-receipt"
        expected_receipt_link = pointers.get(receipt_pointer)
        if expected_receipt_link is None:
            return False
        linked_receipt = (receipt_pointer.parent / expected_receipt_link).resolve()
        if linked_receipt != receipt_path.resolve():
            return False
        current_plugin = _snapshot_plugin(runner, grok_home=grok_home)
        if current_plugin.present and current_plugin != plugin:
            return False
        if not current_plugin.present and plugin.present:
            _restore_plugin(runner, plugin, grok_home=grok_home, source=stage)
        _restore_managed_files(managed)
        for pointer, expected in pointers.items():
            if not os.path.lexists(pointer):
                _restore_exact_symlink(pointer, expected)
        if _snapshot_plugin(runner, grok_home=grok_home) != plugin:
            return False
        for row in managed:
            current = row.path.read_bytes() if row.path.is_file() else None
            if current != row.content:
                return False
        path.unlink()
        if path.parent.is_dir():
            _fsync_parent(path.parent)
        return True
    except Exception:  # noqa: BLE001 - malformed recovery must fail closed
        return False


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
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_mode,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_mode,
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
                if snapshot.path.parent.is_dir():
                    _fsync_parent(snapshot.path.parent)
            continue
        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = snapshot.path.with_name(
            f".{snapshot.path.name}.uninstall-rollback-{uuid.uuid4().hex}"
        )
        try:
            if snapshot.mode is None:  # pragma: no cover - NamedTuple invariant
                raise OSError("managed file snapshot has no mode")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(temporary, flags, snapshot.mode)
            with os.fdopen(fd, "wb") as stream:
                stream.write(snapshot.content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(snapshot.mode)
            os.replace(temporary, snapshot.path)
            _fsync_parent(snapshot.path.parent)
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
            str(row.get("name") or row.get("id") or row.get("plugin") or "") == "oh-my-grok"
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
        if identity["digest"] != snapshot.digest or identity["inventory"] != snapshot.inventory:
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


def _owned_plan_has_hook_artifact(owned_plan: dict) -> bool:
    """True when the surgical plan lists ``user.grok.hook`` (remove or preserve)."""
    for bucket in ("remove", "preserve"):
        for row in owned_plan.get(bucket) or []:
            if isinstance(row, dict) and row.get("id") == "user.grok.hook":
                return True
    return False


def _run_uninstall_locked(
    *,
    yes: bool = False,
    runner=subprocess.run,
    home: Path | None = None,
    project_root: Path | None = None,
    include_user_manifest: bool = False,
) -> int:
    """Remove OMG install surfaces. Requires --yes to mutate.

    Never removes project ``.omg/state``. Never deletes USER:OMG:POLICY blocks
    (guidance.uninstall_global_rules preserves non-OMG content). When an install
    manifest exists, only receipt-owned or manifest-owned *unchanged* regular
    files are unlinked; hash-drifted managed files are preserved.
    """
    gh = _grok_home(home).expanduser().resolve()
    from omg_cli.hook_install import managed_hook_paths

    hook, hook_wrapper, hook_py = managed_hook_paths(home=gh)
    rules = gh / "rules" / "omg.md"
    home_root = Path(os.environ.get("HOME") or Path.home()).expanduser().resolve()
    link = home_root / ".local" / "bin" / "omg"
    checkout = _checkout_root()
    store = gh / "omg"
    current = store / "current"
    receipt_pointer = store / "current-receipt"
    durable_grok_tx = store / "uninstall-current.json"
    if os.path.lexists(durable_grok_tx) and not _recover_durable_grok_uninstall(
        durable_grok_tx,
        runner=runner,
        grok_home=gh,
        expected_paths={hook, hook_wrapper, hook_py, rules, link, current, receipt_pointer},
    ):
        print("omg uninstall: durable Grok recovery failed; evidence preserved", file=sys.stderr)
        return 1

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

    from omg_cli.install_migrate import apply_owned_uninstall, plan_owned_uninstall

    owned_plan = plan_owned_uninstall(
        project_root=project_root,
        include_user_manifest=include_user_manifest,
        grok_home=gh,
    )

    if not yes:
        print("omg uninstall: dry run (no changes). Would remove:")
        print("  - grok plugin uninstall oh-my-grok --confirm")
        print(f"  - global hook json (if present): {hook}")
        print(f"  - global hook wrapper (if present): {hook_wrapper}")
        print(f"  - global hook standalone (if present): {hook_py}")
        print(f"  - OMG managed block in rules (if present): {rules}")
        if receipt is not None:
            print(f"  - receipt-owned immutable stage: {receipt['installed']['stage_realpath']}")
            print(f"  - managed current/receipt pointers under: {store}")
        print(f"  - ~/.local/bin/omg only if it is a symlink into this checkout ({checkout})")
        print("  - project .omg/state: NOT removed (intentionally left untouched)")
        if owned_plan.get("has_manifest"):
            for row in owned_plan.get("remove_external") or []:
                print(f"  - manifest-owned Agy plugin via uninstall: {row.get('path')}")
            for row in owned_plan.get("remove") or []:
                print(f"  - manifest-owned unchanged: {row.get('path')}")
            for row in owned_plan.get("preserve") or []:
                print(f"  - preserve ({row.get('reason')}): {row.get('path')}")
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
            if (
                compute_package_identity(plugin_path, canonicalize_posix_launchers=False)["digest"]
                != expected
            ):
                print("omg uninstall: host plugin bytes drifted; preserved", file=sys.stderr)
                return 1
            owned_hooks = {
                str(row.get("path")): str(row.get("identity"))
                for row in receipt.get("owned_inventory", [])
                if isinstance(row, dict) and row.get("kind") == "global_hook"
            }
            for managed in (hook, hook_wrapper, hook_py):
                expected_file = owned_hooks.get(str(managed))
                if managed.is_file() and (
                    managed.is_symlink()
                    or expected_file is None
                    or hashlib.sha256(managed.read_bytes()).hexdigest() != expected_file
                ):
                    print(
                        f"omg uninstall: drifted global hook preserved: {managed}", file=sys.stderr
                    )
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
                    render_managed_block(str(receipt["installed"]["package_version"])).encode(
                        "utf-8"
                    )
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

    hook_paths = (hook, hook_wrapper, hook_py)
    managed_paths = (*hook_paths, rules)
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

    agy_home_for_finalize: Path | None = None

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
        if agy_home_for_finalize is not None:
            try:
                from omg_cli.antigravity_install import (
                    config_root,
                    restore_recovery_snapshot,
                )

                recovery = (
                    config_root(agy_home_for_finalize) / ".omg-transactions" / "agy-uninstall"
                )
                if not restore_recovery_snapshot(
                    recovery, runner=runner, undo_committed=True
                ):
                    failures.append("agy-restore")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"agy:{type(exc).__name__}")
        suffix = (
            f"; rollback readback FAILED ({', '.join(failures)})"
            if failures
            else "; exact prior state restored and read back"
        )
        print(f"omg uninstall: {reason}{suffix}", file=sys.stderr)
        return 1

    # 1. Remove manifest-owned Agy before any Grok mutation. Its committed
    # durable marker remains until the whole cross-runtime uninstall completes.
    # Manifest-owned exact Agy import, then unchanged regular files.  The
    # plugin is removed only through the host CLI and only when its package
    # digest still matches the manifest.
    if owned_plan.get("has_manifest"):
        external = owned_plan.get("remove_external") or []
        if external:
            if len(external) != 1:
                print(
                    "omg uninstall: ambiguous Antigravity ownership; plugin preserved",
                    file=sys.stderr,
                )
                return rollback("ambiguous Antigravity ownership")
            external_row = external[0]
            target = Path(str(external_row.get("path") or ""))
            try:
                agy_home = target.parents[3]
                expected_digest = str(external_row["content_hash"])
                expected_registry = str(external_row["registry_identity"])
                expected_mcp_registry = str(external_row["mcp_registry_identity"])
                from omg_cli.antigravity_install import (
                    uninstall_owned_plugin,
                )

                agy_home_for_finalize = agy_home
                removed = uninstall_owned_plugin(
                    expected_digest=expected_digest,
                    expected_registry_identity=expected_registry,
                    expected_mcp_registry_identity=expected_mcp_registry,
                    runner=runner,
                    home=agy_home,
                    retain_committed=True,
                )
            except (OSError, KeyError, IndexError, ValueError):
                removed = False
            if not removed:
                print(
                    "omg uninstall: Antigravity plugin changed before locked uninstall; preserved",
                    file=sys.stderr,
                )
                return rollback("Antigravity locked uninstall failed")
            print("omg uninstall: removed manifest-owned Antigravity plugin")

    if (
        receipt is not None
        and receipt_path is not None
        and plugin_snapshot is not None
        and verified_stage is not None
    ):
        _write_durable_grok_uninstall(
            durable_grok_tx,
            managed=managed_snapshots,
            rules=rules,
            plugin=plugin_snapshot,
            stage=verified_stage,
            pointers=pointer_targets,
            receipt_path=receipt_path,
            receipt_hash=str(receipt["receipt_hash"]),
        )

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
            return rollback("host refused grok plugin removal")
    except OSError as exc:
        if receipt is not None:
            return rollback(f"host plugin uninstall failed ({type(exc).__name__})")
        print(f"omg uninstall: grok plugin uninstall skipped: {exc}")

    # 2. remove global hook (json FIRST, then standalone .py — never leave an
    #    active json pointing at a missing script). Shared with the installer.
    #    Skip only when the owned plan actually lists user.grok.hook (remove or
    #    preserve). A skill-only / unrelated manifest must not leave the hook
    #    behind. Manifest-owned drifted hooks stay preserved; matching owned
    #    paths are unlinked surgically later.
    skip_legacy_hooks = receipt is None and _owned_plan_has_hook_artifact(owned_plan)
    try:
        from omg_cli.hook_install import remove_global_hook

        if skip_legacy_hooks:
            print("omg uninstall: global hook removal deferred to manifest-owned plan")
        else:
            if receipt is not None:
                owned = {
                    str(row.get("path")): str(row.get("identity"))
                    for row in receipt.get("owned_inventory", [])
                    if isinstance(row, dict) and row.get("kind") == "global_hook"
                }
                import hashlib

                for managed in hook_paths:
                    expected_hook_identity = owned.get(str(managed))
                    if managed.is_file() and (
                        managed.is_symlink()
                        or expected_hook_identity is None
                        or hashlib.sha256(managed.read_bytes()).hexdigest()
                        != expected_hook_identity
                    ):
                        return rollback(f"global hook changed concurrently: {managed}")
            removed = remove_global_hook(home=gh)
            if receipt is not None and any(os.path.lexists(path) for path in hook_paths):
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

        # Always use block-aware removal so merged user text in omg.md is kept.
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

    # 6. Receipt-owned pointers and immutable stage.  Historical receipts stay
    # for audit; an immutable `uninstalled` receipt records the terminal action.
    stage_to_cleanup: Path | None = None
    terminal_receipt_material: dict | None = None
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
            from omg_cli.setup_cmd import _receipt_material

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
            terminal_receipt_material = material
        except Exception as exc:  # noqa: BLE001
            return rollback(f"terminal receipt failed ({type(exc).__name__})")
        stage_to_cleanup = stage

    # Commit the destructive transaction before emitting the append-only audit
    # receipt. A crash can therefore omit the audit row, but can never leave an
    # `uninstalled` receipt alongside a journal that will restore the install.
    if durable_grok_tx.is_file():
        durable_grok_tx.unlink()
        if durable_grok_tx.parent.is_dir():
            _fsync_parent(durable_grok_tx.parent)

    if agy_home_for_finalize is not None:
        from omg_cli.antigravity_install import finalize_owned_uninstall

        if not finalize_owned_uninstall(home=agy_home_for_finalize):
            print(
                "omg uninstall: runtimes removed, but Antigravity transaction "
                "cleanup remains recoverable",
                file=sys.stderr,
            )
            return 1

    # Idempotent post-commit cleanup. It must not be inside the Grok rollback
    # window: these manifest files and shared global references are governed by
    # their own exact-CAS logic and are not represented by the Grok journal.
    if owned_plan.get("has_manifest"):
        applied = apply_owned_uninstall(owned_plan)
        if applied.get("ok") is not True:
            print(
                "omg uninstall: runtimes removed, but manifest/reference cleanup "
                "remains recoverable",
                file=sys.stderr,
            )
            return 1
        for path in applied.get("removed") or []:
            print(f"omg uninstall: removed manifest-owned {path}")
        for path in applied.get("preserved") or []:
            print(f"omg uninstall: preserved {path}")

    if terminal_receipt_material is not None:
        try:
            from omg_cli.setup_cmd import _write_install_receipt

            terminal, _data = _write_install_receipt(
                store / "receipts", terminal_receipt_material
            )
            print(f"omg uninstall: wrote immutable terminal receipt {terminal}")
        except Exception as exc:  # noqa: BLE001
            print(
                f"omg uninstall: terminal audit receipt failed ({type(exc).__name__})",
                file=sys.stderr,
            )
            return 1
    if stage_to_cleanup is not None and stage_to_cleanup.is_dir():
        for path in sorted(stage_to_cleanup.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                path.chmod(0o700 if path.is_dir() else 0o600)
            except OSError:
                pass
        stage_to_cleanup.chmod(0o700)
        import shutil

        try:
            shutil.rmtree(stage_to_cleanup)
        except OSError as exc:
            print(
                f"omg uninstall: immutable stage cleanup deferred ({type(exc).__name__})",
                file=sys.stderr,
            )
        else:
            print(f"omg uninstall: removed immutable stage {stage_to_cleanup}")

    # 7. never touch project .omg/state
    print("omg uninstall: project `.omg/state` was intentionally left untouched")
    return 0


def run_uninstall(
    *,
    yes: bool = False,
    runner=subprocess.run,
    home: Path | None = None,
    project_root: Path | None = None,
    include_user_manifest: bool = False,
) -> int:
    """Serialize the full cross-runtime uninstall with manifest installs."""
    from omg_cli.contracts.path_keys import ensure_managed_dir, exclusive_lock
    from omg_cli.install_manifest import user_store

    lock_root = user_store()
    ensure_managed_dir(lock_root)
    with exclusive_lock(lock_root / ".install-manifest.lock"):
        return _run_uninstall_locked(
            yes=yes,
            runner=runner,
            home=home,
            project_root=project_root,
            include_user_manifest=include_user_manifest,
        )
