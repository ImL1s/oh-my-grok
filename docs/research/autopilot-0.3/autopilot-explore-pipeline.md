# Autopilot explore: `omg pipeline` as product (not just tests)

**Date:** 2026-07-20  
**Repo:** `<repo-root>`  
**Sources:** `omg_cli/pipeline.py`, `omg_cli/main.py` (`cmd_pipeline`), `omg_cli/dual_review.py`, `omg_cli/acceptance.py`, `omg_cli/modes.py`, `omg_cli/ralplan.py`, `skills/omg-pipeline/SKILL.md`, `skills/omg-dual-review/SKILL.md`, `README.md`, `tests/test_pipeline.py`, `tests/test_dual_review.py`, `docs/research/live-gates-2026-07-20-suite.md`

---

## 1. What a user can do today with `omg pipeline "goal"`

### One-command composition (CLI-owned FSM)

```text
plan → implement → integrate → dual_review → accept → report.json
```

Default invocation:

```bash
omg pipeline "goal"
```

Creates a `mode=pipeline` run under `.omg/state/runs/<id>/`, writes `pipeline.json` + final `report.json`, and never sets `OMG_ALLOW_EXTERNAL_CLI`. External advisors stay human-owned (`omg ask`), not auto-shelled.

### Stage behavior (product path)

| Stage | What happens | Default |
|-------|----------------|---------|
| **plan** | Embeds `run_ralplan` on the **same run_id** (draft→critic→revise→verifier). Terminal plan OK only if ralplan `accepted` (verifier artifact whole-word **APPROVE** / JSON). | On (`max_plan_rounds=3`) |
| **implement** | `run_mode(ralph\|ulw)` on same run; **pipeline owns accept** (`require_acceptance=False` inside implement). | `ralph`, `max_iter=3` |
| **integrate** | Runs when `implement=ulw` **or** ULW envelopes exist under `.omg/artifacts/ulw-results/`. Re-runs after dual-review re-implement (AC4). ULW + missing envelopes → hard fail. | Conditional |
| **dual_review** | Sequential headless critic→verifier (`run_dual_review`). Loop up to `max_dual_review_rounds` (API default **2**). `APPROVE` continues; `FAILED` stops; `REQUEST_CHANGES`/`UNKNOWN` → re-implement + re-integrate. | On |
| **accept** | `load_prd` → freeze + run acceptance → `set_verified` only on real CLI stamp. | `require_acceptance=True` in cmd |
| **report** | Always writes `runs/<id>/report.json` (`writer: omg-cli`) on success **and** terminal failure. | Always |

### Flags / modes users can actually pass today

| Flag | Effect |
|------|--------|
| `--plan-only` | Stop after accepted plan; status `completed`, **not** product verified |
| `--skip-plan` | Skip ralplan; mark `plan_accepted` and start at implement |
| `--implement ralph\|ulw` | Implement engine (default ralph) |
| `--max-plan-rounds N` | Ralplan rounds (default 3) |
| `--max-iter N` | Ralph/ulw iters (default 3) |
| `--dual-review` / `--no-dual-review` | Dual stage on/off (default **on** via code, not argparse default) |
| `--require-acceptance` / `--no-require-acceptance` | Fail if not verified (code default **on**) |
| `--dry-run` | FSM + argv/prompt artifacts; no live grok; dry plan/dual treated as progress so order is testable |
| `--resume RUN_ID` | Resume from `pipeline.json` stage; re-integrates if envelope heads are stale |
| `--force` | Supersede active run mutex |
| `--timeout SEC` | Per-grok-launch timeout |
| `--safe` / `--yolo` | Common parents; dual/ralplan RO stages force safe + disallow shell |

### Skill surface

`skills/omg-pipeline/SKILL.md` steers the model to prefer CLI FSM over inventing parallel autopilot, documents stage table, and anti-pattern: dual APPROVE ≠ product verified.

### Honest product ceiling today

- **Works well as:** composition glue + state machine + report for Grok-native workers (hermetic tests cover order, integrate re-run, skip-plan, plan-only, report-on-fail, no allow-env).
- **Does not work as:** true one-shot autopilot that ends in `verified=true` for a greenfield goal without human/PRD setup. Live dual-review is **interim sequential headless**, not native `spawn_subagent` dual-review (`OMG_DUAL_REVIEW_REQUIRE_NATIVE=1` refuse gate).

---

## 2. What’s broken / awkward (flags, skip-plan, dual, accept)

### Flags (UX + wiring gaps)

1. **Inverted / lying help text**
   - `--require-acceptance` is `store_true` with argparse `default=False`, help says *“default on”*. Actual default is applied in `cmd_pipeline` (always True unless `--no-require-acceptance`). Confusing for `--help` readers and completion tools.
   - Same pattern for `--dual-review` (`store_true` default False; code default on unless `--no-dual-review`).

2. **API flags not exposed on CLI**
   - `run_pipeline(..., max_dual_review_rounds=2)` — **no** `--max-dual-review-rounds` on `omg pipeline`.
   - `run_pipeline(..., require_squash=False)` — **no** `--require-squash` on pipeline (only on `omg integrate`). ULW autopilot cannot demand squash from the product entrypoint.

3. **Resume + flags**
   - `--resume` needs no goal, but other flags (`skip_plan`, `implement`, dual on/off) are re-read from CLI args rather than fully rehydrated from `pipeline.json` for every decision (state has some fields; resume stage skip uses history + stage). Easy to resume with mismatched `--implement` / dual toggles vs original run.

4. **Mutual exclusion** only for `--skip-plan` ∩ `--plan-only` (exit 2). Good. No clear error if user passes both dual flags; last-wins order in `cmd_pipeline` is: start dual=True → `--no-dual-review` off → `--dual-review` on again.

### skip-plan

- Correctly marks history `plan/skip` and sets `plan_accepted=True`.
- Product awkwardness: **no plan artifact** is required for implement/dual/accept. Fine for “I already planned,” dangerous as default autopilot shortcut if users skip consensus without a PRD.

### dual_review

1. **Interim, not native** — two sequential `grok -p` processes with agent bodies injected; not spawn isolation. Documented honestly in skill + module doc + live gates (“do not claim native dual-review shipped”).

2. **Artifact I/O is hope-driven**
   - `_launch_grok` does **not** capture stdout into `stages/dual-verifier-NN.md`.
   - After launch, if file missing → CLI writes **stub without APPROVE** → `parse_verdict` → `UNKNOWN`.
   - Live path depends on the model **writing** the stage path with tools while forced `safe=True` + `--disallowed-tools run_terminal_command` + agent frontmatter forbidding `search_replace`. Fragile contract.

3. **Loop budget** fixed at 2 rounds in library; not user-tunable from CLI. Exhausted `REQUEST_CHANGES`/`UNKNOWN` → pipeline failed.

4. **dry_run dual** breaks out of loop without APPROVE (so FSM can reach accept) — correct for tests; live users must not treat dry-run exit 0 as review quality.

### accept / verified

1. **Empty PRD scaffold is the default reality**
   - Implement (`ralph`) writes `prd.json` scaffold with `stories: []`, `global_commands: []`.
   - Pipeline accept: `prd_has_acceptance_commands` → False → `do_accept()` False → with default `require_acceptance` → **exit 1**, status `completed` not `verified`.
   - So **`omg pipeline "goal"` rarely ships a verified run** unless implement (or human) fills real argv commands first.

2. **No pipeline stage generates acceptance commands from the goal**
   - Ralplan can discuss acceptance; it does not author authoritative `prd.json` commands for the runner.
   - Dual APPROVE does not create or run acceptance.

3. **Semantics: dual APPROVE ≠ verified** is correct security design, but product messaging is weak: users who hear “autopilot” expect end-to-end green. Skill anti-pattern mentions it; CLI success path for “not verified” is stderr line + exit 1, easy to misread as hard crash rather than “missing PRD commands.”

### Other product friction

| Issue | Detail |
|-------|--------|
| Active-run mutex | Second pipeline without `--force` fails while another mode is active — good, but no “queue” UX |
| Long wall-clock | plan (multi launches) + implement loop + dual (2×Grok) + accept — no progress UX beyond status JSON |
| README CLI tree | One synopsis still shows `{setup,doctor,...,ralplan}` without `pipeline`/`ask` in the brace list (table row exists) |
| Live suite | L-DUAL-1 covers standalone dual-review, **not** full `omg pipeline` e2e to verified |

---

## 3. Verdict parse residual (APPROVE vs REQUEST_CHANGES)

### Implementations (two slightly different contracts)

| Path | APPROVE | REQUEST_CHANGES | FAILED | Priority when co-present |
|------|---------|-----------------|--------|---------------------------|
| **ralplan** `artifact_contains_approve` | Case-sensitive whole-word `APPROVE`; JSON field exact `"APPROVE"` | N/A (boolean gate only) | N/A | Approve-or-not only |
| **dual_review** `parse_verdict` | Case-sensitive whole-word `APPROVE`; JSON keys `verdict|decision|status` normalized upper | Case-**insensitive** `REQUEST[_ -]?CHANGES` | Case-**insensitive** whole-word `FAILED` | **FAILED > REQUEST_CHANGES > APPROVE** |

### Documented / residual false positives

From `dual_review.py` comment + live residual notes:

1. **Instruction text pollution**  
   `"do not APPROVE lightly"` / agent body phrases containing whole-word `APPROVE` without a terminal `REQUEST CHANGES` / `FAILED` can parse as **APPROVE**.  
   Live note (`docs/research/live-gates-2026-07-20-suite.md`): *“Dual-review CLI summary line may print APPROVE while stage markdown is REQUEST_CHANGES (parser residual) — stages/artifacts remain source of truth.”*  
   (That specific mismatch may also come from synthesis print vs which file was parsed; treat artifacts as SoT.)

2. **Case asymmetry**  
   Prose `approve` / `Approve` → `UNKNOWN` (tests assert). JSON `"verdict": "approve"` normalizes to APPROVE. Mixed signals between JSON and prose.

3. **NEEDS_REVIEW dry stubs**  
   Dry dual stubs use `NEEDS_REVIEW` → `UNKNOWN`. Pipeline maps UNKNOWN like REQUEST_CHANGES for re-loop (live), but **dry_run** dual breaks without re-implement — intentional for tests.

4. **Pipeline loop mapping**

```text
APPROVE        → leave dual loop, proceed to accept
FAILED         → pipeline failed (exit 1)
REQUEST_CHANGES / UNKNOWN → re-implement + re-integrate until max_dual_review_rounds
exhausted      → failed
```

5. **Ralplan vs dual inconsistency**  
   Plan gate is APPROVE-or-fail only (no structured REQUEST_CHANGES FSM beyond ralplan’s own revise loop). Dual has richer verdicts. Users may expect the same string contract everywhere; prompts say `REQUEST CHANGES` with a space; dual regex accepts space/underscore/hyphen; ralplan never “accepts” on REQUEST_CHANGES (correct).

### Minimal parse hardenings (for product slice, not full rewrite)

- Prefer a **final** `## Verdict` section or last-line token over full-blob scan.
- Strip / ignore lines that are clearly negations (`do not APPROVE`, `never APPROVE`).
- Unify ralplan + dual on one `parse_verdict` module.
- Capture Grok stdout → stage artifact when model does not write the path (eliminates stub→UNKNOWN thrash).

---

## 4. Minimal product slice: make pipeline a real autopilot entry

Goal: a new user can run one command, get an honest end state (`verified` **or** clear blocker with next action), without inventing their own orchestration.

### Task A — Acceptance path that can actually green (highest leverage)

**Problem:** empty PRD scaffold → accept always fails under default `require_acceptance`.  
**Slice:**

1. After implement (or as part of plan accept), ensure `prd.json` has at least one policy-legal command path:
   - Option 1 (minimal): if project has conventional test entry (`pytest -q`, `python3 -m pytest`, etc.), **scaffold** `global_commands` when empty (CLI-owned, not model-forged).
   - Option 2: ralplan verifier acceptance **must** propose argv arrays; pipeline copies them into `prd.json` only after structural validate + policy check.
2. Accept stage error message: print concrete next step (`edit prd.json commands` / `omg accept --review`) and `report.json` path.
3. Hermetic test: pipeline with stub implement + real `[["true"]]` PRD → exit 0 + `verified=true`.

### Task B — Dual-review output contract (stop UNKNOWN thrash)

**Problem:** no stdout capture; missing stage file → UNKNOWN → fake re-implement budget burn.  
**Slice:**

1. After each dual/ralplan launch, if stage `.md` missing/stub, capture process stdout (or `--output-format plain` tee) into the stage artifact path.
2. Prompt one-liner: “Your **entire** response is the stage artifact; first heading must be `## Verdict`.”
3. Tighten `parse_verdict` to prefer last `## Verdict` block; add negation guard for `do not APPROVE`.
4. Unit tests for residual cases from live-gates note.

### Task C — Flag / help honesty + missing knobs

**Slice:**

1. Fix argparse: use defaults that match product (`dual_review` default True; `require_acceptance` default True) via `BooleanOptionalAction` or explicit `--no-*` only.
2. Expose `--max-dual-review-rounds` and `--require-squash` on `omg pipeline`.
3. Align README synopsis brace list with real subcommands; add 3 pipeline examples (default / plan-only / skip-plan+ulw).

### Task D — Single live gate for the product entry

**Slice:**

1. `scripts/live_suite.sh` (or smoke): `omg pipeline "<fixture goal>" --max-iter 1 --max-plan-rounds 1 --timeout …` with a PRD that can pass (`true` or project pytest).
2. Assert: `report.json` exists; `verified` true **or** documented fail stage; dual never sets verified alone; no `OMG_ALLOW_EXTERNAL_CLI` on parent.
3. Keep claim language: sequential dual interim, not native spawn.

### Task E (optional in same slice) — Resume + status UX

**Slice:**

1. `omg state` (or `omg pipeline --status`) prints stage timeline from `pipeline.json` history in human lines.
2. On resume, prefer frozen fields from `pipeline.json` over CLI overrides unless `--force-config`.

---

## Verdict (product, not test)

| Dimension | Status |
|-----------|--------|
| FSM completeness | **Strong** — order, integrate re-run, resume stale heads, report always, Grok-native only |
| Hermetic tests | **Strong** — order/skip/plan-only/integrate/re-integrate/mutex |
| CLI product polish | **Weak** — flag defaults/help lie; missing max dual / squash |
| End-to-end “autopilot to verified” | **Broken for greenfield** — accept needs real PRD commands pipeline does not author |
| Dual-review as gate | **Interim usable, parse + artifact residual** — not native multi-agent |
| Marketing-safe claim | Pipeline is a **composed run supervisor**, not Autopilot 1.0 |

**Bottom line:** `omg pipeline` is a real CLI product skeleton with correct security boundaries and good tests. It is **not** yet a trustworthy autopilot entry until (A) acceptance commands can be produced/seeded, (B) dual verdicts are reliably materialised and parsed, and (C) flags/docs match defaults. Tasks A–D above are the minimal slice.

---

## File map (quick)

| Path | Role |
|------|------|
| `<repo-root>/omg_cli/pipeline.py` | FSM implementation |
| `<repo-root>/omg_cli/main.py` | `cmd_pipeline` + argparse |
| `<repo-root>/omg_cli/dual_review.py` | Sequential dual + `parse_verdict` |
| `<repo-root>/omg_cli/ralplan.py` | Plan stage + APPROVE gate |
| `<repo-root>/omg_cli/acceptance.py` | Only path to verified |
| `<repo-root>/skills/omg-pipeline/SKILL.md` | Model playbook |
| `<repo-root>/tests/test_pipeline.py` | Hermetic FSM tests |

DONE.
