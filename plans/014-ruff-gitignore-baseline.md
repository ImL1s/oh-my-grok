# Plan 014: Add ruff baseline + ignore tool caches

> **Drift check**: `git diff --stat 997bcce..HEAD -- .gitignore requirements-dev.txt pyproject.toml ruff.toml .github/workflows/ci.yml`

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

No project lint config; local `.ruff_cache` exists but is not gitignored — risk of accidental commit and inconsistent style. Cheap DX win for agents and humans.

## Current state

- `requirements-dev.txt` — `pytest>=8.0` only
- `.gitignore` — has pytest/pycache, not `.ruff_cache` / `.mypy_cache`
- No `ruff.toml` / tool tables

## Commands

| Purpose | Command | Expected |
|---------|---------|----------|
| Ruff | `ruff check omg_cli hooks/bin tests` | exit 0 after baseline fixes or noqa policy |
| Pytest | `python -m pytest -q -m "not live"` | exit 0 |

## Scope

**In scope**: `.gitignore`, `requirements-dev.txt` or `pyproject.toml` dev deps, minimal ruff config, CI optional step, fix only **trivial** ruff violations you introduce config for  
**Out of scope**: Full mypy --strict; mass reformat of entire history without operator OK

## Steps

1. Add `.ruff_cache/` and `.mypy_cache/` to `.gitignore`.
2. Add ruff to dev deps (pin reasonably, e.g. `ruff>=0.8`).
3. Minimal `pyproject.toml` or `ruff.toml`: target py311, lint `E,F,I` first; line-length 100 or match existing code.
4. Run ruff; fix high-signal issues only. If thousands of findings, start with `F` (pyflakes) only and `extend-ignore` the rest — do not reformat entire codebase in this plan unless ruff check is already clean.
5. Optional CI step: `ruff check omg_cli hooks/bin` (not blocking format initially).

## Done criteria

- [ ] Cache dirs ignored
- [ ] `ruff check` documented in CONTRIBUTING
- [ ] CI or local gate exists
- [ ] Pytest still green

## STOP conditions

- Auto-fix would touch 50+ files for style only — stop after gitignore + config + CONTRIBUTING; leave cleanup follow-up.

## Maintenance notes

- Prefer one tool (ruff) over black+isort+flake8 stack.
