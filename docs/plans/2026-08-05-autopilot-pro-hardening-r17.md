# Autopilot Hardening Round 17 (Pro R16 P1 legacy cancel race) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close Pro R16 remaining P1 — legacy-v1 `_cancel_run_legacy` races `_launch_grok` (cancel can commit `cancelled` while launch still Popens after its pre-check), and legacy `write_status` can resurrect a cancelled run.

**Source:** `/tmp/chatgpt-pro-r16-reaudit.txt` (HAS_P2PLUS @ `ae40e7d`).

**Architecture:** Linearize legacy cancel with the same per-run `transition_guard` as `_launch_grok` / strict cancel (commit terminal + snapshot PIDs under guard; signal outside). Make terminal statuses absorbing on the legacy `write_status` path (guard + no-op refuse). Prefer product-path strict-v2 only if cheap — **not** cheap for `omg pipeline` / process fanout (many `write_status` calls without `execution_lease`); document residual + A/B closure instead.

---

### Task R17-1: Linearize `_cancel_run_legacy` with `transition_guard`

**Files:** `omg_cli/state.py`; tests in `tests/test_state.py` / `tests/test_modes.py`.

**Behavior:**
1. Acquire `transition_guard` for the run
2. Under guard: if already `cancelled` (or verified complete) → idempotent return; snapshot pid targets; write `status=cancelled`; clear active
3. Release guard
4. `_signal_cancel_targets` outside guard (same contract as strict: never signal while transition held)
5. Optional short second guard to record `kill_actions` if still cancelled

**Tests:**
- Cancel-first: legacy cancel then `_launch_grok` → refuse, no Popen
- Launch-holds-guard: barrier after cancel-check under guard; concurrent legacy cancel waits; after Popen+pid publish, cancel sees PID and signals
- Unit: cancel commits cancelled under guard before signalling

**Commit:** `fix(state): linearize legacy cancel under transition_guard`

### Task R17-2: Legacy `write_status` terminal absorbing

**Files:** `omg_cli/state.py`; tests in `tests/test_state.py`.

**Behavior:** Legacy path takes `transition_guard` (or nullcontext if already held). If current status ∈ `TERMINAL_STATUSES` and new status differs → fail-closed no-op (return current bytes; do not resurrect). Same status → idempotent return.

**Tests:** `write_status(..., "running")` after cancel leaves `cancelled`.

**Commit:** `fix(state): refuse legacy write_status resurrection of terminals`

### Task R17-3: Docs + push (no merge)

CHANGELOG Unreleased + short `docs/security-model.md` note: legacy cancel linearized; terminal absorbing; pipeline/ULW remain legacy-v1 by design (lease surface too large for cheap strict-v2 upgrade). Push branch; report HEAD SHA; do **not** merge.
