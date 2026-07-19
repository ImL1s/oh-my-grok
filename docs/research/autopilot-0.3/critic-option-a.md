# Critic Verdict — Option A spawn fail-closed

**date_utc:** 2026-07-20  
**Scope:** Ship slice as isolation **defense-in-depth** (not hard sandbox)  
**Reviewer:** Critic (read-only investigation of `omg_cli/deny.py`, `hooks/hooks.json`, `tests/test_deny.py`, install path, doctor, security-model, P0 spec)

---

**VERDICT: REQUEST_CHANGES**

**Overall Assessment**: Core decision logic (`decide_spawn_subagent`) is sound, unit-tested, and honestly documented as fail-open-on-hook-crash. It is **not shippable as isolation defense-in-depth** yet: the **documented effective install path** (`scripts/install-plugin.sh` → `~/.grok/hooks/omg-pretool-deny.json`) still matches only shell tools, so the new policy never runs in production soft-gate conditions. Spec also required doctor to note the spawn gate; that is missing.

**Pre-commitment Predictions** (before deep dive) vs actual:

| Predicted risk | Found? |
|----------------|--------|
| Matcher updated in plugin but not in global install path | **YES — CRITICAL** (`install-plugin.sh` matcher stale) |
| Role table incomplete / false-positive substrings | Partial (substring heuristics OK for P0; nested spawn not gated) |
| Tests cover happy path only | Partial — matrix present for main cases; doctor/install not tested |
| Docs overclaim “hard” | No — security-model stays soft-gate honest |
| Exception path fail-opens | Yes, intentional; residual already known |

---

## Critical Findings (blocks execution / ship)

### 1. Global soft-gate installer still omits `spawn_subagent|Task`

**Evidence:**

```34:34:scripts/install-plugin.sh
        "matcher": "run_terminal_command|Bash|Shell",
```

vs plugin bundle:

```38:38:hooks/hooks.json
        "matcher": "run_terminal_command|Bash|Shell|spawn_subagent|Task",
```

**Why this matters:** `docs/security-model.md` (Global PreToolUse install) and `install-plugin.sh` comments state live 2026-07-19 evidence: **plugin-bundled hooks may not fire**; soft-gate effectiveness requires `~/.grok/hooks/omg-pretool-deny.json`. That file is written by install-plugin with the **old** matcher. Result: unit tests green, plugin JSON updated, **live sessions that depend on the global hook never invoke spawn policy**. Re-running install-plugin after a manual fix **regresses** spawn matching.

**Confidence:** HIGH  
**Fix:** Update install-plugin matcher to match `hooks/hooks.json` (`…|spawn_subagent|Task`). Add a unit/contract test that parses the heredoc matcher (or shared constant) and asserts spawn tools are present. Document that operators must re-run `scripts/install-plugin.sh` after upgrade.

---

## Major Findings

### 1. Spec P0 item 4 unmet: doctor does not note spawn gate

**Evidence:** Spec (`docs/research/autopilot-0.3/spec.md`): *“doctor notes spawn gate exists.”*  
`omg_cli/doctor.py`: no spawn / capability_mode / `decide_spawn_subagent` references.  
`check_pre_tool_use()` only verifies PreToolUse key non-empty — does **not** assert matcher includes `spawn_subagent`.  
`check_global_pretool_hook()` only checks deny script path — **not** matcher contents (fixture in `tests/test_doctor.py` even uses shell-only matcher and still passes).

**Why this matters:** Operators get a green doctor while the effective global hook cannot fire Option A. Doctor was the 0.2.5 pattern for soft-gate install awareness; Option A needs the same signal.

**Confidence:** HIGH  
**Fix:**  
- Soft footer or hard check: plugin + global PreToolUse matcher must include `spawn_subagent` (and ideally `Task`).  
- Fail hard (or warn-loud) if global hook matcher is shell-only after Option A.

### 2. No live spawn-deny canary (acknowledged residual — still a ship gate for *claiming* defense-in-depth works on host)

**Evidence:** `scripts/canary_pretool.py` only exercises shell CLI deny for parent/child, not “spawn without `capability_mode` → host honors deny reason”. Architect Option A success criteria required live deny oracle for bad spawn.

**Why this matters:** Unit tests prove policy JSON; they do **not** prove host delivers spawn tool input to PreToolUse or honors exit 2 for spawn. Without canary, shipping is “code ready,” not “isolation defense-in-depth proven on Grok.”

**Confidence:** HIGH  
**Fix:** Minimal canary: spawn with missing mode; require exact reason substring `oh-my-grok: spawn_subagent requires capability_mode` (or shared constant). Exit non-zero on prose-only denial. May stay `quota-heavy` / optional, but must exist before marketing the layer as active.

---

## Minor Findings

1. **Layer table residual stale** (`docs/security-model.md` L11): still says omitted mode falls back to agent defaults with no pointer that spawn-gate **denies** omit when hook+matcher fire. Section “Spawn fail-closed” is correct; table residual should cross-reference.
2. **Layer 5 description** still only mentions external agent CLI deny — does not mention spawn capability validation.
3. **Matrix gaps in unit tests:** no `plan` / `omg-verifier` wrong-mode cases; no `OMG_ALLOW_UNSAFE_SPAWN=1` allow test; no empty `subagent_type` + mode present.
4. **No nested-spawn deny** — Architect Option A included depth gate; P0 success criteria in `spec.md` did not. Acceptable deferral if explicitly residual (not claimed closed).
5. **`general-purpose` forced RW** blocks legitimate RO explore via that type — intentional per spec; skills must keep explore types separate (already do).

---

## What's Missing

- Effective deploy path: global matcher + reinstall note  
- Doctor signal for spawn gate  
- Live spawn-deny canary  
- Nested spawn / depth enforcement (optional residual)  
- Shared matcher constant between `hooks.json` and `install-plugin.sh` (drift source of CRITICAL #1)  
- Plugin version still `0.2.5` while docs label Option A as 0.3.0 — versioning hygiene only

---

## Ambiguity Risks

- Spec says *“fail-closed on decision when hook runs”* while residual is host fail-open — **correct if marketing stays soft-gate**. Risk: readers of “Option A shipped” assume hard isolation. Keep claim language: defense-in-depth only.
- Unknown `subagent_type` + any valid RO/RW mode → **allow**. Interpretation A: correct (host enforces mode). Interpretation B: should deny unknown types. Current code = A; document it.

---

## Multi-Perspective Notes

- **Executor:** Policy code is implementable and already written; stuck points are install-plugin + doctor, not deny.py.
- **Stakeholder:** Does this close “isolation is convention only”? **Partially in-process, not on the documented live soft-gate path.** Without global matcher + canary, Gap 1 is not closed in production.
- **Skeptic / Security:** Strongest failure: “we added spawn deny, CI green, but sessions never match spawn tools.” That is worse than no feature — creates false confidence. Escape hatch `OMG_ALLOW_UNSAFE_SPAWN=1` is env-only (good); exception path still fail-opens (expected).

---

## Spec compliance matrix (P0)

| Requirement | Status | Notes |
|-------------|--------|-------|
| Matcher includes `spawn_subagent` (+ Task) in plugin `hooks.json` | PASS | Present |
| Deny missing `capability_mode` | PASS | `decide_spawn_subagent` + unit test |
| Deny mode incompatible with role table | PASS | executor/gp/explore + execute denied |
| Role table initial RO/RW | PASS | Matches spec; substring heuristics for critic/verifier/explore/executor |
| Unit tests allow/deny matrix | PASS (thin) | Core criteria covered |
| Doctor notes spawn gate | **FAIL** | No note/check |
| Docs honesty (soft-gate residual) | PASS | security-model Option A section |
| Effective global install matcher | **FAIL** | install-plugin.sh stale |
| pytest green | NOT RE-RUN here | Tests exist; assume author ran; not verified this pass |

---

## Verdict Justification

Mode: **ADVERSARIAL** after CRITICAL install-path gap + 2 MAJOR ship-claim gaps.

Realist check:  
- CRITICAL #1 not theoretical — security-model itself says global hook is required for soft-gate effectiveness. Mitigated by? None if operators use install-plugin (the documented path). **Keep CRITICAL.**  
- Live canary residual was user-noted; still MAJOR for shipping *as isolation defense-in-depth*, not for merging dead code.  
- Host fail-open residual is accepted product honesty — **not** a reject reason if claim language stays soft.

**Upgrade to APPROVE when:**

1. `install-plugin.sh` matcher includes `spawn_subagent|Task` (ideally shared with `hooks.json`)  
2. Doctor notes and preferably asserts spawn matcher on plugin and/or global hook  
3. (Strongly recommended before any isolation claim) dry/live spawn-deny canary stub or live probe  

Core `decide_spawn_subagent` logic does **not** need redesign.

**Open Questions**

- Does current Grok host load plugin-bundled PreToolUse for spawn in any config (so global-only gap is partial)? Live 2026-07-19 said no for hook_execution — re-verify if host changed.  
- Should unknown types be deny-by-default instead of allow-with-mode?  
- Is nested-spawn deny in 0.3.0 P0 or explicit P1 residual?

---

*Ralplan summary row: N/A (implementation slice review, not ralplan plan)*

**Hand-off:** executor — fix install-plugin matcher + doctor check (+ optional canary); re-request Critic.
