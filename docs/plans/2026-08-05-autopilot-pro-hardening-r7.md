# Autopilot Hardening Round 7 (Pro P1 + Codex resume) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close Codex resume-path P2 plus Pro R5 P1-1/P1-2 and high-value P2-1/P2-4 on PR #84.

**Source:** Codex on `3aa8a50`; Pro `/tmp/chatgpt-pro-r5-reaudit.txt` (HEAD was `9e95e6a`; issues still apply).

---

### Task R7-0: Attached interview resume via autopilot transition

**Files:** `omg_cli/interview.py`, tests

`close_interview` for autopilot must not advertise `omg ralplan … --run` alone (phase still `interview`; embedding rejects). Set:

`omg autopilot transition --run <rid> --phase ralplan`

(optionally append `&& omg ralplan <goal> --run <rid>`).

### Task R7-1: Remove cancelled from generic transition (P1-1)

**Files:** `omg_cli/autopilot.py`, CLI if needed, tests

Remove `cancelled` from `MANUAL_TRANSITIONS` / legal next. Cancellation only via `omg cancel` / `cancel_run`. Test that `transition(..., "cancelled")` raises clean AutopilotError and does not mutate phase sidecar before fail.

### Task R7-2: ralplan_epoch for re-entry invalidation (P1-2)

**Files:** `omg_cli/autopilot.py`, tests

Add monotonic `ralplan_epoch` on autopilot state:
- start with interview: epoch 0; skip-interview: epoch 1
- first `interview→ralplan` with epoch 0: set epoch=1, no quality invalidate
- any later entry to ralplan with epoch>=1: invalidate quality (+ consensus if stamp), then epoch += 1

Do **not** gate quality invalidation on stamp existence.

**Test:** break_glass consensus path with no stamp, qa→ralplan → old review/QA invalid.

### Task R7-3: Require goal on consensus stamp (P2-1)

**Files:** `omg_cli/autopilot.py`, tests

`_consensus_ready`: for autopilot runs, require non-empty string `goal` matching frozen run goal (missing/null → False). Update helpers/tests that omit goal.

### Task R7-4: Reject legacy autopilot ralplan embedding (P2-4)

**Files:** `omg_cli/ralplan.py`, tests

If `mode=="autopilot"` and schema is not STRICT_V2, reject. Do not enter `_run_ralplan_v1` for autopilot.

### Task R7-5: Docs + push + Pro/Codex re-audit

---
