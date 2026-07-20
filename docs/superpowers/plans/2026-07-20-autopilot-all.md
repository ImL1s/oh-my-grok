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

## Out of scope this run (P0-4+ backlog)

- Full live suite semantic rewrite
- grok inspect doctor oracle
- Session-aware ralph native resume
- ULW auto-integrate

## Success

- `pytest -q -m 'not live'` green
- Negation / stub / rc fail-closed unit-proven
- Commits on main (or ready to commit)
