# oh-my-grok README translations

English | [简体中文](./README.zh.md) | [繁體中文](./README.zh-TW.md)

This folder holds the localized README files for oh-my-grok.

The repository root keeps only the canonical [`README.md`](../../README.md) so the top level stays focused on the primary entry point, package metadata, and project-wide documents.

## Available translations

| Language | File |
| --- | --- |
| English | [../../README.md](../../README.md) |
| 简体中文 | [README.zh.md](./README.zh.md) |
| 繁體中文 | [README.zh-TW.md](./README.zh-TW.md) |

## Translated docs

| Topic | English | 简体中文 | 繁體中文 |
| --- | --- | --- | --- |
| Docs index | [../README.md](../README.md) | [../README.zh.md](../README.zh.md) | [../README.zh-TW.md](../README.zh-TW.md) |
| Skills catalog | [../skills.md](../skills.md) | [../skills.zh.md](../skills.zh.md) | [../skills.zh-TW.md](../skills.zh-TW.md) |
| Autopilot | [../autopilot.md](../autopilot.md) | [../autopilot.zh.md](../autopilot.zh.md) | [../autopilot.zh-TW.md](../autopilot.zh-TW.md) |
| Workflows | [../workflows.md](../workflows.md) | [../workflows.zh.md](../workflows.zh.md) | [../workflows.zh-TW.md](../workflows.zh-TW.md) |
| Security model | [../security-model.md](../security-model.md) | [../security-model.zh.md](../security-model.zh.md) | [../security-model.zh-TW.md](../security-model.zh-TW.md) |
| Release protocol | [../RELEASE.md](../RELEASE.md) | [../RELEASE.zh.md](../RELEASE.zh.md) | [../RELEASE.zh-TW.md](../RELEASE.zh-TW.md) |

## Maintenance rules

* Treat [`../../README.md`](../../README.md) as the canonical source.
* Add new README translations in this folder, not at the repository root.
* Keep the language list synchronized between the canonical README and each localized variant.
* Keep relative links valid from `docs/readme/`.
* Prefer updating existing translations instead of introducing duplicate files or alternate naming schemes (use `.zh.md` / `.zh-TW.md` only — never `.zh-Hant.md`).
* Agent/skill contracts (`AGENTS.md`, `skills/*/SKILL.md`) stay English; only human-facing catalog links should point here.

## Related docs

* The canonical project entry point remains [`../../README.md`](../../README.md).
* Locale policy for contributors: see [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md).
