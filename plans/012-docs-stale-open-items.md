# Plan 012: Refresh stale research/docs claims (OPEN-ITEMS, counts, security headers)

> **Drift check**: `git diff --stat 997bcce..HEAD -- docs/research docs/security-model.md CHANGELOG.md README.md plugin.json`

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: docs
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

Roadmap and verification docs still claim work is open or quote contradictory hermetic counts. Operators re-plan shipped features (interview/QA/goal) and distrust verification packs.

## Current state (at audit; re-check at execution)

- `docs/research/omc-parity-council/OPEN-ITEMS.md` P2 lists deep-interview / UltraQA / goal ledger as open — shipped in 0.2.5+ / skills exist.
- Verification docs disagree on pytest counts (301 vs 402 vs current ~439+).
- `docs/security-model.md` section titled “Spawn fail-closed (0.3.0 Option A)” while plugin is 0.3.0+ shipped soft-gate (retitle honesty).
- `docs/research/test-matrix.md` may pin old product version.
- Absolute home paths in research pointers (CONTRIBUTING forbids).

## Commands

| Purpose | Command | Expected |
|---------|---------|----------|
| Link sanity | manual read + `rg -n "/Users/iml1s" docs/` | no private home paths in tracked docs you edit |
| Version SoT | `python -c "import json; print(json.load(open('plugin.json'))['version'])"` | matches README/security headers you touch |

## Scope

**In scope**: docs listed above (markdown only)  
**Out of scope**: rewriting all historical live logs; code changes

## Steps

1. OPEN-ITEMS: mark shipped P2 items closed; leave true open items (live gates, multi-worker ULW, clean-host).
2. Verification: replace frozen contradictory counts with “re-run: `python -m pytest -q -m 'not live'`” + date; point STATUS at one SoT.
3. security-model: retitle spawn soft fail-closed as shipped; keep fail-open residual.
4. test-matrix header: version or “0.3.x last checked DATE”.
5. Scrub absolute home paths from tracked research pointers.

## Done criteria

- [ ] OPEN-ITEMS does not list shipped interview/QA/goal as unfinished
- [ ] No conflicting canonical pytest counts
- [ ] No new absolute home paths introduced; remove those in files you edit
- [ ] Version headers consistent with `plugin.json`

## STOP conditions

- Unsure if a P2 item is only partially shipped — mark “partial” with file evidence, do not invent DONE.

## Maintenance notes

- After each release, refresh OPEN-ITEMS in the same PR as CHANGELOG.
