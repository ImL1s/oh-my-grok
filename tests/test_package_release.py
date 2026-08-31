"""#26 deterministic packager tests."""

from __future__ import annotations

import tarfile
from pathlib import Path

from omg_cli.package_release import (
    archive_name_for_version,
    build_release_archive_bytes,
    checksum_bytes_for,
    write_release_bundle,
)
from omg_cli.setup_cmd import compute_package_identity, verify_release_archive


ROOT = Path(__file__).resolve().parents[1]


def test_dual_build_byte_identity() -> None:
    a, id_a = build_release_archive_bytes(ROOT, fixed_mtime=0)
    b, id_b = build_release_archive_bytes(ROOT, fixed_mtime=0)
    assert a == b
    assert id_a["digest"] == id_b["digest"]
    assert id_a["version"] == id_b["version"]


def test_write_bundle_self_verifies(tmp_path: Path) -> None:
    meta = write_release_bundle(ROOT, tmp_path / "bundle", fixed_mtime=0)
    assert meta["ok"] is True
    name = meta["archive_name"]
    assert name == archive_name_for_version(meta["version"])
    archive = tmp_path / "bundle" / name
    sums = tmp_path / "bundle" / "SHA256SUMS"
    assert archive.is_file() and sums.is_file()
    verified = verify_release_archive(archive, sums)
    assert verified["asset_sha256"] == meta["archive_sha256"]
    # Exact checksum form
    expected = checksum_bytes_for(meta["archive_sha256"], name)
    assert sums.read_bytes() == expected


def test_archive_contains_shipping_prefix_and_plugin(tmp_path: Path) -> None:
    meta = write_release_bundle(ROOT, tmp_path / "b", fixed_mtime=0)
    archive = tmp_path / "b" / meta["archive_name"]
    prefix = f"oh-my-grok-{meta['version']}"
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert f"{prefix}/" in names or any(n.startswith(f"{prefix}/") for n in names)
    assert f"{prefix}/plugin.json" in names
    assert f"{prefix}/hooks.json" in names
    assert f"{prefix}/mcp_config.json" in names
    assert f"{prefix}/omg_cli/__init__.py" in names
    # No secrets / vcs noise
    joined = "\n".join(names)
    assert ".git/" not in joined
    assert "__pycache__" not in joined
    assert ".env" not in joined


def test_package_identity_digest_matches_source() -> None:
    _, identity = build_release_archive_bytes(ROOT, fixed_mtime=0)
    live = compute_package_identity(ROOT)
    assert identity["digest"] == live["digest"]


def test_second_write_is_byte_stable(tmp_path: Path) -> None:
    out = tmp_path / "out"
    m1 = write_release_bundle(ROOT, out, fixed_mtime=0)
    a1 = (out / m1["archive_name"]).read_bytes()
    s1 = (out / "SHA256SUMS").read_bytes()
    m2 = write_release_bundle(ROOT, out, fixed_mtime=0)
    a2 = (out / m2["archive_name"]).read_bytes()
    s2 = (out / "SHA256SUMS").read_bytes()
    assert a1 == a2 and s1 == s2
    assert m1["archive_sha256"] == m2["archive_sha256"]


def test_public_upload_order(tmp_path: Path) -> None:
    meta = write_release_bundle(ROOT, tmp_path / "out", fixed_mtime=0)
    assert meta["public_upload_order"] == [
        meta["archive_name"],
        "SHA256SUMS",
    ]
