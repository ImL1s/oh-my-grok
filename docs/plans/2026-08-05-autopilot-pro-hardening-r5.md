# Autopilot Hardening Round 5 (Pro P2 after R4) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close the four P2s from GPT Pro + Codex on PR #84 HEAD `a1eb21c` so Pro reports 「無 P2 以上問題」.

**Source:** `/tmp/chatgpt-pro-r4-reaudit.txt` + Codex「Clear invalidation when ralplan accepts again」.

**Architecture:** Treat any on-disk CLI `ralplan.json` as a prior consensus epoch: re-entering `ralplan` invalidates it regardless of immediate `src`. Fresh accept must clear invalidation. Attach-run and embedded ralplan must re-verify under lease and bind frozen autopilot goal/phase.

---

### Task R5-1: Clear invalidation on fresh accept + reset cycle start

**Files:** `omg_cli/ralplan.py`, tests

When writing `accepted=True` (legacy + strict-v2 paths), clear `invalidated` / `invalidated_reason` / `invalidated_at`.

Optionally when resuming after `invalidated=True` and `accepted=False`, reset history/round for a fresh cycle (or at least clear invalidation at the start of a new consensus attempt under lease once Critic path begins — prefer clear-on-accept + clear-on-cycle-start when `invalidated` was True).

**Test:** invalidate stamp → run accept path (or simulate accept write) → `_consensus_ready` True.

### Task R5-2: Invalidate on any ralplan re-entry with prior stamp

**Files:** `omg_cli/autopilot.py`, tests

Replace `src not in {"interview","init"}` with: if `next_phase == "ralplan"` and a CLI-owned `ralplan.json` already exists for this run_id, bump `cycles.ralplan`, `invalidate_ralplan_consensus`, `invalidate_quality_stages`.

First `interview → ralplan` with no stamp: no-op.

**Test:** `review → blocked → interview → ralplan` with prior accepted stamp → stamp invalidated; implement blocked.

### Task R5-3: Re-authorize attach-run under lease

**Files:** `omg_cli/interview.py`, tests

After acquiring `execution_lease` in `start_interview` (attach path), re-load run + autopilot, re-check mode/phase/non-terminal/goal match before any write.

**Test:** (unit) simulate phase change between attach check and lease by calling internal helpers, or document with a focused unit that `_attach` authorize is re-invoked under lease.

### Task R5-4: Bind embedded ralplan to frozen autopilot goal + phase

**Files:** `omg_cli/ralplan.py` (and/or `commands/modes.py`), tests

When target run `mode == "autopilot"`:
- Require autopilot phase == `ralplan` (under lease if possible)
- Goal must equal frozen run/status goal; reject mismatched CLI goal
- `_consensus_ready` optionally also checks `goal` matches current run goal (defense in depth)

**Test:** mismatched goal rejected; wrong phase rejected.

### Task R5-5: Docs + CHANGELOG + push + Pro/Codex re-audit

Do not merge until clean.

---
