# Team job-backed workers (#69 PR4)

Team launches workers through one execution abstraction:

```text
Team Task
   │
   ▼
launch_worker(...)
    │
    ├── topology=pane  → tmux pane (existing default)
    └── topology=job   → durable Jobs plane (#68)
```

Task lifecycle (claims, mailbox, ownership) stays on Team. Process lifecycle
for job-backed workers is owned by Jobs (start / retry / cancel / heartbeat /
terminal state). Team persists only durable execution references.

## CLI

```bash
omg team start --goal "…" --tasks-json '[…]' --worker-topology=pane   # default
omg team start --goal "…" --tasks-json '[…]' --worker-topology=job
omg team run   --goal "…" --tasks-json '[…]' --worker-topology=job
omg team launch --workers N --goal "…" --worker-topology=job
```

`--worker-topology=job` requires a Jobs-admitted provider (`fake` or
`antigravity`, or `executor=fixture` → `fake`). Zero-config `grok` remains
pane-only.

Dry-run / materialize-only writes launch descriptors and never creates a
pane, subprocess, or job.

### `omg team run` + job topology (honesty)

Job-backed `team run` / team-exec does **not** pane-wait. It observes Jobs
health (`observe_job_for_task`) then proceeds to `collect` (seal/integrate
remain fail-closed on unsealed worktrees). It does **not** auto-call
`apply_job_completion` — terminal promotion requires non-empty matching claim
tokens (see below). No `live_*` / Antigravity live evidence is claimed.

## Status

Aggregate status exposes topology per worker (not part of the locked
`--json` freeze):

```text
worker:
  topology: pane

worker:
  topology: job
  job_id: 20260809T120000Z-abcd1234
```

## Execution record

Persisted under each `team.json` task as:

```json
{
  "schema": 1,
  "topology": "job",
  "launch_generation": 1,
  "job_id": "…"
}
```

Exactly one of `job_id` / `pane_id` may exist on a live handle. Never both.
Team never persists PID / PGID / subprocess objects for job-backed workers.

Job launches stamp `team_id` onto the Jobs immutable `request` so resume bind
can refuse foreign jobs in the same project root.

## Fail-closed invariants

1. Exactly one execution handle (pane XOR job). Corrupt dual-id prior records
   are refused on stamp (never healed by overwrite).
2. Missing Jobs metadata after start → launch fails (never fabricate Team state).
3. Job terminal states complete a task only when **non-empty** claim tokens
   match, plus attempt and worker ownership; `None`/`None` is rejected
   (`claim_token_required`), never soft success.
4. Stale attempt completions are ignored.
5. Cancel goes Team → Jobs cancel → Team task update only for **successful**
   cancels. Failed Jobs cancel → Team does **not** claim `stop_state=stopped`
   / blanket cancelled (no desync while Job still runs).
6. Leader resume binds existing jobs from Team state + Jobs metadata (no PID
   inspection alone; no duplicate launch). Binder `team_id` must match Jobs
   `request.team_id` (missing stamp → `foreign_team_job`).
7. Unknown job → `UNPROVEN` (never synthesize success).
8. Topology cannot mutate in place (`pane` → `job` requires a new launch
   generation).

## Non-goals (this slice)

- No live Antigravity proof / `live_*` maturity claims
- No Hyperplan / security compositions / presentation state
- No replacement attempts
- Does **not** close #69

Refs #69.
