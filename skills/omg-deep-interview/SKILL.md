---
name: omg-deep-interview
description: Deterministic, resumable requirements convergence before planning or implementation.
---

# omg-deep-interview — requirements first

Use this skill when a request is vague, brownfield, or missing explicit
boundaries and acceptance. The `omg` CLI owns the interview state and final
specification; an agent may provide repository facts or suggested wording, but
must not write authoritative interview state.

## Contract

- One pending question per round, always targeting the weakest unresolved
  clarity dimension after intent-first stage priority.
- Profiles: `quick` (threshold `.30`, 5 rounds), `standard` (`.20`, 12), and
  `deep` (`.15`, 20).
- Brownfield scoring uses intent, outcome, scope, constraints, success, and
  context. Repository evidence is gathered before asking for discoverable facts.
- Non-goals, decision boundaries, acceptance, and one explicit pressure pass
  are mandatory even when the numeric threshold passes.
- A clear labeled task may take a zero-question path, but it still requires a
  pressure pass before close.
- Until `status` is `complete`, do not plan or implement. `waiting_input`
  always includes the exact resume command.
- Wrong-run, stale-question, corrupt-state, and artifact-hash mismatches fail
  closed.

## CLI

```bash
omg interview start "task" --profile standard
omg interview status --run RUN
omg interview answer --run RUN --question-id QUESTION --text "answer"
omg interview pressure-pass --run RUN --text "assumption and trade-off result"
omg interview close --run RUN
```

`start`, `answer`, and `pressure-pass` print the current state and an exact
`resume_command`. The final CLI-stamped JSON spec and transcript live under the
run `stages/` directory; a human-readable projection is written under
`.omg/plans/`.

## Labeled zero-question intake

For already-clear work, the task text may provide these labels (one per line or
separated by semicolons): `Intent`, `Outcome`, `Scope`, `Constraints`,
`Success`, `Context`, `Non-goals`, `Decision boundaries`, and `Acceptance`.
The CLI validates completeness and ambiguity; labels do not bypass the pressure
pass or close gate.

## Hard rules

- Do not invoke an LLM from this primitive and do not build a parallel chat UI.
- Do not mutate product code during an incomplete interview.
- Do not self-declare completion or write `passes`/`verified`; only the CLI may
  stamp artifacts and lifecycle state.
- Model/agent prose is a proposal. It becomes authoritative only through the
  CLI command path.
