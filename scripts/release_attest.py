#!/usr/bin/env python3
"""Verify an OMG release archive and report its exact package identity.

This is a read-only W1 harness.  It never publishes and never changes the live
CLI/plugin.  ``scripts/install.sh`` uses the same setup_cmd verification and
transaction primitives for both online and manual/offline installation.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omg_cli.setup_cmd import (  # noqa: E402
    InstallError,
    compute_package_identity,
    extract_release_archive,
    verify_release_archive,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", required=True, type=Path)
    parser.add_argument("--checksums", type=Path)
    parser.add_argument("--asset-sha256")
    args = parser.parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="omg-release-attest-"))
    try:
        evidence = verify_release_archive(
            args.asset,
            args.checksums,
            expected_sha256=args.asset_sha256,
        )
        package_root = extract_release_archive(args.asset, work / "unpack")
        identity = compute_package_identity(package_root)
        expected_name = f"oh-my-grok-{identity['version']}.tar.gz"
        if evidence["asset_name"] != expected_name:
            raise InstallError("archive filename/version differs from package identity")
        print(
            json.dumps(
                {
                    "verified": True,
                    "asset_name": evidence["asset_name"],
                    "asset_sha256": evidence["asset_sha256"],
                    "checksums_sha256": evidence["checksums_sha256"],
                    "package_version": identity["version"],
                    "package_digest": identity["digest"],
                    "inventory_count": len(identity["inventory"]),
                },
                sort_keys=True,
            )
        )
        return 0
    except InstallError as exc:
        print(f"release attestation failed: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
