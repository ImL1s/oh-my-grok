# Autopilot Hardening Round 10 (R8 leftover P2) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close Pro R8 residual P2s after Round 9: consensus stamp must be full strict-v2 (P2-R8-D); interview writers all check cancel (P2-R8-C); ralplan re-authorize before each sidecar save (P2-R8-B partial).

**Baseline:** HEAD `432c567`.

---

### Task R10-1: Require strict-v2 schema on consensus stamp (P2-R8-D)

**Files:** `omg_cli/autopilot.py` `_consensus_ready`, tests

Require `schema_version == 2` and `lifecycle_version == 2` on ralplan.json for autopilot consensus (fail-closed if missing). Update stamp helpers in tests.

### Task R10-2: Interview answer/pressure-pass/start assert writable (P2-R8-C)

**Files:** `omg_cli/interview.py`

Under lease, before `_save` in `answer_interview`, `pressure_pass_interview`, and non-attach `start_interview` path, call `_assert_run_writable` (reload run first).

### Task R10-3: Ralplan re-authorize before each save under lease (P2-R8-B)

**Files:** `omg_cli/ralplan.py`

Helper `_assert_autopilot_still_writable(root, run_id, goal)` called before `save_ralplan_state` and before accept write (in addition to existing checks). Fail closed if terminal/cancel.

### Task R10-4: Docs + push + Pro/Codex; merge only if 無 P2+

---
