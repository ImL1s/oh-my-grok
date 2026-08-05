# Autopilot Hardening Round 4 (Codex P2: replan + interview CLI) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close the two Codex P2s on PR #84 HEAD `46ff833`: (1) invalidate stale `ralplan.json` accepted stamps on replan; (2) provide a real CLI path that writes interview envelopes under an autopilot `run_id`.

**Baseline:** `fix/autopilot-pro-hardening-r1` @ `46ff833`.

**Architecture:** Mirror implementation-receipt invalidation for ralplan stamps. For interview, allow `omg interview` to attach to an existing autopilot run in phase `interview` (same run_id) instead of always `create_run(mode=interview)`.

**Tech Stack:** Python 3.11+, pytest, `omg_cli/autopilot.py`, `omg_cli/ralplan.py`, `omg_cli/interview.py`, `omg_cli/commands/workflow.py`.

---

### Task R4-1: Invalidate stale ralplan stamps on replan

**Files:**
- Modify: `omg_cli/ralplan.py` (add `invalidate_ralplan_consensus`)
- Modify: `omg_cli/autopilot.py` (call on review/qa → ralplan; `_consensus_ready` rejects invalidated)
- Test: `tests/test_autopilot.py`

**Step 1: Failing test**

```python
def test_replan_invalidates_accepted_ralplan_stamp(tmp_path):
    # start skip_interview → stamp accepted ralplan → implement → review (break_glass/work) → ralplan
    # after replan, _consensus_ready False; ralplan→implement raises without new stamp
```

Use existing helpers/patterns in `tests/test_autopilot.py` for phase walking.

**Step 2:** Run test → FAIL (old stamp still accepted)

**Step 3: Implement**

- `invalidate_ralplan_consensus(root, run_id, *, reason)`: if `ralplan.json` exists, CLI-write `accepted=False`, `invalidated=True`, `invalidated_reason`, `invalidated_at`; clear `status.ralplan_consensus` if present.
- On `next_phase == "ralplan" and src in {"review", "qa"}` (and any other replan entry that bumps `cycles.ralplan`), call invalidate.
- `_consensus_ready`: return False if `invalidated is True` even if somehow accepted.

**Step 4:** Tests pass

**Step 5:** Commit `fix(autopilot): invalidate ralplan stamp on replan`

---

### Task R4-2: CLI path for autopilot interview envelopes

**Files:**
- Modify: `omg_cli/interview.py` (`_load`/`_validate`/`start_interview` attach path)
- Modify: `omg_cli/commands/workflow.py` (`--attach-run`)
- Test: `tests/test_interview.py` and/or `tests/test_autopilot.py`
- Docs: `docs/autopilot.md` (one line on `omg interview start --attach-run`)

**Step 1: Failing test**

```python
def test_interview_attach_run_writes_envelope_for_autopilot(tmp_path):
    st = start_autopilot(tmp_path, "attach interview", skip_interview=False)
    rid = st["run_id"]
    # start_interview(..., attach_run_id=rid) must succeed (not wrong mode)
    # close path or write complete envelope via real CLI helpers
    # _interview_complete(tmp_path, rid) is True
```

Also assert bare `omg interview start` without attach still creates mode=interview run.

**Step 2:** FAIL on wrong mode

**Step 3: Implement**

- Allow interview ops when `run.mode == "interview"` OR (`run.mode == "autopilot"` and autopilot phase == `"interview"`).
- `start_interview(..., attach_run_id=None)`: if attach_run_id set, load that run, require autopilot+interview phase, seed `interview.json` under that run_id (no `create_run`).
- CLI: `omg interview start --attach-run RUN_ID` (task from run goal if omitted).
- Keep fail-closed: cannot attach to implement/review/qa phases; cannot attach to non-autopilot modes except interview.

**Step 4:** Tests pass

**Step 5:** Commit `fix(interview): attach interview CLI to autopilot run_id`

---

### Task R4-3: Docs + CHANGELOG + push + re-review

Update CHANGELOG Unreleased; docs/autopilot.md replan + attach-run; commit plan file; push; `@codex review`; ask-gpt-pro-github on new HEAD. Do not merge until CI green and Pro says 「無 P2 以上問題」.

---
