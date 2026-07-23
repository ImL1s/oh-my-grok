from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import omg_cli.contracts.run_manifest as run_manifest_contract
from omg_cli.contracts.parity_schema import (
    NORMATIVE_ARTIFACT_HASHES,
    OMG_OWNER_PATTERNS,
)
from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    IMMUTABLE_SOURCE_MODE,
    MANAGED_DIR_MODE,
    mode_bits,
)
from omg_cli.contracts.release_transaction import (
    make_call_record,
    release_transaction_identity_hash,
)
from omg_cli.contracts.run_manifest import (
    OMG_OWNERSHIP_MANIFEST_HASH,
    RUN_MANIFEST_STATE_SET,
    STANDALONE_INPUT_SLOTS,
    _deny_body_bytes,
    _extract_function_bytes,
    _verify_complete_owner_path_union,
    _verify_proposal_paths_current,
    build_generated_output_attestation,
    emit_owner_handoff,
    expected_manifest_path,
    expected_repository_aggregate_path,
    expected_release_completion_evidence_path,
    expected_trust_root,
    finalize_release_run_manifest,
    initialize_run_manifest,
    read_run_manifest,
    sign_repository_aggregate,
    transition_run_manifest,
    verify_repository_aggregate,
)
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.contracts.writer_chain import (
    FINAL_AGGREGATE_DOMAIN,
    HANDOFF_DOMAIN,
    INPUT_AGGREGATE_DOMAIN,
    canonical_json_bytes,
    hmac_sha256_hex,
    parse_canonical_json_bytes,
    sha256_hex,
    verify_final_candidate,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fixture_owned_paths() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for wave in (f"OMG-W{index}" for index in range(6)):
        paths: list[str] = []
        for pattern in OMG_OWNER_PATTERNS[wave]:
            if pattern.endswith("/**"):
                paths.append(f"{pattern[:-3].rstrip('/')}/fixture.txt")
            elif pattern == "agents/*.md":
                paths.append("agents/fixture.md")
            elif pattern == "skills/*/SKILL.md":
                paths.append("skills/fixture/SKILL.md")
            else:
                paths.append(pattern)
        result[wave] = sorted(set(paths), key=lambda item: item.encode("utf-8"))
    return result


FIXTURE_OWNED_PATHS = _fixture_owned_paths()


def _write_fixture_repository(root: Path) -> tuple[str, str]:
    if not (root / ".git").is_dir():
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "fixture@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=root, check=True)
        for relative in {
            path for paths in FIXTURE_OWNED_PATHS.values() for path in paths
        }:
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(f"fixture bytes for {relative}\n", encoding="utf-8")
        for relative in (
            "scripts/generate_standalone_hook.py",
            "omg_cli/deny.py",
            "hooks/bin/_common.py",
        ):
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((PROJECT_ROOT / relative).read_bytes())
        (root / "plugin.json").write_bytes(
            canonical_json_bytes({"name": "oh-my-grok", "version": "0.5.0"})
        )
        (root / "omg_capabilities.lock.json").write_bytes(
            canonical_json_bytes({"version": "0.5.0"})
        )
        (root / ".gitignore").write_text(".omg/\n", encoding="utf-8")
        (root / "LICENSE").write_text("fixture license\n", encoding="utf-8")
        fixture_bin = root / "bin" / "omg"
        fixture_bin.parent.mkdir(parents=True, exist_ok=True)
        fixture_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fixture_bin.chmod(0o755)
        generated = subprocess.run(
            [sys.executable, "scripts/generate_standalone_hook.py"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert generated.returncode == 0, generated.stderr
        subprocess.run(["git", "add", "--all"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture base"], cwd=root, check=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=root, check=True)
        remote = root.with_name(f"{root.name}-origin.git")
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=root, check=True)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True
    ).strip()
    return commit, tree


def _init(root: Path, run_id: str = "manifest-test") -> tuple[dict, Path]:
    base_commit, base_tree = _write_fixture_repository(root)
    manifest = initialize_run_manifest(
        root,
        repository_id="OMG",
        run_id=run_id,
        frozen_base_commit=base_commit,
        frozen_base_tree=base_tree,
        approved_branch="main",
        approved_remote="origin",
        approved_remote_old_oid=base_commit,
        normative_artifact_hashes=NORMATIVE_ARTIFACT_HASHES,
        ownership_manifest_hash=OMG_OWNERSHIP_MANIFEST_HASH,
        claimed_release_channels=["github"],
        created_at="2026-07-22T00:00:00Z",
    )
    return manifest, expected_manifest_path(root, run_id)


def _entry(
    *,
    run_id: str,
    wave: str,
    owner: str,
    path: str,
    initial_sha256: str = "ABSENT",
    final_sha256: str = "d" * 64,
) -> dict:
    return {
        "repository_id": "OMG",
        "run_id": run_id,
        "wave": wave,
        "owner": owner,
        "path": path,
        "initial_sha256": initial_sha256,
        "final_sha256": final_sha256,
        "reason": "exercise authoritative handoff engine",
        "proposal_id": f"{wave}-fixture",
        "targeted_test": {
            "argv": ["python3", "-m", "pytest", "-q", "tests/test_run_manifest.py"],
            "rc": 0,
            "stdout_sha256": "e" * 64,
            "stderr_sha256": "f" * 64,
        },
    }


def _write_w6_request(
    root: Path,
    *,
    run_id: str,
    wave: str,
    name: str,
    payload: dict,
) -> tuple[str, bytes]:
    relative = f".omg/artifacts/dual-parity/{run_id}/{wave}/{name}"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    body = canonical_json_bytes(payload)
    path.write_bytes(body)
    os.chmod(path, DATA_FILE_MODE)
    return relative, body


def _advance(path: Path, current: dict, next_state: str, *, second: int) -> dict:
    return transition_run_manifest(
        path,
        expected_revision=current["revision"],
        expected_previous_manifest_hash=current["previous_manifest_hash"],
        expected_state=current["state"],
        next_state=next_state,
        expected_lease_generation=current["lease_generation"],
        updated_at=f"2026-07-22T00:00:{second:02d}Z",
    )


def _aggregate_binding(root: Path, path: Path, manifest: dict) -> dict:
    return {
        "repository_id": manifest["repository_id"],
        "run_id": manifest["run_id"],
        "run_manifest_path": str(path.relative_to(root)),
        "run_manifest_revision": manifest["revision"],
        "run_manifest_hash": sha256_hex(path.read_bytes()),
        "frozen_base_commit": manifest["frozen_base_commit"],
        "frozen_base_tree": manifest["frozen_base_tree"],
        "approved_branch": manifest["approved_branch"],
        "approved_remote": manifest["approved_remote"],
        "approved_remote_old_oid": manifest["approved_remote_old_oid"],
        "trust_root_path": manifest["trust_root_path"],
        "trust_root_hash": manifest["trust_root_hash"],
        "ownership_manifest_id": manifest["ownership_manifest_id"],
        "ownership_manifest_hash": manifest["ownership_manifest_hash"],
        "normative_artifact_hashes": copy.deepcopy(
            manifest["normative_artifact_hashes"]
        ),
        "claimed_release_channels": list(manifest["claimed_release_channels"]),
        "claimed_registries": copy.deepcopy(manifest["claimed_registries"]),
        "lease_generation": manifest["lease_generation"],
    }


def _generated_request_snapshots(root: Path) -> tuple[dict, dict, dict]:
    generator = (root / "scripts/generate_standalone_hook.py").read_bytes()
    deny = (root / "omg_cli/deny.py").read_bytes()
    common = (root / "hooks/bin/_common.py").read_bytes()
    deny_body = _deny_body_bytes(deny)
    hook_disabled = _extract_function_bytes(common, "hook_disabled")
    generator_row = {
        "path": "scripts/generate_standalone_hook.py",
        "full_bytes_sha256": sha256_hex(generator),
        "full_bytes_size": len(generator),
    }
    deny_row = {
        "path": "omg_cli/deny.py",
        "full_bytes_sha256": sha256_hex(deny),
        "full_bytes_size": len(deny),
        "post_import_body_sha256": sha256_hex(deny_body),
        "post_import_body_size": len(deny_body),
    }
    common_row = {
        "path": "hooks/bin/_common.py",
        "selector": "hook_disabled",
        "extracted_utf8_sha256": sha256_hex(hook_disabled),
        "extracted_utf8_size": len(hook_disabled),
    }
    return generator_row, deny_row, common_row


def _write_generated_requests(
    root: Path,
    *,
    manifest: dict,
    w0_handoff_hash: str,
) -> dict[str, str]:
    generator, deny, common = _generated_request_snapshots(root)
    parent_hashes = [w0_handoff_hash]
    common_fields = {
        "schema_version": 1,
        "repository_id": "OMG",
        "run_id": manifest["run_id"],
        "frozen_base_commit": manifest["frozen_base_commit"],
        "frozen_base_tree": manifest["frozen_base_tree"],
        "ordered_input_slots": STANDALONE_INPUT_SLOTS,
        "parent_handoff_hashes": parent_hashes,
    }
    payloads = {
        "OMG-W1": {
            **common_fields,
            "store_kind": "generated_output_request",
            "wave": "OMG-W1",
            "owner": "omg-install-owner",
            "input_snapshot": {
                "generator": generator,
                "deny": deny,
                "common_hook_disabled": common,
            },
            "generator": {
                **generator,
                "interface": "standalone_hook_generator/1",
            },
        },
        "OMG-W2": {
            **common_fields,
            "store_kind": "generated_input_request",
            "wave": "OMG-W2",
            "owner": "omg-state-owner",
            "owned_inputs": [
                {"position": 2, **deny},
                {"position": 3, **common},
            ],
            "version_selector_request": {
                "path": "plugin.json",
                "json_pointer": "/version",
                "position": 4,
                "required_json_type": "string",
                "value_owner": "OMG-W6",
            },
        },
    }
    result: dict[str, str] = {}
    for wave, payload in payloads.items():
        filename = (
            "generated-output-request.json"
            if wave == "OMG-W1"
            else "generated-input-request.json"
        )
        key = (
            expected_trust_root(root, manifest["run_id"]) / "keys" / f"{wave}.hmac"
        ).read_bytes()
        envelope = {
            "signed_payload": payload,
            "signature": hmac_sha256_hex(key, HANDOFF_DOMAIN, payload),
        }
        relative, _body = _write_w6_request(
            root,
            run_id=manifest["run_id"],
            wave=wave,
            name=filename,
            payload=envelope,
        )
        result[wave] = relative
    return result


def _emit_authenticated_six_wave_chain(
    root: Path,
    path: Path,
    manifest: dict,
) -> tuple[dict[str, dict], dict[str, Path], Path]:
    handoffs: dict[str, dict] = {}
    product_paths: dict[str, Path] = {}
    request_relative, _request_body = _write_w6_request(
        root,
        run_id=manifest["run_id"],
        wave="OMG-W0",
        name="aggregate-request.json",
        payload={"schema": "aggregate-request/1", "value": 1},
    )
    generated_requests: dict[str, str] = {}
    for index, row in enumerate(manifest["ordered_owners"]):
        wave = row["wave"]
        entries: list[dict] = []
        for relative_path in FIXTURE_OWNED_PATHS[wave]:
            body = (root / relative_path).read_bytes()
            entries.append(
                _entry(
                    run_id=manifest["run_id"],
                    wave=wave,
                    owner=row["owner"],
                    path=relative_path,
                    initial_sha256=sha256_hex(body),
                    final_sha256=sha256_hex(body),
                )
            )
        product_paths[wave] = root / FIXTURE_OWNED_PATHS[wave][0]
        request_paths: list[str] = []
        if index == 0:
            request_paths.append(request_relative)
        if wave in generated_requests:
            request_paths.append(generated_requests[wave])
        handoffs[wave] = emit_owner_handoff(
            path,
            wave=wave,
            owner=row["owner"],
            proposal_entries=entries,
            w6_request_paths=request_paths,
            created_at=f"2026-07-22T00:00:{10 + index:02d}Z",
        )
        if index == 0:
            generated_requests = _write_generated_requests(
                root,
                manifest=manifest,
                w0_handoff_hash=handoffs[wave]["handoff_hash"],
            )
    return handoffs, product_paths, root / request_relative


def _input_aggregate_payload(
    root: Path,
    path: Path,
    manifest: dict,
    handoffs: dict[str, dict],
) -> dict:
    roots: list[dict] = []
    accepted: list[dict] = []
    path_roots: list[dict] = []
    for index in range(6):
        wave = f"OMG-W{index}"
        proposal_path = Path(handoffs[wave]["proposal_index_path"])
        handoff_path = Path(handoffs[wave]["handoff_path"])
        proposal_payload = json.loads(proposal_path.read_bytes())["signed_payload"]
        handoff_payload = json.loads(handoff_path.read_bytes())["signed_payload"]
        path_test_root = sha256_hex(canonical_json_bytes(proposal_payload["entries"]))
        requests = copy.deepcopy(proposal_payload["w6_requests"])
        roots.append(
            {
                "wave": wave,
                "owner": proposal_payload["owner"],
                "proposal_index_path": str(proposal_path.relative_to(root)),
                "proposal_index_hash": handoffs[wave]["proposal_index_hash"],
                "handoff_path": str(handoff_path.relative_to(root)),
                "handoff_hash": handoffs[wave]["handoff_hash"],
                "dependency_parent_handoff_hashes": list(
                    handoff_payload["parent_handoff_hashes"]
                ),
                "path_test_root": path_test_root,
                "w6_requests": requests,
            }
        )
        path_roots.append({"wave": wave, "path_test_root": path_test_root})
        accepted.extend({"wave": wave, **request} for request in requests)
    return {
        "store_kind": "repo_aggregate_input",
        "schema_version": 1,
        **_aggregate_binding(root, path, manifest),
        "ordered_owner_roots": roots,
        "parent_handoff_hashes": [row["handoff_hash"] for row in roots],
        "path_test_merkle_root": sha256_hex(canonical_json_bytes(path_roots)),
        "accepted_w6_proposals": accepted,
        "final_commit": None,
    }


def _freeze_final_candidate(
    root: Path, *, manifest: dict, semver: str
) -> tuple[str, str, str]:
    plugin_path = root / "plugin.json"
    plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
    if plugin.get("version") != semver:
        plugin["version"] = semver
        plugin_path.write_bytes(canonical_json_bytes(plugin))
        (root / "omg_capabilities.lock.json").write_bytes(
            canonical_json_bytes({"version": semver})
        )
        generated = subprocess.run(
            [sys.executable, "scripts/generate_standalone_hook.py"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert generated.returncode == 0, generated.stderr
        subprocess.run(
            [
                "git",
                "add",
                "plugin.json",
                "omg_capabilities.lock.json",
                "hooks/bin/omg_pretool_deny_standalone.py",
            ],
            cwd=root,
            check=True,
        )
        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-07-22T00:10:00Z",
            "GIT_COMMITTER_DATE": "2026-07-22T00:10:00Z",
        }
        subprocess.run(
            ["git", "commit", "-qm", f"fixture candidate {semver}"],
            cwd=root,
            env=env,
            check=True,
        )
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True
    ).strip()
    records = verify_final_candidate(
        root,
        base_commit=manifest["frozen_base_commit"],
        candidate_commit=commit,
        ownership=OMG_OWNER_PATTERNS,
    )
    return commit, tree, sha256_hex(canonical_json_bytes(records))


def _write_release_bundle(
    root: Path,
    *,
    run_id: str,
    final_commit: str,
    final_tree: str,
    semver: str,
) -> tuple[str, str, dict]:
    relative = (
        f".omg/artifacts/dual-parity/{run_id}/OMG-W6/release-bundle-manifest.json"
    )
    bundle_directory = str(Path(relative).parent / "release-bundle")
    bundle_path = root / bundle_directory
    bundle_path.mkdir(parents=True, exist_ok=True)
    payload_name = f"oh-my-grok-{semver}.tar.gz"
    payload_body = b"fixture immutable release bytes\n"
    payload_sha = sha256_hex(payload_body)
    checksum_body = f"{payload_sha}  {payload_name}\n".encode()
    (bundle_path / payload_name).write_bytes(payload_body)
    (bundle_path / "SHA256SUMS").write_bytes(checksum_body)
    assets = [
        {
            "name": payload_name,
            "relative_path": f"{bundle_directory}/{payload_name}",
            "byte_length": len(payload_body),
            "sha256": payload_sha,
            "media_type": "application/gzip",
        },
        {
            "name": "SHA256SUMS",
            "relative_path": f"{bundle_directory}/SHA256SUMS",
            "byte_length": len(checksum_body),
            "sha256": sha256_hex(checksum_body),
            "media_type": "text/plain",
        },
    ]
    manifest: dict = {
        "store_kind": "release_bundle_manifest",
        "schema_version": 1,
        "repository_id": "OMG",
        "run_id": run_id,
        "owner": "OMG-W6",
        "candidate_commit": final_commit,
        "candidate_tree": final_tree,
        "semver": semver,
        "bundle_directory": bundle_directory,
        "public_upload_order": [payload_name, "SHA256SUMS"],
        "assets": assets,
        "checksum": {
            "name": "SHA256SUMS",
            "payload_name": payload_name,
            "payload_sha256": payload_sha,
            "bytes_utf8": checksum_body.decode("utf-8"),
            "byte_length": len(checksum_body),
            "sha256": sha256_hex(checksum_body),
        },
        "build_receipt": {},
        "registry_bindings": [],
        "release_asset_root": sha256_hex(canonical_json_bytes(assets)),
    }
    manifest["build_receipt"] = (
        run_manifest_contract._expected_current_build_receipt(
            root,
            repository_id="OMG",
            candidate_commit=final_commit,
            semver=semver,
            bundle=manifest,
        )
    )
    manifest_path = root / relative
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    os.chmod(manifest_path, DATA_FILE_MODE)
    return relative, sha256_hex(manifest_path.read_bytes()), manifest


def _final_aggregate_payload(
    root: Path,
    path: Path,
    manifest: dict,
    input_envelope: dict,
) -> dict:
    run_id = manifest["run_id"]
    semver = "0.6.0"
    final_commit, final_tree, complete_delta_root = _freeze_final_candidate(
        root, manifest=manifest, semver=semver
    )
    bundle_path, bundle_hash, bundle = _write_release_bundle(
        root,
        run_id=run_id,
        final_commit=final_commit,
        final_tree=final_tree,
        semver=semver,
    )
    return {
        "store_kind": "repo_aggregate_final",
        "schema_version": 1,
        **_aggregate_binding(root, path, manifest),
        "input_envelope": input_envelope,
        "input_aggregate_hash": input_envelope["payload_hash"],
        "final_commit": final_commit,
        "final_tree": final_tree,
        "pushed_oid": final_commit,
        "complete_delta_root": complete_delta_root,
        "semver": semver,
        "deterministic_proof_hash": "1" * 64,
        "live_proof_hash": "2" * 64,
        "code_review_proof_hash": "3" * 64,
        "ultraqa_proof_hash": "4" * 64,
        "release_nonce": "release-nonce-1",
        "release_bundle_manifest_path": bundle_path,
        "release_bundle_manifest_sha256": bundle_hash,
        "release_bundle_manifest_schema": "release_bundle_manifest/1",
        "public_upload_order": bundle["public_upload_order"],
        "release_asset_root": bundle["release_asset_root"],
        "generated_output_attestation": build_generated_output_attestation(
            path, input_envelope=input_envelope
        ),
    }


def test_manifest_bootstrap_creates_only_authoritative_path_and_seven_secret_keys(
    tmp_path: Path,
) -> None:
    manifest, path = _init(tmp_path)
    assert path.is_file() and mode_bits(path) == DATA_FILE_MODE
    assert mode_bits(path.parent) == MANAGED_DIR_MODE
    assert read_run_manifest(path, root=tmp_path) == manifest
    assert manifest["state"] == "initializing" and manifest["revision"] == 1
    assert manifest["ordered_owners"] == sorted(
        manifest["ordered_owners"], key=lambda row: int(row["wave"].split("W")[1])
    )
    assert manifest["aggregate_signer_id"] != manifest["aggregate_verifier_id"]

    trust = expected_trust_root(tmp_path, "manifest-test")
    key_paths = sorted((trust / "keys").glob("*.hmac"))
    assert len(key_paths) == 7
    assert all(
        path.stat().st_size == 32 and mode_bits(path) == DATA_FILE_MODE
        for path in key_paths
    )
    trust_body = (trust / "writer-trust.json").read_bytes()
    assert b'"coordinator_capabilities":[]' in trust_body
    for key_path in key_paths:
        assert key_path.read_bytes() not in canonical_json_bytes(manifest)

    with pytest.raises(FileExistsError):
        _init(tmp_path)


def test_omg_manifest_policy_is_github_only_registry_empty_and_oracle_exact(
    tmp_path: Path,
) -> None:
    cases: tuple[tuple[list[str], list[dict[str, str]], str, str], ...] = (
        (
            ["github", "npm"],
            [],
            OMG_OWNERSHIP_MANIFEST_HASH,
            "claimed_release_channels",
        ),
        (
            ["github"],
            [{"registry_id": "npmjs"}],
            OMG_OWNERSHIP_MANIFEST_HASH,
            "registries",
        ),
        (["github"], [], "0" * 64, "ownership manifest hash"),
    )
    for index, (channels, registries, ownership_hash, message) in enumerate(cases):
        case_root = tmp_path / f"case-{index}"
        with pytest.raises(ContractValidationError, match=message):
            initialize_run_manifest(
                case_root,
                repository_id="OMG",
                run_id=f"policy-run-{index}",
                frozen_base_commit="a" * 40,
                frozen_base_tree="b" * 40,
                approved_branch="main",
                approved_remote="origin",
                approved_remote_old_oid="a" * 40,
                normative_artifact_hashes=NORMATIVE_ARTIFACT_HASHES,
                ownership_manifest_hash=ownership_hash,
                claimed_release_channels=channels,
                claimed_registries=registries,
                created_at="2026-07-22T00:00:00Z",
            )
        assert not (case_root / ".omg").exists()


def test_path_test_proof_binds_base_current_rc_and_complete_ownership_oracle(
    tmp_path: Path,
) -> None:
    base_commit, _base_tree = _write_fixture_repository(tmp_path)
    relative = "docs/parity/omg-parity.json"
    body = (tmp_path / relative).read_bytes()
    entry = _entry(
        run_id="path-proof-run",
        wave="OMG-W0",
        owner="omg-contract-owner",
        path=relative,
        initial_sha256=sha256_hex(body),
        final_sha256=sha256_hex(body),
    )
    _verify_proposal_paths_current(tmp_path, [entry], base_commit=base_commit)
    bad_rc = copy.deepcopy(entry)
    bad_rc["targeted_test"]["rc"] = 1
    with pytest.raises(ContractValidationError, match="targeted_test must pass"):
        _verify_proposal_paths_current(tmp_path, [bad_rc], base_commit=base_commit)
    bad_initial = {**entry, "initial_sha256": "0" * 64}
    with pytest.raises(ContractValidationError, match="frozen-base bytes"):
        _verify_proposal_paths_current(tmp_path, [bad_initial], base_commit=base_commit)
    bad_final = {**entry, "final_sha256": "0" * 64}
    with pytest.raises(ContractValidationError, match="current bytes"):
        _verify_proposal_paths_current(tmp_path, [bad_final], base_commit=base_commit)

    entries_by_wave = {
        wave: [{"path": path} for path in paths]
        for wave, paths in FIXTURE_OWNED_PATHS.items()
    }
    manifest = {
        "repository_id": "OMG",
        "frozen_base_commit": base_commit,
    }
    _verify_complete_owner_path_union(
        tmp_path, manifest=manifest, entries_by_wave=entries_by_wave
    )
    incomplete = copy.deepcopy(entries_by_wave)
    incomplete["OMG-W0"].pop()
    with pytest.raises(ContractValidationError, match="ownership oracle"):
        _verify_complete_owner_path_union(
            tmp_path, manifest=manifest, entries_by_wave=incomplete
        )
    duplicate = copy.deepcopy(entries_by_wave)
    duplicate["OMG-W0"].append(copy.deepcopy(duplicate["OMG-W0"][0]))
    with pytest.raises(ContractValidationError, match="duplicate"):
        _verify_complete_owner_path_union(
            tmp_path, manifest=manifest, entries_by_wave=duplicate
        )

    unowned = tmp_path / "unowned-untracked.txt"
    unowned.write_text("must never disappear from the oracle\n", encoding="utf-8")
    with pytest.raises(ContractValidationError, match="exactly one owner"):
        _verify_complete_owner_path_union(
            tmp_path, manifest=manifest, entries_by_wave=entries_by_wave
        )
    unowned.unlink()

    owned = tmp_path / "agents" / "new-untracked.md"
    owned.write_text("owned but not present in the signed union\n", encoding="utf-8")
    with pytest.raises(ContractValidationError, match="ownership oracle"):
        _verify_complete_owner_path_union(
            tmp_path, manifest=manifest, entries_by_wave=entries_by_wave
        )


def test_input_owner_union_ignores_w6_dirty_paths_but_final_candidate_binds_them(
    tmp_path: Path,
) -> None:
    base_commit, _base_tree = _write_fixture_repository(tmp_path)
    entries_by_wave = {
        wave: [{"path": path} for path in paths]
        for wave, paths in FIXTURE_OWNED_PATHS.items()
    }
    w6_path = ".github/workflows/ci.yml"
    w6_destination = tmp_path / w6_path
    w6_destination.parent.mkdir(parents=True, exist_ok=True)
    w6_destination.write_text("name: changed by compositor\n", encoding="utf-8")

    for state in ("inputs_verified", "composition_active"):
        _verify_complete_owner_path_union(
            tmp_path,
            manifest={
                "repository_id": "OMG",
                "frozen_base_commit": base_commit,
                "state": state,
            },
            entries_by_wave=entries_by_wave,
        )

    subprocess.run(["git", "add", w6_path], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fixture composition"],
        cwd=tmp_path,
        check=True,
    )
    candidate_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    delta = verify_final_candidate(
        tmp_path,
        base_commit=base_commit,
        candidate_commit=candidate_commit,
        ownership=OMG_OWNER_PATTERNS,
    )
    assert any(
        row["owner"] == "OMG-W6" and row["path"] == w6_path for row in delta
    )


def test_manifest_cas_traverses_exact_frozen_state_chain(tmp_path: Path) -> None:
    current, path = _init(tmp_path)
    assert RUN_MANIFEST_STATE_SET == (
        "initializing",
        "writers_active",
        "inputs_verified",
        "composition_active",
        "signing_revoked",
        "release_active",
        "closed",
        "blocked",
    )
    for next_state in ("writers_active", "inputs_verified"):
        current = transition_run_manifest(
            path,
            expected_revision=current["revision"],
            expected_previous_manifest_hash=current["previous_manifest_hash"],
            expected_state=current["state"],
            next_state=next_state,
            expected_lease_generation=current["lease_generation"],
            updated_at=f"2026-07-22T00:00:{current['revision']:02d}Z",
        )
    assert current["state"] == "inputs_verified" and current["revision"] == 3
    with pytest.raises(ContractValidationError, match="aggregate handoff"):
        transition_run_manifest(
            path,
            expected_revision=current["revision"],
            expected_previous_manifest_hash=current["previous_manifest_hash"],
            expected_state="inputs_verified",
            next_state="composition_active",
            expected_lease_generation=current["lease_generation"],
        )
    assert read_run_manifest(path, root=tmp_path) == current


def test_manifest_rejects_stale_revision_wrong_path_and_trust_drift(
    tmp_path: Path,
) -> None:
    current, path = _init(tmp_path)
    with pytest.raises(ContractValidationError, match="revision CAS"):
        transition_run_manifest(
            path,
            expected_revision=99,
            expected_previous_manifest_hash=None,
            expected_state="initializing",
            next_state="writers_active",
            expected_lease_generation=1,
        )
    wrong = tmp_path / "copy.json"
    wrong.write_bytes(path.read_bytes())
    os.chmod(wrong, 0o600)
    with pytest.raises(ContractValidationError, match="authoritative path"):
        read_run_manifest(wrong, root=tmp_path)

    key = next(
        (expected_trust_root(tmp_path, current["run_id"]) / "keys").glob("*.hmac")
    )
    key.write_bytes(b"x" * 32)
    os.chmod(key, 0o600)
    with pytest.raises(ContractValidationError, match="digest mismatch"):
        read_run_manifest(path, root=tmp_path)


def test_repository_aggregate_signing_is_manifest_and_generation_fenced(
    tmp_path: Path,
) -> None:
    manifest, path = _init(tmp_path, run_id="aggregate-run")
    manifest = _advance(path, manifest, "writers_active", second=1)
    handoffs, _product_paths, _request_path = _emit_authenticated_six_wave_chain(
        tmp_path, path, manifest
    )
    manifest = _advance(path, manifest, "inputs_verified", second=2)
    input_payload = _input_aggregate_payload(tmp_path, path, manifest, handoffs)

    with pytest.raises(ContractValidationError, match="stale, revoked, or inactive"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"] + 1,
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=input_payload,
        )
    with pytest.raises(ContractValidationError, match="stale, revoked, or inactive"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"] + 1,
            phase="input",
            payload=input_payload,
        )
    with pytest.raises(ContractValidationError, match="stale, revoked, or inactive"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="final",
            payload=input_payload,
        )

    input_envelope = sign_repository_aggregate(
        path,
        expected_revision=manifest["revision"],
        expected_lease_generation=manifest["lease_generation"],
        phase="input",
        payload=input_payload,
    )
    assert input_envelope["signer_id"] == manifest["aggregate_signer_id"]
    assert input_envelope["aggregate_key_id"] == manifest["aggregate_key_id"]
    assert set(input_envelope) == {
        "algorithm",
        "signer_id",
        "aggregate_key_id",
        "payload_hash",
        "payload",
        "signature",
    }
    aggregate_key = (
        expected_trust_root(tmp_path, manifest["run_id"])
        / "keys"
        / "OMG-W6-aggregate.hmac"
    ).read_bytes()
    assert aggregate_key not in canonical_json_bytes(input_envelope)
    assert (
        verify_repository_aggregate(path, phase="input", envelope=input_envelope)
        == input_envelope["payload_hash"]
    )

    manifest = _advance(path, manifest, "composition_active", second=3)
    with pytest.raises(ContractValidationError, match="reserved for atomic final"):
        _advance(path, manifest, "signing_revoked", second=4)
    with pytest.raises(ContractValidationError, match="stale, revoked, or inactive"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=input_payload,
        )
    final_payload = _final_aggregate_payload(tmp_path, path, manifest, input_envelope)
    final_envelope = sign_repository_aggregate(
        path,
        expected_revision=manifest["revision"],
        expected_lease_generation=manifest["lease_generation"],
        phase="final",
        payload=final_payload,
    )
    assert (
        verify_repository_aggregate(path, phase="final", envelope=final_envelope)
        == final_envelope["payload_hash"]
    )
    manifest = read_run_manifest(path, root=tmp_path)
    assert manifest["state"] == "signing_revoked"
    aggregate_store = parse_canonical_json_bytes(
        expected_repository_aggregate_path(
            tmp_path, "OMG", manifest["run_id"]
        ).read_bytes()
    )
    assert aggregate_store["revision"] == 2
    assert aggregate_store["input_envelope"] == input_envelope
    assert aggregate_store["final_envelope"] == final_envelope
    predecessor = {
        **aggregate_store,
        "revision": 1,
        "previous_aggregate_hash": None,
        "final_envelope": None,
    }
    assert aggregate_store["previous_aggregate_hash"] == sha256_hex(
        canonical_json_bytes(predecessor)
    )
    with pytest.raises(ContractValidationError, match="stale, revoked, or inactive"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="final",
            payload=final_payload,
        )
    assert (
        verify_repository_aggregate(path, phase="input", envelope=input_envelope)
        == input_envelope["payload_hash"]
    )
    assert (
        verify_repository_aggregate(path, phase="final", envelope=final_envelope)
        == final_envelope["payload_hash"]
    )
    manifest = _advance(path, manifest, "release_active", second=5)
    with pytest.raises(ContractValidationError, match="reserved for verified release"):
        _advance(path, manifest, "closed", second=6)
    assert read_run_manifest(path, root=tmp_path)["state"] == "release_active"

    states = [
        "branch_readback_passed",
        "commit_proof_passed",
        "tag_readback_passed",
        "prerelease_readback_passed",
        *(
            f"asset-{sha256_hex(name)[:16]}.asset_readback_passed"
            for name in final_payload["public_upload_order"]
        ),
        "assets_readback_passed",
        "github_promotion_readback_passed",
        "github_latest_readback_passed",
        "verified_readback_passed",
        "final_readback_passed",
        "complete",
    ]
    records = []
    predecessor = "candidate_gates_passed"
    for index, state in enumerate(states):
        record = make_call_record(
            repository="OMG",
            semver=final_payload["semver"],
            frozen_commit=final_payload["final_commit"],
            transaction_nonce=final_payload["release_nonce"],
            step=f"step-{index}",
            state=state,
            allowed_predecessor=predecessor,
            attempt=1,
            redacted_external_locator="github:fixture",
            expected_identity={"state": state},
            expected_byte_digest=None,
            request={"state": state},
            prior_mutable_identity=None,
        )
        if state.endswith("_readback_passed"):
            record["object_digest"] = sha256_hex(state)
            record["readback_at"] = "2026-07-23T00:00:00Z"
        records.append(record)
        predecessor = state
    evidence = {
        "store_kind": "release_completion_evidence",
        "schema_version": 1,
        "repository_id": "OMG",
        "run_id": manifest["run_id"],
        "semver": final_payload["semver"],
        "frozen_commit": final_payload["final_commit"],
        "transaction_nonce": final_payload["release_nonce"],
        "transaction_identity_hash": release_transaction_identity_hash(
            "OMG",
            final_payload["semver"],
            final_payload["final_commit"],
            final_payload["release_nonce"],
        ),
        "release_active_manifest_sha256": sha256_hex(path.read_bytes()),
        "release_bundle_manifest_sha256": final_payload[
            "release_bundle_manifest_sha256"
        ],
        "final_state": "complete",
        "call_records": records,
        "verified_at": "2026-07-23T00:01:00Z",
    }
    forged = copy.deepcopy(evidence)
    forged["release_active_manifest_sha256"] = "f" * 64
    with pytest.raises(ContractValidationError, match="manifest hash mismatch"):
        finalize_release_run_manifest(
            path,
            expected_revision=manifest["revision"],
            expected_previous_manifest_hash=manifest["previous_manifest_hash"],
            expected_lease_generation=manifest["lease_generation"],
            evidence=forged,
        )
    completion_path = expected_release_completion_evidence_path(
        tmp_path, "OMG", manifest["run_id"]
    )
    assert not completion_path.exists()

    manifest = finalize_release_run_manifest(
        path,
        expected_revision=manifest["revision"],
        expected_previous_manifest_hash=manifest["previous_manifest_hash"],
        expected_lease_generation=manifest["lease_generation"],
        evidence=evidence,
        updated_at="2026-07-23T00:02:00Z",
    )
    assert manifest["state"] == "closed"
    assert mode_bits(completion_path) == IMMUTABLE_SOURCE_MODE
    assert read_run_manifest(path, root=tmp_path) == manifest

    completion_body = completion_path.read_bytes()
    os.chmod(completion_path, 0o600)
    completion_path.write_bytes(completion_body + b" ")
    with pytest.raises(ContractValidationError):
        read_run_manifest(path, root=tmp_path)


def test_final_sign_transaction_recovers_keyboard_interrupt_at_every_write_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    manifest, path = _init(baseline, run_id="transaction-run")
    manifest = _advance(path, manifest, "writers_active", second=1)
    handoffs, _products, _request = _emit_authenticated_six_wave_chain(
        baseline, path, manifest
    )
    manifest = _advance(path, manifest, "inputs_verified", second=2)
    input_payload = _input_aggregate_payload(baseline, path, manifest, handoffs)
    input_envelope = sign_repository_aggregate(
        path,
        expected_revision=manifest["revision"],
        expected_lease_generation=manifest["lease_generation"],
        phase="input",
        payload=input_payload,
    )
    manifest = _advance(path, manifest, "composition_active", second=3)
    final_payload = _final_aggregate_payload(
        baseline, path, manifest, input_envelope
    )
    aggregate_path = expected_repository_aggregate_path(
        baseline, "OMG", manifest["run_id"]
    )
    initial_manifest_body = path.read_bytes()
    initial_aggregate_body = aggregate_path.read_bytes()
    journal_path = run_manifest_contract._final_sign_transaction_path(
        baseline, "OMG", manifest["run_id"]
    )

    original_atomic_write = run_manifest_contract.atomic_write_bytes
    for target_name, timing in (
        ("journal", "before"),
        ("journal", "after"),
        ("aggregate", "before"),
        ("aggregate", "after"),
        ("manifest", "before"),
        ("manifest", "after"),
    ):
        targets = {
            "journal": journal_path,
            "aggregate": aggregate_path,
            "manifest": path,
        }
        tripped = False

        def interrupt_once(
            destination: Path | str,
            body: bytes,
            *,
            mode: int = DATA_FILE_MODE,
            replace: bool = True,
        ) -> Path:
            nonlocal tripped
            if not tripped and Path(destination).resolve() == targets[target_name].resolve():
                tripped = True
                if timing == "before":
                    raise KeyboardInterrupt(f"injected before {target_name}")
                original_atomic_write(
                    destination, body, mode=mode, replace=replace
                )
                raise KeyboardInterrupt(f"injected after {target_name}")
            return original_atomic_write(
                destination, body, mode=mode, replace=replace
            )

        monkeypatch.setattr(
            run_manifest_contract, "atomic_write_bytes", interrupt_once
        )
        with pytest.raises(KeyboardInterrupt, match=target_name):
            sign_repository_aggregate(
                path,
                expected_revision=manifest["revision"],
                expected_lease_generation=manifest["lease_generation"],
                phase="final",
                payload=final_payload,
            )
        monkeypatch.setattr(
            run_manifest_contract, "atomic_write_bytes", original_atomic_write
        )
        envelope = sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="final",
            payload=final_payload,
        )
        assert tripped
        assert read_run_manifest(path, root=baseline)["state"] == "signing_revoked"
        assert not targets["journal"].exists()
        assert (
            verify_repository_aggregate(
                path, phase="final", envelope=envelope
            )
            == envelope["payload_hash"]
        )
        original_atomic_write(path, initial_manifest_body, mode=DATA_FILE_MODE)
        original_atomic_write(
            aggregate_path, initial_aggregate_body, mode=DATA_FILE_MODE
        )


def test_repository_aggregate_rejects_identity_phase_domain_and_key_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, path = _init(tmp_path, run_id="aggregate-negative-run")
    manifest = _advance(path, manifest, "writers_active", second=1)
    handoffs, product_paths, request_path = _emit_authenticated_six_wave_chain(
        tmp_path, path, manifest
    )
    manifest = _advance(path, manifest, "inputs_verified", second=2)
    payload = _input_aggregate_payload(tmp_path, path, manifest, handoffs)

    omitted_input_field = copy.deepcopy(payload)
    omitted_input_field.pop("normative_artifact_hashes")
    with pytest.raises(
        ContractValidationError, match="input aggregate payload key mismatch"
    ):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=omitted_input_field,
        )

    extra_input_field = copy.deepcopy(payload)
    extra_input_field["unbound"] = True
    with pytest.raises(
        ContractValidationError, match="input aggregate payload key mismatch"
    ):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=extra_input_field,
        )

    manifest_policy_drift = copy.deepcopy(payload)
    manifest_policy_drift["approved_branch"] = "release"
    with pytest.raises(ContractValidationError, match="approved_branch"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=manifest_policy_drift,
        )

    nested_extra = copy.deepcopy(payload)
    nested_extra["ordered_owner_roots"][0]["unbound"] = True
    with pytest.raises(
        ContractValidationError, match=r"ordered_owner_roots\[0\].*key mismatch"
    ):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=nested_extra,
        )

    merkle_drift = copy.deepcopy(payload)
    merkle_drift["path_test_merkle_root"] = "0" * 64
    with pytest.raises(ContractValidationError, match="path_test_merkle_root differs"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=merkle_drift,
        )

    request_binding_drift = copy.deepcopy(payload)
    request_binding_drift["accepted_w6_proposals"][0]["sha256"] = "0" * 64
    with pytest.raises(
        ContractValidationError, match="exactly equal signed current W6 requests"
    ):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=request_binding_drift,
        )

    missing_root = copy.deepcopy(payload)
    missing_root["ordered_owner_roots"].pop()
    missing_root["parent_handoff_hashes"].pop()
    with pytest.raises(ContractValidationError, match="exactly six owner roots"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=missing_root,
        )

    substituted_root = copy.deepcopy(payload)
    substituted_root["ordered_owner_roots"][5]["handoff_hash"] = "0" * 64
    substituted_root["parent_handoff_hashes"][5] = "0" * 64
    with pytest.raises(ContractValidationError, match="authenticated W0-W5"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=substituted_root,
        )

    missing_proposal_path = Path(handoffs["OMG-W2"]["proposal_index_path"])
    missing_proposal_body = missing_proposal_path.read_bytes()
    missing_proposal_path.unlink()
    with pytest.raises(ContractValidationError, match="proposal index is missing"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=payload,
        )
    missing_proposal_path.write_bytes(missing_proposal_body)
    os.chmod(missing_proposal_path, DATA_FILE_MODE)

    missing_handoff_path = Path(handoffs["OMG-W5"]["handoff_path"])
    missing_handoff_body = missing_handoff_path.read_bytes()
    missing_handoff_path.unlink()
    with pytest.raises(ContractValidationError, match="handoff is missing"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=payload,
        )
    missing_handoff_path.write_bytes(missing_handoff_body)
    os.chmod(missing_handoff_path, DATA_FILE_MODE)

    stale_proposal_path = Path(handoffs["OMG-W0"]["proposal_index_path"])
    stale_proposal_body = stale_proposal_path.read_bytes()
    stale_proposal = json.loads(stale_proposal_body)
    stale_proposal["signed_payload"]["run_manifest_revision"] = manifest["revision"]
    stale_proposal["signed_payload"]["run_manifest_hash"] = sha256_hex(
        path.read_bytes()
    )
    stale_proposal["signed_payload"]["lease_generation"] = manifest["lease_generation"]
    owner_key = (
        expected_trust_root(tmp_path, manifest["run_id"]) / "keys" / "OMG-W0.hmac"
    ).read_bytes()
    stale_proposal["signature"] = hmac_sha256_hex(
        owner_key, HANDOFF_DOMAIN, stale_proposal["signed_payload"]
    )
    stale_proposal_path.write_bytes(canonical_json_bytes(stale_proposal))
    os.chmod(stale_proposal_path, DATA_FILE_MODE)
    with pytest.raises(ContractValidationError, match="run_manifest_revision"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=payload,
        )
    stale_proposal_path.write_bytes(stale_proposal_body)
    os.chmod(stale_proposal_path, DATA_FILE_MODE)

    product_path = product_paths["OMG-W3"]
    product_body = product_path.read_bytes()
    product_path.unlink()
    with pytest.raises(ContractValidationError, match="proposal path is missing"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=payload,
        )
    product_path.write_bytes(product_body)
    product_path.write_bytes(b"stale product bytes")
    with pytest.raises(ContractValidationError, match="current bytes differ"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=payload,
        )
    product_path.write_bytes(product_body)

    request_body = request_path.read_bytes()
    request_path.unlink()
    with pytest.raises(ContractValidationError, match="w6 request is missing"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=payload,
        )
    request_path.write_bytes(request_body)
    os.chmod(request_path, DATA_FILE_MODE)
    request_path.write_bytes(
        canonical_json_bytes({"schema": "aggregate-request/1", "value": 2})
    )
    with pytest.raises(ContractValidationError, match="current bytes differ"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=payload,
        )
    request_path.write_bytes(request_body)
    os.chmod(request_path, DATA_FILE_MODE)

    envelope = sign_repository_aggregate(
        path,
        expected_revision=manifest["revision"],
        expected_lease_generation=manifest["lease_generation"],
        phase="input",
        payload=payload,
    )
    aggregate_store_path = expected_repository_aggregate_path(
        tmp_path, "OMG", manifest["run_id"]
    )
    aggregate_store_body = aggregate_store_path.read_bytes()
    aggregate_store = parse_canonical_json_bytes(aggregate_store_body)

    assert (
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="input",
            payload=payload,
        )
        == envelope
    )
    conflicting_store = copy.deepcopy(aggregate_store)
    conflicting_store["input_envelope"]["signature"] = "0" * 64
    aggregate_store_path.write_bytes(canonical_json_bytes(conflicting_store))
    os.chmod(aggregate_store_path, DATA_FILE_MODE)
    try:
        with pytest.raises(ContractValidationError, match="conflicting repository"):
            sign_repository_aggregate(
                path,
                expected_revision=manifest["revision"],
                expected_lease_generation=manifest["lease_generation"],
                phase="input",
                payload=payload,
            )
    finally:
        aggregate_store_path.write_bytes(aggregate_store_body)
        os.chmod(aggregate_store_path, DATA_FILE_MODE)

    public_product = product_paths["OMG-W4"]
    public_product_body = public_product.read_bytes()
    public_product.write_bytes(b"post-sign public product drift\n")
    try:
        with pytest.raises(ContractValidationError, match="current bytes"):
            verify_repository_aggregate(path, phase="input", envelope=envelope)
    finally:
        public_product.write_bytes(public_product_body)

    def verify_tampered_input(candidate: dict, match: str) -> None:
        tampered_store = copy.deepcopy(aggregate_store)
        tampered_store["input_envelope"] = candidate
        aggregate_store_path.write_bytes(canonical_json_bytes(tampered_store))
        os.chmod(aggregate_store_path, DATA_FILE_MODE)
        try:
            with pytest.raises(ContractValidationError, match=match):
                verify_repository_aggregate(path, phase="input", envelope=candidate)
        finally:
            aggregate_store_path.write_bytes(aggregate_store_body)
            os.chmod(aggregate_store_path, DATA_FILE_MODE)

    wrong_signer = copy.deepcopy(envelope)
    wrong_signer["signer_id"] = manifest["aggregate_verifier_id"]
    verify_tampered_input(wrong_signer, "signer mismatch")

    wrong_key_id = copy.deepcopy(envelope)
    wrong_key_id["aggregate_key_id"] = "foreign-key"
    verify_tampered_input(wrong_key_id, "key ID mismatch")

    aggregate_key_path = (
        expected_trust_root(tmp_path, manifest["run_id"])
        / "keys"
        / "OMG-W6-aggregate.hmac"
    )
    aggregate_key = aggregate_key_path.read_bytes()
    owner_key = (
        expected_trust_root(tmp_path, manifest["run_id"]) / "keys" / "OMG-W0.hmac"
    ).read_bytes()

    owner_key_substitution = copy.deepcopy(envelope)
    owner_key_substitution["signature"] = hmac_sha256_hex(
        owner_key, INPUT_AGGREGATE_DOMAIN, owner_key_substitution["payload"]
    )
    verify_tampered_input(owner_key_substitution, "signature mismatch")

    cross_repository = copy.deepcopy(envelope)
    cross_repository["payload"]["repository_id"] = "OMA"
    cross_repository["payload_hash"] = sha256_hex(
        canonical_json_bytes(cross_repository["payload"])
    )
    cross_repository["signature"] = hmac_sha256_hex(
        aggregate_key, INPUT_AGGREGATE_DOMAIN, cross_repository["payload"]
    )
    verify_tampered_input(cross_repository, "repository_id")

    stale_binding = copy.deepcopy(envelope)
    stale_binding["payload"]["run_manifest_revision"] -= 1
    stale_binding["payload_hash"] = sha256_hex(
        canonical_json_bytes(stale_binding["payload"])
    )
    stale_binding["signature"] = hmac_sha256_hex(
        aggregate_key, INPUT_AGGREGATE_DOMAIN, stale_binding["payload"]
    )
    verify_tampered_input(stale_binding, "revision or lease is stale")

    other_manifest, other_path = _init(tmp_path, run_id="aggregate-other-run")
    other_manifest = _advance(other_path, other_manifest, "writers_active", second=1)
    _advance(other_path, other_manifest, "inputs_verified", second=2)
    with pytest.raises(ContractValidationError, match="aggregate handoff"):
        verify_repository_aggregate(other_path, phase="input", envelope=envelope)

    manifest = _advance(path, manifest, "composition_active", second=3)
    final_payload = _final_aggregate_payload(tmp_path, path, manifest, envelope)

    omitted_final_field = copy.deepcopy(final_payload)
    omitted_final_field.pop("ultraqa_proof_hash")
    with pytest.raises(
        ContractValidationError, match="final aggregate payload key mismatch"
    ):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="final",
            payload=omitted_final_field,
        )

    extra_final_field = copy.deepcopy(final_payload)
    extra_final_field["unbound"] = True
    with pytest.raises(
        ContractValidationError, match="final aggregate payload key mismatch"
    ):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="final",
            payload=extra_final_field,
        )

    final_claim_drift = copy.deepcopy(final_payload)
    final_claim_drift["claimed_release_channels"] = ["github", "foreign"]
    with pytest.raises(ContractValidationError, match="claimed_release_channels"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="final",
            payload=final_claim_drift,
        )

    final_input_drift = copy.deepcopy(final_payload)
    final_input_drift["input_aggregate_hash"] = "0" * 64
    with pytest.raises(
        ContractValidationError, match="does not preserve input envelope"
    ):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="final",
            payload=final_input_drift,
        )

    upload_order_drift = copy.deepcopy(final_payload)
    upload_order_drift["public_upload_order"].reverse()
    with pytest.raises(ContractValidationError, match="public_upload_order"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="final",
            payload=upload_order_drift,
        )

    attestation_extra = copy.deepcopy(final_payload)
    attestation_extra["generated_output_attestation"]["unbound"] = True
    with pytest.raises(
        ContractValidationError, match="generated_output_attestation key mismatch"
    ):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="final",
            payload=attestation_extra,
        )

    attestation_drift = copy.deepcopy(final_payload)
    attestation_drift["generated_output_attestation"]["second_output_hash"] = "0" * 64
    with pytest.raises(ContractValidationError, match="not deterministic"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="final",
            payload=attestation_drift,
        )

    head_drift = copy.deepcopy(final_payload)
    head_drift["final_commit"] = head_drift["pushed_oid"] = "6" * 40
    with pytest.raises(ContractValidationError, match="current git HEAD"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="final",
            payload=head_drift,
        )

    tree_drift = copy.deepcopy(final_payload)
    tree_drift["final_tree"] = "6" * 40
    with pytest.raises(ContractValidationError, match="current git HEAD tree"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="final",
            payload=tree_drift,
        )

    delta_drift = copy.deepcopy(final_payload)
    delta_drift["complete_delta_root"] = "0" * 64
    with pytest.raises(ContractValidationError, match="final git delta"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="final",
            payload=delta_drift,
        )

    candidate_head = final_payload["final_commit"]
    (tmp_path / "omg_capabilities.lock.json").write_bytes(
        canonical_json_bytes({"version": "0.6.0", "second_parent": True})
    )
    subprocess.run(
        ["git", "add", "omg_capabilities.lock.json"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "commit", "-qm", "invalid second candidate"],
        cwd=tmp_path,
        check=True,
    )
    parent_drift = copy.deepcopy(final_payload)
    parent_drift["final_commit"] = parent_drift["pushed_oid"] = (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
        ).strip()
    )
    parent_drift["final_tree"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=tmp_path, text=True
    ).strip()
    try:
        with pytest.raises(ContractValidationError, match="exactly frozen_base_commit"):
            sign_repository_aggregate(
                path,
                expected_revision=manifest["revision"],
                expected_lease_generation=manifest["lease_generation"],
                phase="final",
                payload=parent_drift,
            )
    finally:
        subprocess.run(
            ["git", "reset", "--hard", "-q", candidate_head],
            cwd=tmp_path,
            check=True,
        )

    subprocess.run(
        [
            "git",
            "push",
            "-q",
            "--force",
            "origin",
            f"{candidate_head}:refs/heads/main",
        ],
        cwd=tmp_path,
        check=True,
    )
    try:
        with pytest.raises(ContractValidationError, match="remote old OID drifted"):
            sign_repository_aggregate(
                path,
                expected_revision=manifest["revision"],
                expected_lease_generation=manifest["lease_generation"],
                phase="final",
                payload=final_payload,
            )
    finally:
        subprocess.run(
            [
                "git",
                "push",
                "-q",
                "--force",
                "origin",
                f"{manifest['frozen_base_commit']}:refs/heads/main",
            ],
            cwd=tmp_path,
            check=True,
        )

    plugin_path = tmp_path / "plugin.json"
    plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
    plugin["version"] = "0.6.1"
    plugin_path.write_bytes(canonical_json_bytes(plugin))
    subprocess.run(["git", "add", "plugin.json"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "--amend", "--no-edit", "-q"],
        cwd=tmp_path,
        check=True,
    )
    plugin_drift = copy.deepcopy(final_payload)
    plugin_drift["final_commit"] = plugin_drift["pushed_oid"] = (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
        ).strip()
    )
    plugin_drift["final_tree"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=tmp_path, text=True
    ).strip()
    plugin_drift["complete_delta_root"] = sha256_hex(
        canonical_json_bytes(
            verify_final_candidate(
                tmp_path,
                base_commit=manifest["frozen_base_commit"],
                candidate_commit=plugin_drift["final_commit"],
                ownership=OMG_OWNER_PATTERNS,
            )
        )
    )
    try:
        with pytest.raises(ContractValidationError, match="plugin.json version"):
            sign_repository_aggregate(
                path,
                expected_revision=manifest["revision"],
                expected_lease_generation=manifest["lease_generation"],
                phase="final",
                payload=plugin_drift,
            )
    finally:
        subprocess.run(
            ["git", "reset", "--hard", "-q", candidate_head],
            cwd=tmp_path,
            check=True,
        )

    bundle_manifest_path = tmp_path / final_payload["release_bundle_manifest_path"]
    os.chmod(bundle_manifest_path, 0o644)
    try:
        with pytest.raises(ContractValidationError, match="0600"):
            sign_repository_aggregate(
                path,
                expected_revision=manifest["revision"],
                expected_lease_generation=manifest["lease_generation"],
                phase="final",
                payload=final_payload,
            )
    finally:
        os.chmod(bundle_manifest_path, DATA_FILE_MODE)

    bundle_body = bundle_manifest_path.read_bytes()
    bundle = parse_canonical_json_bytes(bundle_body)
    bundle["candidate_commit"] = "6" * 40
    bundle_manifest_path.write_bytes(canonical_json_bytes(bundle))
    os.chmod(bundle_manifest_path, DATA_FILE_MODE)
    candidate_drift = copy.deepcopy(final_payload)
    candidate_drift["release_bundle_manifest_sha256"] = sha256_hex(
        bundle_manifest_path.read_bytes()
    )
    try:
        with pytest.raises(ContractValidationError, match="candidate_commit"):
            sign_repository_aggregate(
                path,
                expected_revision=manifest["revision"],
                expected_lease_generation=manifest["lease_generation"],
                phase="final",
                payload=candidate_drift,
            )
    finally:
        bundle_manifest_path.write_bytes(bundle_body)
        os.chmod(bundle_manifest_path, DATA_FILE_MODE)

    receipt_variants: list[dict] = []
    for field in ("argv", "cwd", "toolchain", "environment", "epoch", "locale"):
        variant = copy.deepcopy(parse_canonical_json_bytes(bundle_body))
        receipt = variant["build_receipt"]
        if field == "argv":
            receipt["argv"].append("--rebuild")
        elif field == "cwd":
            receipt["cwd_realpath_hash"] = "0" * 64
        elif field == "toolchain":
            receipt["toolchain"][0]["binary_sha256"] = "0" * 64
        elif field == "environment":
            receipt["environment_value_hashes"]["LC_ALL"] = "0" * 64
        elif field == "epoch":
            receipt["SOURCE_DATE_EPOCH"] += 1
        else:
            receipt["locale"] = "C"
        receipt["receipt_hash"] = sha256_hex(
            canonical_json_bytes(
                {key: value for key, value in receipt.items() if key != "receipt_hash"}
            )
        )
        receipt_variants.append(variant)
    for variant in receipt_variants:
        bundle_manifest_path.write_bytes(canonical_json_bytes(variant))
        os.chmod(bundle_manifest_path, DATA_FILE_MODE)
        receipt_drift = copy.deepcopy(final_payload)
        receipt_drift["release_bundle_manifest_sha256"] = sha256_hex(
            bundle_manifest_path.read_bytes()
        )
        with pytest.raises(ContractValidationError, match="canonical live tools"):
            sign_repository_aggregate(
                path,
                expected_revision=manifest["revision"],
                expected_lease_generation=manifest["lease_generation"],
                phase="final",
                payload=receipt_drift,
            )
    bundle_manifest_path.write_bytes(bundle_body)
    os.chmod(bundle_manifest_path, DATA_FILE_MODE)

    archive_path = (
        bundle_manifest_path.parent
        / "release-bundle"
        / final_payload["public_upload_order"][0]
    )
    archive_body = archive_path.read_bytes()
    archive_path.write_bytes(b"drifted release asset\n")
    try:
        with pytest.raises(ContractValidationError, match="asset byte drift"):
            sign_repository_aggregate(
                path,
                expected_revision=manifest["revision"],
                expected_lease_generation=manifest["lease_generation"],
                phase="final",
                payload=final_payload,
            )
    finally:
        archive_path.write_bytes(archive_body)

    generated_path = tmp_path / "hooks/bin/omg_pretool_deny_standalone.py"
    generated_body = generated_path.read_bytes()
    generated_path.write_bytes(generated_body + b"# drift\n")
    try:
        with pytest.raises(ContractValidationError, match="clean residual"):
            sign_repository_aggregate(
                path,
                expected_revision=manifest["revision"],
                expected_lease_generation=manifest["lease_generation"],
                phase="final",
                payload=final_payload,
            )
    finally:
        generated_path.write_bytes(generated_body)

    generated_request_path = (
        tmp_path / f".omg/artifacts/dual-parity/{manifest['run_id']}/OMG-W1/"
        "generated-output-request.json"
    )
    generated_request_body = generated_request_path.read_bytes()
    generated_request_path.write_bytes(generated_request_body + b"\n")
    try:
        with pytest.raises(ContractValidationError, match="canonical JSON"):
            sign_repository_aggregate(
                path,
                expected_revision=manifest["revision"],
                expected_lease_generation=manifest["lease_generation"],
                phase="final",
                payload=final_payload,
            )
    finally:
        generated_request_path.write_bytes(generated_request_body)
        os.chmod(generated_request_path, DATA_FILE_MODE)

    original_atomic_write = run_manifest_contract.atomic_write_bytes

    def fail_manifest_commit(
        destination: Path | str,
        body: bytes,
        *,
        mode: int = DATA_FILE_MODE,
        replace: bool = True,
    ) -> Path:
        if Path(destination).resolve() == path.resolve():
            raise OSError("injected manifest commit failure")
        return original_atomic_write(destination, body, mode=mode, replace=replace)

    monkeypatch.setattr(
        run_manifest_contract, "atomic_write_bytes", fail_manifest_commit
    )
    with pytest.raises(OSError, match="injected manifest commit failure"):
        sign_repository_aggregate(
            path,
            expected_revision=manifest["revision"],
            expected_lease_generation=manifest["lease_generation"],
            phase="final",
            payload=final_payload,
        )
    monkeypatch.setattr(
        run_manifest_contract, "atomic_write_bytes", original_atomic_write
    )
    with pytest.raises(ContractValidationError, match="recovery is pending"):
        read_run_manifest(path, root=tmp_path)

    final_envelope = sign_repository_aggregate(
        path,
        expected_revision=manifest["revision"],
        expected_lease_generation=manifest["lease_generation"],
        phase="final",
        payload=final_payload,
    )
    assert read_run_manifest(path, root=tmp_path)["state"] == "signing_revoked"
    recovered = parse_canonical_json_bytes(aggregate_store_path.read_bytes())
    assert recovered["revision"] == 2
    assert recovered["final_envelope"] == final_envelope
    assert not run_manifest_contract._final_sign_transaction_path(
        tmp_path, "OMG", manifest["run_id"]
    ).exists()
    with pytest.raises(ContractValidationError):
        verify_repository_aggregate(path, phase="final", envelope=envelope)
    with pytest.raises(ContractValidationError):
        verify_repository_aggregate(path, phase="input", envelope=final_envelope)
    wrong_domain = copy.deepcopy(final_envelope)
    wrong_domain["signature"] = hmac_sha256_hex(
        aggregate_key, INPUT_AGGREGATE_DOMAIN, wrong_domain["payload"]
    )
    final_store_body = aggregate_store_path.read_bytes()
    final_store = parse_canonical_json_bytes(final_store_body)
    final_store["final_envelope"] = wrong_domain
    aggregate_store_path.write_bytes(canonical_json_bytes(final_store))
    os.chmod(aggregate_store_path, DATA_FILE_MODE)
    try:
        with pytest.raises(ContractValidationError, match="signature mismatch"):
            verify_repository_aggregate(path, phase="final", envelope=wrong_domain)
    finally:
        aggregate_store_path.write_bytes(final_store_body)
        os.chmod(aggregate_store_path, DATA_FILE_MODE)
    assert INPUT_AGGREGATE_DOMAIN != FINAL_AGGREGATE_DOMAIN

    aggregate_key_path.write_bytes(owner_key)
    os.chmod(aggregate_key_path, DATA_FILE_MODE)
    with pytest.raises(ContractValidationError, match="aggregate key digest mismatch"):
        verify_repository_aggregate(path, phase="final", envelope=final_envelope)


def test_repository_aggregate_cli_uses_canonical_0600_files_without_key_output(
    tmp_path: Path,
) -> None:
    manifest, path = _init(tmp_path, run_id="aggregate-cli-run")
    manifest = _advance(path, manifest, "writers_active", second=1)
    handoffs, _product_paths, _request_path = _emit_authenticated_six_wave_chain(
        tmp_path, path, manifest
    )
    manifest = _advance(path, manifest, "inputs_verified", second=2)
    artifact_dir = (
        tmp_path / ".omg" / "artifacts" / "dual-parity" / manifest["run_id"] / "OMG-W6"
    )
    artifact_dir.mkdir(parents=True)
    payload_path = artifact_dir / "input-payload.json"
    envelope_path = expected_repository_aggregate_path(
        tmp_path, "OMG", manifest["run_id"]
    )
    payload_path.write_bytes(
        canonical_json_bytes(
            _input_aggregate_payload(tmp_path, path, manifest, handoffs)
        )
    )
    os.chmod(payload_path, DATA_FILE_MODE)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    sign = subprocess.run(
        [
            sys.executable,
            "-m",
            "omg_cli.contracts.run_manifest",
            "sign-aggregate",
            "--path",
            str(path),
            "--phase",
            "input",
            "--expected-revision",
            str(manifest["revision"]),
            "--expected-lease-generation",
            str(manifest["lease_generation"]),
            "--input",
            str(payload_path),
            "--output",
            str(envelope_path),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert sign.returncode == 0, sign.stderr
    assert mode_bits(envelope_path) == DATA_FILE_MODE
    assert parse_canonical_json_bytes(envelope_path.read_bytes())
    sign_result = json.loads(sign.stdout)
    assert set(sign_result) == {"ok", "path", "payload_hash", "phase"}
    assert "hmac" not in sign.stdout.lower() and "key" not in sign.stdout.lower()

    verify = subprocess.run(
        [
            sys.executable,
            "-m",
            "omg_cli.contracts.run_manifest",
            "verify-aggregate",
            "--path",
            str(path),
            "--phase",
            "input",
            "--input",
            str(envelope_path),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stderr
    assert json.loads(verify.stdout)["payload_hash"] == sign_result["payload_hash"]

    os.chmod(payload_path, 0o644)
    bad_mode = subprocess.run(
        [
            sys.executable,
            "-m",
            "omg_cli.contracts.run_manifest",
            "sign-aggregate",
            "--path",
            str(path),
            "--phase",
            "input",
            "--expected-revision",
            str(manifest["revision"]),
            "--expected-lease-generation",
            str(manifest["lease_generation"]),
            "--input",
            str(payload_path),
            "--output",
            str(envelope_path),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_mode.returncode == 2
    assert "0600" in bad_mode.stderr

    payload_path.write_bytes(payload_path.read_bytes() + b"\n")
    os.chmod(payload_path, DATA_FILE_MODE)
    noncanonical = subprocess.run(
        [
            sys.executable,
            "-m",
            "omg_cli.contracts.run_manifest",
            "sign-aggregate",
            "--path",
            str(path),
            "--phase",
            "input",
            "--expected-revision",
            str(manifest["revision"]),
            "--expected-lease-generation",
            str(manifest["lease_generation"]),
            "--input",
            str(payload_path),
            "--output",
            str(envelope_path),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert noncanonical.returncode == 2
    assert "canonical JSON" in noncanonical.stderr

    payload_path.write_bytes(
        canonical_json_bytes(
            _input_aggregate_payload(tmp_path, path, manifest, handoffs)
        )
    )
    out_of_scope = subprocess.run(
        [
            sys.executable,
            "-m",
            "omg_cli.contracts.run_manifest",
            "sign-aggregate",
            "--path",
            str(path),
            "--phase",
            "input",
            "--expected-revision",
            str(manifest["revision"]),
            "--expected-lease-generation",
            str(manifest["lease_generation"]),
            "--input",
            str(payload_path),
            "--output",
            str(tmp_path / "outside-envelope.json"),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out_of_scope.returncode == 2
    assert "canonical repository aggregate handoff path" in out_of_scope.stderr
    assert not (tmp_path / "outside-envelope.json").exists()


def test_executable_cli_never_prints_hmac_key_bytes(tmp_path: Path) -> None:
    argv = [
        sys.executable,
        "-m",
        "omg_cli.contracts.run_manifest",
        "init",
        "--root",
        str(tmp_path),
        "--repository-id",
        "OMG",
        "--run-id",
        "cli-run",
        "--frozen-base-commit",
        "a" * 40,
        "--frozen-base-tree",
        "b" * 40,
        "--approved-branch",
        "main",
        "--approved-remote",
        "origin",
        "--approved-remote-old-oid",
        "a" * 40,
        "--ownership-manifest-hash",
        OMG_OWNERSHIP_MANIFEST_HASH,
    ]
    for name, digest in NORMATIVE_ARTIFACT_HASHES.items():
        argv.extend(["--artifact-hash", f"{name}={digest}"])
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    result = subprocess.run(
        argv, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output == {
        "manifest_hash": output["manifest_hash"],
        "ok": True,
        "path": str(expected_manifest_path(tmp_path, "cli-run")),
        "revision": 1,
        "state": "initializing",
    }
    assert "hmac" not in result.stdout.lower() and "key" not in result.stdout.lower()
    assert output["manifest_hash"] == sha256_hex(
        expected_manifest_path(tmp_path, "cli-run").read_bytes()
    )


def test_engine_emits_authenticated_w0_proposal_and_handoff_without_key_output(
    tmp_path: Path,
) -> None:
    current, path = _init(tmp_path, run_id="handoff-run")
    transition_run_manifest(
        path,
        expected_revision=1,
        expected_previous_manifest_hash=None,
        expected_state="initializing",
        next_state="writers_active",
        expected_lease_generation=1,
        updated_at="2026-07-22T00:00:01Z",
    )
    entry = _entry(
        run_id="handoff-run",
        wave="OMG-W0",
        owner="omg-contract-owner",
        path="omg_cli/contracts/run_manifest.py",
    )
    result = emit_owner_handoff(
        path,
        wave="OMG-W0",
        owner="omg-contract-owner",
        proposal_entries=[entry],
        parent_handoff_hashes=[],
        created_at="2026-07-22T00:00:02Z",
    )
    assert result["path_count"] == 1
    assert set(result) == {
        "proposal_index_path",
        "proposal_index_hash",
        "handoff_path",
        "handoff_hash",
        "path_count",
    }
    assert len(result["proposal_index_hash"]) == len(result["handoff_hash"]) == 64
    assert mode_bits(result["proposal_index_path"]) == DATA_FILE_MODE
    assert mode_bits(result["handoff_path"]) == DATA_FILE_MODE
    assert b"d" * 64 not in canonical_json_bytes(result)
    assert (
        emit_owner_handoff(
            path,
            wave="OMG-W0",
            owner="omg-contract-owner",
            proposal_entries=[entry],
            parent_handoff_hashes=[],
        )
        == result
    )


def test_engine_binds_sorted_current_w6_requests_into_proposal_and_handoff(
    tmp_path: Path,
) -> None:
    _, path = _init(tmp_path, run_id="request-run")
    transition_run_manifest(
        path,
        expected_revision=1,
        expected_previous_manifest_hash=None,
        expected_state="initializing",
        next_state="writers_active",
        expected_lease_generation=1,
        updated_at="2026-07-22T00:00:01Z",
    )
    second_path, second_body = _write_w6_request(
        tmp_path,
        run_id="request-run",
        wave="OMG-W0",
        name="b-request.json",
        payload={"schema": "fixture/v1", "value": 2},
    )
    first_path, first_body = _write_w6_request(
        tmp_path,
        run_id="request-run",
        wave="OMG-W0",
        name="a-request.json",
        payload={"schema": "fixture/v1", "value": 1},
    )
    entry = _entry(
        run_id="request-run",
        wave="OMG-W0",
        owner="omg-contract-owner",
        path="omg_cli/contracts/run_manifest.py",
    )
    result = emit_owner_handoff(
        path,
        wave="OMG-W0",
        owner="omg-contract-owner",
        proposal_entries=[entry],
        w6_request_paths=[second_path, first_path],
        created_at="2026-07-22T00:00:02Z",
    )
    proposal = json.loads(Path(result["proposal_index_path"]).read_bytes())
    handoff = json.loads(Path(result["handoff_path"]).read_bytes())
    assert proposal["signed_payload"]["w6_requests"] == [
        {
            "path": first_path,
            "byte_length": len(first_body),
            "sha256": sha256_hex(first_body),
        },
        {
            "path": second_path,
            "byte_length": len(second_body),
            "sha256": sha256_hex(second_body),
        },
    ]
    assert result["proposal_index_hash"] == sha256_hex(
        canonical_json_bytes(proposal["signed_payload"])
    )
    assert (
        handoff["signed_payload"]["proposal_index_hash"]
        == result["proposal_index_hash"]
    )
    assert (
        emit_owner_handoff(
            path,
            wave="OMG-W0",
            owner="omg-contract-owner",
            proposal_entries=[entry],
            w6_request_paths=[second_path, first_path],
        )
        == result
    )


def test_engine_rejects_invalid_w6_request_paths_bytes_modes_and_duplicates(
    tmp_path: Path,
) -> None:
    _, path = _init(tmp_path, run_id="bad-request-run")
    transition_run_manifest(
        path,
        expected_revision=1,
        expected_previous_manifest_hash=None,
        expected_state="initializing",
        next_state="writers_active",
        expected_lease_generation=1,
        updated_at="2026-07-22T00:00:01Z",
    )
    entry = _entry(
        run_id="bad-request-run",
        wave="OMG-W0",
        owner="omg-contract-owner",
        path="omg_cli/contracts/run_manifest.py",
    )
    good_path, _ = _write_w6_request(
        tmp_path,
        run_id="bad-request-run",
        wave="OMG-W0",
        name="good-request.json",
        payload={"schema": "fixture/v1"},
    )
    with pytest.raises(ContractValidationError, match="duplicate"):
        emit_owner_handoff(
            path,
            wave="OMG-W0",
            owner="omg-contract-owner",
            proposal_entries=[entry],
            w6_request_paths=[good_path, good_path],
        )
    with pytest.raises(ContractValidationError, match="confined"):
        emit_owner_handoff(
            path,
            wave="OMG-W0",
            owner="omg-contract-owner",
            proposal_entries=[entry],
            w6_request_paths=[
                ".omg/artifacts/dual-parity/bad-request-run/OMG-W1/foreign-request.json"
            ],
        )

    request_file = tmp_path / good_path
    os.chmod(request_file, 0o644)
    with pytest.raises(ContractValidationError, match="0600"):
        emit_owner_handoff(
            path,
            wave="OMG-W0",
            owner="omg-contract-owner",
            proposal_entries=[entry],
            w6_request_paths=[good_path],
        )
    request_file.write_bytes(canonical_json_bytes(["not", "an", "object"]))
    os.chmod(request_file, DATA_FILE_MODE)
    with pytest.raises(ContractValidationError, match="canonical JSON object"):
        emit_owner_handoff(
            path,
            wave="OMG-W0",
            owner="omg-contract-owner",
            proposal_entries=[entry],
            w6_request_paths=[good_path],
        )
    request_file.write_bytes(b'{"schema":"fixture/v1"}\n')
    with pytest.raises(ContractValidationError, match="canonical JSON"):
        emit_owner_handoff(
            path,
            wave="OMG-W0",
            owner="omg-contract-owner",
            proposal_entries=[entry],
            w6_request_paths=[good_path],
        )


def test_parent_verification_rejects_w6_request_drift_mode_and_unsigned_binding(
    tmp_path: Path,
) -> None:
    _, path = _init(tmp_path, run_id="request-drift-run")
    transition_run_manifest(
        path,
        expected_revision=1,
        expected_previous_manifest_hash=None,
        expected_state="initializing",
        next_state="writers_active",
        expected_lease_generation=1,
        updated_at="2026-07-22T00:00:01Z",
    )
    request_path, original_body = _write_w6_request(
        tmp_path,
        run_id="request-drift-run",
        wave="OMG-W0",
        name="w6-request.json",
        payload={"schema": "fixture/v1", "value": 1},
    )
    emit_owner_handoff(
        path,
        wave="OMG-W0",
        owner="omg-contract-owner",
        proposal_entries=[
            _entry(
                run_id="request-drift-run",
                wave="OMG-W0",
                owner="omg-contract-owner",
                path="omg_cli/contracts/writer_chain.py",
            )
        ],
        w6_request_paths=[request_path],
        created_at="2026-07-22T00:00:02Z",
    )
    w1_entry = _entry(
        run_id="request-drift-run",
        wave="OMG-W1",
        owner="omg-install-owner",
        path="install.sh",
    )
    request_file = tmp_path / request_path
    request_file.write_bytes(canonical_json_bytes({"schema": "fixture/v1", "value": 2}))
    with pytest.raises(ContractValidationError, match="current bytes"):
        emit_owner_handoff(
            path,
            wave="OMG-W1",
            owner="omg-install-owner",
            proposal_entries=[w1_entry],
        )

    request_file.write_bytes(original_body)
    os.chmod(request_file, 0o644)
    with pytest.raises(ContractValidationError, match="0600"):
        emit_owner_handoff(
            path,
            wave="OMG-W1",
            owner="omg-install-owner",
            proposal_entries=[w1_entry],
        )

    os.chmod(request_file, DATA_FILE_MODE)
    proposal_path = (
        tmp_path
        / ".omg/artifacts/dual-parity/request-drift-run/OMG-W0/proposal-index.json"
    )
    proposal = json.loads(proposal_path.read_bytes())
    proposal["signed_payload"]["w6_requests"][0]["sha256"] = "0" * 64
    proposal_path.write_bytes(canonical_json_bytes(proposal))
    os.chmod(proposal_path, DATA_FILE_MODE)
    with pytest.raises(ContractValidationError, match="signature"):
        emit_owner_handoff(
            path,
            wave="OMG-W1",
            owner="omg-install-owner",
            proposal_entries=[w1_entry],
        )


def test_nonroot_handoff_derives_actual_same_run_parent_envelopes(
    tmp_path: Path,
) -> None:
    _, path = _init(tmp_path, run_id="parent-run")
    transition_run_manifest(
        path,
        expected_revision=1,
        expected_previous_manifest_hash=None,
        expected_state="initializing",
        next_state="writers_active",
        expected_lease_generation=1,
        updated_at="2026-07-22T00:00:01Z",
    )
    w0 = emit_owner_handoff(
        path,
        wave="OMG-W0",
        owner="omg-contract-owner",
        proposal_entries=[
            _entry(
                run_id="parent-run",
                wave="OMG-W0",
                owner="omg-contract-owner",
                path="omg_cli/contracts/writer_chain.py",
            )
        ],
        created_at="2026-07-22T00:00:02Z",
    )
    with pytest.raises(ContractValidationError, match="asserted parent"):
        emit_owner_handoff(
            path,
            wave="OMG-W1",
            owner="omg-install-owner",
            proposal_entries=[
                _entry(
                    run_id="parent-run",
                    wave="OMG-W1",
                    owner="omg-install-owner",
                    path="install.sh",
                )
            ],
            parent_handoff_hashes=["0" * 64],
        )
    w1 = emit_owner_handoff(
        path,
        wave="OMG-W1",
        owner="omg-install-owner",
        proposal_entries=[
            _entry(
                run_id="parent-run",
                wave="OMG-W1",
                owner="omg-install-owner",
                path="install.sh",
            )
        ],
        parent_handoff_hashes=[w0["handoff_hash"]],
        created_at="2026-07-22T00:00:03Z",
    )
    payload = json.loads(Path(w1["handoff_path"]).read_text())["signed_payload"]
    assert payload["parent_handoff_hashes"] == [w0["handoff_hash"]]


def test_nonroot_handoff_rejects_missing_or_tampered_parent_artifact(
    tmp_path: Path,
) -> None:
    _, path = _init(tmp_path, run_id="tampered-parent-run")
    transition_run_manifest(
        path,
        expected_revision=1,
        expected_previous_manifest_hash=None,
        expected_state="initializing",
        next_state="writers_active",
        expected_lease_generation=1,
        updated_at="2026-07-22T00:00:01Z",
    )
    w1_entry = _entry(
        run_id="tampered-parent-run",
        wave="OMG-W1",
        owner="omg-install-owner",
        path="install.sh",
    )
    with pytest.raises(ContractValidationError, match="missing"):
        emit_owner_handoff(
            path,
            wave="OMG-W1",
            owner="omg-install-owner",
            proposal_entries=[w1_entry],
        )
    w0 = emit_owner_handoff(
        path,
        wave="OMG-W0",
        owner="omg-contract-owner",
        proposal_entries=[
            _entry(
                run_id="tampered-parent-run",
                wave="OMG-W0",
                owner="omg-contract-owner",
                path="omg_cli/contracts/run_manifest.py",
            )
        ],
        created_at="2026-07-22T00:00:02Z",
    )
    handoff_path = Path(w0["handoff_path"])
    tampered = json.loads(handoff_path.read_text())
    tampered["signature"] = "0" * 64
    handoff_path.write_bytes(canonical_json_bytes(tampered))
    os.chmod(handoff_path, DATA_FILE_MODE)
    with pytest.raises(ContractValidationError, match="signature"):
        emit_owner_handoff(
            path,
            wave="OMG-W1",
            owner="omg-install-owner",
            proposal_entries=[w1_entry],
        )
