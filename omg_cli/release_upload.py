"""Identity-safe GitHub release asset upload planning (#169 PR1).

Pure decision helpers only — no ``gh`` / network. Callers supply local
digests and any observed remote digest/length. Blind ``--clobber`` is never
a planned action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

UploadPlan = Literal["upload", "skip_identical", "refuse_mismatch"]


@dataclass(frozen=True)
class LocalAssetIdentity:
    """Exact bytes the publisher intends to publish."""

    name: str
    sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        if not self.name or "/" in self.name or self.name in {".", ".."}:
            raise ValueError(f"unsafe asset name: {self.name!r}")
        if len(self.sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.sha256
        ):
            raise ValueError("sha256 must be lowercase 64-hex")
        if not isinstance(self.byte_length, int) or isinstance(
            self.byte_length, bool
        ):
            raise ValueError("byte_length must be a non-bool int")
        if self.byte_length < 0:
            raise ValueError("byte_length must be >= 0")


@dataclass(frozen=True)
class RemoteAssetIdentity:
    """Observed remote asset identity (digest optional until readback)."""

    name: str
    byte_length: int | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("remote name required")
        if self.byte_length is not None:
            if not isinstance(self.byte_length, int) or isinstance(
                self.byte_length, bool
            ):
                raise ValueError("byte_length must be a non-bool int")
            if self.byte_length < 0:
                raise ValueError("byte_length must be >= 0")
        if self.sha256 is not None:
            if len(self.sha256) != 64 or any(
                c not in "0123456789abcdef" for c in self.sha256
            ):
                raise ValueError("sha256 must be lowercase 64-hex")


def plan_release_asset_upload(
    local: LocalAssetIdentity,
    remote: RemoteAssetIdentity | None,
) -> UploadPlan:
    """Decide whether to upload, skip, or refuse for one release asset.

    - Missing remote → ``upload`` (caller must upload **without** ``--clobber``).
    - Remote present with matching digest (and length when known) →
      ``skip_identical``.
    - Remote present with conflicting length or digest → ``refuse_mismatch``
      (never clobber).
    - Remote present but digest unknown: length mismatch refuses; length match
      alone is insufficient — caller must supply digest after readback before
      skip; until then this returns ``refuse_mismatch`` so ambiguous retries
      cannot overwrite.
    """

    if remote is None:
        return "upload"
    if remote.name != local.name:
        raise ValueError(
            f"name mismatch: local={local.name!r} remote={remote.name!r}"
        )
    if remote.byte_length is not None and remote.byte_length != local.byte_length:
        return "refuse_mismatch"
    if remote.sha256 is None:
        # Size match without digest is not identity — refuse blind overwrite.
        return "refuse_mismatch"
    if remote.sha256 != local.sha256:
        return "refuse_mismatch"
    if remote.byte_length is not None and remote.byte_length != local.byte_length:
        return "refuse_mismatch"
    return "skip_identical"


__all__ = [
    "LocalAssetIdentity",
    "RemoteAssetIdentity",
    "UploadPlan",
    "plan_release_asset_upload",
]
