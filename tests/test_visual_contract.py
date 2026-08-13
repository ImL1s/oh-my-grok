from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from omg_cli.contracts.visual_contract import (
    COMPARISON_KIND,
    DIMENSION_IDS,
    MAX_IMAGE_BYTES,
    MAX_MASKS,
    RESULT_KIND,
    SCHEMA_VERSION,
    VisualContractError,
    aggregate_score,
    compare,
    comparison_digest,
    mask_union_area,
    validate_compatibility,
    validate_dimensions,
    validate_image_descriptor,
    validate_masks,
)

MODULE_PATH = Path(__file__).resolve().parents[1] / "omg_cli" / "contracts" / "visual_contract.py"
ALLOWED_IMPORT_ROOTS = {
    "__future__",
    "collections",
    "hashlib",
    "json",
    "pathlib",
    "re",
    "typing",
}


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
        "schema_version": SCHEMA_VERSION,
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


def test_happy_path_orders_dimensions_and_masks_and_aggregates() -> None:
    shuffled_dims = list(reversed(_dims(scores=[10000, 0, 5000, 5000, 2500, 7500, 10000, 0, 3333, 6667])))
    shuffled_masks = [
        {"x": 10, "y": 10, "width": 4, "height": 4},
        {"x": 0, "y": 0, "width": 2, "height": 2},
        {"x": 0, "y": 0, "width": 2, "height": 2},
    ]
    first = compare(_document(dimensions=shuffled_dims, masks=shuffled_masks))
    second = compare(_document(dimensions=shuffled_dims, masks=shuffled_masks))
    assert first["status"] == "scored"
    assert first["kind"] == RESULT_KIND
    assert first["aggregate"] == 5000
    assert [row["id"] for row in first["dimensions"]] == list(DIMENSION_IDS)
    assert first["masks"] == [
        {"x": 0, "y": 0, "width": 2, "height": 2},
        {"x": 10, "y": 10, "width": 4, "height": 4},
    ]
    assert first == second
    assert first["comparison_digest"] == second["comparison_digest"]
    assert set(first) == {
        "schema_version",
        "kind",
        "status",
        "aggregate",
        "threshold",
        "dimensions",
        "masks",
        "comparison_digest",
    }


def test_half_up_aggregate_is_integer_only() -> None:
    weights = [5000, 1, 1, 1, 1, 1, 1, 1, 1, 4992]
    up = _dims(scores=[1, 0, 0, 0, 0, 0, 0, 0, 0, 0], weights=weights)
    down_weights = [4999, 1, 1, 1, 1, 1, 1, 1, 1, 4993]
    down = _dims(scores=[1, 0, 0, 0, 0, 0, 0, 0, 0, 0], weights=down_weights)
    assert aggregate_score(up) == 1
    assert compare(_document(dimensions=up))["aggregate"] == 1
    assert compare(_document(dimensions=down))["aggregate"] == 0


@pytest.mark.parametrize(
    "mutator",
    [
        lambda doc: doc.__setitem__("schema_version", True),
        lambda doc: doc.__setitem__("schema_version", 1.0),
        lambda doc: doc.__setitem__("schema_version", 1 + 0j),
        lambda doc: doc.__setitem__("schema_version", 2),
        lambda doc: doc.__setitem__("kind", "omg.visual.other"),
        lambda doc: doc.__setitem__("threshold", True),
        lambda doc: doc.__setitem__("threshold", -1),
        lambda doc: doc.__setitem__("threshold", 10001),
        lambda doc: doc.pop("masks"),
        lambda doc: doc.__setitem__("extra", 1),
    ],
)
def test_schema_bool_range_and_key_failures(mutator: object) -> None:
    document = _document()
    mutator(document)  # type: ignore[operator]
    with pytest.raises(VisualContractError):
        compare(document)


def test_weight_and_dimension_identity_failures() -> None:
    with pytest.raises(VisualContractError, match="sum"):
        validate_dimensions(_dims(weights=[1000] * 9 + [999]))
    unknown = _dims()
    unknown[0]["id"] = "not_a_dimension"
    with pytest.raises(VisualContractError, match="unknown"):
        validate_dimensions(unknown)
    missing = _dims()[1:]
    with pytest.raises(VisualContractError, match="exactly ten"):
        validate_dimensions(missing)
    duplicate = _dims()
    duplicate[1]["id"] = DIMENSION_IDS[0]
    with pytest.raises(VisualContractError, match="duplicate"):
        validate_dimensions(duplicate)
    bool_score = _dims()
    bool_score[0]["score"] = True
    with pytest.raises(VisualContractError, match="integer"):
        validate_dimensions(bool_score)


@pytest.mark.parametrize("field", [
    "viewport_width",
    "viewport_height",
    "dpr_milli",
    "platform",
    "theme",
    "locale",
])
def test_every_compatibility_mismatch_is_blocked(field: str) -> None:
    candidate = _compat()
    if field in {"viewport_width", "viewport_height", "dpr_milli"}:
        current = candidate[field]
        assert isinstance(current, int)
        candidate[field] = current + 1
    else:
        candidate[field] = "zh-Hant"
    result = compare(_document(candidate_compatibility=candidate))
    assert result["status"] == "blocked"
    assert result["block_code"] == "compatibility_mismatch"
    assert result["block_field"] == field
    assert "aggregate" not in result
    assert result["comparison_digest"]


@pytest.mark.parametrize(
    ("candidate_overrides", "block_field"),
    [
        ({"width": 199}, "image_width"),
        ({"height": 199}, "image_height"),
    ],
)
def test_image_dimension_mismatch_is_blocked_before_verdict(
    candidate_overrides: dict[str, object], block_field: str
) -> None:
    candidate = _image(path="fixtures/visual/cur.png", sha256="b" * 64)
    candidate.update(candidate_overrides)
    result = compare(_document(candidate=candidate))
    assert result["status"] == "blocked"
    assert result["block_code"] == "image_dimension_mismatch"
    assert result["block_field"] == block_field
    assert "aggregate" not in result
    assert "threshold" not in result
    assert result["comparison_digest"]


@pytest.mark.parametrize("field", [
    "viewport_width",
    "viewport_height",
    "dpr_milli",
    "platform",
    "theme",
    "locale",
])
@pytest.mark.parametrize("blank", [None, "", "absent"])
def test_missing_or_empty_compatibility_is_blocked(field: str, blank: object) -> None:
    compat = _compat()
    if blank == "absent":
        del compat[field]
    else:
        compat[field] = blank
    result = compare(_document(reference_compatibility=compat))
    assert result["status"] == "blocked"
    assert result["block_code"] == "compatibility_missing"
    assert result["block_field"] == field
    assert "aggregate" not in result


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/ref.png",
        "C:/ref.png",
        "../secret.png",
        "foo/../bar.png",
        "dir\\file.png",
        "has\nnewline.png",
        "foo//bar.png",
        "./rel.png",
        ".",
        "dir/",
    ],
)
def test_path_caps_reject_absolute_traversal_and_non_canonical(path: str) -> None:
    with pytest.raises(VisualContractError):
        validate_image_descriptor(_image(path=path))


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64, "a" * 65])
def test_hash_must_be_lowercase_sha256(digest: str) -> None:
    with pytest.raises(VisualContractError, match="sha256"):
        validate_image_descriptor(_image(sha256=digest))


@pytest.mark.parametrize(
    "overrides",
    [
        {"byte_size": 0},
        {"byte_size": MAX_IMAGE_BYTES + 1},
        {"width": 16385},
        {"height": 16385},
        {"width": 10001, "height": 10000},
        {"width": 100_000_001, "height": 1},
        {"width": True, "height": 10},
    ],
)
def test_size_and_pixel_caps(overrides: dict[str, object]) -> None:
    with pytest.raises(VisualContractError):
        validate_image_descriptor(_image(**overrides))


def test_max_legal_image_bounds_are_accepted() -> None:
    assert validate_image_descriptor(
        _image(byte_size=MAX_IMAGE_BYTES, width=16384, height=6103)
    )["width"] == 16384


def test_masks_empty_boundary_overlap_oob_zero_and_percent_cap() -> None:
    assert validate_masks([], width=100, height=100) == []
    assert validate_masks(
        [{"x": 99, "y": 99, "width": 1, "height": 1}], width=100, height=100
    ) == [{"x": 99, "y": 99, "width": 1, "height": 1}]
    overlapped = validate_masks(
        [
            {"x": 0, "y": 0, "width": 50, "height": 50},
            {"x": 25, "y": 0, "width": 50, "height": 50},
        ],
        width=200,
        height=200,
    )
    assert mask_union_area(overlapped) == 3750
    assert mask_union_area(overlapped) != 5000
    with pytest.raises(VisualContractError, match="out of bounds"):
        validate_masks([{"x": 90, "y": 0, "width": 20, "height": 10}], width=100, height=100)
    with pytest.raises(VisualContractError):
        validate_masks([{"x": 0, "y": 0, "width": 0, "height": 10}], width=100, height=100)
    with pytest.raises(VisualContractError, match="25 percent"):
        validate_masks([{"x": 0, "y": 0, "width": 51, "height": 50}], width=100, height=100)
    exact = validate_masks([{"x": 0, "y": 0, "width": 50, "height": 50}], width=100, height=100)
    assert mask_union_area(exact) == 2500


def test_mask_count_cap_is_checked_before_dedup_and_union_work() -> None:
    mask = {"x": 0, "y": 0, "width": 1, "height": 1}
    assert validate_masks([mask] * MAX_MASKS, width=100, height=100) == [mask]
    with pytest.raises(VisualContractError, match=rf"at most {MAX_MASKS}"):
        validate_masks([mask] * (MAX_MASKS + 1), width=100, height=100)


def test_digest_mutation_matrix_changes_binding() -> None:
    baseline = compare(_document(masks=[{"x": 1, "y": 1, "width": 2, "height": 2}]))
    digest = baseline["comparison_digest"]
    mutations = [
        _document(task_criteria="other task", masks=[{"x": 1, "y": 1, "width": 2, "height": 2}]),
        _document(
            dimensions=_dims(scores=[9999, 10000, 10000, 10000, 10000, 10000, 10000, 10000, 10000, 10000]),
            masks=[{"x": 1, "y": 1, "width": 2, "height": 2}],
        ),
        _document(
            dimensions=_dims(weights=[999, 1001, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000]),
            masks=[{"x": 1, "y": 1, "width": 2, "height": 2}],
        ),
        _document(threshold=7999, masks=[{"x": 1, "y": 1, "width": 2, "height": 2}]),
        _document(masks=[{"x": 1, "y": 1, "width": 2, "height": 2}, {"x": 8, "y": 8, "width": 2, "height": 2}]),
        _document(masks=[]),
        _document(masks=[{"x": 2, "y": 1, "width": 2, "height": 2}]),
    ]
    for document in mutations:
        mutated = compare(document)["comparison_digest"]
        assert mutated != digest
    binding = {
        "candidate": validate_image_descriptor(_image(path="fixtures/visual/cur.png", sha256="b" * 64)),
        "candidate_compatibility": validate_compatibility(_compat()),
        "dimensions": validate_dimensions(_dims()),
        "kind": COMPARISON_KIND,
        "masks": [{"x": 1, "y": 1, "width": 2, "height": 2}],
        "reference": validate_image_descriptor(_image()),
        "reference_compatibility": validate_compatibility(_compat()),
        "schema_version": SCHEMA_VERSION,
        "task_criteria": "primary CTA visible",
        "threshold": 8000,
    }
    assert comparison_digest(binding) == digest


@pytest.mark.parametrize("side", ["reference", "candidate"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "fixtures/visual/other.png"),
        ("sha256", "c" * 64),
        ("media_type", "image/jpeg"),
        ("byte_size", 2048),
        ("width", 199),
        ("height", 199),
    ],
)
def test_digest_binds_each_reference_and_candidate_image_field(
    side: str, field: str, value: object
) -> None:
    masks = [{"x": 1, "y": 1, "width": 2, "height": 2}]
    baseline = compare(_document(masks=masks))["comparison_digest"]
    document = _document(masks=masks)
    image = copy.deepcopy(document[side])
    assert isinstance(image, dict)
    image[field] = value
    document[side] = image
    assert compare(document)["comparison_digest"] != baseline


@pytest.mark.parametrize("side", ["reference_compatibility", "candidate_compatibility"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("viewport_width", 1279),
        ("viewport_height", 719),
        ("dpr_milli", 1999),
        ("platform", "linux"),
        ("theme", "light"),
        ("locale", "zh-Hant"),
    ],
)
def test_digest_binds_each_reference_and_candidate_compatibility_field(
    side: str, field: str, value: object
) -> None:
    masks = [{"x": 1, "y": 1, "width": 2, "height": 2}]
    baseline = compare(_document(masks=masks))["comparison_digest"]
    document = _document(masks=masks)
    compatibility = copy.deepcopy(document[side])
    assert isinstance(compatibility, dict)
    compatibility[field] = value
    document[side] = compatibility
    assert compare(document)["comparison_digest"] != baseline


def test_cjk_and_long_task_criteria_are_safe() -> None:
    criteria = "核對主按鈕可見。" + ("長" * 4000)
    result = compare(
        _document(
            task_criteria=criteria,
            reference_compatibility=_compat(locale="zh-Hant", theme="深色", platform="macOS-桌面"),
            candidate_compatibility=_compat(locale="zh-Hant", theme="深色", platform="macOS-桌面"),
            reference=_image(path="fixtures/visual/截圖.png"),
        )
    )
    assert result["status"] == "scored"
    assert len(result["comparison_digest"]) == 64


def test_serialization_omits_bytes_base64_and_gate_words() -> None:
    result = compare(_document())
    encoded = json.dumps(result, ensure_ascii=False)
    for token in ("approved", "passes", "verified", "base64", "iVBOR"):
        assert token not in encoded
    dumped = json.dumps(_document(), ensure_ascii=False)
    assert "base64" not in dumped
    assert "\\u0000" not in encoded


def test_module_import_purity() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".", 1)[0] in ALLOWED_IMPORT_ROOTS
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None
            assert node.module.split(".", 1)[0] in ALLOWED_IMPORT_ROOTS


def test_compare_does_not_mutate_input() -> None:
    document = _document(masks=[{"x": 3, "y": 1, "width": 1, "height": 1}])
    snapshot = copy.deepcopy(document)
    compare(document)
    assert document == snapshot
