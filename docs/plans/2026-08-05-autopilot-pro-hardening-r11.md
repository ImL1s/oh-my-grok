# Autopilot Hardening Round 11 (Pro R9 + Codex) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close Codex P2 (advance closed interview before resume launch) and Pro R9 P1 on overly permissive `ralplan_epoch` migration; add cancel check before `_launch_grok`.

**Baseline:** HEAD `2ddae8a` (R10).

---

### Task R11-1: Preflight completed interview before launch (Codex)

**Files:** `omg_cli/autopilot.py` `run_autopilot`

Before launching Grok for phase `interview`, if `_interview_complete` then call `_try_advance_after_launch` / transition to ralplan first so resume does not spawn an unnecessary interview Grok.

**Test:** attach+close → phase still interview → `run_autopilot --resume` advances to ralplan without requiring interview launch (mock/dry or assert phase after resume step).

### Task R11-2: Stricter missing-epoch migration (Pro P1)

**Files:** `omg_cli/autopilot.py` `_normalize_ralplan_epoch`

Migrate to 0 only if ALL hold:
- phase == interview
- no CLI ralplan stamp
- cycles.ralplan == 0
- history never contains post-interview phases (ralplan/implement/review/rework/qa/acceptance) — if history missing/corrupt, treat as >=1
- no clean review/QA stamps / implementation receipt on disk

Otherwise migrate to >=1. Persist `ralplan_epoch_source` = native|migrated when writing.

**Test:** missing epoch + phase interview + cycles 0 + prior QA history → migrate to 1 and invalidate on ralplan entry.

### Task R11-3: Cancel/terminal gate before `_launch_grok`

**Files:** `omg_cli/autopilot.py`

Before Popen in `_launch_grok` (or call site in run_autopilot), re-check status not terminal and no cancel request; refuse spawn.

### Task R11-4: Docs + push + Pro/Codex; merge only if 無 P2+

---
