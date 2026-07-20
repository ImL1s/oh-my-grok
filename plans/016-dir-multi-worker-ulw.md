# Plan 016: [Direction spike] Multi-worker ULW closed path as product-default truth

> **Executor instructions**: This is a **design/spike plan**, not “build everything.”  
> Deliver a short design doc + minimal proof prototype or live gate sketch. Do not claim product done without evidence.
>
> **Drift check**: `git diff --stat 997bcce..HEAD -- omg_cli/workers.py omg_cli/integrate.py omg_cli/modes.py skills/omg-ultrawork scripts/live_suite.sh docs/research`

## Status

- **Priority**: P2 (direction)
- **Effort**: L (spike M; full L)
- **Risk**: MED
- **Depends on**: 001, 003 for healthy integrate/accept
- **Category**: direction
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

“Ultrawork” markets parallel work. Default path is often single-leader spawn soft-nudge; process fanout experimental; live suite has L-ULW-1 without multi-envelope proof. Non-authors cannot trust parallel claims.

## Current state (evidence)

- OPEN-ITEMS P1-2 multi-worker prepare/seal/integrate still open (re-check file).
- `scripts/live_suite.sh` — L-ULW style single path.
- Ownership/seal/join APIs exist in `workers.py` / `integrate.py` with hermetic tests; **live closed path** is the gap.
- Scope honesty: not full OMC; HUD/tmux team NEVER.

## Deliverables (spike)

1. **Design note** under `docs/research/` or `plans/artifacts/016-ulw-closed-path.md`:
   - Happy path: prepare×N → spawn N RW implementers → seal×N → integrate → accept
   - Solo path remains exit-0 without envelopes
   - Failure modes: partial seal, base_sha mismatch, join gate
2. **Minimal hermetic extension** (optional in spike): fixture with 2 sealed envelopes + integrate dry_run assertions (if not already in `test_workers`/`test_integrate` — cite existing and only add live gap list).
3. **Live gate sketch** for `live_suite.sh`: assert ≥2 sealed envelopes OR explicit skip reason.
4. **Claim freeze** text for README: when multi-worker is PARTIAL vs HAVE.

## Out of scope

- Process fanout as default
- tmux multi-CLI team
- Auto-integrating untrusted envelopes without ownership manifest

## Done criteria (spike)

- [ ] Written design with success metrics
- [ ] Gap list vs current code with file:line
- [ ] Live gate acceptance criteria defined (even if not implemented)
- [ ] No false README claim that multi-worker is proven without evidence

## STOP conditions

- Host cannot spawn N workers reliably — document host limit and reduce N=2 aspirational only.

## Maintenance notes

- Full implementation should be a follow-up plan after spike approval.
