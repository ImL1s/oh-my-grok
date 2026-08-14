"""Hermetic fake-GitHub publication + evidence producer tests (#169 PR2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omg_cli.contracts.release_transaction import validate_release_completion_evidence
from omg_cli.release_evidence import (
    GitHubReadbackFacts,
    GitHubReleaseObservation,
    LatestInstallObservation,
    ReleaseEvidenceError,
    assemble_github_observe_facts,
    build_release_completion_evidence,
    classify_protection_readback,
    commit_oid_or_none,
    extract_changelog_section,
    github_cli_http_status,
    plan_github_publication,
    produce_release_evidence_from_facts,
    require_annotated_tag,
)
from omg_cli.release_upload import LocalAssetIdentity, RemoteAssetIdentity


TAG = "v1.2.3"
COMMIT = "a" * 40
ARCHIVE = LocalAssetIdentity(
    name="oh-my-grok-1.2.3.tar.gz",
    sha256="b" * 64,
    byte_length=27,
)
SUMS = LocalAssetIdentity(
    name="SHA256SUMS",
    sha256="c" * 64,
    byte_length=90,
)
ASSETS = (ARCHIVE, SUMS)
INSTALL_OK = LatestInstallObservation(
    ok=True,
    tag=TAG,
    archive_sha256=ARCHIVE.sha256,
    checksums_sha256=SUMS.sha256,
    doctor_status="integrity-verified",
)


def _remote(asset: LocalAssetIdentity) -> RemoteAssetIdentity:
    return RemoteAssetIdentity(
        name=asset.name, byte_length=asset.byte_length, sha256=asset.sha256
    )


def _remotes() -> tuple[RemoteAssetIdentity, ...]:
    return tuple(_remote(asset) for asset in ASSETS)


def _facts(**kwargs: object) -> GitHubReadbackFacts:
    defaults = {
        "approved_branch": "main",
        "branch_oid": COMMIT,
        "tag": TAG,
        "tag_object_type": "tag",
        "peeled_commit": COMMIT,
        "draft": False,
        "prerelease": False,
        "is_latest": True,
        "latest_tag": TAG,
        "assets": ASSETS,
        "remote_assets": _remotes(),
        "install": INSTALL_OK,
        "readback_at": "2026-08-14T00:00:00Z",
        "redacted_locator": "github:ImL1s/oh-my-grok@v1.2.3",
        "github_target_commitish": COMMIT,
    }
    defaults.update(kwargs)
    return GitHubReadbackFacts(**defaults)  # type: ignore[arg-type]


def _view(
    *,
    assets: tuple[RemoteAssetIdentity, ...] | None = None,
    target: str = COMMIT,
    draft: bool = False,
    prerelease: bool = False,
    is_latest: bool = True,
    timed_out: bool = False,
    call_failed: bool = False,
    result_count: int = 1,
) -> GitHubReleaseObservation:
    rows = assets if assets is not None else tuple(_remote(a) for a in ASSETS)
    return GitHubReleaseObservation(
        tag=TAG,
        target_commit=target,
        draft=draft,
        prerelease=prerelease,
        is_latest=is_latest,
        assets=rows,
        result_count=result_count,
        timed_out=timed_out,
        call_failed=call_failed,
    )


def _plan(**kwargs: object):
    defaults = {
        "expected_tag": TAG,
        "expected_commit": COMMIT,
        "expected_assets": ASSETS,
        "tag_object_type": "tag",
        "peeled_commit": COMMIT,
        "observation": _view(),
        "latest_tag": TAG,
        "latest_install": INSTALL_OK,
        "require_complete": True,
    }
    defaults.update(kwargs)
    return plan_github_publication(**defaults)  # type: ignore[arg-type]


def test_changelog_extracts_versioned_section_not_unreleased() -> None:
    text = (
        "# Changelog\n\n## [Unreleased]\n\n- wip\n\n"
        "## [1.2.3] - 2026-08-01\n\n### Fixed\n- identity-safe upload\n\n"
        "## [1.2.2] - 2026-07-01\n\n- old\n"
    )
    section = extract_changelog_section(text, "1.2.3")
    assert "identity-safe upload" in section
    assert "Unreleased" not in section
    assert "1.2.2" not in section
    with pytest.raises(ReleaseEvidenceError, match="E_RELEASE_CHANGELOG_SECTION_MISSING"):
        extract_changelog_section(text, "9.9.9")


def test_annotated_tag_required() -> None:
    require_annotated_tag(
        tag=TAG,
        object_type="tag",
        peeled_commit=COMMIT,
        expected_commit=COMMIT,
    )
    with pytest.raises(ReleaseEvidenceError, match="E_RELEASE_TAG_NOT_ANNOTATED"):
        require_annotated_tag(
            tag=TAG,
            object_type="commit",
            peeled_commit=COMMIT,
            expected_commit=COMMIT,
        )
    with pytest.raises(ReleaseEvidenceError, match="E_RELEASE_TAG_TARGET_MISMATCH"):
        require_annotated_tag(
            tag=TAG,
            object_type="tag",
            peeled_commit="d" * 40,
            expected_commit=COMMIT,
        )


def test_fresh_publish_plans_upload_without_clobber() -> None:
    plan = _plan(observation=None, require_complete=False, latest_tag=None, latest_install=None)
    assert plan.status == "fresh"
    assert plan.asset_plans == (
        (ARCHIVE.name, "upload"),
        (SUMS.name, "upload"),
    )


def test_exact_idempotent_retry_skips_matching_assets() -> None:
    plan = _plan()
    assert plan.status == "idempotent"
    assert plan.asset_plans == (
        (ARCHIVE.name, "skip_identical"),
        (SUMS.name, "skip_identical"),
    )


def test_partial_assets_refuse() -> None:
    plan = _plan(observation=_view(assets=(_remote(ARCHIVE),)))
    assert plan.status == "refuse"
    assert plan.code == "E_RELEASE_PARTIAL_ASSETS"


def test_wrong_tag_target_refuses() -> None:
    plan = _plan(observation=_view(target="e" * 40))
    assert plan.status == "refuse"
    assert plan.code == "E_RELEASE_TAG_TARGET_MISMATCH"


def test_wrong_bytes_refuse_never_clobber() -> None:
    bad = RemoteAssetIdentity(
        name=ARCHIVE.name, byte_length=ARCHIVE.byte_length, sha256="d" * 64
    )
    plan = _plan(observation=_view(assets=(bad, _remote(SUMS))))
    assert plan.status == "refuse"
    assert plan.code == "E_RELEASE_ASSET_MISMATCH"


def test_missing_latest_refuses_completion() -> None:
    plan = _plan(observation=_view(is_latest=False, prerelease=True), latest_tag=None)
    assert plan.status == "refuse"
    assert plan.code == "E_RELEASE_LATEST_MISSING"


def test_failed_public_install_refuses() -> None:
    plan = _plan(
        latest_install=LatestInstallObservation(
            ok=False,
            tag=TAG,
            archive_sha256=ARCHIVE.sha256,
            checksums_sha256=SUMS.sha256,
            doctor_status="failed",
        )
    )
    assert plan.status == "refuse"
    assert plan.code == "E_RELEASE_LATEST_INSTALL_FAILED"


def test_unknown_readback_is_not_success() -> None:
    plan = _plan(observation=_view(timed_out=True, result_count=0, assets=()))
    assert plan.status == "refuse"
    assert plan.code == "E_RELEASE_READBACK_UNKNOWN"


def test_producer_emits_schema_valid_completion_evidence() -> None:
    evidence = build_release_completion_evidence(
        repository_id="OMG",
        run_id="github-run",
        semver="1.2.3",
        frozen_commit=COMMIT,
        transaction_nonce="nonce-1",
        release_active_manifest_sha256="1" * 64,
        release_bundle_manifest_sha256="2" * 64,
        facts=_facts(),
        verified_at="2026-08-14T00:01:00Z",
    )
    assert evidence["final_state"] == "complete"
    validate_release_completion_evidence(
        evidence,
        repository_id="OMG",
        run_id="github-run",
        semver="1.2.3",
        frozen_commit=COMMIT,
        transaction_nonce="nonce-1",
        release_active_manifest_sha256="1" * 64,
        release_bundle_manifest_sha256="2" * 64,
        claimed_release_channels=["github"],
        asset_names=[ARCHIVE.name, SUMS.name],
    )


def test_producer_refuses_locator_with_home_or_token() -> None:
    facts = _facts(redacted_locator="github://example/home/token")
    with pytest.raises(ReleaseEvidenceError, match="E_RELEASE_EVIDENCE_LOCATOR"):
        build_release_completion_evidence(
            repository_id="OMG",
            run_id="github-run",
            semver="1.2.3",
            frozen_commit=COMMIT,
            transaction_nonce="nonce-1",
            release_active_manifest_sha256="1" * 64,
            release_bundle_manifest_sha256="2" * 64,
            facts=facts,
            verified_at="2026-08-14T00:01:00Z",
        )


def test_produce_from_facts_json_round_trip() -> None:
    payload = {
        "repository_id": "OMG",
        "run_id": "github-run",
        "semver": "1.2.3",
        "frozen_commit": COMMIT,
        "transaction_nonce": "nonce-1",
        "release_active_manifest_sha256": "1" * 64,
        "release_bundle_manifest_sha256": "2" * 64,
        "verified_at": "2026-08-14T00:01:00Z",
        "readback": {
            "approved_branch": "main",
            "branch_oid": COMMIT,
            "tag": TAG,
            "tag_object_type": "tag",
            "peeled_commit": COMMIT,
            "draft": False,
            "prerelease": False,
            "is_latest": True,
            "latest_tag": TAG,
            "assets": [
                {
                    "name": ARCHIVE.name,
                    "sha256": ARCHIVE.sha256,
                    "byte_length": ARCHIVE.byte_length,
                },
                {
                    "name": SUMS.name,
                    "sha256": SUMS.sha256,
                    "byte_length": SUMS.byte_length,
                },
            ],
            "remote_assets": [
                {
                    "name": ARCHIVE.name,
                    "sha256": ARCHIVE.sha256,
                    "byte_length": ARCHIVE.byte_length,
                },
                {
                    "name": SUMS.name,
                    "sha256": SUMS.sha256,
                    "byte_length": SUMS.byte_length,
                },
            ],
            "install": {
                "ok": True,
                "tag": TAG,
                "archive_sha256": ARCHIVE.sha256,
                "checksums_sha256": SUMS.sha256,
                "doctor_status": "integrity-verified",
            },
            "readback_at": "2026-08-14T00:00:00Z",
            "redacted_locator": "github:ImL1s/oh-my-grok@v1.2.3",
        },
    }
    evidence = produce_release_evidence_from_facts(payload)
    assert evidence["run_id"] == "github-run"


def test_protection_readback_never_claims_without_proof() -> None:
    unavailable = classify_protection_readback(
        branch_http_status=403,
        branch_body=None,
        ruleset_http_status=403,
        ruleset_body=None,
    )
    assert unavailable["claimed"] is False
    assert unavailable["main_required_checks"] == "unavailable"
    missing = classify_protection_readback(
        branch_http_status=200,
        branch_body={"required_status_checks": None},
        ruleset_http_status=200,
        ruleset_body={"rulesets": []},
    )
    assert missing["claimed"] is False
    assert missing["main_required_checks"] == "missing"
    configured = classify_protection_readback(
        branch_http_status=200,
        branch_body={
            "required_status_checks": {"strict": True},
            "enforce_admins": {"enabled": True},
            "required_pull_request_reviews": {"required_approving_review_count": 1},
        },
        ruleset_http_status=200,
        ruleset_body={
            "rulesets": [
                {
                    "include": ["refs/tags/v*"],
                    "rules": [{"type": "deletion"}, {"type": "update"}],
                }
            ]
        },
    )
    assert configured["claimed"] is True
    assert configured["v_star_immutable_tags"] == "configured"


def test_changelog_notes_script(tmp_path: Path) -> None:
    import subprocess
    import sys

    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## [Unreleased]\n\n- wip\n\n## [1.2.3] - 2026-08-01\n\n- ship it\n",
        encoding="utf-8",
    )
    out = tmp_path / "notes.md"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "release_github_facts.py"),
            "notes",
            "--changelog",
            str(changelog),
            "--version",
            "1.2.3",
            "--output",
            str(out),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ship it" in out.read_text(encoding="utf-8")
    assert "Unreleased" not in out.read_text(encoding="utf-8")


def test_draft_release_refuses() -> None:
    plan = _plan(observation=_view(draft=True))
    assert plan.status == "refuse"
    assert plan.code == "E_RELEASE_DRAFT"


def test_github_oid_target_mismatch_refuses() -> None:
    plan = _plan(github_target_commitish="e" * 40)
    assert plan.status == "refuse"
    assert plan.code == "E_RELEASE_TAG_TARGET_MISMATCH"


def test_observed_main_oid_mismatch_refuses_completion() -> None:
    plan = _plan(observed_branch_oid="e" * 40)
    assert plan.status == "refuse"
    assert plan.code == "E_RELEASE_BRANCH_OID"


def test_extra_remote_assets_are_ignored() -> None:
    extra = RemoteAssetIdentity(
        name="notes.md", byte_length=4, sha256="d" * 64
    )
    facts = assemble_github_observe_facts(
        tag=TAG,
        expected_commit=COMMIT,
        branch_oid=COMMIT,
        tag_object_type="tag",
        peeled_commit=COMMIT,
        draft=False,
        prerelease=False,
        is_latest=True,
        latest_tag=TAG,
        local_assets=ASSETS,
        remote_assets=(*_remotes(), extra),
        repository="ImL1s/oh-my-grok",
        readback_at="2026-08-14T00:00:00Z",
        github_target_commitish=COMMIT,
    )
    assert [row["name"] for row in facts["remote_assets"]] == [
        ARCHIVE.name,
        SUMS.name,
    ]


def test_github_branch_target_commitish_is_not_an_oid() -> None:
    assert commit_oid_or_none("main") is None
    plan = _plan(github_target_commitish="main")
    assert plan.status == "idempotent"


def test_empty_remote_assets_refuse_observe_assembly() -> None:
    with pytest.raises(ReleaseEvidenceError, match="E_RELEASE_PARTIAL_ASSETS"):
        assemble_github_observe_facts(
            tag=TAG,
            expected_commit=COMMIT,
            branch_oid=COMMIT,
            tag_object_type="tag",
            peeled_commit=COMMIT,
            draft=False,
            prerelease=False,
            is_latest=True,
            latest_tag=TAG,
            local_assets=ASSETS,
            remote_assets=(),
            repository="ImL1s/oh-my-grok",
            readback_at="2026-08-14T00:00:00Z",
            github_target_commitish=COMMIT,
        )


def test_producer_refuses_remote_digest_mismatch() -> None:
    bad = RemoteAssetIdentity(
        name=ARCHIVE.name, byte_length=ARCHIVE.byte_length, sha256="d" * 64
    )
    with pytest.raises(ReleaseEvidenceError, match="E_RELEASE_ASSET_MISMATCH"):
        build_release_completion_evidence(
            repository_id="OMG",
            run_id="github-run",
            semver="1.2.3",
            frozen_commit=COMMIT,
            transaction_nonce="nonce-1",
            release_active_manifest_sha256="1" * 64,
            release_bundle_manifest_sha256="2" * 64,
            facts=_facts(remote_assets=(bad, _remote(SUMS))),
            verified_at="2026-08-14T00:01:00Z",
        )


def test_producer_refuses_branch_oid_mismatch() -> None:
    with pytest.raises(ReleaseEvidenceError, match="E_RELEASE_BRANCH_OID"):
        build_release_completion_evidence(
            repository_id="OMG",
            run_id="github-run",
            semver="1.2.3",
            frozen_commit=COMMIT,
            transaction_nonce="nonce-1",
            release_active_manifest_sha256="1" * 64,
            release_bundle_manifest_sha256="2" * 64,
            facts=_facts(branch_oid="e" * 40),
            verified_at="2026-08-14T00:01:00Z",
        )


def test_facts_require_remote_assets() -> None:
    payload = {
        "repository_id": "OMG",
        "run_id": "github-run",
        "semver": "1.2.3",
        "frozen_commit": COMMIT,
        "transaction_nonce": "nonce-1",
        "release_active_manifest_sha256": "1" * 64,
        "release_bundle_manifest_sha256": "2" * 64,
        "verified_at": "2026-08-14T00:01:00Z",
        "readback": {
            "approved_branch": "main",
            "branch_oid": COMMIT,
            "tag": TAG,
            "tag_object_type": "tag",
            "peeled_commit": COMMIT,
            "draft": False,
            "prerelease": False,
            "is_latest": True,
            "latest_tag": TAG,
            "assets": [
                {
                    "name": ARCHIVE.name,
                    "sha256": ARCHIVE.sha256,
                    "byte_length": ARCHIVE.byte_length,
                },
                {
                    "name": SUMS.name,
                    "sha256": SUMS.sha256,
                    "byte_length": SUMS.byte_length,
                },
            ],
            "install": {
                "ok": True,
                "tag": TAG,
                "archive_sha256": ARCHIVE.sha256,
                "checksums_sha256": SUMS.sha256,
                "doctor_status": "integrity-verified",
            },
            "readback_at": "2026-08-14T00:00:00Z",
            "redacted_locator": "github:ImL1s/oh-my-grok@v1.2.3",
        },
    }
    with pytest.raises(ReleaseEvidenceError, match="E_RELEASE_FACTS"):
        produce_release_evidence_from_facts(payload)


def test_github_cli_http_status_unknown_is_unavailable() -> None:
    assert github_cli_http_status(returncode=0) == 200
    assert github_cli_http_status(returncode=1, stderr="gh: HTTP 403") == 403
    assert github_cli_http_status(returncode=1, stderr="network down") == 0


def test_parity_release_evidence_cli(tmp_path: Path) -> None:
    import json
    import os
    import subprocess
    import sys

    facts = {
        "repository_id": "OMG",
        "run_id": "cli-ev",
        "semver": "1.2.3",
        "frozen_commit": COMMIT,
        "transaction_nonce": "nonce-1",
        "release_active_manifest_sha256": "1" * 64,
        "release_bundle_manifest_sha256": "2" * 64,
        "verified_at": "2026-08-14T00:01:00Z",
        "readback": {
            "approved_branch": "main",
            "branch_oid": COMMIT,
            "tag": TAG,
            "tag_object_type": "tag",
            "peeled_commit": COMMIT,
            "draft": False,
            "prerelease": False,
            "is_latest": True,
            "latest_tag": TAG,
            "github_target_commitish": COMMIT,
            "assets": [
                {
                    "name": ARCHIVE.name,
                    "sha256": ARCHIVE.sha256,
                    "byte_length": ARCHIVE.byte_length,
                },
                {
                    "name": SUMS.name,
                    "sha256": SUMS.sha256,
                    "byte_length": SUMS.byte_length,
                },
            ],
            "remote_assets": [
                {
                    "name": ARCHIVE.name,
                    "sha256": ARCHIVE.sha256,
                    "byte_length": ARCHIVE.byte_length,
                },
                {
                    "name": SUMS.name,
                    "sha256": SUMS.sha256,
                    "byte_length": SUMS.byte_length,
                },
            ],
            "install": {
                "ok": True,
                "tag": TAG,
                "archive_sha256": ARCHIVE.sha256,
                "checksums_sha256": SUMS.sha256,
                "doctor_status": "integrity-verified",
            },
            "readback_at": "2026-08-14T00:00:00Z",
            "redacted_locator": "github:ImL1s/oh-my-grok@v1.2.3",
        },
    }
    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps(facts), encoding="utf-8")
    output = tmp_path / "evidence.json"
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
            "release-evidence",
            "--facts",
            str(facts_path),
            "--output",
            str(output),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["final_state"] == "complete"
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["run_id"] == "cli-ev"
