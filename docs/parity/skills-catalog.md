# Skill parity catalog

Generated from [`skills/catalog.json`](../../skills/catalog.json). Do not hand-edit this table.

**Wave A first cut (#70):** inventory + loader + aliases + resource confinement + continuation policy. The 16 Grok plugin playbooks stay the in-session surface. Catalog-only rows are classified; they are **not** live-verified and must not set `verified`.

Antigravity files under `docs/parity/projections/antigravity/skills/` are **projections only**.

| ID | Kind | Classification | Owner | CLI twin | Status | Live | Continuation |
|----|------|----------------|-------|----------|--------|------|--------------|
| `omg-ai-slop-cleaner` | canonical | `omg_native` | `omg-cli` | `—` | `deferred` | `unproven` | `none` |
| `omg-ask` | plugin | `omg_native` | `omg-cli` | `ask` | `plugin` | `unproven` | `none` |
| `omg-autopilot` | plugin | `omg_native` | `omg-cli` | `autopilot` | `plugin` | `unproven` | `owner` |
| `omg-autoresearch` | canonical | `omg_native` | `omg-cli` | `—` | `deferred` | `unproven` | `none` |
| `omg-autoresearch-goal` | canonical | `omg_native` | `omg-cli` | `goal` | `deferred` | `unproven` | `none` |
| `omg-best-practice-research` | canonical | `omg_native` | `omg-cli` | `ask` | `configured` | `unproven` | `none` |
| `omg-build-fix` | canonical | `omg_native` | `omg-cli` | `qa` | `configured` | `unproven` | `none` |
| `omg-cancel` | plugin | `omg_native` | `omg-cli` | `cancel` | `plugin` | `unproven` | `none` |
| `omg-comment-checker` | canonical | `omg_native` | `omg-cli` | `—` | `deferred` | `unproven` | `none` |
| `omg-configure-notifications` | canonical | `omg_native` | `omg-cli` | `notify` | `configured` | `unproven` | `none` |
| `omg-deep-dive` | canonical | `omg_native` | `omg-cli` | `—` | `configured` | `unproven` | `none` |
| `omg-deep-interview` | plugin | `omg_native` | `omg-cli` | `interview` | `plugin` | `unproven` | `none` |
| `omg-deepinit` | canonical | `omg_native` | `omg-cli` | `setup` | `configured` | `unproven` | `none` |
| `omg-design` | canonical | `omg_native` | `omg-cli` | `—` | `configured` | `unproven` | `none` |
| `omg-dual-review` | plugin | `omg_native` | `omg-cli` | `dual-review` | `plugin` | `unproven` | `none` |
| `omg-ecomode` | canonical | `omg_native` | `omg-cli` | `—` | `configured` | `unproven` | `none` |
| `omg-external-context` | canonical | `omg_native` | `omg-cli` | `memory` | `configured` | `unproven` | `none` |
| `omg-git-master` | canonical | `host_owned` | `host` | `—` | `catalogued` | `unproven` | `none` |
| `omg-hud` | plugin | `omg_native` | `omg-cli` | `hud` | `plugin` | `unproven` | `none` |
| `omg-hyperplan` | canonical | `omg_native` | `team` | `team` | `deferred` | `unproven` | `none` |
| `omg-lsp` | plugin | `host_owned` | `host` | `lsp` | `plugin` | `unproven` | `none` |
| `omg-mcp-setup` | canonical | `omg_native` | `omg-cli` | `mcp-install` | `configured` | `unproven` | `none` |
| `omg-parallel-research` | canonical | `omg_native` | `omg-cli` | `ask` | `configured` | `unproven` | `none` |
| `omg-pipeline` | plugin | `omg_native` | `omg-cli` | `pipeline` | `plugin` | `unproven` | `owner` |
| `omg-project-session-manager` | canonical | `omg_native` | `omg-cli` | `session` | `configured` | `unproven` | `none` |
| `omg-prometheus-strict` | canonical | `omg_native` | `omg-cli` | `ralplan` | `deferred` | `unproven` | `none` |
| `omg-ralph` | plugin | `omg_native` | `omg-cli` | `ralph` | `plugin` | `unproven` | `owner` |
| `omg-ralph-init` | canonical | `omg_native` | `omg-cli` | `ralph` | `configured` | `unproven` | `none` |
| `omg-ralplan` | plugin | `omg_native` | `omg-cli` | `ralplan` | `plugin` | `unproven` | `owner` |
| `omg-release` | canonical | `omg_native` | `omg-cli` | `parity` | `configured` | `unproven` | `none` |
| `omg-security-research` | canonical | `omg_native` | `team` | `team` | `deferred` | `unproven` | `none` |
| `omg-security-review` | canonical | `omg_native` | `omg-cli` | `review` | `configured` | `unproven` | `none` |
| `omg-self-improve` | canonical | `excluded` | `none` | `—` | `excluded` | `none` | `none` |
| `omg-skill` | canonical | `omg_native` | `omg-cli` | `skill` | `configured` | `unproven` | `none` |
| `omg-tdd` | canonical | `omg_native` | `omg-cli` | `qa` | `configured` | `unproven` | `none` |
| `omg-team` | plugin | `omg_native` | `team` | `team` | `plugin` | `unproven` | `owner` |
| `omg-trace` | canonical | `omg_native` | `omg-cli` | `tracker` | `configured` | `unproven` | `none` |
| `omg-ultragoal` | plugin | `omg_native` | `omg-cli` | `goal` | `plugin` | `unproven` | `owner` |
| `omg-ultraqa` | plugin | `omg_native` | `omg-cli` | `qa` | `plugin` | `unproven` | `owner` |
| `omg-ultrawork` | plugin | `omg_native` | `omg-cli` | `ulw` | `plugin` | `unproven` | `owner` |
| `omg-using` | plugin | `omg_native` | `omg-cli` | `doctor` | `plugin` | `unproven` | `none` |
| `omg-visual-ralph` | canonical | `omg_native` | `omg-cli` | `ralph` | `deferred` | `unproven` | `none` |
| `omg-visual-verdict` | canonical | `omg_native` | `omg-cli` | `—` | `deferred` | `unproven` | `none` |
| `omg-wiki` | plugin | `omg_native` | `omg-cli` | `wiki` | `plugin` | `unproven` | `none` |
| `omg-writer-memory` | canonical | `omg_native` | `omg-cli` | `memory` | `configured` | `unproven` | `none` |
| `code-review` | alias | `alias` | `none` | `—` | `catalogued` | `none` | `none` |
| `goal` | alias | `alias` | `none` | `—` | `catalogued` | `none` | `none` |
| `init-deep` | alias | `alias` | `none` | `—` | `catalogued` | `none` | `none` |
| `plan` | alias | `alias` | `none` | `—` | `catalogued` | `none` | `none` |
| `psm` | alias | `alias` | `none` | `—` | `catalogued` | `none` | `none` |
| `sciomc` | alias | `alias` | `none` | `—` | `catalogued` | `none` | `none` |
| `skillify` | alias | `alias` | `none` | `—` | `catalogued` | `none` | `none` |
| `ulw-loop` | alias | `alias` | `none` | `—` | `catalogued` | `none` | `none` |

## Host-native protection

These names cannot be Grok plugin directories and cannot silently replace host slash commands:

`agents`, `compact`, `goal`, `help`, `loop`, `mcp`, `plan`, `plugin`, `skills`

`plan` and `goal` exist only as **aliases** (`host_native_protected`) resolving to `omg-ralplan` / `omg-ultragoal`.

## Continuation authority

Exactly one of: `omg-autopilot`, `omg-pipeline`, `omg-ralph`, `omg-ralplan`, `omg-team`, `omg-ultragoal`, `omg-ultraqa`, `omg-ultrawork`

Conflicts resolve to `refuse`, `adopt_existing`, or `artifact_only` (`omg_cli.skills_catalog.resolve_continuation`). Cancel/using always adopt. Wiki/HUD/LSP/ask are artifact-only under an active loop.

