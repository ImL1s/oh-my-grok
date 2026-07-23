"""Evidence boundary tests for Grok /create-workflow and Rhai."""
from __future__ import annotations

from pathlib import Path

import pytest

from omg_cli.workflows.grok_adapter import (
    NativeWorkflowUnsupported,
    assess_native_capability,
    project_to_rhai,
    safe_headless_probe,
)


def test_default_native_capability_is_optional_unclaimed(tmp_path: Path) -> None:
    result = assess_native_capability(tmp_path)
    assert result["status"] == "optional_unclaimed"
    assert result["semantic_claim"] is False


def test_bundled_rhai_file_alone_never_promotes_claim(tmp_path: Path) -> None:
    workflow_dir = tmp_path / ".grok" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "review.rhai").write_text("fn main() {}\n", encoding="utf-8")
    result = assess_native_capability(tmp_path)
    assert result["local_bundle_observed"] is True
    assert result["rhai_files"] == [".grok/workflows/review.rhai"]
    assert result["status"] == "optional_unclaimed"


def test_only_stable_public_schema_plus_fresh_slash_proof_can_promote(tmp_path: Path) -> None:
    partial = assess_native_capability(
        tmp_path,
        public_schema_evidence={"stable": True, "public": True, "schema_digest": "a" * 64},
    )
    assert partial["status"] == "optional_unclaimed"
    claimed = assess_native_capability(
        tmp_path,
        public_schema_evidence={"stable": True, "public": True, "schema_digest": "a" * 64},
        fresh_invocation_evidence={
            "fresh": True,
            "slash_command": "/create-workflow",
            "success": True,
            "output_digest": "b" * 64,
        },
    )
    assert claimed["status"] == "claimed"


def test_malformed_evidence_digests_never_promote(tmp_path: Path) -> None:
    result = assess_native_capability(
        tmp_path,
        public_schema_evidence={
            "stable": True,
            "public": True,
            "schema_digest": "not-a-digest",
        },
        fresh_invocation_evidence={
            "fresh": True,
            "slash_command": "/create-workflow",
            "success": True,
            "output_digest": "also-not-a-digest",
        },
    )
    assert result["status"] == "optional_unclaimed"
    assert result["semantic_claim"] is False


def test_arbitrary_rhai_projection_is_disabled() -> None:
    with pytest.raises(NativeWorkflowUnsupported, match="NATIVE_UNSUPPORTED"):
        project_to_rhai({"contract": "repository-workflow/v1"})


def test_missing_binary_headless_probe_is_honest(monkeypatch) -> None:
    monkeypatch.setattr("omg_cli.workflows.grok_adapter.shutil.which", lambda _name: None)
    result = safe_headless_probe()
    assert result["binary_found"] is False
    assert result["status"] == "optional_unclaimed"
