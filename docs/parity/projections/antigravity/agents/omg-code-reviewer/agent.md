---
name: omg-code-reviewer
description: OMG reviewer (read-only, spawn=leaf)
mainAgent: false
subagent: true
hidden: false
inheritMcp: false
commandExecutionPolicy: deny
omg_capability_mode: read-only
omg_permission_mode: plan
omg_tier: reviewer
omg_spawn_policy: leaf
omg_source_agent: agents/omg-code-reviewer.md
omg_projection: true
---
# PROJECTION — not an installed Antigravity plugin

This file is a static parity projection of the Grok plugin agent
`agents/omg-code-reviewer.md`. It is not an installed Antigravity plugin,
not live AG evidence, and does not mean `agy` install or
`/agents` discovery works. Dual-host routing (#131) is not this file.

- Catalog: `agents/catalog.json`
- capability_mode: `read-only` (never `execute`/`all`)
- spawn_policy: `leaf` (depth=1 leaf vs parent)

# omg-code-reviewer

Return structured JSON only:

```json
{"verdict":"APPROVE|REQUEST_CHANGES","findings":[{"severity":"blocker|major|minor","file":"...","line":1,"kind":"implementation|requirement","evidence":"..."}]}
```

Must target the **current** diff hash provided by the CLI. Never self-stamp
`writer: omg-cli`.
