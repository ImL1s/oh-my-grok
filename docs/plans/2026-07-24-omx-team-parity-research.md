# OMX Team Parity — Research Snapshot (2026-07-24)

Synthesized from OMX upstream tree
`oh-my-grok/.omx/tmp/upstreams-current/oh-my-codex` and live gap checks on
omg / oma / omcu. (External agy/codex research runners returned empty in this
session; this file is the authoritative planning brief.)

## OMX MUST-HAVE (from `TEAM_API_OPERATIONS`, 33)

Lifecycle: `omx team …` / `status` / `resume` / `shutdown`

API ops:
send-message, broadcast, mailbox-list, mailbox-mark-delivered,
mailbox-mark-notified, create-task, read-task, list-tasks, update-task,
claim-task, transition-task-status, release-task-claim, read-config,
read-manifest, read-worker-status, read-worker-heartbeat,
update-worker-heartbeat, write-worker-inbox, write-worker-identity,
append-event, read-events, await-event, read-idle-state, read-stall-state,
get-summary, cleanup, orphan-cleanup, write-shutdown-request,
read-shutdown-ack, read-monitor-snapshot, write-monitor-snapshot,
read-task-approval, write-task-approval

State: `.omx/state/team/<team>/{config,manifest,tasks,mailbox,workers,dispatch}`

## Gap summary

| Surface | OMG | OMA | OMCU |
|---|---|---|---|
| tmux workers | yes (experimental gate) | yes | yes (experimental) |
| mailbox data plane | library yes; no `team api` | aggregate mailbox; no `team api` | none |
| claim lifecycle | ownership/seal oriented | internal claimTask | path exclusivity only |
| `team api` CLI | missing | missing | missing |
| inbox.md | missing | missing | missing |
| status/resume/stop | status/resume/stop | status/stop (+tick…) | status/stop |

## P0 shared subset (all three plans)

send-message, mailbox-list, mailbox-mark-delivered, create-task, list-tasks,
claim-task, transition-task-status, release-task-claim, get-summary,
write-worker-inbox (+ read-config where applicable)

## Non-goals

- Native host team products (Grok/Cursor/agy)
- Removing OMG experimental gate in P0
- Equating host `--madmax` tmux with team plane
- Full 33-op clone in first pass
