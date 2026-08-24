"""Hermetic SCIP Index protobuf codec (#73 leftover)."""

from __future__ import annotations

from pathlib import Path

from omg_cli.scip_codec import (
    ScipCodecError,
    classify_scip_cli_text,
    decode_index,
    detect_scip_cli,
    encode_document,
    encode_index,
    encode_occurrence,
    looks_like_scip_symbol,
    make_scip_symbol,
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
    hello_sym = by_name["hello"]["symbol_id"]
    assert looks_like_scip_symbol(hello_sym)
    assert hello_sym.startswith("omg ")
    assert hello_sym.endswith("hello.")
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
    assert looks_like_scip_symbol(hashed[0]["symbol_id"])
    assert hashed[0]["symbol_id"] != "mod.py#hello"


def test_document_occurrences_use_proto_field_2() -> None:
    from omg_cli.scip_codec import _read_fields

    occ = encode_occurrence(symbol=make_scip_symbol("mod.py", "hello"), line=2, definition=True)
    doc = encode_document(relative_path="mod.py", language="Python", occurrences=[occ])
    fields = [field for field, _wire, _val in _read_fields(doc)]
    assert 2 in fields
    assert 6 not in fields
    decoded = decode_index(encode_index([doc]))
    assert decoded[0]["name"] == "hello"
    fake = (
        b"\x0a\x06mod.py"  # relative_path=1
        + b"\x32" + bytes([len(occ)]) + occ  # field 6 is PositionEncoding, not occurrences
    )
    assert decode_index(encode_index([fake])) == []


def test_make_scip_symbol_matches_grammar() -> None:
    symbol = make_scip_symbol("pkg/mod.py", "hello")
    assert looks_like_scip_symbol(symbol)
    assert symbol.startswith("omg . . . ")
    assert symbol.endswith("/hello.")
    assert not looks_like_scip_symbol("mod.py#hello")
    assert not looks_like_scip_symbol("pkg/mod.py/hello")
    assert looks_like_scip_symbol("local hello")


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
