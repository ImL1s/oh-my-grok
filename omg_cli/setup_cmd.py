# omg_cli/setup_cmd.py
"""Project setup plus the immutable OMG release-install transaction.

``omg setup`` remains a non-destructive project reconciler.  Release and local
plugin installation are separate, explicit entry points used by
``scripts/install.sh`` and ``scripts/install-plugin.sh``.  Both routes converge
on :func:`install_package`, so archive verification, immutable staging, host
plugin activation, CLI switching, doctor classification, receipts and rollback
cannot drift into two implementations.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping, NamedTuple

from omg_cli.state import ensure_omg_dirs

OMG_START = "<!-- OMG:START -->"
OMG_END = "<!-- OMG:END -->"
GITIGNORE_MARKER = "# oh-my-grok"

INSTALL_STORE_KIND = "omg_install_receipt"
INSTALL_SCHEMA_VERSION = 1
PLUGIN_NAME = "oh-my-grok"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

# The exact runtime/plugin package.  Tests, VCS metadata and local state are
# intentionally excluded from installed identity.  Every directory is walked
# recursively with symlinks rejected and byte/mode inventory sorted by UTF-8.
SHIPPING_ROOTS = (
    "plugin.json",
    ".mcp.json",
    ".lsp.json",
    "pyproject.toml",
    "omg_capabilities.lock.json",
    "README.md",
    "README.zh-TW.md",
    "LICENSE",
    "bin",
    "omg_cli",
    "hooks",
    "agents",
    "skills",
    "templates",
    "scripts",
)
_IGNORED_PACKAGE_NAMES = {"__pycache__", ".DS_Store"}
_IGNORED_PACKAGE_SUFFIXES = {".pyc", ".pyo"}


class InstallError(RuntimeError):
    """Hard install failure.  The transaction has restored the prior surfaces."""


def plugin_root() -> Path:
    """Repo / plugin root (parent of omg_cli package)."""
    return Path(__file__).resolve().parents[1]


def _templates_dir() -> Path:
    return plugin_root() / "templates"


def _read_template(name: str) -> str:
    path = _templates_dir() / name
    if not path.is_file():
        raise FileNotFoundError(f"missing template: {path}")
    return path.read_text(encoding="utf-8")


def merge_agents_fragment(project_root: Path) -> str:
    """Write or merge AGENTS.fragment.md into project AGENTS.md.

    Returns action: 'created' | 'appended' | 'unchanged'.
    """
    fragment = _read_template("AGENTS.fragment.md").rstrip() + "\n"
    # Ensure markers wrap fragment for idempotent merge
    if OMG_START not in fragment:
        fragment = f"{OMG_START}\n{fragment}{OMG_END}\n"
    elif OMG_END not in fragment:
        fragment = fragment.rstrip() + f"\n{OMG_END}\n"

    agents_path = project_root / "AGENTS.md"
    if not agents_path.exists():
        agents_path.write_text(fragment, encoding="utf-8")
        return "created"

    existing = agents_path.read_text(encoding="utf-8")
    if OMG_START in existing:
        return "unchanged"

    # Append marker block
    sep = "" if existing.endswith("\n") else "\n"
    agents_path.write_text(existing + sep + "\n" + fragment, encoding="utf-8")
    return "appended"


def merge_gitignore_fragment(project_root: Path) -> str:
    """Write or merge gitignore fragment. Returns action string."""
    fragment = _read_template("gitignore.fragment").rstrip() + "\n"
    gi_path = project_root / ".gitignore"

    if not gi_path.exists():
        body = fragment
        if GITIGNORE_MARKER not in body:
            body = f"{GITIGNORE_MARKER}\n{body}"
        gi_path.write_text(body, encoding="utf-8")
        return "created"

    existing = gi_path.read_text(encoding="utf-8")
    # Idempotent: if marker present or all key lines already ignored, skip
    if GITIGNORE_MARKER in existing:
        return "unchanged"
    key_lines = [
        ln.strip()
        for ln in fragment.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if key_lines and all(any(kl in line for line in existing.splitlines()) for kl in key_lines):
        return "unchanged"

    sep = "" if existing.endswith("\n") else "\n"
    block = fragment
    if GITIGNORE_MARKER not in block:
        block = f"{GITIGNORE_MARKER}\n{block}"
    gi_path.write_text(existing + sep + "\n" + block, encoding="utf-8")
    return "appended"


def run_setup(
    project_root: Path | None = None,
    *,
    install_rules: bool = True,
    install_hook: bool = True,
) -> int:
    from omg_cli.compat import format_isolation_banner

    root = Path(project_root or Path.cwd()).resolve()
    ensure_omg_dirs(root)

    agents_action = merge_agents_fragment(root)
    gi_action = merge_gitignore_fragment(root)

    print(f"oh-my-grok setup complete in {root}")
    print("  .omg/ dirs: ensured")
    print(f"  AGENTS.md: {agents_action}")
    print(f"  .gitignore: {gi_action}")

    if install_rules:
        try:
            from omg_cli.guidance import GuidanceError, install_global_rules

            rpath, raction = install_global_rules()
            print(f"  {rpath}: {raction}")
        except GuidanceError as e:
            print(f"  global rules: SKIPPED ({e})")  # never crash setup

    if install_hook:
        # Install the global PreToolUse soft-gate under $GROK_HOME/hooks/ (self-
        # contained, always readable — never a checkout-path script that bricks
        # other workspaces). Transactional + never raises → never crashes setup.
        try:
            from omg_cli.hook_install import install_global_hook

            hpath, haction = install_global_hook()
            print(f"  {hpath}: {haction}")
            if haction in ("migrated", "quarantined-no-source"):
                print(
                    "    (repaired a prior checkout-path hook that could deny every "
                    "tool; restart any running grok session to pick it up)"
                )
        except Exception as e:  # noqa: BLE001 — never crash setup
            print(f"  global hook: SKIPPED ({type(e).__name__}: {e})")
    else:
        print("  global hook: skipped (--no-global-hook); doctor will report it missing")

    print()
    print("Install/refresh oh-my-grok with the checksum-verified release bootstrap:")
    print("  (performs the exact Grok plugin install and atomic CLI switch)")
    print()
    print("  curl -fsSL https://raw.githubusercontent.com/ImL1s/oh-my-grok/main/scripts/install.sh | bash")
    print()
    print("Manual/offline (already-downloaded release bytes; no pip/npm/network):")
    print("  bash install.sh --offline --archive oh-my-grok-X.Y.Z.tar.gz --checksums SHA256SUMS")
    print()
    print("Maintainers developing from a clean checkout can instead run:")
    print(f"  cd {plugin_root()} && bash scripts/install-plugin.sh")
    print()
    print("Global guidance (~/.grok/rules/omg.md) is installed and loads every")
    print("Grok session (skip with: omg setup --no-global-rules).")
    print()
    print("Then verify:")
    print("  omg doctor")
    print()
    # Always print isolation banner after success (compat.claude C1)
    print(format_isolation_banner())
    return 0


# ---------------------------------------------------------------------------
# Immutable package identity and release archive verification


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_package_rel(relative: str) -> str:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or str(pure) != relative:
        raise InstallError(f"unsafe package path: {relative!r}")
    return relative


def _iter_shipping_files(root: Path) -> list[tuple[str, Path]]:
    root = root.resolve()
    rows: list[tuple[str, Path]] = []

    def visit(path: Path, relative: str) -> None:
        _safe_package_rel(relative)
        try:
            info = path.lstat()
        except OSError as exc:
            raise InstallError(f"shipping path unreadable: {relative}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise InstallError(f"shipping package rejects symlink: {relative}")
        if stat.S_ISREG(info.st_mode):
            if path.name in _IGNORED_PACKAGE_NAMES or path.suffix in _IGNORED_PACKAGE_SUFFIXES:
                return
            rows.append((relative, path))
            return
        if not stat.S_ISDIR(info.st_mode):
            raise InstallError(f"shipping package rejects non-file: {relative}")
        for child in sorted(path.iterdir(), key=lambda item: item.name.encode("utf-8")):
            if child.name in _IGNORED_PACKAGE_NAMES:
                continue
            visit(child, f"{relative}/{child.name}")

    for relative in SHIPPING_ROOTS:
        path = root / relative
        if not path.exists():
            # Documentation translations are optional; runtime identity is not.
            if relative == "README.zh-TW.md":
                continue
            raise InstallError(f"required shipping path missing: {relative}")
        visit(path, relative)
    rows.sort(key=lambda row: row[0].encode("utf-8"))
    return rows


def compute_package_identity(root: Path | str) -> dict[str, Any]:
    """Hash deterministic shipping bytes and their executable-mode contract."""

    package_root = Path(root).resolve()
    try:
        plugin = json.loads((package_root / "plugin.json").read_text(encoding="utf-8"))
        lock = json.loads(
            (package_root / "omg_capabilities.lock.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError("package identity metadata is missing or malformed") from exc
    if not isinstance(plugin, dict) or plugin.get("name") != PLUGIN_NAME:
        raise InstallError(f"plugin.json name must be {PLUGIN_NAME}")
    version = str(plugin.get("version") or "")
    if not _SEMVER_RE.fullmatch(version):
        raise InstallError("plugin.json version is not semantic version")
    if not isinstance(lock, dict) or lock.get("version") != version:
        raise InstallError("capability lock version differs from plugin version")
    inventory: list[dict[str, Any]] = []
    for relative, path in _iter_shipping_files(package_root):
        body = path.read_bytes()
        executable = bool(path.stat().st_mode & 0o111)
        inventory.append(
            {
                "path": relative,
                "type": "regular_file",
                "mode": "0555" if executable else "0444",
                "byte_length": len(body),
                "sha256": _sha256_bytes(body),
                "executable": executable,
                "source": relative,
                "owner": PLUGIN_NAME,
            }
        )
    if not inventory:
        raise InstallError("shipping inventory is empty")
    digest = _sha256_bytes(_canonical_bytes(inventory))
    return {
        "store_kind": "omg_package_identity",
        "schema_version": 1,
        "name": PLUGIN_NAME,
        "version": version,
        "root_realpath": str(package_root),
        "digest": digest,
        "inventory": inventory,
    }


def _checksum_entry(checksums: Path, asset_name: str) -> str:
    try:
        lines = checksums.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError("SHA256SUMS is unreadable") from exc
    matches: list[str] = []
    for raw in lines:
        if not raw.strip():
            continue
        match = re.fullmatch(r"([0-9A-Fa-f]{64})[ \t]+[*]?([^\r\n]+)", raw)
        if match is None:
            raise InstallError("SHA256SUMS contains a malformed record")
        digest, name = match.groups()
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise InstallError("SHA256SUMS asset names must be basenames")
        if name == asset_name:
            matches.append(digest.lower())
    if len(matches) != 1:
        raise InstallError("SHA256SUMS must contain exactly one record for the archive")
    return matches[0]


def verify_release_archive(
    asset: Path | str,
    checksums: Path | str | None = None,
    *,
    expected_sha256: str | None = None,
) -> dict[str, str]:
    """Verify one immutable release asset against explicit trusted checksum bytes."""

    asset_path = Path(asset).resolve()
    if not asset_path.is_file():
        raise InstallError("release archive is missing")
    if not re.fullmatch(r"oh-my-grok-[0-9A-Za-z.+-]+\.tar\.gz", asset_path.name):
        raise InstallError("release archive name is not canonical")
    actual = _sha256_file(asset_path)
    expected_values: list[str] = []
    sums_path: Path | None = None
    if checksums is not None:
        sums_path = Path(checksums).resolve()
        expected_values.append(_checksum_entry(sums_path, asset_path.name))
    if expected_sha256 is not None:
        normalized = expected_sha256.lower()
        if not _SHA256_RE.fullmatch(normalized):
            raise InstallError("expected archive checksum is not SHA-256")
        expected_values.append(normalized)
    if not expected_values:
        raise InstallError("release archive requires SHA256SUMS or an explicit SHA-256")
    if any(value != actual for value in expected_values):
        raise InstallError("release archive checksum mismatch")
    return {
        "asset_path": str(asset_path),
        "asset_name": asset_path.name,
        "asset_sha256": actual,
        "checksums_path": str(sums_path) if sums_path is not None else "",
        "checksums_sha256": _sha256_file(sums_path) if sums_path is not None else "",
    }


def extract_release_archive(asset: Path | str, destination: Path | str) -> Path:
    """Bounded link-free tar extraction; return the unique package root."""

    asset_path = Path(asset).resolve()
    output = Path(destination).resolve()
    if output.exists() and any(output.iterdir()):
        raise InstallError("release extraction destination is not empty")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with tarfile.open(asset_path, "r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > 50_000:
                raise InstallError("unsafe archive member count")
            total = 0
            for member in members:
                name = member.name.rstrip("/")
                pure = PurePosixPath(name)
                if (
                    not name
                    or pure.is_absolute()
                    or ".." in pure.parts
                    or str(pure) != name
                    or member.issym()
                    or member.islnk()
                    or not (member.isdir() or member.isfile())
                ):
                    raise InstallError(f"unsafe archive member: {member.name!r}")
                total += int(member.size)
                if total > 512 * 1024 * 1024 or member.size > 64 * 1024 * 1024:
                    raise InstallError("unsafe archive size")
                target = output.joinpath(*pure.parts)
                try:
                    target.relative_to(output)
                except ValueError as exc:  # pragma: no cover - pure path guard above
                    raise InstallError("unsafe archive path escape") from exc
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o755)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                source = archive.extractfile(member)
                if source is None:
                    raise InstallError("archive file payload is missing")
                with source, target.open("wb") as stream:
                    shutil.copyfileobj(source, stream, length=1024 * 1024)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    except (tarfile.TarError, OSError) as exc:
        raise InstallError("release archive extraction failed") from exc
    candidates = sorted(
        (path.parent for path in output.rglob("plugin.json")),
        key=lambda path: str(path).encode("utf-8"),
    )
    valid: list[Path] = []
    for candidate in candidates:
        try:
            payload = json.loads((candidate / "plugin.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("name") == PLUGIN_NAME:
            valid.append(candidate.resolve())
    if len(valid) != 1:
        raise InstallError("release archive must contain exactly one oh-my-grok package root")
    compute_package_identity(valid[0])
    return valid[0]


def _copy_package_to_stage(source: Path, destination: Path, identity: Mapping[str, Any]) -> None:
    temporary = destination.with_name(f".{destination.name}.stage-{uuid.uuid4().hex}")
    temporary.mkdir(parents=True, mode=0o700)
    try:
        for row in identity["inventory"]:
            relative = str(row["path"])
            src = source.joinpath(*PurePosixPath(relative).parts)
            dst = temporary.joinpath(*PurePosixPath(relative).parts)
            dst.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            dst.write_bytes(src.read_bytes())
            dst.chmod(0o755 if row["executable"] else 0o644)
        staged = compute_package_identity(temporary)
        if staged["digest"] != identity["digest"] or staged["version"] != identity["version"]:
            raise InstallError("staged package identity differs from source")
        for directory in sorted(
            (path for path in temporary.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            directory.chmod(0o555)
        for file_path in (path for path in temporary.rglob("*") if path.is_file()):
            current = file_path.stat().st_mode
            file_path.chmod(0o555 if current & 0o111 else 0o444)
        temporary.chmod(0o555)
        try:
            os.replace(temporary, destination)
        except OSError as exc:
            if destination.exists():
                existing = compute_package_identity(destination)
                if existing["digest"] == identity["digest"]:
                    shutil.rmtree(temporary, ignore_errors=True)
                    return
            raise InstallError("immutable stage publish failed") from exc
    except Exception:
        if temporary.exists():
            for path in temporary.rglob("*"):
                try:
                    path.chmod(0o700 if path.is_dir() else 0o600)
                except OSError:
                    pass
            shutil.rmtree(temporary, ignore_errors=True)
        raise


def _verify_immutable_stage(stage: Path, identity: Mapping[str, Any]) -> None:
    """Reread staged bytes/type/mode inventory exactly before any switch."""

    actual = compute_package_identity(stage)
    if actual["digest"] != identity["digest"] or actual["inventory"] != identity["inventory"]:
        raise InstallError("immutable stage inventory readback differs from source")
    for row in identity["inventory"]:
        path = stage.joinpath(*PurePosixPath(str(row["path"])).parts)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != int(str(row["mode"]), 8):
            raise InstallError("immutable stage file type/mode differs from inventory")
    directories = [stage, *(path for path in stage.rglob("*") if path.is_dir())]
    if any(path.is_symlink() or stat.S_IMODE(path.lstat().st_mode) != 0o555 for path in directories):
        raise InstallError("immutable stage directory mode differs from contract")


def stage_immutable_package(
    source_root: Path | str,
    releases_dir: Path | str,
) -> tuple[Path, dict[str, Any]]:
    source = Path(source_root).resolve()
    identity = compute_package_identity(source)
    releases = Path(releases_dir).resolve()
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = releases / f"{identity['version']}-{identity['digest'][:16]}"
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise InstallError("immutable stage path is not a regular directory")
        installed = compute_package_identity(destination)
        if installed["digest"] != identity["digest"]:
            raise InstallError("immutable stage path already contains different bytes")
    else:
        _copy_package_to_stage(source, destination, identity)
    _verify_immutable_stage(destination, identity)
    return destination.resolve(), identity


# ---------------------------------------------------------------------------
# Host transaction, receipts and rollback


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _redact_detail(value: object) -> str:
    text = str(value)
    text = re.sub(r"(?i)(authorization|cookie|token|secret|password)=?[^\s,;]*", r"\1=<redacted>", text)
    text = re.sub(r"https?://[^\s?]+\?[^\s]+", "<redacted-url>", text)
    return text[:1000]


@contextmanager
def _install_lock(store: Path):
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = store / "install.lock"
    with lock_path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _symlink_snapshot(path: Path) -> dict[str, Any]:
    if not _lexists(path):
        return {"path": str(path), "kind": "absent", "target": None}
    info = path.lstat()
    if not stat.S_ISLNK(info.st_mode):
        return {"path": str(path), "kind": "foreign", "target": None}
    return {"path": str(path), "kind": "symlink", "target": os.readlink(path)}


def _restore_symlink(snapshot: Mapping[str, Any], *, expected_current: str | None = None) -> None:
    path = Path(str(snapshot["path"]))
    if _lexists(path):
        if expected_current is not None:
            if not path.is_symlink() or os.readlink(path) != expected_current:
                raise InstallError("managed pointer changed concurrently during rollback")
        path.unlink()
    if snapshot["kind"] == "symlink":
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.symlink_to(str(snapshot["target"]))
    elif snapshot["kind"] == "foreign":
        raise InstallError("cannot restore a foreign non-symlink pointer")


def _atomic_symlink(target: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.switch-{uuid.uuid4().hex}")
    temporary.symlink_to(target)
    try:
        os.replace(temporary, path)
    finally:
        if _lexists(temporary):
            temporary.unlink()


def _publish_symlink_no_clobber(target: str, path: Path) -> None:
    """Publish an absent pointer without replacing a concurrent filesystem entry."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.symlink_to(target)
    except FileExistsError as exc:
        raise InstallError(
            "managed receipt pointer appeared concurrently during publication"
        ) from exc


def _file_snapshot(path: Path) -> dict[str, Any]:
    if not _lexists(path):
        return {"path": str(path), "kind": "absent", "body": None, "mode": None, "target": None}
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return {"path": str(path), "kind": "symlink", "body": None, "mode": None, "target": os.readlink(path)}
    if not stat.S_ISREG(info.st_mode):
        raise InstallError("managed global path is neither regular file nor symlink")
    return {
        "path": str(path),
        "kind": "file",
        "body": path.read_bytes(),
        "mode": stat.S_IMODE(info.st_mode),
        "target": None,
    }


def _restore_file(snapshot: Mapping[str, Any]) -> None:
    path = Path(str(snapshot["path"]))
    if _lexists(path):
        if path.is_dir() and not path.is_symlink():
            raise InstallError("managed global file was replaced by a directory")
        path.unlink()
    kind = snapshot["kind"]
    if kind == "absent":
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if kind == "symlink":
        path.symlink_to(str(snapshot["target"]))
    elif kind == "file":
        temporary = path.with_name(f".{path.name}.restore-{uuid.uuid4().hex}")
        temporary.write_bytes(bytes(snapshot["body"]))
        temporary.chmod(int(snapshot["mode"]))
        os.replace(temporary, path)
    else:  # pragma: no cover - only values built above
        raise InstallError("unknown file snapshot kind")


def _command_record(argv: list[str], result: Any) -> dict[str, Any]:
    stdout = str(getattr(result, "stdout", "") or "")
    stderr = str(getattr(result, "stderr", "") or "")
    return {
        "argv": argv,
        "rc": int(getattr(result, "returncode", 1)),
        "stdout_sha256": _sha256_bytes(stdout.encode("utf-8", errors="replace")),
        "stderr_sha256": _sha256_bytes(stderr.encode("utf-8", errors="replace")),
    }


def _run_host(
    runner: Callable[..., Any],
    argv: list[str],
    records: list[dict[str, Any]],
    *,
    required: bool = True,
) -> Any:
    try:
        result = runner(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(f"host command failed to start: {argv[:3]!r}") from exc
    records.append(_command_record(argv, result))
    if required and int(getattr(result, "returncode", 1)) != 0:
        raise InstallError(f"host command rejected transaction: {argv[:3]!r}")
    return result


def _plugin_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("plugins", "items", "data", "result"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return [payload]
    return []


def _read_plugin_inventory(
    runner: Callable[..., Any], records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result = _run_host(runner, ["grok", "plugin", "list", "--json"], records)
    try:
        payload = json.loads(str(getattr(result, "stdout", "") or ""))
    except json.JSONDecodeError as exc:
        raise InstallError("grok plugin inventory is malformed") from exc
    matching = []
    for item in _plugin_entries(payload):
        name = str(item.get("name") or item.get("id") or item.get("plugin") or "")
        if name == PLUGIN_NAME or name.startswith(PLUGIN_NAME + "@"):
            matching.append(item)
    if len(matching) > 1:
        raise InstallError("multiple oh-my-grok plugin entries are ambiguous; preserved without mutation")
    return matching


def _resolve_entry_identity(
    entry: Mapping[str, Any],
    *,
    allow_source_fallback: bool,
) -> tuple[Path, dict[str, Any]]:
    """Resolve host identity without masking an invalid installed snapshot.

    ``installPath``/``install_path``/``path`` are authoritative in that order.
    Once one is present, malformed or missing bytes are a hard failure; a valid
    ``source`` must never hide corruption in the host-managed copy.  Source-only
    fallback is reserved for explicitly legacy/pre-mutation inventory reads.
    """

    for key in ("installPath", "install_path", "path"):
        raw = entry.get(key)
        if raw is None or raw == "":
            continue
        if not isinstance(raw, str) or not raw.strip():
            raise InstallError("authoritative installed plugin path is malformed")
        candidate = Path(os.path.expanduser(raw)).absolute()
        try:
            identity = compute_package_identity(candidate)
        except InstallError as exc:
            raise InstallError("authoritative installed plugin identity is unresolved") from exc
        return candidate.resolve(), identity
    if allow_source_fallback:
        raw_source = entry.get("source")
        if isinstance(raw_source, str) and raw_source.strip():
            source = Path(os.path.expanduser(raw_source)).absolute()
            try:
                return source.resolve(), compute_package_identity(source)
            except InstallError as exc:
                raise InstallError("plugin source fallback identity is unresolved") from exc
    raise InstallError("authoritative installed plugin path is missing")


def _plugin_entry_is_enabled(entry: Mapping[str, Any], *, grok_home: Path) -> bool:
    enabled = entry.get("enabled")
    if isinstance(enabled, bool):
        return enabled
    if str(entry.get("status") or "").strip().lower() in {"disabled", "inactive"}:
        return False
    try:
        config = tomllib.loads((grok_home / "config.toml").read_text(encoding="utf-8"))
        plugins = config.get("plugins")
        values = plugins.get("enabled") if isinstance(plugins, dict) else None
        return isinstance(values, list) and PLUGIN_NAME in values
    except (OSError, tomllib.TOMLDecodeError):
        return False


def _verify_host_plugin_path(plugin_path: Path, *, stage: Path, grok_home: Path) -> None:
    """Confine a host-selected plugin to the stage or Grok's managed copy root."""

    if plugin_path == stage:
        return
    installed_plugins = grok_home / "installed-plugins"
    try:
        parent_info = installed_plugins.lstat()
        plugin_info = plugin_path.lstat()
    except OSError as exc:
        raise InstallError("host plugin path is missing") from exc
    if (
        plugin_path.parent != installed_plugins
        or not stat.S_ISDIR(parent_info.st_mode)
        or stat.S_ISLNK(parent_info.st_mode)
        or not stat.S_ISDIR(plugin_info.st_mode)
        or stat.S_ISLNK(plugin_info.st_mode)
    ):
        raise InstallError("host plugin path escapes Grok managed plugin storage")


def _is_omg_cli_target(target: Path) -> bool:
    try:
        real = target.resolve(strict=True)
        if real.name != "omg" or real.parent.name != "bin":
            return False
        plugin = json.loads((real.parents[1] / "plugin.json").read_text(encoding="utf-8"))
        return isinstance(plugin, dict) and plugin.get("name") == PLUGIN_NAME
    except (OSError, json.JSONDecodeError, IndexError):
        return False


def classify_doctor_probe(mode: str, probe: Mapping[str, Any]) -> str:
    from scripts.omg_install_classifier import classify_doctor_result

    rc = probe.get("rc")
    classification = classify_doctor_result(
        mode=mode,
        rc=rc if isinstance(rc, int) and not isinstance(rc, bool) else None,
        valid=probe.get("valid") is True,
    )
    if classification == "hard_failure":
        if probe.get("valid") is not True or not isinstance(rc, int) or isinstance(rc, bool):
            raise InstallError("doctor output is malformed")
        raise InstallError(f"doctor gate rejected candidate (rc={rc})")
    return classification


def _default_doctor_probe(stage: Path, env: dict[str, str]) -> dict[str, Any]:
    argv = [sys.executable, "-I", "-B", str(stage / "bin" / "omg"), "doctor", "--strict"]
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=env,
        cwd=tempfile.gettempdir(),
        timeout=60,
        check=False,
    )
    rc = result.returncode
    if rc != 0 and env.get("OMG_INSTALL_MODE") == "development":
        # Strict doctor deliberately promotes local coexistence/compatibility
        # warnings.  Re-run without promotion to distinguish those soft risks
        # from actual install-integrity failures.  The lifecycle classifier
        # maps this exact soft-only case to development-only rc=2; release never
        # receives this relaxation.
        relaxed = subprocess.run(
            argv[:-1],
            capture_output=True,
            text=True,
            env=env,
            cwd=tempfile.gettempdir(),
            timeout=60,
            check=False,
        )
        if relaxed.returncode == 0:
            rc = 2
    return {
        "argv": argv,
        "rc": rc,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "valid": True,
    }


def _probe_record(probe: Mapping[str, Any]) -> dict[str, Any]:
    argv = probe.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        argv = ["omg", "doctor", "--strict"]
    stdout = str(probe.get("stdout") or "")
    stderr = str(probe.get("stderr") or "")
    rc_value = probe.get("rc")
    rc = rc_value if isinstance(rc_value, int) else 1
    return {
        "argv": list(argv),
        "rc": rc,
        "stdout_sha256": _sha256_bytes(stdout.encode("utf-8", errors="replace")),
        "stderr_sha256": _sha256_bytes(stderr.encode("utf-8", errors="replace")),
    }


def _receipt_material(
    *,
    transaction_id: str,
    status: str,
    mode: str,
    source: Mapping[str, Any],
    stage: Path,
    plugin_path: Path | None,
    asset: Mapping[str, str] | None,
    source_uri: str | None,
    source_tag: str | None,
    commands: list[dict[str, Any]],
    owned_inventory: list[dict[str, str]],
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "store_kind": INSTALL_STORE_KIND,
        "schema_version": INSTALL_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "status": status,
        "mode": mode,
        "source": {
            "uri": source_uri,
            "tag": source_tag,
            "asset_name": asset.get("asset_name") if asset else None,
            "asset_sha256": asset.get("asset_sha256") if asset else None,
            "checksums_sha256": asset.get("checksums_sha256") if asset else None,
            "package_realpath": str(source["root_realpath"]),
            "package_version": str(source["version"]),
            "package_digest": str(source["digest"]),
        },
        "installed": {
            "stage_realpath": str(stage),
            "plugin_realpath": str(plugin_path) if plugin_path is not None else None,
            "package_version": str(source["version"]),
            "package_digest": str(source["digest"]),
            "inventory": source["inventory"],
        },
        "owned_inventory": sorted(owned_inventory, key=lambda row: row["path"].encode("utf-8")),
        "commands": commands,
        "error": _redact_detail(error) if error else None,
        "created_at": _utc_now(),
    }


def _write_install_receipt(receipts: Path, material: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    receipt_material = dict(material)
    receipt = {
        **receipt_material,
        "receipt_hash": _sha256_bytes(_canonical_bytes(receipt_material)),
    }
    path = receipts / f"{receipt['transaction_id']}.json"
    receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = _canonical_bytes(receipt)
    fd, temporary_name = tempfile.mkstemp(dir=receipts, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, 0o400)
        if path.exists():
            if path.read_bytes() != body:
                raise InstallError("immutable receipt already exists with different bytes")
            os.unlink(temporary_name)
        else:
            os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return path, receipt


def read_install_receipt(path: Path | str) -> dict[str, Any]:
    receipt_path = Path(path)
    try:
        info = receipt_path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o400:
            raise InstallError("install receipt must be immutable 0400 regular file")
        raw = receipt_path.read_bytes()
        receipt = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("install receipt is unreadable or malformed") from exc
    if not isinstance(receipt, dict):
        raise InstallError("install receipt must contain an object")
    required = {
        "store_kind", "schema_version", "transaction_id", "status", "mode",
        "source", "installed", "owned_inventory", "commands", "error",
        "created_at", "receipt_hash",
    }
    if set(receipt) != required:
        raise InstallError("install receipt keys differ from schema")
    if receipt["store_kind"] != INSTALL_STORE_KIND or receipt["schema_version"] != 1:
        raise InstallError("install receipt header mismatch")
    if receipt["status"] not in {"installed", "completed_with_warning", "rolled_back", "uninstalled"}:
        raise InstallError("install receipt status is invalid")
    digest = receipt["receipt_hash"]
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise InstallError("install receipt hash is invalid")
    material = {key: receipt[key] for key in receipt if key != "receipt_hash"}
    if _sha256_bytes(_canonical_bytes(material)) != digest:
        raise InstallError("install receipt hash mismatch")
    return receipt


def _current_receipt(store: Path) -> tuple[Path, dict[str, Any]] | None:
    pointer = store / "current-receipt"
    if not pointer.is_symlink():
        return None
    try:
        path = pointer.resolve(strict=True)
        path.relative_to((store / "receipts").resolve())
        return path, read_install_receipt(path)
    except (OSError, ValueError, InstallError):
        raise InstallError("current install receipt pointer is corrupt")


class VerifiedCurrentInstall(NamedTuple):
    receipt_path: Path
    receipt: dict[str, Any]
    stage: Path


def verified_current_install(store: Path, cli_pointer: Path) -> VerifiedCurrentInstall:
    """Prove that current receipt, stage, plugin, and pointers are store-owned.

    This is the destructive/execute authority boundary used by update and
    uninstall.  Receipt hashes authenticate bytes, but do not by themselves
    confine paths, so every path is also checked lexically and without an
    intermediate symlink before any caller may mutate or execute it.
    """

    store = store.absolute()
    receipts = store / "receipts"
    releases = store / "releases"
    current = store / "current"
    pointer = store / "current-receipt"

    for directory in (store, receipts, releases):
        try:
            info = directory.lstat()
        except OSError as exc:
            raise InstallError("managed install store is missing") from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise InstallError("managed install store contains a symlink or non-directory")

    try:
        if not pointer.is_symlink():
            raise InstallError("current receipt pointer is not a symlink")
        raw_receipt = Path(os.readlink(pointer))
        if not raw_receipt.is_absolute():
            raise InstallError("current receipt pointer must use its canonical absolute target")
        receipt_path = Path(os.path.normpath(str(raw_receipt)))
        if receipt_path != raw_receipt or receipt_path.parent != receipts:
            raise InstallError("current receipt target escapes the immutable receipt store")
        receipt = read_install_receipt(receipt_path)
        if receipt.get("status") not in {"installed", "completed_with_warning"}:
            raise InstallError("current receipt is not an active install")
        transaction_id = receipt.get("transaction_id")
        if (
            not isinstance(transaction_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", transaction_id)
            or receipt_path.name != f"{transaction_id}.json"
        ):
            raise InstallError("current receipt path does not match its transaction")

        installed = receipt.get("installed")
        if not isinstance(installed, dict):
            raise InstallError("current receipt installed record is malformed")
        raw_stage_value = installed.get("stage_realpath")
        if not isinstance(raw_stage_value, str):
            raise InstallError("current receipt stage is malformed")
        raw_stage = Path(raw_stage_value)
        if not raw_stage.is_absolute():
            raise InstallError("current receipt stage must be absolute")
        stage = Path(os.path.normpath(str(raw_stage)))
        if stage != raw_stage or stage.parent != releases:
            raise InstallError("current receipt stage escapes the immutable release store")
        stage_info = stage.lstat()
        if not stat.S_ISDIR(stage_info.st_mode) or stat.S_ISLNK(stage_info.st_mode):
            raise InstallError("current receipt stage is not a regular release directory")
        identity = compute_package_identity(stage)
        digest = installed.get("package_digest")
        if not isinstance(digest, str) or identity["digest"] != digest:
            raise InstallError("immutable stage package digest differs from receipt")
        if installed.get("package_version") != identity["version"]:
            raise InstallError("immutable stage package version differs from receipt")
        if stage.name != f"{identity['version']}-{identity['digest'][:16]}":
            raise InstallError("immutable stage path does not match package identity")
        raw_plugin_value = installed.get("plugin_realpath")
        if not isinstance(raw_plugin_value, str):
            raise InstallError("current receipt host plugin path is malformed")
        raw_plugin = Path(raw_plugin_value)
        if not raw_plugin.is_absolute():
            raise InstallError("current receipt host plugin path must be absolute")
        plugin_path = Path(os.path.normpath(str(raw_plugin)))
        if plugin_path != raw_plugin:
            raise InstallError("current receipt host plugin path is not canonical")
        _verify_host_plugin_path(plugin_path, stage=stage, grok_home=store.parent)
        plugin_identity = compute_package_identity(plugin_path)
        if (
            plugin_identity["digest"] != digest
            or plugin_identity["version"] != identity["version"]
            or plugin_identity["inventory"] != identity["inventory"]
        ):
            raise InstallError("host plugin bytes differ from immutable stage")

        if not current.is_symlink() or os.readlink(current) != str(stage):
            raise InstallError("current pointer is not the exact receipt-owned target")
        if current.resolve(strict=True) != stage:
            raise InstallError("current pointer resolution differs from receipt")
        expected_cli_target = current / "bin" / "omg"
        if not cli_pointer.is_symlink() or os.readlink(cli_pointer) != str(expected_cli_target):
            raise InstallError("CLI pointer is not the exact receipt-owned target")
        if cli_pointer.resolve(strict=True) != stage / "bin" / "omg":
            raise InstallError("CLI pointer resolution differs from receipt")

        owned = receipt.get("owned_inventory")
        if not isinstance(owned, list):
            raise InstallError("receipt ownership inventory is malformed")
        expected_rows = {
            (str(stage), "immutable_stage", digest),
            (str(current), "current_pointer", digest),
            (str(cli_pointer), "cli_pointer", digest),
            (str(plugin_path), "host_plugin", digest),
        }
        actual_rows = {
            (row.get("path"), row.get("kind"), row.get("identity"))
            for row in owned
            if isinstance(row, dict)
        }
        if not expected_rows.issubset(actual_rows):
            raise InstallError("receipt does not own the exact install targets")
    except Exception as exc:  # noqa: BLE001 — normalize all malformed receipt shapes
        raise InstallError("current install confinement proof failed") from exc

    return VerifiedCurrentInstall(receipt_path, receipt, stage)


def _source_is_dirty(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # A verified release archive is deliberately installable on hosts with no
        # VCS client.  The extracted package cannot be a dirty checkout, so
        # absence/failure of git is not a reason to require network or a clone.
        return False
    if result.returncode != 0 or result.stdout.strip() != "true":
        return False
    try:
        status_result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # If git identified a worktree but cannot prove it clean, fail closed.
        return True
    return status_result.returncode != 0 or bool(status_result.stdout.strip())


def install_package(
    source_root: Path | str,
    *,
    home: Path | str | None = None,
    grok_home: Path | str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    doctor_probe: Callable[[Path, dict[str, str]], Mapping[str, Any]] | None = None,
    mode: str = "release",
    asset: Path | str | None = None,
    checksums: Path | str | None = None,
    expected_asset_sha256: str | None = None,
    source_uri: str | None = None,
    source_tag: str | None = None,
    failpoint: str | None = None,
) -> dict[str, Any]:
    """Install one exact package as a jointly rolled-back CLI/plugin transaction.

    The host registry is external and cannot offer a multi-object POSIX rename.
    This function therefore serializes OMG mutations, stages immutable bytes,
    records every host call and guarantees *all-or-prior* visibility: any hard
    failure restores the prior plugin, CLI/current pointers, hook and guidance.
    """

    if mode not in {"release", "development"}:
        raise InstallError("install mode must be release or development")
    if failpoint not in {None, "before_pointer_switch", "after_pointer_switch"}:
        raise InstallError("unknown install failure injection point")
    source = Path(source_root).resolve()
    home_path = Path(home).resolve() if home is not None else Path(os.environ.get("HOME") or Path.home()).resolve()
    if grok_home is None:
        raw_grok = os.environ.get("GROK_HOME")
        grok_path = Path(raw_grok).expanduser().resolve() if raw_grok else home_path / ".grok"
    else:
        grok_path = Path(grok_home).resolve()
    if mode == "release" and _source_is_dirty(source):
        raise InstallError("release install refuses a dirty/local checkout")

    asset_evidence: dict[str, str] | None = None
    if mode == "release":
        if asset is None:
            raise InstallError("release install requires an immutable archive")
        asset_evidence = verify_release_archive(
            asset,
            checksums,
            expected_sha256=expected_asset_sha256,
        )

    # Validate source/tag/archive cohesion before publishing even an immutable
    # stage.  A mismatched release must leave no managed filesystem residue.
    identity = compute_package_identity(source)
    if mode == "release" and asset_evidence is not None:
        match = re.fullmatch(r"oh-my-grok-([0-9A-Za-z.+-]+)\.tar\.gz", asset_evidence["asset_name"])
        if match is None or match.group(1) != identity["version"]:
            raise InstallError("archive filename version differs from package identity")
        if source_tag is not None and source_tag != f"v{identity['version']}":
            raise InstallError("release source tag differs from package version")

    store = grok_path / "omg"
    releases = store / "releases"
    receipts = store / "receipts"
    current = store / "current"
    receipt_pointer = store / "current-receipt"
    cli_pointer = home_path / ".local" / "bin" / "omg"
    stage, staged_identity = stage_immutable_package(source, releases)
    if staged_identity != identity:
        raise InstallError("staged package identity differs from validated source")

    transaction_id = uuid.uuid4().hex
    commands: list[dict[str, Any]] = []
    owned_inventory = [
        {"path": str(stage), "kind": "immutable_stage", "identity": str(identity["digest"])},
        {"path": str(current), "kind": "current_pointer", "identity": str(identity["digest"])},
        {"path": str(cli_pointer), "kind": "cli_pointer", "identity": str(identity["digest"])},
    ]
    doctor = doctor_probe or _default_doctor_probe

    with _install_lock(store):
        current_snapshot = _symlink_snapshot(current)
        cli_snapshot = _symlink_snapshot(cli_pointer)
        receipt_snapshot = _symlink_snapshot(receipt_pointer)
        if current_snapshot["kind"] == "foreign":
            raise InstallError("foreign current install path is preserved")
        if cli_snapshot["kind"] == "foreign":
            raise InstallError("foreign CLI path is preserved")
        if cli_snapshot["kind"] == "symlink":
            raw_target = Path(str(cli_snapshot["target"]))
            resolved_target = raw_target if raw_target.is_absolute() else cli_pointer.parent / raw_target
            if not _is_omg_cli_target(resolved_target):
                raise InstallError("foreign CLI symlink is preserved")
        if receipt_snapshot["kind"] == "foreign":
            raise InstallError("foreign receipt pointer is preserved")

        hook_json = grok_path / "hooks" / "omg-pretool-deny.json"
        hook_py = grok_path / "hooks" / "omg_pretool_deny_standalone.py"
        rules = grok_path / "rules" / "omg.md"
        global_snapshots = [_file_snapshot(path) for path in (hook_json, hook_py, rules, rules.with_suffix(".md.bak"))]

        prior_rows = _read_plugin_inventory(runner, commands)
        prior_plugin_path: Path | None = None
        prior_plugin_identity: dict[str, Any] | None = None
        prior_restore_path: Path | None = None
        if prior_rows:
            prior_plugin_path, prior_plugin_identity = _resolve_entry_identity(
                prior_rows[0],
                allow_source_fallback=True,
            )
            prior_restore_path, restore_identity = stage_immutable_package(
                prior_plugin_path,
                releases,
            )
            if restore_identity["digest"] != prior_plugin_identity["digest"]:
                raise InstallError("prior plugin rollback snapshot differs from host bytes")

        candidate_plugin_path: Path | None = None
        plugin_mutated = False
        current_target = str(stage)
        cli_target = str(current / "bin" / "omg")

        # Exact idempotent readback: no host uninstall/install and no receipt churn.
        if (
            current_snapshot["kind"] == "symlink"
            and Path(str(current_snapshot["target"])).resolve() == stage
            and cli_snapshot["kind"] == "symlink"
            and (cli_pointer.parent / str(cli_snapshot["target"])).resolve() == (stage / "bin" / "omg").resolve()
            and prior_plugin_identity is not None
            and prior_plugin_identity["digest"] == identity["digest"]
        ):
            try:
                from omg_cli.hook_install import install_global_hook
                from omg_cli.guidance import install_global_rules

                _path, hook_action = install_global_hook(home=grok_path, root=stage)
                if hook_action.startswith("failed") or hook_action in {"skipped-no-source", "quarantined-no-source"}:
                    raise InstallError("global PreToolUse ownership reconciliation failed")
                install_global_rules(version=str(identity["version"]), home=grok_path)
                env = dict(os.environ)
                env.update(
                    {
                        "HOME": str(home_path),
                        "GROK_HOME": str(grok_path),
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "OMG_INSTALL_MODE": mode,
                    }
                )
                probe = dict(doctor(stage, env))
                classify_doctor_probe(mode, probe)
                verified_existing = verified_current_install(store, cli_pointer)
                receipt_path = verified_existing.receipt_path
                receipt = verified_existing.receipt
                if receipt["installed"]["package_digest"] != identity["digest"]:
                    raise InstallError("current receipt identity differs from exact install")
                return {
                    "ok": True,
                    "status": "already_installed",
                    "stage_path": str(stage),
                    "receipt_path": str(receipt_path),
                    "receipt_hash": receipt["receipt_hash"],
                    "package_digest": identity["digest"],
                }
            except Exception:
                for snapshot in reversed(global_snapshots):
                    _restore_file(snapshot)
                raise

        installed_receipt_path: Path | None = None
        receipt_pointer_hidden = False
        try:
            _run_host(runner, ["grok", "plugin", "validate", str(stage)], commands)
            if prior_rows:
                _run_host(
                    runner,
                    ["grok", "plugin", "uninstall", PLUGIN_NAME, "--confirm"],
                    commands,
                )
                plugin_mutated = True
            _run_host(runner, ["grok", "plugin", "install", str(stage), "--trust"], commands)
            plugin_mutated = True
            _run_host(runner, ["grok", "plugin", "enable", PLUGIN_NAME], commands)
            candidate_rows = _read_plugin_inventory(runner, commands)
            if len(candidate_rows) != 1:
                raise InstallError("installed plugin readback is absent")
            candidate_plugin_path, candidate_identity = _resolve_entry_identity(
                candidate_rows[0],
                allow_source_fallback=False,
            )
            _verify_host_plugin_path(
                candidate_plugin_path,
                stage=stage,
                grok_home=grok_path,
            )
            if (
                candidate_identity["version"] != identity["version"]
                or candidate_identity["digest"] != identity["digest"]
                or candidate_identity["inventory"] != identity["inventory"]
            ):
                raise InstallError("installed plugin bytes differ from immutable stage")
            if candidate_rows[0].get("enabled") is False:
                raise InstallError("installed plugin readback reports disabled")

            from omg_cli.hook_install import install_global_hook
            from omg_cli.guidance import install_global_rules, render_managed_block

            _hook_path, hook_action = install_global_hook(home=grok_path, root=stage)
            if hook_action.startswith("failed") or hook_action in {"skipped-no-source", "quarantined-no-source"}:
                raise InstallError("global PreToolUse ownership reconciliation failed")
            install_global_rules(version=str(identity["version"]), home=grok_path)

            if failpoint == "before_pointer_switch":
                raise InstallError("injected failure before pointer switch")
            _atomic_symlink(current_target, current)
            _atomic_symlink(cli_target, cli_pointer)
            if failpoint == "after_pointer_switch":
                raise InstallError("injected failure after pointer switch")

            # A prior receipt describes the prior current pointer.  Hide it while
            # the replacement is pending so doctor validates the explicit
            # OMG_EXPECTED_INSTALL_* candidate rather than mixing new pointers
            # with stale receipt identity.
            if receipt_snapshot["kind"] == "symlink":
                if (
                    not receipt_pointer.is_symlink()
                    or os.readlink(receipt_pointer) != receipt_snapshot["target"]
                ):
                    raise InstallError(
                        "managed receipt pointer changed concurrently before pending probe"
                    )
                receipt_pointer.unlink()
                receipt_pointer_hidden = True

            discovery = _run_host(runner, ["grok", "inspect", "--json"], commands)
            discovery_body = str(getattr(discovery, "stdout", "") or "")
            try:
                json.loads(discovery_body)
            except json.JSONDecodeError as exc:
                raise InstallError("fresh discovery probe emitted malformed JSON") from exc
            if "oh-my-grok" not in discovery_body and "omg-" not in discovery_body:
                raise InstallError("fresh discovery probe did not observe OMG surfaces")

            env = dict(os.environ)
            env.update(
                {
                    "HOME": str(home_path),
                    "GROK_HOME": str(grok_path),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "OMG_INSTALL_MODE": mode,
                    "OMG_EXPECTED_INSTALL_DIGEST": str(identity["digest"]),
                    "OMG_EXPECTED_INSTALL_STAGE": str(stage),
                }
            )
            probe = dict(doctor(stage, env))
            status_value = classify_doctor_probe(mode, probe)
            commands.append(_probe_record(probe))
            owned_inventory.extend(
                [
                    {"path": str(candidate_plugin_path), "kind": "host_plugin", "identity": str(identity["digest"])},
                    {"path": str(hook_json), "kind": "global_hook", "identity": _sha256_file(hook_json)},
                    {"path": str(hook_py), "kind": "global_hook", "identity": _sha256_file(hook_py)},
                    {
                        "path": str(rules),
                        "kind": "global_guidance",
                        # The owned unit is only the OMG marker block.  Foreign/user
                        # text outside it may change and must survive uninstall.
                        "identity": _sha256_bytes(
                            render_managed_block(str(identity["version"])).encode("utf-8")
                        ),
                    },
                ]
            )
            material = _receipt_material(
                transaction_id=transaction_id,
                status=status_value,
                mode=mode,
                source=identity,
                stage=stage,
                plugin_path=candidate_plugin_path,
                asset=asset_evidence,
                source_uri=source_uri,
                source_tag=source_tag,
                commands=commands,
                owned_inventory=owned_inventory,
            )
            receipt_path, receipt = _write_install_receipt(receipts, material)
            installed_receipt_path = receipt_path
            _publish_symlink_no_clobber(str(receipt_path), receipt_pointer)

            # The first strict probe validates the candidate while the transaction
            # is explicitly marked pending via OMG_EXPECTED_INSTALL_*.  Repeat it
            # after publishing the immutable receipt pointer so success also proves
            # the normal, environment-free receipt/readback path.  A failure here
            # still enters the same all-or-prior rollback below.
            final_env = dict(env)
            final_env.pop("OMG_EXPECTED_INSTALL_DIGEST", None)
            final_env.pop("OMG_EXPECTED_INSTALL_STAGE", None)
            final_probe = dict(doctor(stage, final_env))
            classify_doctor_probe(mode, final_probe)
            return {
                "ok": True,
                "status": status_value,
                "stage_path": str(stage),
                "receipt_path": str(receipt_path),
                "receipt_hash": receipt["receipt_hash"],
                "package_digest": identity["digest"],
            }
        except Exception as exc:
            rollback_errors: list[str] = []
            try:
                _restore_symlink(cli_snapshot, expected_current=cli_target if _lexists(cli_pointer) else None)
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_errors.append(_redact_detail(rollback_exc))
            try:
                _restore_symlink(current_snapshot, expected_current=current_target if _lexists(current) else None)
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_errors.append(_redact_detail(rollback_exc))
            try:
                if (
                    receipt_pointer_hidden
                    and installed_receipt_path is None
                    and _lexists(receipt_pointer)
                ):
                    raise InstallError(
                        "managed receipt pointer changed concurrently during rollback"
                    )
                _restore_symlink(
                    receipt_snapshot,
                    expected_current=(
                        str(installed_receipt_path)
                        if installed_receipt_path is not None and _lexists(receipt_pointer)
                        else None
                    ),
                )
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_errors.append(_redact_detail(rollback_exc))
            for snapshot in reversed(global_snapshots):
                try:
                    _restore_file(snapshot)
                except Exception as rollback_exc:  # noqa: BLE001
                    rollback_errors.append(_redact_detail(rollback_exc))
            if plugin_mutated:
                try:
                    _run_host(
                        runner,
                        ["grok", "plugin", "uninstall", PLUGIN_NAME, "--confirm"],
                        commands,
                        required=False,
                    )
                    if prior_restore_path is not None and prior_plugin_identity is not None:
                        restored = _run_host(
                            runner,
                            ["grok", "plugin", "install", str(prior_restore_path), "--trust"],
                            commands,
                            required=False,
                        )
                        if int(getattr(restored, "returncode", 1)) != 0:
                            raise InstallError("prior plugin reinstall failed")
                        enabled = _run_host(
                            runner,
                            ["grok", "plugin", "enable", PLUGIN_NAME],
                            commands,
                            required=False,
                        )
                        if int(getattr(enabled, "returncode", 1)) != 0:
                            raise InstallError("prior plugin re-enable failed")
                        restored_rows = _read_plugin_inventory(runner, commands)
                        if len(restored_rows) != 1:
                            raise InstallError("prior plugin restore readback is absent")
                        restored_plugin_path, restored_identity = _resolve_entry_identity(
                            restored_rows[0],
                            allow_source_fallback=False,
                        )
                        _verify_host_plugin_path(
                            restored_plugin_path,
                            stage=prior_restore_path,
                            grok_home=grok_path,
                        )
                        if (
                            restored_identity["version"] != prior_plugin_identity["version"]
                            or restored_identity["digest"] != prior_plugin_identity["digest"]
                            or restored_identity["inventory"]
                            != prior_plugin_identity["inventory"]
                        ):
                            raise InstallError("prior plugin restore readback differs")
                        if not _plugin_entry_is_enabled(
                            restored_rows[0],
                            grok_home=grok_path,
                        ):
                            raise InstallError(
                                "prior plugin restore readback reports disabled"
                            )
                except Exception as rollback_exc:  # noqa: BLE001
                    rollback_errors.append(_redact_detail(rollback_exc))
            rollback_material = _receipt_material(
                # Receipts are immutable.  If the post-publication strict doctor
                # failed, retain that transient receipt for audit and write a
                # distinct rollback terminal record instead of attempting overwrite.
                transaction_id=(uuid.uuid4().hex if installed_receipt_path is not None else transaction_id),
                status="rolled_back",
                mode=mode,
                source=identity,
                stage=stage,
                plugin_path=None,
                asset=asset_evidence,
                source_uri=source_uri,
                source_tag=source_tag,
                commands=commands,
                owned_inventory=[owned_inventory[0]],
                error=f"{exc}; rollback={'ok' if not rollback_errors else '|'.join(rollback_errors)}",
            )
            try:
                _write_install_receipt(receipts, rollback_material)
            except Exception as receipt_exc:  # noqa: BLE001
                rollback_errors.append(f"rollback receipt: {_redact_detail(receipt_exc)}")
            detail = _redact_detail(exc)
            if rollback_errors:
                detail += "; rollback incomplete: " + " | ".join(rollback_errors)
            raise InstallError(detail) from exc


def _install_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m omg_cli.setup_cmd")
    sub = parser.add_subparsers(dest="command")
    release = sub.add_parser("install-release")
    release.add_argument("--source-root", required=True, type=Path)
    release.add_argument("--asset", required=True, type=Path)
    release.add_argument("--checksums", type=Path)
    release.add_argument("--asset-sha256")
    release.add_argument("--source-uri")
    release.add_argument("--source-tag")
    source = sub.add_parser("install-source")
    source.add_argument("--source-root", required=True, type=Path)
    verify = sub.add_parser("verify-release")
    verify.add_argument("--asset", required=True, type=Path)
    verify.add_argument("--checksums", type=Path)
    verify.add_argument("--asset-sha256")
    args = parser.parse_args(argv)
    if args.command is None:
        return run_setup()
    try:
        if args.command == "verify-release":
            result = verify_release_archive(
                args.asset,
                args.checksums,
                expected_sha256=args.asset_sha256,
            )
        elif args.command == "install-source":
            result = install_package(args.source_root, mode="development")
        else:
            result = install_package(
                args.source_root,
                mode="release",
                asset=args.asset,
                checksums=args.checksums,
                expected_asset_sha256=args.asset_sha256,
                source_uri=args.source_uri,
                source_tag=args.source_tag,
            )
    except InstallError as exc:
        print(f"install failed: {_redact_detail(exc)}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    return _install_cli(list(argv) if argv is not None else sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
