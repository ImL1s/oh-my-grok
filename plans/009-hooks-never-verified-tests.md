# Plan 009: Regression lock — hooks never set verified

> **Drift check**: `git diff --stat 997bcce..HEAD -- hooks/bin tests/test_hooks_common.py`

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

Product contract: only `omg` CLI sets `passes`/`verified`. Stop/session hooks must only record events. Today this is comment-enforced; a “helpful” future hook change could forge completion.

## Current state

- `hooks/bin/stop.py` — documents never verified; appends events only.
- `hooks/bin/session_start.py`, `subagent_stop.py` — event spool / resume inject.
- `tests/test_hooks_common.py` — covers `_common` helpers, not verified invariant.

## Commands

| Purpose | Command | Expected |
|---------|---------|----------|
| Hooks tests | `python -m pytest -q tests/test_hooks_common.py --tb=short` | exit 0 |
| Full | `python -m pytest -q -m "not live"` | exit 0 |

## Scope

**In scope**: `tests/test_hooks_common.py` (or new `tests/test_hooks_contract.py`); hooks only if a bug is found  
**Out of scope**: Making hooks fail-closed; Stop pin feature

## Steps

1. Static: for each `hooks/bin/*.py`, assert source does not import `set_verified` / `run_acceptance` / write `verified: true` into status.json (AST or simple string scan excluding comments if needed).
2. Behavioral: create temp project with a run status `verified: false`, pipe minimal JSON stdin to `stop.py` / `session_start.py` / `subagent_stop.py` as subprocess with `PYTHONPATH=repo root`, assert status verified remains false and no acceptance.result created.
3. Full suite.

## Done criteria

- [ ] Automated lock exists
- [ ] Hermetic green

## STOP conditions

- Host requires stop hook to write verified — contradict security-model; STOP.

## Maintenance notes

- New hook scripts must be added to the scan list.
