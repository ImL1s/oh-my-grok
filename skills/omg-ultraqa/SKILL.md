---
name: omg-ultraqa
description: Bounded adversarial QA repair loop — freeze scenarios, diagnose, retest; never sets verified.
---

# omg-ultraqa

Use after structured review is clean. The CLI owns QA state under
`.omg/state/runs/<run>/stages/ultraqa.json`.

## Contract

- Freeze scenarios before running (`omg qa freeze`).
- Max 5 cycles; repeated failure fingerprints block.
- Product-change repairs require a changed product hash.
- **QA clean ≠ verified.** Only `omg accept` / same-process acceptance can verify.

## CLI

```bash
omg qa freeze --run RUN --scenarios-json '[{"id":"t1","command":"python3 -m pytest tests/test_x.py -q"}]'
omg qa run --run RUN
omg qa run --run RUN --repair-classification product_change
omg qa status --run RUN
```
