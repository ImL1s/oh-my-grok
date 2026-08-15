# Antigravity skill projections

**Status:** static parity projection for
[#70](https://github.com/ImL1s/oh-my-grok/issues/70).

These files are **not**:

- an installed Antigravity plugin
- live AG evidence
- proof that `agy` install or skill discovery works
- a Grok UserPromptSubmit injector

They are generated from `skills/catalog.json` plus `skills/omg-*/SKILL.md` by
`scripts/generate_antigravity_skill_projections.py`. Dual-host **routing**
consumes the same catalog: Grok global rules fill `<workflow_routing>` from
triggers/aliases; these AG files are projections of the playbook body only.

Catalog-only / alias / excluded rows have no AG file. Playbooks without live
smoke stay `configured`, not `verified`.

Regenerate:

```bash
python scripts/generate_antigravity_skill_projections.py
python scripts/generate_antigravity_skill_projections.py --check
```
