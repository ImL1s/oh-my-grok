"""CLI surface for Visual Contract V1 compare/capture/verdict/ralph (#75)."""

from __future__ import annotations

import ast
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from omg_cli.cli_envelope import SCHEMA_VERSION
from omg_cli.command_registry import KNOWN_SUBCOMMANDS
from omg_cli.contracts.visual_contract import (
    COMPARISON_KIND,
    DIMENSION_IDS,
    RESULT_KIND,
    SCHEMA_VERSION as VISUAL_SCHEMA,
    compare,
)
from omg_cli.main import build_parser, main

FORBIDDEN_TOKENS = ("approved", "passes", "verified", "base64", "iVBOR")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "visual"
RUNTIME_PATH = Path(__file__).resolve().parents[1] / "omg_cli" / "visual_runtime.py"


def _image(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "path": "fixtures/visual/ref.png",
        "sha256": "a" * 64,
        "media_type": "image/png",
        "byte_size": 1024,
        "width": 200,
        "height": 200,
    }
    payload.update(overrides)
    return payload


def _compat(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "viewport_width": 1280,
        "viewport_height": 720,
        "dpr_milli": 2000,
        "platform": "macos",
        "theme": "dark",
        "locale": "en-US",
    }
    payload.update(overrides)
    return payload


def _dims(
    scores: list[int] | None = None,
    weights: list[int] | None = None,
) -> list[dict[str, Any]]:
    if scores is None:
        scores = [10000] * 10
    if weights is None:
        weights = [1000] * 10
    return [
        {"id": dim_id, "score": score, "weight": weight}
        for dim_id, score, weight in zip(DIMENSION_IDS, scores, weights, strict=True)
    ]


def _document(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": VISUAL_SCHEMA,
        "kind": COMPARISON_KIND,
        "reference": _image(),
        "candidate": _image(path="fixtures/visual/cur.png", sha256="b" * 64),
        "reference_compatibility": _compat(),
        "candidate_compatibility": _compat(),
        "dimensions": _dims(),
        "threshold": 8000,
        "masks": [],
        "task_criteria": "primary CTA visible",
    }
    payload.update(overrides)
    return payload


def _write_input(tmp_path: Path, document: dict[str, object]) -> Path:
    path = tmp_path / "comparison.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _out(capsys) -> dict[str, Any]:
    raw = capsys.readouterr().out
    assert raw.strip(), "expected JSON on stdout"
    return json.loads(raw)


def _assert_no_forbidden(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False)
    for token in FORBIDDEN_TOKENS:
        assert token not in encoded
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("approved", "passes", "verified"):
            assert key not in result


def test_visual_subcommand_registered() -> None:
    parser = build_parser()
    choices = None
    for act in parser._actions:
        if getattr(act, "dest", None) == "command" and hasattr(act, "choices"):
            choices = act.choices
            break
    assert choices is not None
    assert "visual" in choices
    assert "visual" in KNOWN_SUBCOMMANDS
    nested = None
    for a2 in choices["visual"]._actions:
        if getattr(a2, "choices", None) and "compare" in a2.choices:
            nested = set(a2.choices)
            break
    assert nested == {"compare", "capture", "verdict", "ralph"}


def test_cli_compare_golden_scored(tmp_path: Path, capsys) -> None:
    document = _document()
    path = _write_input(tmp_path, document)
    rc = main(["visual", "compare", "--input", str(path)])
    assert rc == 0
    payload = _out(capsys)
    expected = compare(document)
    assert payload["ok"] is True
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == "visual.compare"
    assert payload["result"] == expected
    assert payload["result"]["status"] == "scored"
    assert payload["result"]["kind"] == RESULT_KIND
    assert payload["result"]["aggregate"] == 10000
    assert payload["result"]["threshold"] == 8000
    _assert_no_forbidden(payload)


def test_cli_compare_blocked_compatibility(tmp_path: Path, capsys) -> None:
    document = _document(candidate_compatibility=_compat(theme="light"))
    path = _write_input(tmp_path, document)
    rc = main(["--json", "visual", "compare", "--input", str(path)])
    assert rc == 0
    payload = _out(capsys)
    expected = compare(document)
    assert payload["ok"] is True
    assert payload["command"] == "visual.compare"
    assert payload["result"] == expected
    assert payload["result"]["status"] == "blocked"
    assert payload["result"]["block_code"] == "compatibility_mismatch"
    assert payload["result"]["block_field"] == "theme"
    assert "aggregate" not in payload["result"]
    assert "threshold" not in payload["result"]
    _assert_no_forbidden(payload)


def test_cli_does_not_pass_fail_on_threshold(tmp_path: Path, capsys) -> None:
    document = _document(dimensions=_dims(scores=[0] * 10), threshold=8000)
    path = _write_input(tmp_path, document)
    rc = main(["visual", "compare", "--input", str(path)])
    assert rc == 0
    payload = _out(capsys)
    assert payload["ok"] is True
    assert payload["result"]["status"] == "scored"
    assert payload["result"]["aggregate"] == 0
    assert payload["result"]["threshold"] == 8000
    _assert_no_forbidden(payload)


def test_cli_refuses_forbidden_keys_on_stdout(tmp_path: Path, capsys) -> None:
    path = _write_input(tmp_path, _document())
    rc = main(["visual", "compare", "--input", str(path)])
    assert rc == 0
    raw = capsys.readouterr().out
    for token in FORBIDDEN_TOKENS:
        assert token not in raw


def test_cli_invalid_document_is_usage_exit(tmp_path: Path, capsys) -> None:
    document = _document(schema_version=2)
    path = _write_input(tmp_path, document)
    rc = main(["visual", "compare", "--input", str(path)])
    assert rc == 2
    payload = _out(capsys)
    assert payload["ok"] is False
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == "visual.compare"
    err = payload.get("error") or {}
    assert err.get("code") == "E_VISUAL_CONTRACT" or payload.get("error_code") == (
        "E_VISUAL_CONTRACT"
    )
    _assert_no_forbidden(payload)


def test_cli_unreadable_input_is_usage_exit(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "nope.json"
    rc = main(["visual", "compare", "--input", str(missing)])
    assert rc == 2
    payload = _out(capsys)
    assert payload["ok"] is False
    err = payload.get("error") or {}
    assert err.get("code") == "E_VISUAL_INPUT" or payload.get("error_code") == (
        "E_VISUAL_INPUT"
    )


def test_cli_malformed_json_is_usage_exit(tmp_path: Path, capsys) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    rc = main(["visual", "compare", "--input", str(path)])
    assert rc == 2
    payload = _out(capsys)
    assert payload["ok"] is False
    err = payload.get("error") or {}
    assert err.get("code") == "E_VISUAL_INPUT" or payload.get("error_code") == (
        "E_VISUAL_INPUT"
    )


def test_cli_missing_input_flag_is_usage_exit(capsys) -> None:
    rc = main(["visual", "compare"])
    assert rc == 2
    payload = _out(capsys)
    assert payload["ok"] is False
    err = payload.get("error") or {}
    assert err.get("code") == "E_USAGE" or payload.get("error_code") == "E_USAGE"


def test_cli_invalid_dimension_id_does_not_echo_payload(
    tmp_path: Path, capsys
) -> None:
    from omg_cli.commands.visual import CONTRACT_VALIDATION_MESSAGE

    payload_id = "iVBORw0KGgoAAAANSUhEUgAA" + ("A" * 64)
    document = _document()
    document["dimensions"][0]["id"] = payload_id  # type: ignore[index]
    path = _write_input(tmp_path, document)
    rc = main(["visual", "compare", "--input", str(path)])
    assert rc == 2
    payload = _out(capsys)
    err = payload.get("error") or {}
    assert err.get("code") == "E_VISUAL_CONTRACT" or payload.get("error_code") == (
        "E_VISUAL_CONTRACT"
    )
    dumped = json.dumps(payload, ensure_ascii=False)
    assert payload_id not in dumped
    assert "iVBORw0KGgo" not in dumped
    message = err.get("message") or payload.get("message") or ""
    assert message == CONTRACT_VALIDATION_MESSAGE
    _assert_no_forbidden(payload)


def test_cli_oversized_document_is_input_exit(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from omg_cli.commands import visual as visual_mod

    monkeypatch.setattr(visual_mod, "MAX_COMPARE_DOCUMENT_BYTES", 64)
    path = tmp_path / "padded.json"
    path.write_bytes(b"{" + (b" " * 80) + b"}")
    rc = main(["visual", "compare", "--input", str(path)])
    assert rc == 2
    payload = _out(capsys)
    err = payload.get("error") or {}
    assert err.get("code") == "E_VISUAL_INPUT" or payload.get("error_code") == (
        "E_VISUAL_INPUT"
    )
    assert "exceeds size limit" in (err.get("message") or payload.get("message") or "")


def test_cli_missing_compare_action_is_usage_exit(capsys) -> None:
    rc = main(["visual"])
    assert rc == 2
    out = capsys.readouterr()
    assert "usage: omg visual {compare,capture,verdict,ralph}" in out.err
    assert not out.out.strip()


def _seed(tmp_path: Path) -> None:
    shutil.copyfile(FIXTURES / "ref.png", tmp_path / "ref.png")
    shutil.copyfile(FIXTURES / "current.png", tmp_path / "current.png")


def _write_config(tmp_path: Path, payload: dict[str, Any], name: str = "visual.yaml") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _compat_cfg(**overrides: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "viewport_width": 1280,
        "viewport_height": 720,
        "dpr_milli": 1000,
        "platform": "windows",
        "theme": "light",
        "locale": "en-US",
        "width": 200,
        "height": 200,
        "task_criteria": "primary CTA visible",
        "editor_role": "omg-designer",
        "reviewer_role": "omg-vision",
        "reference": {
            "path": "ref.png",
            "width": 200,
            "height": 200,
            "media_type": "image/png",
        },
        "actual": {
            "path": "current.png",
            "width": 200,
            "height": 200,
            "media_type": "image/png",
        },
    }
    payload.update(overrides)
    return payload


def _argv(tmp_path: Path, *rest: str) -> list[str]:
    return ["--project-root", str(tmp_path), "--json", *rest]


def test_runtime_does_not_import_pixel_decoders() -> None:
    tree = ast.parse(RUNTIME_PATH.read_text(encoding="utf-8"))
    banned = {"PIL", "Pillow", "pillow", "playwright"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".", 1)[0] not in banned
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".", 1)[0] not in banned


def test_capture_fake_driver(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    config = _compat_cfg(
        capture={
            "command": [sys.executable, str(FIXTURES / "fake_capture.py")],
            "target": "about:blank",
            "readiness": "explicit",
        }
    )
    cfg = _write_config(tmp_path, config)
    rc = main(_argv(tmp_path, "visual", "capture", "--config", str(cfg), "--run-id", "cap1"))
    assert rc == 0
    payload = _out(capsys)
    assert payload["ok"] is True
    assert payload["command"] == "visual.capture"
    result = payload["result"]
    assert result["status"] == "captured"
    assert result["source"] == "config"
    assert result["pixel_decode"] is False
    assert result["playwright_required"] is False
    assert result["image"]["sha256"]
    assert result["image"]["path"].startswith(".omg/artifacts/visual/")
    _assert_no_forbidden(payload)
    assert (tmp_path / ".omg" / "artifacts" / "visual" / "cap1" / "current.png").is_file()


def test_capture_none_is_blocked_not_fake_pass(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    cfg = _write_config(tmp_path, _compat_cfg())
    rc = main(_argv(tmp_path, "visual", "capture", "--config", str(cfg), "--run-id", "capnone"))
    assert rc == 0
    payload = _out(capsys)
    result = payload["result"]
    assert result["status"] == "blocked"
    assert result["block_code"] == "capture_unavailable"
    assert result["source"] == "none"
    _assert_no_forbidden(payload)


def test_verdict_mismatched_dims_blocked(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    config = _compat_cfg(dimensions=_dims())
    config["actual"] = {
        "path": "current.png",
        "width": 200,
        "height": 100,
        "media_type": "image/png",
    }
    cfg = _write_config(tmp_path, config)
    rc = main(
        _argv(
            tmp_path,
            "visual",
            "verdict",
            "--config",
            str(cfg),
            "--reference",
            "ref.png",
            "--actual",
            "current.png",
            "--threshold",
            "90",
            "--run-id",
            "mismatch1",
        )
    )
    assert rc == 0
    payload = _out(capsys)
    result = payload["result"]
    assert result["status"] == "blocked"
    assert result["comparison"]["block_code"] == "image_dimension_mismatch"
    assert result["comparison"]["block_field"] == "image_height"
    assert result["reviewer_status"] == "blocked"
    assert "verified" not in result
    _assert_no_forbidden(payload)


def test_verdict_masks_and_byte_identity(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    shutil.copyfile(tmp_path / "ref.png", tmp_path / "current.png")
    config = _compat_cfg(
        masks=[{"x": 0, "y": 0, "width": 10, "height": 10}],
        dimensions=_dims(),
    )
    cfg = _write_config(tmp_path, config)
    rc = main(
        _argv(
            tmp_path,
            "visual",
            "verdict",
            "--config",
            str(cfg),
            "--reference",
            "ref.png",
            "--actual",
            "current.png",
            "--threshold",
            "90",
            "--run-id",
            "mask1",
        )
    )
    assert rc == 0
    payload = _out(capsys)
    result = payload["result"]
    assert result["status"] == "scored"
    assert result["overlay"]["mode"] == "descriptor_only"
    assert result["overlay"]["pixel_decode"] is False
    assert result["overlay"]["byte_identity"] is True
    assert result["comparison"]["masks"] == [{"x": 0, "y": 0, "width": 10, "height": 10}]
    assert result["reviewer_status"] == "threshold_met"
    overlay = json.loads(
        (tmp_path / ".omg" / "artifacts" / "visual" / "mask1" / "overlay.json").read_text(
            encoding="utf-8"
        )
    )
    assert overlay["mode"] == "descriptor_only"
    _assert_no_forbidden(payload)


def test_independent_reviewer_enforcement(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    shutil.copyfile(tmp_path / "ref.png", tmp_path / "current.png")
    cfg = _write_config(tmp_path, _compat_cfg())
    rc = main(
        _argv(
            tmp_path,
            "visual",
            "verdict",
            "--config",
            str(cfg),
            "--reference",
            "ref.png",
            "--actual",
            "current.png",
            "--editor-role",
            "omg-designer",
            "--reviewer-role",
            "omg-designer",
            "--run-id",
            "rev-same",
        )
    )
    assert rc == 2
    payload = _out(capsys)
    err = payload.get("error") or {}
    assert err.get("code") == "E_VISUAL_REVIEWER" or payload.get("error_code") == (
        "E_VISUAL_REVIEWER"
    )

    capsys.readouterr()
    rc = main(
        _argv(
            tmp_path,
            "visual",
            "verdict",
            "--config",
            str(cfg),
            "--reference",
            "ref.png",
            "--actual",
            "current.png",
            "--editor-role",
            "omg-executor",
            "--reviewer-role",
            "omg-designer",
            "--run-id",
            "rev-rw",
        )
    )
    assert rc == 2
    payload = _out(capsys)
    err = payload.get("error") or {}
    assert err.get("code") == "E_VISUAL_REVIEWER" or payload.get("error_code") == (
        "E_VISUAL_REVIEWER"
    )


def test_path_traversal_rejected(tmp_path: Path, capsys) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _seed(project)
    outside = tmp_path / "escape.png"
    shutil.copyfile(project / "ref.png", outside)
    cfg = _write_config(project, _compat_cfg())
    rc = main(
        _argv(
            project,
            "visual",
            "verdict",
            "--config",
            str(cfg),
            "--reference",
            "../escape.png",
            "--actual",
            "current.png",
            "--run-id",
            "trav1",
        )
    )
    assert rc == 2
    payload = _out(capsys)
    err = payload.get("error") or {}
    assert err.get("code") == "E_VISUAL_PATH" or payload.get("error_code") == "E_VISUAL_PATH"


def test_ralph_iteration_history_and_budget(tmp_path: Path, capsys, monkeypatch) -> None:
    _seed(tmp_path)
    monkeypatch.setenv("OMG_VISUAL_FAKE_SOURCE", str(tmp_path / "current.png"))
    config = _compat_cfg(
        threshold=90,
        max_iter=2,
        dimensions=_dims(scores=[0] * 10),
        capture={
            "command": [sys.executable, str(FIXTURES / "fake_capture.py")],
            "target": "about:blank",
        },
    )
    cfg = _write_config(tmp_path, config)
    rc = main(
        _argv(
            tmp_path,
            "visual",
            "ralph",
            "--config",
            str(cfg),
            "--max-iter",
            "2",
            "--run-id",
            "ralph-budget",
        )
    )
    assert rc == 0
    payload = _out(capsys)
    result = payload["result"]
    assert payload["command"] == "visual.ralph"
    assert result["stop_reason"] == "budget_exhausted"
    assert result["reviewer_status"] == "below_threshold"
    assert len(result["score_history"]) == 2
    assert [row["iteration"] for row in result["score_history"]] == [1, 2]
    assert all(row["aggregate"] == 0 for row in result["score_history"])
    assert result["pixel_decode"] is False
    repair = tmp_path / ".omg" / "artifacts" / "visual" / "ralph-budget" / "iterations" / "1" / "repair_prompt.json"
    assert repair.is_file()
    repair_payload = json.loads(repair.read_text(encoding="utf-8"))
    assert repair_payload["spawned"] is False
    _assert_no_forbidden(payload)
    state = tmp_path / ".omg" / "state"
    assert not state.exists()


def test_ralph_capture_required_for_next_iter(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    config = _compat_cfg(
        threshold=90,
        max_iter=5,
        dimensions=_dims(scores=[0] * 10),
    )
    cfg = _write_config(tmp_path, config)
    rc = main(_argv(tmp_path, "visual", "ralph", "--config", str(cfg), "--run-id", "ralph-stop"))
    assert rc == 0
    payload = _out(capsys)
    result = payload["result"]
    assert result["stop_reason"] == "blocked"
    notes = [row.get("note") for row in result["iterations"] if row.get("note")]
    assert any("capture required for next iter" in str(note) for note in notes)
    assert result["reviewer_status"] == "blocked"
    _assert_no_forbidden(payload)


def test_ralph_no_verified_mutation(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    shutil.copyfile(tmp_path / "ref.png", tmp_path / "current.png")
    config = _compat_cfg(threshold=90, dimensions=_dims())
    cfg = _write_config(tmp_path, config)
    rc = main(_argv(tmp_path, "visual", "ralph", "--config", str(cfg), "--run-id", "ralph-pass"))
    assert rc == 0
    payload = _out(capsys)
    result = payload["result"]
    assert result["stop_reason"] == "threshold_met"
    assert result["reviewer_status"] == "threshold_met"
    encoded = json.dumps(payload)
    for token in FORBIDDEN_TOKENS:
        assert token not in encoded
    assert not (tmp_path / ".omg" / "state").exists()
    manifest = json.loads(
        (tmp_path / ".omg" / "artifacts" / "visual" / "ralph-pass" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    for key in ("approved", "passes", "verified"):
        assert key not in manifest


def test_doctor_visual_capture_none(monkeypatch, tmp_path: Path) -> None:
    from omg_cli.doctor import check_visual_capture

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OMG_VISUAL_CAPTURE", raising=False)
    monkeypatch.delenv("OMG_PROJECT_ROOT", raising=False)
    name, level, detail = check_visual_capture()
    assert name == "visual capture adapter"
    assert level == "ok"
    assert "optional" in detail
    assert "playwright" not in detail.lower() or "not required" in detail.lower() or "none" in detail


def test_yaml_config_roundtrip(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    yaml_text = (
        "width: 200\n"
        "height: 200\n"
        "editor_role: omg-designer\n"
        "reviewer_role: omg-vision\n"
        "threshold: 90\n"
    )
    cfg = tmp_path / "visual.yaml"
    cfg.write_text(yaml_text, encoding="utf-8")
    rc = main(_argv(tmp_path, "visual", "capture", "--config", str(cfg), "--run-id", "yaml1"))
    assert rc == 0
    payload = _out(capsys)
    assert payload["result"]["status"] == "blocked"
    assert payload["result"]["block_code"] == "capture_unavailable"


def test_default_run_id_is_valid() -> None:
    from omg_cli.visual_runtime import new_run_id, validate_run_id

    rid = new_run_id()
    assert rid == validate_run_id(rid)
    assert "T" not in rid and "Z" not in rid


def test_reviewer_capability_cannot_override_catalog() -> None:
    from omg_cli.visual_runtime import VisualReviewerError, enforce_independent_reviewer

    with pytest.raises(VisualReviewerError, match="cannot override"):
        enforce_independent_reviewer(
            editor_role="omg-designer",
            reviewer_role="omg-executor",
            reviewer_capability="read-only",
        )


def test_zero_dimensions_are_metadata_errors(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    cfg = _write_config(tmp_path, _compat_cfg())
    rc = main(
        _argv(
            tmp_path,
            "visual",
            "verdict",
            "--config",
            str(cfg),
            "--reference",
            "ref.png",
            "--actual",
            "current.png",
            "--width",
            "0",
            "--height",
            "200",
            "--run-id",
            "zero-w",
        )
    )
    assert rc == 2
    payload = _out(capsys)
    err = payload.get("error") or {}
    assert err.get("code") == "E_VISUAL_METADATA" or payload.get("error_code") == (
        "E_VISUAL_METADATA"
    )


def test_capture_unlinks_stale_output(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    config = _compat_cfg(
        capture={
            "command": [sys.executable, str(FIXTURES / "noop_capture.py")],
            "target": "about:blank",
            "readiness": "explicit",
        }
    )
    cfg = _write_config(tmp_path, config)
    stale = tmp_path / ".omg" / "artifacts" / "visual" / "stale1" / "current.png"
    stale.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tmp_path / "ref.png", stale)
    rc = main(_argv(tmp_path, "visual", "capture", "--config", str(cfg), "--run-id", "stale1"))
    assert rc == 0
    payload = _out(capsys)
    assert payload["result"]["status"] == "blocked"
    assert payload["result"]["block_code"] == "capture_failed"
    assert not stale.is_file()
