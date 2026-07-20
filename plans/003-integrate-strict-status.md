# Plan 003: Map integrate failure status to strict-v2-legal values

> **Drift check**: `git diff --stat 997bcce..HEAD -- omg_cli/integrate.py omg_cli/state.py omg_cli/pipeline.py tests/test_integrate.py tests/test_workers.py`

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none (001 helpful but not required)
- **Category**: bug
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

Strict-v2 allows status only: `initialized | running | blocked | cancelled | verified`. Integrate failure paths call `write_status(..., "failed")`, which raises `ValueError` on strict runs — `integrate.result.json` may already say failed while `status.json` stays stale. Ralph modes already map non-success to `blocked` for strict; integrate must match.

## Current state

- `omg_cli/state.py` — strict status validation rejects `"failed"` (confirmed: `ValueError: invalid strict-v2 status 'failed'`).
- `omg_cli/integrate.py` — multiple `_write_status("failed", extra={...})` sites (envelope load error ~1043, missing base_sha ~1076, apply failure ~1272+). Result payload still uses `result["status"] = "failed"` (JSON result — OK to keep string for humans; **run status.json** must not use illegal values).
- Legacy v1 still allows historical `"failed"` / `"completed"` — preserve for non-strict runs.
- `pipeline.py` also writes `failed`/`completed`/`verifying` but default pipeline creates legacy runs; optional follow-up in same PR only if you touch pipeline for schema mapping helper.

## Commands

| Purpose | Command | Expected |
|---------|---------|----------|
| Integrate tests | `python -m pytest -q tests/test_integrate.py tests/test_workers.py --tb=short` | exit 0 |
| Full | `python -m pytest -q -m "not live" --tb=short` | exit 0 |

## Scope

**In scope**:
- `omg_cli/integrate.py`
- `tests/test_integrate.py` (and workers if needed)

**Out of scope**:
- Cherry-pick algorithm changes
- Changing integrate.result.json `status` vocabulary (keep `failed`/`missing`/`ok` in result file unless tests force rename)
- Full pipeline FSM rewrite (note residual risk in Maintenance)

## Steps

### Step 1: Schema-aware status helper inside integrate

Near `_write_status` in `integrate_results`:

```python
def _run_status_for_failure() -> str:
    return "blocked" if schema is RunSchema.STRICT_V2 else "failed"
```

Replace every `_write_status("failed", ...)` that targets **run** status with `_write_status(_run_status_for_failure(), ...)`. Keep `result["status"] = "failed"` for the result document.

Success path: if anything writes `"completed"`, map strict → leave `running` or use a legal status + extra fields (prefer **not** inventing new strict statuses). Inspect success `_write_status` calls and only change illegal ones.

### Step 2: Tests

Add/adjust a strict-v2 integrate failure test:
- Create run with `schema_version=2`, `lifecycle_version=2`, optional `base_sha`.
- Trigger a failure that currently calls `_write_status("failed")` (e.g. missing base_sha for strict, or bad envelopes).
- Assert: no uncaught ValueError; `load_run(...).get("status") in {"blocked", "running", "initialized"}` (expect `blocked`); `integrate.result.json` still records failure.

Use hermetic git fixtures from `tests/test_integrate.py` patterns.

### Step 3: Full suite

**Verify**: full `not live` pytest green.

## Done criteria

- [ ] No strict-v2 path calls `write_status(..., "failed")`
- [ ] Failure leaves consistent run status + integrate result
- [ ] tests pass
- [ ] README status DONE

## STOP conditions

- Tests assert run status literally `"failed"` for strict runs — update tests to `blocked` only after confirming product language in README/skills still OK.
- Success path requires a new strict status token — STOP and report; do not expand `_STRICT_STATUSES` without a dedicated plan.

## Maintenance notes

- Pipeline still uses legacy statuses on legacy runs — if pipeline ever creates schema v2, it needs the same mapping (deferred).
