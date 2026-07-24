# oh-my-grok documentation

English | [简体中文](./README.zh.md) | [繁體中文](./README.zh-TW.md)

User-facing docs for the Grok Build plugin + `omg` CLI.  
**Version:** see [`plugin.json`](../plugin.json) · **Changelog:** [`CHANGELOG.md`](../CHANGELOG.md)

## Start here

| Doc | What |
|-----|------|
| [../README.md](../README.md) · [./readme/README.zh.md](./readme/README.zh.md) · [./readme/README.zh-TW.md](./readme/README.zh-TW.md) | Install, mental model, default flow, CLI reference |
| [skills.md](./skills.md) · [skills.zh.md](./skills.zh.md) · [skills.zh-TW.md](./skills.zh-TW.md) | **All skills** — triggers, invoke, CLI twin, examples |
| [autopilot.md](./autopilot.md) · [autopilot.zh.md](./autopilot.zh.md) · [autopilot.zh-TW.md](./autopilot.zh-TW.md) | Deep dive: autopilot skill + phase machine |
| [workflows.md](./workflows.md) · [workflows.zh.md](./workflows.zh.md) · [workflows.zh-TW.md](./workflows.zh-TW.md) | Versioned repository workflows, receipt execution, and ship gates |
| [security-model.md](./security-model.md) · [security-model.zh.md](./security-model.zh.md) · [security-model.zh-TW.md](./security-model.zh-TW.md) | Isolation honesty (capability_mode, allowlist, fail-open hooks) |
| [RELEASE.md](./RELEASE.md) · [RELEASE.zh.md](./RELEASE.zh.md) · [RELEASE.zh-TW.md](./RELEASE.zh-TW.md) | Maintainer release protocol |

## Skills (quick map)

| Want… | Skill | CLI |
|-------|--------|-----|
| Which mode? | `omg-using` | `omg doctor` / `omg resume` |
| Full auto end-to-end | `omg-autopilot` | `omg autopilot *` |
| Parallel slices | `omg-ultrawork` | `omg ulw` + worker/integrate |
| Persist until done | `omg-ralph` | `omg ralph` |
| Plan only | `omg-ralplan` | `omg ralplan` |
| Clarify vague goal | `omg-deep-interview` | `omg interview *` |
| Multi-story ledger | `omg-ultragoal` | `omg goal *` |
| QA loop | `omg-ultraqa` | `omg qa *` |
| Dual review | `omg-dual-review` | `omg dual-review` / `omg review` |
| Pipeline FSM | `omg-pipeline` | `omg pipeline` |
| External advisor | `omg-ask` | `omg ask` |
| Cancel | `omg-cancel` | `omg cancel` |
| Wiki / HUD / LSP | `omg-wiki` / `omg-hud` / `omg-lsp` | `omg wiki` / `hud` / `lsp` |
| Repeatable staged review | repository workflow | `omg workflow install|list|show|plan|run` |
| Recover / remember / observe | product services | `omg recover` / `memory` / `tracker` / `compact` |

Full tables and copy-paste examples: **[skills.md](./skills.md)**.

## Research (not product docs)

Historical parity / stop-continuation / live gates live under [`research/`](./research/).  
Prefer product docs above for day-to-day use.
