# OMC parity multi-advisor council (2026-07-20)

Research pack from the multi-Grok + external advisor audit of **oh-my-grok vs OMC-class** product completeness, host-feasible “don’t stop,” and post-audit shipping.

## Start here

| Doc | Purpose |
|-----|---------|
| **[STATUS.md](./STATUS.md)** | **Done / not-done matrix** (Grok · Codex · Claude/Fable · product · live) |
| **[OPEN-ITEMS.md](./OPEN-ITEMS.md)** | Remaining backlog (Fable re-run, P0 leftovers, P1+) |
| **[SYNTHESIS.md](./SYNTHESIS.md)** | Council merge + Codex strictest-wins roadmap |
| **[verification](../verification-2026-07-20.md)** | Unit + live gate summary after shipping P0 |
| **[external-advisors.md](../external-advisors.md)** | Codex + Fable CLI contracts (repo copy of global skills) |

## Advisor reports

| File | Advisor | Status |
|------|---------|--------|
| `01-feature-inventory.md` | Grok explore | Done |
| `02-dont-stop-design.md` | Grok architect | Done |
| `03-critic-gaps.md` | Grok critic | Done |
| `04-skill-depth.md` | Grok explore | Done |
| `05-roadmap-0.3.md` | Grok planner | Done |
| `06-security-isolation.md` | Grok security-reviewer | Done |
| `07-live-evidence.md` | Grok verifier | Done |
| **`08-codex.md`** | **Codex gpt-5.6-sol max** (free explore) | **Done** — highest-signal external audit |
| **`09-fable.md`** | **Claude Fable 5** free explore | **BLOCKED** — no independent long report this round |
| `code-review-spawn-retry.md` | Grok code-reviewer | Done (spawn-retry UX) |
| `BRIEF.md` / `external-brief-*.md` | Shared briefs (sanitized for hooks) | Done |

## Related

- Stop continuation DO_NOT_BUILD: [`../stop-continuation/CONSENSUS.md`](../stop-continuation/CONSENSUS.md)
- Security model: [`../../security-model.md`](../../security-model.md)
- Live suite (how to regenerate): [`../live/README.md`](../live/README.md)
- Live suite narrative: [`../live-gates-2026-07-20-suite.md`](../live-gates-2026-07-20-suite.md)
- Autopilot plan: [`../../superpowers/plans/2026-07-20-autopilot-all.md`](../../superpowers/plans/2026-07-20-autopilot-all.md)

## Honest claim language

- **Do not** say “OMC 功能基本都有了.”
- **Do not** say Claude/Fable free audit completed this round.
- **Do** say Codex free audit completed and drove P0 verdict fail-closed work.
- **Do** cite [`../verification-2026-07-20.md`](../verification-2026-07-20.md) for unit+quick+full gates; raw machine logs are local/gitignored.
