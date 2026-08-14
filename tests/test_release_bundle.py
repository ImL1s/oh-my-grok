"""Hermetic tests for canonical release-bundle-manifest producer (#169 PR2)."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest

from omg_cli.contracts.release_transaction import (
    expected_bundle_manifest_relative_path,
    validate_release_bundle_manifest,
    verify_release_bundle_files,
)
from omg_cli.contracts.writer_chain import sha256_hex
from omg_cli.release_bundle import (
    ReleaseBundleError,
    attestation_argv,
    build_release_bundle_manifest,
    local_asset_identities_from_files,
    make_build_receipt,
    produce_release_bundle_from_files,
)
from omg_cli.release_upload import LocalAssetIdentity


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "release"
COMMIT = "a" * 40
TREE = "b" * 40


def _receipt(*, archive_rel: str, sums_rel: str, archive_sha: str) -> dict:
    return make_build_receipt(
        argv=attestation_argv(
            archive_relative=archive_rel,
            checksums_relative=sums_rel,
            archive_sha256=archive_sha,
        ),
        cwd_realpath_hash="5" * 64,
        toolchain=[
            {
                "name": "python3",
                "version": "Python 3.11.0",
                "binary_sha256": "6" * 64,
            }
        ],
        source_date_epoch=1700000000,
    )


def test_producer_matches_schema_and_file_set(tmp_path: Path) -> None:
    archive = tmp_path / "oh-my-grok-1.2.3.tar.gz"
    sums = tmp_path / "SHA256SUMS"
    archive.write_bytes((FIXTURES / "payload.bytes").read_bytes())
    digest = sha256_hex(archive.read_bytes())
    sums.write_bytes(f"{digest}  {archive.name}\n".encode("utf-8"))
    archive_id, sums_id, archive_bytes, sums_bytes = local_asset_identities_from_files(
        archive, sums
    )
    relative = expected_bundle_manifest_relative_path("OMG", "fixture-run")
    bundle_dir = str(PurePosixPath(relative).parent / "release-bundle")
    receipt = _receipt(
        archive_rel=f"{bundle_dir}/{archive_id.name}",
        sums_rel=f"{bundle_dir}/SHA256SUMS",
        archive_sha=archive_id.sha256,
    )
    manifest = build_release_bundle_manifest(
        repository_id="OMG",
        run_id="fixture-run",
        candidate_commit=COMMIT,
        candidate_tree=TREE,
        semver="1.2.3",
        archive=archive_id,
        checksums=sums_id,
        checksum_utf8=sums_bytes.decode("utf-8"),
        build_receipt=receipt,
    )
    validate_release_bundle_manifest(manifest, manifest_relative_path=relative)
    layout_root = tmp_path / "repo"
    from omg_cli.release_bundle import write_release_bundle_layout

    path = write_release_bundle_layout(
        layout_root,
        manifest,
        archive_bytes=archive_bytes,
        checksum_bytes=sums_bytes,
    )
    assert path.is_file()
    verify_release_bundle_files(
        layout_root, manifest, manifest_relative_path=relative
    )


def test_checksum_drift_refuses_before_manifest(tmp_path: Path) -> None:
    archive = tmp_path / "oh-my-grok-1.2.3.tar.gz"
    sums = tmp_path / "SHA256SUMS"
    archive.write_bytes(b"not the fixture payload")
    sums.write_bytes((FIXTURES / "SHA256SUMS").read_bytes())
    with pytest.raises(ReleaseBundleError, match="SHA256SUMS"):
        local_asset_identities_from_files(archive, sums)


def test_wrong_archive_name_refuses() -> None:
    receipt = _receipt(
        archive_rel="x/oh-my-grok-1.2.3.tar.gz",
        sums_rel="x/SHA256SUMS",
        archive_sha="b" * 64,
    )
    with pytest.raises(ReleaseBundleError, match="archive name"):
        build_release_bundle_manifest(
            repository_id="OMG",
            run_id="fixture-run",
            candidate_commit=COMMIT,
            candidate_tree=TREE,
            semver="1.2.3",
            archive=LocalAssetIdentity(
                name="wrong.tar.gz", sha256="b" * 64, byte_length=1
            ),
            checksums=LocalAssetIdentity(
                name="SHA256SUMS", sha256="c" * 64, byte_length=90
            ),
            checksum_utf8="b" * 64 + "  oh-my-grok-1.2.3.tar.gz\n",
            build_receipt=receipt,
        )


def test_produce_from_files_writes_canonical_layout(tmp_path: Path) -> None:
    src = tmp_path / "dist"
    src.mkdir()
    archive = src / "oh-my-grok-1.2.3.tar.gz"
    sums = src / "SHA256SUMS"
    archive.write_bytes((FIXTURES / "payload.bytes").read_bytes())
    digest = sha256_hex(archive.read_bytes())
    sums.write_bytes(f"{digest}  {archive.name}\n".encode("utf-8"))
    relative = expected_bundle_manifest_relative_path("OMG", "ci-run")
    bundle_dir = str(PurePosixPath(relative).parent / "release-bundle")
    archive_sha = sha256_hex(archive.read_bytes())
    receipt = _receipt(
        archive_rel=f"{bundle_dir}/{archive.name}",
        sums_rel=f"{bundle_dir}/SHA256SUMS",
        archive_sha=archive_sha,
    )
    result = produce_release_bundle_from_files(
        tmp_path / "repo",
        run_id="ci-run",
        candidate_commit=COMMIT,
        candidate_tree=TREE,
        semver="1.2.3",
        archive=archive,
        checksums=sums,
        build_receipt=receipt,
        write=True,
    )
    assert result["ok"] is True
    written = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert written["run_id"] == "ci-run"
    assert written["release_asset_root"] == result["manifest"]["release_asset_root"]


def test_parity_release_bundle_cli_writes_layout(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    src = tmp_path / "dist"
    src.mkdir()
    archive = src / "oh-my-grok-1.2.3.tar.gz"
    sums = src / "SHA256SUMS"
    archive.write_bytes((FIXTURES / "payload.bytes").read_bytes())
    digest = sha256_hex(archive.read_bytes())
    sums.write_bytes(f"{digest}  {archive.name}\n".encode("utf-8"))
    relative = expected_bundle_manifest_relative_path("OMG", "cli-run")
    bundle_dir = str(PurePosixPath(relative).parent / "release-bundle")
    receipt = _receipt(
        archive_rel=f"{bundle_dir}/{archive.name}",
        sums_rel=f"{bundle_dir}/SHA256SUMS",
        archive_sha=digest,
    )
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    result = subprocess.run(
        [
            sys.executable,
            str(root / "bin" / "omg"),
            "parity",
            "release-bundle",
            "--root",
            str(repo),
            "--run-id",
            "cli-run",
            "--archive",
            str(archive),
            "--checksums",
            str(sums),
            "--candidate-commit",
            COMMIT,
            "--candidate-tree",
            TREE,
            "--semver",
            "1.2.3",
            "--receipt",
            str(receipt_path),
            "--write",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["manifest_relative_path"].endswith("release-bundle-manifest.json")


def test_live_receipt_flag_is_wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.release_bundle import produce_release_bundle_from_files

    src = tmp_path / "dist"
    src.mkdir()
    archive = src / "oh-my-grok-1.2.3.tar.gz"
    sums = src / "SHA256SUMS"
    archive.write_bytes((FIXTURES / "payload.bytes").read_bytes())
    digest = sha256_hex(archive.read_bytes())
    sums.write_bytes(f"{digest}  {archive.name}\n".encode("utf-8"))
    relative = expected_bundle_manifest_relative_path("OMG", "live-run")
    bundle_dir = str(PurePosixPath(relative).parent / "release-bundle")
    receipt = _receipt(
        archive_rel=f"{bundle_dir}/{archive.name}",
        sums_rel=f"{bundle_dir}/SHA256SUMS",
        archive_sha=digest,
    )

    def fake_live_receipt(*_args: object, **_kwargs: object) -> dict:
        return receipt

    monkeypatch.setattr(
        "omg_cli.release_bundle.live_attestation_receipt", fake_live_receipt
    )
    result = produce_release_bundle_from_files(
        tmp_path / "repo",
        run_id="live-run",
        candidate_commit=COMMIT,
        candidate_tree=TREE,
        semver="1.2.3",
        archive=archive,
        checksums=sums,
        live_receipt=True,
        write=True,
    )
    assert result["ok"] is True
    assert result["manifest"]["run_id"] == "live-run"
