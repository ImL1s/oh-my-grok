"""Minimal SCIP Index protobuf codec (no pip protobuf) and PATH identity.

Writes/reads the Index/Document/Occurrence subset used by CodeGraph.
Refuses Homebrew MIP ``scip`` (scipopt) as Sourcegraph SCIP.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

# scip.proto (subset): Index.documents=2, Document.relative_path=1,
# Document.language=4, Document.occurrences=6, Occurrence.range=1,
# Occurrence.symbol=2, Occurrence.symbol_roles=3.
_WT_VARINT = 0
_WT_LEN = 2
SYMBOL_ROLE_DEFINITION = 1
SYMBOL_ROLE_REFERENCE = 8


class ScipCodecError(ValueError):
    """Malformed SCIP protobuf."""


def _varint(n: int) -> bytes:
    if n < 0:
        raise ScipCodecError("negative varint")
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _ld(field: int, payload: bytes) -> bytes:
    return _key(field, _WT_LEN) + _varint(len(payload)) + payload


def _string(field: int, text: str) -> bytes:
    return _ld(field, text.encode("utf-8"))


def _packed_varints(field: int, values: list[int]) -> bytes:
    return _ld(field, b"".join(_varint(n) for n in values))


def _read_packed_varints(data: bytes) -> list[int]:
    i = 0
    out: list[int] = []
    while i < len(data):
        n, i = _read_varint(data, i)
        out.append(n)
    return out


def encode_occurrence(*, symbol: str, line: int, definition: bool) -> bytes:
    start = max(0, int(line))
    role = SYMBOL_ROLE_DEFINITION if definition else SYMBOL_ROLE_REFERENCE
    # proto3 packed repeated int32 range = 1
    # (startLine, startCharacter, endLine, endCharacter)
    return b"".join(
        (
            _packed_varints(1, [start, 0, start, 0]),
            _string(2, symbol),
            _key(3, _WT_VARINT) + _varint(role),
        )
    )


def encode_document(
    *,
    relative_path: str,
    language: str,
    occurrences: list[bytes],
) -> bytes:
    parts = [_string(1, relative_path), _string(4, language)]
    parts.extend(_ld(6, occ) for occ in occurrences)
    return b"".join(parts)


def encode_index(documents: list[bytes]) -> bytes:
    return b"".join(_ld(2, doc) for doc in documents)


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    shift = 0
    n = 0
    while i < len(data):
        b = data[i]
        i += 1
        n |= (b & 0x7F) << shift
        if b < 0x80:
            return n, i
        shift += 7
        if shift > 63:
            raise ScipCodecError("varint too long")
    raise ScipCodecError("truncated varint")


def _read_fields(data: bytes) -> list[tuple[int, int, bytes | int]]:
    i = 0
    out: list[tuple[int, int, bytes | int]] = []
    while i < len(data):
        key, i = _read_varint(data, i)
        field = key >> 3
        wire = key & 7
        if wire == _WT_VARINT:
            val, i = _read_varint(data, i)
            out.append((field, wire, val))
        elif wire == _WT_LEN:
            n, i = _read_varint(data, i)
            if i + n > len(data):
                raise ScipCodecError("truncated length-delimited field")
            out.append((field, wire, data[i : i + n]))
            i += n
        else:
            raise ScipCodecError(f"unsupported wire type {wire}")
    return out


def decode_index(blob: bytes) -> list[dict[str, Any]]:
    """Return occurrence dicts: path, name, role, line, symbol_id."""
    if not isinstance(blob, (bytes, bytearray)):
        raise ScipCodecError("empty index")
    occs: list[dict[str, Any]] = []
    if not blob:
        return occs
    for field, wire, val in _read_fields(bytes(blob)):
        if field != 2 or wire != _WT_LEN or not isinstance(val, bytes):
            continue
        path = ""
        for dfield, dwire, dval in _read_fields(val):
            if dfield == 1 and dwire == _WT_LEN and isinstance(dval, bytes):
                path = dval.decode("utf-8", errors="replace")
            if dfield != 6 or dwire != _WT_LEN or not isinstance(dval, bytes):
                continue
            symbol = ""
            role_bits = 0
            line = 0
            saw_line = False
            for ofield, owire, oval in _read_fields(dval):
                if ofield == 1:
                    if owire == _WT_LEN and isinstance(oval, bytes):
                        packed = _read_packed_varints(oval)
                        if packed and not saw_line:
                            line = packed[0]
                            saw_line = True
                    elif owire == _WT_VARINT and isinstance(oval, int):
                        if not saw_line:
                            line = oval
                            saw_line = True
                elif ofield == 2 and owire == _WT_LEN and isinstance(oval, bytes):
                    symbol = oval.decode("utf-8", errors="replace")
                elif ofield == 3 and owire == _WT_VARINT and isinstance(oval, int):
                    role_bits = oval
            if not symbol or not path:
                continue
            role = "definition" if role_bits & SYMBOL_ROLE_DEFINITION else "reference"
            leaf = symbol.rsplit("#", 1)[-1]
            name = leaf.rsplit("/", 1)[-1]
            occs.append(
                {
                    "path": path,
                    "name": name,
                    "role": role,
                    "line": line,
                    "symbol_id": symbol,
                }
            )
    return occs


def occurrences_to_index(occurrences: list[dict[str, Any]]) -> bytes:
    by_path: dict[str, list[bytes]] = {}
    for occ in occurrences:
        if not isinstance(occ, dict):
            continue
        path = str(occ.get("path") or "")
        name = str(occ.get("name") or "")
        if not path or not name:
            continue
        symbol = str(occ.get("symbol_id") or f"{path}/{name}")
        line = int(occ.get("line") or 0)
        definition = occ.get("role") == "definition"
        by_path.setdefault(path, []).append(
            encode_occurrence(symbol=symbol, line=line, definition=definition)
        )
    docs = [
        encode_document(relative_path=path, language="", occurrences=occs)
        for path, occs in by_path.items()
    ]
    return encode_index(docs)


def classify_scip_cli_text(text: str) -> str:
    """Return ``mip``, ``sourcegraph``, or ``unknown`` from help/version text."""
    blob = (text or "").lower()
    if (
        "mixed integer" in blob
        or "scipopt" in blob
        or "optimization suite" in blob
        or "constraint integer" in blob
    ):
        return "mip"
    if (
        "sourcegraph" in blob
        or "code intelligence" in blob
        or "scip index" in blob
        or "lsif" in blob
    ):
        return "sourcegraph"
    return "unknown"


def detect_scip_cli(command: str = "scip") -> dict[str, Any]:
    """Identify PATH ``scip`` without treating MIP solvers as Sourcegraph SCIP."""
    resolved = shutil.which(command)
    if not resolved:
        return {"ok": False, "kind": "missing", "path": None, "not_scip": True}
    try:
        proc = subprocess.run(
            [resolved, "--help"],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "ok": False,
            "kind": "unreadable",
            "path": resolved,
            "not_scip": True,
        }
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    kind = classify_scip_cli_text(text)
    return {
        "ok": kind == "sourcegraph",
        "kind": kind,
        "path": resolved,
        "not_scip": kind != "sourcegraph",
        "help_excerpt": text[:400],
    }


def write_scip_file(path: Path, blob: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, path)
