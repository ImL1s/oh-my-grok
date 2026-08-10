# Team operation catalog (schema v2)

Machine-readable contract for `omg team api` operation names and metadata.

Authoritative implementation: `omg_cli/team/operation_catalog.py`.
Introspection CLI: `omg team api catalog` (no `--input`, no team state, no
tmux, no `.omg`, no subprocess) — emits **schema v2** by default.

Golden freeze: `tests/golden/team_operation_catalog_v2.json`.
Schema v1 remains frozen at `tests/golden/team_operation_catalog_v1.json`
/ `docs/team-operation-catalog-v1.md` (unchanged).

## Delta from v1

| Change | Detail |
| --- | --- |
| `schema_version` | `2` |
| New op | `replace-worker` (`domain=worker`, implemented, leader-only) |

`replace-worker` is the identity-fenced worker replacement attempt API
(#69 PR5). Workers are denied (`worker_allowed=false`). See `docs/team.md`.

## Document shape

Same as v1:

```json
{
  "kind": "omg.team.operation_catalog",
  "schema_version": 2,
  "operations": [ /* TeamOperation rows */ ]
}
```

## Honesty

v2 adds one leader-only mutation (`replace-worker`) on top of the v1 OMX-shaped
names surface. Hermetic / fixture-proven for pane + fake-job topologies.
Antigravity uses the existing provider/Jobs path structurally but has **no
live proof** in this slice. Does **not** claim full OMX parity, Hyperplan /
security compositions, or issue completion. Presentation State V1 is catalog
**v3** (`docs/team-operation-catalog-v3.md`).

```bash
omg team api catalog
omg team api replace-worker --input '{
  "run_id":"RUN","team_id":"team","worker":"t1","mode":"lost",
  "expected_attempt":1,"expected_launch_generation":1,
  "idempotency_key":"repl-1"
}'
```

Refs #69.
