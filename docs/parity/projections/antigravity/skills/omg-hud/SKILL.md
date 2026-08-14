---
name: omg-hud
description: OMG skill projection (omg_native, read-only)
omg_projection: true
omg_classification: omg_native
omg_capability_mode: read-only
omg_source_skill: skills/omg-hud/SKILL.md
---
# PROJECTION — not an installed Antigravity plugin

This file is a static parity projection of the Grok plugin skill
`skills/omg-hud/SKILL.md`. It is not an installed Antigravity plugin,
not live AG evidence, and does not mean `agy` skill discovery works.

- Catalog: `skills/catalog.json`
- capability_mode: `read-only` (never `execute`/`all`)
- Playbook without runtime evidence stays `configured`, not `verified`.

# omg-hud

Grok has no OMC host statusline chrome. OMG HUD is a **CLI one-liner** (and JSON pack).

```bash
omg hud
omg hud --run RUN
omg hud --json
```

Example: `omg-hud: ralph|running|implement|run=abc123|−`

Pair with `omg state --human` and `omg resume` for full continuity.
