# Autopilot Hardening Round 9 (Pro R7 leftover P1/P2) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close Pro R7 findings still open after Round 8 on PR #84: missing `ralplan_epoch` migration (P1-2), cancelled-run sidecar/status split on transition/resume (P1-1), non-idempotent attach resume (P2-1).

**Baseline:** HEAD `3432dea` (R8 cancel checks landed).

---

### Task R9-1: Migrate missing ralplan_epoch conservatively (P1-2)

**Files:** `omg_cli/autopilot.py`, tests

Do **not** treat missing `ralplan_epoch` as 0.

On load/transition, if field missing:
- Only set 0 if phase=="interview" AND no CLI ralplan stamp AND cycles.ralplan==0 AND history never shows ralplan (if tracked)
- Otherwise set to at least 1 (and if re-entering ralplan, invalidate quality+consensus)

Reject bool/float/negative; require int >= 0.

**Test:** pre-R7 autopilot.json without epoch, with accepted stamp, review→ralplan → invalidates.

### Task R9-2: Refuse transition/resume when run status terminal (P1-1)

**Files:** `omg_cli/autopilot.py`

At start of `transition()` under lease, before `_save`: if `load_run` status in TERMINAL_STATUSES or cancellation pending → AutopilotError, no sidecar write.

`run_autopilot` / resume: prefer `status.json` terminal over sidecar phase; refuse to launch grok if cancelled.

`status_autopilot`: if run terminal, `legal_next` empty / note cancelled.

### Task R9-3: Idempotent attach resume command (P2-1)

**Files:** `omg_cli/interview.py`

Set attached close `resume_command` to `omg autopilot run --resume <rid>` (single idempotent entry). On early return when interview already complete, refresh resume_command to current version.

### Task R9-4: Docs + push + Pro/Codex; merge only if 無 P2+

---
