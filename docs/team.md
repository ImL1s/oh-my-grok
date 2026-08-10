# Team workers — job topology + replacement attempts (#69 PR4/PR5)

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

# Leader-only replacement attempt (catalog v2):
omg team api replace-worker --input '{
  "run_id":"RUN","team_id":"team","worker":"t1","mode":"lost",
  "expected_attempt":1,"expected_launch_generation":1,
  "idempotency_key":"repl-1"
}'
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

## Replacement attempts (#69 PR5)

`replace-worker` replaces a lost, failed, or explicitly restarted worker with
a **new attempt** on the same logical worker/task slot:

- Modes: `lost` | `failed` | `restart` (restart requires exact successful
  fencing of a still-running old handle).
- Request fences: `run_id`, `team_id`, worker/task binding,
  `expected_attempt`, `expected_launch_generation`, `idempotency_key`.
- Same topology / provider / role / worktree / logical slot; new physical
  execution identity via existing `launch_worker()` (pane|job).
- Old execution archived under `prior_attempts` (handle/evidence only —
  **never** claim tokens).
- Attempt and `launch_generation` advance by exactly one under the lifecycle
  lock + CAS.
- Crash-safe WAL + idempotency adoption; resume recovers pending replacement
  **before** claim reconcile / job bind.
- Leader-only (catalog ACL). Never sets `verified`.

Hermetic / fixture-proven for pane + fake-job. Antigravity uses the existing
provider/Jobs path structurally but has **no live proof** in this PR.

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
   are refused on stamp (never healed by overwrite). Prior handles are
   immutable history after replacement.
2. Missing Jobs metadata after start → launch fails (never fabricate Team state).
3. Job terminal states complete a task only when **non-empty** claim tokens
   match, plus attempt, launch generation, and worker ownership; `None`/`None`
   is rejected (`claim_token_required`), never soft success.
4. Stale attempt / launch-generation completions are ignored.
5. Cancel goes Team → Jobs cancel → Team task update only for **successful**
   cancels. Failed Jobs cancel → Team does **not** claim `stop_state=stopped`
   / blanket cancelled (no desync while Job still runs). Replacement restart
   similarly refuses launch when identity-bound cancel fails.
6. Leader resume binds existing jobs from Team state + Jobs metadata (no PID
   inspection alone; no duplicate launch). Binder `team_id` must match Jobs
   `request.team_id` (missing stamp → `foreign_team_job`). Pending replacement
   WAL is recovered before claim reconcile.
7. Unknown job → `UNPROVEN` (never synthesize success).
8. Topology cannot mutate in place (`pane` → `job` requires a new launch
   generation). Replacement never migrates pane↔job.

## Non-goals (this slice)

- No live Antigravity proof / `live_*` maturity claims
- No Hyperplan / security compositions / presentation state
- No automatic replacement policy / retry scheduler / attempt budgets
- No pane↔job migration during replacement
- Does **not** close #69

Refs #69.
