#!/usr/bin/env python3
"""CLI: plan/upload GitHub release assets without blind --clobber (#169 PR1).

Usage (publish job)::

  python scripts/release_upload_assets.py \\
    --tag vX.Y.Z \\
    --asset dist/release-bundle/oh-my-grok-X.Y.Z.tar.gz \\
    --asset dist/release-bundle/SHA256SUMS

For each asset: if remote missing → ``gh release upload`` (no ``--clobber``);
if remote digest matches → skip; if mismatch → exit 2.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from omg_cli.release_upload import (
    LocalAssetIdentity,
    RemoteAssetIdentity,
    plan_release_asset_upload,
)


def _sha256_file(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def _remote_assets(tag: str) -> dict[str, dict[str, object]]:
    proc = subprocess.run(
        ["gh", "release", "view", tag, "--json", "assets"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # No release yet — caller creates it; treat as empty.
        return {}
    payload = json.loads(proc.stdout)
    out: dict[str, dict[str, object]] = {}
    for row in payload.get("assets") or []:
        name = str(row.get("name") or "")
        if not name:
            continue
        size = row.get("size")
        out[name] = {
            "name": name,
            "byte_length": int(size) if isinstance(size, int) else None,
        }
    return out


def _download_digest(tag: str, name: str) -> str:
    with tempfile.TemporaryDirectory() as td:
        dest_dir = Path(td)
        subprocess.run(
            ["gh", "release", "download", tag, "-p", name, "-D", str(dest_dir)],
            check=True,
        )
        path = dest_dir / name
        return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument(
        "--asset",
        action="append",
        dest="assets",
        required=True,
        help="local asset path (repeatable; upload order = argv order)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print plans only; never call gh upload",
    )
    args = parser.parse_args(argv)
    tag = str(args.tag)
    remote_index = _remote_assets(tag)

    for raw in args.assets:
        path = Path(raw)
        if not path.is_file():
            print(f"missing asset: {path}", file=sys.stderr)
            return 2
        digest, length = _sha256_file(path)
        local = LocalAssetIdentity(
            name=path.name, sha256=digest, byte_length=length
        )
        remote_row = remote_index.get(local.name)
        remote: RemoteAssetIdentity | None = None
        if remote_row is not None:
            # Always read back digest before skip/refuse (size alone is not identity).
            remote_digest = _download_digest(tag, local.name)
            remote = RemoteAssetIdentity(
                name=local.name,
                byte_length=remote_row.get("byte_length")  # type: ignore[arg-type]
                if isinstance(remote_row.get("byte_length"), int)
                else length,
                sha256=remote_digest,
            )
        plan = plan_release_asset_upload(local, remote)
        print(f"{local.name}: {plan} sha256={local.sha256} bytes={local.byte_length}")
        if plan == "skip_identical":
            continue
        if plan == "refuse_mismatch":
            print(
                f"refuse overwrite of {local.name}: remote identity differs",
                file=sys.stderr,
            )
            return 2
        if args.dry_run:
            continue
        # No --clobber: existing divergent assets must have refused above.
        subprocess.run(
            ["gh", "release", "upload", tag, str(path)],
            check=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
