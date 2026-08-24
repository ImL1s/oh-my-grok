"""CLI surface for Visual Contract V1 compare/capture/verdict/ralph/overlay (#75)."""

from __future__ import annotations

import ast
import json
import os
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
    assert nested == {"compare", "capture", "verdict", "ralph", "overlay"}


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
    assert "usage: omg visual {compare,capture,verdict,ralph,overlay}" in out.err
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


def _hide_path_screencapture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic: no PATH /usr/sbin screencapture fallback (not a fake pass)."""
    orig_which = shutil.which

    def fake_which(cmd: str | None = None, *args: object, **kwargs: object) -> str | None:
        name = Path(str(cmd or "")).name.lower()
        if name in {"screencapture", "screencapture.exe"}:
            return None
        return orig_which(cmd, *args, **kwargs)

    monkeypatch.setattr("omg_cli.visual_runtime.shutil.which", fake_which)
    monkeypatch.setattr(
        "omg_cli.visual_runtime.MACOS_SCREENCAPTURE",
        Path("/nonexistent/omg-no-screencapture"),
    )


@pytest.fixture(autouse=True)
def _hermetic_hide_path_screencapture(monkeypatch: pytest.MonkeyPatch) -> None:
    _hide_path_screencapture(monkeypatch)


def _install_fake_path_screencapture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """PATH ``screencapture`` that writes PNG to the last argv (ignores env)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "screencapture"
    png_hex = (
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"PNG = bytes.fromhex({png_hex!r})\n"
        "if len(sys.argv) < 2:\n"
        "    raise SystemExit('missing output path')\n"
        "dest = Path(sys.argv[-1])\n"
        "if dest.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}:\n"
        "    raise SystemExit('last argv is not an output file')\n"
        "dest.parent.mkdir(parents=True, exist_ok=True)\n"
        "dest.write_bytes(PNG)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("OMG_VISUAL_CAPTURE", raising=False)

    def fake_which(cmd: str | None = None, *args: object, **kwargs: object) -> str | None:
        name = Path(str(cmd or "")).name.lower()
        if name in {"screencapture", "screencapture.exe"}:
            return str(fake)
        return None

    monkeypatch.setattr("omg_cli.visual_runtime.shutil.which", fake_which)
    return fake


def test_runtime_does_not_import_pixel_decoders() -> None:
    banned = {"PIL", "Pillow", "pillow", "playwright", "numpy"}
    paths = (
        RUNTIME_PATH,
        RUNTIME_PATH.parent / "visual_pixels.py",
        RUNTIME_PATH.parent / "contracts" / "visual_contract.py",
    )
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
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


def test_capture_none_is_blocked_not_fake_pass(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hide_path_screencapture(monkeypatch)
    monkeypatch.delenv("OMG_VISUAL_CAPTURE", raising=False)
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


def test_verdict_decoded_png_dimension_mismatch_is_blocked(
    tmp_path: Path, capsys
) -> None:
    """Pixel overlay must not turn a dim mismatch into E_VISUAL_PIXEL."""
    from omg_cli.visual_pixels import encode_png_rgba

    _seed(tmp_path)
    (tmp_path / "ref.png").write_bytes(
        encode_png_rgba(2, 2, bytes([255, 0, 0, 255] * 4))
    )
    (tmp_path / "current.png").write_bytes(
        encode_png_rgba(3, 3, bytes([0, 255, 0, 255] * 9))
    )
    config = _compat_cfg(dimensions=_dims())
    # Descriptors claim the same size so only the decoded IHDR differs.
    config["reference"] = {
        "path": "ref.png",
        "width": 8,
        "height": 8,
        "media_type": "image/png",
    }
    config["actual"] = {
        "path": "current.png",
        "width": 8,
        "height": 8,
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
            "mismatch-decoded",
        )
    )
    assert rc == 0
    payload = _out(capsys)
    result = payload["result"]
    assert result["status"] == "blocked"
    assert result["comparison"]["block_code"] == "image_dimension_mismatch"
    assert "E_VISUAL_PIXEL" not in json.dumps(payload)
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
    assert result["overlay"]["mode"] == "pixel"
    assert result["overlay"]["pixel_decode"] is True
    assert result["overlay"]["byte_identity"] is True
    assert result["overlay"]["changed_pixels"] == 0
    assert result["pixel_decode"] is True
    assert result["comparison"]["masks"] == [{"x": 0, "y": 0, "width": 10, "height": 10}]
    assert result["reviewer_status"] == "threshold_met"
    overlay = json.loads(
        (tmp_path / ".omg" / "artifacts" / "visual" / "mask1" / "overlay.json").read_text(
            encoding="utf-8"
        )
    )
    assert overlay["mode"] == "pixel"
    overlay_png = tmp_path / overlay["overlay_png"]
    assert overlay_png.is_file()
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

    _hide_path_screencapture(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OMG_VISUAL_CAPTURE", raising=False)
    monkeypatch.delenv("OMG_PROJECT_ROOT", raising=False)
    name, level, detail = check_visual_capture()
    assert name == "visual capture adapter"
    assert level == "ok"
    assert "optional" in detail
    assert "playwright" not in detail.lower() or "not required" in detail.lower() or "none" in detail


def test_yaml_config_roundtrip(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hide_path_screencapture(monkeypatch)
    monkeypatch.delenv("OMG_VISUAL_CAPTURE", raising=False)
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


def test_capture_redacts_command_secrets(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    secret = "sk-live-super-secret-token"
    config = _compat_cfg(
        capture={
            "command": [
                sys.executable,
                str(FIXTURES / "fake_capture.py"),
                "--header",
                f"Authorization: Bearer {secret}",
            ],
            "target": "about:blank",
            "readiness": "explicit",
        }
    )
    cfg = _write_config(tmp_path, config)
    rc = main(_argv(tmp_path, "visual", "capture", "--config", str(cfg), "--run-id", "redact1"))
    assert rc == 0
    payload = _out(capsys)
    dumped = json.dumps(payload)
    assert secret not in dumped
    capture_json = (
        tmp_path / ".omg" / "artifacts" / "visual" / "redact1" / "capture.json"
    )
    assert secret not in capture_json.read_text(encoding="utf-8")


def test_capture_redacts_split_flag_and_stderr(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    secret = "supersecret-split-token"
    fail_script = tmp_path / "fail_capture.py"
    fail_script.write_text(
        "import sys\n"
        f"sys.stderr.write('token={secret}\\n')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    config = _compat_cfg(
        capture={
            "command": [sys.executable, str(fail_script), "--token", secret],
            "target": "about:blank",
            "readiness": "explicit",
        }
    )
    cfg = _write_config(tmp_path, config)
    rc = main(_argv(tmp_path, "visual", "capture", "--config", str(cfg), "--run-id", "redact2"))
    assert rc == 0
    payload = _out(capsys)
    dumped = json.dumps(payload)
    assert secret not in dumped
    capture_json = tmp_path / ".omg" / "artifacts" / "visual" / "redact2" / "capture.json"
    body = capture_json.read_text(encoding="utf-8")
    assert secret not in body
    command = payload["result"]["command"]
    assert secret not in command
    assert command[command.index("--token") + 1] == "[REDACTED]" or (
        command[command.index("--token") + 1] != secret
    )


def test_capture_redacts_target_query_secret(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    secret = "supersecret-target-token"
    target = f"https://example.test/page?token={secret}"
    config = _compat_cfg(
        capture={
            "command": [
                sys.executable,
                str(FIXTURES / "fake_capture.py"),
            ],
            "target": target,
            "readiness": "explicit",
        }
    )
    cfg = _write_config(tmp_path, config)
    rc = main(_argv(tmp_path, "visual", "capture", "--config", str(cfg), "--run-id", "redact3"))
    assert rc == 0
    payload = _out(capsys)
    dumped = json.dumps(payload)
    assert secret not in dumped
    capture_json = tmp_path / ".omg" / "artifacts" / "visual" / "redact3" / "capture.json"
    body = capture_json.read_text(encoding="utf-8")
    assert secret not in body
    persisted = payload["result"]["target"]
    assert persisted is not None
    assert secret not in persisted
    assert "[REDACTED]" in persisted


def test_verdict_missing_image_is_path_error(tmp_path: Path, capsys) -> None:
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
            "missing-ref.png",
            "--actual",
            "current.png",
            "--run-id",
            "miss1",
        )
    )
    assert rc == 2
    payload = _out(capsys)
    err = payload.get("error") or {}
    assert err.get("code") == "E_VISUAL_PATH" or payload.get("error_code") == "E_VISUAL_PATH"


def test_width_above_contract_max_is_metadata_error(tmp_path: Path, capsys) -> None:
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
            "16385",
            "--height",
            "200",
            "--run-id",
            "huge-w",
        )
    )
    assert rc == 2
    payload = _out(capsys)
    err = payload.get("error") or {}
    assert err.get("code") == "E_VISUAL_METADATA" or payload.get("error_code") == (
        "E_VISUAL_METADATA"
    )


def test_threshold_rejects_non_integer_config() -> None:
    from omg_cli.visual_runtime import VisualConfigError, resolve_threshold

    with pytest.raises(VisualConfigError):
        resolve_threshold({"threshold": "90"}, None)
    with pytest.raises(VisualConfigError):
        resolve_threshold({"threshold": 90.9}, None)
    with pytest.raises(VisualConfigError):
        resolve_threshold({"threshold": True}, None)


def test_overlay_identical_pngs(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    shutil.copyfile(tmp_path / "ref.png", tmp_path / "current.png")
    rc = main(
        _argv(
            tmp_path,
            "visual",
            "overlay",
            "--reference",
            "ref.png",
            "--candidate",
            "current.png",
            "--run-id",
            "ov-same",
        )
    )
    assert rc == 0
    payload = _out(capsys)
    result = payload["result"]
    assert payload["command"] == "visual.overlay"
    assert result["pixel_decode"] is True
    assert result["changed_pixels"] == 0
    assert isinstance(result["changed_pixels"], int)
    overlay_png = tmp_path / result["overlay_png"]
    assert overlay_png.is_file()
    assert "verified" not in result
    _assert_no_forbidden(payload)
    dumped = json.dumps(payload)
    assert "iVBOR" not in dumped
    assert "base64" not in dumped


def test_overlay_different_pngs(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    rc = main(
        _argv(
            tmp_path,
            "visual",
            "overlay",
            "--reference",
            "ref.png",
            "--candidate",
            "current.png",
            "--run-id",
            "ov-diff",
        )
    )
    assert rc == 0
    payload = _out(capsys)
    result = payload["result"]
    assert result["pixel_decode"] is True
    assert isinstance(result["changed_pixels"], int)
    assert result["changed_pixels"] > 0
    overlay_png = tmp_path / result["overlay_png"]
    assert overlay_png.is_file()
    overlay_bytes = overlay_png.read_bytes()
    assert overlay_bytes != (tmp_path / "ref.png").read_bytes()
    assert overlay_bytes != (tmp_path / "current.png").read_bytes()
    assert "verified" not in result
    _assert_no_forbidden(payload)


def test_overlay_symlink_refused(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    link = tmp_path / "link.png"
    link.symlink_to(tmp_path / "ref.png")
    rc = main(
        _argv(
            tmp_path,
            "visual",
            "overlay",
            "--reference",
            "link.png",
            "--candidate",
            "current.png",
            "--run-id",
            "ov-link",
        )
    )
    assert rc == 2
    payload = _out(capsys)
    err = payload.get("error") or {}
    assert err.get("code") == "E_VISUAL_PATH" or payload.get("error_code") == "E_VISUAL_PATH"
    _assert_no_forbidden(payload)


def test_overlay_truncated_png_refused(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    truncated = tmp_path / "trunc.png"
    truncated.write_bytes((tmp_path / "ref.png").read_bytes()[:20])
    rc = main(
        _argv(
            tmp_path,
            "visual",
            "overlay",
            "--reference",
            "trunc.png",
            "--candidate",
            "current.png",
            "--run-id",
            "ov-trunc",
        )
    )
    assert rc == 2
    payload = _out(capsys)
    err = payload.get("error") or {}
    assert err.get("code") == "E_VISUAL_PIXEL" or payload.get("error_code") == (
        "E_VISUAL_PIXEL"
    )
    _assert_no_forbidden(payload)


def test_overlay_descriptor_only_skips_decode(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    rc = main(
        _argv(
            tmp_path,
            "visual",
            "overlay",
            "--reference",
            "ref.png",
            "--candidate",
            "current.png",
            "--descriptor-only",
            "--run-id",
            "ov-desc",
        )
    )
    assert rc == 0
    payload = _out(capsys)
    result = payload["result"]
    assert result["pixel_decode"] is False
    assert result["mode"] == "descriptor_only"
    assert "changed_pixels" not in result
    assert not (tmp_path / ".omg" / "artifacts" / "visual" / "ov-desc" / "overlay.png").exists()
    _assert_no_forbidden(payload)


def test_overlay_missing_flags_are_usage(capsys) -> None:
    rc = main(["--json", "visual", "overlay"])
    assert rc == 2
    payload = _out(capsys)
    err = payload.get("error") or {}
    assert err.get("code") == "E_USAGE" or payload.get("error_code") == "E_USAGE"


class _FakeProc:
    def __init__(self, code: int = 0, stderr: str = "") -> None:
        self.returncode = code
        self.stdout = ""
        self.stderr = stderr


def test_execute_capture_command_appends_screencapture_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.visual_runtime import ENV_OUTPUT, execute_capture_command

    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeProc:
        seen["argv"] = list(argv)
        seen["env"] = dict(kwargs.get("env") or {})
        return _FakeProc()

    monkeypatch.setattr("omg_cli.visual_runtime.subprocess.run", fake_run)
    output = tmp_path / "out.png"
    result = execute_capture_command(
        ["screencapture", "-x"],
        root=tmp_path,
        output=output,
        env={},
    )
    assert seen["argv"] == ["screencapture", "-x", str(output)]
    assert seen["env"][ENV_OUTPUT] == str(output)
    assert result["status"] == "captured"
    assert result["exit_code"] == 0
    for key in ("approved", "passes", "verified"):
        assert key not in result
    result = execute_capture_command(
        ["screencapture.exe", "-x"],
        root=tmp_path,
        output=output,
        env={},
    )
    assert seen["argv"] == ["screencapture.exe", "-x", str(output)]
    assert result["status"] == "captured"
    result = execute_capture_command(
        ["screencapture", "-x", str(output)],
        root=tmp_path,
        output=output,
        env={},
    )
    assert seen["argv"] == ["screencapture", "-x", str(output)]


def test_execute_capture_command_substitutes_output_placeholders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.visual_runtime import ENV_OUTPUT, execute_capture_command

    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeProc:
        seen["argv"] = list(argv)
        seen["env"] = dict(kwargs.get("env") or {})
        return _FakeProc()

    monkeypatch.setattr("omg_cli.visual_runtime.subprocess.run", fake_run)
    output = tmp_path / "current.png"
    result = execute_capture_command(
        ["mycap", "--out", "{output}"],
        root=tmp_path,
        output=output,
        env={},
    )
    assert seen["argv"] == ["mycap", "--out", str(output)]
    assert seen["env"][ENV_OUTPUT] == str(output)
    assert result["status"] == "captured"

    result = execute_capture_command(
        ["screencapture", "-x", "{OMG_VISUAL_OUTPUT}"],
        root=tmp_path,
        output=output,
        env={},
    )
    assert seen["argv"] == ["screencapture", "-x", str(output)]
    assert result["status"] == "captured"


def test_diagnose_path_screencapture_keeps_absolute_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.visual_runtime import diagnose_capture_source

    monkeypatch.setattr(
        "omg_cli.visual_runtime.discover_path_screencapture",
        lambda environ=None: "/usr/sbin/screencapture",
    )
    diagnosis = diagnose_capture_source({}, env={})
    assert diagnosis["source"] == "path"
    assert diagnosis["command"] == ["/usr/sbin/screencapture", "-x"]


def test_diagnose_path_screencapture_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.visual_runtime import diagnose_capture_source

    fake = _install_fake_path_screencapture(tmp_path, monkeypatch)
    diagnosis = diagnose_capture_source({}, env={})
    assert diagnosis["source"] == "path"
    assert diagnosis["status"] == "ready"
    assert diagnosis["command"] == [str(fake), "-x"]
    assert diagnosis["playwright_required"] is False
    assert diagnosis["block_code"] is None
    assert Path(fake).name == "screencapture"


def test_capture_path_screencapture_writes_via_output_argv(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.visual_runtime import diagnose_capture_source

    _seed(tmp_path)
    fake = _install_fake_path_screencapture(tmp_path, monkeypatch)
    diagnosis = diagnose_capture_source({}, env={})
    assert diagnosis["source"] == "path"
    assert diagnosis["command"] == [str(fake), "-x"]
    cfg = _write_config(tmp_path, _compat_cfg())
    rc = main(
        _argv(tmp_path, "visual", "capture", "--config", str(cfg), "--run-id", "pathcap")
    )
    assert rc == 0
    payload = _out(capsys)
    result = payload["result"]
    assert payload["ok"] is True
    assert payload["command"] == "visual.capture"
    assert result["status"] == "captured"
    assert result["source"] == "path"
    assert result["command"][0] == str(fake)
    assert result["playwright_required"] is False
    current = tmp_path / ".omg" / "artifacts" / "visual" / "pathcap" / "current.png"
    assert current.is_file()
    assert current.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    _assert_no_forbidden(payload)
    encoded = json.dumps(payload)
    for token in ("approved", "passes", "verified"):
        assert token not in encoded
    assert not (tmp_path / ".omg" / "state").exists()
