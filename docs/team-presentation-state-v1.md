# Team Presentation State V1 (#69 PR6)

Pure read-only projection of Team persisted facts into one versioned object.

Authoritative builder: `omg_cli.team.presentation.build_team_presentation_v1`.
The identical payload is exposed through:

```bash
omg team status --presentation [--json]
omg team api read-presentation-state --input '{"run_id":"RUN","team_id":"team"}'
# MCP team_status.read with projection=presentation.v1
```

Default `omg team status --json` / `--full` schemas are unchanged.

## Document shape

```json
{
  "kind": "omg.team.presentation_state",
  "schema_version": 1,
  "run_id": "…",
  "team_id": "…",
  "team_name": "…",
  "state_generation": 0,
  "lifecycle": { "dry_run": true, "workspace_mode": "worktree", "startup_status": null, "worker_topology": "pane" },
  "workspace": { "mode": "worktree" },
  "members": [ /* ordered */ ]
}
```

Each member carries logical/task/API-task ids, role, canonical capability
floor (`read-only` | `read-write` | `unknown`), route descriptor, relative
worktree + ownership state, `current_attempt`, and ordered `attempts`.

Attempt identity is `(member_id, attempt, launch_generation)` — duplicates
fail closed.

## Route descriptor

New start/scale stamps an additive route:

```json
{
  "schema": 1,
  "kind": "external_executor",
  "executor": "fixture",
  "provider": "fake",
  "role": "executor",
  "posture": "read-write"
}
```

Legacy records without `route` render `kind=unknown` (never inferred from
argv / executable names). Optional `native_host_receipt` may pass through a
validated relative `receipt_ref` + sha256 `receipt_digest` (no native
execution path in this PR). Replacement archives the prior route with
`prior_attempts` and restamps the live row.

## Fail-closed invariants

- Generation-fenced snapshot (meta + API-task versions); retry once then
  `E_TEAM_PRESENTATION_RACE`
- No tmux / Jobs / provider / network / paid probes; no state writes
- Never expose owner/claim tokens, idempotency keys, argv, prompts, env,
  credentials, absolute machine paths
- Dual execution handles refused; corrupt lineage / unsafe paths typed errors
- Never sets `verified`

## Honesty

Presentation State V1 landed under #69 PR6. Does **not** close #69
(Hyperplan / security compositions / live Antigravity / full OMX remain
open). No `live_*` maturity claims.

Refs #69.
