from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BIN_OMG = ROOT / "bin" / "omg"
FIXTURES = ROOT / "tests" / "fixtures" / "release"


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return subprocess.run(
        [sys.executable, str(BIN_OMG), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _release_bundle(root: Path) -> Path:
    owner = root / ".omg" / "artifacts" / "dual-parity" / "fixture-run" / "OMG-W6"
    bundle = owner / "release-bundle"
    bundle.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "payload.bytes", bundle / "oh-my-grok-1.2.3.tar.gz")
    shutil.copyfile(FIXTURES / "SHA256SUMS", bundle / "SHA256SUMS")
    manifest = owner / "release-bundle-manifest.json"
    shutil.copyfile(FIXTURES / "valid-omg-release-bundle-manifest.json", manifest)
    return manifest


def test_release_readback_accepts_exact_prebuilt_file_set(tmp_path: Path) -> None:
    manifest = _release_bundle(tmp_path)
    result = _run(
        "parity", "release-readback", "--manifest", str(manifest), cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["verified"] is True
    assert output["semver"] == "1.2.3"
    assert output["public_upload_order"] == [
        "oh-my-grok-1.2.3.tar.gz",
        "SHA256SUMS",
    ]


def test_release_readback_rejects_extra_or_drifted_bytes(tmp_path: Path) -> None:
    manifest = _release_bundle(tmp_path)
    bundle = manifest.parent / "release-bundle"
    (bundle / "unexpected.txt").write_text("extra\n", encoding="utf-8")
    extra = _run(
        "parity", "release-readback", "--manifest", str(manifest), cwd=tmp_path
    )
    assert extra.returncode == 1
    assert "missing/extra/renamed" in extra.stderr

    (bundle / "unexpected.txt").unlink()
    (bundle / "oh-my-grok-1.2.3.tar.gz").write_bytes(b"drift")
    drift = _run(
        "parity", "release-readback", "--manifest", str(manifest), cwd=tmp_path
    )
    assert drift.returncode == 1
    assert "byte drift" in drift.stderr


def test_release_readback_rejects_missing_or_renamed_assets(tmp_path: Path) -> None:
    manifest = _release_bundle(tmp_path)
    bundle = manifest.parent / "release-bundle"
    checksum = bundle / "SHA256SUMS"
    checksum.rename(bundle / "CHECKSUMS.txt")

    result = _run(
        "parity", "release-readback", "--manifest", str(manifest), cwd=tmp_path
    )
    assert result.returncode == 1
    assert "missing/extra/renamed" in result.stderr
