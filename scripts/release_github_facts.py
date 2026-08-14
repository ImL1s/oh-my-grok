#!/usr/bin/env python3
"""GitHub Actions helpers for #169 PR2 (notes, tag identity, observe, protection).

Does not publish.  Observe/protection talk to git/gh/curl; facts JSON never
contains tokens, HOME, or account names.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omg_cli.release_bundle import local_asset_identities_from_files  # noqa: E402
from omg_cli.release_evidence import (  # noqa: E402
    ReleaseEvidenceError,
    assemble_github_observe_facts,
    classify_protection_readback,
    extract_changelog_section,
    github_cli_http_status,
    require_annotated_tag,
    select_expected_remotes,
)
from omg_cli.release_upload import RemoteAssetIdentity  # noqa: E402


def _git(*argv: str) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"git {' '.join(argv)} failed")
    return result.stdout.strip()


def cmd_notes(args: argparse.Namespace) -> int:
    text = Path(args.changelog).read_text(encoding="utf-8")
    try:
        section = extract_changelog_section(text, args.version)
    except ReleaseEvidenceError as exc:
        print(exc, file=sys.stderr)
        return 2
    Path(args.output).write_text(section, encoding="utf-8")
    print(json.dumps({"ok": True, "bytes": len(section.encode("utf-8"))}))
    return 0


def cmd_tag_identity(args: argparse.Namespace) -> int:
    object_type = _git("cat-file", "-t", args.tag)
    peeled = _git("rev-parse", f"{args.tag}^{{commit}}")
    try:
        require_annotated_tag(
            tag=args.tag,
            object_type=object_type,
            peeled_commit=peeled,
            expected_commit=args.expected_commit,
        )
    except ReleaseEvidenceError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "tag": args.tag,
                "object_type": object_type,
                "peeled_commit": peeled,
            }
        )
    )
    return 0


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _gh_json(argv: list[str]) -> dict:
    env = os.environ.copy()
    result = subprocess.run(
        ["gh", *argv],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "gh failed")
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise SystemExit("gh JSON was not an object")
    return payload


def _observed_main_oid() -> str:
    for ref in ("refs/remotes/origin/main", "origin/main", "refs/heads/main"):
        result = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        oid = (result.stdout or "").strip()
        if result.returncode == 0 and oid:
            return oid
    raise SystemExit("cannot observe origin/main OID")


def _public_latest_tag(url: str) -> str | None:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "oh-my-grok-release",
                "Accept": "application/vnd.github+json",
            },
        )
        # Public readback: do not send GH_TOKEN / GITHUB_TOKEN.
        with urllib.request.urlopen(req, timeout=30) as resp:
            latest_payload = json.loads(resp.read().decode("utf-8"))
        if isinstance(latest_payload, dict):
            tag = latest_payload.get("tag_name")
            return str(tag) if tag else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def cmd_observe(args: argparse.Namespace) -> int:
    archive = Path(args.archive)
    checksums = Path(args.checksums)
    archive_id, sums_id, _, _ = local_asset_identities_from_files(archive, checksums)
    expected_names = (archive_id.name, sums_id.name)
    attempts = max(1, int(args.wait_latest_attempts))
    delay = max(0.0, float(args.wait_latest_seconds))
    latest_url = (
        os.environ.get("OMG_INSTALL_LATEST_API_URL")
        or f"https://api.github.com/repos/{args.repository}/releases/latest"
    )
    view: dict = {}
    latest_tag = None
    remote_assets: tuple[RemoteAssetIdentity, ...] = ()
    for attempt in range(attempts):
        view = _gh_json(
            [
                "release",
                "view",
                args.tag,
                "--json",
                "tagName,isDraft,isPrerelease,isLatest,targetCommitish,assets",
            ]
        )
        hashed = []
        with tempfile.TemporaryDirectory(prefix="omg-rel-obs-") as td:
            for row in view.get("assets") or []:
                name = str(row.get("name") or "")
                if name not in expected_names:
                    continue
                subprocess.run(
                    ["gh", "release", "download", args.tag, "-p", name, "-D", td],
                    check=True,
                )
                path = Path(td) / name
                hashed.append(
                    RemoteAssetIdentity(
                        name=name,
                        byte_length=path.stat().st_size,
                        sha256=_sha256_file(path),
                    )
                )
        try:
            remote_assets = select_expected_remotes(hashed, expected_names)
        except ReleaseEvidenceError as exc:
            print(exc, file=sys.stderr)
            return 2
        latest_tag = _public_latest_tag(latest_url)
        if bool(view.get("isLatest")) and latest_tag == args.tag:
            break
        if attempt + 1 < attempts:
            time.sleep(delay)
    try:
        facts = assemble_github_observe_facts(
            tag=args.tag,
            expected_commit=args.expected_commit,
            branch_oid=_observed_main_oid(),
            tag_object_type=_git("cat-file", "-t", args.tag),
            peeled_commit=_git("rev-parse", f"{args.tag}^{{commit}}"),
            draft=bool(view.get("isDraft")),
            prerelease=bool(view.get("isPrerelease")),
            is_latest=bool(view.get("isLatest")),
            latest_tag=str(latest_tag) if latest_tag else None,
            local_assets=(archive_id, sums_id),
            remote_assets=remote_assets,
            repository=args.repository,
            readback_at=args.readback_at,
            github_target_commitish=str(view.get("targetCommitish") or ""),
        )
    except ReleaseEvidenceError as exc:
        print(exc, file=sys.stderr)
        return 2
    Path(args.output).write_text(
        json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"ok": True, "output": args.output, "latest_tag": latest_tag}))
    return 0


def cmd_protection(args: argparse.Namespace) -> int:
    def _probe(path: str) -> tuple[int, dict | None]:
        result = subprocess.run(
            ["gh", "api", path],
            check=False,
            capture_output=True,
            text=True,
        )
        body = None
        if result.stdout.strip():
            try:
                parsed = json.loads(result.stdout)
                if isinstance(parsed, dict):
                    body = parsed
                elif isinstance(parsed, list):
                    body = {"rulesets": parsed}
            except json.JSONDecodeError:
                body = None
        if result.returncode == 0:
            return 200, body
        status = github_cli_http_status(
            returncode=result.returncode,
            stderr=result.stderr or "",
            stdout=result.stdout or "",
        )
        return status, body

    branch_http, branch_body = _probe(
        "repos/:owner/:repo/branches/main/protection"
    )
    rules_http, rules_body = _probe("repos/:owner/:repo/rulesets")
    evidence = classify_protection_readback(
        branch_http_status=branch_http,
        branch_body=branch_body,
        ruleset_http_status=rules_http,
        ruleset_body=rules_body,
    )
    Path(args.output).write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"ok": True, "claimed": evidence["claimed"]}))
    return 0


def cmd_public_install_probe(args: argparse.Namespace) -> int:
    """Record a local offline install probe against already-downloaded bytes.

    The workflow downloads public bytes with tokens unset, then calls this.
    Evidence stores digests and doctor_status only.
    """

    archive = Path(args.archive)
    checksums = Path(args.checksums)
    archive_id, sums_id, _, _ = local_asset_identities_from_files(archive, checksums)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "install.sh"),
            "--offline",
            "--archive",
            str(archive),
            "--checksums",
            str(checksums),
            "--source-tag",
            args.tag,
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(Path(args.home)),
            "GH_TOKEN": "",
            "GITHUB_TOKEN": "",
        },
    )
    combined = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0 and "integrity-verified" in combined:
        doctor = "integrity-verified"
        ok = True
    elif result.returncode == 0 and "completed_with_warning" in combined:
        doctor = "completed_with_warning"
        ok = True
    else:
        doctor = "failed"
        ok = False
    payload = {
        "ok": ok,
        "tag": args.tag,
        "archive_sha256": archive_id.sha256,
        "checksums_sha256": sums_id.sha256,
        "doctor_status": doctor,
    }
    Path(args.output).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"ok": ok, "doctor_status": doctor}))
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    notes = sub.add_parser("notes")
    notes.add_argument("--changelog", default="CHANGELOG.md")
    notes.add_argument("--version", required=True)
    notes.add_argument("--output", required=True)
    notes.set_defaults(func=cmd_notes)

    tag = sub.add_parser("tag-identity")
    tag.add_argument("--tag", required=True)
    tag.add_argument("--expected-commit", required=True)
    tag.set_defaults(func=cmd_tag_identity)

    observe = sub.add_parser("observe")
    observe.add_argument("--tag", required=True)
    observe.add_argument("--expected-commit", required=True)
    observe.add_argument("--repository", default="ImL1s/oh-my-grok")
    observe.add_argument("--archive", required=True)
    observe.add_argument("--checksums", required=True)
    observe.add_argument("--output", required=True)
    observe.add_argument("--readback-at", required=True)
    observe.add_argument("--wait-latest-attempts", type=int, default=1)
    observe.add_argument("--wait-latest-seconds", type=float, default=0)
    observe.set_defaults(func=cmd_observe)

    protection = sub.add_parser("protection")
    protection.add_argument("--output", required=True)
    protection.set_defaults(func=cmd_protection)

    probe = sub.add_parser("public-install-probe")
    probe.add_argument("--tag", required=True)
    probe.add_argument("--archive", required=True)
    probe.add_argument("--checksums", required=True)
    probe.add_argument("--home", required=True)
    probe.add_argument("--output", required=True)
    probe.set_defaults(func=cmd_public_install_probe)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
