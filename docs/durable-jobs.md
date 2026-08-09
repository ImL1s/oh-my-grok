# Durable jobs (#68)

Canonical background-job plane under `.omg/jobs/<job-id>/`. The `omg` CLI is
the only writer of job state; agents never stamp `passes` / `verified` from
jobs. Evidence stays in job artifacts.

## Commands

```bash
omg job start --provider fake|antigravity --prompt-file task.md [--attempt-budget N]
omg job status|wait|collect|cancel|list …
omg job retry JOB_ID --attempt N    # exact next attempt; no auto-scheduler
omg job gc --retention-days N       # terminal jobs only
omg ask fake|agy "…" --background   # thin seam → durable job; returns job_id
```

Public start admits `fake` and `antigravity` only. Internal `grok-acp-session`
is Team/ACP-sidecar only and cannot be retried via `omg job retry`.

## Retry / attempt budget

- `attempt_budget` is immutable after start (schema v1 additive fields).
- Retry requires `--attempt` == current+1; budget exhaustion fails closed.
- Prior attempt evidence is archived under `attempts/NNNN/` (never overwritten).
- Retry classification (`automatic` | `manual_only` | `never` | `unknown`) is
  stamped for operators; **public retry remains explicit** (no auto-requeue).
- Retry refuses live runner/provider identity, ACP-internal providers, and
  request snapshots that no longer pass provider preflight.

## GC / retention

- `omg job gc --retention-days N` deletes **terminal** jobs older than retention
  (`terminal_at`, else `updated_at` / `created_at`).
- Never deletes queued/starting/running jobs.
- Refuses ACP-bound jobs; skips malformed records; re-validates under the job
  lock immediately before delete.
- Extensible hook site for future Team binding protection.

## `omg ask --background`

Creates a durable job via the existing `start_job` / runner path and returns
`job_id` immediately. Synchronous `omg ask` is unchanged by default.
Background admits `fake` and `agy` only (maps to jobs `fake` / `antigravity`).
Does not write ask artifacts or invoke a second launcher.

## Recovery / privacy

- After leader restart: `omg job status|wait|collect|cancel` against durable
  `job.json` + recorded PID/PGID (cancel is sibling-safe; see PR2 ownership).
- Large outputs stay in `artifacts/`; status/collect return descriptors only.
- Prompt context is the job `prompt.md` copy — not the full leader transcript.
- Jobs never grant `verified`.

## Open follow-ups (#68 remains open)

Automatic retry scheduling, lease reconciliation beyond current runtime, Team
worker integration (#69), and authenticated live Antigravity maturity claims
are out of scope for this PR3 slice.
