# Autopilot Hardening Round 15 (Pro R14 P2) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close the four remaining Pro R14 P2 findings on PR #84 HEAD `63453f0b` until Pro reports 「無 P2 以上問題」.

**Source:** `/tmp/chatgpt-pro-r14-reaudit.txt` (HAS_P2PLUS @ `63453f0bca6447d1eababf130f398c594a4ccdf9`).

**Architecture:** Tri-state PID identity (MATCH/MISMATCH/UNKNOWN); bind stamps to the *target* run's lease via `_require_current_lease`; apply live-leader refuse to ralph/`run_mode` resume as well as autopilot; on fanout pid-publish failure, kill all already-started workers in the batch.

**Tech Stack:** Python 3.11+, pytest, `omg_cli/state.py`, `implementation.py`, `autopilot.py`, `modes.py`, `fanout.py`.

---

### Task R15-1: Tri-state live leader PID identity (P2-1)

**Files:**
- Modify: `omg_cli/state.py` (`pid_matches_recorded`, `live_leader_pid_conflict`)
- Modify: `omg_cli/autopilot.py` (spawn path — only clear on MISMATCH/DEAD)
- Test: `tests/test_autopilot.py` / `tests/test_state.py`

**Behavior:**
- MATCH → refuse spawn, do not clear pid.json
- DEAD or MISMATCH (alive but starttime differs) → clear, allow spawn
- UNKNOWN (alive + recorded starttime present + `process_starttime` returns None) → refuse spawn, do not clear

**Test:** mock `_pid_alive=True`, recorded starttime set, `process_starttime=None` → refuse, pid.json preserved, no Popen.

**Commit:** `fix(state): refuse spawn when live pid starttime probe unknown`

---

### Task R15-2: Bind stamp to target run lease (P2-2)

**Files:**
- Modify: `omg_cli/implementation.py` `stamp_implementation_receipt`
- Test: `tests/test_autopilot.py`

**Before any write and again under `transition_guard`:**
- Call `_require_current_lease(root, run_id, lease)` (from state)
- Refuse if status terminal / pending cancel
- Refuse unless autopilot phase == `implement` (when autopilot sidecar exists)
- `lease.root`/`lease.run_id` must match target

**Tests:**
- run A lease cannot stamp run B
- root A lease cannot stamp root B  
- non-implement phase cannot rebind binder

**Commit:** `fix(implementation): require target-run lease for receipt stamp`

---

### Task R15-3: Ralph/resume live-leader gate (P2-3)

**Files:**
- Modify: `omg_cli/modes.py` (`_launch_grok` / `run_mode` resume path)
- Test: `tests/test_modes.py`

**Before `_spawn_grok_process` on non-dry launch:** call `live_leader_pid_conflict`; on conflict refuse; on stale clear then spawn. Same helper as autopilot.

**Commit:** `fix(modes): refuse ralph resume spawn on live leader pid`

---

### Task R15-4: Fanout rollback prior workers on publish fail (P2-4)

**Files:**
- Modify: `omg_cli/fanout.py`
- Test: `tests/test_fanout.py`

When worker N's `write_pid_metadata` fails after Popen: kill N (already done) **and** kill all earlier successfully published workers in this launch batch; do not leave orphans.

**Commit:** `fix(fanout): kill prior workers if later pid publish fails`

---

### Task R15-5: Docs + push + CI + Pro; merge only if 無 P2+

Update CHANGELOG / security-model / autopilot honesty notes. Push. Codex may be quota-blocked — still request if possible. GPT Pro @GitHub re-audit. Merge only when Pro says 「無 P2 以上問題」.

---
