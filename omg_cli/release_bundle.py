"""Canonical producer for ``release-bundle-manifest.json`` (#169 PR2).

Pure construction + local file copy.  No GitHub, no run-manifest closer.
Callers supply candidate commit/tree and a build receipt; this module never
hand-authors checksum bytes or asset rows.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from omg_cli.contracts.release_transaction import (
    expected_bundle_manifest_relative_path,
    validate_build_receipt,
    validate_release_bundle_manifest,
    verify_release_bundle_files,
)
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_git_oid,
    require_nonempty_string,
    require_safe_id,
    require_sha256,
)
from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
from omg_cli.release_upload import LocalAssetIdentity


def archive_name_for_version(version: str) -> str:
    return f"oh-my-grok-{version}.tar.gz"


def checksum_bytes_for(payload_sha256: str, payload_name: str) -> bytes:
    return f"{payload_sha256}  {payload_name}\n".encode("utf-8")


class ReleaseBundleError(ValueError):
    """Typed fail-closed bundle construction error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def attestation_argv(
    *,
    archive_relative: str,
    checksums_relative: str,
    archive_sha256: str,
) -> list[str]:
    """Exact OMG dual-parity attestation argv (locked by run-manifest)."""

    require_sha256(archive_sha256, label="archive_sha256")
    return [
        "python3",
        "scripts/release_attest.py",
        "--asset",
        archive_relative,
        "--checksums",
        checksums_relative,
        "--asset-sha256",
        archive_sha256,
    ]


def make_build_receipt(
    *,
    argv: list[str],
    cwd_realpath_hash: str,
    toolchain: list[Mapping[str, str]],
    source_date_epoch: int,
    locale: str = "C.UTF-8",
    timezone: str = "UTC",
    umask: str = "022",
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Construct a receipt that passes ``validate_build_receipt``."""

    env = dict(
        environment
        or {
            "SOURCE_DATE_EPOCH": str(source_date_epoch),
            "LC_ALL": locale,
            "TZ": timezone,
        }
    )
    allowlist = list(env)
    receipt: dict[str, Any] = {
        "argv": list(argv),
        "cwd_realpath_hash": cwd_realpath_hash,
        "toolchain": [dict(row) for row in toolchain],
        "environment_allowlist": allowlist,
        "environment_value_hashes": {
            name: sha256_hex(value) for name, value in env.items()
        },
        "SOURCE_DATE_EPOCH": source_date_epoch,
        "locale": locale,
        "timezone": timezone,
        "umask": umask,
    }
    receipt["receipt_hash"] = sha256_hex(
        canonical_json_bytes({key: receipt[key] for key in receipt})
    )
    return validate_build_receipt(receipt)


def local_asset_identities_from_files(
    archive: Path, checksums: Path
) -> tuple[LocalAssetIdentity, LocalAssetIdentity, bytes, bytes]:
    """Hash local archive + SHA256SUMS without rebuilding."""

    if not archive.is_file() or archive.is_symlink():
        raise ReleaseBundleError(
            "E_RELEASE_BUNDLE_MISSING", f"archive missing or unsafe: {archive}"
        )
    if not checksums.is_file() or checksums.is_symlink():
        raise ReleaseBundleError(
            "E_RELEASE_BUNDLE_MISSING", f"SHA256SUMS missing or unsafe: {checksums}"
        )
    archive_bytes = archive.read_bytes()
    sums_bytes = checksums.read_bytes()
    archive_sha = sha256_hex(archive_bytes)
    expected_sums = checksum_bytes_for(archive_sha, archive.name)
    if sums_bytes != expected_sums:
        raise ReleaseBundleError(
            "E_RELEASE_BUNDLE_CHECKSUM_DRIFT",
            "SHA256SUMS bytes are not the exact archive digest line",
        )
    archive_id = LocalAssetIdentity(
        name=archive.name, sha256=archive_sha, byte_length=len(archive_bytes)
    )
    sums_id = LocalAssetIdentity(
        name="SHA256SUMS", sha256=sha256_hex(sums_bytes), byte_length=len(sums_bytes)
    )
    return archive_id, sums_id, archive_bytes, sums_bytes


def build_release_bundle_manifest(
    *,
    repository_id: str,
    run_id: str,
    candidate_commit: str,
    candidate_tree: str,
    semver: str,
    archive: LocalAssetIdentity,
    checksums: LocalAssetIdentity,
    checksum_utf8: str,
    build_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a validated ``release_bundle_manifest/1`` object."""

    if repository_id != "OMG":
        raise ReleaseBundleError(
            "E_RELEASE_BUNDLE_REPOSITORY",
            "this producer constructs OMG GitHub-channel bundles only",
        )
    require_safe_id(run_id, label="run_id")
    require_git_oid(candidate_commit, label="candidate_commit")
    require_git_oid(candidate_tree, label="candidate_tree")
    require_nonempty_string(semver, label="semver")
    expected_archive = archive_name_for_version(semver)
    if archive.name != expected_archive:
        raise ReleaseBundleError(
            "E_RELEASE_BUNDLE_NAME",
            f"archive name {archive.name!r} != {expected_archive!r}",
        )
    if checksums.name != "SHA256SUMS":
        raise ReleaseBundleError(
            "E_RELEASE_BUNDLE_NAME", "checksum asset must be named SHA256SUMS"
        )
    relative = expected_bundle_manifest_relative_path("OMG", run_id)
    bundle_directory = str(PurePosixPath(relative).parent / "release-bundle")
    assets = [
        {
            "name": archive.name,
            "relative_path": f"{bundle_directory}/{archive.name}",
            "byte_length": archive.byte_length,
            "sha256": archive.sha256,
            "media_type": "application/gzip",
        },
        {
            "name": "SHA256SUMS",
            "relative_path": f"{bundle_directory}/SHA256SUMS",
            "byte_length": checksums.byte_length,
            "sha256": checksums.sha256,
            "media_type": "text/plain",
        },
    ]
    manifest: dict[str, Any] = {
        "store_kind": "release_bundle_manifest",
        "schema_version": 1,
        "repository_id": "OMG",
        "run_id": run_id,
        "owner": "OMG-W6",
        "candidate_commit": candidate_commit,
        "candidate_tree": candidate_tree,
        "semver": semver,
        "bundle_directory": bundle_directory,
        "public_upload_order": [archive.name, "SHA256SUMS"],
        "assets": assets,
        "checksum": {
            "name": "SHA256SUMS",
            "payload_name": archive.name,
            "payload_sha256": archive.sha256,
            "bytes_utf8": checksum_utf8,
            "byte_length": checksums.byte_length,
            "sha256": checksums.sha256,
        },
        "build_receipt": dict(build_receipt),
        "registry_bindings": [],
        "release_asset_root": sha256_hex(canonical_json_bytes(assets)),
    }
    try:
        return validate_release_bundle_manifest(
            manifest, manifest_relative_path=relative
        )
    except ContractValidationError as exc:
        raise ReleaseBundleError("E_RELEASE_BUNDLE_INVALID", str(exc)) from exc


def _under_root(root: Path, relative: str) -> Path:
    return root.joinpath(*PurePosixPath(relative).parts)


def write_release_bundle_layout(
    root: Path | str,
    manifest: Mapping[str, Any],
    *,
    archive_bytes: bytes,
    checksum_bytes: bytes,
) -> Path:
    """Write canonical sibling ``release-bundle/`` + manifest under *root*."""

    root_path = Path(root).resolve()
    run_id = require_safe_id(manifest["run_id"], label="run_id")
    relative = expected_bundle_manifest_relative_path("OMG", run_id)
    bundle_dir = _under_root(root_path, str(manifest["bundle_directory"]))
    bundle_dir.mkdir(parents=True, exist_ok=True)
    archive_name = str(manifest["assets"][0]["name"])
    (bundle_dir / archive_name).write_bytes(archive_bytes)
    (bundle_dir / "SHA256SUMS").write_bytes(checksum_bytes)
    manifest_path = _under_root(root_path, relative)
    body = canonical_json_bytes(manifest)
    manifest_path.write_bytes(body)
    os.chmod(manifest_path, 0o600)
    verify_release_bundle_files(
        root_path, manifest, manifest_relative_path=relative
    )
    return manifest_path


def live_attestation_receipt(
    root: Path | str,
    *,
    candidate_commit: str,
    semver: str,
    bundle: Mapping[str, Any],
) -> dict[str, Any]:
    """Reconstruct the dual-parity live receipt (toolchain + git epoch)."""

    from omg_cli.contracts.run_manifest import _expected_current_build_receipt

    return _expected_current_build_receipt(
        Path(root).resolve(),
        repository_id="OMG",
        candidate_commit=candidate_commit,
        semver=semver,
        bundle=bundle,
    )


def produce_release_bundle_from_files(
    root: Path | str,
    *,
    run_id: str,
    candidate_commit: str,
    candidate_tree: str,
    semver: str,
    archive: Path,
    checksums: Path,
    build_receipt: Mapping[str, Any] | None = None,
    live_receipt: bool = False,
    write: bool = True,
) -> dict[str, Any]:
    """Hash local files, construct, optionally write the canonical layout."""

    archive_id, sums_id, archive_bytes, sums_bytes = local_asset_identities_from_files(
        Path(archive), Path(checksums)
    )
    checksum_utf8 = sums_bytes.decode("utf-8")
    relative = expected_bundle_manifest_relative_path("OMG", run_id)
    bundle_directory = str(PurePosixPath(relative).parent / "release-bundle")
    skeleton = {
        "assets": [
            {
                "name": archive_id.name,
                "relative_path": f"{bundle_directory}/{archive_id.name}",
                "byte_length": archive_id.byte_length,
                "sha256": archive_id.sha256,
                "media_type": "application/gzip",
            },
            {
                "name": "SHA256SUMS",
                "relative_path": f"{bundle_directory}/SHA256SUMS",
                "byte_length": sums_id.byte_length,
                "sha256": sums_id.sha256,
                "media_type": "text/plain",
            },
        ],
        "bundle_directory": bundle_directory,
    }
    if live_receipt:
        receipt = live_attestation_receipt(
            root,
            candidate_commit=candidate_commit,
            semver=semver,
            bundle=skeleton,
        )
    elif build_receipt is not None:
        receipt = validate_build_receipt(dict(build_receipt))
    else:
        raise ReleaseBundleError(
            "E_RELEASE_BUNDLE_RECEIPT",
            "pass build_receipt or live_receipt=True",
        )
    manifest = build_release_bundle_manifest(
        repository_id="OMG",
        run_id=run_id,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
        semver=semver,
        archive=archive_id,
        checksums=sums_id,
        checksum_utf8=checksum_utf8,
        build_receipt=receipt,
    )
    result: dict[str, Any] = {
        "ok": True,
        "manifest": manifest,
        "manifest_relative_path": relative,
        "manifest_sha256": sha256_hex(canonical_json_bytes(manifest)),
    }
    if write:
        path = write_release_bundle_layout(
            root,
            manifest,
            archive_bytes=archive_bytes,
            checksum_bytes=sums_bytes,
        )
        result["manifest_path"] = str(path)
    return result


__all__ = [
    "ReleaseBundleError",
    "attestation_argv",
    "build_release_bundle_manifest",
    "live_attestation_receipt",
    "local_asset_identities_from_files",
    "make_build_receipt",
    "produce_release_bundle_from_files",
    "write_release_bundle_layout",
]
