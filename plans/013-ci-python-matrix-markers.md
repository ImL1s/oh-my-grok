# Plan 013: Expand CI Python matrix and apply pytest markers honestly

> **Drift check**: `git diff --stat 997bcce..HEAD -- .github/workflows/ci.yml pytest.ini tests/`

## Status

- **Priority**: P3
- **Effort**: S–M
- **Risk**: LOW
- **Depends on**: plan 011 (avoid collecting bad tests)
- **Category**: dx
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

README claims Python 3.11+. CI only runs 3.11/3.12 while maintainers may use 3.14. Markers `unit`/`integration`/`live`/`slow` are declared but unused, so `pytest -m "not live"` is hollow discipline.

## Current state

- `.github/workflows/ci.yml` matrix: `["3.11", "3.12"]`
- `pytest.ini` markers defined; almost no `@pytest.mark.*` on tests
- CI: `pytest -q -m "not live"` + smoke STRICT=0

## Commands

| Purpose | Command | Expected |
|---------|---------|----------|
| Local | `python -m pytest -q -m "not live"` | exit 0 |
| Marker check | `python -m pytest --collect-only -q -m live` | only intentionally live tests |

## Scope

**In scope**: `.github/workflows/ci.yml`, `pytest.ini`, selective marker annotations on tests  
**Out of scope**: Enabling live suite on PR CI (needs grok quota — plan 019)

## Steps

1. Add `3.13` to matrix; add `3.14` only if `actions/setup-python` supports it on ubuntu-latest — if not, document skip.
2. Enable `--strict-markers` in pytest.ini addopts (optional if many unknown markers — then fix markers first).
3. Tag pure tests as `unit` (verdict, deny, command_policy) and subprocess/tmp-git as `integration` (state, integrate, modes). Do not require CI to split jobs yet — tagging alone is valuable.
4. Ensure no product test is mis-marked `live`.

## Done criteria

- [ ] CI includes ≥3.13 or documented blocker
- [ ] Markers used on a meaningful subset OR unused markers removed from pytest.ini
- [ ] Hermetic green on 3.11 locally if available

## STOP conditions

- 3.13/3.14 reveals real failures — fix or pin requires-python; do not silently drop versions from README.

## Maintenance notes

- Prefer adding 3.14 when GHA images support it.
