"""Stdlib PNG overlay evidence (#75 leftover)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from omg_cli.contracts.visual_contract import MAX_EDGE
from omg_cli.visual_pixels import (
    MAX_RGBA_BYTES,
    VisualPixelError,
    decode_png_rgba,
    encode_png_rgba,
    pixel_diff_stats,
    write_overlay_png,
)

MODULE = Path(__file__).resolve().parents[1] / "omg_cli" / "visual_pixels.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "visual"
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def _rgba(width: int, height: int, color: tuple[int, int, int, int]) -> bytes:
    return bytes(color) * (width * height)


def test_pixels_module_stdlib_only() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    allowed = {"__future__", "collections", "pathlib", "struct", "typing", "zlib"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                assert root in allowed or root == "omg_cli"
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            assert root in allowed or root == "omg_cli"
            assert "PIL" not in node.module
            assert "numpy" not in node.module


def test_encode_decode_roundtrip() -> None:
    width, height = 3, 2
    rgba = bytearray(_rgba(width, height, (10, 20, 30, 255)))
    rgba[4:8] = bytes((1, 2, 3, 4))
    encoded = encode_png_rgba(width, height, bytes(rgba))
    assert encoded.startswith(b"\x89PNG")
    decoded_w, decoded_h, decoded = decode_png_rgba_from_bytes(encoded)
    assert (decoded_w, decoded_h) == (width, height)
    assert decoded == bytes(rgba)


def decode_png_rgba_from_bytes(body: bytes) -> tuple[int, int, bytes]:
    from omg_cli.visual_pixels import _decode_png_bytes

    return _decode_png_bytes(body)


def test_fixture_pngs_decode() -> None:
    ref_w, ref_h, ref = decode_png_rgba(FIXTURES / "ref.png")
    cand_w, cand_h, cand = decode_png_rgba(FIXTURES / "current.png")
    assert ref_w == cand_w
    assert ref_h == cand_h
    assert len(ref) == ref_w * ref_h * 4
    assert len(cand) == cand_w * cand_h * 4


def test_identical_stats_and_overlay_file(tmp_path: Path) -> None:
    rgba = _rgba(2, 2, (9, 8, 7, 255))
    image = tmp_path / "a.png"
    image.write_bytes(encode_png_rgba(2, 2, rgba))
    decoded = decode_png_rgba(image)
    stats = pixel_diff_stats(decoded, decoded, [])
    assert stats["changed_pixels"] == 0
    assert stats["total_pixels"] == 4
    assert stats["changed_ratio_milli"] == 0
    assert stats["bbox"] is None
    dest = tmp_path / "overlay.png"
    write_overlay_png(dest, decoded, decoded, [])
    assert dest.is_file()


def test_different_stats_and_overlay_bytes_differ(tmp_path: Path) -> None:
    ref = _rgba(2, 2, (0, 0, 0, 255))
    cand = bytearray(ref)
    cand[0:4] = bytes((255, 0, 0, 255))
    ref_png = tmp_path / "ref.png"
    cand_png = tmp_path / "cand.png"
    ref_png.write_bytes(encode_png_rgba(2, 2, ref))
    cand_png.write_bytes(encode_png_rgba(2, 2, bytes(cand)))
    ref_rgba = decode_png_rgba(ref_png)
    cand_rgba = decode_png_rgba(cand_png)
    stats = pixel_diff_stats(ref_rgba, cand_rgba, [])
    assert stats["changed_pixels"] == 1
    assert stats["total_pixels"] == 4
    assert stats["changed_ratio_milli"] == 250
    assert stats["bbox"] == {"x": 0, "y": 0, "width": 1, "height": 1}
    dest = tmp_path / "overlay.png"
    write_overlay_png(dest, ref_rgba, cand_rgba, [])
    overlay = dest.read_bytes()
    assert overlay != ref_png.read_bytes()
    assert overlay != cand_png.read_bytes()


def test_masks_exclude_changed_pixels() -> None:
    ref = _rgba(2, 2, (0, 0, 0, 255))
    cand = bytearray(ref)
    cand[0:4] = bytes((255, 0, 0, 255))
    stats = pixel_diff_stats(
        (2, 2, ref),
        (2, 2, bytes(cand)),
        [{"x": 0, "y": 0, "width": 1, "height": 1}],
    )
    assert stats["changed_pixels"] == 0
    assert stats["total_pixels"] == 3


def test_truncated_png_refused(tmp_path: Path) -> None:
    path = tmp_path / "trunc.png"
    path.write_bytes(PNG_1X1[:24])
    with pytest.raises(VisualPixelError, match="truncated|not a PNG|CRC"):
        decode_png_rgba(path)


def test_non_png_refused(tmp_path: Path) -> None:
    path = tmp_path / "nope.png"
    path.write_bytes(b"not a png")
    with pytest.raises(VisualPixelError, match="not a PNG"):
        decode_png_rgba(path)


def test_symlink_refused(tmp_path: Path) -> None:
    target = tmp_path / "real.png"
    target.write_bytes(PNG_1X1)
    link = tmp_path / "link.png"
    link.symlink_to(target)
    with pytest.raises(VisualPixelError, match="regular file"):
        decode_png_rgba(link)


def test_oversize_ihdr_refused(tmp_path: Path) -> None:
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", MAX_EDGE + 1, 1, 8, 6, 0, 0, 0)
    body = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", b"x")
        + chunk(b"IEND", b"")
    )
    path = tmp_path / "huge.png"
    path.write_bytes(body)
    with pytest.raises(VisualPixelError, match="dimensions"):
        decode_png_rgba(path)


def test_oversize_rgba_working_set_refused(tmp_path: Path) -> None:
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    # Within MAX_EDGE / MAX_PIXELS, but 10000×10000×4 = 400 MiB reconstructed.
    assert 10000 * 10000 * 4 > MAX_RGBA_BYTES
    ihdr = struct.pack(">IIBBBBB", 10000, 10000, 1, 0, 0, 0, 0)
    body = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", b"x")
        + chunk(b"IEND", b"")
    )
    path = tmp_path / "rgba-bomb.png"
    path.write_bytes(body)

    with pytest.raises(VisualPixelError, match="working-set") as caught:
        decode_png_rgba(path)
    assert caught.value.code == "E_VISUAL_PIXEL"


def test_dimension_mismatch_refused() -> None:
    with pytest.raises(VisualPixelError, match="dimensions differ"):
        pixel_diff_stats(
            (1, 1, _rgba(1, 1, (0, 0, 0, 255))),
            (2, 1, _rgba(2, 1, (0, 0, 0, 255))),
            [],
        )


def test_overlay_json_must_not_inline_bytes() -> None:
    payload = {
        "pixel_decode": True,
        "changed_pixels": 1,
        "overlay_png": ".omg/artifacts/visual/r1/overlay.png",
    }
    dumped = json.dumps(payload)
    assert "iVBOR" not in dumped
    assert "base64" not in dumped
    assert "verified" not in dumped
