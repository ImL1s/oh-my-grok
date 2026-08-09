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

## Fail-closed invariants

1. Exactly one execution handle (pane XOR job).
2. Missing Jobs metadata after start → launch fails (never fabricate Team state).
3. Job terminal states complete a task only when claim token, attempt, and
   worker ownership match; otherwise ignored.
4. Stale attempt completions are ignored.
5. Cancel goes Team → Jobs cancel → Team task update (never reverse ownership).
6. Leader resume binds existing jobs from Team state + Jobs metadata (no PID
   inspection alone; no duplicate launch).
7. Unknown job → `UNPROVEN` (never synthesize success).
8. Topology cannot mutate in place (`pane` → `job` requires a new launch
   generation).

## Non-goals (this slice)

- No live Antigravity proof / `live_*` maturity claims
- No Hyperplan / security compositions / presentation state
- No replacement attempts
- Does **not** close #69

Refs #69.
