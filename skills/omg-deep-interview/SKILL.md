---
name: omg-deep-interview
description: >
  Deterministic Socratic requirements gate before plan/implement. Use when user
  says deep interview, clarify requirements, ambiguity gate, interview start,
  or vague multi-day task needs structured intake. CLI owns interview state.
---

# omg-deep-interview — requirements first

Use when the request is vague, brownfield, or missing acceptance. The **`omg`
CLI** owns interview state and the final stamped spec; agents propose wording
only.

## HARD RULES

1. Until interview `status` is `complete`, **do not plan or implement product code**.
2. Fan-out only via Grok `spawn_subagent` (depth=1); gather repo facts **read-only**.
3. Never write authoritative interview state files by hand — only `omg interview *`.
4. Never self-declare completion or write `passes`/`verified`.
5. Always surface the CLI `resume_command` to the human when `waiting_input`.
6. Cancel with `omg cancel` if abandoning the run.

## Use when

- User says `deep interview`, `clarify`, `requirements gate`, `interview`, ambiguity.
- Autopilot phase `interview` or before `omg ralplan` on vague goals.
- Brownfield work needing scope/constraints/non-goals.

## Do not use when

- Task is already fully labeled (Intent/Outcome/…/Acceptance) and user wants skip → still need pressure-pass/close path, or autopilot `--skip-interview` only when requirements already closed.
- Pure code fix with clear acceptance → work or ralph directly.

## Session playbook (Socratic)

### 0. Start

```bash
omg interview start "TASK" --profile standard
# profiles: quick | standard | deep
omg interview status --run RUN
```

### 1. Answer loop

While pending question:

1. Optionally `spawn_subagent` explore **read-only** for discoverable repo facts.
2. Draft answer; submit via CLI:

```bash
omg interview answer --run RUN --question-id QUESTION --text "..."
```

3. Print/follow `resume_command` from CLI output every round.

### 2. Pressure pass (mandatory)

Even when numeric threshold passes:

```bash
omg interview pressure-pass --run RUN --text "assumptions, trade-offs, risks"
```

### 3. Close → plan

```bash
omg interview close --run RUN
# then ralplan / autopilot transition
omg ralplan "..."
# or autopilot:
omg autopilot transition --run RUN --phase ralplan \
  --evidence-json '{"interview_complete":true}' --reason "interview closed"
```

## Contract

- Profiles: `quick` (threshold ~.30, 5 rounds), `standard` (~.20, 12), `deep` (~.15, 20).
- Dimensions: intent, outcome, scope, constraints, success, context (+ non-goals, decision boundaries, acceptance).
- One pending question per round targeting weakest dimension.
- Labeled zero-question intake allowed when task text includes labels; **pressure pass still required**.
- Wrong-run, stale-question, corrupt-state, artifact-hash mismatches **fail closed**.

## Labeled zero-question intake

Provide labels (one per line or `;`-separated): `Intent`, `Outcome`, `Scope`,
`Constraints`, `Success`, `Context`, `Non-goals`, `Decision boundaries`, `Acceptance`.

## Continuity

- Mid-interview session end → `omg resume` or `omg interview status --run RUN`.
- SessionStart may write `.omg/state/RESUME.md` — read it first (see `omg-using`).

## Anti-patterns

- Parallel chat “interview” without CLI stamps
- Implementing during `waiting_input`
- Skipping pressure-pass
- Lying in evidence-json for autopilot skip

## Related

- `omg-autopilot` · `omg-ralplan` · `omg-using` · `omg resume`

## CLI

```bash
omg interview start "task" --profile standard
omg interview status --run RUN
omg interview answer --run RUN --question-id QUESTION --text "answer"
omg interview pressure-pass --run RUN --text "..."
omg interview close --run RUN
```
