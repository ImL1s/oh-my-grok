# Plan 019: [Direction] Expand live gates for ralplan / pipeline / ask

> **Drift check**: `git diff --stat 997bcce..HEAD -- scripts/live_suite.sh scripts/canary_pretool.py docs/research/live docs/research/test-matrix.md`

## Status

- **Priority**: P2 (direction)
- **Effort**: M
- **Risk**: MED (quota/flaky)
- **Depends on**: 002, 007 helpful
- **Category**: direction
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

PR CI only hermetic. Host isolation and mode e2e are L2-only. OPEN-ITEMS notes ralplan/pipeline/ask L2 missing from `live_suite --full`. Modes users hit daily can regress without CI signal.

## Current state

- `scripts/live_suite.sh` — ULW/dual/ralph-class gates; re-check for pipeline/ralplan.
- Canary: `scripts/canary_pretool.py`
- Evidence under `docs/research/live/` (often gitignored machine logs)

## Deliverables

1. Design stages:
   - **L-RALPLAN**: dry_run-heavy, artifact + APPROVE parse, no long implement
   - **L-PIPELINE**: stage-order / report.json smoke
   - **L-ASK**: human-broker dry path or mocked provider only (never auto-shell advisors)
2. Implement stages behind `--full` or explicit flags; keep `--quick` fast.
3. Document: PR stays hermetic; release ritual runs live suite; age of evidence note.
4. Optional: non-blocking nightly GHA if secrets/quota available — **do not** hard-fail PRs on live without operator OK.

## Out of scope

- Claiming hard sandbox from canary
- Committing raw machine logs with secrets/home paths

## Done criteria

- [ ] live_suite can run new stages (or documented skip with reason)
- [ ] Summary JSON records pass/fail per stage
- [ ] Docs describe release bar vs PR bar

## STOP conditions

- No grok auth in environment — implement stages as code + dry fixtures; mark live evidence BLOCKED.

## Maintenance notes

- Semantic verdicts: reuse hardened `parse_verdict` (plan 002).
