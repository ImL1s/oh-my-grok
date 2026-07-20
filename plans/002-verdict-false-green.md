# Plan 002: Close residual verdict false-green (FAILED case + schema-v2 prose)

> **Executor instructions**: Follow step by step. Run every verification command.
> On STOP conditions, report — do not improvise. Update `plans/README.md` when done.
>
> **Drift check**: `git diff --stat 997bcce..HEAD -- omg_cli/verdict.py tests/test_verdict.py omg_cli/ralplan.py omg_cli/dual_review.py`

## Status

- **Priority**: P0
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none (can parallel with 001)
- **Category**: bug
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

Dual-review and ralplan gates use `parse_verdict` / `artifact_contains_approve`. Two residual false-green paths remain after R3 hardening:

1. **`FAILED` is case-sensitive** while `REQUEST_CHANGES` is not — prose `Failed` / `failed` does not block terminal `APPROVE`.
2. **schema_version=2 documents that parse as `UNKNOWN`** (e.g. `verdict: ITERATE`) still fall through to prose and can return `APPROVE` from a terminal line.

## Current state

- `omg_cli/verdict.py`:
  ```python
  _REQUEST_CHANGES_RE = re.compile(..., re.IGNORECASE)
  _FAILED_RE = re.compile(
      r"(?<![A-Za-z0-9_])FAILED(?![A-Za-z0-9_])",
  )  # NO re.IGNORECASE
  ```
- `parse_verdict` (~234–258):
  ```python
  sv2 = parse_schema_v2_verdict(...)
  if sv2 is not None and sv2 != "UNKNOWN":
      return sv2
  # ... JSON objects ...
  # prose path can return APPROVE
  if has_approve:
      return "APPROVE"
  if sv2 == "UNKNOWN":
      return "UNKNOWN"
  ```
- Confirmed at plan time:
  - `parse_verdict("Failed\n\nAPPROVE\n")` → `"APPROVE"`
  - `parse_verdict('{"schema_version":2,"verdict":"ITERATE"}\n\nAPPROVE\n')` → `"APPROVE"`
- Module design comment: FAILED should be safe to over-match; priority FAILED > REQUEST_CHANGES > APPROVE.
- `artifact_contains_approve` does **not** pass `expected_run_id` (secondary; include if cheap).

## Commands you will need

| Purpose | Command | Expected |
|---------|---------|----------|
| Verdict unit | `python -m pytest -q tests/test_verdict.py --tb=short` | exit 0 |
| Related gates | `python -m pytest -q tests/test_dual_review.py tests/test_ralplan.py tests/test_v2_regression_locks.py --tb=short` | exit 0 |
| Full hermetic | `python -m pytest -q -m "not live" --tb=short` | exit 0 |

## Scope

**In scope**:
- `omg_cli/verdict.py`
- `tests/test_verdict.py`
- Optionally `omg_cli/ralplan.py` / `artifact_contains_approve` signature if adding `expected_run_id` (keep optional if timeboxed)

**Out of scope**:
- Changing structured-v2 field parsers used by lifecycle JSON stamps (`parse_structured_verdict`)
- Softening APPROVE terminal-line rules
- Live model fixture rewrites

## Git workflow

- Branch: `advisor/002-verdict-false-green`
- Commit: `fix(verdict): case-insensitive FAILED + no prose fallback for schema-v2`

## Steps

### Step 1: Case-insensitive FAILED

Change `_FAILED_RE` to use `re.IGNORECASE` (same word-boundary shape as today).

Do **not** make bare English word `failed` mid-sentence overly greedy if tests show false FAILED on innocent prose — word boundaries already require token `FAILED`/`Failed`/`failed`. If `"Something failed badly\n\nAPPROVE\n"` becomes FAILED and product wants that, keep it (fail-closed). If dual-review tests break, narrow to line-anchored `^FAILED` styles **and** whole-word IGNORECASE — prefer whole-word IGNORECASE first.

**Verify**:
```bash
python - <<'EOF'
from omg_cli.verdict import parse_verdict
assert parse_verdict("Failed\n\nAPPROVE\n") == "FAILED"
assert parse_verdict("FAILED\n\nAPPROVE\n") == "FAILED"
assert parse_verdict("failed\n\nAPPROVE\n") == "FAILED"
print("ok")
EOF
```

### Step 2: Schema-v2 present ⇒ no prose fallback

In `parse_verdict`, when `sv2 is not None` (including `"UNKNOWN"`), **return `sv2` immediately** before prose:

```python
sv2 = parse_schema_v2_verdict(text, expected_run_id=expected_run_id)
if sv2 is not None:
    return sv2
```

Remove the dead trailing `if sv2 == "UNKNOWN": return "UNKNOWN"`.

**Semantic note**: schema-v2 with usable APPROVE already returns early today. schema-v2 with ITERATE currently returns UNKNOWN from `parse_schema_v2_verdict` only when `_json_verdict` is None — check whether `ITERATE` is returned as a real token. At plan time `verdict: ITERATE` produced prose APPROVE, so `_json_verdict` did not accept ITERATE → UNKNOWN path. After this fix, result should be `UNKNOWN` (fail-closed), not APPROVE.

If product needs structured ITERATE as a first-class return for dual-review, that is a separate enhancement — do **not** invent prose APPROVE for it.

**Verify**:
```bash
python - <<'EOF'
from omg_cli.verdict import parse_verdict
assert parse_verdict('{"schema_version":2,"verdict":"ITERATE"}\n\nAPPROVE\n') != "APPROVE"
assert parse_verdict('{"schema_version":2,"verdict":"APPROVE"}') == "APPROVE"
print("ok")
EOF
```

### Step 3: Unit tests

In `tests/test_verdict.py` (extend existing R3 suite), add cases:

1. `Failed` / `failed` / `FAILED` + terminal APPROVE → `FAILED`
2. schema_v2 ITERATE (or missing verdict) + terminal APPROVE → not APPROVE (`UNKNOWN` or structured non-approve)
3. schema_v2 APPROVE still APPROVE
4. prose-only terminal APPROVE still APPROVE (no schema_v2)
5. REQUEST_CHANGES still beats APPROVE (existing)

**Verify**: `python -m pytest -q tests/test_verdict.py --tb=short` → pass

### Step 4 (optional, same PR if small): `artifact_contains_approve(..., expected_run_id=)`

If ralplan still calls without run_id binding, add optional param and thread from `ralplan.py` verifier gate. STOP if ralplan call graph is larger than ~10 lines.

**Verify**: full hermetic pytest

## Test plan

Model after existing `tests/test_verdict.py` R3 cases. Do not weaken dual_review dry_run stub behavior.

## Done criteria

- [ ] `Failed`/`failed` + terminal APPROVE → FAILED
- [ ] schema_v2 non-APPROVE document cannot prose-APPROVE
- [ ] schema_v2 APPROVE still works
- [ ] `pytest -q tests/test_verdict.py` and full `not live` pass
- [ ] `plans/README.md` → DONE

## STOP conditions

- Existing tests intentionally require prose fallback after schema-v2 UNKNOWN — quote the test and report; do not delete without consensus.
- `_json_verdict` already returns ITERATE and something depends on prose override — report.

## Maintenance notes

- Prefer JSON `{"verdict":"APPROVE"}` for clean acceptance; prose is legacy.
- Reviewer: ensure fail-closed (more UNKNOWN/FAILED) not more APPROVE.
