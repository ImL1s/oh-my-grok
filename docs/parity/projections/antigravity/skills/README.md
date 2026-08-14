# Antigravity skill projections

**Status:** static parity projection for
[#70](https://github.com/ImL1s/oh-my-grok/issues/70).

These files are **not**:

- an installed Antigravity plugin
- live AG evidence
- proof that `agy` install or skill discovery works
- a Grok UserPromptSubmit injector

They are generated from `skills/catalog.json` plus `skills/omg-*/SKILL.md` by
`scripts/generate_antigravity_skill_projections.py`. Only the 16 Grok plugin
skills are projected. Catalog-only / alias / deferred workflows have no AG file.

Regenerate:

```bash
python scripts/generate_antigravity_skill_projections.py
python scripts/generate_antigravity_skill_projections.py --check
```
