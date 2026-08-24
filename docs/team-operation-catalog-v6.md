# Team operation catalog (schema v6)

Machine-readable contract for `omg team api` operation names and metadata.

Authoritative implementation: `omg_cli/team/operation_catalog.py`.
Introspection CLI: `omg team api catalog` (no `--input`, no team state, no
tmux, no `.omg`, no subprocess) — emits **schema v6** by default.

Golden freeze: `tests/golden/team_operation_catalog_v6.json`.
Schema v1 remains frozen at `tests/golden/team_operation_catalog_v1.json`
/ `docs/team-operation-catalog-v1.md`.
Schema v2 remains frozen at `tests/golden/team_operation_catalog_v2.json`
/ `docs/team-operation-catalog-v2.md`.
Schema v3 remains frozen at `tests/golden/team_operation_catalog_v3.json`
/ `docs/team-operation-catalog-v3.md`.
Schema v4 remains frozen at `tests/golden/team_operation_catalog_v4.json`
/ `docs/team-operation-catalog-v4.md`.
Schema v5 remains frozen at `tests/golden/team_operation_catalog_v5.json`
/ `docs/team-operation-catalog-v5.md`.

## Delta from v5

| Change | Detail |
| --- | --- |
| `schema_version` | `6` |
| `mailbox-mark-notified` | `domain=mailbox`, **implemented** (was reserved), worker-allowed (self only), mutating — notify cursor in a **separate** confined store (`notify/<recipient>/notify_cursor.json`). Mailbox schema v1 keys are unchanged. |
| `write-worker-identity` | `domain=worker`, **implemented**, leader-only, mutating — confined `identity.json` per worker |
| `await-event` | `domain=event`, **implemented**, worker-allowed, read-only — bounded read of `events.jsonl` (`timeout_ms=0` is one snapshot; timeout capped at 1000ms; no threads) |
| `read-idle-state` | `domain=summary`, **implemented**, worker-allowed, read-only — derived from heartbeat + task timestamps (no live tmux) |
| `read-stall-state` | `domain=summary`, **implemented**, worker-allowed, read-only — derived from heartbeat + task timestamps (no live tmux) |
| `cleanup` | `domain=lifecycle`, **implemented**, leader-only, mutating — distinct from `orphan-cleanup`; removes terminal team-id artifacts after shutdown ack; fail-closed if team still running or claims unexpired |
| `read-monitor-snapshot` | `domain=monitor`, **implemented**, worker-allowed, read-only |
| `write-monitor-snapshot` | `domain=monitor`, **implemented**, leader-only, mutating — schema-versioned, redacted, no secrets, no tmux probe |
| `read-task-approval` | `domain=task`, **implemented**, worker-allowed, read-only |
| `write-task-approval` | `domain=task`, **implemented**, leader-only, mutating — durable JSON per task_id; completed/failed requires `allow_terminal`; never writes OMG `verified` |

Counts: **42 named / 42 dispatched**. No new op names. v1–v5 goldens are unchanged.

Worker-allowed v6 ops: `mailbox-mark-notified` (self only), `await-event`,
`read-idle-state`, `read-stall-state`, `read-monitor-snapshot`,
`read-task-approval`. Leader-only: `write-worker-identity`, `cleanup`,
`write-monitor-snapshot`, `write-task-approval`.

**No MCP mutation** for these ops in this slice.

## Document shape

Same as v1–v5:

```json
{
  "kind": "omg.team.operation_catalog",
  "schema_version": 6,
  "operations": [ /* TeamOperation rows */ ]
}
```

## Honesty

v6 implements the remaining reserved OMX **names** on hermetic file stores.
It does **not** claim full OMX live parity, live Antigravity evidence, live
grok job smoke, host-TUI prompt-queue consume, composition live executors,
maturity promotion, `live_*`, `passes`, or `verified`. `execution_supported`
on composition compile/produce is unchanged (`false` except fixture-backed
execution receipts). Issue #69 remains open for those leftovers.

```bash
omg team api catalog
omg team api mailbox-mark-notified --input '{"run_id":"RUN","team_id":"t","worker":"w1","message_id":"msg-1"}' --json
omg team api write-worker-identity --input '{"run_id":"RUN","team_id":"t","worker":"w1","role":"executor"}' --json
omg team api await-event --input '{"run_id":"RUN","team_id":"t","timeout_ms":0}' --json
omg team api read-idle-state --input '{"run_id":"RUN","team_id":"t"}' --json
omg team api read-stall-state --input '{"run_id":"RUN","team_id":"t"}' --json
omg team api cleanup --input '{"run_id":"RUN","team_id":"t"}' --json
omg team api write-monitor-snapshot --input '{"run_id":"RUN","team_id":"t"}' --json
omg team api read-monitor-snapshot --input '{"run_id":"RUN","team_id":"t"}' --json
omg team api write-task-approval --input '{"run_id":"RUN","team_id":"t","task_id":"1","decision":"approved"}' --json
omg team api read-task-approval --input '{"run_id":"RUN","team_id":"t","task_id":"1"}' --json
```

Refs #69.
