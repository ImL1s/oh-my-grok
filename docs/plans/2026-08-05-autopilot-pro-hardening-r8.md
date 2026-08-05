# Autopilot Hardening Round 8 (remaining Pro P2) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close remaining Pro R6 P2s still open after Round 7: interview/ralplan cancel races (P2-2/P2-3) and fresh-cycle session UUID reuse (P2-5).

**Baseline:** PR #84 HEAD `c07c78d` (R7 landed P1-1/P1-2/P2-1/P2-4/P2-6).

---

### Task R8-1: Mint new session UUIDs on fresh replan cycle (P2-5)

**Files:** `omg_cli/ralplan.py` `_reset_for_fresh_cycle`, tests

When resetting for a fresh cycle, assign new `session_id` UUIDs for planner/architect/critic and set `attempts=0` (do not reuse old session IDs with attempts reset to 0).

### Task R8-2: Ralplan embedding rejects terminal/cancelled (P2-3)

**Files:** `omg_cli/ralplan.py`

Extend `_authorize_autopilot_embedding` (and under-lease re-check) to reject terminal statuses and pending cancellation requests. Before writing `accepted=True`, re-check. Prefer fail before `save_ralplan_state` when already cancelled.

### Task R8-3: Interview writes check cancel/terminal under lease (P2-2)

**Files:** `omg_cli/interview.py`

Before `_save` / spec write in attach start and `close_interview`, re-check non-terminal + no cancel request under the execution lease (helper shared with `_reauthorize_attach`). Fail closed without writing sidecar if cancelled.

### Task R8-4: Docs + push + Pro/Codex; merge only if 無 P2+

---
