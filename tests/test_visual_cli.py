"""CLI surface for Visual Contract V1 compare (#75) — envelope + exit codes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    assert nested == {"compare"}


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
    assert "usage: omg visual {compare}" in out.err
    assert not out.out.strip()
