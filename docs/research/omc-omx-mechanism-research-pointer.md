# OMC/OMX mechanism research (pointer)

Canonical deep research (external teamwork artifact):

`/Users/iml1s/teamwork_projects/omc_omx_research/omc_omx_mechanism_research.md`

Summary themes: R1 feature inventory (skills/agents/hooks), R2 “don’t stop until
done” + three continuity pillars, R3 false-APPROVE (negation/fences) + exit-code
override, R4 0.3.x roadmap (P0 safety/continuity → P1 interview/QA → P2 LSP/wiki).

## OMG actions derived from R3/R4 P0

| Research | OMG action |
|----------|------------|
| R3 fence + expanded negation false-green | `omg_cli/verdict.py` prose harden + `tests/test_verdict.py` |
| R3 Exit Code Override Law | Already in `apply_stage_exit_codes`; regression locked in tests |
| R1/R4 session ultragoal | `skills/omg-ultragoal` playbook + `omg-using` route |
| R2 resume / SessionStart / RESUME.md | **Deferred** to 0.3.x (design only here) |
| P2 wiki / HUD / LSP | Out of scope for this plan |

## Deferred (honest)

- `omg resume` smart routing + RESUME.md workspace inject
- Host-equivalent of OMC Stop veto (not feasible on Grok Stop today)
- Structured run_id JSON verdict schema beyond prose harden + existing JSON field
