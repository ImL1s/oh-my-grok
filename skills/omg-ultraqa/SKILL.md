---
name: omg-ultraqa
description: >
  Bounded adversarial QA repair loop for oh-my-grok. Use when user says ultraqa,
  QA cycle, fix failing tests, retest until green, freeze scenarios, or post-review
  QA. CLI owns stages/ultraqa.json; QA clean ≠ verified.
---

# omg-ultraqa — In-session adversarial QA loop

Drive **`omg qa *`** until scenarios pass or the cycle budget blocks — then hand
off to acceptance. Never self-verify the run.

## HARD RULES

1. Fan-out only via Grok `spawn_subagent` (depth=1).
2. Always set `capability_mode` (`read-write` implementer, `read-only` diagnose).
3. **QA clean ≠ verified.** Only `omg accept` / autopilot complete may verify.
4. Never write `passes`/`verified` under `.omg/state/`.
5. Freeze scenarios before run; do not ad-hoc change the freeze mid-cycle without CLI.
6. Cancel with `omg cancel` — never self-matching `pkill -f`.

## Use when

- User says `ultraqa`, `QA loop`, `fix failing tests`, `retest`, post dual-review QA.
- Autopilot / pipeline entered `qa` phase.
- Need bounded diagnose→repair→retest with fingerprint block.

## Do not use when

- No scenarios yet and review not clean → finish review first.
- Want run verified → `omg accept`, not this skill alone.
- One-off single test without freeze → still prefer freeze for auditability.

## Session playbook

### 0. Bootstrap

```bash
omg doctor
omg qa status --run RUN   # if resuming
```

### 1. Freeze scenarios

Commands must pass the **acceptance command policy** at freeze time (same floor
as `omg accept`). Prefer:

- `python3 -m pytest -q -m 'not live'` — **quote** the marker expression
- `python3 path/to/project_check.py` — project `.py` under the repo
- `true` / `false` for trivial harness checks

**Do not** freeze: `grep`, `test`, `omg`, `python3 -c '…'`, or
`python3 -m omg_cli.main` (only `-m pytest|unittest` allowed).

```bash
omg qa freeze --run RUN --scenarios-json '[
  {"id":"unit","command":"python3 -m pytest -q -m '"'"'not live'"'"'"},
  {"id":"check","command":"python3 scripts/check_smoke.py"}
]'
```

Unquoted ` -m not live ` is auto-coalesced to `not live` when possible, but
always quote markers in playbooks.

### 2. Cycle: run → diagnose → repair → re-run

```bash
omg qa run --run RUN
omg qa status --run RUN
```

On failure:

1. Read failure fingerprint / logs under run `stages/`.
2. `spawn_subagent` debugger/explorer **read-only** to locate root cause.
3. `spawn_subagent` executor **read-write** for product fix (or test fix if intentional).
4. Product-change repairs: tell CLI classification when required:

```bash
omg qa run --run RUN --repair-classification product_change
```

5. Repeat until clean or max cycles (CLI enforces ~5; repeated fingerprints block).

### 3. Hand off (do not claim verified)

```bash
omg qa status --run RUN
# if clean:
omg autopilot transition --run RUN --phase acceptance --reason "ultraqa clean"
# prd.json optional: accept/complete materialize from clean ultraqa when missing
omg accept --run RUN --yes
# or (idempotent if already verified):
omg autopilot complete --run RUN
```

## Contract

- State: `.omg/state/runs/<run>/stages/ultraqa.json`
- Max cycles + fingerprint block are CLI-owned.
- Product-change repairs may require a changed product hash (CLI).

## Anti-patterns

- Infinite manual retest without freeze
- Claiming verified after green pytest alone
- Skipping dual-review then only running QA
- Editing ultraqa.json by hand

## Related

- `omg-autopilot` phase `qa` · `omg-dual-review` · `omg-ralph` outer loop
- Continuity: `omg resume` / `.omg/state/RESUME.md` if session ends mid-QA

## CLI

```bash
omg qa freeze --run RUN --scenarios-json '[...]'
omg qa run --run RUN
omg qa run --run RUN --repair-classification product_change
omg qa status --run RUN
```
