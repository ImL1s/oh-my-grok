# Autopilot Hardening Round 2 (remaining Pro P2+) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close remaining P1/P2 from GPT Pro re-audit + Codex after Round 1 PR #84, until Pro reports 「無 P2 以上問題」.

**Baseline:** `fix/autopilot-pro-hardening-r1` after `12a2bc9` (implement fingerprint + receipt cycle bind + cache exclude). Prior Pro audit targeted stale `ae9684c` — re-audit after this round.

**Architecture:** Destination gates must not depend only on `src` phase; review/QA freshness must bind to current workspace; interview/consensus trusted paths must verify real CLI stamp envelopes.

**Out of scope:** Full team plane split; live tmux CI matrix.

---

### Task R2-1: QA freshness uses implement-class fingerprint (close Pro P1-1 for QA)

**Files:** `omg_cli/autopilot.py` `stage_qa_is_clean`; tests

When last clean cycle stores `product_hash` from `qa.product_hash`, ALSO store `workspace_fp` from `_implement_workspace_fingerprint` at QA clean time (or compare both). Prefer: at QA clean write, record `implement_workspace_fp` snapshot; `stage_qa_is_clean` recomputes `_implement_workspace_fingerprint` and mismatches → not clean.

Do not change `qa.product_hash` repair-cycle semantics unless tests require.

### Task R2-2: Review gate binds to current workspace fingerprint

**Files:** `omg_cli/review.py`, `omg_cli/autopilot.py` `stage_review_is_clean`

On `run_structured_review`, record `_implement_workspace_fingerprint(root)` as `workspace_fp` on stamp.
`stage_review_is_clean` requires `workspace_fp` match current recompute (fail-closed if missing on new stamps; legacy without field still fail-closed for strict-v2 autopilot runs — or require field always for schema_version>=2 stamps).

Reject empty `--diff-text` on non-test CLI path if easy; else document + test adapter only.

### Task R2-3: blocked→* re-applies destination gates

**Files:** `omg_cli/autopilot.py` `transition`

When `src == "blocked"`, destination gates for `implement`/`ralplan`/`review`/`qa` must still run (already mostly destination-based — verify and add tests that `blocked→implement` without consensus fails; `blocked→review` without work evidence fails; `blocked→qa` without clean review fails). Fix any src-only shortcuts.

### Task R2-4: Interview trusted path uses CLI envelope

**Files:** `omg_cli/interview.py`, `omg_cli/autopilot.py` `_interview_complete`

`_interview_complete` must require CLI writer + valid complete envelope (not bare status string alone). Align with interview.py stamp rules.

### Task R2-5: Consensus requires accepted ralplan CLI stamp fields

**Files:** `omg_cli/autopilot.py` `_consensus_ready`

Require `writer==omg-cli`, `accepted is True`, and run_id match on `ralplan.json`; do not treat `status.ralplan_consensus` alone as sufficient without stamp (or require both). Prefer stamp as source of truth.

### Task R2-6: Docs + Pro re-audit + Codex + merge

Update CHANGELOG/docs; `@codex review`; ask-gpt-pro-github; merge only if CI green and no P2+.

---
