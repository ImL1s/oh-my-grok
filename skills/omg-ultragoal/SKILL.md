---
name: omg-ultragoal
description: Repo-native durable multi-story goal ledger with hash-chained checkpoints and verified-only terminal state.
---

# omg-ultragoal — durable goal ledger

Use this skill for multi-story work that must survive process and run
boundaries. The `omg` CLI owns the authoritative snapshot and ledger; agents
may propose evidence files but never write goal state directly.

## Contract

- Goals live under `.omg/ultragoal/goals/<goal_id>/` (`snapshot.json` +
  `ledger.jsonl`).
- Every ledger event is hash-chained: contiguous `sequence`, `prev_hash`, and
  recomputed `event_hash`.
- Story readiness follows `depends_on`. Only ready stories may start.
- Checkpoints require a real evidence file path + SHA-256.
- Goal `verified` is allowed only when a linked run is CLI-verified.
- Agent/model files under proposals become durable only via CLI
  `proposal_imported` events — direct snapshot/ledger edits are rejected.
- Corrupt final tail: `omg goal repair --dry-run` then `--yes` (byte-for-byte
  hash-named backup first). Mid-chain/hash corruption refuses automatic
  repair and sets a forensic blocker.

## CLI

```bash
omg goal init --goal GOAL --stories-json '[{"id":"s1","depends_on":[],"acceptance":"..."}]'
omg goal status --goal GOAL
omg goal link-run --goal GOAL --run RUN
omg goal start-story --goal GOAL --story s1
omg goal checkpoint --goal GOAL --story s1 --evidence PATH --message "..."
omg goal block-story --goal GOAL --story s1 --reason "..." --next-action "..."
omg goal resume-story --goal GOAL --story s1
omg goal complete-story --goal GOAL --story s1
omg goal verify --goal GOAL
omg goal repair --goal GOAL --dry-run
omg goal repair --goal GOAL --yes
```

## Hard rules

- Do not set `verified` on goals or runs from agent prose.
- Do not truncate mid-chain damage; restore from backup / forensic path.
- Prefer linking real runs so goal verification stays coupled to CLI acceptance.
