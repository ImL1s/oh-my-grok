# Plan 011: Isolate machine-local research validator from product pytest

> **Drift check**: `git diff --stat 997bcce..HEAD -- tests/report_validator PROJECT.md TEST_READY.md progress.md pytest.ini .gitignore`

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

Untracked (or future-committed) `tests/report_validator/test_validate.py` hardcodes  
`~/teamwork_projects/omc_omx_research/omc_omx_mechanism_research.md` and **fails if missing**. Default `testpaths = tests` collects it. On author machine with the file present, suite stays green (439+); on CI/contributors it fails or silently differs. Product CLI suite must not depend on private absolute paths.

## Current state

- `tests/report_validator/test_validate.py` — absolute path + pytest.fail if missing
- `tests/report_validator/test_mock_report.py` — hermetic mock (keep)
- `PROJECT.md`, `TEST_READY.md`, `progress.md` — side research scaffolding (may be untracked)
- CONTRIBUTING forbids committing absolute home paths

## Commands

| Purpose | Command | Expected |
|---------|---------|----------|
| Product suite | `python -m pytest -q -m "not live" --tb=short` | exit 0 without requiring private report path |
| Mock validator | `python -m pytest -q tests/report_validator/test_mock_report.py` | exit 0 |

## Scope

**In scope**:
- `tests/report_validator/*`
- `pytest.ini` if needed
- Optional delete/move of untracked `PROJECT.md` / `TEST_READY.md` / `progress.md` only if operator wants (prefer not delete without confirmation — default: leave untracked files alone; fix test collection)

**Out of scope**:
- Content of the private research report
- Relocating teamwork_projects files

## Steps

### Preferred fix (pick one)

**A**: Mark `test_report_validation` with `@pytest.mark.live` or `@pytest.mark.skipif(not path.exists())` **and** exclude from default via marker — but `not live` already used in CI; marking `live` is enough **if** the test is committed.

**B**: Move absolute-path test out of `tests/` to `scripts/` or `research/` so pytest does not collect it; keep `test_mock_report.py` hermetic under tests.

**C**: Delete absolute-path test; keep mock only.

Recommend **B or C**. Do not keep hard-fail absolute path in default collection.

### Also

- Ensure no absolute `~/...` remains under tracked `tests/`.
- If files are still untracked, either add to `.gitignore` (`PROJECT.md`, research harness) or document they must never be committed.

## Done criteria

- [ ] `pytest -m "not live"` does not require private report file
- [ ] Hermetic mock remains if useful
- [ ] No new absolute home paths in tracked tests

## STOP conditions

- Operator requires the absolute-path test in CI — rewrite to env var `OMG_RESEARCH_REPORT_PATH` with skip-if-unset, never hardcode home.

## Maintenance notes

- Research E2E is a different product track; keep it out of `omg_cli` gate.
