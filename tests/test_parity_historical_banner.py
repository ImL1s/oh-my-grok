"""Historical research parity docs carry a NON-AUTHORITATIVE banner."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

BANNER_MARKER = "HISTORICAL / NON-AUTHORITATIVE"

HISTORICAL_DOCS = (
    (
        ROOT / "docs/research/core-parity-matrix-2026-07-20.md",
        "../parity/omg-parity.json",
        "../parity/FEATURE-MATRIX.md",
    ),
    (
        ROOT / "docs/research/omc-parity-council/README.md",
        "../../parity/omg-parity.json",
        "../../parity/FEATURE-MATRIX.md",
    ),
)


def test_historical_parity_docs_carry_non_authoritative_banner() -> None:
    for path, inventory_link, matrix_link in HISTORICAL_DOCS:
        text = path.read_text(encoding="utf-8")
        assert BANNER_MARKER in text, f"missing banner in {path}"
        assert text.count(BANNER_MARKER) == 1, f"duplicate banner in {path}"
        assert inventory_link in text, f"missing inventory link in {path}"
        assert matrix_link in text, f"missing matrix link in {path}"
        assert "predates the v2 parity inventory" in text
