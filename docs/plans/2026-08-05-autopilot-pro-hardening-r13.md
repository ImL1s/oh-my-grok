# Autopilot Hardening Round 13 (launch/cancel linearization) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close Pro R11-P1 residual: linearize cancel-check + Popen + pid publish under a short transition_guard so cancel cannot miss a newly spawned Grok; kill child if pid publish fails.

**Baseline:** HEAD `75f05fa` (R12 driver flock + accept gate).

---

### Task R13-1: Split spawn vs wait in modes._launch_grok

**Files:** `omg_cli/modes.py`

Extract `_spawn_grok_process(argv, cwd, run_dir) -> Popen` that materializes prompt, writes last_argv, Popen, writes pid metadata; on pid-write failure kill the child and raise.

`_launch_grok` calls spawn then wait (preserve API).

### Task R13-2: Autopilot spawn under transition_guard

**Files:** `omg_cli/autopilot.py`

After building argv, under `transition_guard`:
1. refuse if terminal/cancel
2. refuse if live leader pid still alive (optional if hard)
3. `_spawn_grok_process`
4. exit guard
5. wait on the process (timeout handling as today)

**Test:** monkeypatch cancel landing between refuse-check and spawn inside guard path — prefer unit test that spawn path re-checks under guard.

### Task R13-3: Docs + push + Pro/Codex; merge only if 無 P2+

---
