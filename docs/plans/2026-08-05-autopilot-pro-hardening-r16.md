# Autopilot Hardening Round 16 (Pro R15 P1 + nest fix confirm) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close Pro R15 remaining P1 (ralph `_launch_grok` ignores cancel/terminal) and confirm nested-guard dry-run fix on HEAD `c20ddb6+`.

**Source:** `/tmp/chatgpt-pro-r15-reaudit.txt` (HAS_P2PLUS @ `0f35d5e6`; nest P2 already fixed in `c20ddb6`).

**Architecture:** Share `_launch_refused_for_cancel` (or move to `state.py` / `modes.py`) and invoke it inside `_launch_grok`'s transition_guard **before** `prepare_leader_spawn` + Popen, matching autopilot.

---

### Task R16-1: Cancel/terminal recheck inside `_launch_grok` guard

**Files:** `omg_cli/modes.py`, optionally hoist helper from `omg_cli/autopilot.py` to `omg_cli/state.py` or shared module; tests in `tests/test_modes.py`.

**Behavior under transition_guard (or already-held guard):**
1. Refuse if status terminal (`cancelled`/`verified`/`completed`/`failed`) or pending `cancel.request.json`
2. `prepare_leader_spawn`
3. dry_run / Popen / pid publish

**Tests:**
- status cancelled → `_launch_grok` refuses, no Popen
- pending cancel.request.json → refuse, no Popen
- happy path still spawns when running + no cancel

**Commit:** `fix(modes): refuse launch when run cancelled or cancel pending`

### Task R16-2: Docs + push + CI + Pro; merge only if 無 P2+

Note nest fix already in c20ddb6. Re-audit at new HEAD with full SHA.
---
