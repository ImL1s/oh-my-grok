# Design: Live gates expand (plan 019)

**Date:** 2026-07-21 · **Status:** design only

## Stages to add

| Stage | Intent | PR CI |
|-------|--------|-------|
| L-RALPLAN | dry_run + artifact APPROVE parse | no |
| L-PIPELINE | stage-order / report.json smoke | no |
| L-ASK | broker dry / mocked provider only | no |

## Release bar

- PR: hermetic `pytest -m "not live"` + smoke STRICT=0
- Release: `live_suite --quick` or `--full` when grok auth present
- Evidence: scrubbed summary under docs/research/live/ (no home paths/secrets)

## Skip

If no grok: stage records SKIP with reason; do not claim pass.
