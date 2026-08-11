# Team workers — job topology + replacement + presentation (#69 PR4/PR5/PR6)

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

# Leader-only replacement attempt (catalog v2+):
omg team api replace-worker --input '{
  "run_id":"RUN","team_id":"team","worker":"t1","mode":"lost",
  "expected_attempt":1,"expected_launch_generation":1,
  "idempotency_key":"repl-1"
}'

# Presentation State V1 (catalog v3; identical via MCP projection=presentation.v1):
omg team status --run RUN --presentation --json
omg team api read-presentation-state --input '{"run_id":"RUN","team_id":"team"}'
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
  **never** claim tokens). Route descriptors are preserved on the prior
  attempt and restamped on the live row.
- Attempt and `launch_generation` advance by exactly one under the lifecycle
  lock + CAS.
- Crash-safe WAL + idempotency adoption; resume recovers pending replacement
  **before** claim reconcile / job bind.
- Leader-only (catalog ACL). Never sets `verified`.

Hermetic / fixture-proven for pane + fake-job. Antigravity uses the existing
provider/Jobs path structurally but has **no live proof** in this PR.

## Presentation State V1 (#69 PR6)

`build_team_presentation_v1()` is a pure read-only, generation-fenced
projection of team.json + ownership + bindings + startup + prior_attempts.
See `docs/team-presentation-state-v1.md`. Default locked/`--full` status
schemas are unchanged. Catalog **v3** adds leader-only
`read-presentation-state`. MCP `team_status.read` accepts optional
`projection=presentation.v1`.

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

New start/scale also stamp an additive `route` descriptor
(`kind=external_executor`); legacy rows without it present as `unknown`.

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
9. Presentation projection never probes tmux/Jobs/network and never writes
   state or `verified`.

## Non-goals (this slice)

- No live Antigravity proof / `live_*` maturity claims
- No Hyperplan **execution** / Security Research **composition execution** /
  model synthesis / PoC running
  (Security Research hermetic result production landed in PR9;
  Hyperplan hermetic result production landed in PR10)
- No automatic replacement policy / retry scheduler / attempt budgets
- No pane↔job migration during replacement
- No TUI / native execution path
- Does **not** close #69

## Hyperplan V1 (hermetic produce)

See `docs/team-hyperplan-v1.md`. Contract + hermetic result production landed;
execution remains open (`execution_supported=false`).

```bash
omg team hyperplan plan --spec SPEC.json --json
omg team hyperplan materialize --spec SPEC.json --run RUN
omg team hyperplan validate-decision --run RUN --input DECISION.json
omg team hyperplan produce-decision --run RUN --input RESULT_BUNDLE.json
```

## Security Research V1 (hermetic produce)

See `docs/team-security-research-v1.md`. Contract + hermetic result production
landed; composition execution / PoC running remain open
(`execution_supported=false`).

```bash
omg team security-research plan --spec SPEC.json --json
omg team security-research materialize --spec SPEC.json --run RUN
omg team security-research validate-report --run RUN --input REPORT.json
omg team security-research produce-report --run RUN --input RESULT_BUNDLE.json
```

Refs #69.
