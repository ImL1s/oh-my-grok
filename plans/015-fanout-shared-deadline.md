# Plan 015: Bound process-fanout waits with a shared deadline

> **Drift check**: `git diff --stat 997bcce..HEAD -- omg_cli/fanout.py tests/test_fanout.py`

## Status

- **Priority**: P3
- **Effort**: S–M
- **Risk**: LOW
- **Depends on**: plan 004 (env sanitize) nice-to-have first
- **Category**: perf
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

Workers launch in parallel but waits are sequential with full timeout each — N workers can take ~N×T wall clock. Experimental path still should not hang that badly.

## Current state

- `omg_cli/fanout.py` ~352–360: loop `_wait_proc(proc, launch_timeout)`
- `_wait_proc` ~194–212: `proc.wait(timeout=…)` from wait-call time, not spawn time

## Commands

| Purpose | Command | Expected |
|---------|---------|----------|
| Fanout | `python -m pytest -q tests/test_fanout.py --tb=short` | exit 0 |
| Full | `python -m pytest -q -m "not live"` | exit 0 |

## Scope

**In scope**: `omg_cli/fanout.py`, `tests/test_fanout.py`  
**Out of scope**: Making fanout default; changing skill fanout path

## Steps

1. At spawn, `deadline = time.monotonic() + launch_timeout` (if timeout set; None = unlimited).
2. Each wait: `remaining = max(0, deadline - monotonic())`; if 0, kill remaining workers.
3. Tests with mocked slow procs: second wait does not get full T after first timeout.
4. Full suite.

## Done criteria

- [ ] Shared deadline semantics documented in fanout module docstring
- [ ] Test covers multi-worker timeout budget
- [ ] Hermetic green

## STOP conditions

- Platform wait APIs cannot express remaining timeout — use poll loop with short sleeps; do not busy-spin.

## Maintenance notes

- Align with cancel_run kill semantics for remaining workers.
