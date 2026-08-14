# Antigravity agent.md projections

**Status:** static parity projection for
[#71](https://github.com/ImL1s/oh-my-grok/issues/71).

These files are **not**:

- an installed Antigravity plugin
- live AG evidence
- proof that `agy` install or `/agents` discovery works
- dual-host routing runtime ([#131](https://github.com/ImL1s/oh-my-grok/issues/131))

They are generated from `agents/catalog.json` plus `agents/omg-*.md` by
`scripts/generate_antigravity_agent_projections.py`. Frontmatter maps OMG
spawn/capability floors onto documented AG keys (`mainAgent`, `subagent`,
`commandExecutionPolicy`). OMG does **not** claim Antigravity honors those
fields at runtime.

Regenerate:

```bash
python scripts/generate_antigravity_agent_projections.py
python scripts/generate_antigravity_agent_projections.py --check
```
