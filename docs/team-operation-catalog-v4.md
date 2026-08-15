# Team operation catalog (schema v4)

Machine-readable contract for `omg team api` operation names and metadata.

Authoritative implementation: `omg_cli/team/operation_catalog.py`.
Introspection CLI: `omg team api catalog` (no `--input`, no team state, no
tmux, no `.omg`, no subprocess) — historically emitted **schema v4**; the
**default catalog is now v5** (see `docs/team-operation-catalog-v5.md`).
v4 remains frozen as a golden / documentation snapshot.

Golden freeze: `tests/golden/team_operation_catalog_v4.json`.
Schema v1 remains frozen at `tests/golden/team_operation_catalog_v1.json`
/ `docs/team-operation-catalog-v1.md`.
Schema v2 remains frozen at `tests/golden/team_operation_catalog_v2.json`
/ `docs/team-operation-catalog-v2.md`.
Schema v3 remains frozen at `tests/golden/team_operation_catalog_v3.json`
/ `docs/team-operation-catalog-v3.md`.

## Delta from v3

| Change | Detail |
| --- | --- |
| `schema_version` | `4` |
| New op | `bulk-create-tasks` (`domain=task`, implemented, leader-only, mutating) |

`bulk-create-tasks` admits a bounded (1–32) intra-batch dependency DAG
atomically: prepare → reserve contiguous task IDs → write tasks with
immutable batch/task-key binding → re-read verify → commit marker last.
Uncommitted batch tasks are invisible to `read-task` / `list-tasks` /
`claim-task`. Same idempotency key + digest resumes or returns the original
mapping; key/digest conflicts fail closed. See
`omg_cli/team/task_batch.py` (`compile_task_batch_v1` /
`admit_task_batch_v1`).

Workers are denied (`worker_allowed=false`). **No MCP mutation** for this
op in this slice.

## Document shape

Same as v1–v3:

```json
{
  "kind": "omg.team.operation_catalog",
  "schema_version": 4,
  "operations": [ /* TeamOperation rows */ ]
}
```

## Honesty

v4 is a **partial** bounded catalog expansion (one leader-only mutating
batch admission op on top of v3). Hermetic / fixture-proven. Fixture-backed
composition `execute` is a **CLI/Python path** (`omg team
hyperplan|security-research execute`), **not** a catalog v5 operation.
Does **not** claim full OMX parity, live Antigravity evidence, maturity
promotion, `live_*`, `passes`, `verified`, or compile-time
`execution_supported=true`. Issue #69 remains open.

```bash
omg team api catalog
omg team api bulk-create-tasks --input '{"schema_version":1,"run_id":"RUN","team_id":"team","batch_id":"b1","idempotency_key":"k1","source":{"kind":"fixture","source_id":"s1","digest":"<sha256>"},"tasks":[...]}' --json
```

Refs #69.
