# Plan 007: Ralplan v2 happy-path tests + skill/docs alignment

> **Drift check**: `git diff --stat 997bcce..HEAD -- omg_cli/ralplan.py tests/test_ralplan.py tests/test_v2_regression_locks.py skills/omg-ralplan/SKILL.md tests/test_skill_inventory.py`

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: plan 002 recommended first (verdict correctness)
- **Category**: tests
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

Strict ralplan v2 validation (`_validate_v2_proposal`) requires identity binding and stage fields, but hermetic tests use incomplete fake executors so `accepted` never becomes true for the right reasons. Negative tests stay green if the gate breaks. The in-session skill still documents v1-only stages while CLI has dual FSM.

## Current state

- `omg_cli/ralplan.py` — `_run_ralplan_v1` vs `_run_ralplan_v2`; new runs without schema may still be v1; resume/existing_run_id can enter v2.
- `tests/test_v2_regression_locks.py` — thin v2 ralplan coverage; fake executor often omits identity fields.
- `tests/test_ralplan.py` — v1 FSM covered.
- `skills/omg-ralplan/SKILL.md` — draft→critic→revise→verifier (v1); missing planner/architect/critic structured stamps; may still say `omg ask` is “future”.
- `tests/test_skill_inventory.py` — deep-checks only some skills.

## Commands

| Purpose | Command | Expected |
|---------|---------|----------|
| Ralplan tests | `python -m pytest -q tests/test_ralplan.py tests/test_v2_regression_locks.py -k ralplan --tb=short` | exit 0 |
| Skills inventory | `python -m pytest -q tests/test_skill_inventory.py --tb=short` | exit 0 |
| Full | `python -m pytest -q -m "not live"` | exit 0 |

## Scope

**In scope**:
- `tests/test_v2_regression_locks.py` and/or new `tests/test_ralplan_v2.py`
- `skills/omg-ralplan/SKILL.md`
- `tests/test_skill_inventory.py` (needles for ralplan)
- Minimal code changes only if a test-only injection point is missing (prefer not changing product code)

**Out of scope**:
- Live Grok ralplan
- Deleting v1 path
- Dual-review rewrite (plan 018)

## Steps

### Step 1: Read `_validate_v2_proposal` and stage executor kwargs

Document required fields for planner / architect / critic proposals (invocation_id, session_id, input_sha256, verdict fields). Use those exact field names in the test fake.

### Step 2: Happy-path hermetic test

Inject a stage executor that, for each stage, writes a valid JSON artifact meeting validation so the FSM reaches `accepted is True` while `verified is False` (ralplan must never set verified).

Assert stamp files under the run dir if the code writes them.

### Step 3: Reject matrix unit tests

Missing identity → not accepted; stub markers → fail; critic REQUEST_CHANGES → not accepted.

### Step 4: Skill update

Update `skills/omg-ralplan/SKILL.md`:
- Document v1 frozen path vs strict-v2 planner→architect→critic when applicable
- Replace “future `omg ask`” with human-invoked `omg ask` / skill `omg-ask`
- HARD RULES: capability_mode, no verified forge, depth=1

### Step 5: Inventory needles

Extend `test_skill_inventory.py` so ralplan skill must mention capability_mode + (v2 or planner) + never set verified.

## Done criteria

- [ ] At least one hermetic test reaches ralplan v2 `accepted=True` for the right validation reasons
- [ ] Skill no longer calls ask “future”
- [ ] Inventory test locks key needles
- [ ] Hermetic suite green

## STOP conditions

- v2 path is unreachable without live host fields you cannot synthesize — STOP and report missing factory helpers; do not mark accepted with mocks that skip `_validate_v2_proposal`.

## Maintenance notes

- When adding ralplan stages, extend the happy-path fake and inventory needles.
