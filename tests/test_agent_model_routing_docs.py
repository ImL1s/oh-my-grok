"""Drift guards for dual-host agent model routing architecture (#133).

English page is canonical. Maintained indexes/locales must link to it rather
than forking a second support matrix. No runtime claims in this module.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCH = ROOT / "docs" / "architecture" / "agent-model-routing.md"
PLAN = ROOT / "docs" / "plans" / "2026-08-09-dual-host-agent-model-routing.md"
CHECK_DOCS = ROOT / "scripts" / "check_docs_links.py"

# Entry points that must surface the canonical page (relative path as linked).
INDEX_LINKS: tuple[tuple[str, str], ...] = (
    ("README.md", "docs/architecture/agent-model-routing.md"),
    ("docs/README.md", "architecture/agent-model-routing.md"),
    ("docs/README.zh.md", "architecture/agent-model-routing.md"),
    ("docs/README.zh-TW.md", "architecture/agent-model-routing.md"),
    ("docs/readme/README.md", "architecture/agent-model-routing.md"),
    ("docs/readme/README.zh.md", "architecture/agent-model-routing.md"),
    ("docs/readme/README.zh-TW.md", "architecture/agent-model-routing.md"),
)

# Normative fragments that must appear on the English architecture page.
ARCH_REQUIRED_SNIPPETS: tuple[str, ...] = (
    "first-class baseline",
    "Optional enhanced host",
    "hard dependency",
    "baseline",
    "optional extension",
    "unsupported",
    "unavailable",
    "incompatible",
    "unknown",
    "external_executor",
    "native",
    "Initial candidate selection",
    "Retry within one route",
    "Fallback to another native route",
    "External worker replacement",
    "429",
    "oh-my-grok#131",
    "oh-my-grok#133",
    "oh-my-grok#134",
    "ImL1s/medley#287",
    "ImL1s/medley#289",
    "omg doctor",
    "route kind",
)

# Affirmative "Medley required" phrases — only allowed inside explicit negations.
_MEDLEY_REQUIRED_PHRASE = re.compile(
    r"Medley is required for baseline"
    r"|must install Medley"
    r"|Medley is a hard dependency"
    r"|requires Medley to run OMG",
    re.IGNORECASE,
)
_NEGATION_WINDOW = re.compile(
    r"(?i)(no statement that|must not|must \*\*not\*\*|never|not required"
    r"|\*\*no\*\*|do not claim|does \*\*not\*\*|is \*\*not\*\*)"
)

# Secret / account shaped tokens that must not appear in architecture examples.
_SECRETISH = re.compile(
    r"(?i)(api[_-]?key\s*[:=]\s*['\"][^'\"]+['\"]"
    r"|sk-[A-Za-z0-9]{10,}"
    r"|Bearer\s+[A-Za-z0-9\-._~+/]+=*"
    r"|xox[baprs]-[A-Za-z0-9-]+"
    r"|-----BEGIN (?:RSA |EC )?PRIVATE KEY-----)"
)


def test_canonical_architecture_page_exists() -> None:
    assert ARCH.is_file(), f"missing {ARCH.relative_to(ROOT)}"


def test_architecture_page_has_required_contract_snippets() -> None:
    body = ARCH.read_text(encoding="utf-8")
    missing = [s for s in ARCH_REQUIRED_SNIPPETS if s not in body]
    assert not missing, f"architecture page missing: {missing}"


def test_architecture_does_not_claim_medley_required() -> None:
    body = ARCH.read_text(encoding="utf-8")
    for m in _MEDLEY_REQUIRED_PHRASE.finditer(body):
        window = body[max(0, m.start() - 100) : m.end() + 20]
        assert _NEGATION_WINDOW.search(window), (
            f"affirmative Medley-required claim without negation: {window!r}"
        )
    # Positive baseline honesty
    assert "Medley **absent**" in body or "not** required" in body
    assert "hard dependency" in body
    assert "never" in body.lower()


def test_architecture_distinguishes_native_and_external_routes() -> None:
    body = ARCH.read_text(encoding="utf-8")
    assert "kind: native" in body or "`native`" in body or "kind: native" in body.replace("`", "")
    assert "external_executor" in body
    assert "Medley API" in body or "Medley API **provider**" in body


def test_architecture_separates_selection_retry_fallback_replacement() -> None:
    body = ARCH.read_text(encoding="utf-8")
    for phrase in (
        "Initial candidate selection",
        "Retry within one route",
        "Fallback to another native route",
        "External worker replacement",
    ):
        assert phrase in body


def test_architecture_forbids_generic_429_failover() -> None:
    body = ARCH.read_text(encoding="utf-8")
    assert "429" in body
    low = body.lower()
    assert "alone" in low or "not authorize" in low or "prohibited" in low


def test_architecture_has_no_secretish_tokens() -> None:
    body = ARCH.read_text(encoding="utf-8")
    assert _SECRETISH.search(body) is None, "secret-like token in architecture docs"


def test_plan_points_at_canonical_architecture() -> None:
    plan = PLAN.read_text(encoding="utf-8")
    assert "architecture/agent-model-routing.md" in plan


def test_maintained_indexes_link_to_canonical_page() -> None:
    for rel, needle in INDEX_LINKS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert needle in text, f"{rel} missing link marker {needle!r}"


def test_locale_indexes_do_not_fork_support_matrix() -> None:
    """zh / zh-TW indexes must link, not re-author the normative matrix heading."""
    for rel in ("docs/README.zh.md", "docs/README.zh-TW.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "architecture/agent-model-routing.md" in text
        assert "Normative support matrix" not in text
        assert "host.native-exact-model.v1" not in text


def test_shipped_cli_names_in_architecture_are_registered() -> None:
    """Only assert CLI verbs already registered; agents* remain contract-only."""
    from omg_cli.main import build_parser

    parser = build_parser()
    top: set[str] = set()
    for act in parser._actions:
        if getattr(act, "choices", None):
            top.update(act.choices.keys())
    body = ARCH.read_text(encoding="utf-8")
    for cmd in ("doctor", "team"):
        assert cmd in top
        assert f"omg {cmd}" in body
    # Contract surfaces may be mentioned but must not be claimed as shipped-only.
    if "omg agents" in body:
        assert "Contract" in body or "contract" in body


def test_check_docs_links_includes_architecture() -> None:
    proc = subprocess.run(
        [sys.executable, str(CHECK_DOCS)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "docs_ok" in proc.stdout


def test_check_docs_links_source_lists_architecture() -> None:
    src = CHECK_DOCS.read_text(encoding="utf-8")
    assert "docs/architecture/agent-model-routing.md" in src
    assert '"docs/README.md", "architecture/agent-model-routing.md"' in src or (
        "architecture/agent-model-routing.md" in src and "docs/README.md" in src
    )
