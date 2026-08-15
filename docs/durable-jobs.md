# Durable jobs (#68)

Canonical background-job plane under `.omg/jobs/<job-id>/`. The `omg` CLI is
the only writer of job state; agents never stamp `passes` / `verified` from
jobs. Evidence stays in job artifacts.

## Commands

```bash
omg job start --provider fake|antigravity|grok --prompt-file task.md [--attempt-budget N]
omg job status|wait|collect|cancel|list …
omg job recover JOB_ID [--dry-run]          # reconcile expired/abandoned → lost
omg job recover --all [--run RUN_ID] [--provider fake|antigravity|grok] [--dry-run]
omg job retry JOB_ID --attempt N            # exact next attempt (explicit)
omg job auto-retry JOB_ID [--dry-run]       # bounded scheduler tick (one job)
omg job auto-retry --all [--run RUN_ID] [--provider fake|antigravity|grok] \
  [--limit N] [--dry-run]                   # one-pass batch tick (default limit 1, max 32)
omg job gc --retention-days N               # terminal jobs only
omg ask fake|agy "…" --background           # thin seam → durable job; returns job_id
```

Public start admits `fake`, `antigravity`, and `grok`. Internal `grok-acp-session`
is Team/ACP-sidecar only and cannot be retried or recovered via the public
`omg job` CLI. Grok jobs are **headless** (`grok --prompt-file --cwd
--output-format plain`); they are not interactive TTY owners and never
fabricate `TUI_READY` / `PROVIDER_ECHO`.

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
`E_JOB_RECOVERY_ORPHAN_LIVE`). Claimed provider launch/bind without a complete
durable PID/PGID (`launching` unbound or `bound` incomplete), present-but-
malformed `spawn_identity.json` (including mismatched embedded `job_id`), and
non-strict identity types (bool/float PID/PGID, non-string fingerprints) in any
recorded state including `exited` are `IDENTITY_UNPROVEN`, never
provider-absent. A live inner provider with a dead outer runner is an orphan —
use `omg job cancel`, not recover. `--dry-run` observes without writes or
signals. Recovery never auto-relaunches.

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
  retry — live/unproven/spawn-uncertain/provider-unbound /
  bound-but-incomplete / present-but-malformed `spawn_identity.json` /
  non-strict PID/PGID/fingerprint types including on `exited` block)
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

## Open follow-ups (owned by #69)

Authenticated live Antigravity evidence, live grok job smoke, and live
Team job-backed workers remain open under #69. Hermetic grok job provider
admission landed; #68 is closed; do not treat it as a current blocker.
