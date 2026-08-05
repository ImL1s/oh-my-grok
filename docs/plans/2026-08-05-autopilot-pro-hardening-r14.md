# Autopilot Hardening Round 14 (Pro R13 P1/P2) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close the five remaining Pro R13 findings on PR #84 HEAD `98671be` until Pro reports 「無 P2 以上問題」.

**Source:** `/tmp/chatgpt-pro-r13-reaudit.txt` (HAS_P2PLUS @ `98671be`).

**Architecture:** Replace env-based `force` verified with a process-private capability token; bind implementation receipts to a live `ExecutionLease`; refuse spawn when a live leader PID still matches `pid.json`; require non-empty process starttime for PID publication. For mutable-status authority, add fail-closed re-validation under lease that rejects hand-edited autopilot sidecars lacking matching run_id/writer/generation markers used by CLI transitions (pragmatic bound without full OS sandbox).

**Tech Stack:** Python 3.11+, pytest, existing `omg_cli/state.py` / `implementation.py` / `modes.py` / `autopilot.py`.

---

### Task R14-1: Replace env force-verified with process-private capability

**Files:**
- Modify: `omg_cli/state.py` (`set_verified`, `FORCE_VERIFIED_ENV`)
- Modify: tests that set `OMG_INTERNAL_FORCE_VERIFIED=1`
- Test: `tests/test_state.py` / `tests/test_autopilot.py`

**Step 1: Failing test**

```python
def test_set_verified_force_ignores_env_var(tmp_path, monkeypatch):
    # With only env OMG_INTERNAL_FORCE_VERIFIED=1 and force=True, must refuse
    # unless capability token was issued in-process via enable_force_verified_for_tests().
    monkeypatch.setenv("OMG_INTERNAL_FORCE_VERIFIED", "1")
    ...
    with pytest.raises(PermissionError, match="force"):
        set_verified(tmp_path, rid, force=True)
```

**Step 2:** Run → FAIL (currently accepts env)

**Step 3: Implement**

- Add module-private `_FORCE_VERIFIED_CAPABILITY: object | None = None` and `enable_force_verified_for_tests()` that sets a unique token object (tests only).
- `set_verified(force=True)` requires `force is True` AND `_FORCE_VERIFIED_CAPABILITY is not None` (same object identity check after enable). **Ignore env var entirely** (or treat env alone as insufficient).
- Keep `FORCE_VERIFIED_ENV` deprecated/no-op with a comment for docs honesty.
- Update all unit tests that used the env var to call `enable_force_verified_for_tests()` in a fixture/teardown.

**Step 4:** Tests pass

**Step 5:** Commit `fix(state): replace env force-verified with process capability`

---

### Task R14-2: Bind implementation receipts to live ExecutionLease

**Files:**
- Modify: `omg_cli/implementation.py`
- Modify: call sites / tests
- Test: `tests/test_autopilot.py`

**Step 1: Failing test** — hand-forged receipt with fake `invocation_id` must NOT unlock implement→review; stamp without live lease must raise.

**Step 2:** FAIL

**Step 3: Implement**

- `stamp_implementation_receipt(..., lease: ExecutionLease)` required; call `lease.assert_current()`; record `invocation_id=lease.invocation_id`, `lease_generation=lease.generation` (or whatever field exists).
- `read_implementation_receipt` requires matching fields present.
- Gate `_implementation_work_evidence`: when trusting on-disk receipt, re-load current lease metadata for the run (or require receipt generation matches last known CLI implement-cycle generation stored on autopilot state). Prefer: store `implement_lease_generation` / `implement_invocation_id` on autopilot when entering implement; receipt must match those + fingerprint.
- On implement entry (already invalidates receipt), also stamp expected generation/invocation onto autopilot state when transition enters implement under lease.

**Step 4:** PASS

**Step 5:** Commit `fix(implementation): bind receipts to live execution lease`

---

### Task R14-3: Refuse spawn when live leader PID still alive

**Files:**
- Modify: `omg_cli/autopilot.py` (spawn path under transition_guard)
- Modify: `omg_cli/modes.py` or helpers reading `pid.json`
- Test: `tests/test_autopilot.py`

**Step 1: Failing test** — write pid.json with live PID+starttime; resume spawn must refuse without Popen.

**Step 2:** FAIL

**Step 3: Implement**

- Before `_spawn_grok_process` under guard: load `pid.json`; if PID alive AND starttime matches, refuse with clear error (do not overwrite).
- If PID dead or starttime mismatch (reused PID), clear stale metadata then allow spawn.

**Step 4:** PASS

**Step 5:** Commit `fix(autopilot): refuse spawn when live leader pid matches`

---

### Task R14-4: Require starttime for PID publication

**Files:**
- Modify: `omg_cli/state.py` `write_pid_metadata`
- Modify: `omg_cli/modes.py` `_spawn_grok_process` (already kills on raise)
- Test: `tests/test_state.py` / `tests/test_modes.py`

**Step 1: Failing test** — monkeypatch `process_starttime` → None; `write_pid_metadata` raises; spawn kills child.

**Step 2:** FAIL

**Step 3: Implement** — if `process_starttime(pid)` is None, raise (do not write pid.json).

**Step 4:** PASS

**Step 5:** Commit `fix(state): require process starttime for pid publication`

---

### Task R14-5: Harden autopilot accept authority under lease (P1-2 pragmatic)

**Files:**
- Modify: `omg_cli/state.py` `set_verified`, `omg_cli/autopilot.py` `complete_with_acceptance` / `load_autopilot`
- Test: `tests/test_autopilot.py`

**Step 1: Failing test** — hand-edit `status.json` mode away from autopilot then `omg accept` still must not verify an autopilot-created run; hand-edit `autopilot.json` phase to `acceptance` without CLI transition markers must fail `set_verified` / `complete_with_acceptance`.

**Step 2:** FAIL

**Step 3: Implement (YAGNI vs full OS sandbox)**

- On `start_autopilot`, record `authority_nonce` (uuid) in both status extra and autopilot.json.
- Every CLI `transition` / `complete_with_acceptance` refreshes nonce under lease.
- `set_verified` for autopilot: require `load_autopilot` phase==acceptance AND matching `authority_nonce` between status and sidecar AND writer==omg-cli AND run_id match. Hand-edited phase without matching nonce fails.
- `cmd_accept`: if run has autopilot sidecar present (file exists) OR status.mode was ever autopilot via sidecar, refuse bare accept (prefer: if `autopilot.json` exists for run_id, always refuse bare accept regardless of status.mode).

**Step 4:** PASS

**Step 5:** Commit `fix(autopilot): authority nonce binds accept/verified path`

---

### Task R14-6: Docs + CHANGELOG + push + CI + Codex + Pro; merge only if 無 P2+

Update `docs/autopilot.md`, `docs/security-model.md`, CHANGELOG; push; `@codex review`; ask-gpt-pro-github. Repeat rounds if P2+ remain.

---
