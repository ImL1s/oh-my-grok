"""Deterministic release packaging for OMG shipping identity (#26).

Build once from :data:`omg_cli.setup_cmd.SHIPPING_ROOTS` / package identity.
Produces::

    oh-my-grok-{semver}.tar.gz
    SHA256SUMS   # exact: ``{sha256}  {archive_name}\\n``

Does **not** publish, tag, or talk to GitHub. Publish jobs must upload these
exact bytes without rebuilding.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path
from typing import Any

from omg_cli.setup_cmd import (
    InstallError,
    _iter_shipping_files,
    compute_package_identity,
    plugin_root,
    verify_release_archive,
)


class PackageReleaseError(RuntimeError):
    """Hard packaging failure (no partial upload)."""


def _sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def archive_name_for_version(version: str) -> str:
    return f"oh-my-grok-{version}.tar.gz"


def checksum_bytes_for(payload_sha256: str, payload_name: str) -> bytes:
    """Canonical SHA256SUMS line (release_transaction exact form)."""
    return f"{payload_sha256}  {payload_name}\n".encode("utf-8")


def build_release_archive_bytes(
    source_root: Path | str,
    *,
    fixed_mtime: int = 0,
) -> tuple[bytes, dict[str, Any]]:
    """Return gzip-compressed tar bytes + package identity for *source_root*."""
    root = Path(source_root).resolve()
    try:
        identity = compute_package_identity(root)
    except InstallError as exc:
        raise PackageReleaseError(str(exc)) from exc
    version = str(identity["version"])
    prefix = f"oh-my-grok-{version}"
    files = list(_iter_shipping_files(root))

    # Tar payload (uncompressed) then gzip with mtime=0 for reproducibility.
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        # Directory entry for the package root.
        dir_info = tarfile.TarInfo(name=f"{prefix}/")
        dir_info.type = tarfile.DIRTYPE
        dir_info.mode = 0o755
        dir_info.mtime = int(fixed_mtime)
        dir_info.uid = 0
        dir_info.gid = 0
        dir_info.uname = ""
        dir_info.gname = ""
        tar.addfile(dir_info)

        for relative, path in files:
            body = path.read_bytes()
            info = tarfile.TarInfo(name=f"{prefix}/{relative}")
            info.size = len(body)
            info.mtime = int(fixed_mtime)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            # Match package identity mode contract for executables.
            executable = bool(path.stat().st_mode & 0o111)
            info.mode = 0o555 if executable else 0o444
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(body))

    raw_tar = tar_buf.getvalue()
    out = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", fileobj=out, mtime=int(fixed_mtime), compresslevel=9
    ) as gz:
        gz.write(raw_tar)
    return out.getvalue(), identity


def write_release_bundle(
    source_root: Path | str,
    output_dir: Path | str,
    *,
    fixed_mtime: int = 0,
) -> dict[str, Any]:
    """Write archive + SHA256SUMS into *output_dir*; return metadata."""
    root = Path(source_root).resolve()
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    archive_bytes, identity = build_release_archive_bytes(
        root, fixed_mtime=fixed_mtime
    )
    version = str(identity["version"])
    name = archive_name_for_version(version)
    payload_sha = _sha256_bytes(archive_bytes)
    sums = checksum_bytes_for(payload_sha, name)

    archive_path = out_dir / name
    sums_path = out_dir / "SHA256SUMS"
    # Atomic-ish write: temp then replace
    tmp_a = out_dir / f".{name}.tmp"
    tmp_s = out_dir / ".SHA256SUMS.tmp"
    try:
        tmp_a.write_bytes(archive_bytes)
        tmp_s.write_bytes(sums)
        os.replace(tmp_a, archive_path)
        os.replace(tmp_s, sums_path)
    finally:
        for p in (tmp_a, tmp_s):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

    try:
        verified = verify_release_archive(archive_path, sums_path)
    except InstallError as exc:
        raise PackageReleaseError(f"self-verify failed: {exc}") from exc

    meta = {
        "ok": True,
        "version": version,
        "archive_name": name,
        "archive_path": str(archive_path),
        "archive_sha256": payload_sha,
        "archive_byte_length": len(archive_bytes),
        "checksums_path": str(sums_path),
        "checksums_sha256": _sha256_bytes(sums),
        "checksums_byte_length": len(sums),
        "package_identity_digest": identity["digest"],
        "file_count": len(identity["inventory"]),
        "public_upload_order": [name, "SHA256SUMS"],
        "self_verified": verified,
    }
    meta_path = out_dir / "package-meta.json"
    meta_path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    meta["meta_path"] = str(meta_path)
    return meta


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Build deterministic oh-my-grok release archive + SHA256SUMS (#26)"
    )
    p.add_argument(
        "--root",
        type=Path,
        default=None,
        help="source checkout root (default: plugin root)",
    )
    p.add_argument(
        "--out",
        type=Path,
        required=True,
        help="output directory for archive + SHA256SUMS",
    )
    p.add_argument(
        "--mtime",
        type=int,
        default=0,
        help="fixed mtime for tar/gzip members (default: 0 / SOURCE_DATE_EPOCH)",
    )
    args = p.parse_args(argv)
    root = args.root if args.root is not None else plugin_root()
    mtime = args.mtime
    if mtime == 0 and os.environ.get("SOURCE_DATE_EPOCH"):
        try:
            mtime = int(os.environ["SOURCE_DATE_EPOCH"])
        except ValueError:
            mtime = 0
    try:
        meta = write_release_bundle(root, args.out, fixed_mtime=mtime)
    except PackageReleaseError as exc:
        print(f"package_release: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(meta, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
