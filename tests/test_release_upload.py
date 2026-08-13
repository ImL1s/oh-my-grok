"""Hermetic tests for identity-safe release upload planning (#169 PR1)."""

from __future__ import annotations

import pytest

from omg_cli.release_upload import (
    LocalAssetIdentity,
    RemoteAssetIdentity,
    plan_release_asset_upload,
)


def _local(**kwargs: object) -> LocalAssetIdentity:
    base = {
        "name": "oh-my-grok-1.2.3.tar.gz",
        "sha256": "a" * 64,
        "byte_length": 100,
    }
    base.update(kwargs)
    return LocalAssetIdentity(**base)  # type: ignore[arg-type]


def test_missing_remote_plans_upload() -> None:
    assert plan_release_asset_upload(_local(), None) == "upload"


def test_identical_digest_and_length_skips() -> None:
    local = _local()
    remote = RemoteAssetIdentity(
        name=local.name, byte_length=local.byte_length, sha256=local.sha256
    )
    assert plan_release_asset_upload(local, remote) == "skip_identical"


def test_digest_mismatch_refuses() -> None:
    local = _local()
    remote = RemoteAssetIdentity(
        name=local.name, byte_length=local.byte_length, sha256="b" * 64
    )
    assert plan_release_asset_upload(local, remote) == "refuse_mismatch"


def test_length_mismatch_refuses() -> None:
    local = _local()
    remote = RemoteAssetIdentity(
        name=local.name, byte_length=local.byte_length + 1, sha256=local.sha256
    )
    assert plan_release_asset_upload(local, remote) == "refuse_mismatch"


def test_size_only_without_digest_refuses_ambiguous_retry() -> None:
    local = _local()
    remote = RemoteAssetIdentity(name=local.name, byte_length=local.byte_length)
    assert plan_release_asset_upload(local, remote) == "refuse_mismatch"


def test_rejects_bool_lengths_and_bad_digest() -> None:
    with pytest.raises(ValueError):
        LocalAssetIdentity(name="a.tar.gz", sha256="a" * 64, byte_length=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        LocalAssetIdentity(name="a.tar.gz", sha256="A" * 64, byte_length=1)
    with pytest.raises(ValueError):
        LocalAssetIdentity(name="../x", sha256="a" * 64, byte_length=1)
