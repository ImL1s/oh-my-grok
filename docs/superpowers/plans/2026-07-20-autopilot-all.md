# Autopilot plan — 「都要」(2026-07-20)

## Goal

Ship everything from the multi-council thread:

1. **Spawn-retry UX** (already coded) — commit
2. **P0 false-green gates** (Codex critical) — dual-review + ralplan strict verdict
3. **Research docs** omc-parity-council — commit
4. **Tests green** — full unit suite
5. **Fable argv notes** already in global skills

## P0 implementation (this run)

| ID | Work | Files |
|----|------|-------|
| P0-1 | Shared strict verdict: no negated APPROVE; APPROVE only terminal line or JSON | `omg_cli/verdict.py`, dual_review, ralplan |
| P0-2 | Fail-closed: stage rc≠0 cannot return APPROVE | `dual_review.py` |
| P0-3 | Tests: Do not APPROVE; stub; rc≠0 | `test_dual_review.py`, `test_ralplan.py`, `test_verdict.py` |

## Completed in autopilot continuation

- [x] Live suite L-DUAL-1 semantic verdict/rc gate
- [x] Doctor effective discovery foreign orch soft check
- [x] ULW auto-integrate-or-fail (`_ulw_auto_integrate`)
- [x] `omg state --human` lightweight next-hint summary
- [x] Unit tests (300+ non-live)

## Still later (host-heavy / multi-day)

- Full live suite matrix (ralplan/pipeline/ask/multi-worker ULW) on clean host
- Native sessionId / `grok --resume` continuity for ralph
- Claude/Fable free audit re-run (seat BLOCKED 2026-07-20 — see `docs/research/omc-parity-council/STATUS.md`)
- Optional dual-review (Codex+Fable) of post-P0 product commits

## Success (achieved same day)

- `pytest -q -m 'not live'` green (301+)
- Negation / stub / rc fail-closed unit-proven
- live canary + live_suite --quick/--full OK (evidence under `docs/research/live/`)
- Commits on main
- Docs status pack: `docs/research/omc-parity-council/{README,STATUS}.md`
