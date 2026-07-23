<!-- OMG:START -->
# oh-my-grok (Grok Build orchestration)

This project uses **oh-my-grok** for multi-agent workflows on Grok Build.

## Hard rules
- Fan-out **only** via Grok `spawn_subagent` (depth=1; children must NOT spawn).
- **Always** set `capability_mode` on every spawn:
  - `read-only` — explore / plan / critic / verifier
  - `read-write` — implementers (`general-purpose`, `omg-executor`)
  - never `execute` / `all`
- **If spawn is DENIED** (oh-my-grok PreToolUse / missing capability_mode):
  **RETRY IMMEDIATELY** in the same turn with the required `capability_mode`.
  Do **not** abandon multi-agent work. Do **not** switch to solo-only because of one deny.
- **Never** invoke claude/codex/omc team/agy/cursor-agent as default workers.
- State: only the **`omg` CLI** is authoritative for `passes` / `verified` under `.omg/state/`.
- Agents may write proposals under `.omg/artifacts/` only.
- Cancel with `omg cancel` (PID files) — never self-matching `pkill -f`.

## Commands
```bash
omg setup          # ensure .omg dirs + merge this fragment
omg doctor         # health checks
omg state          # active run status
omg cancel         # abort active run
omg ulw "goal"     # parallel ultrawork
omg ralph "goal"   # persistence loop
omg ralplan "goal" # plan consensus
omg workflow list  # installed repository workflows
omg capabilities  # honest configured -> verified tiers
```

## Layout
```text
.omg/
  state/runs/<run-id>/   # CLI single-writer status
  workflows/ memory/ state/recovery/
  plans/ research/ handoffs/ artifacts/ ultragoal/ wiki/
```
<!-- OMG:END -->
