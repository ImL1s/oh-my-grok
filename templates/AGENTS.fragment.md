<!-- OMG:START -->
# oh-my-grok (Grok Build orchestration)

This project uses **oh-my-grok** for multi-agent workflows on Grok Build.

## Hard rules
- Fan-out **only** via Grok `spawn_subagent` (depth=1; children must NOT spawn).
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
```

## Layout
```text
.omg/
  state/runs/<run-id>/   # CLI single-writer status
  plans/ research/ handoffs/ artifacts/ ultragoal/
```
<!-- OMG:END -->
