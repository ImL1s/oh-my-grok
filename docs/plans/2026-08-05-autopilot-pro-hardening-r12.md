# Autopilot Hardening Round 12 (Pro R10 P1s + Codex receipt) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close Pro R10 P1 (omg accept bypasses autopilot FSM) and Codex P2 (forgeable implementation receipt); add outer-driver flock so dual resume cannot double-launch.

**Baseline:** HEAD `3d62104` (R11).

---

### Task R12-1: Autopilot mode refuse bare omg accept → verified

**Files:** `omg_cli/commands/team.py` `cmd_accept`, `omg_cli/state.py` `set_verified`, tests

- `cmd_accept`: if `run.mode == "autopilot"`, refuse (exit non-zero) directing user to `omg autopilot complete` / `complete_with_acceptance`. Do not call `set_verified`.
- `set_verified`: if mode==autopilot (and not force), require autopilot sidecar phase == `"acceptance"` (or status autopilot_phase already acceptance after complete path). Fail-closed otherwise.

**Test:** start autopilot skip_interview, `cmd_accept` / set_verified without phase acceptance → refused; status not verified.

### Task R12-2: Authenticate implementation receipts with lease invocation_id

**Files:** `omg_cli/implementation.py`, callers of `stamp_implementation_receipt`, tests

- `stamp_implementation_receipt` requires `invocation_id` (from execution lease); write it on the record.
- `read_implementation_receipt` requires non-empty `invocation_id` string; missing → None (untrusted).
- Wire stamp call sites to pass lease.invocation_id.

**Test:** hand-written writer=omg-cli receipt without invocation_id rejected by gate.

### Task R12-3: Autopilot driver flock for run_autopilot

**Files:** `omg_cli/autopilot.py`

Acquire exclusive flock on `.omg/state/runs/<rid>/autopilot.driver.lock` for the whole `run_autopilot` invocation. Second concurrent resume returns non-zero "already running" without launching.

**Test:** mock flock held → second resume refuses.

### Task R12-4: Docs + push + Pro/Codex; merge only if 無 P2+

---
