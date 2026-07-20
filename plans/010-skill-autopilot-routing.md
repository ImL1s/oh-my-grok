# Plan 010: Disambiguate autopilot vs pipeline skill triggers

> **Drift check**: `git diff --stat 997bcce..HEAD -- skills/omg-pipeline/SKILL.md skills/omg-autopilot/SKILL.md skills/omg-using/SKILL.md tests/test_skill_inventory.py`

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: MED (routing behavior users may have learned)
- **Depends on**: none
- **Category**: docs
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

`omg-using` routes “autopilot / full auto / build me” → **omg-autopilot** (interview→…→verified CLI phases). `omg-pipeline` still says “User says autopilot…”, loading a different FSM (`omg pipeline` composition). Wrong skill → wrong commands and skipped destination gates.

## Current state

- `skills/omg-using/SKILL.md` — autopilot keywords → `omg-autopilot`; priority cancel > ralplan > autopilot > …
- `skills/omg-pipeline/SKILL.md` ~43 — use-when includes “autopilot”
- Two CLIs: `omg autopilot *` vs `omg pipeline`

## Commands

| Purpose | Command | Expected |
|---------|---------|----------|
| Inventory | `python -m pytest -q tests/test_skill_inventory.py --tb=short` | exit 0 |

## Scope

**In scope**: the three skill markdown files + inventory needles  
**Out of scope**: merging the two FSMs in code

## Steps

1. `omg-pipeline/SKILL.md`: remove bare “autopilot” trigger; use `pipeline`, `plan then implement then accept`, `omg pipeline`. Link: for full lifecycle use `omg-autopilot`.
2. `omg-autopilot/SKILL.md`: keep autopilot keywords; mention pipeline as alternate composition-only path.
3. `omg-using`: ensure priority table unchanged and pipeline has its own keyword row (`pipeline`, not autopilot).
4. Inventory test: pipeline skill must not claim exclusive ownership of word autopilot as primary trigger (e.g. assert “prefer omg-autopilot” or absence of leading autopilot use-when — choose a stable needle).

## Done criteria

- [ ] No skill collision on primary “autopilot” keyword
- [ ] Inventory locks it
- [ ] Hermetic suite still green

## STOP conditions

- Host skill matcher only supports one file and cannot disambiguate — document operator override in omg-using only.

## Maintenance notes

- New mode skills must register unique triggers in omg-using first.
