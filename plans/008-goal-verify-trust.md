# Plan 008: Align goal-verify trust with CLI acceptance (or document disk trust)

> **Drift check**: `git diff --stat 997bcce..HEAD -- omg_cli/goals.py omg_cli/acceptance.py omg_cli/state.py docs/security-model.md tests/test_goals.py`

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: plan 001 recommended (accept path healthy first)
- **Category**: security
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

Run `set_verified` requires an **in-process** acceptance token (disk forgeries rejected). Goal `verify_goal` accepts disk `verified` + CLI stamp with `require_token=False`, so multi-process workflows work but FS-capable agents can promote goals more easily than runs. Product either needs a non-forgeable stamp or an honest security-model residual.

## Current state

- `omg_cli/state.py` `set_verified` / `_has_acceptance_artifact` → `is_trusted_acceptance` (token required).
- `omg_cli/goals.py` ~864–888: `is_cli_acceptance_result(..., require_token=False)` after disk verified.
- `tests/test_goals.py` intentionally clears tokens for multi-process path.

## Commands

| Purpose | Command | Expected |
|---------|---------|----------|
| Goals | `python -m pytest -q tests/test_goals.py --tb=short` | exit 0 |
| Full | `python -m pytest -q -m "not live"` | exit 0 |

## Scope

**In scope**: `omg_cli/goals.py`, `omg_cli/acceptance.py` (if stamp API), `docs/security-model.md`, `tests/test_goals.py`  
**Out of scope**: HSM/keychain; changing run set_verified token model

## Steps

### Product decision (pick A or B in implementation)

**A — Strengthen (preferred if cheap)**  
On `verify_goal`, re-run `freeze_and_run` in-process for the linked run’s frozen manifest (or require operator `omg accept` in same process immediately before verify). Only then append goal verified event. Multi-process path becomes: accept then verify in one CLI invocation wrapper `omg goal verify --accept-first`.

**B — Document residual**  
If multi-process must stay disk-trust: update `docs/security-model.md` with explicit row: “Goal promotion trusts disk CLI stamps; not process-token grade.” Keep tests; add README honesty. Still add a test that pure agent-forged `writer` without matching manifest sha fails.

Implement A unless tests prove multi-process is a hard product requirement that cannot be a single CLI command — then B.

### Implementation notes for A

- Reuse existing freeze/run APIs; do not duplicate policy.
- Do not call `set_verified` with `force=True`.
- Preserve ledger hash chain invariants in `goals.py`.

### Tests

- Forged acceptance.result without valid manifest sha → goal verify fails
- Happy path still works (in-process or documented multi-process)

## Done criteria

- [ ] Either token-grade path exists **or** security-model residual is explicit
- [ ] tests/test_goals.py green
- [ ] Hermetic full suite green

## STOP conditions

- Strengthening breaks documented multi-machine CI goal verify — switch to B and document.

## Maintenance notes

- Reviewer: never weaken run set_verified to match goals; only raise goals or document gap.
