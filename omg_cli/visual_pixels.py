"""Stdlib PNG pixel decode / overlay evidence (#75 leftover).

Contract ``visual_contract.py`` stays pixel-agnostic. This module is the
runtime decoder: PNG only, no Pillow/numpy, never inlines bytes into JSON.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from omg_cli.contracts.visual_contract import MAX_EDGE, MAX_IMAGE_BYTES, MAX_PIXELS

PNG_SIG = b"\x89PNG\r\n\x1a\n"
COLOR_GRAY = 0
COLOR_RGB = 2
COLOR_INDEXED = 3
COLOR_GRAY_A = 4
COLOR_RGBA = 6
HIGHLIGHT_RGBA = (255, 0, 255, 255)
ADAM7 = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)


class VisualPixelError(ValueError):
    """PNG pixel decode/overlay failed closed."""

    code = "E_VISUAL_PIXEL"


def decode_png_rgba(path: Path) -> tuple[int, int, bytes]:
    """Decode a confined regular PNG into packed RGBA8 ``width*height*4`` bytes."""
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise VisualPixelError("image path is not a regular file")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise VisualPixelError("PNG is not readable") from exc
    if size < 1 or size > MAX_IMAGE_BYTES:
        raise VisualPixelError("PNG exceeds Visual Contract V1 size limit")
    try:
        body = target.read_bytes()
    except OSError as exc:
        raise VisualPixelError("PNG is not readable") from exc
    if len(body) > MAX_IMAGE_BYTES:
        raise VisualPixelError("PNG exceeds Visual Contract V1 size limit")
    return _decode_png_bytes(body)


def pixel_diff_stats(
    ref_rgba: tuple[int, int, bytes],
    cand_rgba: tuple[int, int, bytes],
    masks: Sequence[Mapping[str, int]] | None = None,
) -> dict[str, Any]:
    """Compare two RGBA buffers. Masked pixels are excluded from totals."""
    ref_w, ref_h, ref = _require_rgba(ref_rgba, label="reference")
    cand_w, cand_h, cand = _require_rgba(cand_rgba, label="candidate")
    if ref_w != cand_w or ref_h != cand_h:
        raise VisualPixelError("reference and candidate pixel dimensions differ")
    skip = _mask_bitmap(ref_w, ref_h, masks or ())
    total = 0
    changed = 0
    min_x = min_y = max_x = max_y = -1
    span = ref_w * ref_h
    for index in range(span):
        if skip[index]:
            continue
        total += 1
        off = index * 4
        if ref[off : off + 4] != cand[off : off + 4]:
            changed += 1
            x = index % ref_w
            y = index // ref_w
            if min_x < 0:
                min_x = max_x = x
                min_y = max_y = y
            else:
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
    if total < 1:
        return {
            "changed_pixels": 0,
            "total_pixels": 0,
            "changed_ratio_milli": 0,
            "bbox": None,
        }
    bbox = None
    if changed > 0:
        bbox = {
            "x": min_x,
            "y": min_y,
            "width": max_x - min_x + 1,
            "height": max_y - min_y + 1,
        }
    return {
        "changed_pixels": changed,
        "total_pixels": total,
        "changed_ratio_milli": (changed * 1000) // total,
        "bbox": bbox,
    }


def write_overlay_png(
    dest: Path,
    ref: tuple[int, int, bytes],
    cand: tuple[int, int, bytes],
    masks: Sequence[Mapping[str, int]] | None = None,
) -> Path:
    """Write a confined overlay PNG highlighting unmasked changed pixels."""
    target = Path(dest)
    if target.is_symlink():
        raise VisualPixelError("overlay dest must not be a symlink")
    stats = pixel_diff_stats(ref, cand, masks)
    width, height, ref_bytes = ref
    cand_bytes = cand[2]
    skip = _mask_bitmap(width, height, masks or ())
    out = bytearray(cand_bytes)
    hx, hy, hz, ha = HIGHLIGHT_RGBA
    if stats["changed_pixels"]:
        span = width * height
        for index in range(span):
            if skip[index]:
                continue
            off = index * 4
            if ref_bytes[off : off + 4] != cand_bytes[off : off + 4]:
                out[off] = hx
                out[off + 1] = hy
                out[off + 2] = hz
                out[off + 3] = ha
    encoded = encode_png_rgba(width, height, bytes(out))
    if len(encoded) > MAX_IMAGE_BYTES:
        raise VisualPixelError("overlay PNG exceeds Visual Contract V1 size limit")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise VisualPixelError("overlay dest must be a regular file")
    try:
        target.write_bytes(encoded)
    except OSError as exc:
        raise VisualPixelError("overlay PNG is not writable") from exc
    return target


def encode_png_rgba(width: int, height: int, rgba: bytes) -> bytes:
    """Encode an 8-bit RGBA buffer as a non-interlaced PNG."""
    if width < 1 or height < 1 or width > MAX_EDGE or height > MAX_EDGE:
        raise VisualPixelError("PNG dimensions exceed Visual Contract V1 limits")
    if width * height > MAX_PIXELS:
        raise VisualPixelError("PNG pixel count exceeds Visual Contract V1 limits")
    expected = width * height * 4
    if len(rgba) != expected:
        raise VisualPixelError("RGBA buffer length does not match dimensions")
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw.extend(rgba[y * stride : (y + 1) * stride])
    compressed = zlib.compress(bytes(raw), 9)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, COLOR_RGBA, 0, 0, 0)
    return b"".join(
        (
            PNG_SIG,
            _chunk(b"IHDR", ihdr),
            _chunk(b"IDAT", compressed),
            _chunk(b"IEND", b""),
        )
    )


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def _require_rgba(
    value: tuple[int, int, bytes], *, label: str
) -> tuple[int, int, bytes]:
    if not isinstance(value, tuple) or len(value) != 3:
        raise VisualPixelError(f"{label} RGBA tuple is invalid")
    width, height, body = value
    if isinstance(width, bool) or not isinstance(width, int):
        raise VisualPixelError(f"{label} width must be an integer")
    if isinstance(height, bool) or not isinstance(height, int):
        raise VisualPixelError(f"{label} height must be an integer")
    if width < 1 or height < 1 or width > MAX_EDGE or height > MAX_EDGE:
        raise VisualPixelError(f"{label} dimensions exceed Visual Contract V1 limits")
    if width * height > MAX_PIXELS:
        raise VisualPixelError(f"{label} pixel count exceeds Visual Contract V1 limits")
    if not isinstance(body, (bytes, bytearray)):
        raise VisualPixelError(f"{label} pixels must be bytes")
    if len(body) != width * height * 4:
        raise VisualPixelError(f"{label} RGBA buffer length does not match dimensions")
    return width, height, bytes(body)


def _mask_bitmap(
    width: int, height: int, masks: Sequence[Mapping[str, int]]
) -> bytearray:
    skip = bytearray(width * height)
    for index, raw in enumerate(masks):
        if not isinstance(raw, Mapping):
            raise VisualPixelError(f"masks[{index}] must be an object")
        try:
            x = _require_int(raw["x"], label=f"masks[{index}].x", minimum=0)
            y = _require_int(raw["y"], label=f"masks[{index}].y", minimum=0)
            mask_w = _require_int(
                raw["width"], label=f"masks[{index}].width", minimum=1
            )
            mask_h = _require_int(
                raw["height"], label=f"masks[{index}].height", minimum=1
            )
        except KeyError as exc:
            raise VisualPixelError(f"masks[{index}] is missing {exc}") from exc
        if x >= width or y >= height:
            continue
        x1 = min(width, x + mask_w)
        y1 = min(height, y + mask_h)
        if x1 <= x or y1 <= y:
            continue
        for row in range(y, y1):
            start = row * width + x
            end = start + (x1 - x)
            skip[start:end] = b"\x01" * (x1 - x)
    return skip


def _require_int(value: Any, *, label: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VisualPixelError(f"{label} must be an integer")
    if value < minimum:
        raise VisualPixelError(f"{label} must be >= {minimum}")
    return value


def _decode_png_bytes(body: bytes) -> tuple[int, int, bytes]:
    if len(body) < 8 or not body.startswith(PNG_SIG):
        raise VisualPixelError("not a PNG")
    chunks = _read_chunks(body)
    if not chunks or chunks[0][0] != b"IHDR":
        raise VisualPixelError("PNG IHDR is missing")
    if chunks[-1][0] != b"IEND":
        raise VisualPixelError("truncated PNG")
    width, height, bit_depth, color_type, interlace = _parse_ihdr(chunks[0][1])
    palette: list[tuple[int, int, int]] | None = None
    trns: bytes | None = None
    idat = bytearray()
    seen_idat = False
    for tag, data in chunks[1:]:
        if tag == b"IHDR":
            raise VisualPixelError("PNG contains a duplicate IHDR")
        if tag == b"PLTE":
            if seen_idat or palette is not None:
                raise VisualPixelError("PNG PLTE is invalid")
            if len(data) < 3 or len(data) % 3 != 0 or len(data) > 256 * 3:
                raise VisualPixelError("PNG PLTE is invalid")
            palette = [
                (data[i], data[i + 1], data[i + 2]) for i in range(0, len(data), 3)
            ]
            continue
        if tag == b"tRNS":
            if seen_idat or trns is not None:
                raise VisualPixelError("PNG tRNS is invalid")
            trns = data
            continue
        if tag == b"IDAT":
            seen_idat = True
            idat.extend(data)
            if len(idat) > MAX_IMAGE_BYTES:
                raise VisualPixelError("PNG IDAT exceeds size limit")
            continue
        if tag == b"IEND":
            if data:
                raise VisualPixelError("PNG IEND is invalid")
            continue
        if tag[:1].isupper():
            raise VisualPixelError(f"unsupported PNG chunk {tag!r}")
    if not seen_idat:
        raise VisualPixelError("truncated PNG")
    if color_type == COLOR_INDEXED and palette is None:
        raise VisualPixelError("indexed PNG is missing PLTE")
    channels = _channels(color_type)
    bits_pp = bit_depth * channels
    scanline = 1 + ((width * bits_pp + 7) // 8)
    expected_raw = height * scanline
    # Interlace + zlib slack, still bounded by declared pixels.
    max_raw = min(MAX_IMAGE_BYTES, max(expected_raw * 8, expected_raw + 64, 1024))
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(bytes(idat), max_raw)
        if decoder.unconsumed_tail:
            raise VisualPixelError("PNG decompression exceeds bound")
        raw += decoder.flush()
    except zlib.error as exc:
        raise VisualPixelError("truncated PNG") from exc
    if len(raw) > max_raw:
        raise VisualPixelError("PNG decompression exceeds bound")
    rgba = _reconstruct(
        raw,
        width=width,
        height=height,
        bit_depth=bit_depth,
        color_type=color_type,
        bits_pp=bits_pp,
        interlace=interlace,
        palette=palette,
        trns=trns,
    )
    return width, height, rgba


def _read_chunks(body: bytes) -> list[tuple[bytes, bytes]]:
    pos = 8
    chunks: list[tuple[bytes, bytes]] = []
    ended = False
    while pos + 8 <= len(body):
        length = struct.unpack(">I", body[pos : pos + 4])[0]
        tag = body[pos + 4 : pos + 8]
        pos += 8
        if length > MAX_IMAGE_BYTES:
            raise VisualPixelError("PNG chunk exceeds size limit")
        if pos + length + 4 > len(body):
            raise VisualPixelError("truncated PNG")
        data = body[pos : pos + length]
        crc_stored = struct.unpack(">I", body[pos + length : pos + length + 4])[0]
        pos += length + 4
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        if crc != crc_stored:
            raise VisualPixelError("PNG CRC mismatch")
        if len(tag) != 4:
            raise VisualPixelError("PNG chunk type is invalid")
        chunks.append((tag, data))
        if tag == b"IEND":
            ended = True
            break
    if not ended:
        raise VisualPixelError("truncated PNG")
    return chunks


def _parse_ihdr(data: bytes) -> tuple[int, int, int, int, int]:
    if len(data) != 13:
        raise VisualPixelError("PNG IHDR is invalid")
    width, height, bit_depth, color_type, compression, filter_method, interlace = (
        struct.unpack(">IIBBBBB", data)
    )
    if compression != 0 or filter_method != 0:
        raise VisualPixelError("PNG compression/filter method is unsupported")
    if interlace not in {0, 1}:
        raise VisualPixelError("PNG interlace method is unsupported")
    if width < 1 or height < 1 or width > MAX_EDGE or height > MAX_EDGE:
        raise VisualPixelError("PNG dimensions exceed Visual Contract V1 limits")
    if width * height > MAX_PIXELS:
        raise VisualPixelError("PNG pixel count exceeds Visual Contract V1 limits")
    allowed = {
        COLOR_GRAY: {1, 2, 4, 8, 16},
        COLOR_RGB: {8, 16},
        COLOR_INDEXED: {1, 2, 4, 8},
        COLOR_GRAY_A: {8, 16},
        COLOR_RGBA: {8, 16},
    }
    depths = allowed.get(color_type)
    if depths is None or bit_depth not in depths:
        raise VisualPixelError("PNG color type / bit depth is unsupported")
    return width, height, bit_depth, color_type, interlace


def _channels(color_type: int) -> int:
    return {
        COLOR_GRAY: 1,
        COLOR_RGB: 3,
        COLOR_INDEXED: 1,
        COLOR_GRAY_A: 2,
        COLOR_RGBA: 4,
    }[color_type]


def _reconstruct(
    raw: bytes,
    *,
    width: int,
    height: int,
    bit_depth: int,
    color_type: int,
    bits_pp: int,
    interlace: int,
    palette: list[tuple[int, int, int]] | None,
    trns: bytes | None,
) -> bytes:
    bpp = max(1, (bits_pp + 7) // 8)
    out = bytearray(width * height * 4)
    if interlace == 0:
        row_bytes = (width * bits_pp + 7) // 8
        expected = height * (1 + row_bytes)
        if len(raw) != expected:
            raise VisualPixelError("truncated PNG")
        rows = _unfilter(raw, height, row_bytes, bpp)
        _blit_rows(
            out,
            rows,
            width=width,
            height=height,
            bit_depth=bit_depth,
            color_type=color_type,
            palette=palette,
            trns=trns,
            origin_x=0,
            origin_y=0,
            step_x=1,
            step_y=1,
            full_width=width,
        )
        return bytes(out)
    offset = 0
    for origin_x, origin_y, step_x, step_y in ADAM7:
        pass_w = (width - origin_x + step_x - 1) // step_x if origin_x < width else 0
        pass_h = (height - origin_y + step_y - 1) // step_y if origin_y < height else 0
        if pass_w <= 0 or pass_h <= 0:
            continue
        row_bytes = (pass_w * bits_pp + 7) // 8
        expected = pass_h * (1 + row_bytes)
        if offset + expected > len(raw):
            raise VisualPixelError("truncated PNG")
        rows = _unfilter(raw[offset : offset + expected], pass_h, row_bytes, bpp)
        offset += expected
        _blit_rows(
            out,
            rows,
            width=pass_w,
            height=pass_h,
            bit_depth=bit_depth,
            color_type=color_type,
            palette=palette,
            trns=trns,
            origin_x=origin_x,
            origin_y=origin_y,
            step_x=step_x,
            step_y=step_y,
            full_width=width,
        )
    if offset != len(raw):
        raise VisualPixelError("truncated PNG")
    return bytes(out)


def _unfilter(raw: bytes, height: int, row_bytes: int, bpp: int) -> list[bytearray]:
    rows: list[bytearray] = []
    pos = 0
    prev = bytearray(row_bytes)
    for _y in range(height):
        ftype = raw[pos]
        pos += 1
        filt = raw[pos : pos + row_bytes]
        pos += row_bytes
        recon = bytearray(row_bytes)
        if ftype == 0:
            recon[:] = filt
        elif ftype == 1:
            for i, value in enumerate(filt):
                left = recon[i - bpp] if i >= bpp else 0
                recon[i] = (value + left) & 0xFF
        elif ftype == 2:
            for i, value in enumerate(filt):
                recon[i] = (value + prev[i]) & 0xFF
        elif ftype == 3:
            for i, value in enumerate(filt):
                left = recon[i - bpp] if i >= bpp else 0
                recon[i] = (value + ((left + prev[i]) // 2)) & 0xFF
        elif ftype == 4:
            for i, value in enumerate(filt):
                left = recon[i - bpp] if i >= bpp else 0
                up = prev[i]
                ul = prev[i - bpp] if i >= bpp else 0
                recon[i] = (value + _paeth(left, up, ul)) & 0xFF
        else:
            raise VisualPixelError("PNG filter type is unsupported")
        rows.append(recon)
        prev = recon
    return rows


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _blit_rows(
    out: bytearray,
    rows: Sequence[bytearray],
    *,
    width: int,
    height: int,
    bit_depth: int,
    color_type: int,
    palette: list[tuple[int, int, int]] | None,
    trns: bytes | None,
    origin_x: int,
    origin_y: int,
    step_x: int,
    step_y: int,
    full_width: int,
) -> None:
    for py, row in enumerate(rows):
        samples = _unpack_samples(row, width, bit_depth, _channels(color_type))
        y = origin_y + py * step_y
        for px in range(width):
            x = origin_x + px * step_x
            rgba = _sample_rgba(samples, px, color_type, bit_depth, palette, trns)
            off = (y * full_width + x) * 4
            out[off : off + 4] = rgba


def _unpack_samples(
    row: bytearray, width: int, bit_depth: int, channels: int
) -> list[int]:
    count = width * channels
    if bit_depth == 8:
        return list(row[:count])
    if bit_depth == 16:
        if len(row) < count * 2:
            raise VisualPixelError("truncated PNG")
        out = []
        for i in range(count):
            out.append((row[i * 2] << 8) | row[i * 2 + 1])
        return out
    bits = bit_depth
    mask = (1 << bits) - 1
    out: list[int] = []
    buffer = 0
    nbits = 0
    for byte in row:
        buffer = (buffer << 8) | byte
        nbits += 8
        while nbits >= bits and len(out) < count:
            nbits -= bits
            out.append((buffer >> nbits) & mask)
        buffer &= (1 << nbits) - 1 if nbits else 0
    if len(out) < count:
        raise VisualPixelError("truncated PNG")
    return out[:count]


def _sample_rgba(
    samples: Sequence[int],
    px: int,
    color_type: int,
    bit_depth: int,
    palette: list[tuple[int, int, int]] | None,
    trns: bytes | None,
) -> bytes:
    maxv = (1 << bit_depth) - 1
    scale8 = 255 if maxv == 255 else None

    def to8(value: int) -> int:
        if scale8 is not None:
            return value
        if maxv <= 0:
            return 0
        return (value * 255 + maxv // 2) // maxv

    if color_type == COLOR_GRAY:
        g = to8(samples[px])
        alpha = 255
        if trns is not None and len(trns) >= 2:
            key = (trns[0] << 8) | trns[1]
            if samples[px] == key:
                alpha = 0
        return bytes((g, g, g, alpha))
    if color_type == COLOR_GRAY_A:
        g = to8(samples[px * 2])
        a = to8(samples[px * 2 + 1])
        return bytes((g, g, g, a))
    if color_type == COLOR_RGB:
        r = to8(samples[px * 3])
        g = to8(samples[px * 3 + 1])
        b = to8(samples[px * 3 + 2])
        alpha = 255
        if trns is not None and len(trns) >= 6:
            kr = (trns[0] << 8) | trns[1]
            kg = (trns[2] << 8) | trns[3]
            kb = (trns[4] << 8) | trns[5]
            if (
                samples[px * 3] == kr
                and samples[px * 3 + 1] == kg
                and samples[px * 3 + 2] == kb
            ):
                alpha = 0
        return bytes((r, g, b, alpha))
    if color_type == COLOR_RGBA:
        r = to8(samples[px * 4])
        g = to8(samples[px * 4 + 1])
        b = to8(samples[px * 4 + 2])
        a = to8(samples[px * 4 + 3])
        return bytes((r, g, b, a))
    if color_type == COLOR_INDEXED:
        if palette is None:
            raise VisualPixelError("indexed PNG is missing PLTE")
        idx = samples[px]
        if idx >= len(palette):
            raise VisualPixelError("PNG palette index is out of range")
        r, g, b = palette[idx]
        alpha = 255
        if trns is not None and idx < len(trns):
            alpha = trns[idx]
        return bytes((r, g, b, alpha))
    raise VisualPixelError("PNG color type is unsupported")


__all__ = [
    "VisualPixelError",
    "decode_png_rgba",
    "encode_png_rgba",
    "pixel_diff_stats",
    "write_overlay_png",
]
