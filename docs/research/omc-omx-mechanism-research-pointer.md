# OMC/OMX mechanism research (pointer)

Canonical deep research (external teamwork artifact):

`~/teamwork_projects/omc_omx_research/omc_omx_mechanism_research.md`

Summary themes: R1 feature inventory (skills/agents/hooks), R2 “don’t stop until
done” + three continuity pillars, R3 false-APPROVE (negation/fences) + exit-code
override, R4 0.3.x roadmap (P0 safety/continuity → P1 interview/QA → P2 LSP/wiki).

## OMG actions (implemented)

| Research | OMG action |
|----------|------------|
| R3 fence + expanded negation false-green | `omg_cli/verdict.py` prose harden + tests |
| R3 Exit Code Override Law | `apply_stage_exit_codes` + dual_review |
| R3 structured verdict schema + run_id | `schema_version: 2` + `expected_run_id` in `parse_verdict` |
| R1/R4 session ultragoal | `skills/omg-ultragoal` |
| R2 `omg resume` + RESUME.md + SessionStart | `omg_cli/resume.py`, `hooks/bin/session_start.py`, `omg resume` |
| R2 louder pack | resume MD + `omg hud` |
| P1 deep interview / ultraqa session | thick `omg-deep-interview` / `omg-ultraqa` skills (CLI already present) |
| P2 wiki | `omg wiki` + `skills/omg-wiki` → `.omg/wiki/` |
| P2 HUD | `omg hud` + `skills/omg-hud` |
| P2 LSP | `omg lsp status/check` + `skills/omg-lsp` (honest: no host MCP LSP) |

## Still not host-feasible

- OMC-style Stop hook **veto** (`decision: block`) on Grok — Stop remains passive.
- Full OMC MCP LSP/AST bridge (54 tools) — use Grok grep/read + optional local pyright.
