# Team operation catalog (schema v3)

Machine-readable contract for `omg team api` operation names and metadata.

Authoritative implementation: `omg_cli/team/operation_catalog.py`.
Introspection CLI: `omg team api catalog` (no `--input`, no team state, no
tmux, no `.omg`, no subprocess) — historically emitted **schema v3**; the
**default catalog is now v4** (see `docs/team-operation-catalog-v4.md`).
v3 remains frozen as a golden / documentation snapshot.

Golden freeze: `tests/golden/team_operation_catalog_v3.json`.
Schema v1 remains frozen at `tests/golden/team_operation_catalog_v1.json`
/ `docs/team-operation-catalog-v1.md`.
Schema v2 remains frozen at `tests/golden/team_operation_catalog_v2.json`
/ `docs/team-operation-catalog-v2.md`.

## Delta from v2

| Change | Detail |
| --- | --- |
| `schema_version` | `3` |
| New op | `read-presentation-state` (`domain=summary`, implemented, leader-only, read-only) |

`read-presentation-state` returns Team Presentation State V1
(`docs/team-presentation-state-v1.md`). Workers are denied
(`worker_allowed=false`).

## Document shape

Same as v1/v2:

```json
{
  "kind": "omg.team.operation_catalog",
  "schema_version": 3,
  "operations": [ /* TeamOperation rows */ ]
}
```

## Honesty

v3 adds one leader-only read (`read-presentation-state`) on top of v2.
Hermetic / fixture-proven. Does **not** claim full OMX parity, Hyperplan /
security compositions, live Antigravity evidence, or issue completion.

```bash
omg team api catalog
omg team api read-presentation-state --input '{"run_id":"RUN","team_id":"team"}'
omg team status --run RUN --presentation --json
```

Refs #69.
