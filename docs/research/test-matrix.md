# Test matrix (oh-my-grok)

Last updated: 2026-07-20 · Product version: **0.2.5** (docs only; no version bump from this file)

**Sources:** multi-agent review `live-gates-multi-agent-review-2026-07-19.md`, plan `docs/superpowers/plans/2026-07-20-live-gates-completeness.md`.

---

## Pyramid

| Layer | Name | What it proves | Share of live quota |
|-------|------|----------------|---------------------|
| **L0** | Unit (`pytest`) | deny / accept / integrate / pipeline / cancel / doctor / command policy / canary classify | **0%** |
| **L1** | Hermetic e2e | `scripts/e2e_realpath.py` + `scripts/smoke.sh` (temp git, no Grok LLM) | **0%** |
| **L2** | Live suite | host hooks, spawn capability, dual-review tool clamp, real launchers | **only what L0/L1 cannot prove** |

**Rule:** Do not burn Grok quota on regex deny, acceptance floors, or integrate ancestry — those are unit/e2e.

```text
        ┌──────────── L2 live (opt-in, quota) ────────────┐
        │ canary · ulw/ralph · dual · cap-spawn · cancel  │
        └──────────────────────▲──────────────────────────┘
                               │ only host/model gaps
        ┌──────────── L1 hermetic e2e ────────────────────┐
        │ smoke (default OMG_E2E=1) · e2e_realpath        │
        └──────────────────────▲──────────────────────────┘
                               │
        ┌──────────── L0 unit ────────────────────────────┐
        │ pytest markers: unit / integration / slow       │
        │ exclude: -m "not live" for PR CI                │
        └─────────────────────────────────────────────────┘
```

---

## AC coverage map (brief)

| AC / claim area | Owner layer | Primary artifact |
|-----------------|-------------|------------------|
| **AC1** CLI contracts (accept / integrate / cancel / forge deny) | L0 + L1 | `tests/test_acceptance.py`, `test_integrate.py`, `e2e_realpath.py` |
| **AC2** Command policy floors (`python -c`, shells, agent CLIs, git/make grammar) | L0 | `tests/test_command_policy.py` |
| **AC3** Doctor global PreToolUse soft-gate hard check | L0 (+ install) | `tests/test_doctor.py`, `scripts/install-plugin.sh` |
| **AC4** Canary classify DENIED vs INCONCLUSIVE | L0 dry + L2 live | `scripts/canary_pretool.py`, `tests/test_canary_classify.py` |
| **AC5** Host isolation / dual-review / capability spawn | **L2 only** | `scripts/live_suite.sh` + `docs/research/live/` evidence |

Green L0/L1 **does not** satisfy AC5. Dated L2 evidence is required before isolation marketing language.

---

## Commands by cadence

| Cadence | Commands |
|---------|----------|
| **PR / daily** | `PYTHONPATH=. python3 -m pytest -q -m "not live"`<br>`./scripts/smoke.sh` (default `OMG_E2E=1`; doctor soft unless `OMG_SMOKE_STRICT=1`)<br>`python3 scripts/canary_pretool.py --dry` |
| **Weekly** (quota) | `./scripts/live_suite.sh --quick`<br>optional: `./scripts/live_suite.sh --full` |
| **Tag / release** | `OMG_SMOKE_STRICT=1 ./scripts/smoke.sh`<br>`./scripts/live_suite.sh --full` (or `--quota-heavy` if isolation claims ship)<br>Evidence under `docs/research/live/suite-*-summary.json` |

Env notes:

- `OMG_E2E=0` skips hermetic e2e inside smoke.
- `OMG_LIVE_REQUIRE=1` makes live suite **fail** (not skip) when `grok` is missing.
- Live evidence dir: `OMG_LIVE_EVIDENCE_DIR` (default `docs/research/live/`).

---

## Forbidden claim language

| Allowed | Forbidden |
|---------|-----------|
| Live soft-gate canary passed **with global hook** (parent+child) | Plugin hooks alone guarantee no external CLI |
| ulw/ralph headless launch live OK (`--prompt-file`) | Ultrawork **parallel spawn** path live verified |
| Unit/e2e cover integrate/accept/cancel contracts | Production-ready isolation |
| Dual-review is sequential headless interim | Native dual-review shipped / complete |
| Doctor hard-checks `~/.grok/hooks/omg-pretool-deny.json` | Soft-gate is a hard sandbox |
| pytest + smoke green for CLI library contracts | Live host behavior proven by unit green alone |

See also: [`docs/security-model.md`](../security-model.md) · multi-agent review § Allowed vs forbidden.

---

## Related scripts

| Script | Role |
|--------|------|
| `scripts/smoke.sh` | Doctor (soft), dry-runs, canary dry, default hermetic e2e |
| `scripts/e2e_realpath.py` | No-LLM seal → integrate → accept / forge / fanout gate |
| `scripts/canary_pretool.py` | PreToolUse PATH-shim canary (`--dry` / `--live`) |
| `scripts/live_suite.sh` | Opt-in live: `--quick` / `--full` / `--quota-heavy` |
| `scripts/install-plugin.sh` | Plugin + global PreToolUse hook install |
