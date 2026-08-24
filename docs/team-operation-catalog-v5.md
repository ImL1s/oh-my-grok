# Team operation catalog (schema v5)

Machine-readable contract for `omg team api` operation names and metadata.

Authoritative implementation: `omg_cli/team/operation_catalog.py`.
Introspection CLI: `omg team api catalog` (no `--input`, no team state, no
tmux, no `.omg`, no subprocess) — historically emitted **schema v5**; the
**default catalog is now v6** (see `docs/team-operation-catalog-v6.md`).
v5 remains frozen as a golden / documentation snapshot.

Golden freeze: `tests/golden/team_operation_catalog_v5.json`.
Schema v1 remains frozen at `tests/golden/team_operation_catalog_v1.json`
/ `docs/team-operation-catalog-v1.md`.
Schema v2 remains frozen at `tests/golden/team_operation_catalog_v2.json`
/ `docs/team-operation-catalog-v2.md`.
Schema v3 remains frozen at `tests/golden/team_operation_catalog_v3.json`
/ `docs/team-operation-catalog-v3.md`.
Schema v4 remains frozen at `tests/golden/team_operation_catalog_v4.json`
/ `docs/team-operation-catalog-v4.md`.

## Delta from v4

| Change | Detail |
| --- | --- |
| `schema_version` | `5` |
| `broadcast` | `domain=mailbox`, **implemented** (was reserved), leader-only, mutating — N durable DMs via existing `send-message` (per-recipient dedupe `key--recipient`) |
| New op | `enqueue-host-prompt` (`domain=queue`, implemented, leader-only, mutating) |
| New op | `list-host-prompt-queue` (`domain=queue`, implemented, worker-allowed, read-only) |
| New op | `reorder-host-prompt-queue` (`domain=queue`, implemented, leader-only, mutating) |

Counts: **42 named / 32 dispatched**. v1–v4 goldens are unchanged.

Host prompt-queue consume is an OMG-owned durable file
(`.omg/state/runs/<run>/team/<team>/host_prompt_queue.json`). It is **not**
the mailbox and **not** a task claim/ACK path. Host probe
`CAPABILITY_KEYS` does **not** advertise `grok.prompt_queue.*`; Team still
consumes those catalogued capabilities as a LEGACY queue. There is **no**
host-TUI prompt-queue wiring in this slice.

Workers are denied for `broadcast`, `enqueue-host-prompt`, and
`reorder-host-prompt-queue`. `list-host-prompt-queue` is worker-allowed.
**No MCP mutation** for these ops in this slice.

## Document shape

Same as v1–v4:

```json
{
  "kind": "omg.team.operation_catalog",
  "schema_version": 5,
  "operations": [ /* TeamOperation rows */ ]
}
```

## Honesty

v5 is a **partial** bounded catalog expansion (implemented broadcast + Team
host-prompt-queue consume). Hermetic / fixture-proven. Job-backed grok
workers are admitted on the durable Jobs plane (`--provider grok`,
`--worker-topology=job`). Does **not** claim full OMX parity, live
Antigravity evidence, live grok job smoke, host-TUI prompt-queue consume,
maturity promotion, `live_*`, `passes`, or `verified`. Issue #69 remains
open.

```bash
omg team api catalog
omg team api broadcast --input '{"run_id":"RUN","team_id":"t","from_worker":"leader","body":"hi"}' --json
omg team api enqueue-host-prompt --input '{"run_id":"RUN","team_id":"t","body":"next prompt"}' --json
omg team api list-host-prompt-queue --input '{"run_id":"RUN","team_id":"t"}' --json
omg team api reorder-host-prompt-queue --input '{"run_id":"RUN","team_id":"t","order":["hp-00000001","hp-00000000"]}' --json
```

Refs #69.
