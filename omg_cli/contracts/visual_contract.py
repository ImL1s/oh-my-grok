"""Pure Visual Contract V1 — copy-safe comparison schema, scores, and digest.

This module is side-effect free: no filesystem, process, network, clock, or
image decoding. It never emits approved/passes/verified and never carries
image bytes or base64. Callers may persist results; this module does not.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
COMPARISON_KIND = "omg.visual.comparison"
RESULT_KIND = "omg.visual.comparison_result"
SCORE_SCALE = 10000
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_EDGE = 16384
MAX_PIXELS = 100_000_000
MAX_MASKS = 256
MAX_MASK_UNION_PERCENT = 25
MAX_TASK_CRITERIA_CHARS = 8192
MAX_COMPAT_TEXT_CHARS = 128
MAX_DPR_MILLI = 16000

MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})

DIMENSION_IDS: tuple[str, ...] = (
    "geometry_layout_alignment",
    "spacing_sizing",
    "typography",
    "color_contrast",
    "component_state",
    "missing_extra_elements",
    "overflow_clipping_responsiveness",
    "imagery_icons",
    "accessibility_visible",
    "task_specific_behavior",
)

COMPAT_FIELDS: tuple[str, ...] = (
    "viewport_width",
    "viewport_height",
    "dpr_milli",
    "platform",
    "theme",
    "locale",
)

INPUT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "reference",
        "candidate",
        "reference_compatibility",
        "candidate_compatibility",
        "dimensions",
        "threshold",
        "masks",
        "task_criteria",
    }
)
IMAGE_KEYS = frozenset({"path", "sha256", "media_type", "byte_size", "width", "height"})
DIMENSION_KEYS = frozenset({"id", "score", "weight"})
MASK_KEYS = frozenset({"x", "y", "width", "height"})
SCORED_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "status",
        "aggregate",
        "threshold",
        "dimensions",
        "masks",
        "comparison_digest",
    }
)
BLOCKED_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "status",
        "block_code",
        "block_field",
        "comparison_digest",
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_RESULT_KEYS = frozenset({"approved", "passes", "verified"})


class VisualContractError(ValueError):
    """Visual Contract V1 input failed before any comparison verdict."""


def canonical_json_bytes(value: Any) -> bytes:
    """Encode canonical JSON v1: compact UTF-8, sorted keys, no newline."""
    _validate_canonical_value(value)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise VisualContractError(f"canonical JSON encoding failed: {exc}") from exc
    return text.encode("utf-8")


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def validate_image_descriptor(value: Any) -> dict[str, Any]:
    image = _require_object(value, label="image descriptor")
    _require_exact_keys(image, IMAGE_KEYS, label="image descriptor")
    path = _require_canonical_relpath(image["path"], label="path")
    digest = image["sha256"]
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise VisualContractError("sha256 must be lowercase SHA-256 hex")
    media_type = image["media_type"]
    if not isinstance(media_type, str) or media_type not in MEDIA_TYPES:
        raise VisualContractError("media_type is not in the v1 set")
    byte_size = _require_int(
        image["byte_size"], label="byte_size", minimum=1, maximum=MAX_IMAGE_BYTES
    )
    width = _require_int(image["width"], label="width", minimum=1, maximum=MAX_EDGE)
    height = _require_int(image["height"], label="height", minimum=1, maximum=MAX_EDGE)
    if width * height > MAX_PIXELS:
        raise VisualContractError("image pixel count exceeds 100000000")
    return {
        "path": path,
        "sha256": digest,
        "media_type": media_type,
        "byte_size": byte_size,
        "width": width,
        "height": height,
    }


def validate_compatibility(value: Any) -> dict[str, Any]:
    compat = _require_object(value, label="compatibility")
    _require_exact_keys(compat, frozenset(COMPAT_FIELDS), label="compatibility")
    return _validate_complete_compatibility(compat)


def validate_dimensions(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise VisualContractError("dimensions must be an array")
    if len(value) != len(DIMENSION_IDS):
        raise VisualContractError("dimensions must contain exactly ten rows")
    seen: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}
    weight_sum = 0
    for index, raw in enumerate(value):
        row = _require_object(raw, label=f"dimensions[{index}]")
        _require_exact_keys(row, DIMENSION_KEYS, label=f"dimensions[{index}]")
        dim_id = row["id"]
        if not isinstance(dim_id, str) or dim_id not in DIMENSION_IDS:
            raise VisualContractError(f"unknown dimension id at dimensions[{index}]")
        if dim_id in seen:
            raise VisualContractError(f"duplicate dimension id at dimensions[{index}]")
        seen.add(dim_id)
        score = _require_int(
            row["score"], label=f"dimensions[{index}].score", minimum=0, maximum=SCORE_SCALE
        )
        weight = _require_int(
            row["weight"], label=f"dimensions[{index}].weight", minimum=1
        )
        weight_sum += weight
        by_id[dim_id] = {"id": dim_id, "score": score, "weight": weight}
    missing = [dim_id for dim_id in DIMENSION_IDS if dim_id not in seen]
    if missing:
        raise VisualContractError(f"missing dimension ids {missing!r}")
    if weight_sum != SCORE_SCALE:
        raise VisualContractError("dimension weights must sum to exactly 10000")
    return [by_id[dim_id] for dim_id in DIMENSION_IDS]


def validate_masks(value: Any, *, width: int, height: int) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise VisualContractError("masks must be an array")
    if len(value) > MAX_MASKS:
        raise VisualContractError(f"masks must contain at most {MAX_MASKS} rectangles")
    _require_int(width, label="mask space width", minimum=1)
    _require_int(height, label="mask space height", minimum=1)
    canonical: list[dict[str, int]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for index, raw in enumerate(value):
        item = _require_object(raw, label=f"masks[{index}]")
        _require_exact_keys(item, MASK_KEYS, label=f"masks[{index}]")
        x = _require_int(item["x"], label=f"masks[{index}].x", minimum=0)
        y = _require_int(item["y"], label=f"masks[{index}].y", minimum=0)
        mask_w = _require_int(item["width"], label=f"masks[{index}].width", minimum=1)
        mask_h = _require_int(item["height"], label=f"masks[{index}].height", minimum=1)
        if x + mask_w > width or y + mask_h > height:
            raise VisualContractError(f"masks[{index}] is out of bounds")
        key = (x, y, mask_w, mask_h)
        if key in seen:
            continue
        seen.add(key)
        canonical.append({"x": x, "y": y, "width": mask_w, "height": mask_h})
    canonical.sort(key=lambda row: (row["x"], row["y"], row["width"], row["height"]))
    union = mask_union_area(canonical)
    if union * 100 > MAX_MASK_UNION_PERCENT * width * height:
        raise VisualContractError("masked union exceeds 25 percent of reference pixels")
    return canonical


def mask_union_area(masks: Sequence[Mapping[str, int]]) -> int:
    """Overlap-safe union area. Scanline; never allocates a pixel bitmap."""
    if not masks:
        return 0
    events: list[tuple[int, int, int, int]] = []
    for mask in masks:
        x = mask["x"]
        y = mask["y"]
        width = mask["width"]
        height = mask["height"]
        events.append((x, 1, y, y + height))
        events.append((x + width, -1, y, y + height))
    events.sort()
    area = 0
    active: list[tuple[int, int]] = []
    prev_x = events[0][0]
    index = 0
    while index < len(events):
        x = events[index][0]
        if x > prev_x and active:
            area += (x - prev_x) * _covered_length(active)
        while index < len(events) and events[index][0] == x:
            _delta, kind, y0, y1 = events[index]
            if kind == 1:
                active.append((y0, y1))
            else:
                active.remove((y0, y1))
            index += 1
        prev_x = x
    return area


def aggregate_score(rows: Sequence[Mapping[str, int]]) -> int:
    total = 0
    for row in rows:
        total += row["score"] * row["weight"]
    return (total + SCORE_SCALE // 2) // SCORE_SCALE


def comparison_digest(binding: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes(dict(binding)))


def compare(document: Any) -> dict[str, Any]:
    """Validate a comparison document and return scored or blocked output."""
    payload = _require_object(document, label="comparison")
    _require_exact_keys(payload, INPUT_KEYS, label="comparison")
    version = _require_int(payload["schema_version"], label="schema_version")
    if version != SCHEMA_VERSION:
        raise VisualContractError("schema_version must be the integer 1")
    if payload["kind"] != COMPARISON_KIND:
        raise VisualContractError("kind must be omg.visual.comparison")
    reference = validate_image_descriptor(payload["reference"])
    candidate = validate_image_descriptor(payload["candidate"])
    dimensions = validate_dimensions(payload["dimensions"])
    threshold = _require_int(
        payload["threshold"], label="threshold", minimum=0, maximum=SCORE_SCALE
    )
    task_criteria = _require_text(
        payload["task_criteria"],
        label="task_criteria",
        minimum=1,
        maximum=MAX_TASK_CRITERIA_CHARS,
    )
    masks = validate_masks(
        payload["masks"], width=reference["width"], height=reference["height"]
    )
    ref_compat, ref_missing = _inspect_compatibility(
        payload["reference_compatibility"], label="reference_compatibility"
    )
    cand_compat, cand_missing = _inspect_compatibility(
        payload["candidate_compatibility"], label="candidate_compatibility"
    )
    binding = {
        "candidate": candidate,
        "candidate_compatibility": cand_compat,
        "dimensions": dimensions,
        "kind": COMPARISON_KIND,
        "masks": masks,
        "reference": reference,
        "reference_compatibility": ref_compat,
        "schema_version": SCHEMA_VERSION,
        "task_criteria": task_criteria,
        "threshold": threshold,
    }
    digest = comparison_digest(binding)
    if ref_missing is not None:
        return _blocked("compatibility_missing", ref_missing, digest)
    if cand_missing is not None:
        return _blocked("compatibility_missing", cand_missing, digest)
    if reference["width"] != candidate["width"]:
        return _blocked("image_dimension_mismatch", "image_width", digest)
    if reference["height"] != candidate["height"]:
        return _blocked("image_dimension_mismatch", "image_height", digest)
    for field in COMPAT_FIELDS:
        if ref_compat[field] != cand_compat[field]:
            return _blocked("compatibility_mismatch", field, digest)
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "status": "scored",
        "aggregate": aggregate_score(dimensions),
        "threshold": threshold,
        "dimensions": dimensions,
        "masks": masks,
        "comparison_digest": digest,
    }
    _assert_honest_result(result)
    return result


def _blocked(code: str, field: str, digest: str) -> dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "status": "blocked",
        "block_code": code,
        "block_field": field,
        "comparison_digest": digest,
    }
    _assert_honest_result(result)
    return result


def _assert_honest_result(result: Mapping[str, Any]) -> None:
    if _FORBIDDEN_RESULT_KEYS.intersection(result):
        raise VisualContractError("result must not contain approved/passes/verified")
    expected = SCORED_RESULT_KEYS if result.get("status") == "scored" else BLOCKED_RESULT_KEYS
    _require_exact_keys(result, expected, label="comparison result")


def _validate_canonical_value(value: Any, *, path: str = "$") -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, str):
        for character in value:
            codepoint = ord(character)
            if 0xD800 <= codepoint <= 0xDFFF:
                raise VisualContractError(f"{path} contains an unpaired surrogate")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_canonical_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise VisualContractError(f"{path} has a non-string object key")
            _validate_canonical_value(key, path=f"{path}.<key>")
            _validate_canonical_value(item, path=f"{path}.{key}")
        return
    raise VisualContractError(
        f"{path} uses unsupported type {type(value).__name__}; "
        "canonical JSON v1 permits null/bool/string/integer/array/object only"
    )


def _require_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise VisualContractError(f"{label} must be an object")
    return dict(value)


def _require_exact_keys(value: Mapping[str, Any], required: frozenset[str], *, label: str) -> None:
    keys = set(value)
    missing = required - keys
    extra = keys - required
    if missing or extra:
        raise VisualContractError(
            f"{label} key mismatch: missing={sorted(missing)!r} extra={sorted(extra)!r}"
        )


def _require_int(
    value: Any,
    *,
    label: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VisualContractError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise VisualContractError(f"{label} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise VisualContractError(f"{label} must be <= {maximum}")
    return value


def _require_text(value: Any, *, label: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise VisualContractError(f"{label} must be a string")
    if not minimum <= len(value) <= maximum:
        raise VisualContractError(f"{label} length must be {minimum}..{maximum}")
    for character in value:
        codepoint = ord(character)
        if codepoint < 0x20 or 0xD800 <= codepoint <= 0xDFFF:
            raise VisualContractError(f"{label} contains a control or surrogate")
    return value


def _require_canonical_relpath(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise VisualContractError(f"{label} must be a non-empty string")
    for character in value:
        codepoint = ord(character)
        if codepoint < 0x20 or 0xD800 <= codepoint <= 0xDFFF:
            raise VisualContractError(f"{label} contains a control or surrogate")
    if "\\" in value:
        raise VisualContractError(f"{label} must use POSIX separators")
    if value.startswith("/") or (len(value) >= 2 and value[1] == ":"):
        raise VisualContractError(f"{label} must be workspace-relative")
    if value == "." or value.startswith("./") or value.endswith("/") or "//" in value:
        raise VisualContractError(f"{label} is not a canonical relative POSIX path")
    posix = PurePosixPath(value)
    if posix.is_absolute() or "." in posix.parts or ".." in posix.parts:
        raise VisualContractError(f"{label} must not be absolute or traverse")
    if posix.as_posix() != value:
        raise VisualContractError(f"{label} is not a canonical relative POSIX path")
    return value


def _validate_complete_compatibility(compat: Mapping[str, Any]) -> dict[str, Any]:
    viewport_width = _require_int(
        compat["viewport_width"],
        label="viewport_width",
        minimum=1,
        maximum=MAX_EDGE,
    )
    viewport_height = _require_int(
        compat["viewport_height"],
        label="viewport_height",
        minimum=1,
        maximum=MAX_EDGE,
    )
    dpr_milli = _require_int(
        compat["dpr_milli"], label="dpr_milli", minimum=1, maximum=MAX_DPR_MILLI
    )
    platform = _require_text(
        compat["platform"], label="platform", minimum=1, maximum=MAX_COMPAT_TEXT_CHARS
    )
    theme = _require_text(
        compat["theme"], label="theme", minimum=1, maximum=MAX_COMPAT_TEXT_CHARS
    )
    locale = _require_text(
        compat["locale"], label="locale", minimum=1, maximum=MAX_COMPAT_TEXT_CHARS
    )
    return {
        "viewport_width": viewport_width,
        "viewport_height": viewport_height,
        "dpr_milli": dpr_milli,
        "platform": platform,
        "theme": theme,
        "locale": locale,
    }


def _inspect_compatibility(value: Any, *, label: str) -> tuple[dict[str, Any], str | None]:
    compat = _require_object(value, label=label)
    extra = set(compat) - set(COMPAT_FIELDS)
    if extra:
        raise VisualContractError(
            f"{label} key mismatch: missing=[] extra={sorted(extra)!r}"
        )
    canonical: dict[str, Any] = {}
    missing_field: str | None = None
    for field in COMPAT_FIELDS:
        if field not in compat:
            canonical[field] = None
            if missing_field is None:
                missing_field = field
            continue
        raw = compat[field]
        if raw is None or raw == "":
            canonical[field] = None
            if missing_field is None:
                missing_field = field
            continue
        if field in {"viewport_width", "viewport_height"}:
            canonical[field] = _require_int(
                raw, label=f"{label}.{field}", minimum=1, maximum=MAX_EDGE
            )
        elif field == "dpr_milli":
            canonical[field] = _require_int(
                raw, label=f"{label}.{field}", minimum=1, maximum=MAX_DPR_MILLI
            )
        else:
            canonical[field] = _require_text(
                raw,
                label=f"{label}.{field}",
                minimum=1,
                maximum=MAX_COMPAT_TEXT_CHARS,
            )
    return canonical, missing_field


def _covered_length(intervals: Sequence[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    ordered = sorted(intervals)
    total = 0
    start, end = ordered[0]
    for next_start, next_end in ordered[1:]:
        if next_start > end:
            total += end - start
            start, end = next_start, next_end
        elif next_end > end:
            end = next_end
    total += end - start
    return total
