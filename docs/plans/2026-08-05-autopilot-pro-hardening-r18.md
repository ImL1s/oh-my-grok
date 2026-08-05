# Autopilot Hardening Round 18 (Pro R17b P1/P2) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close Pro R17b remaining findings — process-fanout worker Popen/PID publish races legacy cancel (P1-1), legacy `set_verified` can overwrite `cancelled` → `verified` (P1-2), and `clear_active` can unlink a newer run's `active.json` after `create_run` (P2-1).

**Source:** `/tmp/chatgpt-pro-r17b-reaudit.txt` (HAS_P2PLUS @ `d44a1c00`).

**Architecture:** Mirror R16/R17 `_launch_grok` linearization for each fanout worker spawn under per-run `transition_guard`. Make legacy `set_verified` re-read under the same guard and treat `cancelled` as absorbing (idempotent `verified`; force+capability remains the only break-glass). Serialize `active.json` read/compare/unlink with `create.lock` (same flock as `create_run`); never acquire create while holding transition — cancel commits terminal under transition, then clears active under create lock after release. Force-create already holds create → use held-lock / unlocked clear helper (no reentrancy / lock-order reversal).

---

### Task R18-1: Fanout spawn linearize with cancel (P1-1)

**Files:** `omg_cli/fanout.py`; tests in `tests/test_fanout.py`.

**Behavior:**
1. Each worker's cancel recheck → Popen → PID publish under `transition_guard` (same pattern as `_launch_grok`)
2. `launch_refused_for_cancel` under guard → if refuse: do not Popen; stop loop; R15-4-style rollback of prior workers
3. Wait remains outside the guard

**Tests:**
- Cancel-first: cancel then fanout → no Popen for any worker
- Worker holds guard: barrier after cancel-check; concurrent cancel waits; after Popen+pid publish, cancel snapshots and signals that worker

**Commit:** `fix(fanout): linearize process-fanout spawn under transition_guard`

### Task R18-2: Legacy `set_verified` refuse cancelled (P1-2)

**Files:** `omg_cli/state.py`; tests in `tests/test_state.py`.

**Behavior:** Legacy-v1 path takes `transition_guard` (nullcontext if held). Re-load status under guard. `cancelled` → `PermissionError` unless `force=True` with in-process force capability. Already `verified` → idempotent return. Strict-v2 unchanged (`_commit_strict_status_locked` already absorbs).

**Tests:** After cancel + trusted acceptance token, `set_verified` raises and status stays `cancelled`; verified→verified idempotent; force+capability may break-glass.

**Commit:** `fix(state): refuse legacy set_verified overwrite of cancelled`

### Task R18-3: `clear_active` / `create_run` shared create.lock (P2-1)

**Files:** `omg_cli/state.py`; tests in `tests/test_state.py`.

**Behavior:**
1. Track create flock on `_held_lock_kinds` (`"create"`); forbid acquiring create while transition held
2. `_clear_active_unlocked` does read/compare/unlink; `clear_active` acquires `create.lock` unless already held
3. `_cancel_run_legacy`: commit cancelled under transition **without** clear; release; then `clear_active` under create lock
4. `_create_run_unlocked` / force path: clear uses held create lock (no re-acquire)

**Tests:** Barrier race — `clear_active(A)` paused after seeing A; `create_run(B)` publishes active=B; resume clear must not unlink B.

**Commit:** `fix(state): serialize clear_active with create.lock`

### Task R18-4: Docs + push (no merge)

CHANGELOG Unreleased. Push branch; report HEAD SHA + unit test results; do **not** merge.
