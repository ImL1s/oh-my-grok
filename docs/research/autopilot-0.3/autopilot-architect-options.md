# oh-my-grok 0.3.0 — Architect product options (isolation / ULW / pipeline)

**Date:** 2026-07-20  
**Baseline:** plugin **0.2.5** (live-gates suite green; dual-review as process gate **waived** for this planning pass)  
**Author role:** Architect (code-backed options; no implementation)  
**Audience:** solo maintainer with **generous Grok quota**

---

## Summary

After 0.2.5 live-gates, the product is **functionally honest** but not yet a **default open-box autopilot**: isolation is still mostly skill/prompt convention + host `capability_mode` when the model cooperates; ULW “parallel” is skill-driven `spawn_subagent` (true concurrency is host/model behavior, not a CLI-enforced product path); pipeline is a solid CLI FSM shell that still defaults implement to sequential **ralph** and sequential dual-review.

Three ranked 0.3.0 strategies (simple → complex):

| Rank | Option | One-line |
|------|--------|----------|
| 1 (smallest) | **A** — Spawn fail-closed validation only | Turn spawn isolation from convention into a **hard gate** on the spawn tool path |
| 2 | **B** — ULW default parallel path | Make true parallel ULW the **default product path** for multi-slice work (A as P0) |
| 3 (largest) | **C** — Full isolation + pipeline productization | Ship open-box autopilot: hard isolation + default parallel implement + resumeable open-box pipeline UX |

**Default recommendation for solo + generous Grok quota: Option B** (with Option A as a **non-negotiable first ship slice**, not a separate delayed release).

---

## Baseline diagnosis (0.2.5 — what the code actually is)

### Gap 1 — Isolation is convention, not a hard gate

Security model layers (`docs/security-model.md`):

| Layer | Hardness (product claim) | Code locus |
|-------|--------------------------|------------|
| `capability_mode` on `spawn_subagent` | **Hard-ish (host)** when set | Skills + agent frontmatter; host tool filter |
| Agent `disallowedTools` | Hard when honored | `agents/omg-executor.md` bans shell + spawn |
| PreToolUse deny | **Soft / fail-open** | `hooks/bin/pre_tool_use_deny.py` → `omg_cli.deny` |
| Prompt HARD RULES | **Convention only** | `omg_cli/modes.py` `HARD_RULES_REMINDER`; skills |

Evidence:

- Primary contract is documented as workers without shell via `capability_mode`, depth=1, CLI-owned `verified` (`docs/security-model.md` §Primary product contract).
- PreToolUse remains fail-open on timeout/crash/missing binary (`docs/security-model.md` layer 5; `docs/research/subagent-pretooluse-spike.md`).
- Live 0.2.5 **L-CAP-SPAWN** proved implementer without shell tool when spawned correctly (`docs/research/live-gates-2026-07-20-suite.md`), **not** that omitted/wrong mode is refused by omg.
- `omg_cli/deny.py` has **no** `spawn_subagent` / `capability_mode` validation path (grep: zero matches). PreToolUse only soft-denies external agent CLIs on shell tools.
- Leader still has shell by design (R1); isolation proof is on **spawned implementers**, not the leader (`live-gates-2026-07-20-suite.md` residual notes).

**Root cause:** Isolation depends on the model **choosing** the right spawn args + agent type. There is no omg-owned **fail-closed** gate that denies a spawn missing `capability_mode` / wrong mode for role / nested spawn attempt.

### Gap 2 — ULW true parallel is not the default product path

- Default ULW path: one `grok -p` leader + skill body telling it to emit multiple `spawn_subagent` (`skills/omg-ultrawork/SKILL.md`; `omg_cli/fanout.py` module docstring).
- Process multi-PID fanout exists but is **experimental opt-in** (`OMG_EXPERIMENTAL_PROCESS_FANOUT=1`) and explicitly **not** the isolation story (`omg_cli/main.py` ~97–106; `omg_cli/fanout.py` header).
- Pipeline default implement is **`ralph`** (sequential stories), not `ulw` (`omg_cli/pipeline.py` `run_pipeline(..., implement: str = "ralph")`; `skills/omg-pipeline/SKILL.md` table).
- Worker prepare/seal + integrate envelopes close the **no-shell commit** loop (`omg_cli/workers.py`, `omg_cli/integrate.py`) but do not force multi-spawn.

**Root cause:** Parallelism is a **skill aspiration** inside a single leader process. The product does not default pipeline/autopilot to ULW, does not auto-decompose into sealed tasks, and does not promote a supervised parallel path as first-class UX.

### Gap 3 — Pipeline FSM is a shell, not open-box autopilot

What already works well (0.2.5):

- Stage order: `plan → implement → integrate → dual_review → accept` + `report.json` (`omg_cli/pipeline.py` `STAGE_ORDER`, `write_pipeline_report`).
- Resume + stale-envelope re-integrate (`_integrate_stale`, AC4 comments).
- Never sets `OMG_ALLOW_EXTERNAL_CLI` (`run_pipeline` docstring / `_assert_no_allow_env`).
- Dual-review is sequential headless interim (`omg_cli/dual_review.py`); native spawn dual-review not shipped.

What is **not** open-box autopilot:

- Operator must still understand PRD acceptance, envelopes, dual-review verdict vs `verified`, and when to pass `--implement ulw`.
- Implement default **ralph** biases toward sequential one-story loops.
- Dual-review is not native parallel spawn; process gate for dual-review **waived** for this options doc (do not block 0.3.0 design on dual-review nativeization).
- No single “open the box” UX that: decomposes → prepares worktrees → parallel spawns with enforced capability → seals → integrates → accept → report with progressive disclosure for failures.

---

## Option A — Minimal: spawn fail-closed validation only

### Intent

Close Gap 1 only. Make **incorrect or missing isolation on spawn** a **hard deny** (product-controlled), without changing default ULW/pipeline product path.

### Scope (0.3.0 ship box)

1. **PreToolUse (or host-equivalent) spawn policy** when tool is `spawn_subagent` / Task-like:
   - **Fail-closed defaults** if payload lacks required isolation fields (missing `capability_mode` → deny, not allow).
   - Role → mode table (product-owned):
     - implementer types (`omg-executor`, write-ish `general-purpose` when labeled implement): require `capability_mode=read-write` (no Execute).
     - critic / verifier / explore / plan: require `read-only` (or plan permission mode).
   - **Depth / nested spawn:** if event exposes parent is already a subagent (or depth marker), deny further spawn (align with executor `disallowedTools` + HARD RULES).
2. **Structured deny reasons** stable enough for canary (`oh-my-grok: spawn missing capability_mode`, etc.).
3. **`omg doctor` hard check** that spawn-gate hook is installed (mirror global PreToolUse soft-gate pattern from 0.2.5).
4. **Live canary** extension: spawn without mode → DENIED; spawn with wrong mode for critic → DENIED; good spawn still succeeds (L-CAP-SPAWN regression).
5. Docs: update `docs/security-model.md` layer table — spawn validation becomes a **product hard gate** (still host-dependent on hook honor); keep PreToolUse CLI-deny soft residual separate.

**Explicit non-goals for A:** pipeline default changes, process fanout promotion, native dual-review, open-box autopilot UX, auto worktree prepare orchestration.

### Assumptions

- Grok PreToolUse (or equivalent) delivers **spawn tool name + input JSON** to the hook, including `capability_mode` / agent type / subagent depth when present.
- Host continues to honor hook **deny** for spawn the same way it does for shell (proven for shell parent+child in canary; spawn-specific must be re-proven live).
- Fail-closed on **missing fields** is acceptable product UX (stricter than “model forgot mode → full defaults”).
- Leader shell remains allowed (R1 unchanged).

### Complexity

| Dimension | Level |
|-----------|--------|
| Engineering | **Low–medium** (one policy module + hook branch + doctor + canary + tests) |
| Host dependency research | **Medium** (must confirm spawn payload shape on live Grok; may need argv/host flag fallback if fields absent) |
| Maintainer ops | **Low** (install-plugin + doctor; no new modes) |
| Quota burn | **Low** (canary-sized live probes) |

### Risks

| Risk | Mitigation |
|------|------------|
| Host omits `capability_mode` from PreToolUse payload → all spawns denied | Feature-detect payload; if host cannot supply fields, **fail doctor** with “spawn hard-gate unsupported on this grok” rather than silent allow |
| Over-strict role table blocks legitimate `general-purpose` RO explore | Explicit allowlist of types + optional prompt annotation `omg_role=implement\|review` |
| Hook still fail-open on crash | Document residual; doctor + canary; never claim OS sandbox |
| Model uses alternate spawn tool id | Deny-unknown-spawn-tools list; live suite updates after Grok upgrades |

### Success criteria

1. Unit: spawn events without `capability_mode` → **deny**; wrong mode for critic → **deny**; valid implementer RW + critic RO → allow.
2. Live: canary shows host-honored deny reason string for bad spawn (not prose-only).
3. Regression: L-CAP-SPAWN still `DENIED_OR_RAN=denied` for shell on RW implementer; L-CANARY parent+child shell deny still green.
4. Security-model docs no longer call capability_mode “prompt MUST” alone for spawn; cite **spawn-gate**.
5. No change required to pipeline stage order or ULW CLI flags for A to ship.

### Dependencies on Grok host

| Need | Why | Fallback if missing |
|------|-----|---------------------|
| PreToolUse (or gate) on `spawn_subagent` | Enforce before child starts | Abort Option A as hard-gate; keep convention + document “host gap” |
| Spawn tool input includes capability/agent fields | Fail-closed validation | Parent-side wrapper only (weaker); or CLI that only launches sessions with parent `--disallowed-tools` + skill (not true spawn gate) |
| Deny decision honored for spawn | Product hard gate | Soft advisory only — do not ship A as “hard” |
| Subagent depth / parent context in event (ideal) | Nested spawn deny | Rely on child `disallowedTools` + convention for depth |

### Effort estimate (solo)

~2–5 focused days including live canary iteration (not counting host payload reverse if undocumented).

---

## Option B — ULW default parallel path

### Intent

Close Gap 2 as the **product center of gravity** for 0.3.0, while absorbing Option A as **P0** so parallel does not multiply unconstrained shellful workers.

### Scope (0.3.0 ship box)

**P0 (must ship with B):** Option A spawn fail-closed validation.

**P1 — Product path for true parallel ULW:**

1. **Default implement path for multi-slice goals = ULW skill fanout** (still one leader + N `spawn_subagent`), not process fanout.
2. **CLI / pipeline defaults:**
   - `omg pipeline` gains a clear parallel path: either `--implement ulw` becomes recommended default for autopilot marketing, **or** `omg pipeline` keeps ralph default but adds `omg pipeline --autopilot` / `omg ulw --product` that is documented as **the** parallel product entry (pick one UX; prefer single flag over silent default flip if ralph users rely on sequential).
   - Recommendation for solo+quota: **`--implement ulw` as pipeline default when goal is multi-file / user says parallel|ulw|autopilot**; keep ralph for single-story persistence. Minimal concrete change: pipeline **default `implement=ulw`** *or* document dual entrypoints without silent flip — see Trade-offs.
3. **Leader orchestration hardening (still skill + CLI, not tmux):**
   - Prompt pack requires multi-`spawn_subagent` in one turn for ≥2 independent slices (already in skill anti-pattern “serializing independent work”).
   - Inject prepare/seal contract: `omg worker prepare` → child edits worktree → `omg worker seal` → `omg integrate` (already modules; productize in ULW skill + pipeline implement stage checklist).
4. **Optional supervised process fanout remains experimental** — do **not** promote to default in B unless spawn-gate + worktree provision are solid; process workers are full `grok -p` leaders with shell residual (`fanout.py` notes capability is prompt-level).
5. **Live suite:** L-ULW multi-slice evidence that ≥2 worker envelopes (or ≥2 spawn child ids) appear under one run; integrate path green.
6. Dual-review remains optional / waivable (user waived process gate); sequential interim OK for B.

**Explicit non-goals for B:** full open-box autopilot UX polish, native dual-review spawn, promoting process fanout, marketplace, hard OS sandbox claims.

### Assumptions

- Option A lands or host spawn payload is sufficient; otherwise parallel multiplies **convention-only** isolation risk.
- Generous Grok quota makes N concurrent subagents acceptable cost.
- Solo maintainer can support **one** primary parallel story (skill spawn), not two (skill + process) as first-class.
- Independent file ownership remains the correct parallel model (integrate ancestry / changed_files already in 0.2.5).

### Complexity

| Dimension | Level |
|-----------|--------|
| Engineering | **Medium** (A + defaults/prompt productization + live multi-spawn gates + docs/UX) |
| Host dependency | **Medium–high** (true parallel = host concurrent subagents + wait APIs already assumed by skill) |
| Maintainer ops | **Medium** (more live suite flakiness; quota use in CI/manual) |
| Quota burn | **Medium–high** (multi-child per ULW run) |

### Risks

| Risk | Mitigation |
|------|------------|
| Model still serializes spawns | Stronger skill + dry-run fixtures that assert multi-spawn **intent** in prompt; live gate counts child ids; accept residual “host/model may serialize” in claim language |
| Parallel merge conflicts | Keep integrate fail-closed (already); require non-overlapping `changed_files` in skill; leader-only conflict resolution |
| Default flip ralph→ulw breaks single-story users | Prefer explicit autopilot entry **or** heuristic; document ralph for durability loops |
| Process fanout confusion | Keep experimental env gate; README one-liner “not product isolation” |
| Quota spikes | `OMG_MAX_WORKERS` / skill max children guidance (e.g. 2–4); doctor warns |

### Success criteria

1. All Option A success criteria green.
2. Product docs/README: **one** recommended command path for parallel work that is not “hope the model fans out.”
3. Live: one ULW run produces multi-child evidence + integrate (or explicit single-slice skip with reason).
4. Pipeline can drive implement=ulw end-to-end with envelopes → integrate → accept without manual glue beyond PRD.
5. Claim language: “default parallel product path is skill `spawn_subagent` under spawn-gate”; process fanout still experimental.
6. pytest hermetic suite remains green; live `--full` includes ULW multi-slice gate.

### Dependencies on Grok host

| Need | Why | Fallback |
|------|-----|----------|
| All Option A host needs | Isolation under parallel load | Ship A-only; do not market B |
| Concurrent `spawn_subagent` + wait/join tools | True parallel | Document “logical parallel / may schedule serially”; still better product path than ralph for multi-slice |
| Worktree / cwd isolation for children | Safe parallel writes | Leader-mediated prepare paths only (already `omg worker prepare`) |
| Stable child ids in session telemetry (ideal) | Live multi-spawn proof | Envelope count + timestamps as weaker oracle |

### Effort estimate (solo)

~1–2 weeks calendar (A first, then defaults + live multi-slice + docs), assuming no major host API gaps.

---

## Option C — Full isolation + pipeline productization

### Intent

Close Gaps 1–3: **hard isolation**, **default parallel implement**, and **open-box autopilot** as one coherent product (OMC-class “just run pipeline”).

### Scope (0.3.0–0.4.x sized; likely multi-release if honest)

Everything in **A + B**, plus:

1. **Open-box pipeline UX**
   - Single entry: `omg pipeline "goal"` (or `omg auto`) with progressive stage banners, human-readable blockers, and `report.json` as primary artifact.
   - Auto PRD scaffold / acceptance command discovery hints when missing (still CLI-owned `verified`).
   - Resume ergonomics: `omg pipeline --resume` prints next stage + why integrate re-ran.
2. **Implement strategy selection**
   - Autopilot chooses ulw vs ralph from goal structure (multi independent slices → ulw; long verification loop → ralph) with override flags.
3. **End-to-end worker lifecycle owned by CLI FSM**
   - Pipeline implement stage orchestrates prepare → (spawn | process) → seal → integrate without relying solely on model memory of envelope paths.
   - Optional: tasks.json decompose step before spawn (process fanout follow-up from council synthesis).
4. **Isolation completeness**
   - Spawn-gate (A) + global soft-gate + capability live canary **matrix** in `live_suite`.
   - Clear leader vs worker threat model in doctor footer (no overclaim).
5. **Dual-review productization** (not a process gate for *this* research, but in C as product feature)
   - Prefer native parallel RO spawn when host ready; keep sequential interim behind flag.
6. **Claim language freeze** for marketplace-ish README: what is hard vs soft vs convention.

**Non-goals still:** tmux team UI, claiming PreToolUse is a sandbox, external multi-LLM default workers.

### Assumptions

- Solo maintainer can afford multi-week focus **or** accepts 0.3.0 = partial C with 0.3.x follow-through.
- Host APIs stable enough to automate prepare/seal around spawns.
- Users want one command autopilot more than composable low-level modes.

### Complexity

| Dimension | Level |
|-----------|--------|
| Engineering | **High** (FSM productization + orchestration + isolation matrix + UX) |
| Host dependency | **High** (spawn gate + concurrent children + telemetry) |
| Maintainer ops | **High** (live suite breadth; regressions across stages) |
| Quota burn | **High** (pipeline plan+ulw+dual+accept per smoke) |

### Risks

| Risk | Mitigation |
|------|------------|
| Scope explosion / half-finished autopilot | Strict stage milestone exits; if timebox slips, demote to B and ship |
| Opaque FSM failures | Stage history already in `pipeline.json`; invest in operator messages before new stages |
| Over-automation fights power users | Keep `ralph` / `ralplan` / `ulw` as escape hatches |
| Solo bus factor | Prefer fewer code paths; avoid dual first-class fanout engines |

### Success criteria

1. A + B criteria green.
2. New user: install → `omg doctor` → `omg pipeline "…"` → `report.json` with `verified` or explicit blocker stage **without** reading internal skill playbooks.
3. Live suite `--quota-heavy` exercises full pipeline path (plan may skip) with isolation canaries.
4. Security-model truth table matches every README claim.
5. Dual-review does not stamp `verified` (already true); accept remains sole verified writer.

### Dependencies on Grok host

All of A + B, plus:

| Need | Why | Fallback |
|------|-----|----------|
| Reliable long-running multi-stage sessions / timeouts | Autopilot wall clock | Per-stage process relaunch (pipeline already stages via CLI) |
| Optional native RO dual spawn | Product dual-review quality | Keep sequential interim |
| Stable plugin hook install paths | Doctor hard checks | install-plugin.sh discipline (0.2.5 pattern) |

### Effort estimate (solo)

~3–6+ weeks for a trustworthy open-box; **do not pack full C into a thin 0.3.0** without cutting B/A quality.

---

## Comparison matrix

| Criterion | A | B | C |
|-----------|---|---|---|
| Closes isolation hard-gate gap | **Primary** | Via P0 A | Full matrix |
| Closes ULW default parallel gap | No | **Primary** | Yes |
| Closes open-box autopilot gap | No | Partial (path exists) | **Primary** |
| Solo maintainer fit | Best short | **Best 0.3.0** | Poor unless phased |
| Generous Grok quota utilization | Low | **High (good)** | Very high |
| Host risk if spawn payload incomplete | Blocks A | Blocks B marketing | Blocks C |
| Regress risk to 0.2.5 live-gates | Low | Medium | High |
| User-visible wow | Low (security) | **Medium–high** | Highest |

---

## Trade-offs (decision table)

| Option | Pros | Cons |
|--------|------|------|
| **A only** | Smallest ship; highest security ROI; low quota; preserves 0.2.5 modes | Leaves ULW/pipeline as “shell + convention”; underuses generous quota; weak product story for 0.3.0 |
| **B (A+ULW path)** | Matches product gap #2; uses quota for real parallel; builds on integrate/workers already shipped; still bounded | Needs host spawn-gate proof; multi-spawn live flakiness; default-flip politics (ralph vs ulw) |
| **C full** | True open-box autopilot; closes all three gaps | Solo overload; long tail UX; risk of shallow completion; dual-review/process fanout temptations |

**Default-flip sub-tradeoff (inside B):**

| Choice | Pros | Cons |
|--------|------|------|
| Pipeline default `implement=ulw` | Autopilot = parallel by default | Surprises ralph-oriented users; more integrate failures on coupled goals |
| Keep default ralph; add `omg auto` / docs “parallel product path” | Safer migration | Two entrypoints; Gap 2 partially remains if users never find ULW |
| **Recommended synthesis** | `omg pipeline` stays ralph default **or** documents implement explicitly; **`omg ulw` + `pipeline --implement ulw`** marketed as parallel product; optional later heuristic | Slightly less “one command” than C |

---

## Recommendation (solo maintainer + generous Grok quota)

### Ship **Option B** for 0.3.0

**Why B (not A-only, not full C):**

1. **Quota is an asset, not a constraint.** A-only leaves the differentiator (multi-agent parallel on Grok-native spawn) under-productized after 0.2.5 already proved capability and integrate mechanics.
2. **Solo capacity fits B, not C.** Workers prepare/seal, integrate ancestry, pipeline FSM, and live_suite already exist — B is **productization of the parallel spine**, not a greenfield autopilot OS.
3. **Isolation without parallel is incomplete product risk.** Parallel without A multiplies convention-only spawns. B **requires A as P0**, so Gap 1 is not deferred indefinitely.
4. **C’s open-box UX is the right 0.3.x/0.4 vision**, but bundling it into one 0.3.0 increases half-finished surface (resume UX, auto-PRD, strategy selection, dual-review nativeization) that a solo maintainer will not verify end-to-end.

### Suggested 0.3.0 sequencing (for planner/executor)

1. **P0 — Option A** spawn fail-closed validation + doctor + live canary (blockers if host payload missing → document host gap, do not fake hard gate).
2. **P1 — ULW product path** skill/CLI prompt pack + pipeline `--implement ulw` path live-gated; multi-slice envelope proof; keep process fanout experimental.
3. **P2 — Docs / claim language** security-model + README 0.3.0; residual honesty (leader shell, hook fail-open).
4. **Explicitly defer to 0.3.x/0.4 (Option C slice):** open-box autopilot UX, auto strategy selection, tasks.json auto-decompose, native dual-review spawn, process fanout promotion.

### What not to do

- Do not promote `OMG_EXPERIMENTAL_PROCESS_FANOUT` to default isolation (workers are OS-level grok with shell residual).
- Do not claim PreToolUse soft-gate as hard sandbox even after A (A is fail-closed **when hook healthy + host honors deny**).
- Do not block 0.3.0 on dual-review nativeization (waived process gate; sequential interim remains acceptable).
- Do not set `verified` from pipeline dual-review APPROVE (existing contract).

---

## Consensus-style addendum (steelman)

- **Antithesis (steelman for A-only):** With a solo maintainer, every week spent on multi-spawn live flakiness is a week not spent hardening the only thing that makes parallel *safe*. Shipping A-only 0.3.0 maximizes truth-in-advertising and minimizes host schedule risk; ULW “default path” can wait until spawn payload is proven for a full minor.
- **Tradeoff tension:** **Safety completeness (A)** vs **product differentiator under surplus quota (B)** vs **end-user autopilot completeness (C)**. You cannot maximize all three in one solo-shaped 0.3.0.
- **Synthesis:** B-with-A-as-P0; if host spawn validation is impossible, **automatically demote 0.3.0 to A-docs + ULW prompt-only improvements** and refuse “hard isolation” marketing.
- **Principle flags:** Never market fail-open hooks as hard isolation; never let dual-review or model prose set `verified`; depth=1 remains non-negotiable.

---

## References (code / docs)

| Ref | What it shows |
|-----|----------------|
| `docs/security-model.md` | Layer table: capability hard-ish, PreToolUse fail-open, HARD RULES convention |
| `docs/research/live-gates-2026-07-20-suite.md` | 0.2.5 live green; L-CAP-SPAWN; residual leader-shell honesty |
| `docs/research/subagent-pretooluse-spike.md` | Subagent inherits PreToolUse; still fail-open; global hook install required |
| `omg_cli/deny.py` | No spawn/`capability_mode` validation today |
| `hooks/bin/pre_tool_use_deny.py` | Soft-gate entry → deny package only |
| `omg_cli/modes.py` `HARD_RULES_REMINDER` | Prompt injection of capability_mode MUST (convention) |
| `agents/omg-executor.md` | `capabilityMode: read-write` + disallowed shell/spawn |
| `skills/omg-ultrawork/SKILL.md` | Parallel playbook; multi-spawn in one turn; envelopes |
| `omg_cli/fanout.py` | Process fanout experimental; skill path is default isolation story |
| `omg_cli/main.py` ~97–106 | `OMG_EXPERIMENTAL_PROCESS_FANOUT=1` gate |
| `omg_cli/pipeline.py` | FSM order; default `implement="ralph"`; integrate/report ownership |
| `omg_cli/workers.py` | prepare/seal for no-shell workers |
| `omg_cli/dual_review.py` | Sequential headless interim dual-review |
| `skills/omg-pipeline/SKILL.md` | Autopilot prefers CLI FSM; default implement ralph |
| `plugin.json` | Version **0.2.5** baseline |
| `docs/research/council-v021-synthesis.md` | Parallel-without-tmux design; process fanout residual |

---

## DONE criteria for this artifact

- [x] Three ranked options A/B/C with assumptions, complexity, risks, success criteria, Grok host dependencies  
- [x] One recommended default for solo + generous Grok quota (**B**, A as P0)  
- [x] Written to `.omc/research/autopilot-architect-options.md`
