# Team operation catalog (schema v1)

Machine-readable contract for `omg team api` operation names and metadata.

Authoritative implementation: `omg_cli/team/operation_catalog.py`.
Introspection CLI: `omg team api catalog` (no `--input`, no team state, no
tmux, no `.omg`, no subprocess).

Golden freeze: `tests/golden/team_operation_catalog_v1.json`.

## Document shape

```json
{
  "kind": "omg.team.operation_catalog",
  "schema_version": 1,
  "operations": [ /* TeamOperation rows */ ]
}
```

Each operation row:

| Field | Type | Notes |
| --- | --- | --- |
| `name` | string | Stable CLI op token (`omg team api <name>`) |
| `domain` | string | `mailbox` / `task` / `config` / `worker` / `event` / `summary` / `lifecycle` / `monitor` |
| `dispatch_state` | string | `implemented` \| `reserved` \| `planned` |
| `implemented` | bool | Mirrors `dispatch_state == "implemented"` |
| `reserved` | bool | Named in catalog; returns `E_TEAM_API_UNIMPLEMENTED` until shipped |
| `planned` | bool | Future surface not yet named for dispatch |
| `mutates_state` | bool | Durable team-store mutation vs read-only |
| `worker_allowed` | bool | Pane-worker ACL; only valid when `implemented` |

Exactly one of `implemented` / `reserved` / `planned` is true.

## Derived exports

Do **not** hand-maintain parallel lists. The catalog derives:

- `TEAM_API_OPERATIONS` — all `name` values (catalog order)
- `P0_OPERATIONS` — `implemented` names
- `WORKER_ALLOWED_OPS` — implemented ∧ `worker_allowed`
- `WORKER_DENIED_OPS` — implemented ∧ ¬`worker_allowed`

`omg_cli.team.api._HANDLERS` remains the handler map. Golden tests require:

```text
{op.name for op in catalog if op.implemented} == set(_HANDLERS)
WORKER_ALLOWED_OPS ∪ WORKER_DENIED_OPS == set(P0_OPERATIONS)
WORKER_ALLOWED_OPS ∩ WORKER_DENIED_OPS == ∅
```

## Honesty

v1 freezes the current OMX-shaped **names** surface (36 ops) with the
shipped P0′ **implemented** subset (25 handlers). Reserved rows are not
parity claims. Leader-resume **task-claim reconciliation** ships through
`omg team resume` (`resume_for_identity` → `reconcile_task_claims`) and is
**not** a catalog-v1 / MCP operation — a public `omg team api reconcile`
requires a future catalog version. Issue #69 remains open for job-backed
workers, replacement attempts, Hyperplan/compositions, API/MCP breadth, and
full OMX surface parity.

```bash
omg team api catalog
```
