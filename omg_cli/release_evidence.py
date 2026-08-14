"""Canonical GitHub-channel release evidence producer (#169 PR2).

Pure decision + schema construction.  Callers inject git/GitHub/install
observations; this module never invokes ``gh``, git, or the network.

The only constructor for ``release-completion-evidence`` / the finalizer
input is :func:`build_release_completion_evidence`.  Hand-authored records
are rejected by the existing schema once they drift from these facts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from omg_cli.contracts.release_transaction import (
    classify_external_observation,
    make_call_record,
    release_transaction_identity_hash,
    validate_release_completion_evidence,
)
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_git_oid,
    require_iso8601,
    require_nonempty_string,
    require_safe_id,
    require_sha256,
)
from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
from omg_cli.release_upload import LocalAssetIdentity, plan_release_asset_upload
from omg_cli.release_upload import RemoteAssetIdentity


PublicationStatus = Literal["fresh", "idempotent", "refuse"]


class ReleaseEvidenceError(ValueError):
    """Typed fail-closed publication / evidence error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


_CHANGELOG_HEADING = re.compile(
    r"^## \[([^\]]+)\](?:\s+-\s+\S+)?\s*$", re.MULTILINE
)
_GIT_OID_RE = re.compile(r"^[0-9a-f]{40}$")


def commit_oid_or_none(value: str | None) -> str | None:
    """Return a lowercase git OID, or None when *value* is a branch/ref name."""

    text = (value or "").strip().lower()
    if _GIT_OID_RE.fullmatch(text):
        return text
    return None


def github_cli_http_status(
    *, returncode: int, stderr: str = "", stdout: str = ""
) -> int:
    """Map ``gh api`` output to an HTTP status. Unknown failures are 0 (unavailable)."""

    if returncode == 0:
        return 200
    combined = f"{stderr or ''}{stdout or ''}"
    for code in (401, 403, 404):
        if f"HTTP {code}" in combined:
            return code
    return 0


def extract_changelog_section(text: str, version: str) -> str:
    """Return the Keep-a-Changelog section for *version* (not Unreleased)."""

    require_nonempty_string(version, label="version")
    matches = list(_CHANGELOG_HEADING.finditer(text))
    for index, match in enumerate(matches):
        if match.group(1) != version:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.start() : end].strip()
        if not body:
            break
        return body + "\n"
    raise ReleaseEvidenceError(
        "E_RELEASE_CHANGELOG_SECTION_MISSING",
        f"CHANGELOG.md has no versioned section ## [{version}]",
    )


def require_annotated_tag(
    *,
    tag: str,
    object_type: str,
    peeled_commit: str,
    expected_commit: str,
) -> str:
    """Fail closed unless *tag* is annotated and peels to *expected_commit*."""

    require_nonempty_string(tag, label="tag")
    require_git_oid(peeled_commit, label="peeled_commit")
    require_git_oid(expected_commit, label="expected_commit")
    if object_type != "tag":
        raise ReleaseEvidenceError(
            "E_RELEASE_TAG_NOT_ANNOTATED",
            f"{tag} is {object_type!r}, not an annotated tag object",
        )
    if peeled_commit != expected_commit:
        raise ReleaseEvidenceError(
            "E_RELEASE_TAG_TARGET_MISMATCH",
            f"{tag} peels to {peeled_commit}, expected {expected_commit}",
        )
    return peeled_commit


@dataclass(frozen=True)
class GitHubReleaseObservation:
    """One bounded GitHub release view (no credentials)."""

    tag: str
    target_commit: str
    draft: bool
    prerelease: bool
    is_latest: bool
    assets: tuple[RemoteAssetIdentity, ...]
    result_count: int = 1
    timed_out: bool = False
    call_failed: bool = False


@dataclass(frozen=True)
class LatestInstallObservation:
    """Public-latest install probe result (no HOME/token in evidence)."""

    ok: bool
    tag: str
    archive_sha256: str
    checksums_sha256: str
    doctor_status: str


@dataclass(frozen=True)
class PublicationPlan:
    status: PublicationStatus
    code: str | None
    message: str
    asset_plans: tuple[tuple[str, str], ...]


def _classify_view(observation: GitHubReleaseObservation | None) -> str:
    if observation is None:
        return classify_external_observation(0)
    return classify_external_observation(
        observation.result_count,
        timed_out=observation.timed_out,
        call_failed=observation.call_failed,
    )


def plan_github_publication(
    *,
    expected_tag: str,
    expected_commit: str,
    expected_assets: Sequence[LocalAssetIdentity],
    tag_object_type: str,
    peeled_commit: str,
    observation: GitHubReleaseObservation | None,
    latest_tag: str | None,
    latest_install: LatestInstallObservation | None,
    require_complete: bool = True,
    github_target_commitish: str | None = None,
    observed_branch_oid: str | None = None,
) -> PublicationPlan:
    """Plan fresh upload, idempotent skip, or typed refuse.

    ``require_complete=False`` is the asset-upload phase (latest/install may
    still be absent).  Completion evidence always uses ``require_complete=True``.
    """

    require_annotated_tag(
        tag=expected_tag,
        object_type=tag_object_type,
        peeled_commit=peeled_commit,
        expected_commit=expected_commit,
    )
    target_oid = commit_oid_or_none(github_target_commitish)
    if target_oid is not None and target_oid != expected_commit:
        return PublicationPlan(
            status="refuse",
            code="E_RELEASE_TAG_TARGET_MISMATCH",
            message=(
                f"GitHub targetCommitish {target_oid} != {expected_commit}"
            ),
            asset_plans=(),
        )
    if observed_branch_oid:
        require_git_oid(observed_branch_oid, label="observed_branch_oid")
        if require_complete and observed_branch_oid != expected_commit:
            return PublicationPlan(
                status="refuse",
                code="E_RELEASE_BRANCH_OID",
                message=(
                    f"observed main {observed_branch_oid} != {expected_commit}"
                ),
                asset_plans=(),
            )
    if len(expected_assets) != 2:
        raise ReleaseEvidenceError(
            "E_RELEASE_ASSET_SET",
            "expected exactly archive + SHA256SUMS",
        )
    names = tuple(asset.name for asset in expected_assets)
    if names[-1] != "SHA256SUMS":
        raise ReleaseEvidenceError(
            "E_RELEASE_ASSET_SET",
            "SHA256SUMS must be last in public upload order",
        )

    kind = _classify_view(observation)
    if kind in {"unknown", "failed", "ambiguous"}:
        return PublicationPlan(
            status="refuse",
            code=f"E_RELEASE_READBACK_{kind.upper()}",
            message=f"GitHub release view is {kind}, not success",
            asset_plans=(),
        )
    if kind == "absent" or observation is None:
        if require_complete:
            return PublicationPlan(
                status="refuse",
                code="E_RELEASE_MISSING",
                message="release is absent; cannot complete the transaction",
                asset_plans=(),
            )
        return PublicationPlan(
            status="fresh",
            code=None,
            message="no remote release; upload without --clobber",
            asset_plans=tuple((asset.name, "upload") for asset in expected_assets),
        )

    if observation.tag != expected_tag:
        return PublicationPlan(
            status="refuse",
            code="E_RELEASE_TAG_MISMATCH",
            message=f"viewed tag {observation.tag!r} != {expected_tag!r}",
            asset_plans=(),
        )
    if observation.target_commit != expected_commit:
        return PublicationPlan(
            status="refuse",
            code="E_RELEASE_TAG_TARGET_MISMATCH",
            message=(
                f"release target {observation.target_commit} != {expected_commit}"
            ),
            asset_plans=(),
        )
    if observation.draft:
        return PublicationPlan(
            status="refuse",
            code="E_RELEASE_DRAFT",
            message="draft releases are not a public success state",
            asset_plans=(),
        )

    remote_by_name = {asset.name: asset for asset in observation.assets}
    present = [name for name in names if name in remote_by_name]
    missing = [name for name in names if name not in remote_by_name]
    if present and missing:
        return PublicationPlan(
            status="refuse",
            code="E_RELEASE_PARTIAL_ASSETS",
            message=f"partial assets present={present} missing={missing}",
            asset_plans=(),
        )
    if missing:
        if require_complete:
            return PublicationPlan(
                status="refuse",
                code="E_RELEASE_PARTIAL_ASSETS",
                message=f"assets missing: {missing}",
                asset_plans=(),
            )
        return PublicationPlan(
            status="fresh",
            code=None,
            message="assets absent; upload without --clobber",
            asset_plans=tuple((asset.name, "upload") for asset in expected_assets),
        )

    asset_plans: list[tuple[str, str]] = []
    for local in expected_assets:
        remote = remote_by_name[local.name]
        decision = plan_release_asset_upload(local, remote)
        if decision == "refuse_mismatch":
            return PublicationPlan(
                status="refuse",
                code="E_RELEASE_ASSET_MISMATCH",
                message=f"remote {local.name} identity differs; never clobber",
                asset_plans=(),
            )
        asset_plans.append((local.name, decision))

    if not require_complete:
        matching = all(plan == "skip_identical" for _, plan in asset_plans)
        return PublicationPlan(
            status="idempotent" if matching else "fresh",
            code=None,
            message=(
                "assets identity-safe; promotion/latest not required yet"
                if observation.prerelease
                else "assets identity-safe"
            ),
            asset_plans=tuple(asset_plans),
        )

    if observation.prerelease or not observation.is_latest:
        return PublicationPlan(
            status="refuse",
            code="E_RELEASE_LATEST_MISSING",
            message="GitHub latest/prerelease state is not the public latest",
            asset_plans=tuple(asset_plans),
        )
    if latest_tag != expected_tag:
        return PublicationPlan(
            status="refuse",
            code="E_RELEASE_LATEST_MISSING",
            message=f"latest tag {latest_tag!r} != {expected_tag!r}",
            asset_plans=tuple(asset_plans),
        )
    if latest_install is None or not latest_install.ok:
        return PublicationPlan(
            status="refuse",
            code="E_RELEASE_LATEST_INSTALL_FAILED",
            message="public-latest install/doctor probe did not succeed",
            asset_plans=tuple(asset_plans),
        )
    if latest_install.tag != expected_tag:
        return PublicationPlan(
            status="refuse",
            code="E_RELEASE_LATEST_INSTALL_FAILED",
            message=f"install resolved {latest_install.tag!r} != {expected_tag!r}",
            asset_plans=tuple(asset_plans),
        )
    archive = expected_assets[0]
    sums = expected_assets[1]
    if latest_install.archive_sha256 != archive.sha256:
        return PublicationPlan(
            status="refuse",
            code="E_RELEASE_LATEST_INSTALL_FAILED",
            message="public-latest archive digest differs from frozen bundle",
            asset_plans=tuple(asset_plans),
        )
    if latest_install.checksums_sha256 != sums.sha256:
        return PublicationPlan(
            status="refuse",
            code="E_RELEASE_LATEST_INSTALL_FAILED",
            message="public-latest SHA256SUMS digest differs from frozen bundle",
            asset_plans=tuple(asset_plans),
        )
    if latest_install.doctor_status not in {"integrity-verified", "completed_with_warning"}:
        return PublicationPlan(
            status="refuse",
            code="E_RELEASE_LATEST_INSTALL_FAILED",
            message=f"install doctor status {latest_install.doctor_status!r}",
            asset_plans=tuple(asset_plans),
        )
    return PublicationPlan(
        status="idempotent",
        code=None,
        message="tag, assets, latest, and public install match frozen identity",
        asset_plans=tuple(asset_plans),
    )


def github_omg_evidence_states(asset_names: Sequence[str]) -> tuple[str, ...]:
    return (
        "branch_readback_passed",
        "commit_proof_passed",
        "tag_readback_passed",
        "prerelease_readback_passed",
        *(f"asset-{sha256_hex(name)[:16]}.asset_readback_passed" for name in asset_names),
        "assets_readback_passed",
        "github_promotion_readback_passed",
        "github_latest_readback_passed",
        "verified_readback_passed",
        "final_readback_passed",
        "complete",
    )


@dataclass(frozen=True)
class GitHubReadbackFacts:
    """Exact facts bound into completion-evidence call records."""

    approved_branch: str
    branch_oid: str
    tag: str
    tag_object_type: str
    peeled_commit: str
    draft: bool
    prerelease: bool
    is_latest: bool
    latest_tag: str
    assets: tuple[LocalAssetIdentity, ...]
    remote_assets: tuple[RemoteAssetIdentity, ...]
    install: LatestInstallObservation
    readback_at: str
    redacted_locator: str
    github_target_commitish: str = ""


def _identity_for_state(
    state: str,
    *,
    facts: GitHubReadbackFacts,
    frozen_commit: str,
) -> dict[str, Any]:
    if state == "branch_readback_passed":
        return {"branch": facts.approved_branch, "oid": facts.branch_oid}
    if state == "commit_proof_passed":
        return {"commit": frozen_commit}
    if state == "tag_readback_passed":
        return {
            "tag": facts.tag,
            "object_type": facts.tag_object_type,
            "peeled_commit": facts.peeled_commit,
        }
    if state == "prerelease_readback_passed":
        return {
            "tag": facts.tag,
            "draft": facts.draft,
            "prerelease": facts.prerelease,
        }
    if state.endswith(".asset_readback_passed"):
        for asset in facts.assets:
            digest = f"asset-{sha256_hex(asset.name)[:16]}.asset_readback_passed"
            if digest == state:
                return {
                    "name": asset.name,
                    "sha256": asset.sha256,
                    "byte_length": asset.byte_length,
                }
    if state == "assets_readback_passed":
        return {
            "order": [asset.name for asset in facts.assets],
            "digests": [asset.sha256 for asset in facts.assets],
        }
    if state == "github_promotion_readback_passed":
        return {"tag": facts.tag, "prerelease": facts.prerelease, "draft": facts.draft}
    if state == "github_latest_readback_passed":
        return {"latest_tag": facts.latest_tag, "is_latest": facts.is_latest}
    if state == "verified_readback_passed":
        return {
            "install_tag": facts.install.tag,
            "archive_sha256": facts.install.archive_sha256,
            "doctor_status": facts.install.doctor_status,
        }
    if state == "final_readback_passed":
        return {"tag": facts.tag, "commit": frozen_commit}
    if state == "complete":
        return {"final_state": "complete"}
    raise ReleaseEvidenceError(
        "E_RELEASE_EVIDENCE_STATE", f"no identity mapping for {state}"
    )


def build_release_completion_evidence(
    *,
    repository_id: str,
    run_id: str,
    semver: str,
    frozen_commit: str,
    transaction_nonce: str,
    release_active_manifest_sha256: str,
    release_bundle_manifest_sha256: str,
    facts: GitHubReadbackFacts,
    verified_at: str,
) -> dict[str, Any]:
    """The only production constructor for finalizer input evidence."""

    if repository_id != "OMG":
        raise ReleaseEvidenceError(
            "E_RELEASE_EVIDENCE_REPOSITORY",
            "this producer constructs OMG GitHub-channel evidence only",
        )
    require_safe_id(run_id, label="run_id")
    require_safe_id(transaction_nonce, label="transaction_nonce")
    require_sha256(
        release_active_manifest_sha256, label="release_active_manifest_sha256"
    )
    require_sha256(
        release_bundle_manifest_sha256, label="release_bundle_manifest_sha256"
    )
    require_iso8601(verified_at, label="verified_at")
    require_iso8601(facts.readback_at, label="readback_at")
    if "://" in facts.redacted_locator or "token" in facts.redacted_locator.lower():
        raise ReleaseEvidenceError(
            "E_RELEASE_EVIDENCE_LOCATOR",
            "redacted locator must not contain URLs or credential material",
        )
    if any(
        marker in facts.redacted_locator.lower()
        for marker in ("home", "users/", "\\users\\", "ghp_", "github_pat")
    ):
        raise ReleaseEvidenceError(
            "E_RELEASE_EVIDENCE_LOCATOR",
            "redacted locator must not contain private paths or tokens",
        )

    plan = plan_github_publication(
        expected_tag=facts.tag,
        expected_commit=frozen_commit,
        expected_assets=facts.assets,
        tag_object_type=facts.tag_object_type,
        peeled_commit=facts.peeled_commit,
        observation=GitHubReleaseObservation(
            tag=facts.tag,
            target_commit=facts.peeled_commit,
            draft=facts.draft,
            prerelease=facts.prerelease,
            is_latest=facts.is_latest,
            assets=facts.remote_assets,
        ),
        latest_tag=facts.latest_tag,
        latest_install=facts.install,
        require_complete=True,
        github_target_commitish=facts.github_target_commitish,
        observed_branch_oid=facts.branch_oid,
    )
    if plan.status == "refuse":
        raise ReleaseEvidenceError(plan.code or "E_RELEASE_REFUSE", plan.message)

    asset_names = [asset.name for asset in facts.assets]
    states = github_omg_evidence_states(asset_names)
    records: list[dict[str, Any]] = []
    predecessor = "candidate_gates_passed"
    for index, state in enumerate(states):
        identity = _identity_for_state(
            state, facts=facts, frozen_commit=frozen_commit
        )
        record = make_call_record(
            repository=repository_id,
            semver=semver,
            frozen_commit=frozen_commit,
            transaction_nonce=transaction_nonce,
            step=f"github-{index:02d}-{state}",
            state=state,
            allowed_predecessor=predecessor,
            attempt=1,
            redacted_external_locator=facts.redacted_locator,
            expected_identity=identity,
            expected_byte_digest=identity.get("sha256")
            if isinstance(identity.get("sha256"), str)
            else None,
            request=identity,
            prior_mutable_identity=None,
        )
        if state.endswith("_readback_passed"):
            record["object_digest"] = sha256_hex(canonical_json_bytes(identity))
            record["readback_at"] = facts.readback_at
        records.append(record)
        predecessor = state

    evidence = {
        "store_kind": "release_completion_evidence",
        "schema_version": 1,
        "repository_id": repository_id,
        "run_id": run_id,
        "semver": semver,
        "frozen_commit": frozen_commit,
        "transaction_nonce": transaction_nonce,
        "transaction_identity_hash": release_transaction_identity_hash(
            repository_id, semver, frozen_commit, transaction_nonce
        ),
        "release_active_manifest_sha256": release_active_manifest_sha256,
        "release_bundle_manifest_sha256": release_bundle_manifest_sha256,
        "final_state": "complete",
        "call_records": records,
        "verified_at": verified_at,
    }
    try:
        return validate_release_completion_evidence(
            evidence,
            repository_id=repository_id,
            run_id=run_id,
            semver=semver,
            frozen_commit=frozen_commit,
            transaction_nonce=transaction_nonce,
            release_active_manifest_sha256=release_active_manifest_sha256,
            release_bundle_manifest_sha256=release_bundle_manifest_sha256,
            claimed_release_channels=["github"],
            asset_names=asset_names,
        )
    except ContractValidationError as exc:
        raise ReleaseEvidenceError("E_RELEASE_EVIDENCE_INVALID", str(exc)) from exc


def _parse_local_assets(rows: object) -> tuple[LocalAssetIdentity, ...]:
    if not isinstance(rows, list) or len(rows) != 2:
        raise ReleaseEvidenceError("E_RELEASE_FACTS", "facts.assets must have two rows")
    assets: list[LocalAssetIdentity] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ReleaseEvidenceError("E_RELEASE_FACTS", "asset row must be an object")
        try:
            assets.append(
                LocalAssetIdentity(
                    name=str(row["name"]),
                    sha256=str(row["sha256"]),
                    byte_length=int(row["byte_length"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReleaseEvidenceError(
                "E_RELEASE_FACTS", "asset row is missing name/sha256/byte_length"
            ) from exc
    return tuple(assets)


def _parse_remote_assets(rows: object) -> tuple[RemoteAssetIdentity, ...]:
    if not isinstance(rows, list) or len(rows) != 2:
        raise ReleaseEvidenceError(
            "E_RELEASE_FACTS", "facts.remote_assets must have two hashed rows"
        )
    assets: list[RemoteAssetIdentity] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ReleaseEvidenceError(
                "E_RELEASE_FACTS", "remote_assets row must be an object"
            )
        try:
            assets.append(
                RemoteAssetIdentity(
                    name=str(row["name"]),
                    sha256=str(row["sha256"]),
                    byte_length=int(row["byte_length"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReleaseEvidenceError(
                "E_RELEASE_FACTS",
                "remote_assets row is missing name/sha256/byte_length",
            ) from exc
    return tuple(assets)


def select_expected_remotes(
    remotes: Sequence[RemoteAssetIdentity],
    expected_names: Sequence[str],
) -> tuple[RemoteAssetIdentity, ...]:
    """Keep only the frozen upload set; extra GitHub assets are ignored."""

    by_name = {asset.name: asset for asset in remotes}
    missing = [name for name in expected_names if name not in by_name]
    if missing:
        raise ReleaseEvidenceError(
            "E_RELEASE_PARTIAL_ASSETS",
            f"hashed remotes missing expected names: {missing}",
        )
    return tuple(by_name[name] for name in expected_names)


def assemble_github_observe_facts(
    *,
    tag: str,
    expected_commit: str,
    branch_oid: str,
    tag_object_type: str,
    peeled_commit: str,
    draft: bool,
    prerelease: bool,
    is_latest: bool,
    latest_tag: str | None,
    local_assets: Sequence[LocalAssetIdentity],
    remote_assets: Sequence[RemoteAssetIdentity],
    repository: str,
    readback_at: str,
    github_target_commitish: str,
) -> dict[str, Any]:
    """Pure observe JSON (no tokens/HOME). Refuses empty remote hashes."""

    if len(local_assets) != 2:
        raise ReleaseEvidenceError("E_RELEASE_ASSET_SET", "expected archive + SHA256SUMS")
    selected = select_expected_remotes(
        remote_assets, [asset.name for asset in local_assets]
    )
    return {
        "tag": tag,
        "tag_object_type": tag_object_type,
        "peeled_commit": peeled_commit,
        "draft": draft,
        "prerelease": prerelease,
        "is_latest": is_latest,
        "latest_tag": latest_tag,
        "approved_branch": "main",
        "branch_oid": branch_oid,
        "github_target_commitish": github_target_commitish,
        "assets": [
            {
                "name": asset.name,
                "sha256": asset.sha256,
                "byte_length": asset.byte_length,
            }
            for asset in local_assets
        ],
        "remote_assets": [
            {
                "name": asset.name,
                "sha256": asset.sha256,
                "byte_length": asset.byte_length,
            }
            for asset in selected
        ],
        "redacted_locator": f"github:{repository}@{tag}",
        "readback_at": readback_at,
        "expected_commit": expected_commit,
    }


def facts_from_mapping(value: Mapping[str, Any]) -> GitHubReadbackFacts:
    """Parse injected JSON facts (no live GitHub)."""

    assets = _parse_local_assets(value.get("assets"))
    remotes = _parse_remote_assets(value.get("remote_assets"))
    install_raw = value.get("install")
    if not isinstance(install_raw, Mapping):
        raise ReleaseEvidenceError("E_RELEASE_FACTS", "facts.install is required")
    install = LatestInstallObservation(
        ok=bool(install_raw.get("ok")),
        tag=str(install_raw.get("tag") or ""),
        archive_sha256=str(install_raw.get("archive_sha256") or ""),
        checksums_sha256=str(install_raw.get("checksums_sha256") or ""),
        doctor_status=str(install_raw.get("doctor_status") or ""),
    )
    return GitHubReadbackFacts(
        approved_branch=str(value.get("approved_branch") or "main"),
        branch_oid=str(value["branch_oid"]),
        tag=str(value["tag"]),
        tag_object_type=str(value.get("tag_object_type") or "tag"),
        peeled_commit=str(value["peeled_commit"]),
        draft=bool(value.get("draft")),
        prerelease=bool(value.get("prerelease")),
        is_latest=bool(value.get("is_latest")),
        latest_tag=str(value.get("latest_tag") or ""),
        assets=assets,
        remote_assets=remotes,
        install=install,
        readback_at=str(value["readback_at"]),
        redacted_locator=str(value.get("redacted_locator") or "github:release"),
        github_target_commitish=str(value.get("github_target_commitish") or ""),
    )


def produce_release_evidence_from_facts(payload: Mapping[str, Any]) -> dict[str, Any]:
    """CLI/workflow entry: one JSON object in, validated evidence out."""

    facts = facts_from_mapping(
        payload["readback"] if "readback" in payload else payload
    )
    evidence = build_release_completion_evidence(
        repository_id=str(payload.get("repository_id") or "OMG"),
        run_id=str(payload["run_id"]),
        semver=str(payload["semver"]),
        frozen_commit=str(payload["frozen_commit"]),
        transaction_nonce=str(payload["transaction_nonce"]),
        release_active_manifest_sha256=str(
            payload["release_active_manifest_sha256"]
        ),
        release_bundle_manifest_sha256=str(
            payload["release_bundle_manifest_sha256"]
        ),
        facts=facts,
        verified_at=str(payload["verified_at"]),
    )
    return evidence


def classify_protection_readback(
    *,
    branch_http_status: int,
    branch_body: Mapping[str, Any] | None,
    ruleset_http_status: int,
    ruleset_body: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Honest settings gate: never claim configured without exact readback."""

    def _unavailable(status: int) -> bool:
        return status in {401, 403, 404} or status >= 500 or status == 0

    main_state = "unavailable"
    tag_state = "unavailable"
    if not _unavailable(branch_http_status) and branch_http_status == 200:
        checks = (branch_body or {}).get("required_status_checks")
        enforce = (branch_body or {}).get("enforce_admins")
        reviews = (branch_body or {}).get("required_pull_request_reviews")
        if (
            isinstance(checks, Mapping)
            and checks.get("strict") is True
            and isinstance(enforce, Mapping)
            and enforce.get("enabled") is True
            and isinstance(reviews, Mapping)
            and int(reviews.get("required_approving_review_count") or 0) >= 1
        ):
            main_state = "configured"
        else:
            main_state = "missing"
    if not _unavailable(ruleset_http_status) and ruleset_http_status == 200:
        rows = ruleset_body.get("rulesets") if isinstance(ruleset_body, Mapping) else ruleset_body
        found = False
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                include = row.get("include") or []
                rules = row.get("rules") or []
                if not isinstance(include, list) or not isinstance(rules, list):
                    continue
                if "refs/tags/v*" not in include and "refs/tags/v**" not in include:
                    continue
                for rule in rules:
                    if isinstance(rule, Mapping) and rule.get("type") in {
                        "update",
                        "deletion",
                        "non_fast_forward",
                    }:
                        found = True
        tag_state = "configured" if found else "missing"
    claimed = main_state == "configured" and tag_state == "configured"
    return {
        "store_kind": "github_protection_readback",
        "schema_version": 1,
        "main_required_checks": main_state,
        "v_star_immutable_tags": tag_state,
        "claimed": claimed,
        "branch_http_status": branch_http_status,
        "ruleset_http_status": ruleset_http_status,
    }


__all__ = [
    "GitHubReadbackFacts",
    "GitHubReleaseObservation",
    "LatestInstallObservation",
    "PublicationPlan",
    "ReleaseEvidenceError",
    "assemble_github_observe_facts",
    "build_release_completion_evidence",
    "classify_protection_readback",
    "commit_oid_or_none",
    "extract_changelog_section",
    "facts_from_mapping",
    "github_cli_http_status",
    "github_omg_evidence_states",
    "plan_github_publication",
    "produce_release_evidence_from_facts",
    "require_annotated_tag",
    "select_expected_remotes",
]
