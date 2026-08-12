"""Drift guards for dual-host agent model routing architecture (#133).

English page is canonical. Maintained indexes/locales must link to it rather
than forking a second support matrix. The eight-row Normative support matrix
is bound to tests/fixtures/docs/normative_support_matrix_v1.json until the
#131 capability registry replaces that docs contract. Tests bind documented
CLI shapes and Presentation route.kind strings to shipped parser/constants;
they do not implement routing runtime.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

from omg_cli.main import build_parser
from omg_cli.team.plane import STATUS_TOP_KEYS
from omg_cli.team.presentation import (
    ROUTE_KIND_EXTERNAL,
    ROUTE_KIND_NATIVE_RECEIPT,
    ROUTE_KIND_UNKNOWN,
    ROUTE_SCHEMA,
    build_external_route,
    unknown_route,
)

ROOT = Path(__file__).resolve().parents[1]
ARCH = ROOT / "docs" / "architecture" / "agent-model-routing.md"
ARCH_ZH = ROOT / "docs" / "architecture" / "agent-model-routing.zh.md"
ARCH_ZH_TW = ROOT / "docs" / "architecture" / "agent-model-routing.zh-TW.md"
PLAN = ROOT / "docs" / "plans" / "2026-08-09-dual-host-agent-model-routing.md"
CHECK_DOCS = ROOT / "scripts" / "check_docs_links.py"
SUPPORT_MATRIX_FIXTURE = (
    ROOT / "tests" / "fixtures" / "docs" / "normative_support_matrix_v1.json"
)
_MATRIX_TABLE_HEADER = "| Capability | Original Grok Build | Medley |"
_MATRIX_FIRST_CAPABILITY = "OMG agents, skills, workflows, acceptance"

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

INDEX_FILES: tuple[str, ...] = (
    "README.md",
    "docs/README.md",
    "docs/README.zh.md",
    "docs/README.zh-TW.md",
    "docs/readme/README.md",
    "docs/readme/README.zh.md",
    "docs/readme/README.zh-TW.md",
)

LOCALE_FORK_FILES: tuple[str, ...] = (
    "docs/README.zh.md",
    "docs/README.zh-TW.md",
    "docs/architecture/agent-model-routing.zh.md",
    "docs/architecture/agent-model-routing.zh-TW.md",
    "docs/readme/README.zh.md",
    "docs/readme/README.zh-TW.md",
)

SECRET_SCAN_FILES: tuple[Path, ...] = (
    ARCH,
    PLAN,
    ROOT / "README.md",
    ROOT / "docs" / "readme" / "README.md",
    ROOT / "docs" / "readme" / "README.zh.md",
    ROOT / "docs" / "readme" / "README.zh-TW.md",
    ARCH_ZH,
    ARCH_ZH_TW,
)

SHIPPED_OMG_COMMANDS: tuple[str, ...] = (
    "omg doctor",
    "omg doctor --strict",
    "omg doctor --json",
    "omg --json doctor",
    "omg team status",
    "omg team status --json",
    "omg team status --presentation",
    "omg ask fake",
    "omg ask fake --background",
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
    "native_host_receipt",
    "Initial candidate selection",
    "Retry within one route",
    "Fallback to another native route",
    "External worker replacement",
    "429",
    "oh-my-grok#131",
    "oh-my-grok#133",
    "oh-my-grok#134",
    "oh-my-grok#138",
    "runtime_kind",
    "native_host",
    "external_cli",
    "purpose",
    "advisory",
    "task_execution",
    "lifecycle",
    "foreground",
    "background_job",
    "team_member",
    "ImL1s/medley#287",
    "ImL1s/medley#289",
    "ImL1s/medley#207",
    "ImL1s/medley#290",
    "narrow-width",
    "no-color",
    "omg doctor",
    "route kind",
    "test_stock_host_medley_absent",
    "explicit import blocker",
    "ROUTE_SCHEMA",
    "dual-carried",
    "never infer",
    "versioned migration",
)

# Affirmative "Medley required" phrases — only allowed inside explicit negations.
_MEDLEY_REQUIRED_PHRASE = re.compile(
    r"Medley is required for baseline"
    r"|must install Medley"
    r"|Medley is a hard dependency"
    r"|requires Medley to run OMG"
    r"|Medley 是硬依赖"
    r"|Medley 是硬依賴"
    r"|必须安装 Medley"
    r"|必須安裝 Medley"
    r"|hard dependency",
    re.IGNORECASE,
)
_NEGATION_WINDOW = re.compile(
    r"(?i)(no statement that|must not|must \*\*not\*\*|never|not required"
    r"|\*\*no\*\*|do not claim|does \*\*not\*\*|is \*\*not\*\*"
    r"|不是|并非|並非|不要求)"
)

_NATIVE_RECEIPT_NEGATION = re.compile(
    r"not equal|not shipped|not\b|不等於|不等于|≠",
    re.IGNORECASE,
)
_INVENTED_KIND_NEGATION = re.compile(
    r"not invent|do not invent|must not|never|not shipped|not\b|不要发明|不要發明",
    re.IGNORECASE,
)

# Secret / account shaped tokens. Matches inside github.com/ URLs are ignored.
_SECRETISH = re.compile(
    r"(?i)("
    r"api[_-]?key\s*[:=]\s*['\"][^'\"]+['\"]"
    r"|sk-[A-Za-z0-9]{10,}"
    r"|Bearer\s+[A-Za-z0-9\-._~+/]+=*"
    r"|Authorization\s*:\s*\S+"
    r"|xox[baprs]-[A-Za-z0-9-]+"
    r"|-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"
    r"|[?&](?:token|access_token|auth)=[^\s&\"']+"
    r"|account_id\s*=\s*\S+"
    r"|acct_[A-Za-z0-9]+"
    r"|http://(?:10\.|192\.168\.)\S*(?::\d+|/\S*)"
    r"|http://127\.0\.0\.1(?::\d+|/\S*)"
    r")"
)
_GITHUB_URL_SPAN = re.compile(
    r"https?://(?:www\.)?github\.com/[^\s)\]>'\"`]+",
    re.IGNORECASE,
)

_BACKTICK_OMG = re.compile(r"`(omg\s+[^`]+)`")
_OPTIONAL_FLAG = re.compile(r"\[(--[A-Za-z0-9-]+)\]")
_AGENTS_MENTION = re.compile(r"omg agents")
_429_NEGATION = re.compile(
    r"alone|not authorize|must not|never|no documentation may imply|不得",
    re.IGNORECASE,
)
_MD_LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_A_ID = re.compile(r"<a\s+[^>]*\bid=['\"]([^'\"]+)['\"]", re.IGNORECASE)
_PUNCT_FOR_SLUG = re.compile(r"[^\w\s\-]", re.UNICODE)

_AGENTS_CONTRACT_WINDOW = 320
_KIND_WINDOW = 160
_429_WINDOW = 120


def _load_docs_checker():
    spec = importlib.util.spec_from_file_location("check_docs_links", CHECK_DOCS)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _top_level_choices(parser: argparse.ArgumentParser) -> set[str]:
    found: set[str] = set()
    for act in parser._actions:
        if isinstance(act, argparse._SubParsersAction):
            found.update(act.choices.keys())
    return found


def _nested_choices(parser: argparse.ArgumentParser, dest_cmd: str) -> set[str]:
    for act in parser._actions:
        if isinstance(act, argparse._SubParsersAction) and dest_cmd in act.choices:
            for a2 in act.choices[dest_cmd]._actions:
                if isinstance(a2, argparse._SubParsersAction):
                    return set(a2.choices.keys())
    return set()


def _try_parse_argv(argv: list[str]) -> bool:
    """Parse *argv* (no leading ``omg``) without calling ``sys.exit``."""
    parser = build_parser()
    try:
        parser.parse_args(argv)
    except SystemExit:
        return False
    return True


def _expand_optional_groups(command: str) -> list[str]:
    """Expand ``[--json]`` / ``[--strict]`` (any ``[--flag]``) into variants."""
    seed = command.replace("<agent-or-profile>", "omg-verifier-example").replace(
        "<provider>", "fake"
    )
    variants = [seed]
    expanded: list[str] = []

    def rec(cmd: str) -> None:
        m = _OPTIONAL_FLAG.search(cmd)
        if m is None:
            cleaned = re.sub(r"\s+", " ", cmd).strip()
            if cleaned:
                expanded.append(cleaned)
            return
        rec(cmd[: m.start()] + m.group(1) + cmd[m.end() :])
        rec(cmd[: m.start()] + cmd[m.end() :])

    for item in variants:
        rec(item)
    # Preserve order, drop dupes.
    return list(dict.fromkeys(expanded))


def _documented_omg_commands(body: str) -> list[str]:
    found: list[str] = []
    for m in _BACKTICK_OMG.finditer(body):
        raw = m.group(1).strip()
        found.extend(_expand_optional_groups(raw))
    return list(dict.fromkeys(found))


def _argv_for(command: str) -> list[str]:
    parts = command.split()
    if not parts or parts[0] != "omg":
        raise AssertionError(f"not an omg command: {command!r}")
    return parts[1:]


def _first_verb(argv: list[str]) -> str | None:
    for tok in argv:
        if not tok.startswith("-"):
            return tok
    return None


def _window(text: str, start: int, end: int, radius: int) -> str:
    return text[max(0, start - radius) : min(len(text), end + radius)]


def _inside_github_url(text: str, start: int, end: int) -> bool:
    for m in _GITHUB_URL_SPAN.finditer(text):
        if m.start() <= start and end <= m.end():
            return True
    return False


def _secretish_hits(text: str) -> list[str]:
    hits: list[str] = []
    for m in _SECRETISH.finditer(text):
        if _inside_github_url(text, m.start(), m.end()):
            continue
        hits.append(m.group(0))
    return hits


def _heading_slug(heading: str) -> str:
    """GitHub-style slug: each remaining space becomes a hyphen (keep ``--``)."""
    s = heading.strip().lower().replace("`", "")
    s = _PUNCT_FOR_SLUG.sub("", s)
    return s.replace(" ", "-").strip("-")


def _fragments_for(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    found = {m.group(1) for m in _A_ID.finditer(text)}
    found.update(_heading_slug(m.group(1)) for m in _HEADING.finditer(text))
    return found


def _clean_md_dest(raw: str) -> str:
    dest = raw.strip()
    if dest.startswith("<") and ">" in dest:
        dest = dest[1 : dest.index(">")].strip()
    if dest and dest[0] not in {'"', "'"}:
        dest = dest.split()[0]
    return dest


def _md_hrefs(text: str) -> list[str]:
    return [_clean_md_dest(m.group(2)) for m in _MD_LINK.finditer(text)]


def _is_remote(dest: str) -> bool:
    return dest.lower().startswith(("http://", "https://", "mailto:"))


def _strip_md_cell(text: str) -> str:
    """Strip whitespace and surrounding ``**`` emphasis."""
    cell = text.strip()
    if len(cell) >= 4 and cell.startswith("**") and cell.endswith("**"):
        cell = cell[2:-2].strip()
    return cell


def _parse_md_table_rows(section: str) -> list[list[str]]:
    """Split markdown pipe rows; skip ``-`` / ``:`` / space separator rows."""
    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        parts = stripped.split("|")
        if parts and parts[0].strip() == "":
            parts = parts[1:]
        if parts and parts[-1].strip() == "":
            parts = parts[:-1]
        if not parts:
            continue
        if all(part.strip() and set(part.strip()) <= set("-: ") for part in parts):
            continue
        rows.append([_strip_md_cell(part) for part in parts])
    return rows


def _normative_matrix_section(body: str) -> str:
    """Slice ``## Normative support matrix`` up to the next ``## `` heading."""
    lines = body.splitlines(keepends=True)
    start: int | None = None
    end: int | None = None
    for i, line in enumerate(lines):
        if line.startswith("## Normative support matrix"):
            start = i
            continue
        if start is not None and line.startswith("## "):
            end = i
            break
    assert start is not None, "missing ## Normative support matrix"
    return "".join(lines[start:end])


def _load_support_matrix_fixture() -> dict:
    data = json.loads(SUPPORT_MATRIX_FIXTURE.read_text(encoding="utf-8"))
    assert data.get("schema") == "omg.docs.support_matrix/v1", data.get("schema")
    header = data.get("header")
    rows = data.get("rows")
    assert isinstance(header, list) and len(header) == 3, header
    assert isinstance(rows, list) and len(rows) == 8, rows
    return data


def _assert_local_dest_resolves(src: Path, dest: str, *, check_fragment: bool) -> None:
    path_part, frag = (dest.split("#", 1) + [""])[:2]
    target = src if not path_part else (src.parent / path_part)
    assert target.is_file(), f"{src.relative_to(ROOT)}: missing local target {dest!r}"
    if not check_fragment or not frag:
        return
    frags = _fragments_for(target)
    assert frag in frags, (
        f"{src.relative_to(ROOT)}: fragment {frag!r} not in {target.relative_to(ROOT)}"
    )


def test_canonical_architecture_page_exists() -> None:
    assert ARCH.is_file(), f"missing {ARCH.relative_to(ROOT)}"


def test_architecture_page_has_required_contract_snippets() -> None:
    body = ARCH.read_text(encoding="utf-8")
    missing = [s for s in ARCH_REQUIRED_SNIPPETS if s not in body]
    assert not missing, f"architecture page missing: {missing}"


def test_stock_host_medley_absent_smoke_is_documented_and_present() -> None:
    body = ARCH.read_text(encoding="utf-8")
    assert "test_stock_host_medley_absent.py" in body
    assert (ROOT / "tests" / "test_stock_host_medley_absent.py").is_file()
    for path in (ARCH_ZH, ARCH_ZH_TW):
        text = path.read_text(encoding="utf-8")
        assert "test_stock_host_medley_absent.py" in text, path.name


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


def test_architecture_states_ux_ownership_and_accessibility() -> None:
    body = ARCH.read_text(encoding="utf-8")
    assert (
        "is **not** UI" in body
        or "not** UI" in body
        or "not UI / TUI" in body
    ), "architecture must negate routing/backend completion as UI/TUI completion"
    assert "declarative Agents" in body
    renderer_hits = list(re.finditer(r"renderer", body))
    assert renderer_hits, "architecture must mention renderer"
    assert any(
        re.search(
            r"not|does \*\*not\*\* own",
            _window(body, m.start(), m.end(), 80),
            re.IGNORECASE,
        )
        for m in renderer_hits
    ), "architecture must say OMG does not own a stock-host renderer"
    assert "ImL1s/medley#207" in body
    assert "ImL1s/medley#290" in body
    assert "oh-my-grok#134" in body
    assert "narrow-width" in body
    assert "no-color" in body
    assert "contract target" in body
    assert "unsupported" in body
    assert "unavailable" in body


def test_architecture_distinguishes_advisory_from_task_execution() -> None:
    body = ARCH.read_text(encoding="utf-8")
    for token in (
        "runtime_kind",
        "purpose",
        "lifecycle",
        "native_host",
        "external_cli",
        "advisory",
        "task_execution",
        "foreground",
        "background_job",
        "team_member",
    ):
        assert token in body, f"architecture missing dimension token {token!r}"
    assert (
        "not** an external Team executor" in body
        or "not an external Team executor" in body
        or "is **not** an external Team" in body
    ), "architecture must negate advisory/omg ask as a Team executor"
    assert "never set `verified`" in body, (
        "architecture must say ask artifacts never set verified"
    )
    assert (
        "does **not** claim a shipped council runtime" in body
        or "not** claim a shipped council" in body
    ), "architecture must not claim a shipped council runtime"
    assert "Medley API" in body
    assert "#138" in body
    assert "oh-my-grok#138" in body


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
    matches = list(re.finditer(r"429", body))
    assert matches, "architecture page must mention 429"
    for m in matches:
        window = _window(body, m.start(), m.end(), _429_WINDOW)
        assert _429_NEGATION.search(window), (
            f"429 without local negation window: {window!r}"
        )


def test_architecture_has_no_secretish_tokens() -> None:
    body = ARCH.read_text(encoding="utf-8")
    hits = _secretish_hits(body)
    assert not hits, f"secret-like token in architecture docs: {hits}"


def test_routing_related_docs_have_no_secretish_tokens() -> None:
    for path in SECRET_SCAN_FILES:
        hits = _secretish_hits(path.read_text(encoding="utf-8"))
        assert not hits, f"secret-like token in {path.relative_to(ROOT)}: {hits}"


def test_plan_points_at_canonical_architecture() -> None:
    plan = PLAN.read_text(encoding="utf-8")
    assert "architecture/agent-model-routing.md" in plan


def test_maintained_indexes_link_to_canonical_page() -> None:
    for rel, needle in INDEX_LINKS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert needle in text, f"{rel} missing link marker {needle!r}"


def test_locale_indexes_do_not_fork_support_matrix() -> None:
    """zh / zh-TW indexes and locale architecture must not fork the matrix."""
    for rel in LOCALE_FORK_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "agent-model-routing.md" in text, f"{rel} missing canonical pointer"
        assert "Normative support matrix" not in text
        assert _MATRIX_TABLE_HEADER not in text
        assert _MATRIX_FIRST_CAPABILITY not in text
        assert "host.native-exact-model.v1" not in text


def test_normative_support_matrix_matches_managed_fixture() -> None:
    fixture = _load_support_matrix_fixture()
    assert len(fixture["rows"]) == 8
    body = ARCH.read_text(encoding="utf-8")
    parsed = _parse_md_table_rows(_normative_matrix_section(body))
    assert parsed, "normative matrix section has no table rows"
    header, *rows = parsed
    assert header == [_strip_md_cell(cell) for cell in fixture["header"]]
    assert header == fixture["header"]
    assert len(rows) == 8
    assert rows == fixture["rows"]
    for row in fixture["rows"]:
        assert len(row) == 3


def test_normative_support_matrix_rejects_removal_reorder_or_weaken() -> None:
    """Comparison stays exact: removal, reorder, or weaken must not equal."""
    fixture = _load_support_matrix_fixture()
    expected = fixture["rows"]
    assert len(expected) == 8
    parsed_rows = _parse_md_table_rows(
        _normative_matrix_section(ARCH.read_text(encoding="utf-8"))
    )[1:]
    assert parsed_rows == fixture["rows"]

    for i in range(len(expected)):
        removed = [list(row) for row in expected]
        del removed[i]
        assert removed != fixture["rows"]

    swapped = [list(row) for row in expected]
    swapped[0], swapped[1] = swapped[1], swapped[0]
    assert swapped != fixture["rows"]

    weaken_required = [list(row) for row in expected]
    assert weaken_required[0][1] == "Required"
    weaken_required[0][1] = "Optional"
    assert weaken_required != fixture["rows"]

    weaken_assumed = [list(row) for row in expected]
    assert weaken_assumed[3][1] == "Not assumed"
    weaken_assumed[3][1] = "Assumed"
    assert weaken_assumed != fixture["rows"]

    weaken_owned = [list(row) for row in expected]
    assert weaken_owned[7][1] == "OMG-owned"
    weaken_owned[7][1] = "Host-owned"
    assert weaken_owned != fixture["rows"]

    # Row 2 has three distinct cells; swapping any pair changes equality.
    cell_reordered = [list(row) for row in expected]
    cell_reordered[2] = [
        cell_reordered[2][1],
        cell_reordered[2][2],
        cell_reordered[2][0],
    ]
    assert cell_reordered != fixture["rows"]


def test_locale_architecture_projection_honesty() -> None:
    for path in (ARCH_ZH, ARCH_ZH_TW):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT).as_posix()
        for needle in (
            "first-class baseline",
            "hard dependency",
            "unsupported",
            "unavailable",
            "agent-model-routing.md",
        ):
            assert needle in text, f"{rel} missing {needle!r}"
        assert "#131" in text, f"{rel} missing #131"
        assert "#138" in text, f"{rel} missing #138"
        assert "#207" in text, f"{rel} missing #207"
        assert "#290" in text, f"{rel} missing #290"
        assert "narrow-width" in text, f"{rel} missing narrow-width"
        assert "no-color" in text, f"{rel} missing no-color"
        assert (
            "UI" in text or "TUI" in text
        ), f"{rel} missing UI/TUI not-complete claim"
        assert "runtime_kind" in text, f"{rel} missing runtime_kind"
        assert "advisory" in text, f"{rel} missing advisory"
        assert "omg ask" in text, f"{rel} missing omg ask"
        assert (
            "尚未出貨" in text or "尚未出货" in text or "planned" in text
        ), f"{rel} missing planned-extension honesty"
        assert "contract-only" in text, f"{rel} missing contract-only"
        assert (
            "不可跑" in text or "不可运行" in text or "not runnable" in text
        ), f"{rel} missing not-runnable honesty"
        assert "host.native-exact-model.v1" not in text
        assert "Normative support matrix" not in text
        assert _MATRIX_TABLE_HEADER not in text
        assert _MATRIX_FIRST_CAPABILITY not in text
        for banned in ("可解鎖增強", "可解锁增强", "can unlock enhanced"):
            assert banned not in text, f"{rel} ships routing phrase {banned!r}"
        for m in _MEDLEY_REQUIRED_PHRASE.finditer(text):
            window = text[max(0, m.start() - 100) : m.end() + 20]
            assert _NEGATION_WINDOW.search(window), (
                f"{rel}: Medley-required claim without negation: {window!r}"
            )


def test_shipped_cli_names_in_architecture_are_registered() -> None:
    """Only assert CLI verbs already registered; agents* remain contract-only."""
    parser = build_parser()
    top: set[str] = set()
    for act in parser._actions:
        if getattr(act, "choices", None):
            top.update(act.choices.keys())
    body = ARCH.read_text(encoding="utf-8")
    for cmd in ("doctor", "team", "ask"):
        assert cmd in top
        assert f"omg {cmd}" in body
    # Contract surfaces may be mentioned but must not be claimed as shipped-only.
    if "omg agents" in body:
        assert "Contract" in body or "contract" in body


def test_documented_omg_command_shapes_match_parser() -> None:
    """Backtick ``omg …`` shapes must parse if shipped, and agents must not."""
    body = ARCH.read_text(encoding="utf-8")
    parser = build_parser()
    top = _top_level_choices(parser)
    assert "doctor" in top
    assert "team" in top
    assert "agents" not in top
    assert "status" in _nested_choices(parser, "team")

    for shipped in SHIPPED_OMG_COMMANDS:
        assert _try_parse_argv(_argv_for(shipped)), f"shipped command failed parse: {shipped}"

    for command in _documented_omg_commands(body):
        argv = _argv_for(command)
        verb = _first_verb(argv)
        if verb == "ask":
            try:
                ask_at = argv.index("ask")
            except ValueError:
                ask_at = -1
            if ask_at >= 0 and not any(
                not tok.startswith("-") for tok in argv[ask_at + 1 :]
            ):
                argv.insert(ask_at + 1, "fake")
        if verb == "agents":
            assert not _try_parse_argv(argv), f"contract-only command parsed: {command}"
            continue
        assert _try_parse_argv(argv), f"documented shipped shape failed parse: {command}"


def test_omg_agents_mentions_are_contract_only() -> None:
    body = ARCH.read_text(encoding="utf-8")
    mentions = list(_AGENTS_MENTION.finditer(body))
    assert mentions, "architecture must mention omg agents as contract-only"
    honesty = re.compile(r"not runnable|not registered", re.IGNORECASE)
    for m in mentions:
        window = _window(body, m.start(), m.end(), _AGENTS_CONTRACT_WINDOW)
        assert re.search(r"contract", window, re.IGNORECASE), (
            f"omg agents without nearby 'contract': {window!r}"
        )
        assert honesty.search(window), (
            f"omg agents without nearby not-runnable/not-registered: {window!r}"
        )


def test_route_kind_constants_match_architecture() -> None:
    assert ROUTE_KIND_EXTERNAL == "external_executor"
    assert ROUTE_KIND_NATIVE_RECEIPT == "native_host_receipt"
    assert ROUTE_KIND_UNKNOWN == "unknown"
    body = ARCH.read_text(encoding="utf-8")
    for kind in (
        ROUTE_KIND_EXTERNAL,
        ROUTE_KIND_NATIVE_RECEIPT,
        ROUTE_KIND_UNKNOWN,
    ):
        assert kind in body, f"architecture missing shipped route.kind {kind!r}"
    hits = list(re.finditer(r"native_host_receipt", body))
    assert hits, "architecture must mention native_host_receipt"
    assert any(
        _NATIVE_RECEIPT_NEGATION.search(_window(body, m.start(), m.end(), _KIND_WINDOW))
        for m in hits
    ), "architecture must negate policy native == native_host_receipt"


def test_architecture_binds_legacy_provider_migration_to_presentation_exports() -> None:
    """Shipped Presentation route schema/kind/unknown/dual-carry — not #131."""
    assert ROUTE_SCHEMA == 1
    assert ROUTE_KIND_UNKNOWN == "unknown"
    unknown = unknown_route()
    assert unknown["schema"] == ROUTE_SCHEMA
    assert unknown["kind"] == ROUTE_KIND_UNKNOWN
    assert unknown["executor"] is None
    assert unknown["provider"] is None
    dual = build_external_route(
        executor="fixture",
        provider="fake",
        role="executor",
        posture="read-write",
    )
    assert dual["schema"] == ROUTE_SCHEMA
    assert dual["kind"] == ROUTE_KIND_EXTERNAL
    assert dual["executor"] == "fixture"
    assert dual["provider"] == "fake"
    body = ARCH.read_text(encoding="utf-8")
    for snippet in ("ROUTE_SCHEMA", "dual-carried", "never infer"):
        assert snippet in body, f"architecture missing {snippet!r}"
    assert "unknown_route" in body or "`unknown`" in body
    assert "versioned migration" in body
    assert "implementation #131/Team" not in body


def test_architecture_mentions_external_cli_executor_only_as_unshipped() -> None:
    body = ARCH.read_text(encoding="utf-8")
    hits = list(re.finditer(r"external_cli_executor", body))
    assert hits, "architecture must mention external_cli_executor as not invented"
    for m in hits:
        window = _window(body, m.start(), m.end(), _KIND_WINDOW)
        assert _INVENTED_KIND_NEGATION.search(window), (
            f"external_cli_executor without negation: {window!r}"
        )


def test_presentation_has_no_unshipped_route_kind_exports() -> None:
    import omg_cli.team.presentation as pres

    assert not hasattr(pres, "execution_kind")
    assert not hasattr(pres, "ROUTE_KIND_NATIVE")
    assert not hasattr(pres, "external_cli_executor")
    exported = set(getattr(pres, "__all__", ()))
    assert "execution_kind" not in exported
    assert "ROUTE_KIND_NATIVE" not in exported
    assert "external_cli_executor" not in exported
    assert getattr(pres, "ROUTE_KIND_NATIVE_RECEIPT", None) != "native"


def test_status_json_top_keys_have_no_route() -> None:
    assert STATUS_TOP_KEYS == (
        "run_id",
        "session",
        "dry_run",
        "workspace_mode",
        "tasks",
    )
    body = ARCH.read_text(encoding="utf-8")
    assert "omg team status --json" in body
    assert re.search(r"no `route`|has no route field|没有.*route|沒有.*route", body)


def test_routing_docs_local_markdown_links_resolve() -> None:
    checker = _load_docs_checker()
    for rel in checker.ROUTING_DOCS:
        src = ROOT / rel
        assert src.is_file(), f"missing {rel}"
        for dest in _md_hrefs(src.read_text(encoding="utf-8")):
            if not dest or _is_remote(dest):
                continue
            _assert_local_dest_resolves(src, dest, check_fragment=True)


def test_routing_docs_have_required_external_issue_urls() -> None:
    checker = _load_docs_checker()
    required = set(checker.REQUIRED_EXTERNAL)
    for rel in (
        "docs/architecture/agent-model-routing.md",
        "docs/architecture/agent-model-routing.zh.md",
        "docs/architecture/agent-model-routing.zh-TW.md",
    ):
        https = checker.collect_https((ROOT / rel).read_text(encoding="utf-8"))
        missing = sorted(required - https)
        assert not missing, f"{rel} missing exact external URL {missing}"


def test_index_agent_model_routing_links_resolve() -> None:
    for rel in INDEX_FILES:
        src = ROOT / rel
        text = src.read_text(encoding="utf-8")
        for dest in _md_hrefs(text):
            if "agent-model-routing" not in dest:
                continue
            if _is_remote(dest):
                continue
            path_part = dest.split("#", 1)[0]
            target = src if not path_part else (src.parent / path_part)
            assert target.is_file(), (
                f"{rel}: agent-model-routing dest missing {dest!r}"
            )


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
    assert "ROUTING_DOCS" in src
    assert "REQUIRED_EXTERNAL" in src
    assert "https://github.com/ImL1s/oh-my-grok/issues/131" in src
    assert "https://github.com/ImL1s/oh-my-grok/issues/138" in src
    assert "https://github.com/ImL1s/medley/issues/287" in src
    assert "https://github.com/ImL1s/medley/issues/207" in src
    assert "https://github.com/ImL1s/medley/issues/290" in src
