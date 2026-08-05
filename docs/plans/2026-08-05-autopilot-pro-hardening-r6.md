# Autopilot Hardening Round 6 (Codex P2 after R5) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close two Codex P2s on PR #84 HEAD `9e95e6a`: reset ralplan history/epoch on fresh replan; attached interview close must resume into the same autopilot run.

---

### Task R6-1: Reset ralplan history on fresh replan cycle

**Files:** `omg_cli/ralplan.py`, tests

When starting a strict-v2 cycle under lease with prior `invalidated=True` (or after invalidate left accepted=False with stale history), reset `history` (and session attempt counters / round) so `first_round` starts at 1 and the configured ceiling is available again. Prefer a dedicated `_reset_for_fresh_cycle(state)` called when clearing invalidation at cycle start.

**Test:** seed accepted history through round 5 (or high prior_rounds), invalidate, re-run ralplan with low ceiling — must execute stages, not immediately block.

### Task R6-2: Attached interview close points to autopilot run

**Files:** `omg_cli/interview.py`, tests

On `close_interview` for `mode=="autopilot"`, set `resume_command` to something that keeps the same run_id, e.g. `omg ralplan <goal> --run <run_id>` or `omg autopilot transition ralplan` / documented resume. Do not advertise standalone `omg ralplan <goal>` without `--run`.

**Test:** attach + close → resume_command contains `--run <rid>`.

### Task R6-3: Docs + push + Pro/Codex re-audit

---
