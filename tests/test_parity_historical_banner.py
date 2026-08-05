"""Historical research parity docs carry a NON-AUTHORITATIVE banner."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

BANNER_MARKER = "HISTORICAL / NON-AUTHORITATIVE"


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_parity_docs",
        ROOT / "scripts" / "generate_parity_docs.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_historical_banner_targets_cover_council_and_research_readme() -> None:
    gen = _load_generator()
    targets = gen.historical_banner_targets()
    paths = {path for path, _ in targets}
    assert ROOT / "docs/research/core-parity-matrix-2026-07-20.md" in paths
    assert ROOT / "docs/research/README.md" in paths
    council = ROOT / "docs/research/omc-parity-council"
    council_mds = set(council.rglob("*.md"))
    assert council_mds, "expected omc-parity-council markdown docs"
    assert council_mds <= paths
    # Relative links must resolve to docs/parity from each file's directory.
    for path, parity_rel in targets:
        assert (path.parent / parity_rel / "omg-parity.json").resolve() == (
            ROOT / "docs/parity/omg-parity.json"
        ).resolve()


def test_historical_parity_docs_carry_non_authoritative_banner() -> None:
    gen = _load_generator()
    for path, parity_rel in gen.historical_banner_targets():
        text = path.read_text(encoding="utf-8")
        assert BANNER_MARKER in text, f"missing banner in {path}"
        assert text.count(BANNER_MARKER) == 1, f"duplicate banner in {path}"
        assert f"{parity_rel}/omg-parity.json" in text, f"missing inventory link in {path}"
        assert f"{parity_rel}/FEATURE-MATRIX.md" in text, f"missing matrix link in {path}"
        assert "predates the v2 parity inventory" in text
