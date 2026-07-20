# Spike: Multi-worker ULW closed path (plan 016)

**Date:** 2026-07-21 · **Status:** design only

## Happy path (target product truth)

1. Leader: `omg worker prepare` ×N (or skill instructs prepare per task)
2. Spawn N implementers with `capability_mode=read-write` (no Execute preferred)
3. Each worker: `omg worker seal` → envelope under `.omg/artifacts/ulw-results/<run_id>/`
4. Leader: ownership join + `omg integrate`
5. `omg accept` → verified

Solo path remains valid: zero envelopes → integrate missing/skip; exit 0 without integrate success claim.

## Gaps vs HEAD

- Default skill fanout is soft-nudge spawn, not guaranteed N workers
- Live suite L-ULW-1 does not assert ≥2 sealed envelopes
- Process fanout experimental + env sanitized (plan 004)

## Live gate acceptance criteria

- `live_suite` stage: prepare×2 → seal×2 → integrate result ok OR explicit skip
- Summary JSON records envelope_count ≥ 2 when stage enabled

## Claim freeze

Until live gate green: market as "parallel-capable; multi-worker closed path PARTIAL".
