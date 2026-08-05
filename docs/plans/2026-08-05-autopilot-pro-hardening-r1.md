# Autopilot Hardening Round 1 (Pro+GitHub P2+) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Land the already-started FSM/gate fixes, then close the remaining **P2+** findings from the 2026-08 GPT Pro + @GitHub review of `ImL1s/oh-my-grok@c2e78f5` until a fresh Pro pass reports no P2 or higher.

**Architecture:** Keep CLI single-writer. Prefer immutable CLI stamps over caller booleans. Tighten destination gates and status contracts first; add StageEvidence fingerprint rechecks and a minimal ResumeBundle without rewriting the team plane in Round 1.

**Tech Stack:** Python 3.11+, pytest, existing `omg_cli/autopilot.py` / `resume.py` / `state.py`, GitHub PR + CI.

**Source review:** GPT Pro conversation `https://chatgpt.com/c/6a72104a-f9d8-83ee-bfe8-3acb40b5d56a` (snapshot `c2e78f5`, v0.7.4). Skill for re-audit: `ask-gpt-pro-github`.

**Out of Round 1 (defer Round 2+):** full team plane split (`plane.py`/`scaling.py`), live tmux crash CI matrix, full StageEvidenceEnvelope v3 schema across all stages, cryptographic writer identity.

**Loop until clean:** After merge of Round 1, re-run Pro+@GitHub; if any P2+ remains, write Round 2 plan and repeat.

---

### Task 0: Branch + land in-progress FSM/gate work

**Files:**
- Already modified: `omg_cli/autopilot.py`, `omg_cli/resume.py`, `omg_cli/commands/modes.py`, `tests/test_autopilot.py`, `tests/test_resume.py`, `docs/autopilot.md`, `CHANGELOG.md`

**Step 1: Create branch from main**

```bash
cd /Users/iml1s/Documents/mine/oh-my-grok
git checkout -b fix/autopilot-pro-hardening-r1
```

**Step 2: Verify tests green**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_autopilot.py tests/test_resume.py -m "not live"
```

Expected: PASS (50+)

**Step 3: Commit**

```bash
git add CHANGELOG.md docs/autopilot.md omg_cli/autopilot.py omg_cli/commands/modes.py omg_cli/resume.py tests/test_autopilot.py tests/test_resume.py
git commit -m "$(cat <<'EOF'
fix(autopilot): harden FSM legal_next and stamp-first gates

Split MANUAL vs COMMIT_ONLY transitions so status legal_next never
advertises verified; require CLI stamps or break_glass for interview/
consensus; surface gate_failure on stall; fix ralplan resume hint.
EOF
)"
```

---

### Task 1: Review/QA stamp recheck workspace fingerprint

**Files:**
- Modify: `omg_cli/review.py` (where `diff_hash` is stored)
- Modify: `omg_cli/qa.py` (product hash if present)
- Modify: `omg_cli/autopilot.py` — `stage_review_is_clean` / `stage_qa_is_clean`
- Test: `tests/test_autopilot.py`

**Step 1: Write failing test**

```python
def test_stale_review_stamp_rejected_when_diff_hash_drifts(tmp_path: Path) -> None:
    st = start_autopilot(tmp_path, "stale review", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    transition(tmp_path, rid, "review")
    _stamp_review_clean(tmp_path, rid, diff="original diff")
    # Mutate stamp's recorded hash OR workspace so recompute mismatches
    from omg_cli.review import review_state_path
    import json
    path = review_state_path(tmp_path, rid)
    data = json.loads(path.read_text())
    data["diff_hash"] = "0" * 64  # force mismatch vs recomputed
    path.write_text(json.dumps(data, indent=2) + "\n")
    with pytest.raises(AutopilotError, match="stale|fingerprint|diff_hash"):
        transition(tmp_path, rid, "qa")
```

**Step 2: Run test — expect FAIL**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_autopilot.py::test_stale_review_stamp_rejected_when_diff_hash_drifts -v
```

**Step 3: Minimal implementation**

In `stage_review_is_clean` / `stage_qa_is_clean` (or helpers):
- If stamp has `diff_hash` / product hash fields, recompute from current inputs the same way review/qa writers do.
- Mismatch → treat as not clean (and optionally set `invalidated` reason).
- If hash fields absent (legacy stamps), keep prior behavior but do **not** weaken existing writer/run_id/clean checks.

**Step 4: Tests PASS + commit**

```bash
git commit -m "fix(autopilot): reject review/QA stamps with drifted hashes"
```

---

### Task 2: implement→review requires change or explicit no_change_reason

**Files:**
- Modify: `omg_cli/autopilot.py` — `transition` when `next_phase == "review"` from `implement`
- Modify: `_try_advance_after_launch` implement branch
- Test: `tests/test_autopilot.py`

**Step 1: Failing test**

```python
def test_implement_to_review_requires_evidence_of_work(tmp_path: Path) -> None:
    st = start_autopilot(tmp_path, "impl gate", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence=_ev_consensus_bg())
    with pytest.raises(AutopilotError, match="implementation|no_change"):
        transition(tmp_path, rid, "review")  # no receipt / no_change_reason
    transition(
        tmp_path,
        rid,
        "review",
        evidence={"no_change_reason": "dry-run scaffold only", "break_glass": True},
    )
    assert status_autopilot(tmp_path, rid)["phase"] == "review"
```

**Step 2: Implement**

Accept any of:
- `evidence.implementation_receipt` dict with `writer == omg-cli` (or path to CLI stamp under stages/)
- workspace fingerprint changed vs value recorded when entering implement (store `implement_workspace_fp` on autopilot.json at implement entry)
- `evidence.no_change_reason` + `break_glass=true` (audited)

Outer `_try_advance_after_launch` for implement: if fingerprint unchanged and no receipt, record `gate_failure` and stay on implement (do not silent-advance).

**Step 3: Commit**

```bash
git commit -m "fix(autopilot): gate implement→review on work or break-glass no_change"
```

---

### Task 3: Autopilot sidecar write via atomic helper (mini-WAL)

**Files:**
- Modify: `omg_cli/autopilot.py` — `_save`, `invalidate_quality_stages`
- Optionally small helper in `omg_cli/state.py` if atomic replace already exists
- Test: `tests/test_autopilot.py`

**Step 1: Failing test** — crash mid-invalidate leaves consistent recoverable state OR uses temp+replace

Prefer: replace bare `path.write_text` in `_save` and `invalidate_quality_stages` with temp file + `os.replace` + fsync pattern already used elsewhere in `state.py`.

**Step 2: Implement minimal atomic write helper local to autopilot if none reusable**

**Step 3: Commit**

```bash
git commit -m "fix(autopilot): atomic write for autopilot.json and stage invalidation"
```

Note: Full cross-file transaction WAL is Round 2 if still P2 after Pro re-audit.

---

### Task 4: Minimal ResumeBundle fields on `omg resume`

**Files:**
- Modify: `omg_cli/resume.py` — `build_resume_pack`
- Test: `tests/test_resume.py`

**Step 1: Failing test**

```python
def test_resume_pack_includes_bundle_keys(tmp_path):
    run = create_run(tmp_path, mode="autopilot", goal="bundle")
    rid = run["run_id"]
    write_status(tmp_path, rid, "running", extra={"stage": "autopilot", "autopilot_phase": "review"})
    pack = build_resume_pack(tmp_path, rid)
    assert pack["ok"] is True
    bundle = pack["resume_bundle"]
    assert "run_view" in bundle
    assert "gate_failure" in bundle  # may be null
    assert "provenance" in bundle
```

**Step 2: Implement**

Add `resume_bundle` object with:
- `run_view` (existing status fields)
- `gate_failure` from status if present
- `autopilot_phase` / `legal_next` via `status_autopilot` when mode=autopilot (best-effort)
- `provenance`: `{generated_at, run_id, selector}`
- Do **not** yet pull wiki/memory/compaction (Round 2) — document as partial bundle schema_version=1

**Step 3: Commit**

```bash
git commit -m "feat(resume): add resume_bundle schema_version=1 skeleton"
```

---

### Task 5: Tracker process-start identity fail-closed

**Files:**
- Modify: `omg_cli/tracker.py` — `_default_process_identity_matches` (or equivalent)
- Test: existing tracker tests or new `tests/test_tracker.py` case

**Step 1: Failing test** — when `process_starttime` unavailable, match must be False / unknown, not True

**Step 2: Implement fail-closed**

**Step 3: Commit**

```bash
git commit -m "fix(tracker): fail-closed when process starttime unavailable"
```

---

### Task 6: Harden `set_verified(..., force=True)`

**Files:**
- Modify: `omg_cli/state.py` — `set_verified`
- Test: `tests/test_state.py` / acceptance tests

**Step 1:** Ensure `force=True` is not reachable from any CLI argparse path; rename to `_force_internal` or require env `OMG_INTERNAL_FORCE_VERIFIED=1` in same process only for tests.

**Step 2: Commit**

```bash
git commit -m "fix(state): confine set_verified force escape hatch"
```

---

### Task 7: Docs + CHANGELOG for Round 1

**Files:**
- Modify: `docs/autopilot.md`, `CHANGELOG.md`, optionally `docs/security-model.md` one paragraph on break_glass

**Step 1:** Document stamp-first gates, resume_bundle v1, fingerprint recheck honesty limits

**Step 2: Commit**

```bash
git commit -m "docs: autopilot Round 1 hardening notes"
```

---

### Task 8: Open PR, CI, AI review, GPT Pro re-audit

**Step 1: Push + PR**

```bash
git push -u origin HEAD
gh pr create --title "fix(autopilot): Pro-review Round 1 hardening (P2+)" --body "$(cat <<'EOF'
## Summary
- Land FSM legal_next / commit-only verified contract
- Stamp-first interview/consensus (+ break_glass audit)
- Review/QA hash recheck, implement→review work gate
- Atomic autopilot writes, resume_bundle v1, tracker fail-closed, force-verified confine

## Source
GPT Pro + @GitHub audit of ImL1s/oh-my-grok@c2e78f5

## Test plan
- [ ] `pytest -q tests/test_autopilot.py tests/test_resume.py tests/test_state.py -m "not live"`
- [ ] CI green
- [ ] Bugbot / PR AI review: no P2+
- [ ] Re-run ask-gpt-pro-github on this PR branch: no P2+
EOF
)"
```

**Step 2: Wait for CI + PR AI (Bugbot/review)**

Fix any P2+ from bots before merge.

**Step 3: GPT Pro re-audit via `ask-gpt-pro-github`**

Prompt: review PR branch / latest commit; list only P1/P2 issues; say NONE if clean.

**Step 4: Merge only if CI green AND no P2+ from AI review AND Pro says no P2+**

**Step 5: If Pro still has P2+** → write `docs/plans/2026-08-05-autopilot-pro-hardening-r2.md` and repeat SDD loop.

---

## Task dependency graph

```text
T0 land → T1 fingerprint → T2 implement gate → T3 atomic write
                ↘ T4 resume_bundle
                ↘ T5 tracker
                ↘ T6 force verified
         → T7 docs → T8 PR/CI/Pro → merge or R2
```

T1–T6 may be sequential in one subagent-driven session (review after each).
