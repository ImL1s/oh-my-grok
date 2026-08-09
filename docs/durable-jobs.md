# Durable jobs (#68)

Canonical background-job plane under `.omg/jobs/<job-id>/`. The `omg` CLI is
the only writer of job state; agents never stamp `passes` / `verified` from
jobs. Evidence stays in job artifacts.

## Commands

```bash
omg job start --provider fake|antigravity --prompt-file task.md [--attempt-budget N]
omg job status|wait|collect|cancel|list …
omg job recover JOB_ID [--dry-run]          # reconcile expired/abandoned → lost
omg job recover --all [--run RUN_ID] [--provider fake|antigravity] [--dry-run]
omg job retry JOB_ID --attempt N            # exact next attempt (explicit)
omg job auto-retry JOB_ID [--dry-run]       # bounded scheduler tick (one job)
omg job auto-retry --all [--run RUN_ID] [--provider fake|antigravity] \
  [--limit N] [--dry-run]                   # one-pass batch tick (default limit 1, max 32)
omg job gc --retention-days N               # terminal jobs only
omg ask fake|agy "…" --background           # thin seam → durable job; returns job_id
```

Public start admits `fake` and `antigravity` only. Internal `grok-acp-session`
is Team/ACP-sidecar only and cannot be retried or recovered via the public
`omg job` CLI.

## Owner lease / observation (#68 PR4)

- `starting → running` commits PID/PGID/handle **and** a fresh owner lease in
  one write. The runner heartbeats the lease; terminal/cancel/lost releases it
  in the same write.
- Durable `state=running` is **not** a health claim. `omg job status|list|wait`
  expose a separate `observation.health` (for example `running_healthy`,
  `lease_stale_live`, `recoverable_lost`, `identity_unproven`).
- Owner tokens never appear in public status, list, CLI errors, or docs
  examples.

## Recovery → lost → explicit retry

1. Lease expires (plus clock-skew grace) **and** recorded runner/provider
   identities are proven gone/reused.
2. `omg job recover` CAS-marks the job `lost` (never relaunches; never signals
   a live or reused PID).
3. Reclaim only via existing `omg job retry JOB_ID --attempt current+1`
   (or, when classification is `automatic` and due, `omg job auto-retry`).

Live/unproven identities block recovery (`E_JOB_RECOVERY_UNPROVEN` /
`E_JOB_RECOVERY_ORPHAN_LIVE`). A live inner provider with a dead outer runner
is an orphan — use `omg job cancel`, not recover. `--dry-run` observes without
writes or signals. Recovery never auto-relaunches.

## Auto-retry scheduler (#68 PR5)

Caller-driven **one-pass** tick — not a resident daemon. Cron, a Team leader,
or an orchestration loop may invoke it periodically; PR5 does not install a
service.

Eligibility (all required):

- `state=failed` with persisted **and** recomputed `retry_class=automatic`
- no cancel markers; exact next attempt within `attempt_budget`
- timezone-aware `terminal_at` within clock-skew; deterministic exponential
  backoff (`10s × 2^(attempt-1)`, capped at `300s`) has elapsed
- prior runner/provider identity proven gone/reused (same gates as explicit
  retry — live/unproven/spawn-uncertain/provider-unbound block)
- stored provider request revalidates before attempt consumption

Every mutation goes through `retry_job(intent=automatic)` → existing
`launch_job_runner`. `--dry-run` runs full admission without archive, state
change, or launch. `--limit` (default 1, max 32) bounds due candidates
processed per tick. Project lock: `.omg/jobs/.locks/auto-retry.lock`.

Never auto-retries `cancelled`, `lost`, `manual_only`, `unknown`, `never`, or
nonterminal jobs. Never calls `recover`. Never signals processes.

## Retry / attempt budget

- `attempt_budget` is immutable after start (schema v1 additive fields).
- Explicit retry requires `--attempt` == current+1; budget exhaustion fails closed.
- Prior attempt evidence is archived under `attempts/NNNN/` (never overwritten);
  archives may record `retry_dispatch` provenance (`explicit` | `automatic`).
- Retry classification (`automatic` | `manual_only` | `never` | `unknown`) is
  stamped for operators; explicit retry remains available for
  `manual_only` / `unknown` / `lost` / `cancelled` where admitted.
- Retry permits verified reused historical identities; blocks live/unproven.

## GC / retention

- `omg job gc --retention-days N` deletes **terminal** jobs older than retention
  (`terminal_at`, else `updated_at` / `created_at`).
- Never deletes queued/starting/running jobs; refuses active owner leases,
  ACP-bound jobs, live/unproven identities, and malformed records.

## `omg ask --background`

Creates a durable job via the existing `start_job` / runner path and returns
`job_id` immediately. Synchronous `omg ask` is unchanged by default.
Background admits `fake` and `agy` only (maps to jobs `fake` / `antigravity`).

## Recovery / privacy

- After leader restart: `omg job status|wait|collect|cancel|recover|auto-retry`
  against durable `job.json` + recorded identities.
- Large outputs stay in `artifacts/`; status/collect return descriptors only.
- Jobs never grant `verified`.

## Open follow-ups (#68 remains open)

Authenticated live Antigravity evidence and Team job-backed workers (#69)
remain open after this PR5 auto-retry scheduler slice. #68 stays open.
