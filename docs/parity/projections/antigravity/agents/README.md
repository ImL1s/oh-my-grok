# Antigravity agent.md projections

**Status:** static parity projection for
[#71](https://github.com/ImL1s/oh-my-grok/issues/71).

These files are **not**:

- an installed Antigravity plugin
- live AG evidence
- proof that `agy` install or `/agents` discovery works
- dual-host routing runtime ([#131](https://github.com/ImL1s/oh-my-grok/issues/131))

They are generated from `agents/catalog.yaml` → `agents/catalog.json` plus
`agents/omg-*.md` by `scripts/generate_agents_catalog.py` (which also writes
these projections). `scripts/generate_antigravity_agent_projections.py`
remains as a projection-only helper. Frontmatter maps OMG spawn/capability
floors onto documented AG keys (`mainAgent`, `subagent`,
`commandExecutionPolicy`). OMG does **not** claim Antigravity honors those
fields at runtime. Live AG smoke is **not** claimed.

Regenerate:

```bash
python scripts/generate_agents_catalog.py
python scripts/generate_agents_catalog.py --check
```
