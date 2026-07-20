# Plan 006: Centralize safe run_id validation on all path joiners

> **Drift check**: `git diff --stat 997bcce..HEAD -- omg_cli/state.py omg_cli/evidence.py omg_cli/acceptance.py omg_cli/ask omg_cli/fanout.py omg_cli/modes.py omg_cli/dual_review.py omg_cli/integrate.py omg_cli/interview.py`

## Status

- **Priority**: P2
- **Effort**: S–M
- **Risk**: LOW
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

Some modules validate `run_id` before joining under `.omg/state/runs/`; others join raw CLI/`--run` strings. Path segments like `../` can target unexpected directories when a directory exists. Defense-in-depth: one validator everywhere.

## Current state

- Strict: `omg_cli/evidence.py` `validate_identifier` — `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`
- Loose: `omg_cli/state.py` `_safe_run_id` — rejects empty/`..`/separators
- Unsafe/raw joins reported at plan time: `ask/broker.py`, `fanout.py` `_run_dir`, `modes.py` helpers, `dual_review.py`, `integrate.run_dir` (verify each still raw at execution time)

## Commands

| Purpose | Command | Expected |
|---------|---------|----------|
| Related tests | `python -m pytest -q tests/test_state.py tests/test_ask.py tests/test_fanout.py tests/test_evidence.py --tb=short` | exit 0 |
| Full | `python -m pytest -q -m "not live"` | exit 0 |

## Scope

**In scope**: every `runs / run_id` style join in `omg_cli/` + tests for traversal rejection  
**Out of scope**: renaming historical run IDs on disk; changing run_id generation format

## Steps

1. Pick **one** public helper (prefer export `safe_run_id` from `state.py` or use `evidence.validate_identifier` if all run_ids match that alphabet — real run ids look like `20260720T171602Z-72b5993f` which fits both).
2. Replace raw joins; raise `ValueError` early with clear message.
3. Tests: `../evil`, `foo/bar`, empty string rejected by ask/fanout/CLI `--run` paths.
4. Full hermetic suite.

## Done criteria

- [ ] No raw `runs / run_id` without validation in omg_cli (grep proof)
- [ ] Traversal tests pass
- [ ] Hermetic green

## STOP conditions

- Historical run ids contain characters rejected by strict regex — keep `_safe_run_id` looser but still path-safe; do not orphan old runs.

## Maintenance notes

- New modules must import the helper; add to AGENTS/dev note if convenient (not required).
