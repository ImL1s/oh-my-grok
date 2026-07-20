# Plan 018: [Direction] Dual-review product decision — end permanent “interim”

> **Drift check**: `git diff --stat 997bcce..HEAD -- omg_cli/dual_review.py omg_cli/review.py skills/omg-dual-review docs/security-model.md`

## Status

- **Priority**: P2 (direction)
- **Effort**: M
- **Risk**: MED
- **Depends on**: plan 002 (verdict correctness)
- **Category**: direction
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

Dual-review is on the default spine but still labeled interim sequential headless. `OMG_DUAL_REVIEW_REQUIRE_NATIVE=1` fails without a native product path. Operators need one honest story.

## Options

| Option | Meaning | Cost |
|--------|---------|------|
| **A** | Freeze sequential as permanent PARTIAL product; drop “interim”; keep REQUIRE_NATIVE for power users only | S docs + skill |
| **B** | Ship single-leader dual that spawns critic+verifier RO and only parses artifacts | M–L code + tests |

## Deliverables

1. ADR-style note in `docs/research/` or `docs/` choosing A or B with rationale.
2. Skill + README wording update matching choice.
3. If A: no code beyond wording/tests that lock “never sets verified”.
4. If B: separate build plan after ADR (do not implement full B inside this plan without operator confirm).

## Out of scope

- Dual-review writing `verified`
- Replacing structured `omg review` hash-bound gate

## Done criteria

- [ ] Written decision A or B
- [ ] Skills/docs no longer say open-ended “interim” without date/owner
- [ ] Tests still assert never verified

## STOP conditions

- Product owner unavailable to pick A/B — default **A** and document.

## Maintenance notes

- Structured review (`review.py`) remains separate hash-bound path for autopilot cleanliness.
