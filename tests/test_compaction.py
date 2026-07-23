from __future__ import annotations

from pathlib import Path
import json
import os

import pytest

from omg_cli.compaction import (
    CompactionError,
    create_compaction_checkpoint,
    load_compaction_checkpoint,
    render_resume_context,
)
from omg_cli.session_recovery import recover_session


def _recovery(tmp_path: Path) -> dict:
    source = tmp_path / "session.jsonl"
    rows = [
        {"event_id": "s", "prev_event_id": None, "type": "turn_start", "payload": {"turn_id": "t"}},
        {"event_id": "u", "prev_event_id": "s", "type": "user_message", "payload": {"turn_id": "t", "text": "u"}},
        {"event_id": "a", "prev_event_id": "u", "type": "assistant_message", "payload": {"turn_id": "t", "text": "a"}},
        {"event_id": "e", "prev_event_id": "a", "type": "turn_end", "payload": {"turn_id": "t"}},
    ]
    source.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    return recover_session(source, tmp_path / "recovery")


def test_checkpoint_roundtrip_preserves_guidance_receipts_and_recovery_metadata(tmp_path) -> None:
    guidance = b"<!-- OMX:RUNTIME:START -->\nexact guidance\n<!-- OMX:RUNTIME:END -->"
    recovered = _recovery(tmp_path)
    checkpoint = create_compaction_checkpoint(
        tmp_path,
        run_id="run-1",
        generation=3,
        guidance=guidance,
        receipts=[{"receipt_id": "r1", "Authorization": "Bearer secret"}],
        recovery_manifest=recovered["manifest"],
    )
    loaded = load_compaction_checkpoint(checkpoint["path"])
    assert render_resume_context(loaded)["guidance"] == guidance
    assert loaded["recovery_manifest"] == recovered["manifest"]
    assert loaded["recovery_receipt"]["receipt_sha256"] == recovered["receipt_sha256"]
    assert "secret" not in Path(checkpoint["path"]).read_text(encoding="utf-8")


def test_checkpoint_rejects_stale_generation_without_mutation(tmp_path) -> None:
    recovered = _recovery(tmp_path)
    first = create_compaction_checkpoint(
        tmp_path,
        run_id="run-1",
        generation=4,
        guidance=b"g",
        receipts=[],
        recovery_manifest=recovered["manifest"],
    )
    before = Path(first["path"]).read_bytes()
    with pytest.raises(CompactionError, match="stale generation"):
        create_compaction_checkpoint(
            tmp_path,
            run_id="run-1",
            generation=3,
            guidance=b"changed",
            receipts=[],
            recovery_manifest=recovered["manifest"],
        )
    assert Path(first["path"]).read_bytes() == before


def test_checkpoint_rejects_recovery_copy_manifest_receipt_drift_and_symlinks(
    tmp_path,
) -> None:
    recovered = _recovery(tmp_path)
    copy_path = Path(recovered["immutable_copy_path"])
    os.chmod(copy_path, 0o600)
    with pytest.raises(CompactionError, match="mode must be 0400"):
        create_compaction_checkpoint(
            tmp_path,
            run_id="run-drift",
            generation=1,
            guidance=b"g",
            receipts=[],
            recovery_manifest=recovered["manifest"],
        )
    os.chmod(copy_path, 0o400)

    receipt = Path(recovered["receipt_path"])
    original = receipt.read_bytes()
    os.chmod(receipt, 0o600)
    receipt.write_bytes(original + b" ")
    os.chmod(receipt, 0o400)
    with pytest.raises(CompactionError):
        create_compaction_checkpoint(
            tmp_path,
            run_id="run-drift",
            generation=1,
            guidance=b"g",
            receipts=[],
            recovery_manifest=recovered["manifest"],
        )
