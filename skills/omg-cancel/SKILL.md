---
name: omg-cancel
description: Cancel oh-my-grok runs safely via omg cancel and PID files. Use when user says cancel, stop omg, abort run, or kill workers.
---

# omg-cancel — Abort runs safely

Stop supervised oh-my-grok runs without killing the wrong processes. Prefer the CLI; never use self-matching `pkill -f`.

## HARD RULES (non-negotiable)
- Fan-out ONLY via Grok `spawn_subagent` (depth=1; children must NOT spawn).
- NEVER invoke claude/codex/omc team/agy/cursor-agent as default workers.
- Use Grok tool names: read_file, search_replace, run_terminal_command, spawn_subagent, grep, list_dir.
- Write-heavy work: isolation worktree + background true; wait with wait_commands_or_subagents / get_command_or_subagent_output.
- State: only omg CLI is authoritative for passes/verified; you may write proposals under .omg/artifacts/.

## Use when

- User says `cancel`, `stop omg`, `abort`, `kill workers`, `cancelomc`-style abort for oh-my-grok.
- A run is stuck, wrong branch, or user changed intent.

## Canonical cancel

```bash
omg cancel
```

Optional (when supported by CLI):

```bash
omg cancel --run <run_id>
omg state   # inspect active run before/after
```

The CLI should:

1. Read active run metadata under `.omg/state/`.
2. Signal PIDs recorded under `.omg/state/runs/` (PID files).
3. Mark run cancelled in authoritative state (CLI only).
4. Leave artifacts intact for post-mortem (do not delete unless user asks).

## PID files (layout)

Expected layout (v1):

```text
.omg/state/runs/
  <run_id>/
    run.json          # metadata (mode, goal, status)
    leader.pid        # optional
    workers/
      <name>.pid      # one PID per background worker
```

Rules for any manual fallback:

1. Prefer `omg cancel` always.
2. If CLI missing, kill by **PID file only**:

```bash
# Example — kill one worker by recorded PID
kill "$(cat .omg/state/runs/<run_id>/workers/<name>.pid)" 2>/dev/null || true
```

3. **Never** use self-matching patterns:

```bash
# FORBIDDEN — wrapper/self argv can match and suicide or get blocked
pkill -f 'omg|grok|ulw|ralph'
pkill -f "spawn_subagent"
```

4. If you must scan processes, use `ps` + `awk` with a pattern that does **not** appear verbatim in the same shell command line, or kill only exact PIDs from files.

## Session behavior after cancel

- Stop spawning new children.
- Do not continue implementing the cancelled goal.
- Summarize what was cancelled and where artifacts remain (`.omg/artifacts/`).
- Do not flip verified/passes; cancelled ≠ verified.

## Anti-patterns

- `pkill -f` with long patterns that match the cancel command itself.
- Killing unrelated `grok` / IDE sessions by name.
- Deleting `.omg/state/` wholesale without user request.
- Claiming "cancelled and verified" or clearing evidence silently.
