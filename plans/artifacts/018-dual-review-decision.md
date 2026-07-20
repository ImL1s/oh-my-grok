# ADR: Dual-review product decision (plan 018)

**Date:** 2026-07-21 · **Decision: Option A**

## Choice

**Freeze sequential headless dual-review as permanent PARTIAL product.**

- Drop open-ended "interim forever" marketing
- Keep `OMG_DUAL_REVIEW_REQUIRE_NATIVE=1` as power-user refuse of CLI path
- Never sets `verified` (accept remains sole path)
- Option B (leader + 2 RO spawn packaging) deferred until host stability + quota allow

## Rationale

Independence story is weaker than true parallel critics, but sequential is honest,
tested, and already on the spine. Shipping B without host proof recreates false claims.

## Follow-up

Skill wording: "sequential dual-review (PARTIAL independence)" not "interim".
