# Plan 001: Make `omg accept` set_verified succeed on strict-v2 runs

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 997bcce..HEAD -- omg_cli/main.py omg_cli/state.py omg_cli/acceptance.py tests/test_cli_router.py tests/test_acceptance.py tests/test_state.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P0
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

Default `omg ralph` creates **strict-v2** runs (`schema_version=2`, `lifecycle_version=2`). After acceptance commands pass, `omg accept` calls `set_verified` **without** an execution lease. Strict status commits require a lease, so the operator gets `FencingError` and the run never becomes `verified` — the main product completion gate is broken for the default path. Autopilot and integrate already pass/acquire leases; accept must match.

## Current state

- `omg_cli/modes.py` — new standalone ralph sets schema v2:
  ```python
  if mode == "ralph" and not explicit_resume and existing_run_id is None:
      create_extra.update({"schema_version": 2, "lifecycle_version": 2})
  ```
- `omg_cli/main.py` ~601–605 — `cmd_accept` after successful `freeze_and_run`:
  ```python
  try:
      verified = set_verified(root, run_id, force=False)
  except PermissionError as exc:
      print(f"set_verified failed: {exc}", file=sys.stderr)
      return 1
  ```
  Does **not** import/use `execution_lease`. Does **not** catch `FencingError`.
- `omg_cli/state.py` `set_verified` — for `RunSchema.STRICT_V2` calls `_commit_strict_status_locked` which requires `lease` via `_require_current_lease` when status is not cancel-only.
- Working pattern to copy: `omg_cli/integrate.py` ~952–954 acquires lease when strict and caller lease is None; `omg_cli/autopilot.py` ~400 passes `lease=lease` into `set_verified`.
- `tests/test_cli_router.py` `test_accept_cli_freeze_and_run` uses `create_run` **without** schema v2 extra — so it only proves **legacy** accept works.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Hermetic tests | `python -m pytest -q -m "not live" --tb=short` | exit 0 |
| Focused tests | `python -m pytest -q tests/test_cli_router.py tests/test_state.py tests/test_acceptance.py -k "accept or verified" --tb=short` | exit 0 |
| Smoke | `OMG_E2E=1 OMG_SMOKE_STRICT=0 ./scripts/smoke.sh` | exit 0 |

Use project venv if present: `.venv/bin/python -m pytest ...`

## Scope

**In scope**:
- `omg_cli/main.py` (`cmd_accept` only, unless you choose the state.py auto-lease approach below)
- `omg_cli/state.py` (only if implementing auto-lease inside `set_verified` — preferred single chokepoint)
- `tests/test_cli_router.py` and/or `tests/test_state.py` / `tests/test_acceptance.py` (new regression)

**Out of scope**:
- Changing process-token acceptance trust model
- `force=True` on `set_verified` (must remain unexposed on CLI)
- Pipeline/modes refactor beyond accept path
- Live Grok tests

## Git workflow

- Branch: `advisor/001-accept-strict-v2-lease`
- Commit style (from recent log): conventional, e.g. `fix(accept): acquire execution lease for strict-v2 set_verified`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Choose fix shape (prefer A)

**Option A (recommended)** — auto-acquire short-lived lease inside `set_verified` when:
- schema is strict-v2,
- `lease is None`,
- `force` is False or True (still need lease for strict write),
- then call `_commit_strict_status_locked` with that lease and release it.

Match integrate’s intent string pattern: `execution_lease(root, run_id, intent="accept")` (or `"set_verified"` if intent is free-form — check `ExecutionLease` / `execution_lease` signature and existing intents).

**Option B** — only fix `cmd_accept`:
```python
from omg_cli.state import load_active_run, load_run, set_verified, execution_lease, classify_run_schema, RunSchema, FencingError

run = load_run(root, run_id)
# after acceptance ok:
try:
    if classify_run_schema(run) is RunSchema.STRICT_V2:
        with execution_lease(root, run_id, intent="accept") as lease:
            verified = set_verified(root, run_id, force=False, lease=lease)
    else:
        verified = set_verified(root, run_id, force=False)
except (PermissionError, FencingError) as exc:
    ...
```

Option A fixes all callers (pipeline `_default_accept`, scripts, future CLI). Prefer A unless STOP: `execution_lease` cannot be nested / would deadlock with callers that already hold a lease — then implement: if `lease is not None` use it; else acquire.

Also widen `cmd_accept` except to include `FencingError` (and any `LifecycleLockError` if relevant) so CLI never dumps traceback for fencing failures.

**Verify**: `python -c "from omg_cli.state import set_verified, execution_lease; print('ok')"` → no import error

### Step 2: Regression test — strict-v2 accept path

Add a test modeled after `tests/test_cli_router.py::test_accept_cli_freeze_and_run`:

1. `create_run(tmp_path, mode="ralph", goal="strict accept", force=True, extra={"schema_version": 2, "lifecycle_version": 2})`
2. Write `prd.json` with `true` command (same schema as existing test).
3. Run `omg accept --run <rid> --yes` via the test’s `_run_omg` helper (or in-process if other tests do).
4. Assert returncode 0.
5. `load_run` → `verified is True` and `status == "verified"`.

Also add a pure unit test: after `run_acceptance` in-process, `set_verified(root, rid, force=False)` with **no** caller lease must succeed after Option A (or document that only CLI path is fixed under Option B).

**Verify**:
```bash
python -m pytest -q tests/test_cli_router.py -k "accept" --tb=short
```
→ all pass, including new strict test

### Step 3: Full hermetic gate

**Verify**:
```bash
python -m pytest -q -m "not live" --tb=short
```
→ exit 0

## Test plan

| Case | Expected |
|------|----------|
| Legacy create_run + accept | still verified (existing test) |
| Strict-v2 create_run + accept CLI | verified True, exit 0 |
| Strict-v2 set_verified without caller lease after run_acceptance | succeeds (if Option A) |
| set_verified without acceptance token | still PermissionError |

Pattern: `tests/test_cli_router.py::test_accept_cli_freeze_and_run`

## Done criteria

- [ ] `omg accept` on a strict-v2 run with passing `true` acceptance sets `verified=true` without caller-supplied lease
- [ ] New regression test exists and passes
- [ ] `python -m pytest -q -m "not live"` exits 0
- [ ] No files outside scope modified
- [ ] `plans/README.md` status → DONE

## STOP conditions

- `execution_lease` nesting deadlocks with autopilot/modes when both hold leases — report; implement “use provided lease else acquire” carefully.
- `FencingError` type is not exportable / renamed — find actual exception class in `state.py` and use that.
- Existing accept tests fail for reasons unrelated to lease (PRD schema) — fix test PRD first, do not weaken policy.

## Maintenance notes

- Any new status-mutating CLI path for strict-v2 must either pass a lease or rely on auto-acquire in `set_verified`.
- Reviewer: confirm `force=True` still not wired to argparse; confirm token check still runs **before** lease commit.
- Deferred: pipeline non-terminal statuses mapping (plan 003).
