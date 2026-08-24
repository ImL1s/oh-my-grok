"""Hermetic SCIP Index protobuf codec (#73 leftover)."""

from __future__ import annotations

from pathlib import Path

from omg_cli.scip_codec import (
    ScipCodecError,
    classify_scip_cli_text,
    decode_index,
    detect_scip_cli,
    encode_occurrence,
    occurrences_to_index,
    write_scip_file,
)


def test_occurrences_roundtrip_packed_range() -> None:
    occs = [
        {
            "path": "mod.py",
            "name": "hello",
            "role": "definition",
            "line": 2,
            "symbol_id": "mod.py/hello",
        },
        {
            "path": "mod.py",
            "name": "json",
            "role": "reference",
            "line": 0,
            "symbol_id": "mod.py/json",
        },
    ]
    blob = occurrences_to_index(occs)
    assert blob[:1] == b"\x12"  # Index.documents=2, wire type 2
    decoded = decode_index(blob)
    by_name = {row["name"]: row for row in decoded}
    assert by_name["hello"]["role"] == "definition"
    assert by_name["hello"]["line"] == 2
    assert by_name["hello"]["path"] == "mod.py"
    assert by_name["hello"]["symbol_id"] == "mod.py/hello"
    hashed = decode_index(
        occurrences_to_index(
            [
                {
                    "path": "mod.py",
                    "name": "hello",
                    "role": "definition",
                    "line": 2,
                    "symbol_id": "mod.py#hello",
                }
            ]
        )
    )
    assert hashed[0]["name"] == "hello"
    assert hashed[0]["symbol_id"] == "mod.py#hello"
    assert by_name["json"]["role"] == "reference"
    packed = encode_occurrence(symbol="mod.py/hello", line=2, definition=True)
    assert packed[:1] == b"\x0a"  # Occurrence.range=1 packed (wire type 2)


def test_decode_empty_index() -> None:
    assert decode_index(b"") == []


def test_decode_truncated_raises() -> None:
    try:
        decode_index(b"\x12\x20")
    except ScipCodecError:
        return
    raise AssertionError("expected ScipCodecError")


def test_classify_mip_vs_sourcegraph() -> None:
    assert classify_scip_cli_text("SCIP Optimization Suite mixed integer") == "mip"
    assert classify_scip_cli_text("scipopt constraint integer program") == "mip"
    assert classify_scip_cli_text("Sourcegraph SCIP index / LSIF") == "sourcegraph"
    assert classify_scip_cli_text("hello world") == "unknown"


def test_detect_scip_cli_missing(monkeypatch) -> None:
    monkeypatch.setattr("omg_cli.scip_codec.shutil.which", lambda _cmd: None)
    found = detect_scip_cli()
    assert found["ok"] is False
    assert found["kind"] == "missing"
    assert found["not_scip"] is True
    assert found["path"] is None


def test_detect_scip_cli_refuses_mip(monkeypatch, tmp_path: Path) -> None:
    fake = tmp_path / "scip"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("omg_cli.scip_codec.shutil.which", lambda _cmd: str(fake))

    class _Proc:
        stdout = "SCIP Optimization Suite\nconstraint integer programming\n"
        stderr = ""

    monkeypatch.setattr(
        "omg_cli.scip_codec.subprocess.run",
        lambda *a, **k: _Proc(),
    )
    found = detect_scip_cli()
    assert found["ok"] is False
    assert found["kind"] == "mip"
    assert found["not_scip"] is True
    assert found["path"] == str(fake)


def test_write_scip_file_roundtrip(tmp_path: Path) -> None:
    dest = tmp_path / "local-index.scip"
    blob = occurrences_to_index(
        [
            {
                "path": "a.py",
                "name": "hello",
                "role": "definition",
                "line": 1,
                "symbol_id": "a.py/hello",
            }
        ]
    )
    write_scip_file(dest, blob)
    decoded = decode_index(dest.read_bytes())
    assert decoded[0]["name"] == "hello"
    assert decoded[0]["line"] == 1
