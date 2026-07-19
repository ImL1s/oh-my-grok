# Multi-agent review: live gates completeness (2026-07-19)

**HEAD reviewed:** `20f093e`  
**Reviewers:** code-reviewer (coverage), security-reviewer (adversarial), test-engineer (matrix)  
**Mode:** read-only fan-out; this file is synthesis (strictest wins).

---

## Push / commit status

- Commit `20f093e` pushed to `origin/main` (ImL1s/oh-my-grok).
- Evidence already on tree: `live-gates-2026-07-19.md`, `canary-pretool-latest.json`, spike update, `--prompt-file`, global hook install.

---

## Combined verdict (strictest wins)

| Claim | Verdict |
|-------|---------|
| Live gates **table** (ulw / ralph / PreToolUse canary) done as smoke | **ALMOST** — dated evidence exists; ulw did not exercise spawn |
| Dual-review **product completeness** / production soft-gates | **INCOMPLETE** |
| CLI contracts (accept / integrate / cancel / forge verified) via unit+e2e | **Strong** |
| Isolation hard claim (“workers cannot shell / external CLI”) | **Not proven by live** — primary layer untested adversarially |

**One line:**  
pytest + e2e_realpath + dry smoke ≈ ready for **CLI library contracts**; live ≈ **launcher + prompt-file + global PreToolUse canary**. Do **not** market as production isolation or dual-review complete.

---

## What was tested well

1. **258 unit tests** — verified ownership, command policy floors, integrate ancestry/merge, seal dirty, cancel fail-closed kill, pipeline re-integrate, fanout env gate, dual-review order/RO argv, ask child-only env.
2. **`scripts/e2e_realpath.py`** — no-LLM seal → integrate → accept; forge deny; fanout exit 2.
3. **Live ulw/ralph** — real `grok`, exit 0, fixed artifacts, `--prompt-file` (YAML `---` fix).
4. **Live canary** — honest `REAL_CLI_RAN` when plugin-only; then parent+child **deny** with global hook; PATH shim (no real claude burn).
5. **Honesty in docs** — fail-open residual, capability_mode primary, no “hard sandbox” overclaim in security-model.

---

## Gaps (P0 first)

### P0 — block “production isolation / dual-review complete” claims

| ID | Gap |
|----|-----|
| P0-1 | **`omg dual-review` no live** |
| P0-2 | **`omg pipeline` no live** |
| P0-3 | **`capability_mode` no live oracle** (child no Execute / no shell) |
| P0-4 | **Plugin PreToolUse alone fails** — soft-gate depends on `~/.grok/hooks`; doctor does not hard-check it |
| P0-5 | **No CI** for `OMG_E2E` / live matrix |
| P0-6 | **Live ulw did not spawn** — single-leader file write ≠ ultrawork |

### P1 — next live suite

Process fanout live; multi-envelope integrate live; seal dirty CLI path; cancel killpg live; ask + `OMG_ALLOW` live; pipeline resume; ralplan live; multi-iter ralph; accept → `verified:true` after live; tool-id RO clamp live.

### Security residual (R1–R6)

| Rank | Risk |
|------|------|
| R1 | Leader / process-fanout full shell by design — live never forced isolation |
| R2 | capability_mode omitted → full tools |
| R3 | Interpreter escapes (`python3 -c`, etc.) bypass deny |
| R4 | Plugin-only / broken absolute path in global hook → REAL_CLI |
| R5 | Acceptance allowlist: `git`/`make`/`cargo` any argv too open |
| R6 | Forged ULW envelopes + empty `changed_files` skips check |

### False confidence

| Green signal | Only proves |
|--------------|-------------|
| ulw/ralph exit 0 + OK file | Grok can write a file |
| canary `marker_absent_ok` | Literal `claude` denied **or** model abstained |
| doctor PreToolUse OK | Plugin `hooks.json` has key — **not** that deny fires |
| 258 pytest | Library contracts, not host capability |

---

## Better testing (agreed methods)

### Daily / PR (no quota)

```bash
PYTHONPATH=. python3 -m pytest -q
OMG_E2E=1 ./scripts/smoke.sh
python3 scripts/canary_pretool.py --dry
```

### Minimum next live suite (~45–75 min)

1. **Capability + must-spawn** negative (child cannot shell)  
2. **`omg dual-review` live** (short PLAN; verified still false)  
3. **`omg accept` closed loop** → `verified:true` (CLI only after live)  
4. **Cancel killpg** against long-running grok  
5. **Canary regression** + optional plugin-only negative (expect REAL_CLI)

### Hard-gate upgrades (product)

- Doctor **strict**: require `~/.grok/hooks/omg-pretool-deny.json` + executable path  
- Canary success oracle: session `hook_execution` must show deny — **abstain = INCONCLUSIVE fail**  
- Tighten acceptance grammar for `git`/`make`/`cargo`  
- Integrate: require non-empty `changed_files` matching diff  
- `scripts/live_suite.sh --quick|--full|--quota-heavy` (not yet implemented)

### Pyramid

| Layer | Role | Live share |
|-------|------|------------|
| Unit | deny/accept/integrate/pipeline/cancel | 0% |
| Hermetic e2e | e2e_realpath + smoke | 0% |
| Live | host hooks, spawn capability, dual-review tool clamp | only what unit cannot prove |

**Do not burn Grok on regex deny / acceptance floors.**

---

## Allowed vs forbidden claim language

| Allowed | Forbidden |
|---------|-----------|
| Live soft-gate canary passed **with global hook** (parent+child) | Plugin hooks guarantee no external CLI |
| ulw/ralph headless launch live OK (`--prompt-file`) | Ultrawork parallel spawn path live verified |
| Unit/e2e cover integrate/accept/cancel contracts | Production-ready isolation |
| Dual-review is sequential headless interim | Native dual-review shipped |

---

## Recommended next actions (ordered)

1. Implement `scripts/live_suite.sh --quick` wrapping current three gates + L-ACCEPT-1.  
2. Add doctor hard check for global PreToolUse hook.  
3. Run P0 live: capability-spawn + dual-review + accept verified.  
4. Policy: tighten `git`/`make` allowlist or document as residual.  
5. Keep dual-review completeness claim gated until P0 evidence dated.

---

## Reviewer IDs (for resume)

- code-reviewer: `019f7b96-8e30-7cf1-99d7-9c7d60b72e74`  
- security-reviewer: `019f7b96-8e30-7cf1-99d7-9c8aa6e5ec87`  
- test-engineer: `019f7b96-8e30-7cf1-99d7-9c92bd6e21cc`
