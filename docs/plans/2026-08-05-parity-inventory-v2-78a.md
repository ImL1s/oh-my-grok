# Parity Inventory v2 (#78-A) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Ship #78-A — a claimability-safe parity inventory v2 + strict local drift gate so later Antigravity/parity child PRs cannot overclaim.

**Architecture:** Extend `omg_cli/contracts/parity_schema.py` with v2 schema (ordered maturity enum, expanded classifications, capability rows with upstream pins separate from OMG revision). Keep v1 fixture for migration tests. Add `omg parity check` / `omg parity gaps` over the existing `omg parity` command family. Generate FEATURE-MATRIX.md / GAPS.md without percentages when inventory is bootstrapping. Wire CI `--strict` check. Do **not** close #78/#79.

**Tech Stack:** Python 3.11+, pytest, existing `omg_cli/contracts/*`, `scripts/check_parity_inventory.py`, `omg_cli/commands/inspect.py`.

**Source:** GPT Pro plan `/tmp/chatgpt-pro-issues-plan.txt` @ main `4a759d0d`.

**Out of scope:** Full upstream inventory (#78-B), live evidence freshness (#78-C), Antigravity adapter (#67), Team/jobs runtime changes, capability-lock catalog rewrite, closing #78/#79.

---

### Task 1: Branch + failing schema tests

**Files:**
- Create: `tests/test_parity_inventory_v2.py` (or extend `tests/test_parity_inventory.py`)
- Modify: `omg_cli/contracts/parity_schema.py`
- Create: `tests/fixtures/parity/omg-parity-v1.json` (snapshot of current v1 if not present)

**Step 1:** Write failing tests named exactly:
- `test_v2_uses_user_observable_capability_ids`
- `test_duplicate_capability_id_rejected`
- `test_upstream_pin_requires_exact_revision_and_existing_source_paths`
- `test_alias_requires_existing_canonical_target`
- `test_maturity_prerequisites_are_monotonic`
- `test_live_verified_requires_fresh_runtime_platform_evidence`
- `test_host_impossible_cannot_generate_positive_claim`
- `test_incomplete_inventory_cannot_emit_percentage_or_checkmark`

**Step 2:** `pytest … -q` → FAIL

**Step 3:** Implement v2 validators + constants (maturity ordered enum, classifications including `antigravity_native`, `omg_native`, `alias`, `excluded`; remove reliance on independent booleans for maturity).

**Step 4:** Tests pass for Task 1 names.

**Step 5:** Commit `feat(parity): add inventory v2 schema validators`

---

### Task 2: Canonical inventory JSON (bootstrapping) + migration

**Files:**
- Modify: `docs/parity/omg-parity.json` → schema_version 2 bootstrapping inventory
- Create: `docs/parity/schema-v2.md`, update `docs/parity/README.md`
- Keep v1 fixture under `tests/fixtures/parity/`

Seed rows at least for open P0 gaps: #67, #68, #69, and #78 remaining slices — as `catalogued` / open gaps, **not** live_verified. Upstream pins for OMC/OMX/OmO/Antigravity; **do not** hardcode OMG candidate commit in canonical file.

**Commit:** `feat(parity): seed bootstrapping v2 inventory with P0 gaps`

---

### Task 3: Generators + check scripts

**Files:**
- Modify/Create: `scripts/check_parity_inventory.py` (`--strict`)
- Create/Modify: `scripts/generate_parity_docs.py` (`--check`)
- Generate: `docs/parity/FEATURE-MATRIX.md`, `docs/parity/GAPS.md`

Rules: incomplete/bootstrapping → no parity %, no green checkmarks; `host_impossible`/`excluded` never positive claim; gaps list open P0s.

Tests: `test_generated_feature_matrix_is_current`, `test_generated_gap_report_contains_open_p0s`

**Commit:** `feat(parity): generate matrix/gaps and strict inventory check`

---

### Task 4: CLI `omg parity check` / `gaps`

**Files:**
- Modify: `omg_cli/commands/inspect.py`
- Tests: `tests/test_command_family_inspect.py` — `test_parity_check_uses_global_json_envelope`

**Commit:** `feat(parity): add omg parity check and gaps subcommands`

---

### Task 5: Packaging + CI

**Files:**
- Ensure inventory ships with package (`setup_cmd` / packaging tests)
- Modify: `.github/workflows/ci.yml` to run strict check
- Tests: `test_packaged_install_contains_canonical_parity_inventory`

**Commit:** `ci(parity): enforce strict inventory gate`

---

### Task 6: Docs honesty + CHANGELOG + open PR

CHANGELOG Unreleased; note #78 remains open. Open PR linking #78 (partial). Wait CI + Codex + GPT Pro; merge only if 「無 P2 以上問題」. Do not close #78.

---
